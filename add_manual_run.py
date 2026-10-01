#!/usr/bin/env python3
"""Add a single manually-provided track file (not from the Strava CSV
export) to runs.geojson — e.g. a GPX downloaded directly from Strava for
one activity.

Usage:
    python3 add_manual_run.py path/to/file.gpx [--name "Custom name"]

Applies the same checks as build_geojson.py: must be mostly inside the
Netherlands bbox, and the track is simplified the same way, so the added
run is indistinguishable from the rest of the dataset.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime
from pathlib import Path

import gpxpy

from build_geojson import (
    NL_INSIDE_THRESHOLD,
    SIMPLIFY_TOLERANCE_M,
    format_duration,
    format_pace,
    fraction_in_bbox,
    simplify_track,
)


def haversine_km(lat1, lon1, lat2, lon2) -> float:
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))


def parse_gpx_with_times(path: Path):
    gpx = gpxpy.parse(path.read_text())
    points, times, name = [], [], None
    for track in gpx.tracks:
        name = name or track.name
        for segment in track.segments:
            for pt in segment.points:
                points.append((pt.latitude, pt.longitude))
                times.append(pt.time)
    return points, times, name


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("gpx_path")
    ap.add_argument("--name", help="Override the run's name (default: from the GPX file, or filename)")
    ap.add_argument("--geojson", default="runs.geojson", help="GeoJSON file to append to")
    args = ap.parse_args()

    path = Path(args.gpx_path)
    if not path.exists():
        sys.exit(f"File not found: {path}")

    points, times, gpx_name = parse_gpx_with_times(path)
    if len(points) < 2:
        sys.exit("No usable track points in this GPX file.")

    frac = fraction_in_bbox(points)
    if frac < NL_INSIDE_THRESHOLD:
        sys.exit(f"Only {frac:.0%} of points are inside the Netherlands bbox; refusing to add.")

    simplified = simplify_track(points, SIMPLIFY_TOLERANCE_M)
    coords = [[round(lon, 6), round(lat, 6)] for lat, lon in simplified]

    distance_km = sum(
        haversine_km(points[i][0], points[i][1], points[i + 1][0], points[i + 1][1])
        for i in range(len(points) - 1)
    )

    valid_times = [t for t in times if t is not None]
    if valid_times:
        start, end = min(valid_times), max(valid_times)
        duration_s = (end - start).total_seconds()
        date_iso = start.strftime("%Y-%m-%d")
        month = start.strftime("%Y-%m")
    else:
        duration_s = 0.0
        now = datetime.now()
        date_iso, month = now.strftime("%Y-%m-%d"), now.strftime("%Y-%m")

    name = args.name or gpx_name or path.stem.replace("_", " ")

    feature = {
        "type": "Feature",
        "geometry": {"type": "LineString", "coordinates": coords},
        "properties": {
            "id": None,
            "date": date_iso,
            "month": month,
            "name": name,
            "distance_km": round(distance_km, 2),
            "duration": format_duration(duration_s),
            "avg_pace": format_pace(duration_s, distance_km),
            "strava_url": None,
        },
    }

    geojson_path = Path(args.geojson)
    data = json.loads(geojson_path.read_text())
    data["features"].append(feature)
    geojson_path.write_text(json.dumps(data, ensure_ascii=False))

    p = feature["properties"]
    print(f"Added '{p['name']}' ({p['date']}): {p['distance_km']} km, {p['duration']}, {p['avg_pace']}")
    print(f"{args.geojson} now has {len(data['features'])} features.")


if __name__ == "__main__":
    main()
