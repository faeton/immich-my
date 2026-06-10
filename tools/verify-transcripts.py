#!/usr/bin/env python3
"""Dual-engine transcript verification ("double captioning") for trips.

Every `<stem>.<lang>.srt` sidecar in a trip is independently re-transcribed
by a *second* ASR engine — GigaAM-v3 for Russian (CPU, RTFx ~39), Whisper
large-v3 for everything else (GPU) — and compared word-by-word. Two engines
with unrelated failure modes agreeing means the transcript is real speech;
hard divergence means hallucination, music, mumble, or wrong language.

Files under the agreement threshold go to an LM Studio judge which rules
confirm / drop / unsure per file. Verdicts are saved to the work dir;
nothing is changed without `--apply`. With `--apply` the *saved* verdicts
are executed (never re-judged — the judge is not deterministic even at
temperature 0): dropped files lose their sidecar, the description the
transcript set is blanked in the offline sink (`synced: false`), and the
journal records a `arbiter-hallucination` skip so the worker neither
re-runs Whisper on the clip nor records it.

Insta360 twins (dual-lens `_00_`/`_10_` + LRV proxies carry identical
audio) are verified once per group; verdicts fan out to all members.

GigaAM constraint: hard 25.0 s (400 000-sample) input limit — segments are
cut at 24 s re-encoded, because `-c copy` splits land at 25.024 s and every
segment then fails. GigaAM v3 needs the GitHub install (PyPI 0.1.0 lacks
v3 weights; python ≤ 3.12 — its onnxruntime pin has no 3.14 wheels):

  python3.12 -m venv ~/.immy/venv-gigaam
  ~/.immy/venv-gigaam/bin/pip install 'gigaam @ git+https://github.com/salute-developers/GigaAM.git'

Usage:
  tools/verify-transcripts.py --trip 2024-02-peru-bolivia          # report + judge (dry)
  tools/verify-transcripts.py --trip 2024-02-peru-bolivia --apply  # execute saved verdicts
  tools/verify-transcripts.py --files list.txt                     # explicit sidecar list
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
IMMY_SRC = SCRIPT_DIR.parent / "immy" / "src"
sys.path.insert(0, str(IMMY_SRC))

import yaml  # noqa: E402

from immy import offline as offline_mod  # noqa: E402
from immy import transcripts as t  # noqa: E402
from immy.journal import Journal, transcript_version  # noqa: E402
from immy.process import container_path_for, path_checksum  # noqa: E402

TRIPS_ROOT = Path(os.environ.get("TRIPS_ROOT", str(Path.home() / "Media" / "Trips")))
TWIN_PAT = re.compile(r"^(VID|LRV)_(\d{8}_\d{6})_\d{2}_(\d+)$")
WORD_RE = re.compile(r"[\w'’-]+", re.UNICODE)
SRT_SUFFIXES = (".srt", ".xmp", ".json", ".vtt")
# GigaAM rejects inputs over exactly 25.0 s; 24 s re-encoded stays under.
GIGAAM_SEGMENT_SECONDS = "24"

GIGAAM_WORKER = r"""
import json, sys
from pathlib import Path
import gigaam
model = gigaam.load_model("v3_e2e_rnnt", device="cpu")
plan = json.loads(Path(sys.argv[1]).read_text())
for key, seg_dir in plan.items():
    parts = []
    for seg in sorted(Path(seg_dir).glob("*.wav")):
        try:
            text = str(model.transcribe(str(seg))).strip()
        except Exception as e:
            print(json.dumps({"warn": f"{seg.name}: {e}"}), flush=True)
            text = ""
        if text:
            parts.append(text)
    print(json.dumps({"key": key, "text": " ".join(parts)}, ensure_ascii=False),
          flush=True)
