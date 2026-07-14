#!/usr/bin/env python3
"""Stage 2: render town labels + route-progress map inset onto the timelapse.

Consumes the work folder produced by extract_gps.py (track.csv, labels.csv,
concat.txt), generates an ASS subtitle track and per-second map inset frames,
then runs a single ffmpeg pass that concatenates the clips and burns in both
overlays.

Usage: python render_overlay.py [--config config.ini] [--skip-map] [--dry-run]
"""
from __future__ import annotations

__version__ = "0.1.0"

import argparse
import configparser
import csv
import hashlib
import json
import math
import re
import string
import subprocess
import sys
import time
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


def ffmpeg_filter_path(path: Path) -> str:
    """Escape a filesystem path for embedding inside an ffmpeg filtergraph
    string (e.g. the subtitles= filter). ffmpeg's filtergraph parser treats
    ':' as a key=value separator, so an absolute Windows path's drive-letter
    colon (e.g. "D:/GitHub/RoadBurner/work/labels.ass") breaks the subtitles
    filter's own argument parsing unless escaped. Relative paths - the
    shipped config.example.ini default - have no colon, so this is a no-op
    for the common case; it only matters if work_folder is ever set to an
    absolute path."""
    return path.as_posix().replace(":", r"\:")


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


_DIM_FIELDS = {"speed", "dist", "remain"}


def _split_leading_zeros(numeral: str) -> tuple[str, str]:
    """('072') -> ('0', '72'); ('0000') -> ('000', '0'); ('999') -> ('', '999').
    Always leaves at least the final character bright, so a genuine zero
    value still shows a lit digit (classic digital-odometer look). Handles
    a leading '-' sign so the split stays correct if a formatted value is
    ever negative (shouldn't happen for speed/dist/remain, but keeps this
    a safe general-purpose helper)."""
    sign, digits = ("-", numeral[1:]) if numeral.startswith("-") else ("", numeral)
    i = 0
    while i < len(digits) - 1 and digits[i] == "0":
        i += 1
    return sign + digits[:i], digits[i:]


def _average_speeds_mph(rows: list[dict]) -> list[float]:
    """Per-row speed (mph), one value per row in `rows` (already sorted by
    global_sec), computed as haversine distance to the previous VALID fix
    divided by the real elapsed time between their timestamps - rather
    than trusting the device's own raw instantaneous speed_mph field.

    Why: on a timelapse-mode capture (e.g. one GPS-tagged frame per real
    minute, confirmed on Sean's real trip data), each displayed video-
    second represents a full real minute with only ONE raw device speed
    reading for that whole window - a single transient GPS receiver
    glitch (common right around a stop - multipath/doppler noise while
    stationary) shows up as gospel for that entire displayed second, with
    nothing to average it against. Distance-over-time between real fixes
    is immune to that: it reflects where the vehicle actually was, not
    what the receiver's sensor briefly hiccuped to report. Confirmed
    against real data: a raw-reported 53.5 mph at a rest stop where the
    position barely moved works out to ~4.2 mph by this method.

    Caveat: on a sharp curve between two fixes, straight-line distance
    slightly understates the true driven distance (same disclosed
    limitation extract_gps.py's find_time_gaps() already documents for
    mileage) - a much smaller error than a spurious instantaneous spike,
    but not perfect.

    Falls back to the row's own raw speed_mph for an invalid row, the
    very first valid fix (no previous point to measure from), and any
    pair of fixes with non-positive elapsed time (shouldn't happen -
    chunks are found in increasing time order - but guarded rather than
    dividing by zero).
    """
    from datetime import datetime
    out: list[float] = []
    prev_pt: tuple[float, float] | None = None
    prev_ts: datetime | None = None
    for r in rows:
        if r["valid"] != "1":
            out.append(float(r["speed_mph"]))
            prev_pt, prev_ts = None, None
            continue
        pt = (float(r["lat"]), float(r["lon"]))
        ts = datetime.strptime(r["timestamp_utc"], "%Y-%m-%d %H:%M:%S")
        if prev_pt is not None and prev_ts is not None:
            elapsed_h = (ts - prev_ts).total_seconds() / 3600.0
            out.append(haversine_miles(*prev_pt, *pt) / elapsed_h
                       if elapsed_h > 0 else float(r["speed_mph"]))
        else:
            out.append(float(r["speed_mph"]))
        prev_pt, prev_ts = pt, ts
    return out


def _info_segments_by_point(track_rows: list[dict], cfg: configparser.ConfigParser
                            ) -> list[tuple[float, list[tuple[str, bool]]]]:
    """[(global_sec, [(text, is_dim), ...]), ...] sorted by time - the shared
    speed/cumulative-miles/miles-left/local-time computation used by both
    the legacy ASS 'Info' style (_info_text_by_point) and the Round 2
    PNG-strip renderer (info_text_per_second).

    Structured (segment-list) rather than a single opaque string so the
    zero-padded speed/dist/remain fields can be split into a dimmed
    leading-zero run plus full-brightness significant digits (classic
    digital-odometer look). Walks the [info] format string generically via
    string.Formatter().parse() so any field order/literal text the user
    configures still works, not just the shipped default.
    """
    fmt = cfg.get("info", "format",
                  fallback="{speed:.0f} mph   {dist:.0f} mi   {remain:.0f} mi to go   {time}")
    adjust = cfg.getfloat("info", "utc_offset_adjust", fallback=0.0)
    speed_source = cfg.get("info", "speed_source", fallback="average")
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
    speeds = (_average_speeds_mph(rows) if speed_source == "average"
             else [float(r["speed_mph"]) for r in rows])
    parsed = list(string.Formatter().parse(fmt))
    out: list[tuple[float, list[tuple[str, bool]]]] = []
    for i, r in enumerate(rows):
        zone = tz_name_for(r["state"], float(r["lon"])) if r["state"] else None
        values = {
            "speed": speeds[i], "dist": dists[i],
            "remain": max(total - dists[i], 0.0),
            "time": format_local_time(r["timestamp_utc"], zone, adjust),
            "date": format_local_date(r["timestamp_utc"], zone, adjust),
        }
        segments: list[tuple[str, bool]] = []
        try:
            for literal, field_name, spec, _conversion in parsed:
                if literal:
                    segments.append((literal, False))
                if field_name is None:
                    continue
                text = format(values[field_name], spec or "")
                if field_name in _DIM_FIELDS:
                    dim, bright = _split_leading_zeros(text)
                    if dim:
                        segments.append((dim, True))
                    segments.append((bright, False))
                else:
                    segments.append((text, False))
        except (KeyError, ValueError) as exc:
            print(f"ERROR: bad [info] format string: {exc}", file=sys.stderr)
            break
        out.append((float(r["global_sec"]), segments))
    return out


_ASS_DIM_COLOR = r"{\c&H808080&}"
_ASS_BRIGHT_COLOR = r"{\c&HFFFFFF&}"


def _segments_to_ass_text(segments: list[tuple[str, bool]]) -> str:
    """Flatten (text, is_dim) segments into one ASS-tagged string - inline
    \\c color-override tags switch between dim gray and full white only at
    segment boundaries, so build_ass() needs no changes at all; it already
    inserts the Info style's Text field verbatim into the Dialogue line."""
    parts: list[str] = []
    cur_dim: bool | None = None
    for text, is_dim in segments:
        if is_dim != cur_dim:
            parts.append(_ASS_DIM_COLOR if is_dim else _ASS_BRIGHT_COLOR)
            cur_dim = is_dim
        parts.append(text)
    return "".join(parts)


def _info_text_by_point(track_rows: list[dict], cfg: configparser.ConfigParser
                        ) -> list[tuple[float, str]]:
    """[(global_sec, formatted info text incl. ASS dim/bright color tags),
    ...] sorted by time - the ASS-ready flattening of
    _info_segments_by_point(), used by the legacy ASS 'Info' style
    (build_info_spans)."""
    return [(sec, _segments_to_ass_text(segs))
            for sec, segs in _info_segments_by_point(track_rows, cfg)]


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
                         total_secs: int) -> list[list[tuple[str, bool]]]:
    """One (text, is_dim) segment list per whole video-second, forward-filled
    (same pattern as positions_per_second/dates_per_second). Feeds the
    Round 2 PNG-strip renderer (render_info_frames) instead of the ASS
    'Info' style. Segment-list rather than a plain string so
    render_info_frames() can draw the dimmed-leading-zero digits at reduced
    brightness without re-parsing anything."""
    points = _info_segments_by_point(track_rows, cfg)
    if not points:
        raise ValueError("track.csv contains no rows to build an info line from")
    by_sec = {int(sec): segs for sec, segs in points}
    out: list[list[tuple[str, bool]]] = []
    last = by_sec[min(by_sec)]
    for sec in range(total_secs):
        last = by_sec.get(sec, last)
        out.append(last)
    return out


