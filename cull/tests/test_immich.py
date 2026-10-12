from cull.immich import Immich


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, args):
        self.conn.calls.append((sql, args))

    def fetchall(self):
        return [(p,) for p in self.conn.rows]


class FakeConn:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.calls = []

    def cursor(self):
        return FakeCursor(self)


def test_video_wall_clock_compares_local_datetime_and_duration():
    c = FakeConn(["/mnt/external/originals/x/DJI_0303.MP4"])
    item = {"kind": "video", "time": "2023-07-01T10:00:00", "time_kind": "wall", "utc": None, "duration": 61.4}
    assert Immich(c).candidates(item) == ["/mnt/external/originals/x/DJI_0303.MP4"]
    sql, args = c.calls[0]
    assert '"localDateTime" at time zone' in sql and "duration" in sql and "VIDEO" in sql
    assert args == {"t": "2023-07-01T10:00:00", "w": 5, "d": 61400}


def test_photo_instant_compares_file_created_and_size():
    c = FakeConn([])
    item = {"kind": "photo", "time": "2023-07-01T12:00:00", "time_kind": "instant", "utc": "2023-07-01T10:00:00Z",
            "duration": None, "primary_size": 12345}
    assert Immich(c).candidates(item) == []
    sql, args = c.calls[0]
    assert '"fileCreatedAt"' in sql and '"fileSizeInByte"' in sql
    assert args == {"t": "2023-07-01T10:00:00Z", "w": 2, "s": 12345}


def test_guessed_times_are_not_matched():
    c = FakeConn(["x"])
    assert Immich(c).candidates({"kind": "video", "time": "2023-07-01T10:00:00", "time_kind": "guess"}) == []
    assert c.calls == []


def test_wall_clock_prefilter_is_a_date_range_so_midnight_matches_are_kept():
    c = FakeConn([])
    Immich(c).candidates({"kind": "video", "time": "2023-07-01T23:59:58", "time_kind": "wall", "duration": 2.0})
    sql, _ = c.calls[0]
    assert "between" in sql and "::date" in sql


def test_video_without_duration_is_not_queried():
    for dur in (None, 0):
        c = FakeConn(["x"])
        assert Immich(c).candidates({"kind": "video", "time": "2023-07-01T10:00:00", "time_kind": "wall",
                                     "duration": dur}) == []
        assert c.calls == []


def test_instant_without_utc_is_not_queried():
    c = FakeConn(["x"])
    assert Immich(c).candidates({"kind": "photo", "time": "2023-07-01T12:00:00", "time_kind": "instant",
                                 "utc": None, "primary_size": 1}) == []
    assert c.calls == []


def test_existing_paths():
    c = FakeConn(["/a"])
    assert Immich(c).existing_paths(["/a", "/b"]) == {"/a"}
    assert c.calls[0][1] == {"p": ["/a", "/b"]}
    assert Immich(FakeConn()).existing_paths([]) == set()
