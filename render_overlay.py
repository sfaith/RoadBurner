#!/usr/bin/env python3
"""Stage 2: render town labels + route-progress map inset onto the timelapse.

Consumes the work folder produced by extract_gps.py (track.csv, labels.csv,
concat.txt), generates an ASS subtitle track and per-second map inset frames,
then runs a single ffmpeg pass that concatenates the clips and burns in both
overlays.

Usage: python render_overlay.py [--config config.ini] [--skip-map] [--dry-run]
"""
from __future__ import annotations

import argparse
import configparser
import csv
import json
import math
import re
import subprocess
import sys
from pathlib import Path

MILES_PER_DEG_LAT = 69.172
EARTH_RADIUS_MI = 3958.76
# Dashcam clips are 1920x1080; matches the PlayResX/PlayResY hardcoded into
# build_ass()'s ASS header. Used as the info-strip PNG canvas width so the
# strip composites correctly before any [video] preview_scale downscale.
SOURCE_VIDEO_WIDTH_PX = 1920

# Representative IANA zone per state (route-relevant states; extend as needed)
STATE_TZ = {
    "VA": "America/New_York", "MD": "America/New_York", "DC": "America/New_York",
    "WV": "America/New_York", "NC": "America/New_York", "GA": "America/New_York",
    "KY": "America/New_York", "OH": "America/New_York", "SC": "America/New_York",
    "TN": "America/Chicago", "AL": "America/Chicago", "MS": "America/Chicago",
    "AR": "America/Chicago", "LA": "America/Chicago", "MO": "America/Chicago",
    "OK": "America/Chicago", "TX": "America/Chicago", "KS": "America/Chicago",
    "IL": "America/Chicago", "NE": "America/Chicago", "IA": "America/Chicago",
    "NM": "America/Denver", "CO": "America/Denver", "UT": "America/Denver",
    "WY": "America/Denver", "MT": "America/Denver", "ID": "America/Denver",
    "AZ": "America/Phoenix",
    "NV": "America/Los_Angeles", "CA": "America/Los_Angeles",
    "OR": "America/Los_Angeles", "WA": "America/Los_Angeles",
}
# Split states crossed by the route: (lon threshold, zone east of it, zone west)
LON_TZ_OVERRIDES = {
    "TN": (-85.3, "America/New_York", "America/Chicago"),
    "TX": (-104.9, "America/Chicago", "America/Denver"),
    "FL": (-85.0, "America/New_York", "America/Chicago"),
}

ASS_ALIGN = {
    "bottom_left": 1, "bottom_center": 2, "bottom_right": 3,
    "top_left": 7, "top_center": 8, "top_right": 9,
}


def ass_time(seconds: float) -> str:
    """Seconds -> ASS h:mm:ss.cc timestamp."""
    if seconds < 0:
        raise ValueError(f"negative timestamp: {seconds}")
    cs = int(round(seconds * 100))
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, c = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{c:02d}"


def hex_to_ass_color(hex_color: str) -> str:
    """'#rrggbb' -> ASS '&H00bbggrr'."""
    h = hex_color.lstrip("#")
    if len(h) != 6:
        raise ValueError(f"bad color: {hex_color}")
    return f"&H00{h[4:6]}{h[2:4]}{h[0:2]}".upper()


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two points in miles."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_MI * math.asin(math.sqrt(a))


def tz_name_for(state: str, lon: float) -> str | None:
    """IANA zone for a state abbreviation, with lon splits for divided states."""
    if state in LON_TZ_OVERRIDES:
        threshold, east, west = LON_TZ_OVERRIDES[state]
        return east if lon > threshold else west
    return STATE_TZ.get(state)