def build_ass(spans: list[tuple[float, float, str]], cfg: configparser.ConfigParser,
              info_spans: list[tuple[float, float, str]] | None = None,
              day_titles: list[tuple[float, float, str]] | None = None) -> str:
    font = cfg.get("labels", "font", fallback="Arial")
    size = cfg.getint("labels", "font_size", fallback=48)
    align = ASS_ALIGN.get(cfg.get("labels", "position", fallback="bottom_center"), 2)
    margin_v = cfg.getint("labels", "margin_v", fallback=40)
    fade = cfg.getint("labels", "fade_ms", fallback=500)
    outline = cfg.getint("labels", "outline", fallback=2)
    # Day-title cards ("Day 2 - Bristol, VA to Texarkana, TX") get their own
    # style: top-center, deep enough MarginV to clear the map insets (which
    # occupy roughly the top 270px at default settings), independent font/
    # fade settings from [day_title]. Fully separate from Town/Info above -
    # nothing else shares that screen region, so a plain ASS \fad() tag is
    # enough (no PNG compositing needed, unlike the shield/compass).
    day_title_font = cfg.get("day_title", "font", fallback=font)
    day_title_size = cfg.getint("day_title", "font_size", fallback=56)
    day_title_outline = cfg.getint("day_title", "outline", fallback=3)
    day_title_margin_v = cfg.getint("day_title", "margin_v", fallback=280)
    day_title_fade = cfg.getint("day_title", "fade_ms", fallback=300)
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
        f"{cfg.getint('info', 'margin_v', fallback=8)},1\n"
        f"Style: DayTitle,{day_title_font},{day_title_size},&H00FFFFFF,"
        f"&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,"
        f"{day_title_outline},0,8,40,40,{day_title_margin_v},1\n\n"
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
    for start, end, text in day_titles or []:
        if end - start <= 0.01:
            continue
        events.append(
            f"Dialogue: 1,{ass_time(start)},{ass_time(end)},DayTitle,,0,0,0,,"
            f"{{\\fad({day_title_fade},{day_title_fade})}}{text}"
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


def speeds_per_second(track_rows: list[dict], total_secs: int) -> list[float]:
    """One GPS speed (mph) per whole video-second, forward-filled (same
    pattern as positions_per_second/headings_per_second). Added 2026-07-14
    to feed roads_per_second()'s freeze-while-stopped logic - see there."""
    by_sec: dict[int, float] = {}
    for row in track_rows:
        if row["valid"] == "1":
            by_sec[int(float(row["global_sec"]))] = float(row["speed_mph"])
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


def day_title_text(day_number: int, start_label: str, end_label: str) -> str:
    """Pure formatter for a day-title card: "Day 2 - Bristol, VA to
    Texarkana, TX". start_label/end_label are already "Town, ST" strings
    straight from labels.csv (see extract_gps.py's resolve_town_labels) -
    no separate town/state parsing needed."""
    return f"Day {day_number} - {start_label} to {end_label}"


def day_title_segments(day_segs: list[tuple[int, int]],
                       spans: list[tuple[float, float, str]],
                       display_secs: float = 2.0,
                       min_duration_secs: float = 2.0
                       ) -> list[tuple[float, float, str]]:
    """Builds (start_sec, end_sec, text) day-title card spans for
    build_ass(), one per local-day segment from day_segments() that's long
    enough to show a full card and has usable town-label data at both ends.

    Day numbers ("Day N") are assigned by chronological segment position
    (1-indexed over ALL of day_segs), not by how many cards actually get
    shown - a skipped segment still "used up" its day number, so "Day 3"
    always means the third calendar day of the trip, never "the third card
    rendered."

    A segment shorter than `min_duration_secs` is skipped outright - showing
    a card would otherwise risk bleeding into the next segment's card. A
    segment with no non-blank town label anywhere in its range (e.g. an
    entirely GPS-dark day) is also skipped - there's no town name to build
    a title from; `spans` is expected in the same gap-split form used
    everywhere else (see split_spans_for_gaps() - an empty label means "no
    data here", not a real town).

    `end_sec` for a shown card is clipped to the segment's own end, so a
    `display_secs` longer than `min_duration_secs` still can't bleed into
    the next segment even if the two are configured differently.
    """
    results: list[tuple[float, float, str]] = []
    for day_number, (start_idx, end_idx) in enumerate(day_segs, start=1):
        if end_idx - start_idx < min_duration_secs:
            continue
        start_label = None
        for s, e, label in spans:
            if label and e > start_idx and s < end_idx:
                start_label = label
                break
        end_label = None
        for s, e, label in reversed(spans):
            if label and e > start_idx and s < end_idx:
                end_label = label
                break
        if start_label is None or end_label is None:
            continue
        card_end = min(float(start_idx) + display_secs, float(end_idx))
        results.append((float(start_idx), card_end,
                        day_title_text(day_number, start_label, end_label)))
    return results


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


def _letterbox_pad(lon_span: float, lat_span: float, mean_lat: float,
                   width_px: int, height_px: int) -> tuple[float, float]:
    """Extra (lon, lat) degrees of padding to add to each side of an
    already-padded route bounding box so it fits inside a fixed
    width_px x height_px pixel panel without geographic distortion.

    Used when a panel's pixel size is locked by config (rather than
    auto-fit to the route's own shape via _canvas_height_px()) - e.g. so
    [map_trip]/[map_day] stay the same size across every render regardless
    of how each render's particular route happens to be shaped. Expands
    whichever axis (lon or lat) has "slack" relative to the target pixel
    aspect ratio, leaving the other axis's extent untouched, so the route
    is letterboxed (empty margin added) rather than stretched to fill the
    box. Returns (0.0, 0.0) if the content already matches the target
    aspect ratio (nothing to add)."""
    aspect_correction = 1.0 / max(math.cos(math.radians(mean_lat)), 0.2)
    target_aspect = width_px / height_px
    content_aspect = lon_span / (lat_span * aspect_correction)
    if content_aspect > target_aspect:
        # Content is proportionally wider than the target box - widen the
        # latitude span (add vertical margin) until it matches.
        needed_lat_span = lon_span / (target_aspect * aspect_correction)
        return 0.0, max(0.0, (needed_lat_span - lat_span) / 2)
    # Content is proportionally taller than (or equal to) the target box -
    # widen the longitude span (add horizontal margin) until it matches.
    needed_lon_span = lat_span * aspect_correction * target_aspect
    return max(0.0, (needed_lon_span - lon_span) / 2), 0.0


def render_map_frames(positions: list[tuple[float, float]], cfg: configparser.ConfigParser,
                      out_dir: Path, section: str = "map", frame_offset: int = 0,
                      height_px: int | None = None,
                      alphas: list[float] | None = None) -> tuple[int, int]:
    """Write one inset PNG per video-second for `positions`; returns (width, height) px.

    `section` selects which config section (e.g. "map_trip" / "map_day")
    supplies all style/data keys, so one engine drives both map instances.
    `frame_offset` shifts output filenames so a subset of the timeline (e.g.
    one local-day segment) lands in the right slot of a shared frame sequence.
    `height_px`, given explicitly, overrides the aspect-computed canvas height
    so multiple calls writing into the same sequence share one frame size.
    `alphas`, given explicitly, is one 0.0-1.0 opacity per frame (see
    day_segment_fade_alpha()) scaling the whole panel's alpha channel after
    it's drawn - used for the map_day fade at local-midnight boundaries.
    Every frame still gets a PNG written even at alpha 0.0 (fully
    transparent), since ffmpeg's image2 demuxer needs one file per
    video-second in the sequence."""
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

    # A fixed panel size (explicit height_px param - map_day's shared
    # cross-segment height - or a configured [section] height key - map_trip,
    # or map_day with no per-segment override) letterboxes the route inside
    # that exact box instead of auto-fitting the canvas to this call's own
    # route shape, so the panel stays visually consistent across renders
    # with differently-shaped routes. See _letterbox_pad().
    config_height = cfg.getint(section, "height", fallback=0)
    fixed_height = height_px if height_px is not None else (
        config_height if config_height > 0 else None)
    if fixed_height is not None:
        height_px = fixed_height
        mean_lat = sum(lats) / len(lats)
        lon_span = (max(lons) - min(lons)) + 2 * pad_lon
        lat_span = (max(lats) - min(lats)) + 2 * pad_lat
        extra_lon, extra_lat = _letterbox_pad(lon_span, lat_span, mean_lat,
                                              width_px, height_px)
        pad_lon += extra_lon
        pad_lat += extra_lat
    else:
        height_px = _canvas_height_px(positions, width_px)

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

    # Optional caption (e.g. "TRIP MAP" / "SEGMENT MAP") so it's clear at
    # a glance which inset is the whole-trip route and which is the
    # current local-day segment - blank/unset draws nothing (backward
    # compatible with configs written before this existed).
    caption = cfg.get(section, "caption", fallback="").strip()
    if caption:
        # Top-center, clear of both the bottom-left city-label cluster and
        # the top-right compass/route-data corner - see CLAUDE.md, Sean
        # caught "SEGMENT MAP" overlapping a city label at the old
        # bottom-left position.
        ax.text(0.5, 0.95, caption, transform=ax.transAxes,
               ha="center", va="top",
               fontsize=cfg.getfloat(section, "caption_font_size", fallback=10),
               color=cfg.get(section, "caption_color", fallback="#ffffff"),
               weight="bold", zorder=1.6,
               path_effects=[pe.withStroke(linewidth=2, foreground="#000000")])

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
        frame_path = out_dir / f"{sec + frame_offset:06d}.png"
        fig.savefig(frame_path, transparent=True)
        alpha = alphas[sec] if alphas is not None else 1.0
        if alpha < 0.999:
            from PIL import Image
            img = Image.open(frame_path).convert("RGBA")
            r, g, b, a = img.split()
            a = a.point(lambda v: int(v * alpha))
            Image.merge("RGBA", (r, g, b, a)).save(frame_path)
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


_COMPASS8_POINTS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")


def heading_to_compass8(heading_deg: float) -> str:
    """GPS heading in degrees (0 = north, clockwise) -> nearest of the 8
    compass points (N/NE/E/SE/S/SW/W/NW). Separate from the 4-point
    heading_to_cardinal() above, which feeds the NB/EB/SB/WB highway-sign-
    style shield label and must keep its own 4-way behavior - this one
    feeds the standalone compass indicator instead."""
    idx = int(((heading_deg % 360) + 22.5) // 45) % 8
    return _COMPASS8_POINTS[idx]


def _circular_mean_deg(degrees: list[float]) -> float:
    """Circular mean of a list of compass headings in degrees, so e.g.
    averaging 350 and 10 correctly gives 0 (not 180, as a naive arithmetic
    mean would - the wraparound at 360/0 breaks plain averaging)."""
    if not degrees:
        raise ValueError("no headings to average")
    sin_sum = sum(math.sin(math.radians(d)) for d in degrees)
    cos_sum = sum(math.cos(math.radians(d)) for d in degrees)
    # Double mod: a tiny negative angle right at the 0/360 wraparound (e.g.
    # exact opposite headings whose sin/cos sums nearly cancel to a
    # floating-point-noise-sized negative value) can land at precisely
    # -0.0-ish, and a single `% 360` on that rounds to 360.0 instead of
    # 0.0 - one more `% 360` folds that back down correctly.
    return (math.degrees(math.atan2(sin_sum, cos_sum)) % 360) % 360


def compass_per_second(track_rows: list[dict], total_secs: int,
                       window_secs: int = 5, freeze_below_mph: float = 3.0
                       ) -> list[float]:
    """One smoothed heading (degrees) per whole video-second, feeding the
    compass indicator. `heading` in track.csv is real device-reported GPS
    course (parsed straight out of the Novatek freeGPS chunk alongside
    speed - see extract_gps.py), not a derived point-to-point bearing, so
    it doesn't have the "noisy at low displacement" problem a computed
    bearing would. Even so, two stability layers on top of the raw
    per-point value, so the needle "shouldn't be jumping around
    maniacally":

      1. Forward-fill to per-second (same pattern as headings_per_second()),
         then a circular moving average over `window_secs` seconds, so
         brief GPS course noise doesn't visibly snap the needle frame to
         frame.
      2. Freeze (hold the last smoothed heading) on any second where speed
         is below `freeze_below_mph` - while stopped, device-reported
         heading is frequently noise, not a real orientation change.

    Cardinal-label hysteresis (N/NE/E/... snapping) is handled separately
    by cardinal8_per_second(), same debounce shape as roads_per_second().
    """
    by_sec_heading: dict[int, float] = {}
    by_sec_speed: dict[int, float] = {}
    for row in track_rows:
        if row["valid"] == "1":
            sec = int(float(row["global_sec"]))
            by_sec_heading[sec] = float(row["heading"])
            by_sec_speed[sec] = float(row["speed_mph"])
    if not by_sec_heading:
        raise ValueError("track.csv contains no valid GPS fixes")

    raw: list[float] = []
    spd: list[float] = []
    last_h = by_sec_heading[min(by_sec_heading)]
    last_s = by_sec_speed[min(by_sec_speed)]
    for sec in range(total_secs):
        last_h = by_sec_heading.get(sec, last_h)
        last_s = by_sec_speed.get(sec, last_s)
        raw.append(last_h)
        spd.append(last_s)

    half = window_secs // 2
    smoothed: list[float] = []
    for i in range(total_secs):
        lo, hi = max(0, i - half), min(total_secs, i + half + 1)
        smoothed.append(_circular_mean_deg(raw[lo:hi]))

    out: list[float] = []
    frozen = smoothed[0] if smoothed else 0.0
    for i in range(total_secs):
        if spd[i] >= freeze_below_mph:
            frozen = smoothed[i]
        out.append(frozen)
    return out


def cardinal8_per_second(smoothed_headings: list[float],
                         hold_secs: int = 2) -> list[str]:
    """Per-second 8-point compass label with hysteresis: the displayed
    label only changes once the nearest-octant for the current heading has
    differed from the current label for more than `hold_secs` consecutive
    seconds - same debounce shape as roads_per_second(), so brief
    boundary noise (e.g. hovering right at the S/SW line) doesn't flicker
    the label back and forth.

    Fixed 2026-07-14: the mismatch streak now tracks "consecutive seconds
    not matching the currently displayed label", not "consecutive seconds
    of one specific alternate candidate repeating". The earlier version
    required the exact same candidate octant to repeat hold_secs+1 times
    in a row before switching - fine for boundary flicker (which really
    does bounce between the same two octants), but during a real
    continuous turn the smoothed heading can sweep through several
    different octants one after another (e.g. E -> SE -> S -> SW, each one
    different from the last), which reset that counter every second and
    never let it fire - the label stayed stuck on its pre-turn value for
    the turn's entire duration, then snapped once heading finally held
    still. Counting "away from current" instead of "same repeated
    candidate" still absorbs a heading hovering right at one boundary
    (each dip resets to 0 as soon as it matches current again) but no
    longer stalls indefinitely through a multi-octant sweep - see
    TestCardinal8PerSecond.test_continuous_multi_octant_sweep_does_not_stall.
    """
    out: list[str] = []
    if not smoothed_headings:
        return out
    current = heading_to_compass8(smoothed_headings[0])
    mismatch_streak = 0
    for h in smoothed_headings:
        candidate = heading_to_compass8(h)
        if candidate == current:
            mismatch_streak = 0
        else:
            mismatch_streak += 1
            if mismatch_streak > hold_secs:
                current = candidate
                mismatch_streak = 0
        out.append(current)
    return out


# Route-hierarchy tie-break for concurrent highways (multiple route numbers
# signed on the same physical roadway - e.g. I-10 and US 70 running
# together near Deming, NM). TIGER primary-roads data digitizes each
# concurrent route as its own near-duplicate line, so plain nearest-
# distance is close to a coin flip between them second to second. Lower
# number = higher priority; unlisted route_types (e.g. a future
# "state_route") sort last via the .get() default below. This is the
# same convention every consumer mapping product (Google/Apple/Waze)
# uses for concurrencies - the Interstate is the corridor's "real"
# identity even where a state/US route shares the pavement.
_ROUTE_TYPE_PRIORITY = {"interstate": 0, "us_route": 1}


def nearest_road(lat: float, lon: float, roads: list[dict],
                 tolerance_mi: float) -> tuple[str | None, float]:
    """Closest road's route_id within tolerance_mi, and its distance.
    route_id is None (distance still returned) if nothing is in range.

    Among everything within tolerance, the winner is picked by route-type
    tier first (_ROUTE_TYPE_PRIORITY) and distance only as the tie-break
    within a tier - see the module note above. `best_dist` itself still
    tracks the single closest candidate regardless of tier, since that's
    what decides whether anything is in range at all."""
    best_dist = float("inf")
    best_id: str | None = None
    best_key: tuple[int, float] | None = None
    for road in roads:
        d = point_to_polyline_miles(lat, lon, road["geometry"])
        if d < best_dist:
            best_dist = d
        if d > tolerance_mi:
            continue
        key = (_ROUTE_TYPE_PRIORITY.get(road["route_type"], 99), d)
        if best_key is None or key < best_key:
            best_key = key
            best_id = road["route_id"]
    if best_dist > tolerance_mi:
        return None, best_dist
    return best_id, best_dist


_POINT_CACHE_PRECISION = 5  # decimal places (~1.1m) - far finer than any
# tolerance_mi in use, so quantizing to this precision can't flip a
# tolerance decision; it only lets two near-identical GPS fixes (repeat
# renders of the same clip, a stopped vehicle, etc.) share one answer.


def _quantize_point(lat: float, lon: float) -> str:
    """Cache key for one GPS fix, rounded coarser than GPS noise but far
    finer than any road-matching tolerance - see _POINT_CACHE_PRECISION."""
    return f"{lat:.{_POINT_CACHE_PRECISION}f},{lon:.{_POINT_CACHE_PRECISION}f}"


def nearest_road_cached(lat: float, lon: float, roads: list[dict],
                        tolerance_mi: float,
                        cache: dict[str, tuple[str | None, float]],
                        road_index: dict | None = None
                        ) -> tuple[str | None, float]:
    """nearest_road(), memoized on rounded (lat, lon) in `cache`.

    nearest_road() is a pure function of (lat, lon, roads, tolerance_mi),
    so it's a clean memoization target - and it's specifically the
    expensive one: roads_per_second() only calls it while unmatched, which
    for a highway-only stretch of trip is nearly every second against
    [local_roads]'s dense street file. Caching by point (not by track/
    clip identity, like _cache_*_matches.json) means the cache survives
    adding, removing, or reordering clips - only genuinely new GPS fixes
    pay the brute-force cost.

    `road_index`, if given (see build_road_segment_index()), swaps the
    underlying lookup from nearest_road()'s brute-force scan to
    nearest_road_indexed()'s grid-bucketed one on a cache miss - same
    result, found faster. None (the default) preserves the exact old
    brute-force behavior for any caller that doesn't pass an index."""
    key = _quantize_point(lat, lon)
    hit = cache.get(key)
    if hit is not None:
        return hit
    if road_index is not None:
        result = nearest_road_indexed(lat, lon, road_index, tolerance_mi)
    else:
        result = nearest_road(lat, lon, roads, tolerance_mi)
    cache[key] = result
    return result


# --- Road-matching performance: spatial grid index + route grouping -----
# Diagnosed 2026-07-15 (Sean: "painfully slow road matching"). Two distinct
# O(N) costs were hiding in this module, both scaling with the SIZE OF THE
# ROAD FILE rather than the length of the drive - the real problem for
# [local_roads] (306K real-world segments) and, to a lesser extent,
# [roads] (5,980 segments):
#
#   Cost 1: nearest_road()'s cold-start scan (current is None) walks every
#   entry in `roads`, doing a full point-to-polyline distance check against
#   each - even though almost all of them are obviously nowhere near the
#   query point. For [local_roads], the vehicle is on a mapped local street
#   only rarely, so this cold-start path runs on nearly every second.
#
#   Cost 2: even once matched, the "am I still within tolerance of my
#   CURRENT road" check did `r for r in roads if r["route_id"] == current`
#   - a full pass over the entire roads list just to filter by id, every
#   second, matched or not.
#
# Fixed here with two independent, backward-compatible additions (both are
# opt-in via new optional params - a caller that doesn't pass them gets the
# exact old brute-force behavior, so every existing test/caller is
# unaffected):
#
#   - build_road_segment_index() / nearest_road_indexed(): a lat/lon grid
#     bucketing roads by SEGMENT (not whole polyline), so a query only
#     checks segments in nearby cells instead of the whole file. Cell size
#     is independent of tolerance_mi (a fixed 1 mi default) so a tight
#     tolerance doesn't blow up the number of cells a long segment spans;
#     the query side widens its search ring instead
#     (rings = ceil(tolerance_mi / cell_size_mi)), which is provably
#     sufficient to find every segment within tolerance - see the module
#     docstring math in nearest_road_indexed().
#   - group_roads_by_id(): a one-time {route_id: [geometries]} grouping so
#     the Cost-2 check is O(segments for that one route) instead of O(all
#     roads).
#
# On top of both, a second cache dimension - current_road_distance_cached()
# - memoizes the Cost-2 distance itself per (route_id, quantized point), so
# a re-render after a clip-set change (which invalidates the whole-track
# match cache) never recomputes distance for any GPS fix it's already
# evaluated, matched or not - complementing the existing point cache, which
# only ever covered the cold-start lookup.

_ROAD_GRID_CELL_SIZE_MI = 1.0  # fixed, independent of tolerance_mi - see
# the module note above for why coupling cell size to a tight tolerance
# would be a mistake (a single long TIGER segment could span hundreds of
# tiny cells). The query side compensates by widening its search ring.


def _point_to_segment_miles(px: float, py: float,
                            ax: float, ay: float, bx: float, by: float) -> float:
    """Distance in miles from a projected point (px, py) to a projected
    segment (ax, ay)-(bx, by). Projected = (lon * kx, lat * ky), same
    equirectangular convention as point_to_polyline_miles() - factored out
    here because the grid-indexed path does many single-segment checks
    (not one whole-polyline walk), so it needs the inner-loop math without
    re-deriving kx/ky or re-walking a full route on every call."""
    dx, dy = bx - ax, by - ay
    seg_len2 = dx * dx + dy * dy
    if seg_len2 == 0:
        t = 0.0
    else:
        t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg_len2))
    cx, cy = ax + t * dx, ay + t * dy
    return math.hypot(px - cx, py - cy)


