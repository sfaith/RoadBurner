#!/usr/bin/env python3
"""Stage 1: extract Novatek freeGPS telemetry from dashcam MP4 clips.

Scans the configured clip folder, decodes the embedded GPS stream,
reverse-geocodes town names offline, and writes into the work folder:
  track.csv    - one row per GPS point (clip, video-second, position, ...)
  labels.csv   - town label spans on the concatenated video timeline
  concat.txt   - ffmpeg concat list consumed by render_overlay.py
  gaps.csv     - (start_sec, end_sec, clip) spans for clips with ZERO GPS
                 chunks - footage from these clips is still kept (see
                 Round 4 in CLAUDE.md), this just tells render_overlay.py
                 which stretches to show a "no GPS lock" indicator over
                 instead of stale forward-filled position/speed/town data.
  duration_sec - total concatenated video length in seconds, as a plain
                 float. Written explicitly so render_overlay.py doesn't
                 have to derive the video's total length from track.csv's
                 GPS points, which would undercount if a trailing clip is
                 GPS-dark.

Usage: python extract_gps.py [--config config.ini]
"""
from __future__ import annotations

__version__ = "0.1.0"

import argparse
import configparser
import csv
import math
import re
import struct
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

EARTH_RADIUS_MI = 3958.76  # duplicated from render_overlay.py's constant of
# the same name - kept local so this stage-1 script has no dependency on
# stage-2's module (extract_gps.py is meant to run standalone).

FREEGPS = b"freeGPS"
MARKER = re.compile(rb"[AV][NS][EW]\x00")
CHUNK_WINDOW = 220  # bytes to inspect after each freeGPS tag

US_STATE_ABBR: dict[str, str] = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR",
    "California": "CA", "Colorado": "CO", "Connecticut": "CT",
    "Delaware": "DE", "District of Columbia": "DC", "Florida": "FL",
    "Georgia": "GA", "Hawaii": "HI", "Idaho": "ID", "Illinois": "IL",
    "Indiana": "IN", "Iowa": "IA", "Kansas": "KS", "Kentucky": "KY",
    "Louisiana": "LA", "Maine": "ME", "Maryland": "MD",
    "Massachusetts": "MA", "Michigan": "MI", "Minnesota": "MN",
    "Mississippi": "MS", "Missouri": "MO", "Montana": "MT",
    "Nebraska": "NE", "Nevada": "NV", "New Hampshire": "NH",
    "New Jersey": "NJ", "New Mexico": "NM", "New York": "NY",
    "North Carolina": "NC", "North Dakota": "ND", "Ohio": "OH",
    "Oklahoma": "OK", "Oregon": "OR", "Pennsylvania": "PA",
    "Rhode Island": "RI", "South Carolina": "SC", "South Dakota": "SD",
    "Tennessee": "TN", "Texas": "TX", "Utah": "UT", "Vermont": "VT",
    "Virginia": "VA", "Washington": "WA", "West Virginia": "WV",
    "Wisconsin": "WI", "Wyoming": "WY",
}


@dataclass
class GpsPoint:
    clip: str
    sec_in_clip: int
    timestamp_utc: str
    valid: bool
    lat: float
    lon: float
    speed_mph: float
    heading: float


def nmea_to_decimal(value: float, hemisphere: str) -> float:
    """Convert NMEA ddmm.mmmm to signed decimal degrees."""
    degrees = int(value / 100)
    decimal = degrees + (value - degrees * 100) / 60.0
    return -decimal if hemisphere in ("S", "W") else decimal


def _haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two points in miles (duplicated from
    render_overlay.haversine_miles - see the EARTH_RADIUS_MI comment)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_MI * math.asin(math.sqrt(a))


