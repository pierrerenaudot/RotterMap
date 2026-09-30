#!/usr/bin/env python3
"""Estimate what fraction of Rotterdam's street network has been run on.

Method (the same idea as apps like City Strides use):
1. Fetch Rotterdam's city boundary from Nominatim (OSM relation).
2. Fetch every street/path inside that boundary from the Overpass API.
3. Buffer all run tracks (from runs.geojson) by a threshold distance.
4. A street counts as "discovered" for the length of it that falls inside
   that buffer; sum up covered vs. total length.

Everything is projected to EPSG:28992 (Dutch national grid, RD New) for
accurate metric distances.

Requires: requests, shapely, pyproj (pip install -r requirements.txt)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import requests
from pyproj import Transformer
from shapely.geometry import LineString, Polygon, shape
from shapely.ops import transform, unary_union
from shapely.strtree import STRtree

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
USER_AGENT = "RotterMap-personal-coverage-script/1.0"

CACHE_DIR = Path(".cache")
BOUNDARY_CACHE = CACHE_DIR / "rotterdam_boundary.json"
STREETS_CACHE = CACHE_DIR / "rotterdam_streets.json"

BUFFER_M = 20.0  # how close a run must pass to a street to count it as run

# Street/path types that count as "streets" for discovery purposes: normal
# roads plus footways/paths/cycleways (a lot of running happens in parks and
# along the river, which are tagged as these, not as roads).
HIGHWAY_TYPES = (
    "residential", "living_street", "unclassified", "tertiary", "tertiary_link",
    "secondary", "secondary_link", "primary", "primary_link",
    "pedestrian", "footway", "path", "cycleway", "service", "track", "steps",
)

TO_RD = Transformer.from_crs("EPSG:4326", "EPSG:28992", always_xy=True).transform


def fetch_city_boundary(city: str, country: str) -> tuple[Polygon, int]:
    if BOUNDARY_CACHE.exists():
        cached = json.loads(BOUNDARY_CACHE.read_text())
        return shape(cached["geojson"]), cached["osm_id"]
    resp = requests.get(
        NOMINATIM_URL,
        params={
            "city": city,
            "country": country,
            "format": "json",
            "polygon_geojson": 1,
            "limit": 1,
            "addressdetails": 0,
        },
        headers={"User-Agent": USER_AGENT},
        timeout=30,
    )
    resp.raise_for_status()
    results = resp.json()
    if not results:
        sys.exit(f"Nominatim found nothing for {city}, {country}")
    r = results[0]
    poly = shape(r["geojson"])
    if poly.geom_type == "MultiPolygon":
        poly = max(poly.geoms, key=lambda g: g.area)

    CACHE_DIR.mkdir(exist_ok=True)
    BOUNDARY_CACHE.write_text(json.dumps({"geojson": r["geojson"], "osm_id": int(r["osm_id"])}))
    return poly, int(r["osm_id"])


def fetch_streets_raw(osm_relation_id: int) -> list[dict]:
    """Returns [{"coords": [[lon,lat],...], "highway": "residential"}, ...],
    using a local cache so repeated analysis runs don't re-hit Overpass."""
    if STREETS_CACHE.exists():
        return json.loads(STREETS_CACHE.read_text())

    area_id = 3600000000 + osm_relation_id
    highway_regex = "^(" + "|".join(HIGHWAY_TYPES) + ")$"
    query = f"""
    [out:json][timeout:180];
    area({area_id})->.searchArea;
    (
      way["highway"~"{highway_regex}"](area.searchArea);
    );
    out body;
    >;
    out skel qt;
    """
    import time

    last_exc = None
    data = None
    for attempt in range(3):
        try:
            resp = requests.post(OVERPASS_URL, data={"data": query}, headers={"User-Agent": USER_AGENT}, timeout=200)
            resp.raise_for_status()
            data = resp.json()
            break
        except requests.exceptions.RequestException as exc:
            last_exc = exc
            print(f"  Overpass attempt {attempt + 1} failed ({exc}); retrying...")
            time.sleep(15)
    if data is None:
        raise last_exc

    nodes: dict[int, tuple[float, float]] = {}
    ways: list[tuple[list[int], str]] = []
    for el in data["elements"]:
        if el["type"] == "node":
            nodes[el["id"]] = (el["lon"], el["lat"])
        elif el["type"] == "way":
            ways.append((el["nodes"], el.get("tags", {}).get("highway", "")))

    records = []
    for node_ids, highway in ways:
        coords = [nodes[n] for n in node_ids if n in nodes]
        if len(coords) >= 2:
            records.append({"coords": coords, "highway": highway})

    CACHE_DIR.mkdir(exist_ok=True)
    STREETS_CACHE.write_text(json.dumps(records))
    return records