def build_road_segment_index(roads: list[dict],
                             cell_size_mi: float = _ROAD_GRID_CELL_SIZE_MI
                             ) -> dict:
    """Bucket every road's individual segments (consecutive point pairs,
    not whole polylines) into a lat/lon grid, so nearest_road_indexed() can
    check only nearby segments instead of the whole file. Pure function of
    `roads` - build once per render, reuse across every query in that
    render (and across [roads]/[local_roads] separately, since they're
    different road files).

    Returns a dict: {"segments": [(route_id, route_type, alat, alon, blat,
    blon), ...], "grid": {(cx, cy): [segment indices]}, "kx", "ky"
    (equirectangular projection factors, fixed from the road data's own
    mean latitude), "cell_size_mi"}.

    A segment is bucketed into every cell its bounding box overlaps (not
    just the cell containing one endpoint), so a long segment spanning
    several cells is still found from any of them - see
    nearest_road_indexed()'s docstring for why this makes the search
    provably correct, not just a heuristic."""
    segments: list[tuple[str, str, float, float, float, float]] = []
    lat_sum, lat_n = 0.0, 0
    for road in roads:
        geom = road["geometry"]
        for (alat, alon), (blat, blon) in zip(geom, geom[1:]):
            segments.append((road["route_id"], road["route_type"],
                             alat, alon, blat, blon))
            lat_sum += alat
            lat_n += 1
    if lat_n == 0:
        return {"segments": [], "grid": {}, "kx": MILES_PER_DEG_LAT,
                "ky": MILES_PER_DEG_LAT, "cell_size_mi": cell_size_mi}
    mean_lat = lat_sum / lat_n
    kx = MILES_PER_DEG_LAT * max(math.cos(math.radians(mean_lat)), 0.2)
    ky = MILES_PER_DEG_LAT
    grid: dict[tuple[int, int], list[int]] = {}
    for i, (_rid, _rtype, alat, alon, blat, blon) in enumerate(segments):
        ax, ay = alon * kx, alat * ky
        bx, by = blon * kx, blat * ky
        cx0, cx1 = sorted((math.floor(ax / cell_size_mi), math.floor(bx / cell_size_mi)))
        cy0, cy1 = sorted((math.floor(ay / cell_size_mi), math.floor(by / cell_size_mi)))
        for cx in range(cx0, cx1 + 1):
            for cy in range(cy0, cy1 + 1):
                grid.setdefault((cx, cy), []).append(i)
    return {"segments": segments, "grid": grid, "kx": kx, "ky": ky,
            "cell_size_mi": cell_size_mi}


