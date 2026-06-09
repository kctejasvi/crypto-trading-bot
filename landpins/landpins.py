#!/usr/bin/env python3
"""landpins — turn a CSV of site/survey numbers + verified coordinates into map pins.

This tool does NOT invent or look up coordinates. It only renders coordinates
that YOU provide (e.g. exported/read off an authoritative cadastral source such
as Karnataka Dishaank, Bhu-Naksha, or BBMP e-Aasthi). Garbage in = garbage out,
so it validates aggressively and refuses obviously-bogus input.

Outputs:
  * <out>.geojson  — RFC 7946 FeatureCollection (Leaflet / QGIS / Mapbox / etc.)
  * <out>.kml      — OGC KML (Google Earth / Google My Maps import)
  * <out>.html     — standalone interactive Leaflet map (no build step, no keys)

Usage:
  python landpins.py INPUT.csv --out build/hebbal --title "Hebbal site pins"
  python landpins.py INPUT.csv --formats geojson,kml

CSV columns (header row required; order doesn't matter; extras kept as properties):
  site_number   (required)  e.g. "Site 42", "Sy.No 13/2"
  lat           (required)  decimal degrees, WGS84  e.g. 13.0358
  lon           (required)  decimal degrees, WGS84  e.g. 77.5970
  label         (optional)  display name; defaults to site_number
  notes         (optional)  free text shown in the popup
  <anything>    (optional)  carried through into properties / popup
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import sys
import xml.sax.saxutils as sx
from dataclasses import dataclass, field
from pathlib import Path

# Rough WGS84 bounding box for Bengaluru. Pins outside this are *probably* a
# swapped lat/lon or a typo, so we warn (use --no-bbox-check to silence).
BLR_BBOX = (12.7, 77.3, 13.2, 77.9)  # (min_lat, min_lon, max_lat, max_lon)

RESERVED = {"site_number", "lat", "lon", "label", "notes"}


@dataclass
class Pin:
    site_number: str
    lat: float
    lon: float
    label: str
    notes: str = ""
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def properties(self) -> dict[str, str]:
        props = {"site_number": self.site_number, "label": self.label}
        if self.notes:
            props["notes"] = self.notes
        props.update(self.extra)
        return props


class InputError(Exception):
    """Raised on malformed / suspicious input so the caller can report cleanly."""


def _to_float(value: str, field_name: str, row_no: int) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        raise InputError(f"row {row_no}: {field_name!r} is not a number: {value!r}")


def read_pins(path: Path, *, bbox_check: bool = True) -> list[Pin]:
    """Parse + validate the CSV into Pin objects. Raises InputError on bad data."""
    with path.open(newline="", encoding="utf-8-sig") as fh:
        # Drop blank lines and '#' comment lines before the header is detected,
        # so templates can carry instructions inline.
        rows = (line for line in fh if line.strip() and not line.lstrip().startswith("#"))
        reader = csv.DictReader(rows)
        if reader.fieldnames is None:
            raise InputError("CSV is empty (no header row)")
        cols = {c.strip().lower(): c for c in reader.fieldnames}
        for required in ("site_number", "lat", "lon"):
            if required not in cols:
                raise InputError(
                    f"missing required column {required!r}; found: {reader.fieldnames}"
                )

        pins: list[Pin] = []
        warnings: list[str] = []
        seen: dict[str, int] = {}
        for i, row in enumerate(reader, start=2):  # row 1 is the header
            site = str(row[cols["site_number"]]).strip()
            if not site:
                raise InputError(f"row {i}: empty site_number")
            lat = _to_float(row[cols["lat"]], "lat", i)
            lon = _to_float(row[cols["lon"]], "lon", i)

            if not (-90 <= lat <= 90):
                raise InputError(f"row {i}: lat {lat} out of range [-90, 90]")
            if not (-180 <= lon <= 180):
                raise InputError(f"row {i}: lon {lon} out of range [-180, 180]")
            if lat == 0 and lon == 0:
                raise InputError(f"row {i}: (0, 0) is a placeholder, not a real pin")

            if bbox_check:
                min_la, min_lo, max_la, max_lo = BLR_BBOX
                if not (min_la <= lat <= max_la and min_lo <= lon <= max_lo):
                    warnings.append(
                        f"row {i} ({site}): {lat},{lon} is outside the Bengaluru "
                        f"box — check for swapped lat/lon or a typo"
                    )

            if site in seen:
                warnings.append(
                    f"row {i}: duplicate site_number {site!r} (also row {seen[site]})"
                )
            seen[site] = i

            label = str(row.get(cols.get("label", ""), "") or "").strip() or site
            notes = str(row.get(cols.get("notes", ""), "") or "").strip()
            extra = {
                k: str(row[orig]).strip()
                for k, orig in cols.items()
                if k not in RESERVED and str(row[orig]).strip()
            }
            pins.append(Pin(site, lat, lon, label, notes, extra))

    if not pins:
        raise InputError("no data rows found")
    for w in warnings:
        print(f"  warning: {w}", file=sys.stderr)
    return pins


def to_geojson(pins: list[Pin], title: str) -> str:
    fc = {
        "type": "FeatureCollection",
        "name": title,
        "features": [
            {
                "type": "Feature",
                "properties": p.properties,
                "geometry": {"type": "Point", "coordinates": [p.lon, p.lat]},
            }
            for p in pins
        ],
    }
    return json.dumps(fc, indent=2, ensure_ascii=False)


def to_kml(pins: list[Pin], title: str) -> str:
    def esc(s: str) -> str:
        return sx.escape(str(s))

    marks = []
    for p in pins:
        rows = "".join(
            f"<tr><td><b>{esc(k)}</b></td><td>{esc(v)}</td></tr>"
            for k, v in p.properties.items()
        )
        desc = f"<![CDATA[<table>{rows}</table>]]>"
        marks.append(
            "<Placemark>"
            f"<name>{esc(p.label)}</name>"
            f"<description>{desc}</description>"
            f"<Point><coordinates>{p.lon},{p.lat},0</coordinates></Point>"
            "</Placemark>"
        )
    body = "\n".join(marks)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
        f"<name>{esc(title)}</name>\n{body}\n"
        "</Document></kml>\n"
    )


def to_html(pins: list[Pin], title: str) -> str:
    """Standalone Leaflet map. Embeds the GeoJSON; OSM tiles, no API key."""
    geojson = to_geojson(pins, title)
    lats = [p.lat for p in pins]
    lons = [p.lon for p in pins]
    center = [sum(lats) / len(lats), sum(lons) / len(lons)]
    safe_title = html.escape(title)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{safe_title}</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
  integrity="sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=" crossorigin="">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
  integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=" crossorigin=""></script>
<style>
  html, body {{ height: 100%; margin: 0; }}
  #map {{ height: 100%; }}
  .lp-title {{
    position: absolute; z-index: 1000; top: 10px; left: 50px;
    background: rgba(255,255,255,.9); padding: 6px 12px; border-radius: 6px;
    font: 14px/1.3 system-ui, sans-serif; box-shadow: 0 1px 4px rgba(0,0,0,.3);
  }}
  .leaflet-popup-content table {{ border-collapse: collapse; font: 12px/1.4 system-ui, sans-serif; }}
  .leaflet-popup-content td {{ padding: 2px 6px; vertical-align: top; }}
  .leaflet-popup-content td:first-child {{ color: #555; white-space: nowrap; }}
</style>
</head>
<body>
<div class="lp-title">{safe_title}</div>
<div id="map"></div>
<script>
const data = {geojson};
const map = L.map('map').setView({center}, 17);
L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
  maxZoom: 19,
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
}}).addTo(map);

function popupHtml(props) {{
  let rows = '';
  for (const [k, v] of Object.entries(props)) {{
    const ek = String(k).replace(/[&<>]/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;'}}[c]));
    const ev = String(v).replace(/[&<>]/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;'}}[c]));
    rows += `<tr><td><b>${{ek}}</b></td><td>${{ev}}</td></tr>`;
  }}
  return `<table>${{rows}}</table>`;
}}

const layer = L.geoJSON(data, {{
  pointToLayer: (f, latlng) => L.marker(latlng),
  onEachFeature: (f, l) => {{
    l.bindPopup(popupHtml(f.properties));
    l.bindTooltip(String(f.properties.label || f.properties.site_number),
                  {{permanent: true, direction: 'top', offset: [0, -10]}});
  }}
}}).addTo(map);

if (data.features.length > 1) map.fitBounds(layer.getBounds().pad(0.2));
</script>
</body>
</html>
"""


