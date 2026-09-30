#!/usr/bin/env python3
"""Build runs.geojson from a Strava data export.

Usage:
    python3 build_geojson.py [export_dir] [-o runs.geojson]

Requires: gpxpy, fitparse, rdp  (pip install -r requirements.txt)
"""
from __future__ import annotations

import argparse
import csv
import gzip
import io
import math
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import gpxpy
from fitparse import FitFile
from rdp import rdp

# Roughly the land + coastal waters of the Netherlands.
NL_BBOX = dict(lat_min=50.75, lat_max=53.7, lon_min=3.2, lon_max=7.3)
# Fraction of track points that must fall inside the bbox to keep the run.
NL_INSIDE_THRESHOLD = 0.9
# Douglas-Peucker tolerance, in meters.
SIMPLIFY_TOLERANCE_M = 7.0

EXCLUDED_RUN_TYPES = {"Trail Run", "Virtual Run"}

# Manually excluded activity IDs: passed the NL bbox check but shouldn't be
# shown (e.g. a run actually elsewhere that just clipped the bbox).
EXCLUDED_ACTIVITY_IDS = {
    "15412630306",  # "Fractionné belge" — starts in Brussels
}


@dataclass
class SkipLog:
    no_gps: list = field(default_factory=list)
    outside_nl: list = field(default_factory=list)
    parse_error: list = field(default_factory=list)
    missing_file: list = field(default_factory=list)
    manually_excluded: list = field(default_factory=list)

    def total(self) -> int:
        return (
            len(self.no_gps)
            + len(self.outside_nl)
            + len(self.parse_error)
            + len(self.missing_file)
            + len(self.manually_excluded)
        )


def find_export_dir(cli_arg: str | None) -> Path:
    if cli_arg:
        p = Path(cli_arg)
        if not (p / "activities.csv").exists():
            sys.exit(f"No activities.csv found in {p}")
        return p
    cwd = Path(".")
    if (cwd / "activities.csv").exists():
        return cwd
    candidates = sorted(cwd.glob("export_*"))
    for c in candidates:
        if (c / "activities.csv").exists():
            return c
    sys.exit("Could not find an export directory with activities.csv. Pass it explicitly.")


def read_activities(csv_path: Path):
    """Read activities.csv, handling Strava's duplicate column names by keeping
    the first occurrence of duplicated headers (Elapsed Time, Distance, ...)."""
    with csv_path.open(newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader)
        first_index: dict[str, int] = {}
        for i, name in enumerate(header):
            first_index.setdefault(name, i)

        def get(row, name, default=""):
            idx = first_index.get(name)
            if idx is None or idx >= len(row):
                return default
            return row[idx]

        # Moving Time only appears once, so a plain lookup works, but route it
        # through the same helper for consistency.
        for row in reader:
            if not row:
                continue
            yield {
                "id": get(row, "Activity ID"),
                "date": get(row, "Activity Date"),
                "name": get(row, "Activity Name"),
                "type": get(row, "Activity Type"),
                "elapsed_s": get(row, "Elapsed Time"),
                "distance_km": get(row, "Distance"),
                "moving_s": get(row, "Moving Time"),
                "filename": get(row, "Filename"),
            }


def decompress_if_needed(path: Path) -> tuple[bytes, str]:
    data = path.read_bytes()
    name = path.name
    if name.endswith(".gz"):
        data = gzip.decompress(data)
        name = name[: -len(".gz")]
    return data, name


def parse_gpx_points(data: bytes) -> list[tuple[float, float]]:
    gpx = gpxpy.parse(io.StringIO(data.decode("utf-8", errors="replace")))
    points = []
    for track in gpx.tracks:
        for segment in track.segments:
            for pt in segment.points:
                if pt.latitude is not None and pt.longitude is not None:
                    points.append((pt.latitude, pt.longitude))
    return points