def nearest_road_indexed(lat: float, lon: float, index: dict,
                         tolerance_mi: float) -> tuple[str | None, float]:
    """nearest_road(), but searching only the grid cells near (lat, lon)
    instead of every road in the file. Same (route_id, distance) contract
    and same tier-then-distance tie-break as nearest_road() - see
    _ROUTE_TYPE_PRIORITY.

    Correctness: search widens to `rings = ceil(tolerance_mi /
    cell_size_mi)` cells in every direction from the query's own cell.
    For a query point anywhere within its cell, the searched square always
    extends at least tolerance_mi past the query point on every side
    (worst case is the point sitting on a cell edge, where the extra
    partial cell exactly makes up the difference) - so no segment within
    tolerance_mi can be missed. A segment is only found via a cell its
    bounding box overlaps, and build_road_segment_index() buckets every
    segment into every cell it overlaps, so a segment spanning multiple
    cells is still found from any of them.

    Returns (None, best_dist) if nothing indexed is within tolerance -
    best_dist is float('inf') if no candidate segment was found in the
    searched cells at all (matches nearest_road()'s empty-roads contract),
    otherwise the closest distance actually seen."""
    segments = index["segments"]
    if not segments:
        return None, float("inf")
    kx, ky, cell_size_mi = index["kx"], index["ky"], index["cell_size_mi"]
    grid = index["grid"]
    px, py = lon * kx, lat * ky
    cx0 = math.floor(px / cell_size_mi)
    cy0 = math.floor(py / cell_size_mi)
    rings = max(1, math.ceil(tolerance_mi / cell_size_mi))
    seen: set[int] = set()
    best_dist = float("inf")
    best_key: tuple[int, float] | None = None
    best_id: str | None = None
    for cx in range(cx0 - rings, cx0 + rings + 1):
        for cy in range(cy0 - rings, cy0 + rings + 1):
            for seg_i in grid.get((cx, cy), ()):
                if seg_i in seen:
                    continue
                seen.add(seg_i)
                rid, rtype, alat, alon, blat, blon = segments[seg_i]
                ax, ay = alon * kx, alat * ky
                bx, by = blon * kx, blat * ky
                d = _point_to_segment_miles(px, py, ax, ay, bx, by)
                if d < best_dist:
                    best_dist = d
                if d > tolerance_mi:
                    continue
                key = (_ROUTE_TYPE_PRIORITY.get(rtype, 99), d)
                if best_key is None or key < best_key:
                    best_key = key
                    best_id = rid
    if best_key is None:
        return None, best_dist
    return best_id, best_dist


def concurrent_road_ids_indexed(lat: float, lon: float, index: dict,
                                tolerance_mi: float, exclude_id: str | None,
                                max_extra: int = 1) -> list[str]:
    """All distinct route_ids (other than `exclude_id`) within tolerance_mi
    of (lat, lon) - feeds the "concurrent designations" label, e.g.
    "I-10 / US 70 WB" where I-10 and US-70 run physically concurrent near
    Deming, NM (TIGER digitizes each designation as its own near-duplicate
    line feature at the same real-world location - see the road-hierarchy
    bug note above _ROUTE_TYPE_PRIORITY). Sorted by the same
    (_ROUTE_TYPE_PRIORITY, distance) tie-break as nearest_road_indexed(),
    capped at `max_extra` results.

    Same grid-cell ring search (and the same correctness argument) as
    nearest_road_indexed(), but collects the best distance seen per
    distinct route_id instead of stopping at the single closest candidate.
    Deliberately has no brute-force twin - unlike nearest_road()/
    nearest_road_indexed(), this only ever runs from main() after a
    road_index has already been built for the primary match, so there's no
    caller that needs the unindexed path."""
    segments = index["segments"]
    if not segments:
        return []
    kx, ky, cell_size_mi = index["kx"], index["ky"], index["cell_size_mi"]
    grid = index["grid"]
    px, py = lon * kx, lat * ky
    cx0 = math.floor(px / cell_size_mi)
    cy0 = math.floor(py / cell_size_mi)
    rings = max(1, math.ceil(tolerance_mi / cell_size_mi))
    seen_segments: set[int] = set()
    best_by_id: dict[str, tuple[int, float]] = {}
    for cx in range(cx0 - rings, cx0 + rings + 1):
        for cy in range(cy0 - rings, cy0 + rings + 1):
            for seg_i in grid.get((cx, cy), ()):
                if seg_i in seen_segments:
                    continue
                seen_segments.add(seg_i)
                rid, rtype, alat, alon, blat, blon = segments[seg_i]
                if rid == exclude_id:
                    continue
                ax, ay = alon * kx, alat * ky
                bx, by = blon * kx, blat * ky
                d = _point_to_segment_miles(px, py, ax, ay, bx, by)
                if d > tolerance_mi:
                    continue
                key = (_ROUTE_TYPE_PRIORITY.get(rtype, 99), d)
                prev = best_by_id.get(rid)
                if prev is None or key < prev:
                    best_by_id[rid] = key
    ordered = sorted(best_by_id.items(), key=lambda kv: kv[1])
    return [rid for rid, _key in ordered[:max_extra]]


def group_roads_by_id(roads: list[dict]) -> dict[str, list[list[tuple[float, float]]]]:
    """{route_id: [geometry, ...]} built once per render, so roads_per_
    second()'s per-second 'am I still within tolerance of my CURRENTLY
    MATCHED road' check is O(segments for that one route_id) instead of
    O(every road in the file) - see the Cost 2 note in the module comment
    above nearest_road_indexed(). Pure function of `roads`."""
    grouped: dict[str, list[list[tuple[float, float]]]] = {}
    for road in roads:
        grouped.setdefault(road["route_id"], []).append(road["geometry"])
    return grouped


def current_road_distance_cached(lat: float, lon: float, route_id: str,
                                 roads_by_id: dict[str, list[list[tuple[float, float]]]],
                                 cache: dict[str, float]) -> float:
    """Distance in miles from (lat, lon) to the nearest geometry piece
    matching route_id, memoized on f"{route_id}|{quantized point}".

    Complements nearest_road_cached()'s point cache, which only ever
    covered the cold-start 'find anything nearby' lookup - this covers the
    OTHER expensive per-second check (Cost 2 above), which runs every
    second a road is already matched, not just on cold-start seconds.
    Together, a re-render after any clip-set change (which invalidates the
    whole-track match cache, since it's keyed on track.csv content) never
    recomputes distance for a GPS fix it's already evaluated - matched or
    not - only genuinely new fixes pay to compute anything."""
    key = f"{route_id}|{_quantize_point(lat, lon)}"
    hit = cache.get(key)
    if hit is not None:
        return hit
    dist = min((point_to_polyline_miles(lat, lon, g)
               for g in roads_by_id.get(route_id, ())), default=float("inf"))
    cache[key] = dist
    return dist


def roads_per_second(positions: list[tuple[float, float]], headings: list[float],
                     roads: list[dict], tolerance_mi: float = 0.031,
                     grace_secs: int = 3,
                     point_cache: dict[str, tuple[str | None, float]] | None = None,
                     speeds: list[float] | None = None,
                     freeze_below_mph: float = 3.0,
                     road_index: dict | None = None,
                     roads_by_id: dict[str, list[list[tuple[float, float]]]] | None = None,
                     distance_cache: dict[str, float] | None = None,
                     progress_label: str | None = None,
                     progress_every_secs: float = 15.0
                     ) -> list[tuple[str | None, str | None]]:
    """Per-second (route_id, cardinal_direction) with hysteresis, so a brief
    interchange gap doesn't flicker the shield on and off. Once matched, a
    road stays matched through gaps shorter than `grace_secs` consecutive
    out-of-tolerance seconds; longer gaps (or never matching) give
    (None, None). Switching to a *different* road only happens once the
    current one has been dropped - no snapping to a closer parallel road
    while still within tolerance of the current match.

    tolerance_mi default ~0.031 mi (~50 m), matching the design spec.

    `point_cache`, if given, memoizes the expensive nearest_road() lookups
    across calls (see nearest_road_cached) - pass the same dict in across
    a whole render (and persist/reload it between renders) to skip
    recomputation for GPS fixes seen before.

    `speeds`, if given (one mph value per second, same length as positions/
    headings), freezes the displayed (route_id, cardinal) pair - and the
    underlying hysteresis state, so the "how long have we been off-road"
    clock doesn't even tick while frozen - whenever speed is below
    `freeze_below_mph`. Added 2026-07-14: `heading_to_cardinal()` was being
    fed raw unsmoothed per-second heading with no protection at all, so a
    parked vehicle (heading is pure GPS noise while stopped) could show the
    NB/EB/SB/WB suffix flickering every second; separately, sitting still
    near a real road junction (e.g. Van Horn, TX, where I-10 and US-90
    actually meet) could genuinely drift far enough from the matched
    road's mapped centerline to exhaust `grace_secs` and re-lock onto a
    different nearby road, purely from being parked, not from driving
    anywhere. A stopped vehicle isn't meaningfully "on" any particular
    road or heading any particular direction, so freezing the whole match
    to its last known-good value while stopped fixes both symptoms at
    once - same freeze-while-stopped philosophy already used by
    compass_per_second(). `speeds=None` (the default) preserves the exact
    old per-second re-evaluation behavior for any caller that doesn't pass
    speeds (e.g. old cached-match compatibility, tests that don't care).

    2026-07-15 performance additions (all optional, all default to the
    exact old brute-force behavior - see the module note above
    nearest_road_indexed() for the diagnosis):

    `road_index` (see build_road_segment_index()) speeds up the cold-start
    "nothing matched, find anything nearby" lookup via nearest_road_
    indexed() instead of scanning every road in the file.

    `roads_by_id` (see group_roads_by_id()) speeds up the "am I still
    within tolerance of my CURRENT road" check from a full-list filter-by-
    id scan to just that route's own segments.

    `distance_cache` (see current_road_distance_cached()), if given
    alongside `roads_by_id`, additionally memoizes that same check per
    (route_id, point) - so a re-render after any clip-set change never
    recomputes distance for a GPS fix it's already evaluated, matched or
    not.

    `progress_label`, if given, prints a periodic (every
    `progress_every_secs` wall-clock seconds, plus always on the final
    position) flush=True progress line - the matching phase against a
    real local-roads file can take many minutes with no other output.
    """
    if len(positions) != len(headings):
        raise ValueError("positions and headings must be the same length")
    if speeds is not None and len(speeds) != len(positions):
        raise ValueError("speeds must be the same length as positions")
    out: list[tuple[str | None, str | None]] = []
    current: str | None = None
    off_count = 0
    n = len(positions)
    start_time = time.monotonic()
    last_progress = start_time
    for i, ((lat, lon), heading) in enumerate(zip(positions, headings)):
        if speeds is not None and out and speeds[i] < freeze_below_mph:
            out.append(out[-1])
            continue
        if current is not None:
            if roads_by_id is not None:
                if distance_cache is not None:
                    current_dist = current_road_distance_cached(
                        lat, lon, current, roads_by_id, distance_cache)
                else:
                    current_dist = min(
                        (point_to_polyline_miles(lat, lon, g)
                         for g in roads_by_id.get(current, ())),
                        default=float("inf"))
            else:
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
            if point_cache is not None:
                current, _ = nearest_road_cached(lat, lon, roads, tolerance_mi,
                                                 point_cache, road_index=road_index)
            elif road_index is not None:
                current, _ = nearest_road_indexed(lat, lon, road_index, tolerance_mi)
            else:
                current, _ = nearest_road(lat, lon, roads, tolerance_mi)
        out.append((current, heading_to_cardinal(heading) if current else None))
        if progress_label is not None:
            now = time.monotonic()
            if now - last_progress >= progress_every_secs or i == n - 1:
                elapsed = now - start_time
                pct = (i + 1) / n * 100 if n else 100.0
                rate = (i + 1) / elapsed if elapsed > 0 else 0.0
                eta = (n - i - 1) / rate if rate > 0 else 0.0
                print(f"  [{progress_label}] {i + 1}/{n} positions "
                      f"({pct:.1f}%), {elapsed:.0f}s elapsed, "
                      f"~{eta:.0f}s remaining", flush=True)
                last_progress = now
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