def find_time_gaps(points: list[GpsPoint], threshold_minutes: float = 10.0
                   ) -> list[dict]:
    """Flag real-world time gaps between consecutive valid GPS points wider
    than threshold_minutes - e.g. a camera power loss/malfunction between
    clips, not just normal clip-to-clip segmentation.

    Returns gap records sorted by time:
    {before_clip, before_ts, after_clip, after_ts, gap_minutes,
     straight_line_mi}. straight_line_mi is the haversine distance between
     the two bounding points - a LOWER BOUND on miles missed by the
     point-to-point mileage calc during the gap. If the route curved
     during the gap (turns, a city, a mountain pass) actual driven
     distance for that stretch is undercounted by more than this.
    """
    valid = sorted((p for p in points if p.valid), key=lambda p: p.timestamp_utc)
    gaps: list[dict] = []
    for prev, cur in zip(valid, valid[1:]):
        t1 = datetime.strptime(prev.timestamp_utc, "%Y-%m-%d %H:%M:%S")
        t2 = datetime.strptime(cur.timestamp_utc, "%Y-%m-%d %H:%M:%S")
        gap_minutes = (t2 - t1).total_seconds() / 60.0
        if gap_minutes >= threshold_minutes:
            gaps.append({
                "before_clip": prev.clip, "before_ts": prev.timestamp_utc,
                "after_clip": cur.clip, "after_ts": cur.timestamp_utc,
                "gap_minutes": round(gap_minutes, 1),
                "straight_line_mi": round(
                    _haversine_miles(prev.lat, prev.lon, cur.lat, cur.lon), 1),
            })
    return gaps


def parse_freegps(data: bytes, clip_name: str = "") -> list[GpsPoint]:
    """Parse all Novatek freeGPS chunks found in raw MP4 bytes.

    Layout per chunk (anchored on the fix-status marker A/V + N/S + E/W):
      marker-24 : 6x uint32 LE  -> hour, minute, second, year(2d), month, day
      marker    : 4 bytes       -> fix status, NS hemisphere, EW hemisphere, pad
      marker+4  : 4x float LE   -> lat (ddmm.mmmm), lon (dddmm.mmmm),
                                   speed (knots), heading (degrees)
    """
    points: list[GpsPoint] = []
    i = 0
    while True:
        i = data.find(FREEGPS, i)
        if i < 0:
            break
        chunk = data[i:i + CHUNK_WINDOW]
        m = MARKER.search(chunk, 24)
        if m:
            p = m.start()
            try:
                hh, mi, ss, yy, mo, dd = struct.unpack_from("<6I", chunk, p - 24)
                lat_raw, lon_raw, spd, brg = struct.unpack_from("<4f", chunk, p + 4)
            except struct.error:
                i += len(FREEGPS)
                continue
            valid = chunk[p:p + 1] == b"A"
            points.append(GpsPoint(
                clip=clip_name,
                sec_in_clip=len(points),
                timestamp_utc=f"20{yy:02d}-{mo:02d}-{dd:02d} {hh:02d}:{mi:02d}:{ss:02d}",
                valid=valid,
                lat=round(nmea_to_decimal(lat_raw, chr(chunk[p + 1])), 6),
                lon=round(nmea_to_decimal(lon_raw, chr(chunk[p + 2])), 6),
                speed_mph=round(spd * 1.15078, 1),
                heading=round(brg, 1),
            ))
        i += len(FREEGPS)
    return points