WRITERS = {
    "geojson": (to_geojson, ".geojson"),
    "kml": (to_kml, ".kml"),
    "html": (to_html, ".html"),
}


def build(
    input_csv: Path,
    out_base: Path,
    *,
    title: str,
    formats: list[str],
    bbox_check: bool = True,
) -> list[Path]:
    pins = read_pins(input_csv, bbox_check=bbox_check)
    out_base.parent.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for fmt in formats:
        writer, ext = WRITERS[fmt]
        out_path = out_base.with_suffix(ext)
        out_path.write_text(writer(pins, title), encoding="utf-8")
        written.append(out_path)
    print(f"Rendered {len(pins)} pin(s) -> {', '.join(str(p) for p in written)}")
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Render verified site/survey-number coordinates as map pins.",
        epilog="Coordinates must come from an authoritative source — this tool "
        "never looks them up or guesses.",
    )
    parser.add_argument("input", type=Path, help="input CSV (see --help for columns)")
    parser.add_argument(
        "--out", type=Path, default=Path("build/pins"),
        help="output path base, without extension (default: build/pins)",
    )
    parser.add_argument(
        "--title", default="Land site pins", help="map / document title",
    )
    parser.add_argument(
        "--formats", default="geojson,kml,html",
        help="comma-separated subset of: geojson,kml,html (default: all)",
    )
    parser.add_argument(
        "--no-bbox-check", action="store_true",
        help="skip the 'inside Bengaluru' sanity warning (use outside BLR)",
    )
    args = parser.parse_args(argv)

    formats = [f.strip().lower() for f in args.formats.split(",") if f.strip()]
    bad = [f for f in formats if f not in WRITERS]
    if bad:
        parser.error(f"unknown format(s): {', '.join(bad)}; choose from {list(WRITERS)}")

    try:
        build(
            args.input, args.out,
            title=args.title, formats=formats, bbox_check=not args.no_bbox_check,
        )
    except (InputError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