def fetch_streets(osm_relation_id: int) -> list[LineString]:
    return [LineString(r["coords"]) for r in fetch_streets_raw(osm_relation_id)]


def load_run_tracks(geojson_path: Path) -> list[LineString]:
    data = json.loads(geojson_path.read_text())
    lines = []
    for f in data["features"]:
        coords = f["geometry"]["coordinates"]
        if len(coords) >= 2:
            lines.append(LineString(coords))
    return lines


def main():
    print("Fetching Rotterdam boundary from Nominatim...")
    boundary_wgs84, relation_id = fetch_city_boundary("Rotterdam", "Netherlands")
    print(f"  OSM relation {relation_id}, boundary area ~{boundary_wgs84.area:.4f} deg^2")

    print("Fetching streets inside the boundary from Overpass (this can take a minute)...")
    street_lines_wgs84 = fetch_streets(relation_id)
    print(f"  {len(street_lines_wgs84)} street/path segments")

    print("Loading run tracks from runs.geojson...")
    run_lines_wgs84 = load_run_tracks(Path("runs.geojson"))
    print(f"  {len(run_lines_wgs84)} runs")

    print("Projecting to EPSG:28992 (RD New) for accurate distances...")
    streets_rd = [transform(TO_RD, ln) for ln in street_lines_wgs84]
    runs_rd = [transform(TO_RD, ln) for ln in run_lines_wgs84]

    total_length_m = sum(ln.length for ln in streets_rd)

    print(f"Buffering run tracks by {BUFFER_M:.0f} m and unioning...")
    run_buffer = unary_union([ln.buffer(BUFFER_M) for ln in runs_rd])

    print("Computing covered length per street (this is the slow part)...")
    # Only test streets whose bounding box could plausibly intersect the
    # (possibly complex) buffer union, via an STRtree over buffer polygons.
    buffer_polys = list(run_buffer.geoms) if run_buffer.geom_type == "MultiPolygon" else [run_buffer]
    tree = STRtree(buffer_polys)

    covered_length_m = 0.0
    fully_covered = 0
    partially_covered = 0
    for i, street in enumerate(streets_rd):
        if street.length == 0:
            continue
        candidate_idx = tree.query(street)
        if len(candidate_idx) == 0:
            continue
        candidates = [buffer_polys[j] for j in candidate_idx]
        local_union = candidates[0] if len(candidates) == 1 else unary_union(candidates)
        inter = street.intersection(local_union)
        inter_len = inter.length
        if inter_len <= 0:
            continue
        covered_length_m += inter_len
        if inter_len >= street.length * 0.98:
            fully_covered += 1
        else:
            partially_covered += 1
        if i % 2000 == 0:
            print(f"  ...{i}/{len(streets_rd)}")

    pct = 100.0 * covered_length_m / total_length_m if total_length_m else 0.0

    print("\n--- Result ---")
    print(f"Total street/path length in Rotterdam: {total_length_m/1000:.1f} km ({len(streets_rd)} segments)")
    print(f"Run within {BUFFER_M:.0f} m of:          {covered_length_m/1000:.1f} km")
    print(f"  fully covered segments:  {fully_covered}")
    print(f"  partially covered segments: {partially_covered}")
    print(f"\n=> You've run on ~{pct:.1f}% of Rotterdam's street network.")


if __name__ == "__main__":
    main()