def parse_fit_points(data: bytes) -> list[tuple[float, float]]:
    fit = FitFile(io.BytesIO(data))
    points = []
    SEMI_TO_DEG = 180.0 / (2 ** 31)
    for record in fit.get_messages("record"):
        lat = lon = None
        for field_ in record:
            if field_.name == "position_lat" and field_.value is not None:
                lat = field_.value * SEMI_TO_DEG
            elif field_.name == "position_long" and field_.value is not None:
                lon = field_.value * SEMI_TO_DEG
        if lat is not None and lon is not None:
            points.append((lat, lon))
    return points


def parse_tcx_points(data: bytes) -> list[tuple[float, float]]:
    root = ET.fromstring(data)
    points = []
    ns = {"tcx": "http://www.garmin.com/xmlschema/TrainingCenterDatabase/v2"}
    for trackpoint in root.iter():
        if trackpoint.tag.endswith("Trackpoint"):
            pos = None
            for child in trackpoint:
                if child.tag.endswith("Position"):
                    pos = child
                    break
            if pos is None:
                continue
            lat = lon = None
            for child in pos:
                if child.tag.endswith("LatitudeDegrees"):
                    lat = float(child.text)
                elif child.tag.endswith("LongitudeDegrees"):
                    lon = float(child.text)
            if lat is not None and lon is not None:
                points.append((lat, lon))
    return points


def parse_track(path: Path) -> list[tuple[float, float]]:
    data, inner_name = decompress_if_needed(path)
    lower = inner_name.lower()
    if lower.endswith(".gpx"):
        return parse_gpx_points(data)
    if lower.endswith(".fit"):
        return parse_fit_points(data)
    if lower.endswith(".tcx"):
        return parse_tcx_points(data)
    raise ValueError(f"unrecognized format: {inner_name}")


def fraction_in_bbox(points: list[tuple[float, float]]) -> float:
    inside = sum(
        1
        for lat, lon in points
        if NL_BBOX["lat_min"] <= lat <= NL_BBOX["lat_max"] and NL_BBOX["lon_min"] <= lon <= NL_BBOX["lon_max"]
    )
    return inside / len(points)


def simplify_track(points: list[tuple[float, float]], tolerance_m: float) -> list[tuple[float, float]]:
    if len(points) <= 2:
        return points
    ref_lat = sum(p[0] for p in points) / len(points)
    lat_scale = 111_320.0
    lon_scale = 111_320.0 * math.cos(math.radians(ref_lat))
    # rdp's default distance function uses np.cross, which on newer numpy
    # requires 3-vectors, so pad with a dummy zero z-coordinate.
    projected = [(lon * lon_scale, lat * lat_scale, 0.0) for lat, lon in points]
    mask = rdp(projected, epsilon=tolerance_m, algo="iter", return_mask=True)
    return [p for p, keep in zip(points, mask) if keep]