def _localize_utc(utc_str: str, zone_name: str | None,
                  offset_adjust_h: float = 0.0):
    """Parse a naive UTC timestamp string, apply the manual adjust, and
    localize to zone_name (falls back to UTC if the zone is unavailable)."""
    from datetime import datetime, timedelta, timezone
    dt = datetime.strptime(utc_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    dt += timedelta(hours=offset_adjust_h)
    if zone_name:
        try:
            from zoneinfo import ZoneInfo
            dt = dt.astimezone(ZoneInfo(zone_name))
        except Exception as exc:  # missing tzdata on Windows -> stay UTC
            print(f"WARNING: timezone {zone_name} unavailable ({exc}); "
                  f"showing UTC (pip install tzdata)", file=sys.stderr)
    return dt


def format_local_time(utc_str: str, zone_name: str | None,
                      offset_adjust_h: float = 0.0) -> str:
    """'2022-05-23 14:31:00' UTC -> '09:31 CDT' (24-hour, zero-padded,
    zone-aware)."""
    dt = _localize_utc(utc_str, zone_name, offset_adjust_h)
    tz_abbr = dt.tzname() or ""
    return f"{dt.hour:02d}:{dt.minute:02d} {tz_abbr}"


def local_date_for(utc_str: str, zone_name: str | None,
                   offset_adjust_h: float = 0.0) -> str:
    """'2022-05-23 14:31:00' UTC -> '2022-05-23' local calendar date.

    Pure function used to assign each GPS point to a local day, driving the
    day-map's midnight-boundary re-framing.
    """
    return _localize_utc(utc_str, zone_name, offset_adjust_h).date().isoformat()


def format_local_date(utc_str: str, zone_name: str | None,
                      offset_adjust_h: float = 0.0) -> str:
    """'2022-05-23 14:31:00' UTC -> '2022-05-23' (YYYY-MM-DD local date, for
    the {date} info-line placeholder)."""
    return _localize_utc(utc_str, zone_name, offset_adjust_h).strftime("%Y-%m-%d")


def _info_text_by_point(track_rows: list[dict], cfg: configparser.ConfigParser
                        ) -> list[tuple[float, str]]:
    """[(global_sec, formatted info text), ...] sorted by time - the shared
    speed/cumulative-miles/miles-left/local-time computation used by both
    the legacy ASS 'Info' style (build_info_spans) and the Round 2 PNG-strip
    renderer (info_text_per_second)."""
    fmt = cfg.get("info", "format",
                  fallback="{speed:.0f} mph   {dist:.0f} mi   {remain:.0f} mi to go   {time}")
    adjust = cfg.getfloat("info", "utc_offset_adjust", fallback=0.0)
    rows = sorted(track_rows, key=lambda r: float(r["global_sec"]))
    cum = 0.0
    dists: list[float] = []
    prev: tuple[float, float] | None = None
    for r in rows:
        if r["valid"] == "1":
            pt = (float(r["lat"]), float(r["lon"]))
            if prev is not None:
                cum += haversine_miles(*prev, *pt)
            prev = pt
        dists.append(cum)
    total = cum
    out: list[tuple[float, str]] = []
    for i, r in enumerate(rows):
        zone = tz_name_for(r["state"], float(r["lon"])) if r["state"] else None
        try:
            text = fmt.format(speed=float(r["speed_mph"]), dist=dists[i],
                              remain=max(total - dists[i], 0.0),
                              time=format_local_time(r["timestamp_utc"], zone, adjust),
                              date=format_local_date(r["timestamp_utc"], zone, adjust))
        except (KeyError, ValueError, IndexError) as exc:
            print(f"ERROR: bad [info] format string: {exc}", file=sys.stderr)
            break
        out.append((float(r["global_sec"]), text))
    return out


def build_info_spans(track_rows: list[dict], cfg: configparser.ConfigParser,
                     end_time: float | None = None) -> list[tuple[float, float, str]]:
    """Per-GPS-point info line spans for the legacy ASS 'Info' style.

    `end_time`, when given, is the true total video length - the LAST
    point's span extends all the way to it instead of the old bare
    `start + 1.0` guess. Round 4 bug this fixes: if the last GPS point
    happens more than 1 second before the video actually ends (e.g. a
    trailing GPS-dark clip), the +1.0 guess left a stretch of video with
    NO info-line span at all - not even the no-GPS-lock indicator, since
    split_spans_for_gaps() can only override spans that exist, not
    manufacture coverage for a hole with nothing in it. Falls back to the
    old start + 1.0 behavior if end_time isn't provided (e.g. existing
    direct callers/tests), or if it's somehow earlier than that.
    """
    points = _info_text_by_point(track_rows, cfg)
    spans: list[tuple[float, float, str]] = []
    for i, (start, text) in enumerate(points):
        if i + 1 < len(points):
            end = points[i + 1][0]
        else:
            end = max(start + 1.0, end_time) if end_time is not None else start + 1.0
        spans.append((start, end, text))
    return spans


def info_text_per_second(track_rows: list[dict], cfg: configparser.ConfigParser,
                         total_secs: int) -> list[str]:
    """One formatted info-line string per whole video-second, forward-filled
    (same pattern as positions_per_second/dates_per_second). Feeds the
    Round 2 PNG-strip renderer instead of the ASS 'Info' style."""
    points = _info_text_by_point(track_rows, cfg)
    if not points:
        raise ValueError("track.csv contains no rows to build an info line from")
    by_sec = {int(sec): text for sec, text in points}
    out: list[str] = []
    last = by_sec[min(by_sec)]
    for sec in range(total_secs):
        last = by_sec.get(sec, last)
        out.append(last)
    return out


def build_ass(spans: list[tuple[float, float, str]], cfg: configparser.ConfigParser,
              info_spans: list[tuple[float, float, str]] | None = None) -> str:
    font = cfg.get("labels", "font", fallback="Arial")
    size = cfg.getint("labels", "font_size", fallback=48)
    align = ASS_ALIGN.get(cfg.get("labels", "position", fallback="bottom_center"), 2)
    margin_v = cfg.getint("labels", "margin_v", fallback=40)
    fade = cfg.getint("labels", "fade_ms", fallback=500)
    outline = cfg.getint("labels", "outline", fallback=2)
    header = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1920\nPlayResY: 1080\n"
        "WrapStyle: 2\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Town,{font},{size},&H00FFFFFF,&H00FFFFFF,&H00000000,"
        f"&H80000000,-1,0,0,0,100,100,0,0,1,{outline},0,{align},40,40,{margin_v},1\n"
        f"Style: Info,{font},{cfg.getint('info', 'font_size', fallback=26)},"
        f"&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,"
        f"{max(outline - 1, 1)},0,{align},40,40,"
        f"{cfg.getint('info', 'margin_v', fallback=8)},1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    events = []
    for start, end, label in spans:
        if end - start <= 0.05:
            continue
        text = label.replace("\n", " ")
        events.append(
            f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Town,,0,0,0,,"
            f"{{\\fad({fade},{fade})}}{text}"
        )
    for start, end, text in info_spans or []:
        if end - start <= 0.01:
            continue
        events.append(
            f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Info,,0,0,0,,{text}"
        )
    return header + "\n".join(events) + "\n"


def positions_per_second(track_rows: list[dict], total_secs: int) -> list[tuple[float, float]]:
    """One (lat, lon) per whole video-second, forward/back-filled over gaps."""
    by_sec: dict[int, tuple[float, float]] = {}
    for row in track_rows:
        if row["valid"] == "1":
            by_sec[int(float(row["global_sec"]))] = (float(row["lat"]), float(row["lon"]))
    if not by_sec:
        raise ValueError("track.csv contains no valid GPS fixes")
    out: list[tuple[float, float]] = []
    last = by_sec[min(by_sec)]
    for sec in range(total_secs):
        last = by_sec.get(sec, last)
        out.append(last)
    return out


def headings_per_second(track_rows: list[dict], total_secs: int) -> list[float]:
    """One GPS heading (degrees) per whole video-second, forward-filled
    (same pattern as positions_per_second). Feeds roads_per_second()."""
    by_sec: dict[int, float] = {}
    for row in track_rows:
        if row["valid"] == "1":
            by_sec[int(float(row["global_sec"]))] = float(row["heading"])
    if not by_sec:
        raise ValueError("track.csv contains no valid GPS fixes")
    out: list[float] = []
    last = by_sec[min(by_sec)]
    for sec in range(total_secs):
        last = by_sec.get(sec, last)
        out.append(last)
    return out


def dates_per_second(track_rows: list[dict], total_secs: int,
                     cfg: configparser.ConfigParser) -> list[str]:
    """One local-date string (YYYY-MM-DD) per whole video-second.

    Forward/back-fills state+lon+timestamp over gaps (same pattern as
    positions_per_second), then resolves each second's local calendar date
    via tz_name_for + local_date_for. Drives the day-map's midnight cuts.
    """
    adjust = cfg.getfloat("info", "utc_offset_adjust", fallback=0.0)
    by_sec: dict[int, tuple[str, float, str]] = {}
    for row in track_rows:
        if row["valid"] == "1":
            by_sec[int(float(row["global_sec"]))] = (
                row["state"], float(row["lon"]), row["timestamp_utc"])
    if not by_sec:
        raise ValueError("track.csv contains no valid GPS fixes")
    out: list[str] = []
    last = by_sec[min(by_sec)]
    for sec in range(total_secs):
        last = by_sec.get(sec, last)
        state, lon, ts = last
        zone = tz_name_for(state, lon) if state else None
        out.append(local_date_for(ts, zone, adjust))
    return out


def day_segments(dates: list[str]) -> list[tuple[int, int]]:
    """Collapse a per-second local-date list into contiguous (start, end)
    index spans; end is exclusive, so positions[start:end] is that day's slice.

    Spans are defined by chronological contiguity, not date identity: if a
    date briefly reappears after a tz-crossing flicker near local midnight,
    it gets its own short span rather than merging with an earlier span of
    the same date. That flicker is tolerated (extra short segment, no crash)
    rather than smoothed away.
    """
    if not dates:
        return []
    spans: list[tuple[int, int]] = []
    start = 0
    for i in range(1, len(dates)):
        if dates[i] != dates[i - 1]:
            spans.append((start, i))
            start = i
    spans.append((start, len(dates)))
    return spans


def no_gps_seconds(dark_spans: list[tuple[float, float]], total_secs: int) -> list[bool]:
    """Per-video-second mask (Round 4): True if that whole second falls
    within any GPS-dark span (a clip with zero GPS chunks - see
    extract_gps.py's work/gaps.csv). Feeds the PNG-strip info-line/shield/
    route-label path (roads_on or local_roads_on), so stale forward-filled
    position/speed data isn't displayed as if it were real during a dark
    stretch. dark_spans need not be sorted or non-overlapping."""
    mask = [False] * total_secs
    for start, end in dark_spans:
        lo = max(0, int(start))
        hi = min(total_secs, int(math.ceil(end)))
        for i in range(lo, hi):
            mask[i] = True
    return mask


def split_spans_for_gaps(
    spans: list[tuple[float, float, str]],
    dark_spans: list[tuple[float, float]],
    fill_text: str | None,
) -> list[tuple[float, float, str]]:
    """Split (start, end, text) spans (Round 4) so no span silently bridges
    across a GPS-dark stretch. Any portion of a span overlapping a dark
    span is replaced with `fill_text` (dropped entirely if `fill_text` is
    None/empty); the non-overlapping portions keep their original text.

    Feeds the ASS-based paths: town labels (fill_text=None -> just goes
    blank, matching Sean's "hyphen out missing data" - there's no honest
    town name to show) and the legacy ASS "Info" style (fill_text=a
    "no GPS lock" string). dark_spans need not be sorted or non-overlapping.
    """
    if not dark_spans:
        return spans
    darks = sorted(dark_spans)
    out: list[tuple[float, float, str]] = []
    for start, end, text in spans:
        cursor = start
        for d_start, d_end in darks:
            if d_end <= cursor or d_start >= end:
                continue
            seg_start = max(d_start, cursor)
            seg_end = min(d_end, end)
            if seg_start > cursor:
                out.append((cursor, seg_start, text))
            if fill_text:
                out.append((seg_start, seg_end, fill_text))
            cursor = seg_end
        if cursor < end:
            out.append((cursor, end, text))
    return out


def load_gaps(path: Path) -> list[tuple[float, float, str]]:
    """Load (start_sec, end_sec, clip) GPS-dark spans from extract_gps.py's
    work/gaps.csv (Round 4). Missing file is NOT a warning - most work/
    folders legitimately have no dark clips, and older (pre-Round-4)
    work/ folders never wrote this file at all; either way, no dark spans
    to show an indicator over."""
    if not path.exists():
        return []
    try:
        with open(path, newline="", encoding="utf-8") as fh:
            return [(float(r["start_sec"]), float(r["end_sec"]), r["clip"])
                    for r in csv.DictReader(fh)]
    except (OSError, KeyError, ValueError) as exc:
        print(f"WARNING: cannot load gaps file {path}: {exc}", file=sys.stderr)
        return []


def load_borders(path: Path) -> list[list[tuple[float, float]]]:
    """Load border polylines [(lon, lat), ...] from a GeoJSON MultiLineString."""
    try:
        geo = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"WARNING: cannot load borders file {path}: {exc}", file=sys.stderr)
        return []
    lines: list[list[tuple[float, float]]] = []
    for feature in geo.get("features", []):
        geom = feature.get("geometry", {})
        if geom.get("type") == "MultiLineString":
            lines.extend([tuple(pt) for pt in line] for line in geom["coordinates"])
        elif geom.get("type") == "LineString":
            lines.append([tuple(pt) for pt in geom["coordinates"]])
    return lines


