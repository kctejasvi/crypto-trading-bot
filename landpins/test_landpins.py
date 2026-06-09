"""Tests for landpins. Run: python -m pytest landpins/test_landpins.py
(or plain: python landpins/test_landpins.py)"""
import json
from pathlib import Path

import pytest

import landpins as lp


def write_csv(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "in.csv"
    p.write_text(text, encoding="utf-8")
    return p


GOOD = """site_number,lat,lon,label,notes
Site 13,13.0362,77.5976,Site No. 13,corner plot
Site 14,13.0364,77.5979,Site No. 14,
"""


def test_reads_and_validates(tmp_path):
    pins = lp.read_pins(write_csv(tmp_path, GOOD))
    assert [p.site_number for p in pins] == ["Site 13", "Site 14"]
    assert pins[0].notes == "corner plot"
    assert pins[0].label == "Site No. 13"


def test_label_defaults_to_site_number(tmp_path):
    csv = "site_number,lat,lon\nSite 9,13.03,77.59\n"
    pins = lp.read_pins(write_csv(tmp_path, csv))
    assert pins[0].label == "Site 9"


def test_extra_columns_become_properties(tmp_path):
    csv = "site_number,lat,lon,khata\nSite 1,13.03,77.59,ABC123\n"
    pins = lp.read_pins(write_csv(tmp_path, csv))
    assert pins[0].extra["khata"] == "ABC123"
    assert pins[0].properties["khata"] == "ABC123"


def test_comment_and_blank_lines_skipped(tmp_path):
    csv = "# a note\n\nsite_number,lat,lon\n# inline\nSite 1,13.03,77.59\n"
    pins = lp.read_pins(write_csv(tmp_path, csv))
    assert len(pins) == 1


def test_missing_required_column(tmp_path):
    with pytest.raises(lp.InputError, match="missing required column"):
        lp.read_pins(write_csv(tmp_path, "site_number,lat\nSite 1,13.03\n"))


def test_non_numeric_coord(tmp_path):
    with pytest.raises(lp.InputError, match="not a number"):
        lp.read_pins(write_csv(tmp_path, "site_number,lat,lon\nA,foo,77.5\n"))


def test_zero_zero_rejected(tmp_path):
    with pytest.raises(lp.InputError, match="placeholder"):
        lp.read_pins(write_csv(tmp_path, "site_number,lat,lon\nA,0,0\n"))


def test_out_of_range_lat(tmp_path):
    with pytest.raises(lp.InputError, match="out of range"):
        lp.read_pins(write_csv(tmp_path, "site_number,lat,lon\nA,99,77.5\n"))


def test_empty_site_number(tmp_path):
    with pytest.raises(lp.InputError, match="empty site_number"):
        lp.read_pins(write_csv(tmp_path, "site_number,lat,lon\n ,13.03,77.59\n"))


def test_bbox_warning_does_not_fail(tmp_path, capsys):
    # Valid coords but far from Bengaluru -> warning, not error.
    pins = lp.read_pins(write_csv(tmp_path, "site_number,lat,lon\nA,28.6,77.2\n"))
    assert len(pins) == 1
    assert "outside the Bengaluru" in capsys.readouterr().err


def test_geojson_structure(tmp_path):
    pins = lp.read_pins(write_csv(tmp_path, GOOD))
    fc = json.loads(lp.to_geojson(pins, "t"))
    assert fc["type"] == "FeatureCollection"
    # GeoJSON is [lon, lat] order — guard against accidental swap.
    assert fc["features"][0]["geometry"]["coordinates"] == [77.5976, 13.0362]


def test_kml_contains_placemark(tmp_path):
    pins = lp.read_pins(write_csv(tmp_path, GOOD))
    kml = lp.to_kml(pins, "t")
    assert "<Placemark>" in kml
    assert "77.5976,13.0362,0" in kml  # KML is also lon,lat


def test_build_writes_all_formats(tmp_path):
    inp = write_csv(tmp_path, GOOD)
    out = tmp_path / "build" / "pins"
    written = lp.build(inp, out, title="t", formats=["geojson", "kml", "html"])
    assert {p.suffix for p in written} == {".geojson", ".kml", ".html"}
    assert all(p.exists() and p.stat().st_size > 0 for p in written)


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
