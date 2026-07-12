"""Tests for `immy/immich.py`'s `ImmichClient.upsert_tags` — specifically the
hierarchical-tag response-keying bug found 2026-07-12: a full-library
`tags sync --write` run reported success while `tag_asset` row counts never
moved, because `upsert_tags` keyed its output by the API's `name` field
(the tag's LEAF segment for a hierarchical tag) instead of `value` (the full
path, which is what every caller actually looks up by)."""

from __future__ import annotations

from immy.immich import ImmichClient


def _client(monkeypatch, response):
    client = ImmichClient(url="http://immich", api_key="key")
    monkeypatch.setattr(client, "_request", lambda *a, **k: response)
    return client


def test_upsert_tags_keys_by_value_for_hierarchical_tag(monkeypatch):
    # Real shape confirmed live against `GET /api/tags`: a hierarchical tag's
    # `name` is just its leaf segment; `value` is the full requested path.
    client = _client(monkeypatch, [
        {"id": "id-1", "parentId": "p", "name": "DJI FC8282",
         "value": "Gear/Camera/DJI FC8282"},
    ])
    ids = client.upsert_tags(["Gear/Camera/DJI FC8282"])
    assert ids == {"Gear/Camera/DJI FC8282": "id-1"}


def test_upsert_tags_keys_flat_tag_by_value_too(monkeypatch):
    client = _client(monkeypatch, [
        {"id": "id-2", "name": "post-edited", "value": "post-edited"},
    ])
    ids = client.upsert_tags(["post-edited"])
    assert ids == {"post-edited": "id-2"}


def test_upsert_tags_falls_back_to_name_if_value_missing(monkeypatch):
    client = _client(monkeypatch, [{"id": "id-3", "name": "flat-only"}])
    ids = client.upsert_tags(["flat-only"])
    assert ids == {"flat-only": "id-3"}


def test_upsert_tags_falls_back_to_name_if_value_empty_string(monkeypatch):
    client = _client(
        monkeypatch, [{"id": "id-4", "name": "flat-only", "value": ""}])
    ids = client.upsert_tags(["flat-only"])
    assert ids == {"flat-only": "id-4"}


def test_upsert_tags_multiple_hierarchical_and_flat(monkeypatch):
    client = _client(monkeypatch, [
        {"id": "id-1", "name": "DJI FC8282", "value": "Gear/Camera/DJI FC8282"},
        {"id": "id-2", "name": "Insta360", "value": "Gear/Camera/Insta360"},
        {"id": "id-3", "name": "post-edited", "value": "post-edited"},
    ])
    ids = client.upsert_tags(
        ["Gear/Camera/DJI FC8282", "Gear/Camera/Insta360", "post-edited"])
    assert ids == {
        "Gear/Camera/DJI FC8282": "id-1",
        "Gear/Camera/Insta360": "id-2",
        "post-edited": "id-3",
    }


def test_upsert_tags_empty_names_no_request(monkeypatch):
    called = []
    client = ImmichClient(url="http://immich", api_key="key")
    monkeypatch.setattr(client, "_request", lambda *a, **k: called.append(1))
    assert client.upsert_tags([]) == {}
    assert called == []