def load_cities(path: Path) -> list[dict]:
    """Load city rows (rank, name, state, lat, lon) sorted by rank."""
    try:
        with open(path, newline="", encoding="utf-8") as fh:
            rows = [{"rank": int(r["rank"]), "name": r["name"], "state": r["state"],
                     "lat": float(r["lat"]), "lon": float(r["lon"])}
                    for r in csv.DictReader(fh)]
    except (OSError, KeyError, ValueError) as exc:
        print(f"WARNING: cannot load cities file {path}: {exc}", file=sys.stderr)
        return []
    return sorted(rows, key=lambda r: r["rank"])


def point_to_polyline_miles(lat: float, lon: float,
                            route: list[tuple[float, float]]) -> float:
    """Approx miles from a point to a (lat, lon) polyline (equirectangular)."""
    if not route:
        raise ValueError("empty route")
    kx = MILES_PER_DEG_LAT * math.cos(math.radians(lat))
    ky = MILES_PER_DEG_LAT
    px, py = lon * kx, lat * ky
    best = float("inf")
    ax, ay = route[0][1] * kx, route[0][0] * ky
    for blat, blon in route[1:] or route:
        bx, by = blon * kx, blat * ky
        dx, dy = bx - ax, by - ay
        seg_len2 = dx * dx + dy * dy
        if seg_len2 == 0:
            t = 0.0
        else:
            t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg_len2))
        cx, cy = ax + t * dx, ay + t * dy
        best = min(best, math.hypot(px - cx, py - cy))
        ax, ay = bx, by
    if len(route) == 1:
        best = math.hypot(px - route[0][1] * kx, py - route[0][0] * ky)
    return best


