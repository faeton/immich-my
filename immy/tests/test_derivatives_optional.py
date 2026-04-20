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
