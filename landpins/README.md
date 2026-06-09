# landpins

A small, dependency-free tool that turns a CSV of **site/survey numbers + verified
coordinates** into map pins you can open in a browser or Google Earth.

> ⚠️ **This tool does not look up or invent coordinates.** It only renders the
> coordinates *you* give it. For land/property decisions, every coordinate must
> come from an authoritative cadastral source — otherwise a pin is just a
> confident-looking guess. See [Getting verified coordinates](#getting-verified-coordinates).

## Why

You can't reliably auto-derive an "exact" pin for a numbered site/survey plot
without authoritative survey data. So this tool splits the job cleanly:

1. **You** obtain verified coordinates (from Dishaank / Bhu-Naksha / e-Aasthi).
2. **landpins** validates them and renders accurate, shareable map pins.

## Install

Nothing to install — pure Python 3 standard library (3.9+). The output
`map.html` loads Leaflet from a CDN; no API keys, no build step.
(`pytest` is only needed to run the tests.)

## Usage

```bash
python landpins.py INPUT.csv --out build/hebbal --title "Hebbal site pins"
```

Produces `build/hebbal.geojson`, `build/hebbal.kml`, and `build/hebbal.html`.
Open the `.html` in any browser; import the `.kml` into Google Earth or
Google My Maps; load the `.geojson` into QGIS / Mapbox / Leaflet.

Options:

| Flag | Default | Meaning |
| --- | --- | --- |
| `--out` | `build/pins` | Output path base (extension added per format) |
| `--title` | `Land site pins` | Map / document title |
| `--formats` | `geojson,kml,html` | Comma-separated subset to emit |
| `--no-bbox-check` | off | Silence the "outside Bengaluru" sanity warning |

### Input CSV

Header row required; column order doesn't matter; `#` lines are ignored.

| Column | Required | Notes |
| --- | --- | --- |
| `site_number` | ✅ | e.g. `Site 13`, `Sy.No 13/2` |
| `lat` | ✅ | decimal degrees, WGS84, e.g. `13.0362` |
| `lon` | ✅ | decimal degrees, WGS84, e.g. `77.5976` |
| `label` | — | popup/tooltip name (defaults to `site_number`) |
| `notes` | — | free text shown in the popup |
| anything else | — | carried through into the pin's properties/popup |

See [`examples/sumangali_seva_ashrama_road13.csv`](examples/sumangali_seva_ashrama_road13.csv)
for a template. **Its coordinates are placeholders — replace them.**

### Validation

The tool refuses to emit obviously-wrong data and warns on suspicious data:

- **Errors (exit 1):** missing columns, non-numeric/out-of-range lat/lon,
  empty `site_number`, `(0,0)` placeholder.
- **Warnings:** coordinates outside the Bengaluru bounding box (likely a
  swapped lat/lon or typo), duplicate `site_number`s.

## Getting verified coordinates

These are the authoritative Karnataka sources to read coordinates off of:

- **Dishaank** (`dishaank.karnataka.gov.in`) — GIS cadastral viewer; shows
  survey numbers on a basemap. Click a parcel to read its location.
- **Bhu-Naksha Karnataka** — survey-number plot maps for cross-referencing.
- **BBMP e-Aasthi / Kaveri Online Services** — urban site (khata) records.

Read the coordinate for each site off the map (or export a parcel), put it in
the CSV, and run the tool. The CSV is your audit trail of where each pin came from.

## Tests

```bash
python -m pytest test_landpins.py -q
```

## Note on this repo

This tool is self-contained in the `landpins/` directory and shares no code with
the surrounding project. It lives here only because that's where it was created;
it can be lifted into its own repository unchanged.
