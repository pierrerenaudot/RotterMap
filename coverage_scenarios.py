#!/usr/bin/env python3
"""Compare a few "how generous is the methodology" scenarios for the
Rotterdam street-coverage percentage, side by side with the rigorous one.

Reuses the Overpass/Nominatim cache written by coverage.py so this is fast
to re-run with different parameters.
"""
from __future__ import annotations

import json
from pathlib import Path

from pyproj import Transformer
from shapely.geometry import LineString
from shapely.ops import transform, unary_union
from shapely.strtree import STRtree

from coverage import (
    HIGHWAY_TYPES,
    fetch_city_boundary,
    fetch_streets_raw,
    load_run_tracks,
)

TO_RD = Transformer.from_crs("EPSG:4326", "EPSG:28992", always_xy=True).transform

CORE_TYPES = {
    "residential", "living_street", "unclassified",
    "tertiary", "tertiary_link", "secondary", "secondary_link",
    "primary", "primary_link",
}


def coverage_pct(streets_rd, runs_rd, buffer_m: float, any_touch: bool) -> tuple[float, float, float]:
    total_length_m = sum(s.length for s in streets_rd)
    run_buffer = unary_union([ln.buffer(buffer_m) for ln in runs_rd])
    buffer_polys = list(run_buffer.geoms) if run_buffer.geom_type == "MultiPolygon" else [run_buffer]
    tree = STRtree(buffer_polys)

    covered_length_m = 0.0
    for street in streets_rd:
        if street.length == 0:
            continue
        idx = tree.query(street)
        if len(idx) == 0:
            continue
        candidates = [buffer_polys[j] for j in idx]
        local_union = candidates[0] if len(candidates) == 1 else unary_union(candidates)
        inter_len = street.intersection(local_union).length
        if inter_len <= 0:
            continue
        covered_length_m += street.length if any_touch else inter_len

    pct = 100.0 * covered_length_m / total_length_m if total_length_m else 0.0
    return pct, covered_length_m / 1000, total_length_m / 1000


def main():
    boundary, relation_id = fetch_city_boundary("Rotterdam", "Netherlands")
    raw = fetch_streets_raw(relation_id)
    runs_wgs84 = load_run_tracks(Path("runs.geojson"))
    runs_rd = [transform(TO_RD, ln) for ln in runs_wgs84]

    all_streets_rd = [transform(TO_RD, LineString(r["coords"])) for r in raw]
    core_streets_rd = [
        transform(TO_RD, LineString(r["coords"])) for r in raw if r["highway"] in CORE_TYPES
    ]

    scenarios = [
        ("Rigorous (baseline)", all_streets_rd, 20, False),
        ("Bigger buffer (40m)", all_streets_rd, 40, False),
        ("Bigger buffer (60m)", all_streets_rd, 60, False),
        ("Any-touch = full street credit (20m)", all_streets_rd, 20, True),
        ("Any-touch + 40m buffer", all_streets_rd, 40, True),
        ("Core streets only, no footways/paths (20m)", core_streets_rd, 20, False),
        ("Core streets only + any-touch (20m)", core_streets_rd, 20, True),
        ("Core streets only + any-touch + 40m", core_streets_rd, 40, True),
    ]

    print(f"{'Scenario':<45} {'%':>7}  {'covered km':>11}  {'total km':>9}")
    print("-" * 76)
    for name, streets, buf, touch in scenarios:
        pct, cov_km, tot_km = coverage_pct(streets, runs_rd, buf, touch)
        print(f"{name:<45} {pct:6.1f}%  {cov_km:10.1f}  {tot_km:8.1f}")


if __name__ == "__main__":
    main()
