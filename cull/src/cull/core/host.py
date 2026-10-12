"""`--host`: loopback by default, `tailscale` for this machine's tailnet address."""

from __future__ import annotations

import ipaddress
import re
import shutil
import subprocess
from pathlib import Path
from typing import Callable

CGNAT = ipaddress.ip_network("100.64.0.0/10")
MAC_CLI = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"


class HostError(Exception):
    pass


def _run(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def interfaces() -> list[tuple[str, str]]:
    """(interface, IPv4) pairs: `ip -4 -o addr` on Linux, `ifconfig` elsewhere."""
    if shutil.which("ip"):
        out = _run(["ip", "-4", "-o", "addr", "show"])
        return [(m.group(1), m.group(2)) for m in re.finditer(r"^\d+:\s+(\S+)\s+inet\s+([\d.]+)/", out, re.M)]
    pairs: list[tuple[str, str]] = []
    cur = None
    for line in _run(["ifconfig"]).splitlines():
        m = re.match(r"^(\S+?):\s+flags", line)
        if m:
            cur = m.group(1)
            continue
        m = re.match(r"^\s+inet\s+([\d.]+)", line)
        if m and cur:
            pairs.append((cur, m.group(1)))
    return pairs


def tailscale_cli_ip() -> str | None:
    """`tailscale ip -4` (macOS, where the interface is a utunN)."""
    for exe in (shutil.which("tailscale"), MAC_CLI if Path(MAC_CLI).exists() else None):
        if exe:
            lines = _run([exe, "ip", "-4"]).split()
            if lines:
                return lines[0]
    return None


def _in_cgnat(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip) in CGNAT
    except ValueError:
        return False


def resolve_host(spec: str, *, ifaces: list[tuple[str, str]] | None = None,
                 cli: Callable[[], str | None] | None = None) -> str:
    spec = (spec or "").strip()
    if spec in ("", "0.0.0.0", "::"):
        raise HostError("refusing to listen on every interface; use 127.0.0.1, tailscale or one address")
    if spec != "tailscale":
        return spec
    pairs = interfaces() if ifaces is None else ifaces
    hits = sorted({ip for name, ip in pairs if name.startswith("tailscale") and _in_cgnat(ip)})
    if not hits:
        ip = (cli or tailscale_cli_ip)()
        if ip and _in_cgnat(ip):
            hits = [ip]
    if len(hits) != 1:
        raise HostError(f"expected one Tailscale address, found {', '.join(hits) if hits else 'none'}")
    return hits[0]
