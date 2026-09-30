# RotterMap — interactive map of your runs in the Netherlands

Turns a Strava data export into `runs.geojson` and shows it on a fast,
self-contained Leaflet map (`map.html`).

**This repo is public**, but only `docs/index.html` + `docs/runs.public.geojson`
(geometry only, no dates/names/pace/Strava links — see "Public vs. private
data" below) are meant to be shared. `runs.geojson`, the rich version with
per-run details, stays local and is gitignored.

## 1. Regenerate `runs.geojson` from a fresh export

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

python3 build_geojson.py /path/to/export_XXXXXXXX -o runs.geojson
```

If you drop the export folder (the one containing `activities.csv` and
`activities/`) directly next to `build_geojson.py`, you can omit the path —
the script looks for `export_*` in the current directory:

```bash
python3 build_geojson.py
```

The script prints a summary (runs kept, total km, and why each skipped
activity was skipped) so you can sanity-check the result:

- **Kept**: run activities (`Activity Type == Run`) whose GPS track has at
  least 90% of its points inside a Netherlands bounding box
  (lat 50.75–53.7, lon 3.2–7.3).
- **Skipped — no GPS data**: treadmill runs and similar activities that have
  no track points.
- **Skipped — outside NL**: runs recorded elsewhere (e.g. abroad).
- **Skipped — parse error / missing file**: activity rows whose file is
  missing or unreadable.

Each kept track is simplified with Douglas-Peucker (~7 m tolerance, in
projected meters) to keep the GeoJSON small and the map fast.

Supported source formats: `.gpx`, `.fit`/`.fit.gz`, `.tcx`/`.tcx.gz`.

### Duplicate CSV columns

Strava's `activities.csv` repeats some column names (`Distance`,
`Elapsed Time`, ...) with different units in each occurrence. The script
reads the file positionally and always takes the *first* occurrence for
`Distance` (kilometers) and `Elapsed Time` (seconds), and the one-off
`Moving Time` column for pace, rather than relying on `csv.DictReader`
(which would silently collapse duplicate headers to the last column).

## 2. View the map

From this directory:

```bash
python3 -m http.server 8000
```

Then open <http://localhost:8000/map.html>. (Opening `map.html` directly as
a `file://` URL won't work in most browsers — `fetch('runs.geojson')` is
blocked by CORS for local files.)

### Features

- Basemap in black/blue/green only: OpenStreetMap tiles with a CSS filter
  chain (`grayscale` → `invert` → `sepia` + `hue-rotate`) that strips every
  road/building hue and re-tints the result, so nothing on the map competes
  visually with the run tracks.
- Every track drawn in bold, opaque Dutch orange (fixed high opacity/weight,
  not scaled down for less-run routes) so all of them are easy to pick out
  at a glance.
- Click any track for a popup with date, name, distance, duration, pace, and
  a link to the activity on Strava (from the activity ID).
- Stats panel: runs, total km, % of Rotterdam's street network explored
  (see `coverage.py`), average pace.
- Fits the map to all runs on load. Responsive down to phone width — the
  stats panel becomes a slide-up sheet on narrow screens, opened with the
  "☰ Stats" button.

## Public vs. private data

`map.html` (root) is the private/local version — full stats panel, popups
with date/name/pace/Strava links, reads the rich `runs.geojson`.

`docs/index.html` is the public version served by GitHub Pages — just the
tracks on the map, no popups, no stats, reads `docs/runs.public.geojson`
(geometry only). Regenerate it after updating `runs.geojson`:

```bash
python3 make_public_geojson.py
git add docs/runs.public.geojson
git commit -m "Update public map data"
git push
```

## Street coverage (`coverage.py`)

Estimates what % of Rotterdam's real street/path network (fetched from
OpenStreetMap) you've run on at least once, using a buffer around your
tracks. See the script's docstring for the method and `coverage_scenarios.py`
for a comparison of stricter/looser variants of the same idea. Both cache
the fetched OSM data under `.cache/` (gitignored) so repeated runs are fast.

## Files

- `build_geojson.py` — the regenerable pipeline (Python: gpxpy + fitparse +
  rdp).
- `make_public_geojson.py` — strips `runs.geojson` down to geometry-only for
  the public site.
- `coverage.py` / `coverage_scenarios.py` — Rotterdam street-coverage %.
- `requirements.txt` — pinned versions for the above.
- `runs.geojson` — generated output, consumed by `map.html`. Gitignored (see
  "Public vs. private data").
- `map.html` — the private map. No build step; edit and reload.
- `docs/index.html`, `docs/runs.public.geojson` — the public GitHub Pages
  site.