def concurrent_designations_per_second(
        positions: list[tuple[float, float]],
        matches: list[tuple[str | None, str | None]],
        road_index: dict, tolerance_mi: float,
        speeds: list[float] | None = None,
        freeze_below_mph: float = 3.0,
        max_extra: int = 1) -> list[list[str]]:
    """Per-second list of extra route_ids physically concurrent with the
    already-matched primary road (`matches[i][0]`) - e.g. ["US 70"] on a
    stretch where I-10 and US-70 run the same pavement, empty on any
    unmatched second.

    Deliberately a separate, stateless lookup layered on top of
    roads_per_second()'s already-hysteresis-stabilized primary match,
    rather than a change to that state machine itself - which has been the
    site of two real flicker bugs this project (the Van Horn road/cardinal
    freeze fix, the compass multi-octant-sweep stall). The only stability
    measure here is the same freeze-while-stopped rule roads_per_second()
    itself uses (`speeds`/`freeze_below_mph`): holds the last extras list
    steady while parked, so a stopped vehicle near a real concurrent-route
    junction can't flicker the secondary designation on and off from GPS
    noise alone.

    Only ever called against the highway ([roads]) match/index - concurrent
    LOCAL street names aren't a real-world thing the way numbered highway
    concurrencies are, so this is intentionally out of scope for
    [local_roads]."""
    if speeds is not None and len(speeds) != len(positions):
        raise ValueError("speeds must be the same length as positions")
    out: list[list[str]] = []
    for i, ((lat, lon), (rid, _cardinal)) in enumerate(zip(positions, matches)):
        if speeds is not None and out and speeds[i] < freeze_below_mph:
            out.append(out[-1])
            continue
        if rid is None:
            out.append([])
            continue
        out.append(concurrent_road_ids_indexed(
            lat, lon, road_index, tolerance_mi, exclude_id=rid, max_extra=max_extra))
    return out


# --- Road-match caching --------------------------------------------------
# roads_per_second() is the expensive part of a render (brute-force
# point-to-polyline matching against every road, per second - tens of
# minutes against the real local-roads dataset). It's a pure function of
# the GPS track, the road data, and the matching parameters - nothing
# about info-line formatting, map insets, or the compass. Caching its
# output per work_folder means a config-only re-render (fixing a text
# layout bug, say) doesn't force a full rematch.

def _file_content_hash(path: Path) -> str:
    """SHA-256 hex digest of a file's contents, read in 1MB chunks so this
    stays cheap in memory even for the 200MB+ local-roads GeoJSON. A few
    seconds of hashing is negligible next to the tens of minutes it saves
    by skipping a redundant road-matching pass."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _road_match_cache_key(track_csv_path: Path, roads_file_path: Path,
                          tolerance_mi: float, grace_secs: int,
                          total_secs: int,
                          freeze_below_mph: float = 3.0) -> str:
    """Stable cache key for one roads_per_second() pass: changes if and
    only if something that could actually change the match result changes
    - the GPS track content, the road data content, or the matching
    parameters. Deliberately independent of [info]/[labels]/[compass]
    settings, [video] preview_scale, etc.

    `freeze_below_mph` added 2026-07-14 alongside roads_per_second()'s new
    freeze-while-stopped parameter of the same name - folding it into the
    hash means any pre-existing cache (computed before this parameter
    existed, or with a different value) naturally misses and recomputes
    once, rather than silently reusing a match sequence that didn't freeze
    while parked."""
    parts = [
        _file_content_hash(track_csv_path),
        _file_content_hash(roads_file_path),
        f"{tolerance_mi}", f"{grace_secs}", f"{total_secs}",
        f"{freeze_below_mph}",
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def _load_cached_matches(cache_path: Path, key: str
                         ) -> list[tuple[str | None, str | None]] | None:
    """Cached roads_per_second() output if `cache_path` exists and its
    stored key matches `key`; None on any kind of miss (missing file, key
    mismatch, or an unreadable/corrupt cache). A cache miss just means
    "recompute" - never raises, so a damaged cache file can't break a
    render, only cost it the time it would've taken anyway."""
    if not cache_path.exists():
        return None
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("key") != key:
        return None
    try:
        return [(rid, cardinal) for rid, cardinal in data["matches"]]
    except (KeyError, TypeError, ValueError):
        return None


def _save_cached_matches(cache_path: Path, key: str,
                         matches: list[tuple[str | None, str | None]]) -> None:
    """Best-effort cache write - a failure here (e.g. disk full, odd
    permissions) shouldn't fail an otherwise-successful render, just mean
    the next run recomputes instead of hitting a cache."""
    try:
        cache_path.write_text(json.dumps({"key": key, "matches": matches}),
                              encoding="utf-8")
    except OSError as exc:
        print(f"WARNING: could not write road-match cache {cache_path}: "
              f"{exc}", file=sys.stderr)


def _load_point_cache(cache_path: Path, roads_hash: str, tolerance_mi: float
                      ) -> dict[str, tuple[str | None, float]]:
    """Point-level nearest_road_cached() cache for one roads_file/
    tolerance_mi combo. Unlike _load_cached_matches, this is independent
    of the GPS track (clip set) entirely - keyed only on the road data and
    tolerance, so it survives adding/removing/reordering clips. Any kind
    of miss (missing file, stale roads_hash/tolerance_mi, corrupt JSON)
    just means an empty cache - never raises, costs time, not correctness."""
    if not cache_path.exists():
        return {}
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if (not isinstance(data, dict)
            or data.get("roads_hash") != roads_hash
            or data.get("tolerance_mi") != tolerance_mi):
        return {}
    try:
        return {k: (v[0], v[1]) for k, v in data["points"].items()}
    except (KeyError, TypeError, ValueError, IndexError):
        return {}


def _save_point_cache(cache_path: Path, roads_hash: str, tolerance_mi: float,
                      cache: dict[str, tuple[str | None, float]]) -> None:
    """Best-effort write, same philosophy as _save_cached_matches - a
    failed write costs the next run its cache, not the current render."""
    try:
        cache_path.write_text(json.dumps({
            "roads_hash": roads_hash,
            "tolerance_mi": tolerance_mi,
            "points": {k: list(v) for k, v in cache.items()},
        }), encoding="utf-8")
    except OSError as exc:
        print(f"WARNING: could not write point-match cache {cache_path}: "
              f"{exc}", file=sys.stderr)


def _load_distance_cache(cache_path: Path, roads_hash: str, tolerance_mi: float
                         ) -> dict[str, float]:
    """current_road_distance_cached() cache for one roads_file/tolerance_mi
    combo - same shape and same miss-is-silent philosophy as
    _load_point_cache(), just floats instead of (route_id, dist) tuples
    since the route_id is already part of each cache key."""
    if not cache_path.exists():
        return {}
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if (not isinstance(data, dict)
            or data.get("roads_hash") != roads_hash
            or data.get("tolerance_mi") != tolerance_mi):
        return {}
    try:
        return {k: float(v) for k, v in data["distances"].items()}
    except (KeyError, TypeError, ValueError):
        return {}


def _save_distance_cache(cache_path: Path, roads_hash: str, tolerance_mi: float,
                         cache: dict[str, float]) -> None:
    """Best-effort write, same philosophy as _save_point_cache."""
    try:
        cache_path.write_text(json.dumps({
            "roads_hash": roads_hash,
            "tolerance_mi": tolerance_mi,
            "distances": cache,
        }), encoding="utf-8")
    except OSError as exc:
        print(f"WARNING: could not write distance cache {cache_path}: "
              f"{exc}", file=sys.stderr)


# --- Round 2: programmatic highway shield graphics ---------------------