def select_cities(cities: list[dict], route: list[tuple[float, float]],
                  radius_mi: float, min_gap_mi: float) -> list[dict]:
    """Cities within radius_mi of the route, spaced >= min_gap_mi apart.

    Cities are considered in rank order, so when two are closer than
    min_gap_mi the higher-ranked (larger) city wins.
    """
    step = max(1, len(route) // 600)  # downsample route for speed
    sampled = route[::step] + [route[-1]] if route else []
    chosen: list[dict] = []
    for city in sorted(cities, key=lambda c: c["rank"]):
        if point_to_polyline_miles(city["lat"], city["lon"], sampled) > radius_mi:
            continue
        kx = MILES_PER_DEG_LAT * math.cos(math.radians(city["lat"]))
        too_close = any(
            math.hypot((city["lon"] - c["lon"]) * kx,
                       (city["lat"] - c["lat"]) * MILES_PER_DEG_LAT) < min_gap_mi
            for c in chosen)
        if not too_close:
            chosen.append(city)
    return chosen


def layout_city_labels(cities: list[dict], bounds: tuple[float, float, float, float],
                       size_px: tuple[int, int], font_px: float
                       ) -> list[tuple[dict, float | None, float | None]]:
    """Greedy non-overlapping label placement in pixel space.

    bounds = (x0, x1, y0, y1) in data coords (lon/lat), size_px = (W, H).
    Returns [(city, dx_pt, dy_pt)] with offsets in matplotlib points for an
    annotation anchored at the city with ha='left', va='top'. dx_pt None
    means the label could not be placed without overlap (draw dot only).
    Cities are processed in rank order so larger cities win the good spots.
    """
    x0, x1, y0, y1 = bounds
    W, H = size_px
    placed: list[tuple[float, float, float, float]] = []
    out: list[tuple[dict, float | None, float | None]] = []
    for c in sorted(cities, key=lambda c: c["rank"]):
        px = (c["lon"] - x0) / (x1 - x0) * W
        py = (y1 - c["lat"]) / (y1 - y0) * H  # pixel y grows downward
        w = 0.62 * font_px * len(c["name"])
        h = 1.3 * font_px
        candidates = [(6, -h - 4), (6, 4), (-6 - w, -h - 4), (-6 - w, 4)]
        spot = None
        for dx, dy in candidates:
            bx, by = px + dx, py + dy
            if bx < 1 or by < 1 or bx + w > W - 1 or by + h > H - 1:
                continue
            if any(bx < qx + qw and qx < bx + w and by < qy + qh and qy < by + h
                   for qx, qy, qw, qh in placed):
                continue
            spot = (dx, dy)
            break
        if spot is None:
            out.append((c, None, None))
        else:
            dx, dy = spot
            placed.append((px + dx, py + dy, w, h))
            # px offsets (y down) -> annotation offset points (y up), va='top'
            out.append((c, dx * 0.72, -dy * 0.72))
    return out


def _canvas_height_px(positions: list[tuple[float, float]], width_px: int) -> int:
    """Aspect-matched canvas height for a lon/lat point set, clamped to
    [width_px/3, width_px*2]. Extracted so day-map segments (different bboxes
    per local day) can share one fixed canvas size, which ffmpeg's image2
    demuxer requires across a single PNG-sequence input."""
    lats = [p[0] for p in positions]
    lons = [p[1] for p in positions]
    pad_lat = max((max(lats) - min(lats)) * 0.08, 0.01)
    pad_lon = max((max(lons) - min(lons)) * 0.08, 0.01)
    mean_lat = sum(lats) / len(lats)
    aspect = 1.0 / max(math.cos(math.radians(mean_lat)), 0.2)
    lon_span = (max(lons) - min(lons)) + 2 * pad_lon
    lat_span = (max(lats) - min(lats)) + 2 * pad_lat
    height_px = int(width_px * (lat_span * aspect) / lon_span)
    return max(min(height_px, width_px * 2), width_px // 3)


def render_map_frames(positions: list[tuple[float, float]], cfg: configparser.ConfigParser,
                      out_dir: Path, section: str = "map", frame_offset: int = 0,
                      height_px: int | None = None) -> tuple[int, int]:
    """Write one inset PNG per video-second for `positions`; returns (width, height) px.

    `section` selects which config section (e.g. "map_trip" / "map_day")
    supplies all style/data keys, so one engine drives both map instances.
    `frame_offset` shifts output filenames so a subset of the timeline (e.g.
    one local-day segment) lands in the right slot of a shared frame sequence.
    `height_px`, given explicitly, overrides the aspect-computed canvas height
    so multiple calls writing into the same sequence share one frame size.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.patheffects as pe
    import matplotlib.pyplot as plt
    from matplotlib.collections import LineCollection
    from matplotlib.patches import FancyBboxPatch

    width_px = cfg.getint(section, "width", fallback=320)
    lw = cfg.getfloat(section, "line_width", fallback=2.0)
    lats = [p[0] for p in positions]
    lons = [p[1] for p in positions]
    pad_lat = max((max(lats) - min(lats)) * 0.08, 0.01)
    pad_lon = max((max(lons) - min(lons)) * 0.08, 0.01)
    height_px = height_px if height_px is not None else _canvas_height_px(positions, width_px)

    dpi = 100
    fig = plt.figure(figsize=(width_px / dpi, height_px / dpi), dpi=dpi)
    ax = fig.add_axes((0, 0, 1, 1))
    ax.set_xlim(min(lons) - pad_lon, max(lons) + pad_lon)
    ax.set_ylim(min(lats) - pad_lat, max(lats) + pad_lat)
    ax.set_aspect("auto")
    ax.axis("off")
    ax.add_patch(FancyBboxPatch(
        (0.01, 0.01), 0.98, 0.98, transform=ax.transAxes,
        boxstyle="round,pad=0,rounding_size=0.03",
        facecolor=cfg.get(section, "panel_color", fallback="#000000"),
        alpha=cfg.getfloat(section, "panel_alpha", fallback=0.4),
        edgecolor="none", zorder=0,
    ))
    if cfg.getboolean(section, "show_borders", fallback=False):
        borders = load_borders(Path(cfg.get(section, "borders_file",
                                            fallback="map_data/us_borders.geojson")))
        if borders:
            ax.add_collection(LineCollection(
                borders, colors=cfg.get(section, "border_color", fallback="#8a8a8a"),
                linewidths=cfg.getfloat(section, "border_width", fallback=0.7),
                alpha=0.8, zorder=0.5))

    if cfg.getboolean(section, "show_cities", fallback=False):
        cities = load_cities(Path(cfg.get(section, "cities_file",
                                          fallback="map_data/us_cities.csv")))
        shown = select_cities(cities, positions,
                              cfg.getfloat(section, "city_radius_mi", fallback=40),
                              cfg.getfloat(section, "city_min_gap_mi", fallback=80))
        c_color = cfg.get(section, "city_color", fallback="#ffd35c")
        c_dot = cfg.getfloat(section, "city_dot_size", fallback=3.5)
        c_font = cfg.getfloat(section, "city_font_size", fallback=9)
        x0, x1 = ax.get_xlim()
        y0, y1 = ax.get_ylim()
        font_px = c_font * 100 / 72
        layout = layout_city_labels(shown, (x0, x1, y0, y1),
                                    (width_px, height_px), font_px)
        for city, dx_pt, dy_pt in layout:
            ax.plot([city["lon"]], [city["lat"]], "o", color=c_color,
                    markersize=c_dot, markeredgecolor="#000000",
                    markeredgewidth=0.5, zorder=1.5)
            if dx_pt is None:
                continue  # no non-overlapping spot; dot only
            ax.annotate(city["name"], (city["lon"], city["lat"]),
                        xytext=(dx_pt, dy_pt), textcoords="offset points",
                        ha="left", va="top", fontsize=c_font, color="#ffffff",
                        zorder=1.6,
                        path_effects=[pe.withStroke(linewidth=2, foreground="#000000")])
        print(f"  [{section}] cities shown: "
              f"{', '.join(c['name'] for c in shown) or '(none in range)'}")

    ax.plot(lons, lats, color=cfg.get(section, "route_color", fallback="#9e9e9e"),
            linewidth=lw, alpha=0.9, zorder=1, solid_capstyle="round")
    progress, = ax.plot([], [], color=cfg.get(section, "progress_color", fallback="#ff3b30"),
                        linewidth=lw + 0.5, zorder=2, solid_capstyle="round")
    dot, = ax.plot([], [], "o", color=cfg.get(section, "dot_color", fallback="#ffffff"),
                   markersize=6, markeredgecolor="#000000", markeredgewidth=0.8, zorder=3)

    out_dir.mkdir(parents=True, exist_ok=True)
    for sec in range(len(positions)):
        progress.set_data(lons[:sec + 1], lats[:sec + 1])
        dot.set_data([lons[sec]], [lats[sec]])
        fig.savefig(out_dir / f"{sec + frame_offset:06d}.png", transparent=True)
        if sec and sec % 500 == 0:
            print(f"  [{section}] frames: {sec}/{len(positions)}")
    plt.close(fig)
    return width_px, height_px


# --- Round 2: highway shield map-matching -----------------------------

def load_roads(path: Path) -> list[dict]:
    """Load road features from a GeoJSON LineString/MultiLineString
    FeatureCollection; each feature needs properties.route_id (required)
    and properties.route_type ("interstate" or "us_route", default
    "interstate"). Returns [{"route_id", "route_type", "geometry"}] with
    geometry as [(lat, lon), ...] (converted from GeoJSON's [lon, lat])."""
    try:
        geo = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"WARNING: cannot load roads file {path}: {exc}", file=sys.stderr)
        return []
    roads: list[dict] = []
    for feature in geo.get("features", []):
        props = feature.get("properties", {})
        route_id = props.get("route_id")
        route_type = props.get("route_type", "interstate")
        geom = feature.get("geometry", {})
        lines: list[list[list[float]]] = []
        if geom.get("type") == "LineString":
            lines = [geom["coordinates"]]
        elif geom.get("type") == "MultiLineString":
            lines = geom["coordinates"]
        for coords in lines:
            pts = [(lat, lon) for lon, lat in coords]
            if route_id and len(pts) >= 2:
                roads.append({"route_id": route_id, "route_type": route_type,
                              "geometry": pts})
    return roads


def heading_to_cardinal(heading_deg: float) -> str:
    """GPS heading in degrees (0 = north, clockwise) -> nearest of N/E/S/W."""
    h = heading_deg % 360
    if h >= 315 or h < 45:
        return "N"
    if h < 135:
        return "E"
    if h < 225:
        return "S"
    return "W"


def nearest_road(lat: float, lon: float, roads: list[dict],
                 tolerance_mi: float) -> tuple[str | None, float]:
    """Closest road's route_id within tolerance_mi, and its distance.
    route_id is None (distance still returned) if nothing is in range."""
    best_id: str | None = None
    best_dist = float("inf")
    for road in roads:
        d = point_to_polyline_miles(lat, lon, road["geometry"])
        if d < best_dist:
            best_dist = d
            best_id = road["route_id"]
    if best_dist > tolerance_mi:
        return None, best_dist
    return best_id, best_dist


def roads_per_second(positions: list[tuple[float, float]], headings: list[float],
                     roads: list[dict], tolerance_mi: float = 0.031,
                     grace_secs: int = 3) -> list[tuple[str | None, str | None]]:
    """Per-second (route_id, cardinal_direction) with hysteresis, so a brief
    interchange gap doesn't flicker the shield on and off. Once matched, a
    road stays matched through gaps shorter than `grace_secs` consecutive
    out-of-tolerance seconds; longer gaps (or never matching) give
    (None, None). Switching to a *different* road only happens once the
    current one has been dropped - no snapping to a closer parallel road
    while still within tolerance of the current match.

    tolerance_mi default ~0.031 mi (~50 m), matching the design spec.
    """
    if len(positions) != len(headings):
        raise ValueError("positions and headings must be the same length")
    out: list[tuple[str | None, str | None]] = []
    current: str | None = None
    off_count = 0
    for (lat, lon), heading in zip(positions, headings):
        if current is not None:
            current_dist = min(
                (point_to_polyline_miles(lat, lon, r["geometry"])
                 for r in roads if r["route_id"] == current),
                default=float("inf"))
            if current_dist <= tolerance_mi:
                off_count = 0
            else:
                off_count += 1
                if off_count > grace_secs:
                    current = None
                    off_count = 0
        if current is None:
            current, _ = nearest_road(lat, lon, roads, tolerance_mi)
        out.append((current, heading_to_cardinal(heading) if current else None))
    return out


def merge_road_matches(
    primary: list[tuple[str | None, str | None]],
    secondary: list[tuple[str | None, str | None]],
) -> list[tuple[str | None, str | None]]:
    """Round 3: per-second (route_id, cardinal), preferring `primary`
    (highway, from [roads]) matches; falls back to `secondary` (local road,
    from [local_roads]) only on seconds where primary is unmatched.

    Pure combine step run once, before shield_alpha_per_second(), so the
    two independent hysteresis streams (each with its own tolerance_mi/
    grace_secs) collapse into a single route_id stream - one fade timer,
    one label, whether the current match is a highway or a local street.
    """
    if len(primary) != len(secondary):
        raise ValueError("primary and secondary must be the same length")
    return [p if p[0] else s for p, s in zip(primary, secondary)]


# --- Round 2: programmatic highway shield graphics ---------------------

_FONT_CANDIDATES = {
    True: (r"C:\Windows\Fonts\arialbd.ttf",
           "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
           "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
    False: (r"C:\Windows\Fonts\arial.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"),
}


def _load_font(size_px: int, bold: bool = True):
    from PIL import ImageFont
    for path in _FONT_CANDIDATES[bold]:
        try:
            return ImageFont.truetype(path, size_px)
        except OSError:
            continue
    print(f"WARNING: no {'bold ' if bold else ''}TTF font found; falling back "
          "to PIL's default bitmap font (fixed size, won't scale cleanly)",
          file=sys.stderr)
    return ImageFont.load_default()


def _bezier_points(p0: tuple[float, float], p1: tuple[float, float],
                   p2: tuple[float, float], p3: tuple[float, float],
                   n: int = 16) -> list[tuple[float, float]]:
    """n+1 points sampled along a cubic bezier from p0 to p3 (inclusive)."""
    pts = []
    for i in range(n + 1):
        t = i / n
        mt = 1 - t
        x = mt**3 * p0[0] + 3 * mt**2 * t * p1[0] + 3 * mt * t**2 * p2[0] + t**3 * p3[0]
        y = mt**3 * p0[1] + 3 * mt**2 * t * p1[1] + 3 * mt * t**2 * p2[1] + t**3 * p3[1]
        pts.append((x, y))
    return pts


def _shield_outline(w: int, h: int) -> list[tuple[float, float]]:
    """Closed polygon (px coords) for the interstate-crest shield silhouette:
    arched top, bulged sides, pointed bottom. Normalized control points are
    the approved Round 2 design (see CLAUDE.md); mirrored left/right."""
    top = _bezier_points((0.06, 0.14), (0.30, 0.06), (0.70, 0.06), (0.94, 0.14))
    right = _bezier_points((0.94, 0.14), (1.00, 0.42), (0.92, 0.62), (0.78, 0.78))
    left = _bezier_points((0.22, 0.78), (0.08, 0.62), (0.00, 0.42), (0.06, 0.14))
    norm = top + right[1:] + [(0.50, 1.00), (0.22, 0.78)] + left[1:]
    return [(x * w, y * h) for x, y in norm]


def _shield_width_px(height_px: int) -> int:
    """Shield image width for a given height - shared by render_shield()
    (the actual pixels) and render_info_frames() (layout math, which needs
    a width even on seconds with no highway match, e.g. unmatched or
    local-road-only) so the two stay in sync rather than drifting."""
    return max(1, round(height_px * 0.92))  # crest is slightly taller than wide


def render_shield(route_id: str, route_type: str, height_px: int):
    """Programmatic highway shield graphic (RGBA PIL.Image), no external
    assets. Interstate: blue field, red top-30% band, white bold number,
    white border. US route: white field, black bold number and border.
    Both share the same crest silhouette from `_shield_outline`.
    """
    from PIL import Image, ImageDraw

    w = _shield_width_px(height_px)
    h = height_px
    scale = h / 26.0  # design was specified at a 26px reference height
    border_px = max(1, round(1.4 * scale))
    outline = _shield_outline(w, h)

    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).polygon(outline, fill=255)

    if route_type == "interstate":
        field = Image.new("RGBA", (w, h), (0x00, 0x3F, 0x87, 255))
        ImageDraw.Draw(field).rectangle(
            [0, 0, w, round(h * 0.30)], fill=(0xBF, 0x20, 0x26, 255))
        border_color = (255, 255, 255, 255)
        text_color = (255, 255, 255, 255)
    else:
        field = Image.new("RGBA", (w, h), (255, 255, 255, 255))
        border_color = (0, 0, 0, 255)
        text_color = (0, 0, 0, 255)

    shield = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    shield.paste(field, (0, 0), mask)
    draw = ImageDraw.Draw(shield)
    draw.line(outline + [outline[0]], fill=border_color, width=border_px, joint="curve")

    number = route_id.rsplit("-", 1)[-1].rsplit(" ", 1)[-1]  # "I-30"->"30", "US 82"->"82"
    font = _load_font(max(1, round(h * 0.5)), bold=True)
    tb = draw.textbbox((0, 0), number, font=font)
    tw, th = tb[2] - tb[0], tb[3] - tb[1]
    draw.text((w / 2 - tw / 2 - tb[0], h * 0.60 - th / 2 - tb[1]), number,
              font=font, fill=text_color)
    return shield


def shield_cache_for(roads: list[dict], height_px: int) -> dict:
    """Pre-render one shield image per distinct route_id in roads, so
    render_shield() runs once per route instead of once per video-second."""
    cache: dict = {}
    for road in roads:
        rid = road["route_id"]
        if rid not in cache:
            cache[rid] = render_shield(rid, road["route_type"], height_px)
    return cache


def shield_alpha_per_second(route_ids: list[str | None],
                            fade_secs: int = 1) -> list[float]:
    """0.0-1.0 shield opacity per second, ramping linearly over `fade_secs`
    seconds on every route_id change (unmatched->matched, matched->unmatched,
    or a direct swap between two different roads counts as one change).
    fade_secs <= 1 means an instant on/off with no ramp."""
    if not route_ids:
        return []
    fade_secs = max(1, fade_secs)
    out: list[float] = []
    prev: object = object()  # sentinel: first second always counts as a change
    since_change = 0
    for rid in route_ids:
        if rid != prev:
            since_change = 0
            prev = rid
        else:
            since_change += 1
        ramp = min(1.0, (since_change + 1) / fade_secs)
        out.append(ramp if rid is not None else 1.0 - ramp)
    return out


def route_label(route_id: str | None, cardinal: str | None) -> str | None:
    """'I-30' + 'W' -> 'I-30 WB' - highway-sign-style route + direction-of-
    travel label shown left of the shield. None if unmatched (cardinal is
    already a travel direction letter from heading_to_cardinal, so a plain
    'B' suffix turns it into the familiar NB/EB/SB/WB convention)."""
    if not route_id or not cardinal:
        return None
    return f"{route_id} {cardinal}B"


def _faded(img, alpha: float):
    """Copy of a shield image with its alpha channel scaled by `alpha`
    (0.0-1.0). Returns None if fully transparent (nothing to draw)."""
    if alpha >= 0.999:
        return img
    if alpha <= 0.001:
        return None
    r, g, b, a = img.split()
    a = a.point(lambda v: int(v * alpha))
    from PIL import Image
    return Image.merge("RGBA", (r, g, b, a))


def render_info_frames(texts: list[str], matches: list[tuple[str | None, str | None]],
                       shields: dict, cfg: configparser.ConfigParser,
                       out_dir: Path, video_width_px: int,
                       frame_offset: int = 0) -> tuple[int, int]:
    """One transparent PNG per video-second: the speed/mi/time/date text,
    always horizontally center-locked on the frame, plus a highway shield
    in a FIXED-width slot to its left (and a "I-30 WB"-style route+direction
    label in a further fixed-width slot left of that) that fade in/out
    together (shield_alpha_per_second) without ever moving or resizing the
    text zone - per Sean's layout rule that the text must never shift when
    the shield appears/disappears. `matches` is the (route_id, cardinal)
    list from roads_per_second().
    """
    from PIL import Image, ImageDraw

    shield_h = cfg.getint("roads", "shield_height_px", fallback=52)
    zone_w = cfg.getint("roads", "text_zone_width_px", fallback=460)
    gap = cfg.getint("roads", "shield_gap_px", fallback=12)
    label_w = cfg.getint("roads", "route_label_width_px", fallback=140)
    label_gap = cfg.getint("roads", "route_label_gap_px", fallback=6)
    label_font_size = cfg.getint("roads", "route_label_font_size", fallback=20)
    font_size = cfg.getint("info", "font_size", fallback=26)
    fade_secs = cfg.getint("roads", "shield_fade_secs", fallback=1)
    strip_h = max(shield_h, font_size * 2) + 10
    font = _load_font(font_size, bold=False)
    label_font = _load_font(label_font_size, bold=True)
    route_ids = [m[0] for m in matches]
    alphas = shield_alpha_per_second(route_ids, fade_secs)
    cx = video_width_px // 2

    # Computed unconditionally (not sampled from `shields`, which may be
    # empty on a local-road-only match, or even always empty if [roads] is
    # disabled but [local_roads] is on) so the label position is identical
    # whether the current match is a highway (shield + label) or a local
    # road (label only) - Round 3's version of the original "shield fading
    # in/out must never shift the text" rule.
    shield_w = _shield_width_px(shield_h)
    shield_x = cx - zone_w // 2 - gap - shield_w
    label_x = shield_x - label_gap - label_w
    if label_x < 0:
        print("WARNING: [roads] shield/route-label slot doesn't fit at "
              "this video width (text_zone_width_px/shield_gap_px/"
              "route_label_width_px too large); will be clipped "
              "off-frame", file=sys.stderr)

    out_dir.mkdir(parents=True, exist_ok=True)
    for i, (text, (rid, cardinal), alpha) in enumerate(zip(texts, matches, alphas)):
        img = Image.new("RGBA", (video_width_px, strip_h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        tb = draw.textbbox((0, 0), text, font=font)
        tw, th = tb[2] - tb[0], tb[3] - tb[1]
        draw.text((cx - tw / 2 - tb[0], strip_h / 2 - th / 2 - tb[1]), text,
                  font=font, fill=(255, 255, 255, 255),
                  stroke_width=2, stroke_fill=(0, 0, 0, 255))
        if rid and alpha > 0.001:
            # Shield graphic: only for a highway match (rid present in the
            # pre-rendered `shields` cache, which is only ever built from
            # [roads]' road list). A local-road match's rid is never in
            # `shields`, so this simply doesn't draw one - no route_type
            # branching needed here.
            if rid in shields:
                shield = _faded(shields[rid], alpha)
                if shield is not None:
                    sy = (strip_h - shield.height) // 2
                    if 0 <= shield_x and shield_x + shield.width <= video_width_px:
                        img.alpha_composite(shield, (shield_x, sy))
            # Route/street label: drawn for BOTH highway and local-road
            # matches, always right-aligned to the same fixed shield_x, so
            # it never shifts whether or not a shield is actually present.
            label = route_label(rid, cardinal)
            if label:
                a = max(0, min(255, round(255 * alpha)))
                ltb = draw.textbbox((0, 0), label, font=label_font)
                lw, lh = ltb[2] - ltb[0], ltb[3] - ltb[1]
                lx = shield_x - label_gap - lw - ltb[0]  # right-aligned, hugs the shield slot
                ly = strip_h / 2 - lh / 2 - ltb[1]
                if lx >= 0:
                    draw.text((lx, ly), label, font=label_font,
                              fill=(255, 255, 255, a),
                              stroke_width=2, stroke_fill=(0, 0, 0, a))
        img.save(out_dir / f"{i + frame_offset:06d}.png")
        if i and i % 500 == 0:
            print(f"  [info-strip] frames: {i}/{len(texts)}")
    return video_width_px, strip_h


def overlay_xy(corner: str, margin: int) -> str:
    positions = {
        "top_right": f"main_w-overlay_w-{margin}:{margin}",
        "top_left": f"{margin}:{margin}",
        "bottom_right": f"main_w-overlay_w-{margin}:main_h-overlay_h-{margin}",
        "bottom_left": f"{margin}:main_h-overlay_h-{margin}",
    }
    return positions.get(corner, positions["top_right"])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default="config.ini")
    ap.add_argument("--skip-map", action="store_true",
                    help="reuse existing map/info-strip frames from a previous run")
    ap.add_argument("--dry-run", action="store_true",
                    help="build everything but print the ffmpeg command instead of running it")
    args = ap.parse_args()

    cfg = configparser.ConfigParser()
    # Explicit encoding: configparser.read() otherwise falls back to
    # locale.getpreferredencoding() (cp1252 on plain Windows installs),
    # which mis-decodes non-ASCII bytes in config.ini (e.g. the info-line
    # "·" separator becomes "Â·"). config.ini is UTF-8.
    if not cfg.read(args.config, encoding="utf-8"):
        print(f"ERROR: cannot read config file {args.config}", file=sys.stderr)
        return 1

    work = Path(cfg.get("paths", "work_folder"))
    output = cfg.get("paths", "output_file")
    for required in ("track.csv", "labels.csv", "concat.txt"):
        if not (work / required).exists():
            print(f"ERROR: {work / required} missing - run extract_gps.py first",
                  file=sys.stderr)
            return 1

    with open(work / "track.csv", newline="", encoding="utf-8") as fh:
        track_rows = list(csv.DictReader(fh))
    with open(work / "labels.csv", newline="", encoding="utf-8") as fh:
        spans = [(float(r["start_sec"]), float(r["end_sec"]), r["label"])
                 for r in csv.DictReader(fh)]

    # Round 4: GPS-dark clips (extract_gps.py) keep their footage but have
    # no GPS data - dark_spans marks those stretches so the town label /
    # info line / shield don't silently show stale forward-filled data
    # across them. Empty list (the common case) makes every downstream
    # split_spans_for_gaps()/no_gps_seconds() call a no-op.
    dark_spans = [(s, e) for s, e, _clip in load_gaps(work / "gaps.csv")]
    no_gps_text = cfg.get("info", "no_gps_text", fallback="NO GPS LOCK")
    spans = split_spans_for_gaps(spans, dark_spans, fill_text=None)

    duration_file = work / "duration_sec"
    if duration_file.exists():
        total_secs = int(math.ceil(float(duration_file.read_text(encoding="utf-8").strip())))
    else:
        # Back-compat: pre-Round-4 work/ folders never wrote duration_sec -
        # fall back to the old track.csv-derived estimate, which undercounts
        # if the trailing clip(s) were GPS-dark.
        total_secs = int(math.ceil(max(float(r["global_sec"]) for r in track_rows))) + 1

    labels_on = cfg.getboolean("labels", "enabled", fallback=True)
    trip_on = cfg.getboolean("map_trip", "enabled", fallback=True)
    day_on = cfg.getboolean("map_day", "enabled", fallback=True)
    info_on = cfg.getboolean("info", "enabled", fallback=False)
    roads_on = cfg.getboolean("roads", "enabled", fallback=False)
    local_roads_on = cfg.getboolean("local_roads", "enabled", fallback=False)
    # Round 2/3: the info-strip PNG overlay replaces the ASS "Info" style
    # entirely when either [roads] (highway shields) or [local_roads]
    # (street-name fallback) is enabled; otherwise info stays in the ASS
    # track exactly as before (backward compatible with Round 1 configs).
    info_via_strip = info_on and (roads_on or local_roads_on)
    info_via_ass = info_on and not (roads_on or local_roads_on)

    if labels_on:
        info_spans = (split_spans_for_gaps(
                          build_info_spans(track_rows, cfg, end_time=total_secs),
                          dark_spans, no_gps_text)
                     if info_via_ass else None)
        (work / "labels.ass").write_text(build_ass(spans, cfg, info_spans),
                                         encoding="utf-8")
        print(f"Wrote {work / 'labels.ass'} ({len(spans)} town events, "
              f"{len(info_spans or [])} info events)")

    if (trip_on or day_on or info_via_strip) and not args.skip_map:
        positions = positions_per_second(track_rows, total_secs)
        if trip_on:
            print(f"Rendering {total_secs} trip-map inset frames...")
            w, h = render_map_frames(positions, cfg, work / "map_trip", section="map_trip")
            print(f"  trip inset size: {w}x{h}")
        if day_on:
            dates = dates_per_second(track_rows, total_secs, cfg)
            segments = day_segments(dates)
            print(f"Rendering {total_secs} day-map inset frames "
                  f"({len(segments)} local-day segment(s))...")
            day_width = cfg.getint("map_day", "width", fallback=320)
            shared_h = _canvas_height_px(positions, day_width)
            for seg_start, seg_end in segments:
                w, h = render_map_frames(
                    positions[seg_start:seg_end], cfg, work / "map_day",
                    section="map_day", frame_offset=seg_start, height_px=shared_h)
            print(f"  day inset size: {w}x{h}")
        if info_via_strip:
            headings = headings_per_second(track_rows, total_secs)
            shields: dict = {}
            highway_matches: list[tuple[str | None, str | None]] = [(None, None)] * total_secs
            if roads_on:
                roads_file = cfg.get("roads", "roads_file",
                                     fallback="map_data/synthetic_roads_test.geojson")
                roads = load_roads(Path(roads_file))
                if not roads:
                    print(f"WARNING: [roads] enabled but no roads loaded from "
                          f"{roads_file}; shields will never show", file=sys.stderr)
                shield_h = cfg.getint("roads", "shield_height_px", fallback=52)
                shields = shield_cache_for(roads, shield_h)
                highway_matches = roads_per_second(
                    positions, headings, roads,
                    tolerance_mi=cfg.getfloat("roads", "tolerance_mi", fallback=0.031),
                    grace_secs=cfg.getint("roads", "grace_secs", fallback=3))

            # Round 3: local (non-highway) street names, own roads_file /
            # tolerance_mi / grace_secs (denser network, tighter defaults -
            # see [local_roads] in config.ini). merge_road_matches() keeps
            # the highway match whenever there is one; local only fills in
            # the seconds [roads] left unmatched.
            local_matches: list[tuple[str | None, str | None]] = [(None, None)] * total_secs
            if local_roads_on:
                local_file = cfg.get("local_roads", "roads_file",
                                     fallback="map_data/local_roads.geojson")
                local_roads_list = load_roads(Path(local_file))
                if not local_roads_list:
                    print(f"WARNING: [local_roads] enabled but no roads loaded "
                          f"from {local_file}; local road names will never show",
                          file=sys.stderr)
                local_matches = roads_per_second(
                    positions, headings, local_roads_list,
                    tolerance_mi=cfg.getfloat("local_roads", "tolerance_mi", fallback=0.02),
                    grace_secs=cfg.getint("local_roads", "grace_secs", fallback=2))

            matches = merge_road_matches(highway_matches, local_matches)
            texts = info_text_per_second(track_rows, cfg, total_secs)

            # Round 4: force both text and match to the no-GPS state on any
            # dark second, AFTER the highway/local merge - a dark clip has
            # no track.csv points at all, so positions/matches would
            # otherwise just keep showing whatever was last known.
            dark_mask = no_gps_seconds(dark_spans, total_secs)
            texts = [no_gps_text if dark_mask[i] else t for i, t in enumerate(texts)]
            matches = [(None, None) if dark_mask[i] else m
                      for i, m in enumerate(matches)]

            highway_secs = sum(1 for rid, _ in matches if rid and rid in shields)
            local_secs = sum(1 for rid, _ in matches if rid and rid not in shields)
            dark_secs = sum(dark_mask)
            print(f"Rendering {total_secs} info-strip frames "
                  f"({highway_secs} sec highway-matched, {local_secs} sec "
                  f"local-road fallback, {dark_secs} sec no-GPS-lock)...")
            iw, ih = render_info_frames(texts, matches, shields, cfg,
                                        work / "info_strip", SOURCE_VIDEO_WIDTH_PX)
            print(f"  info-strip size: {iw}x{ih}")

    filters = []
    inputs = ["-f", "concat", "-safe", "0", "-i", str(work / "concat.txt")]
    tail = "[0:v]"
    next_input = 1
    if trip_on:
        inputs += ["-framerate", "1", "-start_number", "0",
                  "-i", str(work / "map_trip" / "%06d.png")]
        xy = overlay_xy(cfg.get("map_trip", "corner", fallback="top_left"),
                        cfg.getint("map_trip", "margin", fallback=20))
        filters.append(f"[{next_input}:v]format=rgba[trip];"
                       f"{tail}[trip]overlay={xy}:shortest=1[v{next_input}]")
        tail = f"[v{next_input}]"
        next_input += 1
    if day_on:
        inputs += ["-framerate", "1", "-start_number", "0",
                  "-i", str(work / "map_day" / "%06d.png")]
        xy = overlay_xy(cfg.get("map_day", "corner", fallback="top_right"),
                        cfg.getint("map_day", "margin", fallback=20))
        filters.append(f"[{next_input}:v]format=rgba[day];"
                       f"{tail}[day]overlay={xy}:shortest=1[v{next_input}]")
        tail = f"[v{next_input}]"
        next_input += 1
    if info_via_strip:
        inputs += ["-framerate", "1", "-start_number", "0",
                  "-i", str(work / "info_strip" / "%06d.png")]
        margin_v = cfg.getint("roads", "margin_v", fallback=8)
        xy = f"0:main_h-overlay_h-{margin_v}"
        filters.append(f"[{next_input}:v]format=rgba[infostrip];"
                       f"{tail}[infostrip]overlay={xy}:shortest=1[v{next_input}]")
        tail = f"[v{next_input}]"
        next_input += 1
    if labels_on:
        ass_path = (work / "labels.ass").as_posix()
        filters.append(f"{tail}subtitles='{ass_path}'[vout]")
        tail = "[vout]"
    if not filters:
        print("ERROR: no overlays enabled in config; nothing to do", file=sys.stderr)
        return 1

    preview_scale = cfg.get("video", "preview_scale", fallback="").strip()
    if preview_scale:
        m = re.fullmatch(r"(\d+)x(\d+)", preview_scale)
        if not m:
            print(f"ERROR: [video] preview_scale must be WIDTHxHEIGHT "
                  f"(e.g. 960x540), got {preview_scale!r}", file=sys.stderr)
            return 1
        filters.append(f"{tail}scale={m.group(1)}:{m.group(2)}[vscaled]")
        tail = "[vscaled]"

    cmd = ["ffmpeg", "-y"] + inputs
    cmd += ["-filter_complex", ";".join(filters), "-map", tail,
            "-an", "-c:v", "libx264",
            "-crf", cfg.get("video", "crf", fallback="20"),
            "-preset", cfg.get("video", "preset", fallback="medium"),
            "-pix_fmt", "yuv420p", output]

    if args.dry_run:
        print("DRY RUN - ffmpeg command:")
        print(" ".join(cmd))
        return 0

    print("Running ffmpeg (this is the long step)...")
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as exc:
        print(f"ERROR: ffmpeg failed with exit code {exc.returncode}", file=sys.stderr)
        return exc.returncode
    print(f"Done: {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
