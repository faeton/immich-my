from __future__ import annotations

from pathlib import Path

import pytest

from immy import derivatives as derivatives_mod


def test_compute_raises_clear_error_when_pyvips_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    trip = tmp_path / "trip"
    trip.mkdir()
    src = trip / "IMG_0001.JPG"
    src.write_bytes(b"x")

    monkeypatch.setattr(derivatives_mod, "pyvips", None)
    monkeypatch.setattr(
        derivatives_mod,
        "_PYVIPS_IMPORT_ERROR",
        OSError("libvips.42.dylib missing"),
    )

    with pytest.raises(RuntimeError, match="pyvips/libvips is unavailable"):
        derivatives_mod.compute_for_asset(
            source_media=src,
            asset_id="abcd1234-ffff-4000-8000-000000000000",
            owner_id="owner-1",
            asset_type="IMAGE",
            trip_folder=trip,
        )


def test_save_kwargs_uses_keep_none_on_newer_libvips():
    class Vips:
        @staticmethod
        def at_least_libvips(major: int, minor: int) -> bool:
            assert (major, minor) == (8, 15)
            return True

    assert derivatives_mod._save_kwargs(Vips) == {"keep": "none"}


def test_save_kwargs_falls_back_to_strip_on_older_libvips():
    class Vips:
        @staticmethod
        def at_least_libvips(major: int, minor: int) -> bool:
            return False

    assert derivatives_mod._save_kwargs(Vips) == {"strip": True}
