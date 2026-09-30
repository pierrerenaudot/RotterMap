#!/usr/bin/env python3
"""Strip runs.geojson down to geometry only, for the public GitHub Pages site.

The private runs.geojson (used by map.html locally) carries per-run
properties: date, name, distance, duration, pace, Strava link. None of that
is meant to be public, so docs/runs.public.geojson keeps only the tracks.

Aggregate stats (total runs, total km, average pace) are NOT per-run
identifying data, so those are kept, computed once here and written to
docs/stats.json for the public page's stats panel.

Update ROTTERDAM_COVERAGE_PCT below by re-running coverage.py after a fresh
export, then re-run this script.

Usage: python3 make_public_geojson.py
"""
import json
import re
from pathlib import Path

# Kept in sync with map.html's ROTTERDAM_COVERAGE_PCT constant.
ROTTERDAM_COVERAGE_PCT = 11.7

src = json.loads(Path("runs.geojson").read_text())
features = src["features"]

out = {
    "type": "FeatureCollection",
    "features": [
        {"type": "Feature", "geometry": f["geometry"], "properties": {}}
        for f in features
    ],
}
Path("docs/runs.public.geojson").write_text(json.dumps(out))
print(f"Wrote docs/runs.public.geojson ({len(out['features'])} features, no properties)")

total_km = sum(f["properties"].get("distance_km", 0) for f in features)


def pace_to_seconds(pace: str) -> float | None:
    m = re.match(r"^(\d+):(\d\d)", pace or "")
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


paces = [pace_to_seconds(f["properties"].get("avg_pace")) for f in features]
paces = [p for p in paces if p is not None]
avg_pace_s = sum(paces) / len(paces) if paces else None
avg_pace_str = f"{int(avg_pace_s // 60)}:{round(avg_pace_s % 60):02d}/km" if avg_pace_s else "-"

stats = {
    "runs": len(features),
    "total_km": round(total_km, 1),
    "avg_pace": avg_pace_str,
    "rotterdam_pct": ROTTERDAM_COVERAGE_PCT,
}
Path("docs/stats.json").write_text(json.dumps(stats))
print(f"Wrote docs/stats.json: {stats}")
