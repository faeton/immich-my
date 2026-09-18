"""`immy similar` — SQL shape + verdict thresholds (no DB)."""

from immy import similar


class _Conn:
    def __init__(self, rows):
        self.rows, self.calls = rows, []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        rows = self.rows
        class _Cur:
            def fetchall(self_inner):
                return rows
        return _Cur()


def test_label_thresholds():
    assert similar.label_for(0.99) == "same frame"
    assert similar.label_for(0.95) == "same frame"
    assert similar.label_for(0.90) == "same subject"
    assert similar.label_for(0.80) == "similar"


def test_search_passes_pgvector_literal_and_filters_min_sim():
    row_a = ("id-a", "/a.jpg", "a.jpg", "IMAGE", None, None, None, 0.97)
    row_b = ("id-b", "/b.mp4", "b.mp4", "VIDEO", None, "Split", "Croatia", 0.70)
    conn = _Conn([row_a, row_b])
    hits = similar.search(conn, [1.0, 0.0], limit=5, min_similarity=0.8)
    sql, params = conn.calls[0]
    assert params["v"] == "[1,0]" and params["limit"] == 5 and params["videos"] is True
    assert "ORDER BY s.embedding <=> %(v)s::vector" in sql
    assert [h.asset_id for h in hits] == ["id-a"]
    assert hits[0].label == "same frame"


def test_search_faces_labels_and_person():
    rows = [("id-x", "/x.heic", "IMAGE", None, "Ivan", 0.951), ("id-y", "/y.heic", "IMAGE", None, "Ivan", 0.82)]
    hits = similar.search_faces(_Conn(rows), [0.0, 1.0], limit=2)
    assert [h.label for h in hits] == ["same frame", "same person"]
    assert hits[0].person == "Ivan"
