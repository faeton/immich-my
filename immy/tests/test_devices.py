"""Tests for capture-device make/model resolution."""

from __future__ import annotations

from immy import devices


def test_standard_exif_passes_through():
    assert devices.resolve("Canon", "Canon EOS R6") == ("Canon", "Canon EOS R6")
    assert devices.resolve("SONY", "ILCE-7M3") == ("SONY", "ILCE-7M3")


def test_dji_still_module_code_mapped():
    assert devices.resolve("DJI", "FC8482") == ("DJI", "DJI Mini 4 Pro")
    assert devices.resolve("DJI", "FC9313") == ("DJI", "DJI Mini 5 Pro")
    assert devices.resolve("DJI", "FC3582") == ("DJI", "DJI Mini 3 Pro")
    assert devices.resolve(None, "L2D-20c") == ("DJI", "DJI Mavic 3")
    assert devices.resolve(None, "AC002") == ("DJI", "DJI Osmo Action 3")
    # FC4170 (Mavic 3 tele) vs L2D-20c (Mavic 3 Hasselblad wide) — same drone.
    assert devices.resolve(None, "FC4170") == ("DJI", "DJI Mavic 3 Tele")


def test_dji_code_lookup_case_insensitive():
    assert devices.resolve(None, "fc8482") == ("DJI", "DJI Mini 4 Pro")


def test_dji_video_encoder_fallback():
    # DJI video: Make/Model empty, real model in the Encoder atom.
    assert devices.resolve(None, None, "DJIMavic3Cine") == ("DJI", "DJI Mavic 3 Cine")
    assert devices.resolve(None, None, "DJI Mini4 Pro") == ("DJI", "DJI Mini 4 Pro")


def test_generic_encoder_ignored():
    # Exported/transcoded clips carry a muxer string — not a device.
    assert devices.resolve(None, None, "Lavf56.15.102") == (None, None)
    assert devices.resolve(None, None, "libavformat") == (None, None)
    assert not devices.is_device_encoder("Lavf58.29.100")
    assert devices.is_device_encoder("DJIMavic3Cine")


def test_explicit_model_beats_encoder():
    # A real Make/Model wins; the Encoder is only a fallback.
    assert devices.resolve("DJI", "FC8482", "Lavf56.15.102") == \
        ("DJI", "DJI Mini 4 Pro")


def test_unmapped_dji_code_still_sets_make():
    make, model = devices.resolve(None, "FC9999")
    assert make == "DJI" and model == "FC9999"


def test_empty_in_empty_out():
    assert devices.resolve(None, None, None) == (None, None)
    assert devices.resolve("", "  ", "") == (None, None)