_FONT_CANDIDATES = {
    True: (r"C:\Windows\Fonts\arialbd.ttf",
           "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
           "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
    False: (r"C:\Windows\Fonts\arial.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"),
}

# Monospace, used specifically for the info-line text (speed/dist/remain/
# time/date) so the bar's width never drifts between digits, and so the
# dimmed-leading-zero segments line up cleanly against the bright digits
# next to them. Same two-tier Windows-then-Linux convention as
# _FONT_CANDIDATES above.
_MONO_FONT_CANDIDATES = {
    True: (r"C:\Windows\Fonts\consolab.ttf",
           "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
           "/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf"),
    False: (r"C:\Windows\Fonts\consola.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf"),
}


def _load_font(size_px: int, bold: bool = True, mono: bool = False):
    from PIL import ImageFont
    candidates = _MONO_FONT_CANDIDATES if mono else _FONT_CANDIDATES
    for path in candidates[bold]:
        try:
            return ImageFont.truetype(path, size_px)
        except OSError:
            continue
    kind = "monospace " if mono else ""
    print(f"WARNING: no {kind}{'bold ' if bold else ''}TTF font found; falling "
          "back to PIL's default bitmap font (fixed size, won't scale cleanly)",
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


_SHIELD_SUPERSAMPLE = 4  # render at Nx, downscale w/ LANCZOS for anti-aliasing


def render_shield(route_id: str, route_type: str, height_px: int):
    """Programmatic highway shield graphic (RGBA PIL.Image), no external
    assets. Interstate: blue field, red top-30% band, white bold number,
    white border. US route: white field, black bold number and border.
    Both share the same crest silhouette from `_shield_outline`.

    Internally supersampled: the silhouette/border/text are drawn at
    `_SHIELD_SUPERSAMPLE`x the requested height, then downscaled to the
    final size with LANCZOS. PIL's polygon/line/text drawing has no
    anti-aliasing at native resolution, which made the curved crest edges
    and border look jagged/rough at typical on-screen shield sizes -
    added 2026-07-14 per Sean's visual review ("crisper versions"). Purely
    a rendering-quality change: shape, colors, and the returned image size
    are unchanged, and shields are still cached once per route_id (not
    per-frame - see shield_cache_for()), so the extra render cost is
    negligible.
    """
    from PIL import Image, ImageDraw

    w = _shield_width_px(height_px)
    h = height_px
    sw, sh = w * _SHIELD_SUPERSAMPLE, h * _SHIELD_SUPERSAMPLE
    scale = sh / 26.0  # design was specified at a 26px reference height
    border_px = max(1, round(1.4 * scale))
    outline = _shield_outline(sw, sh)

    mask = Image.new("L", (sw, sh), 0)
    ImageDraw.Draw(mask).polygon(outline, fill=255)

    if route_type == "interstate":
        field = Image.new("RGBA", (sw, sh), (0x00, 0x3F, 0x87, 255))
        ImageDraw.Draw(field).rectangle(
            [0, 0, sw, round(sh * 0.30)], fill=(0xBF, 0x20, 0x26, 255))
        border_color = (255, 255, 255, 255)
        text_color = (255, 255, 255, 255)
    else:
        field = Image.new("RGBA", (sw, sh), (255, 255, 255, 255))
        border_color = (0, 0, 0, 255)
        text_color = (0, 0, 0, 255)

    shield = Image.new("RGBA", (sw, sh), (0, 0, 0, 0))
    shield.paste(field, (0, 0), mask)
    draw = ImageDraw.Draw(shield)
    draw.line(outline + [outline[0]], fill=border_color, width=border_px, joint="curve")

    number = route_id.rsplit("-", 1)[-1].rsplit(" ", 1)[-1]  # "I-30"->"30", "US 82"->"82"
    font = _load_font(max(1, round(sh * 0.5)), bold=True)
    tb = draw.textbbox((0, 0), number, font=font)
    tw, th = tb[2] - tb[0], tb[3] - tb[1]
    draw.text((sw / 2 - tw / 2 - tb[0], sh * 0.60 - th / 2 - tb[1]), number,
              font=font, fill=text_color)
    return shield.resize((w, h), Image.LANCZOS)


def shield_cache_for(roads: list[dict], height_px: int) -> dict:
    """Pre-render one shield image per distinct route_id in roads, so
    render_shield() runs once per route instead of once per video-second."""
    cache: dict = {}
    for road in roads:
        rid = road["route_id"]
        if rid not in cache:
            cache[rid] = render_shield(rid, road["route_type"], height_px)
    return cache


def _gaussian_glow(source, pad: int, radius_px: float, alpha: int,
                   color: tuple[int, int, int]):
    """Shared halo primitive: pads a copy of `source`'s own alpha channel
    by `pad` px on all sides, Gaussian-blurs it, scales the blur by
    `alpha`/255, and tints it `color`. Factored out so both
    shield_glow_cache_for() (one halo per distinct route_id, cached) and
    render_compass_rose() (recomputed per video-second, since heading
    varies continuously and isn't cacheable the same discrete way a
    route_id is) share one implementation instead of drifting apart.
    Returns the padded RGBA glow image - caller offsets the paste position
    by `-pad` in each direction relative to where `source` itself is drawn.
    """
    from PIL import Image, ImageFilter

    w, h = source.size
    mask = Image.new("L", (w + pad * 2, h + pad * 2), 0)
    mask.paste(source.split()[-1], (pad, pad))
    blurred = mask.filter(ImageFilter.GaussianBlur(radius_px))
    blurred = blurred.point(lambda v: min(255, int(v * alpha / 255)))
    glow = Image.new("RGBA", mask.size, color + (0,))
    glow.putalpha(blurred)
    return glow


def shield_glow_cache_for(shields: dict, radius_px: float, alpha: int,
                         color: tuple[int, int, int]) -> dict:
    """Pre-render one soft blurred halo per distinct shield in `shields`
    (same route_id keys), so the blur runs once per route instead of once
    per video-second - same caching rationale as shield_cache_for(). Added
    2026-07-14 per Sean's visual review ("dark halo works best").

    Returns {route_id: (glow_image, pad)} - `glow_image` is padded `pad`
    px on all sides beyond the shield's own size so the halo can bleed
    outward past the shield's silhouette; `pad` is returned so the caller
    knows how far to offset the paste position. render_info_frames() is
    responsible for clipping this to whatever room is actually available -
    the info strip's height is fixed by the "shield fading in/out must
    never shift the text" layout rule (see its docstring), so a large
    glow radius may get vertically clipped at the strip's top/bottom edge
    rather than growing the canvas. Horizontal room is effectively
    unconstrained (the strip spans the full video width).
    """
    pad = max(1, round(radius_px * 2.5))
    cache: dict = {}
    for route_id, shield in shields.items():
        cache[route_id] = (_gaussian_glow(shield, pad, radius_px, alpha, color), pad)
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


def route_label(route_id: str | None, cardinal: str | None,
                extra_ids: list[str] | None = None) -> str | None:
    """'I-30' + 'W' -> 'I-30 WB' - highway-sign-style route + direction-of-
    travel label shown left of the shield. None if unmatched (cardinal is
    already a travel direction letter from heading_to_cardinal, so a plain
    'B' suffix turns it into the familiar NB/EB/SB/WB convention).

    `extra_ids`, if given (see concurrent_designations_per_second()), are
    other route designations physically concurrent with route_id at this
    point (e.g. I-10 and US-70 running the same stretch near Deming, NM) -
    joined in with " / " ahead of the single shared direction suffix, since
    concurrent designations share the same pavement and therefore the same
    direction of travel: "I-10 / US 70 WB"."""
    if not route_id or not cardinal:
        return None
    ids = " / ".join([route_id, *(extra_ids or [])])
    return f"{ids} {cardinal}B"


def day_segment_fade_alpha(n_frames: int, fade_secs: int,
                           is_first_segment: bool, is_last_segment: bool
                           ) -> list[float]:
    """Per-frame opacity (0.0-1.0) for one map_day local-day segment, so the
    day-map panel fades out just before a local-midnight boundary and fades
    back in just after, instead of hard-cutting straight to the next day's
    bbox (see day_segments()/main()). Sibling to shield_alpha_per_second()
    above (same PIL-alpha-scaling consumer via _faded()-style logic in
    render_map_frames()), but this ramp reaches true 0.0 at the boundary
    edge itself rather than shield_alpha_per_second()'s "first frame after
    a change is already partially visible" shape - see below.

    The very first and very last segments of the whole render have no
    adjacent segment to transition from/to, so only *internal* boundaries
    fade: a non-first segment fades in over its first `fade_secs` frames,
    a non-last segment fades out over its last `fade_secs` frames. Each
    ramp reaches true 0.0 (fully transparent) exactly at the boundary edge
    frame (the very first frame of a fade-in, the very last frame of a
    fade-out), climbing linearly back to 1.0 over the following/preceding
    frames - a panel that visibly clears out of the corner rather than
    just dimming partway.
    fade_secs <= 0 disables fading entirely (every frame fully opaque - the
    original hard-cut behavior)."""
    if fade_secs <= 0 or n_frames <= 0:
        return [1.0] * n_frames
    # Never ramp more than half the segment, so a very short segment can't
    # end up with its fade-in and fade-out overlapping into extra
    # transparency in the middle.
    ramp = max(1, min(fade_secs, n_frames // 2))
    out = [1.0] * n_frames
    if not is_first_segment:
        for i in range(min(ramp, n_frames)):
            out[i] = min(out[i], i / ramp)
    if not is_last_segment:
        for i in range(min(ramp, n_frames)):
            out[n_frames - 1 - i] = min(out[n_frames - 1 - i], i / ramp)
    return out


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


_COMPASS_SUPERSAMPLE = 4  # same anti-aliasing approach as render_shield()


def render_compass_rose(heading_deg: float, size_px: int,
                        needle_color: tuple[int, int, int, int] = (211, 33, 33, 255),
                        tail_color: tuple[int, int, int, int] = (224, 224, 224, 255),
                        ring_color: tuple[int, int, int, int] = (255, 255, 255, 255),
                        label_color: tuple[int, int, int, int] = (255, 255, 255, 255)):
    """Programmatic compass-rose graphic (RGBA PIL.Image), no external
    assets - internally supersampled (`_COMPASS_SUPERSAMPLE`x, downscaled
    with LANCZOS) the same way render_shield() is, for anti-aliased edges
    instead of PIL's default jagged circle/polygon drawing.

    Fixed N (top)/E (right)/S (bottom)/W (left) tick labels ring the dial -
    these never rotate, matching the info strip's fixed "up = true north"
    reference frame (only the needle moves, same as the original design).
    The needle is a traditional two-tone compass-needle diamond: a red
    half pointing toward `heading_deg` (0 = north/up, clockwise) and a
    light-gray tail half pointing the opposite way, instead of the
    original plain line-plus-dot.

    Replaces the earlier `_draw_compass_rose()` (drew straight onto the
    strip's shared ImageDraw at native resolution) - added 2026-07-14 per
    Sean's request for "the same supersampling and drop shadow as the
    highway shield... a red arrow like a traditional compass and maybe
    NSEW markings". The drop-shadow/glow itself is applied by the caller
    (render_info_frames()) via the shared `_gaussian_glow()` helper, same
    as the shield - this function only returns the rose sprite itself.
    """
    from PIL import Image, ImageDraw

    ss = _COMPASS_SUPERSAMPLE
    size = size_px * ss
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    cx = cy = size / 2

    label_font = _load_font(max(1, round(size * 0.24)), bold=True)
    label_margin = max(1, round(size * 0.16))
    radius = size / 2 - label_margin
    ring_w = max(1, round(1.6 * ss))
    draw.ellipse([cx - radius, cy - radius, cx + radius, cy + radius],
                outline=ring_color, width=ring_w)

    for label, angle in (("N", 0.0), ("E", 90.0), ("S", 180.0), ("W", 270.0)):
        a = math.radians(angle)
        lx = cx + (radius + label_margin * 0.5) * math.sin(a)
        ly = cy - (radius + label_margin * 0.5) * math.cos(a)
        tb = draw.textbbox((0, 0), label, font=label_font)
        tw, th = tb[2] - tb[0], tb[3] - tb[1]
        draw.text((lx - tw / 2 - tb[0], ly - th / 2 - tb[1]), label,
                  font=label_font, fill=label_color)

    # Traditional two-tone diamond needle: red half toward heading_deg,
    # light-gray tail half opposite - split at the center point.
    rad = math.radians(heading_deg)
    dx, dy = math.sin(rad), -math.cos(rad)
    px, py = math.cos(rad), math.sin(rad)
    tip_len, tail_len, half_w = radius * 0.82, radius * 0.5, radius * 0.16
    tip = (cx + dx * tip_len, cy + dy * tip_len)
    tail = (cx - dx * tail_len, cy - dy * tail_len)
    left = (cx + px * half_w, cy + py * half_w)
    right = (cx - px * half_w, cy - py * half_w)
    draw.polygon([tip, left, right], fill=needle_color)
    draw.polygon([tail, left, right], fill=tail_color)
    center_r = max(1, round(size * 0.025))
    draw.ellipse([cx - center_r, cy - center_r, cx + center_r, cy + center_r],
                fill=ring_color)

    return img.resize((size_px, size_px), Image.LANCZOS)


def render_info_frames(texts: list[list[tuple[str, bool]]],
                       matches: list[tuple[str | None, str | None]],
                       shields: dict, cfg: configparser.ConfigParser,
                       out_dir: Path, video_width_px: int,
                       frame_offset: int = 0,
                       compass: list[tuple[float, str] | None] | None = None,
                       extras: list[list[str]] | None = None
                       ) -> tuple[int, int]:
    """One transparent PNG per video-second: the speed/mi/time/date text,
    always horizontally center-locked on the frame, plus a highway shield
    in a FIXED-width slot to its left (and a "I-30 WB"-style route+direction
    label in a further fixed-width slot left of that) that fade in/out
    together (shield_alpha_per_second) without ever moving or resizing the
    text zone - per Sean's layout rule that the text must never shift when
    the shield appears/disappears. `matches` is the (route_id, cardinal)
    list from roads_per_second().

    `extras`, if given (see concurrent_designations_per_second()), is a
    per-second list of extra route_ids physically concurrent with the
    primary match - folded into the SAME label slot via route_label()'s
    `extra_ids` param ("I-10 / US 70 WB"), still only one shield (the
    primary route's) and still right-aligned to the same fixed shield_x, so
    a concurrent stretch doesn't change the layout, just widens the text
    within its already-generously-sized zone (see [roads]
    route_label_width_px).

    `texts` is a per-second list of (text, is_dim) segments (see
    info_text_per_second / _info_segments_by_point) - drawn as multiple
    sequential draw.text() calls (dim gray for a leading-zero run, full
    white for the significant digits) instead of one, while landing at
    the exact same overall centered position a single draw.text() call on
    the concatenated string would have used, so the dim/bright split never
    shifts the bar.

    `compass`, when given, is a per-second (heading_deg, 8-point cardinal)
    tuple, or None for a second with no fix (e.g. GPS-dark), from
    compass_per_second() + cardinal8_per_second(). Mirrors the shield/
    label pair on the RIGHT of the text zone: a small rotating needle-in-
    circle plus e.g. "SW 225°" - only drawn when [compass] enabled=true.

    `[roads] shield_glow_enabled` (default false) draws a soft blurred
    halo behind each shield (shield_glow_cache_for()) - approved 2026-07-14
    after a real-footage comparison ("dark halo works best"); the halo is
    vertically clipped to the strip's existing height rather than growing
    the strip, since strip_h is load-bearing for the "shield fading in/out
    must never shift the text" rule - see the clipping logic below.
    """
    from PIL import Image, ImageDraw, ImageColor

    shield_h = cfg.getint("roads", "shield_height_px", fallback=52)
    zone_w = cfg.getint("roads", "text_zone_width_px", fallback=460)
    gap = cfg.getint("roads", "shield_gap_px", fallback=12)
    label_w = cfg.getint("roads", "route_label_width_px", fallback=140)
    label_gap = cfg.getint("roads", "route_label_gap_px", fallback=6)
    label_font_size = cfg.getint("roads", "route_label_font_size", fallback=20)
    font_size = cfg.getint("info", "font_size", fallback=26)
    fade_secs = cfg.getint("roads", "shield_fade_secs", fallback=1)
    strip_h = max(shield_h, font_size * 2) + 10
    font = _load_font(font_size, bold=False, mono=True)
    label_font = _load_font(label_font_size, bold=True)
    route_ids = [m[0] for m in matches]
    alphas = shield_alpha_per_second(route_ids, fade_secs)
    cx = video_width_px // 2

    glow_on = cfg.getboolean("roads", "shield_glow_enabled", fallback=False)
    glows: dict = {}
    if glow_on and shields:
        glow_radius = cfg.getfloat("roads", "shield_glow_radius_px", fallback=6.0)
        glow_alpha = cfg.getint("roads", "shield_glow_alpha", fallback=170)
        glow_color = ImageColor.getrgb(
            cfg.get("roads", "shield_glow_color", fallback="#000000"))
        glows = shield_glow_cache_for(shields, glow_radius, glow_alpha, glow_color)

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

    # Compass indicator: mirrors the shield/label pair, but on the right -
    # icon first (hugging the text zone), then the "SW 225" text further
    # out, so the two sides read as a matched pair.
    compass_on = compass is not None and cfg.getboolean("compass", "enabled", fallback=False)
    compass_size = cfg.getint("compass", "size_px", fallback=44)
    compass_gap = cfg.getint("compass", "gap_px", fallback=12)
    compass_label_gap = cfg.getint("compass", "label_gap_px", fallback=6)
    compass_label_w = cfg.getint("compass", "label_width_px", fallback=100)
    compass_font_size = cfg.getint("compass", "font_size", fallback=20)
    compass_font = _load_font(compass_font_size, bold=True) if compass_on else None
    compass_x = cx + zone_w // 2 + compass_gap
    compass_label_x = compass_x + compass_size + compass_label_gap
    if compass_on and compass_label_x + compass_label_w > video_width_px:
        print("WARNING: [compass] slot doesn't fit at this video width "
              "(text_zone_width_px/gap_px/size_px/label_width_px too "
              "large); will be clipped off-frame", file=sys.stderr)
    compass_needle_color = ImageColor.getrgb(
        cfg.get("compass", "needle_color", fallback="#d32121")) + (255,)
    compass_tail_color = ImageColor.getrgb(
        cfg.get("compass", "tail_color", fallback="#e0e0e0")) + (255,)
    compass_glow_on = compass_on and cfg.getboolean("compass", "glow_enabled", fallback=False)
    compass_glow_pad = 0
    if compass_glow_on:
        compass_glow_radius = cfg.getfloat("compass", "glow_radius_px", fallback=6.0)
        compass_glow_alpha = cfg.getint("compass", "glow_alpha", fallback=170)
        compass_glow_color = ImageColor.getrgb(
            cfg.get("compass", "glow_color", fallback="#000000"))
        compass_glow_pad = max(1, round(compass_glow_radius * 2.5))

    dim_fill = (255, 255, 255, 130)
    bright_fill = (255, 255, 255, 255)
    stroke_fill = (0, 0, 0, 255)

    out_dir.mkdir(parents=True, exist_ok=True)
    for i, (segments, (rid, cardinal), alpha) in enumerate(zip(texts, matches, alphas)):
        img = Image.new("RGBA", (video_width_px, strip_h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        plain = "".join(seg_text for seg_text, _ in segments)
        tb = draw.textbbox((0, 0), plain, font=font)
        tw, th = tb[2] - tb[0], tb[3] - tb[1]
        x = cx - tw / 2 - tb[0]
        y = strip_h / 2 - th / 2 - tb[1]
        for seg_text, is_dim in segments:
            draw.text((x, y), seg_text, font=font,
                      fill=dim_fill if is_dim else bright_fill,
                      stroke_width=2, stroke_fill=stroke_fill)
            x += draw.textlength(seg_text, font=font)
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
                    if glow_on and rid in glows:
                        glow_img, pad = glows[rid]
                        faded_glow = _faded(glow_img, alpha)
                        if faded_glow is not None:
                            gx, gy = shield_x - pad, sy - pad
                            # Clip to the strip's actual canvas instead of
                            # growing it - strip_h is fixed by the "shield
                            # fading in/out must never shift the text" rule,
                            # so a glow radius large enough to want more
                            # vertical room than the strip's existing top/
                            # bottom margin just gets cut off there rather
                            # than moving anything.
                            crop_l = max(0, -gx)
                            crop_t = max(0, -gy)
                            crop_r = min(faded_glow.width, video_width_px - gx)
                            crop_b = min(faded_glow.height, strip_h - gy)
                            if crop_r > crop_l and crop_b > crop_t:
                                visible = faded_glow.crop((crop_l, crop_t, crop_r, crop_b))
                                img.alpha_composite(visible, (gx + crop_l, gy + crop_t))
                    if 0 <= shield_x and shield_x + shield.width <= video_width_px:
                        img.alpha_composite(shield, (shield_x, sy))
            # Route/street label: drawn for BOTH highway and local-road
            # matches, always right-aligned to the same fixed shield_x, so
            # it never shifts whether or not a shield is actually present.
            # extras (concurrent designations) are only ever populated for
            # a highway match - see concurrent_designations_per_second().
            label = route_label(rid, cardinal, extras[i] if extras else None)
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
        if compass_on:
            reading = compass[i]
            if reading is not None:
                heading_deg, point8 = reading
                if 0 <= compass_x and compass_x + compass_size <= video_width_px:
                    rose = render_compass_rose(heading_deg, compass_size,
                                               needle_color=compass_needle_color,
                                               tail_color=compass_tail_color)
                    rose_y = round(strip_h / 2 - compass_size / 2)
                    if compass_glow_on:
                        glow = _gaussian_glow(rose, compass_glow_pad,
                                              compass_glow_radius, compass_glow_alpha,
                                              compass_glow_color)
                        gx, gy = compass_x - compass_glow_pad, rose_y - compass_glow_pad
                        # Same strip-bounds clipping as the shield's glow -
                        # see its comment above.
                        crop_l = max(0, -gx)
                        crop_t = max(0, -gy)
                        crop_r = min(glow.width, video_width_px - gx)
                        crop_b = min(glow.height, strip_h - gy)
                        if crop_r > crop_l and crop_b > crop_t:
                            visible = glow.crop((crop_l, crop_t, crop_r, crop_b))
                            img.alpha_composite(visible, (gx + crop_l, gy + crop_t))
                    img.alpha_composite(rose, (compass_x, rose_y))
                clabel = f"{point8} {heading_deg:03.0f}°"
                ctb = draw.textbbox((0, 0), clabel, font=compass_font)
                clw, clh = ctb[2] - ctb[0], ctb[3] - ctb[1]
                cly = strip_h / 2 - clh / 2 - ctb[1]
                if compass_label_x + clw <= video_width_px:
                    draw.text((compass_label_x - ctb[0], cly), clabel,
                              font=compass_font, fill=(255, 255, 255, 255),
                              stroke_width=2, stroke_fill=(0, 0, 0, 255))
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
    compass_on = cfg.getboolean("compass", "enabled", fallback=False)
    # Round 2/3: the info-strip PNG overlay replaces the ASS "Info" style
    # entirely when either [roads] (highway shields) or [local_roads]
    # (street-name fallback) is enabled; otherwise info stays in the ASS
    # track exactly as before (backward compatible with Round 1 configs).
    info_via_strip = info_on and (roads_on or local_roads_on)
    info_via_ass = info_on and not (roads_on or local_roads_on)

    # Day-title cards ("Day 2 - Bristol, VA to Texarkana, TX"): optional,
    # off by default (`[day_title] enabled`). Cheap pure-function work (no
    # PNG frames), so computed here regardless of --skip-map/map_day being
    # enabled - it only needs the local-day segmentation, not the rendered
    # day-map insets themselves.
    day_title_on = cfg.getboolean("day_title", "enabled", fallback=False)
    day_titles = None
    if day_title_on:
        title_dates = dates_per_second(track_rows, total_secs, cfg)
        title_day_segs = day_segments(title_dates)
        day_titles = day_title_segments(
            title_day_segs, spans,
            display_secs=cfg.getfloat("day_title", "display_secs", fallback=2.0),
            min_duration_secs=cfg.getfloat("day_title", "min_duration_secs", fallback=2.0))
        skipped = len(title_day_segs) - len(day_titles)
        print(f"  [day_title] {len(day_titles)} title card(s) over "
              f"{len(title_day_segs)} local-day segment(s)"
              + (f", {skipped} skipped (too short or no GPS label)" if skipped else ""))

    if labels_on:
        info_spans = (split_spans_for_gaps(
                          build_info_spans(track_rows, cfg, end_time=total_secs),
                          dark_spans, no_gps_text)
                     if info_via_ass else None)
        (work / "labels.ass").write_text(
            build_ass(spans, cfg, info_spans, day_titles), encoding="utf-8")
        print(f"Wrote {work / 'labels.ass'} ({len(spans)} town events, "
              f"{len(info_spans or [])} info events, "
              f"{len(day_titles or [])} day-title events)")

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
            day_height_cfg = cfg.getint("map_day", "height", fallback=0)
            shared_h = day_height_cfg if day_height_cfg > 0 else _canvas_height_px(
                positions, day_width)
            day_fade_secs = cfg.getint("map_day", "fade_secs", fallback=2)
            for seg_i, (seg_start, seg_end) in enumerate(segments):
                seg_alphas = day_segment_fade_alpha(
                    seg_end - seg_start, day_fade_secs,
                    is_first_segment=(seg_i == 0),
                    is_last_segment=(seg_i == len(segments) - 1))
                w, h = render_map_frames(
                    positions[seg_start:seg_end], cfg, work / "map_day",
                    section="map_day", frame_offset=seg_start, height_px=shared_h,
                    alphas=seg_alphas)
            print(f"  day inset size: {w}x{h}")
        if info_via_strip:
            headings = headings_per_second(track_rows, total_secs)
            # 2026-07-14: freeze-while-stopped for the road match + NB/EB/
            # SB/WB suffix (see roads_per_second()) - a stopped vehicle's
            # GPS heading is pure noise, and sitting still near a real
            # junction can drift out of tolerance of the "correct" road
            # just from being parked. Shared by both [roads] and
            # [local_roads] since it's the same vehicle/speed either way.
            speeds = speeds_per_second(track_rows, total_secs)
            roads_freeze_mph = cfg.getfloat("roads", "freeze_below_mph", fallback=3.0)
            shields: dict = {}
            highway_matches: list[tuple[str | None, str | None]] = [(None, None)] * total_secs
            extras: list[list[str]] = [[] for _ in range(total_secs)]
            if roads_on:
                roads_file = cfg.get("roads", "roads_file",
                                     fallback="map_data/synthetic_roads_test.geojson")
                roads_tolerance_mi = cfg.getfloat("roads", "tolerance_mi", fallback=0.031)
                roads_grace_secs = cfg.getint("roads", "grace_secs", fallback=3)
                roads = load_roads(Path(roads_file))
                if not roads:
                    print(f"WARNING: [roads] enabled but no roads loaded from "
                          f"{roads_file}; shields will never show", file=sys.stderr)
                shield_h = cfg.getint("roads", "shield_height_px", fallback=52)
                shields = shield_cache_for(roads, shield_h)
                # Built unconditionally (not just on a match-cache miss,
                # like roads_by_id below) - concurrent_designations_per_
                # second() needs it every render, since concurrent-
                # designation lookups aren't part of the cached primary-
                # match data at all (see below). Cheap for the highway file
                # specifically (~1s for ~6K segments per the 2026-07-15
                # perf-fix benchmarks) even on an otherwise-cached render.
                road_index = build_road_segment_index(roads)
                cache_path = work / "_cache_roads_matches.json"
                cache_key = (_road_match_cache_key(
                                work / "track.csv", Path(roads_file),
                                roads_tolerance_mi, roads_grace_secs, total_secs,
                                roads_freeze_mph)
                            if Path(roads_file).exists() else None)
                cached = _load_cached_matches(cache_path, cache_key) if cache_key else None
                if cached is not None:
                    print(f"  [roads] using cached match results "
                          f"({cache_path.name}) - track/road data/parameters "
                          f"unchanged since last render")
                    highway_matches = cached
                else:
                    points_cache_path = work / "_cache_roads_points.json"
                    distances_cache_path = work / "_cache_roads_distances.json"
                    roads_hash = (_file_content_hash(Path(roads_file))
                                 if Path(roads_file).exists() else "")
                    point_cache = _load_point_cache(points_cache_path, roads_hash,
                                                    roads_tolerance_mi)
                    distance_cache = _load_distance_cache(
                        distances_cache_path, roads_hash, roads_tolerance_mi)
                    # 2026-07-15: route-id grouping - see the module note
                    # above nearest_road_indexed() - is the real fix for
                    # the slow current-road-check scan; a pure function of
                    # `roads`, so built once here rather than per-second
                    # inside roads_per_second(). (road_index itself is now
                    # built unconditionally above, not just here - see that
                    # comment.)
                    roads_by_id = group_roads_by_id(roads)
                    highway_matches = roads_per_second(
                        positions, headings, roads,
                        tolerance_mi=roads_tolerance_mi, grace_secs=roads_grace_secs,
                        point_cache=point_cache, speeds=speeds,
                        freeze_below_mph=roads_freeze_mph,
                        road_index=road_index, roads_by_id=roads_by_id,
                        distance_cache=distance_cache, progress_label="roads")
                    _save_point_cache(points_cache_path, roads_hash,
                                      roads_tolerance_mi, point_cache)
                    _save_distance_cache(distances_cache_path, roads_hash,
                                        roads_tolerance_mi, distance_cache)
                    if cache_key:
                        _save_cached_matches(cache_path, cache_key, highway_matches)

                # Concurrent designations ("I-10 / US 70 WB"): a separate,
                # stateless lookup on top of the already-hysteresis-
                # stabilized highway_matches above - see
                # concurrent_designations_per_second()'s docstring for why
                # this deliberately doesn't touch roads_per_second()'s own
                # state machine. Not part of the cached match data (cheap
                # enough to just recompute every render - see the
                # road_index comment above), and only ever evaluated
                # against the highway match, never the local-road fallback.
                if cfg.getboolean("roads", "show_concurrent_designations",
                                  fallback=False):
                    max_extra = cfg.getint("roads", "max_concurrent_designations",
                                           fallback=1)
                    extras = concurrent_designations_per_second(
                        positions, highway_matches, road_index, roads_tolerance_mi,
                        speeds=speeds, freeze_below_mph=roads_freeze_mph,
                        max_extra=max_extra)

            # Round 3: local (non-highway) street names, own roads_file /
            # tolerance_mi / grace_secs (denser network, tighter defaults -
            # see [local_roads] in config.ini). merge_road_matches() keeps
            # the highway match whenever there is one; local only fills in
            # the seconds [roads] left unmatched.
            local_matches: list[tuple[str | None, str | None]] = [(None, None)] * total_secs
            if local_roads_on:
                local_file = cfg.get("local_roads", "roads_file",
                                     fallback="map_data/local_roads.geojson")
                local_tolerance_mi = cfg.getfloat("local_roads", "tolerance_mi", fallback=0.02)
                local_grace_secs = cfg.getint("local_roads", "grace_secs", fallback=2)
                local_cache_path = work / "_cache_local_roads_matches.json"
                local_cache_key = (_road_match_cache_key(
                                       work / "track.csv", Path(local_file),
                                       local_tolerance_mi, local_grace_secs, total_secs,
                                       roads_freeze_mph)
                                   if Path(local_file).exists() else None)
                local_cached = (_load_cached_matches(local_cache_path, local_cache_key)
                                if local_cache_key else None)
                if local_cached is not None:
                    # Cache hit means we never need local_roads_list at all -
                    # unlike the highway pass, it isn't needed for shields,
                    # so this skips both the 200MB+ GeoJSON parse AND the
                    # matching loop entirely.
                    print(f"  [local_roads] using cached match results "
                          f"({local_cache_path.name}) - track/road data/"
                          f"parameters unchanged since last render")
                    local_matches = local_cached
                else:
                    local_roads_list = load_roads(Path(local_file))
                    if not local_roads_list:
                        print(f"WARNING: [local_roads] enabled but no roads "
                              f"loaded from {local_file}; local road names "
                              f"will never show", file=sys.stderr)
                    local_points_cache_path = work / "_cache_local_roads_points.json"
                    local_distances_cache_path = work / "_cache_local_roads_distances.json"
                    local_roads_hash = (_file_content_hash(Path(local_file))
                                       if Path(local_file).exists() else "")
                    local_point_cache = _load_point_cache(
                        local_points_cache_path, local_roads_hash, local_tolerance_mi)
                    local_distance_cache = _load_distance_cache(
                        local_distances_cache_path, local_roads_hash, local_tolerance_mi)
                    # Same grid-index + route-grouping fix as [roads] above -
                    # this is the layer where it matters most (306K real
                    # local-road segments vs. 6K highway segments).
                    local_road_index = build_road_segment_index(local_roads_list)
                    local_roads_by_id = group_roads_by_id(local_roads_list)
                    local_matches = roads_per_second(
                        positions, headings, local_roads_list,
                        tolerance_mi=local_tolerance_mi, grace_secs=local_grace_secs,
                        point_cache=local_point_cache, speeds=speeds,
                        freeze_below_mph=roads_freeze_mph,
                        road_index=local_road_index, roads_by_id=local_roads_by_id,
                        distance_cache=local_distance_cache,
                        progress_label="local_roads")
                    _save_point_cache(local_points_cache_path, local_roads_hash,
                                      local_tolerance_mi, local_point_cache)
                    _save_distance_cache(local_distances_cache_path, local_roads_hash,
                                        local_tolerance_mi, local_distance_cache)
                    if local_cache_key:
                        _save_cached_matches(local_cache_path, local_cache_key,
                                            local_matches)

            matches = merge_road_matches(highway_matches, local_matches)
            texts = info_text_per_second(track_rows, cfg, total_secs)

            # Round 4: force both text and match to the no-GPS state on any
            # dark second, AFTER the highway/local merge - a dark clip has
            # no track.csv points at all, so positions/matches would
            # otherwise just keep showing whatever was last known.
            dark_mask = no_gps_seconds(dark_spans, total_secs)
            texts = [[(no_gps_text, False)] if dark_mask[i] else t
                    for i, t in enumerate(texts)]
            matches = [(None, None) if dark_mask[i] else m
                      for i, m in enumerate(matches)]
            extras = [[] if dark_mask[i] else e for i, e in enumerate(extras)]

            # Compass indicator: mirrors the shield/label pair on the
            # right of the text zone. Uses the same `headings` already
            # computed above for roads_per_second() - real device-reported
            # GPS course, smoothed + freeze-while-stopped so the needle
            # doesn't jump around maniacally (see compass_per_second()),
            # with its own hysteresis on the 8-point cardinal label
            # (cardinal8_per_second()) so it doesn't flicker at octant
            # boundaries either. Forced to None (not drawn) on GPS-dark
            # seconds, same as the road match above.
            compass_data: list[tuple[float, str] | None] | None = None
            if compass_on:
                smoothed = compass_per_second(
                    track_rows, total_secs,
                    window_secs=cfg.getint("compass", "smoothing_window_secs", fallback=5),
                    freeze_below_mph=cfg.getfloat("compass", "freeze_below_mph", fallback=3.0))
                cardinals8 = cardinal8_per_second(
                    smoothed,
                    hold_secs=cfg.getint("compass", "hysteresis_hold_secs", fallback=2))
                compass_data = [None if dark_mask[i] else (smoothed[i], cardinals8[i])
                               for i in range(total_secs)]

            highway_secs = sum(1 for rid, _ in matches if rid and rid in shields)
            local_secs = sum(1 for rid, _ in matches if rid and rid not in shields)
            dark_secs = sum(dark_mask)
            print(f"Rendering {total_secs} info-strip frames "
                  f"({highway_secs} sec highway-matched, {local_secs} sec "
                  f"local-road fallback, {dark_secs} sec no-GPS-lock)...")
            iw, ih = render_info_frames(texts, matches, shields, cfg,
                                        work / "info_strip", SOURCE_VIDEO_WIDTH_PX,
                                        compass=compass_data, extras=extras)
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
        ass_path = ffmpeg_filter_path(work / "labels.ass")
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

    # Caps ffmpeg's own encoder thread count via its portable -threads flag,
    # rather than OS-level process affinity/priority (which would need
    # separate Windows/Linux/Mac-specific code). Blank/unset (the default)
    # means ffmpeg picks its own thread count - unchanged from before this
    # option existed. Some users would rather a render take longer and
    # leave the rest of their cores free for other work than have ffmpeg
    # claim every core.
    threads = cfg.get("video", "threads", fallback="").strip()
    if threads:
        if not threads.isdigit() or int(threads) < 1:
            print(f"ERROR: [video] threads must be a positive integer, "
                  f"got {threads!r}", file=sys.stderr)
            return 1
        print(f"Encoding with {threads} thread(s) ([video] threads) - leave "
              f"blank to let ffmpeg use all available cores")
    else:
        print("Encoding with ffmpeg's default thread count (all available cores)")

    cmd = ["ffmpeg", "-y"] + inputs
    cmd += ["-filter_complex", ";".join(filters), "-map", tail,
            "-an", "-c:v", "libx264",
            "-crf", cfg.get("video", "crf", fallback="20"),
            "-preset", cfg.get("video", "preset", fallback="medium")]
    if threads:
        cmd += ["-threads", threads]
    cmd += ["-pix_fmt", "yuv420p", output]

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
