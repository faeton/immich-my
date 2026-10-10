import struct

from teslacam import sei


def _varint(n: int) -> bytes:
    out = b""
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out += bytes([b | 0x80])
        else:
            return out + bytes([b])


def _pb(fields: dict[int, object]) -> bytes:
    out = b""
    for f, v in fields.items():
        if isinstance(v, float):
            out += _varint((f << 3) | 1) + struct.pack("<d", v)
        elif isinstance(v, int):
            out += _varint((f << 3) | 0) + _varint(v)
        else:
            out += _varint((f << 3) | 2) + _varint(len(v)) + v
    return out


def _sei_nal(payload: bytes) -> bytes:
    # NAL type 6, Tesla header: [0x06, 0x05, 0x42...(size), 0x69] payload [0x80]
    body = payload.replace(b"\x00\x00", b"\x00\x00\x03")   # add emulation prevention
    return bytes([0x06, 0x05, 0x42, 0x42, 0x42, 0x69]) + body + b"\x80"


def _mp4(nals: list[bytes]) -> bytes:
    mdat = b"".join(struct.pack(">I", len(n)) + n for n in nals)
    ftyp = struct.pack(">I", 16) + b"ftypisom" + b"\x00\x00\x02\x00"
    return ftyp + struct.pack(">I", 8 + len(mdat)) + b"mdat" + mdat


def test_parse_fields_and_median(tmp_path):
    samples = [_pb({1: 1, 4: 15.5, 5: 40, 11: 37.17 + i * 0.001, 12: -3.59, 13: 90.0, 14: 0.1}) for i in range(5)]
    garbage = _pb({11: 0.0, 12: 0.0})
    other = bytes([0x01, 0x00, 0x00, 0x00])        # a slice NAL, must be ignored
    p = tmp_path / "x.mp4"
    p.write_bytes(_mp4([other] + [_sei_nal(s) for s in samples] + [_sei_nal(garbage)]))
    track = sei.sei_track(p)
    assert len(track) == 6
    assert track[0]["accel_pedal_pct"] == 40 and track[0]["accel_x"] == 0.1
    assert abs(track[0]["speed_mps"] - 15.5) < 1e-9
    fix = sei.median_fix(track)
    assert fix["fixes"] == 5 and abs(fix["lat"] - 37.172) < 1e-6 and fix["lon"] == -3.59
    assert sei.last_fix(track)["lat"] == track[4]["lat"]


def test_emulation_prevention_stripped():
    assert sei.strip_emulation_prevention(b"\x00\x00\x03\x01\x00\x00\x03") == b"\x00\x00\x01\x00\x00"


def test_no_mdat_yields_nothing(tmp_path):
    p = tmp_path / "x.mp4"
    p.write_bytes(b"\x00\x00\x00\x08free")
    assert sei.sei_track(p) == []