def parse_date(date_str: str) -> datetime | None:
    for fmt in ("%b %d, %Y, %I:%M:%S %p", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            continue
    return None


def format_duration(seconds: float) -> str:
    seconds = int(round(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def format_pace(moving_s: float, distance_km: float) -> str:
    if distance_km <= 0:
        return ""
    pace_min_per_km = (moving_s / 60.0) / distance_km
    minutes = int(pace_min_per_km)
    seconds = int(round((pace_min_per_km - minutes) * 60))
    if seconds == 60:
        minutes += 1
        seconds = 0
    return f"{minutes}:{seconds:02d}/km"


def build_geojson(export_dir: Path) -> tuple[dict, dict]:
    features = []
    skip = SkipLog()
    total_runs = 0
    kept_km = 0.0

    for row in read_activities(export_dir / "activities.csv"):
        if row["type"] != "Run" or row["type"] in EXCLUDED_RUN_TYPES:
            continue
        total_runs += 1

        filename = row["filename"].strip()
        label = f"{row['id']} ({row['name'] or 'unnamed'})"

        if row["id"] in EXCLUDED_ACTIVITY_IDS:
            skip.manually_excluded.append(label)
            continue

        if not filename:
            skip.missing_file.append(label)
            continue

        path = export_dir / filename
        if not path.exists():
            skip.missing_file.append(f"{label} -> {filename} not found")
            continue

        try:
            points = parse_track(path)
        except Exception as exc:  # noqa: BLE001 - want to log and continue
            skip.parse_error.append(f"{label} -> {exc}")
            continue

        if len(points) < 2:
            skip.no_gps.append(label)
            continue

        frac = fraction_in_bbox(points)
        if frac < NL_INSIDE_THRESHOLD:
            skip.outside_nl.append(f"{label} -> only {frac:.0%} of points in NL bbox")
            continue

        simplified = simplify_track(points, SIMPLIFY_TOLERANCE_M)
        coords = [[round(lon, 6), round(lat, 6)] for lat, lon in simplified]

        dt = parse_date(row["date"])
        date_iso = dt.strftime("%Y-%m-%d") if dt else row["date"]
        month = dt.strftime("%Y-%m") if dt else ""

        try:
            distance_km = float(row["distance_km"]) if row["distance_km"] else 0.0
        except ValueError:
            distance_km = 0.0
        try:
            elapsed_s = float(row["elapsed_s"]) if row["elapsed_s"] else 0.0
        except ValueError:
            elapsed_s = 0.0
        try:
            moving_s = float(row["moving_s"]) if row["moving_s"] else elapsed_s
        except ValueError:
            moving_s = elapsed_s

        kept_km += distance_km

        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "LineString", "coordinates": coords},
                "properties": {
                    "id": row["id"],
                    "date": date_iso,
                    "month": month,
                    "name": row["name"],
                    "distance_km": round(distance_km, 2),
                    "duration": format_duration(elapsed_s),
                    "avg_pace": format_pace(moving_s, distance_km),
                    "strava_url": f"https://www.strava.com/activities/{row['id']}" if row["id"] else None,
                },
            }
        )

    fc = {"type": "FeatureCollection", "features": features}
    summary = {
        "total_run_rows": total_runs,
        "kept": len(features),
        "kept_km": round(kept_km, 1),
        "skipped_no_gps": len(skip.no_gps),
        "skipped_outside_nl": len(skip.outside_nl),
        "skipped_parse_error": len(skip.parse_error),
        "skipped_missing_file": len(skip.missing_file),
        "skipped_manually_excluded": len(skip.manually_excluded),
        "skip_details": {
            "no_gps": skip.no_gps,
            "outside_nl": skip.outside_nl,
            "parse_error": skip.parse_error,
            "missing_file": skip.missing_file,
            "manually_excluded": skip.manually_excluded,
        },
    }
    return fc, summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("export_dir", nargs="?", help="Path to the unzipped Strava export (contains activities.csv)")
    parser.add_argument("-o", "--output", default="runs.geojson", help="Output GeoJSON path")
    args = parser.parse_args()

    export_dir = find_export_dir(args.export_dir)
    print(f"Using export dir: {export_dir}")

    fc, summary = build_geojson(export_dir)

    out_path = Path(args.output)
    out_path.write_text(__import__("json").dumps(fc, ensure_ascii=False))
    print(f"Wrote {out_path} ({len(fc['features'])} features)")

    print("\n--- Summary ---")
    print(f"Run activities in CSV:      {summary['total_run_rows']}")
    print(f"Kept (NL runs):             {summary['kept']}")
    print(f"Total distance kept:        {summary['kept_km']} km")
    print(f"Skipped, no GPS data:       {summary['skipped_no_gps']}")
    print(f"Skipped, outside NL:        {summary['skipped_outside_nl']}")
    print(f"Skipped, parse error:       {summary['skipped_parse_error']}")
    print(f"Skipped, missing file:      {summary['skipped_missing_file']}")
    print(f"Skipped, manually excluded: {summary['skipped_manually_excluded']}")

    for category, items in summary["skip_details"].items():
        if items:
            print(f"\n{category}:")
            for item in items:
                print(f"  - {item}")


if __name__ == "__main__":
    main()