"""


def norm_words(text: str) -> list[str]:
    return [w.lower().replace("ё", "е") for w in WORD_RE.findall(text)]


def find_sidecars(args) -> list[dict]:
    """[{media, srt, lang, trip}] for every video sidecar in scope."""
    rows = []
    if args.files:
        srts = [Path(line.strip()) for line in Path(args.files).read_text().splitlines()
                if line.strip()]
    else:
        trips = ([TRIPS_ROOT / args.trip] if args.trip
                 else [d for d in sorted(TRIPS_ROOT.iterdir()) if d.is_dir()])
        srts = [s for trip in trips for s in trip.rglob("*.srt")
                if len(s.suffixes) >= 2]
    for srt in srts:
        stem = srt.name.rsplit(".", 2)[0]
        lang = srt.suffixes[-2].lstrip(".")
        media = next((m for m in sorted(srt.parent.glob(f"{stem}.*"))
                      if m.suffix.lower() not in SRT_SUFFIXES), None)
        if media is None:
            continue
        trip = media
        while trip.parent != TRIPS_ROOT and trip.parent != trip:
            trip = trip.parent
        rows.append({"media": str(media), "srt": str(srt), "lang": lang,
                     "trip": str(trip)})
    return rows


def twin_key(media: Path) -> tuple:
    m = TWIN_PAT.match(media.stem)
    if m:
        return (str(media.parent), m.group(2), m.group(3))
    return (str(media.parent), media.stem, "")


def second_opinions(rows: list[dict], work: Path, gigaam_python: str) -> dict:
    """{media: text} from the independent engine (gigaam ru / whisper other)."""
    out: dict[str, str] = {}
    second_dir = work / "second"
    second_dir.mkdir(parents=True, exist_ok=True)
    gigaam_plan: dict[str, str] = {}
    for n, row in enumerate(rows):
        cache = second_dir / f"{n:03d}.txt"
        row["_cache"] = cache
        if cache.is_file():
            out[row["media"]] = cache.read_text(encoding="utf-8")
            continue
        wav = work / f"{n:03d}.wav"
        if not wav.is_file():
            r = subprocess.run(
                ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                 "-i", row["media"], "-vn", "-ac", "1", "-ar", "16000",
                 "-c:a", "pcm_s16le", str(wav)],
                capture_output=True, text=True)
            if r.returncode != 0:
                print(f"  ffmpeg failed: {row['media']}", flush=True)
                continue
        if row["lang"] == "ru":
            seg_dir = work / f"{n:03d}-seg"
            if not seg_dir.is_dir():
                seg_dir.mkdir()
                subprocess.run(
                    ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                     "-i", str(wav), "-f", "segment",
                     "-segment_time", GIGAAM_SEGMENT_SECONDS,
                     "-c:a", "pcm_s16le", str(seg_dir / "%04d.wav")],
                    capture_output=True, text=True)
            gigaam_plan[row["media"]] = str(seg_dir)
        else:
            import mlx_whisper
            r = mlx_whisper.transcribe(
                str(wav), path_or_hf_repo=t.DEFAULT_MODEL,
                condition_on_previous_text=False, word_timestamps=True,
                hallucination_silence_threshold=2.0, language=row["lang"])
            text = str(r.get("text") or "").strip()
            cache.write_text(text, encoding="utf-8")
            out[row["media"]] = text
            print(f"  whisper 2nd: {Path(row['media']).name} ({len(text)} chars)",
                  flush=True)
    if gigaam_plan:
        plan_file = work / "gigaam-plan.json"
        plan_file.write_text(json.dumps(gigaam_plan))
        by_media = {r["media"]: r for r in rows}
        proc = subprocess.Popen(
            [gigaam_python, "-c", GIGAAM_WORKER, str(plan_file)],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        for line in proc.stdout:
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "warn" in msg:
                print(f"  gigaam: {msg['warn']}", flush=True)
                continue
            row = by_media[msg["key"]]
            row["_cache"].write_text(msg["text"], encoding="utf-8")
            out[msg["key"]] = msg["text"]
            print(f"  gigaam 2nd: {Path(msg['key']).name} "
                  f"({len(msg['text'])} chars)", flush=True)
        proc.wait()
    return out


def score(rows: list[dict], seconds: dict) -> list[dict]:
    scored = []
    for row in rows:
        if row["media"] not in seconds:
            continue
        qwen_text = t.srt_to_plaintext(
            Path(row["srt"]).read_text(encoding="utf-8"))
        a, b = norm_words(qwen_text), norm_words(seconds[row["media"]])
        if not a and not b:
            seq = 1.0
        elif not a or not b:
            seq = 0.0
        else:
            seq = difflib.SequenceMatcher(None, a, b).ratio()
        scored.append({**row, "seq": round(seq, 3),
                       "words": f"{len(a)}/{len(b)}"})
    scored.sort(key=lambda r: r["seq"])
    return scored


def ask_judge(row: dict, primary: str, second: str, judge_model: str,
              lms_url: str) -> dict:
    second_note = (
        "GigaAM (Russian-only; it CANNOT transcribe English speech, so on "
        "mixed-language clips its text being short does not mean the other "
        "transcript is wrong)" if row["lang"] == "ru"
        else "Whisper large-v3 (multilingual; prone to repetition loops)")
    prompt = f"""Two speech-recognition engines transcribed the same home travel video ({Path(row['media']).name}, detected language: {row['lang']}). Their word-level agreement is low ({row['seq']}).

