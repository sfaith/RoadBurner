"""Download and convert real Census TIGER/Line road data into the
roads.geojson / local_roads.geojson formats render_overlay.py expects.

Public-domain US government data (no API key needed). Not run
automatically as part of the pipeline - the output files are gitignored
(route-specific, regenerate locally per your own trip).

Two products, fetched separately:
  - Highways ([roads]): TIGER "Primary and Secondary Roads" (PRISECROADS),
    shipped ONE FILE PER STATE. States are derived from your own
    work/track.csv, not hardcoded, so this scales as your real_cam/ grows.
    Filtered to RTTYP in (I, U) - Interstates and US routes only.
  - Local streets ([local_roads]): TIGER "All Roads" (ROADS), shipped ONE
    FILE PER COUNTY - far too granular to fetch by state. Counties are
    determined by sampling points along your actual route and reverse-
    geocoding each to a county FIPS via the free FCC Census Block API
    (https://geo.fcc.gov/api/census/block/find) - no local county-boundary
    shapefile/spatial join needed.

Both outputs are trimmed to a padded bounding box around the portion of
your route actually in that state (not the whole state's road network) -
keeps file size and later match-time cost down; a state-wide file for
somewhere like Texas would be enormous and mostly irrelevant.

Usage:
    python tools/fetch_tiger_roads.py --track work/track.csv
    python tools/fetch_tiger_roads.py --skip-local   # highways only, fast
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

import shapefile

# --- USPS state abbreviation -> 2-digit Census FIPS code (all 50 + DC) ---
STATE_FIPS = {
    "AL": "01", "AK": "02", "AZ": "04", "AR": "05", "CA": "06", "CO": "08",
    "CT": "09", "DE": "10", "DC": "11", "FL": "12", "GA": "13", "HI": "15",
    "ID": "16", "IL": "17", "IN": "18", "IA": "19", "KS": "20", "KY": "21",
    "LA": "22", "ME": "23", "MD": "24", "MA": "25", "MI": "26", "MN": "27",
    "MS": "28", "MO": "29", "MT": "30", "NE": "31", "NV": "32", "NH": "33",
    "NJ": "34", "NM": "35", "NY": "36", "NC": "37", "ND": "38", "OH": "39",
    "OK": "40", "OR": "41", "PA": "42", "RI": "44", "SC": "45", "SD": "46",
    "TN": "47", "TX": "48", "UT": "49", "VT": "50", "VA": "51", "WA": "53",
    "WV": "54", "WI": "55", "WY": "56",
}

TIGER_BASE = "https://www2.census.gov/geo/tiger/TIGER{year}"
FCC_BLOCK_API = "https://geo.fcc.gov/api/census/block/find"


def state_bboxes_from_track(track_path: Path, pad_deg: float = 0.15
                            ) -> dict[str, tuple[float, float, float, float]]:
    """{state_abbr: (min_lat, max_lat, min_lon, max_lon)} for every state
    with at least one valid GPS point in track.csv, padded by pad_deg
    (~10 mi at these latitudes) so nearby route segments aren't clipped.
    States not in STATE_FIPS (e.g. a reverse-geocoding artifact tagging a
    Mexican state near a border) are skipped with a warning, not an error.
    """
    boxes: dict[str, list[float]] = {}
    with open(track_path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if row["valid"] != "1" or not row["state"]:
                continue
            state = row["state"]
            if state not in STATE_FIPS:
                continue
            lat, lon = float(row["lat"]), float(row["lon"])
            b = boxes.setdefault(state, [lat, lat, lon, lon])
            b[0] = min(b[0], lat)
            b[1] = max(b[1], lat)
            b[2] = min(b[2], lon)
            b[3] = max(b[3], lon)
    skipped = set()
    with open(track_path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if row["valid"] == "1" and row["state"] and row["state"] not in STATE_FIPS:
                skipped.add(row["state"])
    for s in sorted(skipped):
        print(f"NOTE: skipping non-US state tag {s!r} (reverse-geocoding "
              f"artifact near a border, not a real TIGER state)", file=sys.stderr)
    return {s: (b[0] - pad_deg, b[1] + pad_deg, b[2] - pad_deg, b[3] + pad_deg)
            for s, b in boxes.items()}


def sample_points(track_path: Path, every_n: int = 20) -> list[tuple[float, float]]:
    """Every Nth valid (lat, lon) point, to keep FCC API call volume sane
    (a 2000+ point track sampled every 20th is ~100 calls)."""
    pts: list[tuple[float, float]] = []
    with open(track_path, newline="", encoding="utf-8") as fh:
        rows = [r for r in csv.DictReader(fh) if r["valid"] == "1"]
    for i, row in enumerate(rows):
        if i % every_n == 0:
            pts.append((float(row["lat"]), float(row["lon"])))
    return pts


def counties_for_points(points: list[tuple[float, float]],
                        pause_secs: float = 0.15) -> set[str]:
    """5-digit state+county FIPS for each point, via the free FCC Census
    Block API - no county-boundary shapefile/spatial join needed. Skips
    (with a warning) any point the API can't resolve (e.g. briefly outside
    the US) rather than failing the whole run."""
    fips: set[str] = set()
    for i, (lat, lon) in enumerate(points):
        url = f"{FCC_BLOCK_API}?latitude={lat}&longitude={lon}&format=json"
        try:
            with urllib.request.urlopen(url, timeout=15) as resp:
                data = json.loads(resp.read())
            county = data.get("County", {}).get("FIPS")
            if county:
                fips.add(county)
        except Exception as exc:
            print(f"WARNING: county lookup failed for ({lat}, {lon}): {exc}",
                  file=sys.stderr)
        if (i + 1) % 25 == 0:
            print(f"  county lookups: {i + 1}/{len(points)}")
        time.sleep(pause_secs)
    return fips


def download_zip(url: str, retries: int = 1, backoff_secs: float = 5.0) -> zipfile.ZipFile:
    """Fetch a TIGER zip; on a slow/stalled connection (not necessarily
    rate-limiting - no documented rate limit is published for
    www2.census.gov, and a 120s stall-then-timeout looks more like a slow
    file or transient network hiccup than an explicit throttle response,
    which would normally reject fast) retry once after a short backoff
    before giving up."""
    attempt = 0
    while True:
        try:
            print(f"  downloading {url}" + (f" (retry {attempt})" if attempt else ""))
            with urllib.request.urlopen(url, timeout=120) as resp:
                data = resp.read()
            print(f"    {len(data) / 1e6:.1f} MB")
            return zipfile.ZipFile(io.BytesIO(data))
        except Exception:
            if attempt >= retries:
                raise
            attempt += 1
            print(f"    stalled, backing off {backoff_secs:.0f}s before retry...")
            time.sleep(backoff_secs)


def shapefile_reader(zf: zipfile.ZipFile) -> shapefile.Reader:
    names = zf.namelist()
    shp_name = next(n for n in names if n.endswith(".shp"))
    base = shp_name[:-4]
    return shapefile.Reader(
        shp=io.BytesIO(zf.read(base + ".shp")),
        dbf=io.BytesIO(zf.read(base + ".dbf")),
        shx=io.BytesIO(zf.read(base + ".shx")),
    )


def normalize_highway_id(fullname: str, rttyp: str) -> str | None:
    """TIGER FULLNAME for highways is messy ("I- 395 Hov", "US Hwy 11") -
    extract just the route number and rebuild a clean, consistent id. This
    also fixes a real bug risk: render_shield()'s number-extraction takes
    the LAST space/hyphen-separated token, which would read "Hov" off of
    "I- 395 Hov" instead of "395" if the raw FULLNAME were used as-is.
    Returns None if no route number is found (skip that segment).
    """
    m = re.search(r"\d+", fullname or "")
    if not m:
        return None
    number = m.group(0)
    return f"I-{number}" if rttyp == "I" else f"US {number}"


def in_bbox(lat: float, lon: float, bbox: tuple[float, float, float, float]) -> bool:
    lat_min, lat_max, lon_min, lon_max = bbox
    return lat_min <= lat <= lat_max and lon_min <= lon <= lon_max


def split_shape_parts(shape) -> list[list[tuple[float, float]]]:
    """A shapefile road record can be multi-part (a break/branch in one
    LINEARID); split shape.points at shape.parts boundaries into separate
    (x, y) = (lon, lat) point lists, matching GeoJSON MultiLineString
    semantics."""
    points = shape.points
    starts = list(shape.parts) + [len(points)]
    return [points[starts[i]:starts[i + 1]] for i in range(len(starts) - 1)]


def build_highway_features(sf: shapefile.Reader,
                           bbox: tuple[float, float, float, float]) -> list[dict]:
    features = []
    for sr in sf.iterShapeRecords():
        rec = sr.record.as_dict()
        if rec.get("RTTYP") not in ("I", "U"):
            continue
        route_id = normalize_highway_id(rec.get("FULLNAME", ""), rec["RTTYP"])
        if not route_id:
            continue
        route_type = "interstate" if rec["RTTYP"] == "I" else "us_route"
        for part in split_shape_parts(sr.shape):
            if not any(in_bbox(lat, lon, bbox) for lon, lat in part):
                continue
            if len(part) < 2:
                continue
            features.append({
                "type": "Feature",
                "properties": {"route_id": route_id, "route_type": route_type},
                "geometry": {"type": "LineString",
                            "coordinates": [[lon, lat] for lon, lat in part]},
            })
    return features


def build_local_features(sf: shapefile.Reader,
                         bbox: tuple[float, float, float, float]) -> list[dict]:
    features = []
    for sr in sf.iterShapeRecords():
        rec = sr.record.as_dict()
        if rec.get("MTFCC") not in ("S1200", "S1400"):
            continue
        name = (rec.get("FULLNAME") or "").strip()
        if not name:
            continue
        for part in split_shape_parts(sr.shape):
            if not any(in_bbox(lat, lon, bbox) for lon, lat in part):
                continue
            if len(part) < 2:
                continue
            features.append({
                "type": "Feature",
                "properties": {"route_id": name, "route_type": "local"},
                "geometry": {"type": "LineString",
                            "coordinates": [[lon, lat] for lon, lat in part]},
            })
    return features


def write_geojson(features: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    geo = {"type": "FeatureCollection", "features": features}
    path.write_text(json.dumps(geo), encoding="utf-8")
    print(f"Wrote {path} ({len(features)} features)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--track", default="work/track.csv",
                    help="track.csv from a real extract_gps.py run")
    ap.add_argument("--year", default="2023", help="TIGER/Line vintage")
    ap.add_argument("--roads-out", default="map_data/roads.geojson")
    ap.add_argument("--local-out", default="map_data/local_roads.geojson")
    ap.add_argument("--skip-highways", action="store_true")
    ap.add_argument("--skip-local", action="store_true")
    ap.add_argument("--sample-every", type=int, default=20,
                    help="county-lookup sampling stride (lower = more "
                         "FCC API calls, more precise county coverage)")
    ap.add_argument("--counties-file", default=None,
                    help="skip FCC sampling and fetch just the county FIPS "
                         "codes listed in this file (one per line) - for "
                         "retrying counties that failed a previous run. "
                         "Combine with --skip-highways. WARNING: overwrites "
                         "--local-out rather than merging with a prior run.")
    args = ap.parse_args()

    track_path = Path(args.track)
    if not track_path.exists():
        print(f"ERROR: {track_path} not found - run extract_gps.py first",
              file=sys.stderr)
        return 1

    bboxes = state_bboxes_from_track(track_path)
    if not bboxes:
        print("ERROR: no valid US-state GPS points found in track.csv",
              file=sys.stderr)
        return 1
    print(f"States in route: {sorted(bboxes)}")

    if not args.skip_highways:
        highway_features = []
        for state in sorted(bboxes):
            fips = STATE_FIPS[state]
            url = (f"{TIGER_BASE.format(year=args.year)}/PRISECROADS/"
                   f"tl_{args.year}_{fips}_prisecroads.zip")
            print(f"Highways: {state} ({fips})")
            zf = download_zip(url)
            sf = shapefile_reader(zf)
            feats = build_highway_features(sf, bboxes[state])
            print(f"  {len(feats)} highway segments in route corridor")
            highway_features.extend(feats)
        write_geojson(highway_features, Path(args.roads_out))

    if not args.skip_local:
        if args.counties_file:
            counties = set(Path(args.counties_file).read_text(encoding="utf-8").split())
            print(f"Retrying {len(counties)} counties from {args.counties_file}")
        else:
            print(f"Sampling route for county lookups (every {args.sample_every} pts)...")
            points = sample_points(track_path, args.sample_every)
            print(f"  {len(points)} sample points -> FCC Census Block API")
            counties = counties_for_points(points)
            print(f"Counties found: {sorted(counties)}")

        state_of_county = {c: c[:2] for c in counties}
        fips_to_state = {v: k for k, v in STATE_FIPS.items()}
        local_features = []
        failed_counties: list[str] = []
        for county_fips in sorted(counties):
            state_fips = state_of_county[county_fips]
            state = fips_to_state.get(state_fips)
            if state is None or state not in bboxes:
                continue
            url = (f"{TIGER_BASE.format(year=args.year)}/ROADS/"
                   f"tl_{args.year}_{county_fips}_roads.zip")
            print(f"Local roads: county {county_fips} ({state})")
            try:
                zf = download_zip(url)
            except Exception as exc:
                print(f"  WARNING: county {county_fips} download failed: {exc}",
                      file=sys.stderr)
                failed_counties.append(county_fips)
                continue
            sf = shapefile_reader(zf)
            feats = build_local_features(sf, bboxes[state])
            print(f"  {len(feats)} local-road segments in route corridor")
            local_features.extend(feats)
        write_geojson(local_features, Path(args.local_out))

        if failed_counties:
            retry_path = Path("sandbox/failed_counties.txt")
            retry_path.parent.mkdir(parents=True, exist_ok=True)
            retry_path.write_text("\n".join(failed_counties), encoding="utf-8")
            print(f"NOTE: {len(failed_counties)} counties failed after retry: "
                  f"{failed_counties} - written to {retry_path}. Re-run with "
                  f"--counties-file {retry_path} --skip-highways to fill them in "
                  f"(merges into the same local_features on that run, but note this "
                  f"OVERWRITES {args.local_out} rather than merging with the prior "
                  f"successful run - see README/CLAUDE.md before relying on this).")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