def clip_duration_seconds(path: Path) -> float:
    """Container duration via ffprobe."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(result.stdout.strip())


def dedupe_labels(rows: list[tuple[float, str]], end_time: float) -> list[tuple[float, float, str]]:
    """Merge consecutive identical labels into (start, end, label) spans.

    rows: (global_second, label) sorted by time. Empty labels are skipped
    but still terminate the preceding span at end_time.
    """
    spans: list[tuple[float, float, str]] = []
    cur_label: str | None = None
    cur_start = 0.0
    for t, label in rows:
        if label != cur_label:
            if cur_label:
                spans.append((cur_start, t, cur_label))
            cur_label, cur_start = label, t
    if cur_label:
        spans.append((cur_start, end_time, cur_label))
    return [s for s in spans if s[2]]


def geocode(points: list[GpsPoint]) -> dict[tuple[float, float], dict[str, str]]:
    """Offline reverse geocode; returns {(lat,lon rounded 3dp): raw result
    dict} with reverse_geocoder's own keys, notably 'name', 'admin1', and
    'cc' (ISO country code, e.g. 'US' or 'MX'). Returns the raw dict rather
    than a pre-formatted string so resolve_town_labels() can filter/carry-
    forward on 'cc' - see there for why (a border-adjacent US point can
    resolve to a nearer foreign town in the offline dataset)."""
    import reverse_geocoder as rg  # deferred: slow import, big data file
    keys = sorted({(round(p.lat, 3), round(p.lon, 3)) for p in points if p.valid})
    if not keys:
        return {}
    # mode=1 forces single-threaded lookup (default mode=2 spawns a
    # multiprocessing worker pool sized to CPU count; a burst of those
    # workers each re-importing scipy/numpy can spike Windows' committed
    # virtual memory past the pagefile-backed commit limit even with
    # plenty of physical RAM free - see CLAUDE.md).
    results = rg.search(keys, mode=1, verbose=False)
    return dict(zip(keys, results))


def resolve_town_labels(
    raw_results: list[dict[str, str] | None],
) -> list[tuple[str, str]]:
    """Per-point (town, state) labels from raw geocode() lookups, given in
    track order (one entry per GpsPoint, aligned 1:1).

    - None (an invalid/no-fix GPS point) always yields ("", "") - unchanged
      from the pre-fix behavior, and still what lets dedupe_labels() treat
      it as a real span break (see Round 4 notes in CLAUDE.md).
    - A lookup whose 'cc' is 'US' is a confident match: it's shown, and
      becomes the new carry-forward value.
    - A lookup whose 'cc' is anything else is discarded and the last
      confident US match is repeated instead. This is the 2026-07-13 fix
      for a real, confirmed artifact: the offline reverse_geocoder
      package's nearest-neighbor search operates over a worldwide city
      list, so a GPS point close to the US/Mexico border can resolve to
      the nearer foreign town (e.g. real I-10 points in Fabens/Tornillo/
      San Elizario, TX - solidly on US soil - resolved to "Praxedis
      Guerrero, Chihuahua" because that Mexican town happened to be the
      nearest indexed city) even though the vehicle never left the US.
      Carrying forward (rather than blanking, which would falsely look
      like a GPS-dark span) is correct here because the vehicle's actual
      position is known and confidently in the US - only the reverse-geo
      lookup itself was wrong.
    """
    out: list[tuple[str, str]] = []
    last: tuple[str, str] = ("", "")
    for res in raw_results:
        if res is None:
            out.append(("", ""))
        elif res.get("cc") == "US":
            state = US_STATE_ABBR.get(res["admin1"], res["admin1"])
            last = (res["name"], state)
            out.append(last)
        else:
            out.append(last)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default="config.ini")
    args = ap.parse_args()

    cfg = configparser.ConfigParser()
    # Explicit encoding: see matching note in render_overlay.py main() -
    # configparser.read() otherwise falls back to locale.getpreferredencoding()
    # (cp1252 on plain Windows installs), mis-decoding non-ASCII config.ini bytes.
    if not cfg.read(args.config, encoding="utf-8"):
        print(f"ERROR: cannot read config file {args.config}", file=sys.stderr)
        return 1

    clip_folder = Path(cfg.get("paths", "clip_folder"))
    work = Path(cfg.get("paths", "work_folder"))
    work.mkdir(parents=True, exist_ok=True)

    clips = sorted(clip_folder.glob("*.MP4")) + sorted(clip_folder.glob("*.mp4"))
    clips = sorted(set(clips))
    if not clips:
        print(f"ERROR: no MP4 files found in {clip_folder}", file=sys.stderr)
        return 1
    print(f"Found {len(clips)} clips in {clip_folder}")

    all_points: list[GpsPoint] = []
    offsets: dict[str, float] = {}
    running = 0.0
    concat_lines: list[str] = []
    # Round 4: clips with zero GPS chunks keep their footage (still added
    # to concat_lines / running) - only their GPS-derived data is missing.
    # dark_spans records exactly which stretches those are, on the global
    # concatenated timeline, so render_overlay.py can show a "no GPS lock"
    # indicator instead of silently bridging stale position/town/speed data
    # across the gap. Before Round 4 these clips were dropped from the
    # video entirely, which looked like the car teleporting.
    dark_spans: list[tuple[float, float, str]] = []
    for clip in clips:
        try:
            duration = clip_duration_seconds(clip)
        except (subprocess.CalledProcessError, ValueError) as exc:
            print(f"WARNING: skipping {clip.name}: ffprobe failed ({exc})", file=sys.stderr)
            continue
        try:
            pts = parse_freegps(clip.read_bytes(), clip.name)
        except OSError as exc:
            print(f"WARNING: skipping {clip.name}: read failed ({exc})", file=sys.stderr)
            continue

        concat_lines.append(f"file '{clip.resolve().as_posix()}'")
        if not pts:
            print(f"WARNING: {clip.name}: no GPS data (not a dashcam clip, "
                  f"or GPS was off) - footage is kept, but {duration:.1f}s "
                  f"will show a no-GPS-lock indicator instead of town/info "
                  f"data", file=sys.stderr)
            dark_spans.append((running, running + duration, clip.name))
        else:
            offsets[clip.name] = running
            all_points.extend(pts)
            print(f"  {clip.name}: {duration:6.1f}s video, {len(pts)} GPS points")
        running += duration

    if not all_points:
        print("ERROR: no GPS data found in any clip", file=sys.stderr)
        return 1

    gap_threshold = cfg.getfloat("diagnostics", "gap_warning_minutes", fallback=10.0)
    gaps = find_time_gaps(all_points, gap_threshold)
    for g in gaps:
        hrs = g["gap_minutes"] / 60.0
        print(f"WARNING: {g['gap_minutes']:.1f} min ({hrs:.1f} hr) gap in GPS "
              f"data between {g['before_clip']} ({g['before_ts']}) and "
              f"{g['after_clip']} ({g['after_ts']}) - straight-line distance "
              f"{g['straight_line_mi']:.1f} mi. Cumulative mileage for this "
              f"stretch is a lower bound (chord distance, not route distance) "
              f"unless the road was dead straight during the gap.",
              file=sys.stderr)

    (work / "concat.txt").write_text("\n".join(concat_lines) + "\n", encoding="utf-8")

    with open(work / "gaps.csv", "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["start_sec", "end_sec", "clip"])
        for start, end, clip_name in dark_spans:
            writer.writerow([round(start, 3), round(end, 3), clip_name])

    # Written explicitly (not derived from track.csv's max timestamp in
    # render_overlay.py) so a GPS-dark trailing clip doesn't get its
    # duration silently truncated off the end of the render - see the
    # module docstring.
    (work / "duration_sec").write_text(f"{running:.3f}\n", encoding="utf-8")

    print("Reverse geocoding (offline)...")
    raw_geocode = geocode(all_points)
    # resolve_town_labels() carries forward the last confident US match
    # whenever a valid point's nearest reverse-geo match is non-US (see its
    # docstring - fixes a real Mexico-border mislabel, confirmed 2026-07-13).
    resolved_labels = resolve_town_labels([
        raw_geocode.get((round(p.lat, 3), round(p.lon, 3))) if p.valid else None
        for p in all_points
    ])

    fieldnames = ["clip", "sec_in_clip", "global_sec", "timestamp_utc",
                  "valid", "lat", "lon", "speed_mph", "heading", "town", "state"]
    label_rows: list[tuple[float, str]] = []
    with open(work / "track.csv", "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for p, (town, state) in zip(all_points, resolved_labels):
            gsec = offsets[p.clip] + p.sec_in_clip
            writer.writerow({
                "clip": p.clip, "sec_in_clip": p.sec_in_clip,
                "global_sec": round(gsec, 3), "timestamp_utc": p.timestamp_utc,
                "valid": int(p.valid), "lat": p.lat, "lon": p.lon,
                "speed_mph": p.speed_mph, "heading": p.heading,
                "town": town, "state": state,
            })
            fmt = cfg.get("labels", "format", fallback="{name}, {state}")
            label = fmt.format(name=town, state=state) if town else ""
            label_rows.append((gsec, label))

    # Break the town label at each dark span's start (dedupe_labels already
    # treats an empty label as a span terminator - see its docstring/tests)
    # so a stale town name doesn't silently bridge across a GPS-dark clip.
    # Nothing is needed at the dark span's *end*: the next real point (if
    # any) naturally starts a fresh span there.
    for start, _end, _clip_name in dark_spans:
        label_rows.append((start, ""))

    spans = dedupe_labels(sorted(label_rows), running)
    with open(work / "labels.csv", "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["start_sec", "end_sec", "label"])
        for start, end, label in spans:
            writer.writerow([round(start, 3), round(end, 3), label])

    print(f"Wrote {work / 'track.csv'} ({len(all_points)} points), "
          f"{work / 'labels.csv'} ({len(spans)} label spans), "
          f"{work / 'concat.txt'} ({len(concat_lines)} clips), "
          f"{work / 'gaps.csv'} ({len(dark_spans)} GPS-dark clip(s))")
    print(f"Total video length: {running:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