Transcript A (the candidate to keep):
---
{primary[:1800] or '(empty)'}
---

Transcript B (second opinion from {second_note}):
---
{second[:1800] or '(empty)'}
---

Decide whether Transcript A is real speech worth keeping as the video's subtitles, or ASR hallucination (repeated filler loops, boilerplate, text invented from noise/music/wind). Disagreement alone is not grounds to drop A — judge A's internal coherence and plausibility as casual speech of travelers (Russian/English mix is normal, swearing is normal, exclamations like "Ой"/"Wow" during activities are normal). Dropping a transcript that contains real conversation is worse than keeping a noisy one: prefer "unsure" unless A is dominated by loops/boilerplate.

Answer with ONLY a JSON object: {{"verdict": "confirm"|"drop"|"unsure", "reason": "<one sentence>"}}"""
    body = json.dumps({"model": judge_model,
                       "messages": [{"role": "user", "content": prompt}],
                       "temperature": 0, "max_tokens": 200}).encode()
    req = urllib.request.Request(
        f"{lms_url}/chat/completions", data=body,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as resp:
        out = json.loads(resp.read())["choices"][0]["message"]["content"]
    m = re.search(r"\{.*\}", out, re.S)
    return (json.loads(m.group(0)) if m
            else {"verdict": "unsure", "reason": f"unparseable: {out[:80]}"})


def apply_drop(row: dict) -> None:
    media = Path(row["media"])
    srt = Path(row["srt"])
    if srt.is_file():
        srt.unlink()
    trip = Path(row["trip"])
    root = offline_mod.derive_container_root_from_marker(trip)
    if root is None:
        print(f"  ! no container root for {trip.name}; journal/desc untouched",
              flush=True)
        return
    cs = path_checksum(container_path_for(media, trip, root)).hex()
    off = trip / ".audit" / "offline" / f"{cs}.yml"
    if off.is_file():
        data = yaml.safe_load(off.read_text()) or {}
        exif = data.setdefault("exif", {})
        desc = (exif.get("description") or "").strip()
        if desc and not desc.startswith("AI: "):
            exif["description"] = ""
            data["synced"] = False
            off.write_text(yaml.safe_dump(data, sort_keys=False,
                                          allow_unicode=True))
    journal = Journal.load(trip)
    journal.mark_done(
        cs, "transcript", transcript_version(t.DEFAULT_MODEL),
        meta={"skipped": "arbiter-hallucination",
              "reason": row.get("reason", "")[:200]})
    journal.flush()
    print(f"  dropped {media.name}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--trip", help="single trip folder name under TRIPS_ROOT")
    ap.add_argument("--files", help="text file with explicit .srt paths")
    ap.add_argument("--threshold", type=float, default=0.4,
                    help="agreement below this goes to the judge (default 0.4)")
    ap.add_argument("--work", default=str(Path.home() / ".immy" / "verify-transcripts"),
                    help="work dir for wavs/segments/verdicts (resume-safe)")
    ap.add_argument("--gigaam-python",
                    default=str(Path.home() / ".immy" / "venv-gigaam" / "bin" / "python"))
    ap.add_argument("--judge-model", default="gemma-4-31b-it")
    ap.add_argument("--lms-url", default="http://localhost:1234/v1")
    ap.add_argument("--apply", action="store_true",
                    help="execute saved verdicts (drops); without it: dry run")
    args = ap.parse_args()

    work = Path(args.work)
    work.mkdir(parents=True, exist_ok=True)
    verdicts_file = work / "verdicts.json"

    rows = find_sidecars(args)
    # one verification per Insta360 twin group; verdicts fan out on apply
    groups: dict[tuple, list[dict]] = {}
    for row in rows:
        groups.setdefault(twin_key(Path(row["media"])), []).append(row)
    primaries = [members[0] for members in groups.values()]
    print(f"{len(rows)} sidecars, {len(primaries)} unique audio tracks",
          flush=True)

    if args.apply:
        if not verdicts_file.is_file():
            sys.exit("no saved verdicts — run without --apply first")
        verdicts = json.loads(verdicts_file.read_text())
        dropped = 0
        for v in verdicts:
            if v["verdict"] != "drop":
                continue
            for member in groups.get(twin_key(Path(v["media"])), [v]):
                apply_drop({**member, "reason": v.get("reason", "")})
                dropped += 1
        print(f"applied: {dropped} sidecars dropped (incl. twins)", flush=True)
        return

    seconds = second_opinions(primaries, work, args.gigaam_python)
    scored = score(primaries, seconds)
    report = work / "report.md"
    with report.open("w") as f:
        f.write("| seq | words A/B | lang | file |\n|--|--|--|--|\n")
        for r in scored:
            f.write(f"| {r['seq']} | {r['words']} | {r['lang']} | "
                    f"{Path(r['media']).name} |\n")
    med = scored[len(scored) // 2]["seq"] if scored else "-"
    low = [r for r in scored if r["seq"] < args.threshold]
    print(f"\n{len(scored)} scored | median {med} | {len(low)} below "
          f"{args.threshold} -> judging", flush=True)

    verdicts = []
    for row in low:
        primary = t.srt_to_plaintext(Path(row["srt"]).read_text(encoding="utf-8"))
        try:
            v = ask_judge(row, primary, seconds[row["media"]],
                          args.judge_model, args.lms_url)
        except Exception as e:
            v = {"verdict": "unsure", "reason": f"judge error: {e}"}
        clean = {k: row[k] for k in ("media", "srt", "lang", "trip", "seq")}
        verdicts.append({**clean, **v})
        print(f"  {Path(row['media']).name} (seq={row['seq']}): {v['verdict']}"
              f" — {v.get('reason', '')[:100]}", flush=True)
    verdicts_file.write_text(json.dumps(verdicts, ensure_ascii=False, indent=1))
    counts: dict[str, int] = {}
    for v in verdicts:
        counts[v["verdict"]] = counts.get(v["verdict"], 0) + 1
    print(f"\nreport: {report}\nverdicts: {verdicts_file} {counts or ''}\n"
          f"REVIEW the drops (judge over-drops files with one bad line), edit "
          f"verdicts.json if needed, then re-run with --apply", flush=True)


if __name__ == "__main__":
    main()
