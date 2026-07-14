"""Logic tests for the pure functions in extract_gps.py, render_overlay.py,
and tools/fetch_tiger_roads.py.

Run from the project root:  python -m unittest discover tests
"""
from __future__ import annotations

import configparser
import json
import struct
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from extract_gps import (GpsPoint, dedupe_labels, find_time_gaps,
                         nmea_to_decimal, parse_freegps, resolve_town_labels,
                         _haversine_miles)
from render_overlay import (ass_time, build_ass, build_info_spans, build_road_segment_index,
                            cardinal8_per_second,
                            compass_per_second, concurrent_designations_per_second,
                            concurrent_road_ids_indexed,
                            current_road_distance_cached,
                            dates_per_second,
                            day_segment_fade_alpha,
                            day_segments, day_title_segments, day_title_text,
                            ffmpeg_filter_path,
                            format_local_date, format_local_time,
                            group_roads_by_id,
                            haversine_miles, heading_to_cardinal, heading_to_compass8,
                            headings_per_second,
                            hex_to_ass_color, info_text_per_second, layout_city_labels,
                            load_gaps, load_roads, local_date_for, merge_road_matches,
                            nearest_road, nearest_road_cached, nearest_road_indexed,
                            no_gps_seconds, overlay_xy,
                            point_to_polyline_miles, positions_per_second,
                            render_compass_rose, render_info_frames, render_shield,
                            roads_per_second,
                            route_label, select_cities, shield_alpha_per_second,
                            shield_glow_cache_for,
                            split_spans_for_gaps, tz_name_for, _average_speeds_mph,
                            _bezier_points,
                            _circular_mean_deg, _file_content_hash,
                            _gaussian_glow,
                            _info_segments_by_point, _letterbox_pad,
                            _load_cached_matches, _load_distance_cache, _load_point_cache,
                            _quantize_point,
                            _road_match_cache_key, _save_cached_matches,
                            _save_distance_cache, _save_point_cache,
                            _shield_outline, _split_leading_zeros)
from tools.fetch_tiger_roads import (build_highway_features, build_local_features,
                                     in_bbox, normalize_highway_id, sample_points,
                                     split_shape_parts, state_bboxes_from_track,
                                     write_geojson)


def make_chunk(hh: int, mi: int, ss: int, yy: int, mo: int, dd: int,
               fix: bytes, ns: bytes, ew: bytes,
               lat: float, lon: float, spd: float, brg: float) -> bytes:
    """Build a synthetic Novatek freeGPS chunk matching the real layout."""
    return (b"freeGPS L\x00\x00\x00" + b"\x00" * 36
            + struct.pack("<6I", hh, mi, ss, yy, mo, dd)
            + fix + ns + ew + b"\x00"
            + struct.pack("<4f", lat, lon, spd, brg)
            + b"\x00" * 64)


class TestNmeaConversion(unittest.TestCase):
    def test_north(self):
        self.assertAlmostEqual(nmea_to_decimal(3612.8003, "N"), 36.213338, places=5)

    def test_west_negative(self):
        self.assertAlmostEqual(nmea_to_decimal(8315.501, "W"), -83.258350, places=5)

    def test_south_negative(self):
        self.assertLess(nmea_to_decimal(1234.5, "S"), 0)

    def test_zero(self):
        self.assertEqual(nmea_to_decimal(0.0, "N"), 0.0)


class TestParseFreegps(unittest.TestCase):
    def test_single_chunk(self):
        data = b"\x00" * 32 + make_chunk(13, 13, 28, 22, 5, 22, b"A", b"N", b"W",
                                         3612.8003, 8315.501, 21.34, 215.9)
        pts = parse_freegps(data, "test.MP4")
        self.assertEqual(len(pts), 1)
        p = pts[0]
        self.assertTrue(p.valid)
        self.assertEqual(p.timestamp_utc, "2022-05-22 13:13:28")
        self.assertAlmostEqual(p.lat, 36.213338, places=5)
        self.assertAlmostEqual(p.lon, -83.258350, places=5)
        self.assertAlmostEqual(p.speed_mph, 24.6, places=1)
        self.assertEqual(p.clip, "test.MP4")
        self.assertEqual(p.sec_in_clip, 0)

    def test_multiple_chunks_indexed_sequentially(self):
        data = b"".join(make_chunk(13, 0, s, 22, 5, 22, b"A", b"N", b"W",
                                   3600.0 + s, 8300.0, 10.0, 0.0) for s in range(3))
        pts = parse_freegps(data)
        self.assertEqual([p.sec_in_clip for p in pts], [0, 1, 2])

    def test_void_fix_flagged_invalid(self):
        data = make_chunk(13, 0, 0, 22, 5, 22, b"V", b"N", b"W",
                          3600.0, 8300.0, 0.0, 0.0)
        pts = parse_freegps(data)
        self.assertEqual(len(pts), 1)
        self.assertFalse(pts[0].valid)

    def test_no_gps_data(self):
        self.assertEqual(parse_freegps(b"\x00" * 1024), [])


class TestDedupeLabels(unittest.TestCase):
    def test_merges_consecutive(self):
        rows = [(0.0, "A, TN"), (1.0, "A, TN"), (2.0, "B, TN"), (3.0, "B, TN")]
        self.assertEqual(dedupe_labels(rows, 4.0),
                         [(0.0, 2.0, "A, TN"), (2.0, 4.0, "B, TN")])

    def test_empty_labels_break_spans(self):
        rows = [(0.0, "A, TN"), (1.0, ""), (2.0, "A, TN")]
        self.assertEqual(dedupe_labels(rows, 3.0),
                         [(0.0, 1.0, "A, TN"), (2.0, 3.0, "A, TN")])

    def test_empty_input(self):
        self.assertEqual(dedupe_labels([], 10.0), [])


class TestAssHelpers(unittest.TestCase):
    def test_ass_time(self):
        self.assertEqual(ass_time(0), "0:00:00.00")
        self.assertEqual(ass_time(3661.5), "1:01:01.50")
        self.assertEqual(ass_time(59.999), "0:01:00.00")

    def test_ass_time_negative_raises(self):
        with self.assertRaises(ValueError):
            ass_time(-1)

    def test_hex_to_ass_color_is_bgr(self):
        self.assertEqual(hex_to_ass_color("#ff3b30"), "&H00303BFF")

    def test_hex_to_ass_color_bad_input(self):
        with self.assertRaises(ValueError):
            hex_to_ass_color("#fff")

    def test_ffmpeg_filter_path_relative_unaffected(self):
        # The shipped config.example.ini default (work_folder = work) has no
        # drive-letter colon, so escaping is a no-op for the common case.
        self.assertEqual(ffmpeg_filter_path(Path("work/labels.ass")),
                         "work/labels.ass")

    def test_ffmpeg_filter_path_escapes_windows_drive_colon(self):
        # An absolute Windows path's drive-letter colon breaks ffmpeg's
        # filtergraph parser (':' is its key=value separator) unless escaped.
        self.assertEqual(
            ffmpeg_filter_path(Path("D:/GitHub/RoadBurner/work/labels.ass")),
            r"D\:/GitHub/RoadBurner/work/labels.ass")

    def test_overlay_xy_corners(self):
        self.assertEqual(overlay_xy("top_left", 20), "20:20")
        self.assertIn("main_w-overlay_w", overlay_xy("top_right", 20))
        self.assertIn("main_h-overlay_h", overlay_xy("bottom_left", 20))


class TestPositionsPerSecond(unittest.TestCase):
    def test_forward_fill(self):
        rows = [
            {"valid": "1", "global_sec": "0", "lat": "36.0", "lon": "-83.0"},
            {"valid": "0", "global_sec": "1", "lat": "0", "lon": "0"},
            {"valid": "1", "global_sec": "2", "lat": "36.1", "lon": "-83.1"},
        ]
        pos = positions_per_second(rows, 4)
        self.assertEqual(pos[0], (36.0, -83.0))
        self.assertEqual(pos[1], (36.0, -83.0))  # invalid fix -> hold last
        self.assertEqual(pos[2], (36.1, -83.1))
        self.assertEqual(pos[3], (36.1, -83.1))  # past end -> hold last

    def test_no_valid_fixes_raises(self):
        with self.assertRaises(ValueError):
            positions_per_second([{"valid": "0", "global_sec": "0",
                                   "lat": "0", "lon": "0"}], 1)


class TestCitySelection(unittest.TestCase):
    # Route: straight line due east along lat 35.0 from -84 to -83 (~57 mi)
    ROUTE = [(35.0, -84.0), (35.0, -83.5), (35.0, -83.0)]

    def test_point_on_route_is_zero(self):
        self.assertAlmostEqual(
            point_to_polyline_miles(35.0, -83.5, self.ROUTE), 0.0, places=3)

    def test_point_one_degree_north(self):
        # 1 deg latitude ~ 69.17 miles
        d = point_to_polyline_miles(36.0, -83.5, self.ROUTE)
        self.assertAlmostEqual(d, 69.17, delta=0.1)

    def test_empty_route_raises(self):
        with self.assertRaises(ValueError):
            point_to_polyline_miles(35.0, -83.5, [])

    def test_select_within_radius(self):
        cities = [
            {"rank": 1, "name": "Near", "state": "TN", "lat": 35.1, "lon": -83.5},
            {"rank": 2, "name": "Far", "state": "TN", "lat": 38.0, "lon": -83.5},
        ]
        shown = select_cities(cities, self.ROUTE, radius_mi=40, min_gap_mi=10)
        self.assertEqual([c["name"] for c in shown], ["Near"])

    def test_min_gap_prefers_higher_rank(self):
        cities = [
            {"rank": 2, "name": "Suburb", "state": "TN", "lat": 35.05, "lon": -83.5},
            {"rank": 1, "name": "Metro", "state": "TN", "lat": 35.0, "lon": -83.45},
        ]
        shown = select_cities(cities, self.ROUTE, radius_mi=40, min_gap_mi=20)
        self.assertEqual([c["name"] for c in shown], ["Metro"])


class TestLabelLayout(unittest.TestCase):
    BOUNDS = (-90.0, -80.0, 30.0, 35.0)  # x0, x1, y0, y1
    SIZE = (480, 240)

    def test_isolated_city_gets_label(self):
        cities = [{"rank": 1, "name": "Solo", "state": "TN", "lat": 32.5, "lon": -85.0}]
        (city, dx, dy), = layout_city_labels(cities, self.BOUNDS, self.SIZE, 12)
        self.assertEqual(city["name"], "Solo")
        self.assertIsNotNone(dx)

    def test_colliding_cities_do_not_overlap(self):
        cities = [
            {"rank": 1, "name": "Bigtown", "state": "TN", "lat": 32.5, "lon": -85.0},
            {"rank": 2, "name": "Smalltown", "state": "TN", "lat": 32.5, "lon": -85.0},
        ]
        results = layout_city_labels(cities, self.BOUNDS, self.SIZE, 12)
        self.assertEqual(results[0][0]["name"], "Bigtown")  # rank order
        self.assertIsNotNone(results[0][1])                 # big city labeled
        if results[1][1] is not None:                       # if both placed,
            self.assertNotEqual((results[0][1], results[0][2]),
                                (results[1][1], results[1][2]))  # different spots

    def test_offscreen_city_gets_dot_only(self):
        cities = [{"rank": 1, "name": "EdgeCase", "state": "TN",
                   "lat": 34.99, "lon": -80.01}]
        (_, dx, _), = layout_city_labels(cities, self.BOUNDS, self.SIZE, 200)
        self.assertIsNone(dx)  # label physically cannot fit


class TestInfoLine(unittest.TestCase):
    def test_haversine_one_degree_lat(self):
        self.assertAlmostEqual(haversine_miles(35.0, -90.0, 36.0, -90.0),
                               69.09, delta=0.2)

    def test_haversine_zero(self):
        self.assertEqual(haversine_miles(35.0, -90.0, 35.0, -90.0), 0.0)

    def test_tz_arizona_no_dst(self):
        self.assertEqual(tz_name_for("AZ", -110.9), "America/Phoenix")

    def test_tz_tennessee_split(self):
        self.assertEqual(tz_name_for("TN", -83.9), "America/New_York")   # Knoxville
        self.assertEqual(tz_name_for("TN", -86.8), "America/Chicago")    # Nashville

    def test_tz_west_texas(self):
        self.assertEqual(tz_name_for("TX", -106.4), "America/Denver")    # El Paso
        self.assertEqual(tz_name_for("TX", -96.8), "America/Chicago")    # Dallas

    def test_format_local_time_cdt(self):
        # May 2022 = DST in Chicago; 14:31 UTC -> 09:31 CDT (24h)
        self.assertEqual(format_local_time("2022-05-23 14:31:00", "America/Chicago"),
                         "09:31 CDT")

    def test_format_local_time_phoenix_mst(self):
        # Arizona never observes DST; also exercises the 24h leading zero
        self.assertEqual(format_local_time("2022-05-27 19:05:00", "America/Phoenix"),
                         "12:05 MST")

    def test_format_local_time_offset_adjust(self):
        self.assertEqual(format_local_time("2022-05-23 14:31:00", None, 1.0),
                         "15:31 UTC")

    def test_format_local_time_midnight_hour_has_leading_zero(self):
        self.assertEqual(format_local_time("2022-05-23 05:03:00", None),
                         "05:03 UTC")


class TestLocalDate(unittest.TestCase):
    def test_arizona_no_dst(self):
        self.assertEqual(local_date_for("2022-05-27 19:05:00", "America/Phoenix"),
                         "2022-05-27")

    def test_crosses_to_previous_local_day(self):
        # 03:00 UTC in Chicago (UTC-5 during CDT) is 22:00 the previous day
        self.assertEqual(local_date_for("2022-05-24 03:00:00", "America/Chicago"),
                         "2022-05-23")

    def test_none_zone_uses_utc_date(self):
        self.assertEqual(local_date_for("2022-05-23 23:30:00", None), "2022-05-23")

    def test_offset_adjust_can_shift_date(self):
        self.assertEqual(
            local_date_for("2022-05-23 23:30:00", None, offset_adjust_h=1.0),
            "2022-05-24")


class TestFormatLocalDate(unittest.TestCase):
    def test_yyyy_mm_dd(self):
        self.assertEqual(format_local_date("2022-05-23 19:05:00", "America/Phoenix"),
                         "2022-05-23")

    def test_crosses_to_previous_local_day(self):
        # 03:00 UTC in Chicago (CDT, UTC-5) is 22:00 the previous local day
        self.assertEqual(format_local_date("2022-05-24 03:00:00", "America/Chicago"),
                         "2022-05-23")

    def test_none_zone_uses_utc_date(self):
        self.assertEqual(format_local_date("2022-01-05 10:00:00", None),
                         "2022-01-05")


class TestDaySegments(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(day_segments([]), [])

    def test_single_day(self):
        self.assertEqual(day_segments(["2022-05-23"] * 5), [(0, 5)])

    def test_clean_multi_day(self):
        dates = ["2022-05-23"] * 3 + ["2022-05-24"] * 4
        self.assertEqual(day_segments(dates), [(0, 3), (3, 7)])

    def test_covers_full_range_contiguously(self):
        dates = ["2022-05-23"] * 2 + ["2022-05-24"] * 3 + ["2022-05-25"] * 1
        segments = day_segments(dates)
        self.assertEqual(segments[0][0], 0)
        self.assertEqual(segments[-1][1], len(dates))
        for (s1, e1), (s2, e2) in zip(segments, segments[1:]):
            self.assertEqual(e1, s2)  # no gaps or overlaps

    def test_midnight_flicker_produces_extra_short_segment(self):
        # A tz-crossing near local midnight can briefly repeat an earlier
        # date; day_segments must not crash and must still cover every index
        # (documented "handle gracefully" behavior, not smoothed away).
        dates = ["2022-05-23", "2022-05-24", "2022-05-23", "2022-05-24"]
        segments = day_segments(dates)
        self.assertEqual(segments, [(0, 1), (1, 2), (2, 3), (3, 4)])
        self.assertEqual(sum(e - s for s, e in segments), len(dates))


class TestDaySegmentFadeAlpha(unittest.TestCase):
    def test_fade_secs_zero_disables_fade(self):
        self.assertEqual(
            day_segment_fade_alpha(5, 0, is_first_segment=True, is_last_segment=False),
            [1.0] * 5)

    def test_first_segment_only_fades_out(self):
        # Last frame of the segment (the boundary edge) hits true 0.0.
        alphas = day_segment_fade_alpha(6, 2, is_first_segment=True, is_last_segment=False)
        self.assertEqual(alphas, [1.0, 1.0, 1.0, 1.0, 0.5, 0.0])

    def test_last_segment_only_fades_in(self):
        # First frame of the segment (the boundary edge) starts at true 0.0.
        alphas = day_segment_fade_alpha(6, 2, is_first_segment=False, is_last_segment=True)
        self.assertEqual(alphas, [0.0, 0.5, 1.0, 1.0, 1.0, 1.0])

    def test_middle_segment_fades_both_ends(self):
        alphas = day_segment_fade_alpha(6, 2, is_first_segment=False, is_last_segment=False)
        self.assertEqual(alphas, [0.0, 0.5, 1.0, 1.0, 0.5, 0.0])

    def test_single_segment_covering_whole_render_never_fades(self):
        # Single segment covering the whole render (no day boundary at all)
        # never fades regardless of fade_secs.
        alphas = day_segment_fade_alpha(6, 2, is_first_segment=True, is_last_segment=True)
        self.assertEqual(alphas, [1.0] * 6)

    def test_short_segment_ramp_clamped_to_half_length(self):
        # fade_secs=5 requested on a 3-frame segment: ramp clamps to 1 frame
        # each side, so only the two edge frames fade (fully, since a
        # 1-frame ramp has no room for a gradual step) and the middle frame
        # stays fully opaque - fade-in and fade-out never overlap.
        alphas = day_segment_fade_alpha(3, 5, is_first_segment=False, is_last_segment=False)
        self.assertEqual(alphas, [0.0, 1.0, 0.0])

    def test_empty_segment_returns_empty(self):
        self.assertEqual(
            day_segment_fade_alpha(0, 2, is_first_segment=True, is_last_segment=True), [])


class TestDayTitleText(unittest.TestCase):
    def test_format(self):
        self.assertEqual(
            day_title_text(2, "Bristol, VA", "Texarkana, TX"),
            "Day 2 - Bristol, VA to Texarkana, TX")


class TestDayTitleSegments(unittest.TestCase):
    def test_normal_two_day_trip(self):
        # Real example from the project's own design discussion.
        day_segs = [(0, 100), (100, 250)]
        spans = [
            (0.0, 50.0, "Alexandria, VA"),
            (50.0, 100.0, "Front Royal, VA"),
            (100.0, 200.0, "Bristol, VA"),
            (200.0, 250.0, "Texarkana, TX"),
        ]
        result = day_title_segments(day_segs, spans)
        self.assertEqual(result, [
            (0.0, 2.0, "Day 1 - Alexandria, VA to Front Royal, VA"),
            (100.0, 102.0, "Day 2 - Bristol, VA to Texarkana, TX"),
        ])

    def test_short_segment_skipped_but_day_number_survives(self):
        # Middle segment is a 1-second midnight-flicker artifact (see
        # day_segments()) - too short for a card, but "Day 3" must still
        # mean the third calendar segment, not "the second card shown".
        day_segs = [(0, 100), (100, 101), (101, 300)]
        spans = [
            (0.0, 100.0, "Town A, VA"),
            (100.0, 101.0, "Town B, VA"),
            (101.0, 300.0, "Town C, TX"),
        ]
        result = day_title_segments(day_segs, spans)
        self.assertEqual(result, [
            (0.0, 2.0, "Day 1 - Town A, VA to Town A, VA"),
            (101.0, 103.0, "Day 3 - Town C, TX to Town C, TX"),
        ])

    def test_fully_dark_segment_skipped(self):
        # No town label at all overlaps the second day segment (e.g. an
        # entirely GPS-dark day) - nothing to build a title from, so it's
        # skipped just like a too-short segment, not shown blank.
        day_segs = [(0, 100), (100, 200)]
        spans = [(0.0, 100.0, "Town A, VA")]
        result = day_title_segments(day_segs, spans)
        self.assertEqual(result, [(0.0, 2.0, "Day 1 - Town A, VA to Town A, VA")])

    def test_card_end_clipped_to_segment_end(self):
        # display_secs (5.0) longer than the whole segment (3.0) - the card
        # must not bleed past the segment's own end.
        day_segs = [(0, 3)]
        spans = [(0.0, 3.0, "Town A, VA")]
        result = day_title_segments(day_segs, spans, display_secs=5.0,
                                    min_duration_secs=2.0)
        self.assertEqual(result, [(0.0, 3.0, "Day 1 - Town A, VA to Town A, VA")])

    def test_empty_day_segs(self):
        self.assertEqual(day_title_segments([], [(0.0, 1.0, "Town A, VA")]), [])


class TestBuildAssDayTitle(unittest.TestCase):
    def test_style_and_dialogue_emitted(self):
        cfg = configparser.ConfigParser()
        day_titles = [(0.0, 2.0, "Day 1 - Alexandria, VA to Bristol, VA")]
        ass = build_ass([], cfg, None, day_titles)
        self.assertIn("Style: DayTitle,", ass)
        self.assertIn(
            "Dialogue: 1,0:00:00.00,0:00:02.00,DayTitle,,0,0,0,,"
            "{\\fad(300,300)}Day 1 - Alexandria, VA to Bristol, VA", ass)

    def test_no_day_titles_means_no_dialogue(self):
        cfg = configparser.ConfigParser()
        ass = build_ass([], cfg, None, None)
        self.assertIn("Style: DayTitle,", ass)  # style is always defined
        self.assertNotIn("DayTitle,,0,0,0,,{\\fad", ass)  # but no events


class TestDatesPerSecond(unittest.TestCase):
    def test_forward_fill_and_tz_conversion(self):
        cfg = configparser.ConfigParser()
        rows = [
            {"valid": "1", "global_sec": "0", "lat": "36.0", "lon": "-83.9",
             "state": "TN", "timestamp_utc": "2022-05-23 12:00:00"},
            {"valid": "1", "global_sec": "1", "lat": "36.0", "lon": "-83.9",
             "state": "TN", "timestamp_utc": "2022-05-24 03:30:00"},
        ]
        dates = dates_per_second(rows, 3, cfg)
        # TN east of the split -> America/New_York; 03:30 UTC in EDT is
        # 23:30 the previous local day, and sec 2 forward-fills from sec 1.
        self.assertEqual(dates, ["2022-05-23", "2022-05-23", "2022-05-23"])

    def test_no_valid_fixes_raises(self):
        cfg = configparser.ConfigParser()
        with self.assertRaises(ValueError):
            dates_per_second([{"valid": "0", "global_sec": "0", "lat": "0",
                               "lon": "0", "state": "", "timestamp_utc": ""}], 1, cfg)


class TestHeadingsPerSecond(unittest.TestCase):
    def test_forward_fill(self):
        rows = [
            {"valid": "1", "global_sec": "0", "heading": "90.0"},
            {"valid": "0", "global_sec": "1", "heading": "999"},
            {"valid": "1", "global_sec": "2", "heading": "180.0"},
        ]
        headings = headings_per_second(rows, 4)
        self.assertEqual(headings, [90.0, 90.0, 180.0, 180.0])

    def test_no_valid_fixes_raises(self):
        with self.assertRaises(ValueError):
            headings_per_second([{"valid": "0", "global_sec": "0",
                                  "heading": "0"}], 1)


class TestHeadingToCardinal(unittest.TestCase):
    def test_cardinal_centers(self):
        self.assertEqual(heading_to_cardinal(0), "N")
        self.assertEqual(heading_to_cardinal(90), "E")
        self.assertEqual(heading_to_cardinal(180), "S")
        self.assertEqual(heading_to_cardinal(270), "W")

    def test_boundaries(self):
        self.assertEqual(heading_to_cardinal(44.9), "N")
        self.assertEqual(heading_to_cardinal(45), "E")
        self.assertEqual(heading_to_cardinal(134.9), "E")
        self.assertEqual(heading_to_cardinal(135), "S")
        self.assertEqual(heading_to_cardinal(224.9), "S")
        self.assertEqual(heading_to_cardinal(225), "W")
        self.assertEqual(heading_to_cardinal(314.9), "W")
        self.assertEqual(heading_to_cardinal(315), "N")

    def test_wraps_past_360(self):
        self.assertEqual(heading_to_cardinal(350), "N")
        self.assertEqual(heading_to_cardinal(370), "N")  # 370 % 360 = 10


class TestHeadingToCompass8(unittest.TestCase):
    def test_octant_centers(self):
        self.assertEqual(heading_to_compass8(0), "N")
        self.assertEqual(heading_to_compass8(45), "NE")
        self.assertEqual(heading_to_compass8(90), "E")
        self.assertEqual(heading_to_compass8(135), "SE")
        self.assertEqual(heading_to_compass8(180), "S")
        self.assertEqual(heading_to_compass8(225), "SW")
        self.assertEqual(heading_to_compass8(270), "W")
        self.assertEqual(heading_to_compass8(315), "NW")

    def test_wraps_past_360(self):
        self.assertEqual(heading_to_compass8(370), "N")  # 370 % 360 = 10

    def test_octant_boundary(self):
        self.assertEqual(heading_to_compass8(22.4), "N")
        self.assertEqual(heading_to_compass8(22.5), "NE")


class TestCircularMeanDeg(unittest.TestCase):
    def test_simple_average(self):
        self.assertAlmostEqual(_circular_mean_deg([10, 20, 30]), 20.0, places=3)

    def test_wraparound_average(self):
        # 350 and 10 should average to 0 (not 180, as a naive arithmetic
        # mean would give - the wraparound at 360/0 breaks plain averaging)
        self.assertAlmostEqual(_circular_mean_deg([350, 10]), 0.0, places=3)

    def test_empty_raises(self):
        with self.assertRaises(ValueError):
            _circular_mean_deg([])


class TestCompassPerSecond(unittest.TestCase):
    def _row(self, sec, heading, speed):
        return {"valid": "1", "global_sec": str(sec), "heading": str(heading),
               "speed_mph": str(speed)}

    def test_forward_fill_and_smoothing(self):
        rows = [self._row(0, 90, 30), self._row(5, 90, 30)]
        out = compass_per_second(rows, total_secs=6, window_secs=1,
                                 freeze_below_mph=0.0)
        self.assertEqual(len(out), 6)
        self.assertAlmostEqual(out[0], 90.0, places=1)
        self.assertAlmostEqual(out[5], 90.0, places=1)

    def test_freeze_below_speed_threshold(self):
        # Moving at 30mph heading 90, then "stopped" (speed 0) with a
        # wildly different noisy heading - the frozen heading must hold
        # the last real value, not jump to the stopped-noise reading.
        rows = [self._row(0, 90, 30), self._row(1, 200, 0)]
        out = compass_per_second(rows, total_secs=2, window_secs=1,
                                 freeze_below_mph=3.0)
        self.assertAlmostEqual(out[1], out[0], places=1)

    def test_empty_rows_raises(self):
        with self.assertRaises(ValueError):
            compass_per_second([], total_secs=3)


class TestCardinal8PerSecond(unittest.TestCase):
    def test_stable_heading_no_flicker(self):
        headings = [225.0] * 5
        self.assertEqual(cardinal8_per_second(headings), ["SW"] * 5)

    def test_brief_boundary_noise_does_not_switch(self):
        # Dips into S territory for a single second, then back to SW -
        # hysteresis (hold_secs=2) should hold "SW" throughout.
        headings = [225.0, 225.0, 179.0, 225.0, 225.0]
        result = cardinal8_per_second(headings, hold_secs=2)
        self.assertTrue(all(c == "SW" for c in result))

    def test_sustained_change_does_switch(self):
        headings = [225.0] * 3 + [180.0] * 5
        result = cardinal8_per_second(headings, hold_secs=2)
        self.assertEqual(result[-1], "S")

    def test_empty_returns_empty(self):
        self.assertEqual(cardinal8_per_second([]), [])

    def test_continuous_multi_octant_sweep_does_not_stall(self):
        # Regression for the 2026-07-14 bug: a real turn sweeps the
        # smoothed heading through several different octants in a row
        # (E -> SE -> S -> SW), each one different from the last. The
        # buggy version required one specific alternate candidate to
        # repeat hold_secs+1 times before switching, so a sweep like this
        # reset its counter every second and never fired - "E" would have
        # stuck around for the whole tail below instead of updating to
        # "SW" as soon as heading had been away from "E" for hold_secs+1
        # consecutive seconds.
        headings = [90.0, 135.0, 180.0, 225.0, 225.0, 225.0]
        result = cardinal8_per_second(headings, hold_secs=2)
        self.assertEqual(result, ["E", "E", "E", "SW", "SW", "SW"])

    def test_boundary_flicker_still_absorbed_after_fix(self):
        # Same scenario as test_brief_boundary_noise_does_not_switch, just
        # confirming the rewritten mismatch-streak logic didn't regress
        # the original boundary-flicker-absorption behavior.
        headings = [225.0, 179.0, 225.0, 179.0, 225.0]
        result = cardinal8_per_second(headings, hold_secs=2)
        self.assertTrue(all(c == "SW" for c in result))


class TestNearestRoad(unittest.TestCase):
    ROADS = [{"route_id": "I-30", "route_type": "interstate",
             "geometry": [(35.0, -90.0), (35.0, -89.5), (35.0, -89.0)]}]

    def test_point_on_road_matches(self):
        route_id, dist = nearest_road(35.0, -89.7, self.ROADS, tolerance_mi=0.05)
        self.assertEqual(route_id, "I-30")
        self.assertAlmostEqual(dist, 0.0, places=3)

    def test_point_far_away_no_match(self):
        route_id, dist = nearest_road(36.0, -89.7, self.ROADS, tolerance_mi=0.05)
        self.assertIsNone(route_id)
        self.assertGreater(dist, 0.05)

    def test_empty_roads_no_match(self):
        route_id, dist = nearest_road(35.0, -89.7, [], tolerance_mi=0.05)
        self.assertIsNone(route_id)
        self.assertEqual(dist, float("inf"))

    def test_concurrent_interstate_wins_over_closer_us_route(self):
        # Real-world case: I-10 and US 70 run concurrently near Deming, NM
        # - TIGER digitizes each route number as its own near-duplicate
        # line, so the US route can be a hair *closer* than the Interstate
        # at a given point. The Interstate should still win.
        roads = [
            {"route_id": "US 70", "route_type": "us_route",
             "geometry": [(35.0, -90.0), (35.0, -89.5), (35.0, -89.0)]},
            {"route_id": "I-10", "route_type": "interstate",
             "geometry": [(35.0001, -90.0), (35.0001, -89.5), (35.0001, -89.0)]},
        ]
        route_id, dist = nearest_road(35.0, -89.7, roads, tolerance_mi=0.05)
        self.assertEqual(route_id, "I-10")

    def test_non_interstate_wins_when_interstate_out_of_range(self):
        roads = [
            {"route_id": "US 70", "route_type": "us_route",
             "geometry": [(35.0, -90.0), (35.0, -89.5), (35.0, -89.0)]},
            {"route_id": "I-10", "route_type": "interstate",
             "geometry": [(40.0, -90.0), (40.0, -89.5), (40.0, -89.0)]},
        ]
        route_id, dist = nearest_road(35.0, -89.7, roads, tolerance_mi=0.05)
        self.assertEqual(route_id, "US 70")

    def test_unknown_route_type_sorts_below_us_route(self):
        roads = [
            {"route_id": "Some State Rd", "route_type": "state_route",
             "geometry": [(35.0, -90.0), (35.0, -89.5), (35.0, -89.0)]},
            {"route_id": "US 70", "route_type": "us_route",
             "geometry": [(35.0001, -90.0), (35.0001, -89.5), (35.0001, -89.0)]},
        ]
        route_id, dist = nearest_road(35.0, -89.7, roads, tolerance_mi=0.05)
        self.assertEqual(route_id, "US 70")


class TestRoadsPerSecond(unittest.TestCase):
    ROADS = [{"route_id": "I-30", "route_type": "interstate",
             "geometry": [(35.0, -90.0), (35.0, -89.0)]}]

    def test_hysteresis_survives_brief_gap_but_drops_long_gap(self):
        # on, on, brief-off(1), on, off(1), off(2), off(3)->drop, on->reacquire
        positions = [
            (35.0, -89.9), (35.0, -89.8), (35.01, -89.7), (35.0, -89.6),
            (35.01, -89.5), (35.01, -89.4), (35.01, -89.3), (35.0, -89.2),
        ]
        headings = [90.0] * 8
        result = roads_per_second(positions, headings, self.ROADS,
                                  tolerance_mi=0.05, grace_secs=2)
        expected = [("I-30", "E")] * 6 + [(None, None), ("I-30", "E")]
        self.assertEqual(result, expected)

    def test_never_in_range_stays_unmatched(self):
        positions = [(40.0, -89.7)] * 3
        headings = [0.0] * 3
        result = roads_per_second(positions, headings, self.ROADS,
                                  tolerance_mi=0.05, grace_secs=2)
        self.assertEqual(result, [(None, None)] * 3)

    def test_mismatched_lengths_raise(self):
        with self.assertRaises(ValueError):
            roads_per_second([(35.0, -89.7)], [0.0, 90.0], self.ROADS)

    def test_freeze_while_stopped_preserves_last_match_and_cardinal(self):
        # 2026-07-14 regression: stopped near (but drifted off) the road,
        # with noisy heading and a position drift that would otherwise
        # exhaust grace_secs and drop the match entirely - the real Van
        # Horn, TX case that motivated this fix. freeze_below_mph should
        # hold the last known-good (route, cardinal) for every second
        # below the threshold, then resume normal evaluation once moving
        # again.
        positions = [
            (35.0, -89.9), (35.0, -89.8),        # moving, matched to I-30
            (35.05, -89.75), (35.05, -89.75),    # stopped, drifted off-tolerance
            (35.05, -89.75), (35.05, -89.75),
            (35.0, -89.6),                       # moving again, back on I-30
        ]
        headings = [90.0, 90.0, 12.0, 200.0, 300.0, 45.0, 90.0]
        speeds = [30.0, 25.0, 1.0, 0.0, 0.5, 2.0, 30.0]
        result = roads_per_second(positions, headings, self.ROADS,
                                  tolerance_mi=0.05, grace_secs=2,
                                  speeds=speeds, freeze_below_mph=3.0)
        last_moving = result[1]
        self.assertEqual(result[1], ("I-30", "E"))
        self.assertEqual(result[2:6], [last_moving] * 4)
        self.assertEqual(result[6], ("I-30", "E"))

    def test_no_speeds_preserves_old_behavior(self):
        # speeds=None (the default) must behave exactly like before this
        # fix existed - old callers, cached-match compatibility, etc.
        positions = [(35.0, -89.9), (35.0, -89.8)]
        headings = [90.0, 90.0]
        result = roads_per_second(positions, headings, self.ROADS, tolerance_mi=0.05)
        self.assertEqual(result, [("I-30", "E"), ("I-30", "E")])

    def test_mismatched_speeds_length_raises(self):
        with self.assertRaises(ValueError):
            roads_per_second([(35.0, -89.9)], [90.0], self.ROADS,
                             speeds=[1.0, 2.0])


class TestMergeRoadMatches(unittest.TestCase):
    def test_prefers_primary_when_matched(self):
        primary = [("I-30", "W"), ("I-30", "W"), (None, None)]
        secondary = [("Maple Street", "N"), (None, None), ("Oak Ave", "S")]
        self.assertEqual(merge_road_matches(primary, secondary),
                         [("I-30", "W"), ("I-30", "W"), ("Oak Ave", "S")])

    def test_both_unmatched_stays_unmatched(self):
        self.assertEqual(merge_road_matches([(None, None)], [(None, None)]),
                         [(None, None)])

    def test_secondary_only_fills_gaps_primary_left(self):
        # Primary drops out for the middle second only - secondary should
        # cover exactly that second, not bleed into the ones on either side.
        primary = [(None, None), ("I-30", "E"), (None, None)]
        secondary = [("Maple Street", "E")] * 3
        self.assertEqual(merge_road_matches(primary, secondary),
                         [("Maple Street", "E"), ("I-30", "E"), ("Maple Street", "E")])

    def test_mismatched_lengths_raise(self):
        with self.assertRaises(ValueError):
            merge_road_matches([(None, None)], [(None, None), (None, None)])


class TestRoadMatchCache(unittest.TestCase):
    def test_file_content_hash_deterministic_and_sensitive(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "a.txt"
            p.write_text("hello")
            h1 = _file_content_hash(p)
            h2 = _file_content_hash(p)
            self.assertEqual(h1, h2)
            p.write_text("hello!")
            self.assertNotEqual(h1, _file_content_hash(p))

    def test_cache_key_changes_with_any_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            track = Path(tmp) / "track.csv"
            roads = Path(tmp) / "roads.geojson"
            track.write_text("a")
            roads.write_text("b")
            base = _road_match_cache_key(track, roads, 0.02, 2, 100)
            self.assertEqual(base, _road_match_cache_key(track, roads, 0.02, 2, 100))

            track.write_text("a-changed")
            self.assertNotEqual(base, _road_match_cache_key(track, roads, 0.02, 2, 100))
            track.write_text("a")  # restore track content

            self.assertNotEqual(base, _road_match_cache_key(track, roads, 0.03, 2, 100))
            self.assertNotEqual(base, _road_match_cache_key(track, roads, 0.02, 3, 100))
            self.assertNotEqual(base, _road_match_cache_key(track, roads, 0.02, 2, 101))
            # 2026-07-14: freeze_below_mph is also part of the key, so a
            # pre-existing cache from before this parameter existed (or
            # with a different value) naturally misses and recomputes.
            self.assertNotEqual(base, _road_match_cache_key(track, roads, 0.02, 2, 100, 5.0))

    def test_save_then_load_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "cache.json"
            matches = [("I-30", "W"), (None, None), ("US 82", "N")]
            _save_cached_matches(cache_path, "key123", matches)
            loaded = _load_cached_matches(cache_path, "key123")
            self.assertEqual(loaded, [tuple(m) for m in matches])

    def test_load_returns_none_on_key_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "cache.json"
            _save_cached_matches(cache_path, "old-key", [("I-30", "W")])
            self.assertIsNone(_load_cached_matches(cache_path, "new-key"))

    def test_load_returns_none_when_file_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(_load_cached_matches(Path(tmp) / "nope.json", "any"))

    def test_load_returns_none_on_corrupt_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "cache.json"
            cache_path.write_text("{not valid json")
            self.assertIsNone(_load_cached_matches(cache_path, "any"))


class TestQuantizePoint(unittest.TestCase):
    def test_rounds_to_configured_precision(self):
        self.assertEqual(_quantize_point(35.123456789, -89.987654321),
                         "35.12346,-89.98765")

    def test_nearby_points_share_a_key(self):
        # Two fixes ~0.1m apart (well under GPS noise) should quantize
        # identically.
        self.assertEqual(_quantize_point(35.000001, -89.000001),
                         _quantize_point(35.000002, -89.000002))

    def test_distinct_points_get_distinct_keys(self):
        self.assertNotEqual(_quantize_point(35.0, -89.0),
                            _quantize_point(35.001, -89.0))


class TestNearestRoadCached(unittest.TestCase):
    ROADS = [{"route_id": "I-30", "route_type": "interstate",
             "geometry": [(35.0, -90.0), (35.0, -89.5), (35.0, -89.0)]}]

    def test_matches_uncached_result(self):
        cache: dict = {}
        cached = nearest_road_cached(35.0, -89.7, self.ROADS, 0.05, cache)
        uncached = nearest_road(35.0, -89.7, self.ROADS, 0.05)
        self.assertEqual(cached, uncached)

    def test_populates_cache_on_miss(self):
        cache: dict = {}
        nearest_road_cached(35.0, -89.7, self.ROADS, 0.05, cache)
        self.assertEqual(len(cache), 1)

    def test_hit_reuses_cached_value_even_if_roads_changed(self):
        # Proves the cache - not a fresh nearest_road() call - produced the
        # second result: the roads list is emptied between calls, so an
        # uncached second call would return no match.
        cache: dict = {}
        first = nearest_road_cached(35.0, -89.7, self.ROADS, 0.05, cache)
        second = nearest_road_cached(35.0, -89.7, [], 0.05, cache)
        self.assertEqual(first, second)
        self.assertEqual(first[0], "I-30")

    def test_distinct_points_both_populate_cache(self):
        cache: dict = {}
        nearest_road_cached(35.0, -89.7, self.ROADS, 0.05, cache)
        nearest_road_cached(40.0, -89.7, self.ROADS, 0.05, cache)
        self.assertEqual(len(cache), 2)


class TestRoadsPerSecondPointCache(unittest.TestCase):
    ROADS = [{"route_id": "I-30", "route_type": "interstate",
             "geometry": [(35.0, -90.0), (35.0, -89.0)]}]

    def test_result_identical_with_and_without_cache(self):
        positions = [(35.0, -89.9), (40.0, -89.7), (35.0, -89.5)]
        headings = [90.0] * 3
        without = roads_per_second(positions, headings, self.ROADS,
                                   tolerance_mi=0.05, grace_secs=2)
        with_cache = roads_per_second(positions, headings, self.ROADS,
                                      tolerance_mi=0.05, grace_secs=2,
                                      point_cache={})
        self.assertEqual(without, with_cache)

    def test_cache_reused_across_calls_survives_road_removal(self):
        # Simulates a re-render after a clip is dropped: the same GPS fix
        # recurs, but `roads` is now empty. A shared point_cache should
        # still produce the original match for points it already saw.
        positions = [(35.0, -89.7)]
        headings = [90.0]
        cache: dict = {}
        first = roads_per_second(positions, headings, self.ROADS,
                                 tolerance_mi=0.05, grace_secs=2,
                                 point_cache=cache)
        second = roads_per_second(positions, headings, [],
                                  tolerance_mi=0.05, grace_secs=2,
                                  point_cache=cache)
        self.assertEqual(first, second)


class TestPointCachePersistence(unittest.TestCase):
    def test_save_then_load_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "points.json"
            cache = {"35.00000,-89.00000": ("I-30", 0.01),
                     "36.00000,-89.00000": (None, 1.5)}
            _save_point_cache(cache_path, "hash123", 0.05, cache)
            loaded = _load_point_cache(cache_path, "hash123", 0.05)
            self.assertEqual(loaded, cache)

    def test_load_empty_on_roads_hash_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "points.json"
            _save_point_cache(cache_path, "old-hash", 0.05,
                              {"35.00000,-89.00000": ("I-30", 0.0)})
            self.assertEqual(_load_point_cache(cache_path, "new-hash", 0.05), {})

    def test_load_empty_on_tolerance_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "points.json"
            _save_point_cache(cache_path, "hash123", 0.05,
                              {"35.00000,-89.00000": ("I-30", 0.0)})
            self.assertEqual(_load_point_cache(cache_path, "hash123", 0.02), {})

    def test_load_empty_when_file_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(
                _load_point_cache(Path(tmp) / "nope.json", "any", 0.05), {})

    def test_load_empty_on_corrupt_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "points.json"
            cache_path.write_text("{not valid json")
            self.assertEqual(_load_point_cache(cache_path, "any", 0.05), {})


class TestRoadSegmentIndex(unittest.TestCase):
    # Two roads, plus a distant one, so tests can confirm the index only
    # finds nearby things and doesn't accidentally scan/return everything.
    ROADS = [
        {"route_id": "I-30", "route_type": "interstate",
         "geometry": [(35.0, -90.0), (35.0, -89.5), (35.0, -89.0)]},
        {"route_id": "US 82", "route_type": "us_route",
         "geometry": [(35.05, -90.0), (35.05, -89.0)]},
        {"route_id": "Far Away Rd", "route_type": "state_route",
         "geometry": [(45.0, -90.0), (45.0, -89.0)]},
    ]

    def test_index_matches_brute_force_on_road(self):
        index = build_road_segment_index(self.ROADS)
        indexed = nearest_road_indexed(35.0, -89.7, index, tolerance_mi=0.05)
        brute = nearest_road(35.0, -89.7, self.ROADS, tolerance_mi=0.05)
        self.assertEqual(indexed, brute)

    def test_index_matches_brute_force_out_of_range(self):
        index = build_road_segment_index(self.ROADS)
        route_id, dist = nearest_road_indexed(35.5, -89.7, index, tolerance_mi=0.05)
        self.assertIsNone(route_id)
        self.assertGreater(dist, 0.05)

    def test_index_respects_route_type_priority(self):
        # Same concurrent-route scenario as TestNearestRoad - the indexed
        # path must apply the same tier-then-distance tie-break, not just
        # nearest raw distance.
        roads = [
            {"route_id": "US 70", "route_type": "us_route",
             "geometry": [(35.0, -90.0), (35.0, -89.5), (35.0, -89.0)]},
            {"route_id": "I-10", "route_type": "interstate",
             "geometry": [(35.0001, -90.0), (35.0001, -89.5), (35.0001, -89.0)]},
        ]
        index = build_road_segment_index(roads)
        route_id, _ = nearest_road_indexed(35.0, -89.7, index, tolerance_mi=0.05)
        self.assertEqual(route_id, "I-10")

    def test_empty_roads_gives_inf_distance(self):
        index = build_road_segment_index([])
        route_id, dist = nearest_road_indexed(35.0, -89.7, index, tolerance_mi=0.05)
        self.assertIsNone(route_id)
        self.assertEqual(dist, float("inf"))

    def test_long_segment_found_from_either_end_cell(self):
        # A single segment long enough to span several 1mi grid cells -
        # querying near either endpoint must still find it, proving
        # build_road_segment_index() buckets by bounding box, not just by
        # one representative cell.
        roads = [{"route_id": "Long Rd", "route_type": "interstate",
                  "geometry": [(35.0, -90.0), (35.0, -89.0)]}]  # ~55 mi long
        index = build_road_segment_index(roads, cell_size_mi=1.0)
        near_west, _ = nearest_road_indexed(35.0, -89.99, index, tolerance_mi=0.05)
        near_east, _ = nearest_road_indexed(35.0, -89.01, index, tolerance_mi=0.05)
        self.assertEqual(near_west, "Long Rd")
        self.assertEqual(near_east, "Long Rd")

    def test_tolerance_wider_than_cell_size_still_finds_match(self):
        # rings = ceil(tolerance_mi / cell_size_mi) must widen correctly
        # when tolerance exceeds one cell - deliberately small cell_size_mi
        # relative to tolerance_mi so rings > 1 (3, here).
        index = build_road_segment_index(self.ROADS, cell_size_mi=0.02)
        route_id, dist = nearest_road_indexed(35.05, -89.7, index, tolerance_mi=0.06)
        self.assertEqual(route_id, "US 82")
        self.assertAlmostEqual(dist, 0.0, places=3)


class TestGroupRoadsById(unittest.TestCase):
    def test_groups_multiple_pieces_of_same_route(self):
        roads = [
            {"route_id": "I-30", "route_type": "interstate",
             "geometry": [(35.0, -90.0), (35.0, -89.5)]},
            {"route_id": "I-30", "route_type": "interstate",
             "geometry": [(35.0, -89.5), (35.0, -89.0)]},
            {"route_id": "US 82", "route_type": "us_route",
             "geometry": [(35.05, -90.0), (35.05, -89.0)]},
        ]
        grouped = group_roads_by_id(roads)
        self.assertEqual(len(grouped["I-30"]), 2)
        self.assertEqual(len(grouped["US 82"]), 1)

    def test_empty_roads_gives_empty_dict(self):
        self.assertEqual(group_roads_by_id([]), {})


class TestCurrentRoadDistanceCached(unittest.TestCase):
    ROADS = [{"route_id": "I-30", "route_type": "interstate",
             "geometry": [(35.0, -90.0), (35.0, -89.0)]}]

    def test_matches_uncached_distance(self):
        roads_by_id = group_roads_by_id(self.ROADS)
        cache: dict = {}
        cached = current_road_distance_cached(35.0, -89.7, "I-30", roads_by_id, cache)
        uncached = point_to_polyline_miles(35.0, -89.7, self.ROADS[0]["geometry"])
        self.assertAlmostEqual(cached, uncached, places=6)

    def test_populates_cache_on_miss(self):
        roads_by_id = group_roads_by_id(self.ROADS)
        cache: dict = {}
        current_road_distance_cached(35.0, -89.7, "I-30", roads_by_id, cache)
        self.assertEqual(len(cache), 1)

    def test_hit_reuses_cached_value_even_if_roads_changed(self):
        roads_by_id = group_roads_by_id(self.ROADS)
        cache: dict = {}
        first = current_road_distance_cached(35.0, -89.7, "I-30", roads_by_id, cache)
        second = current_road_distance_cached(35.0, -89.7, "I-30", {}, cache)
        self.assertEqual(first, second)

    def test_unknown_route_id_gives_inf(self):
        roads_by_id = group_roads_by_id(self.ROADS)
        cache: dict = {}
        dist = current_road_distance_cached(35.0, -89.7, "Nonexistent", roads_by_id, cache)
        self.assertEqual(dist, float("inf"))


class TestDistanceCachePersistence(unittest.TestCase):
    def test_save_then_load_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "distances.json"
            cache = {"I-30|35.00000,-89.00000": 0.01,
                     "US 82|36.00000,-89.00000": 1.5}
            _save_distance_cache(cache_path, "hash123", 0.05, cache)
            loaded = _load_distance_cache(cache_path, "hash123", 0.05)
            self.assertEqual(loaded, cache)

    def test_load_empty_on_roads_hash_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "distances.json"
            _save_distance_cache(cache_path, "old-hash", 0.05,
                                 {"I-30|35.00000,-89.00000": 0.0})
            self.assertEqual(_load_distance_cache(cache_path, "new-hash", 0.05), {})

    def test_load_empty_on_tolerance_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "distances.json"
            _save_distance_cache(cache_path, "hash123", 0.05,
                                 {"I-30|35.00000,-89.00000": 0.0})
            self.assertEqual(_load_distance_cache(cache_path, "hash123", 0.02), {})

    def test_load_empty_when_file_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(
                _load_distance_cache(Path(tmp) / "nope.json", "any", 0.05), {})

    def test_load_empty_on_corrupt_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "distances.json"
            cache_path.write_text("{not valid json")
            self.assertEqual(_load_distance_cache(cache_path, "any", 0.05), {})


class TestRoadsPerSecondPerfParams(unittest.TestCase):
    # Confirms roads_per_second() produces identical output whether or not
    # the new road_index/roads_by_id/distance_cache params are supplied -
    # they're a faster path to the same answer, not a different one.
    ROADS = [
        {"route_id": "I-30", "route_type": "interstate",
         "geometry": [(35.0, -90.0), (35.0, -89.5), (35.0, -89.0)]},
        {"route_id": "US 82", "route_type": "us_route",
         "geometry": [(35.05, -90.0), (35.05, -89.0)]},
    ]

    def test_road_index_gives_same_result_as_brute_force(self):
        positions = [(35.0, -89.9), (40.0, -89.7), (35.0, -89.5), (35.05, -89.4)]
        headings = [90.0] * 4
        without = roads_per_second(positions, headings, self.ROADS,
                                   tolerance_mi=0.05, grace_secs=2)
        index = build_road_segment_index(self.ROADS)
        with_index = roads_per_second(positions, headings, self.ROADS,
                                      tolerance_mi=0.05, grace_secs=2,
                                      road_index=index)
        self.assertEqual(without, with_index)

    def test_roads_by_id_gives_same_result_as_brute_force(self):
        positions = [(35.0, -89.9), (35.0, -89.8), (35.5, -89.7), (35.0, -89.6)]
        headings = [90.0] * 4
        without = roads_per_second(positions, headings, self.ROADS,
                                   tolerance_mi=0.05, grace_secs=2)
        grouped = group_roads_by_id(self.ROADS)
        with_grouping = roads_per_second(positions, headings, self.ROADS,
                                         tolerance_mi=0.05, grace_secs=2,
                                         roads_by_id=grouped)
        self.assertEqual(without, with_grouping)

    def test_distance_cache_gives_same_result_as_brute_force(self):
        positions = [(35.0, -89.9), (35.0, -89.8), (35.5, -89.7), (35.0, -89.6)]
        headings = [90.0] * 4
        without = roads_per_second(positions, headings, self.ROADS,
                                   tolerance_mi=0.05, grace_secs=2)
        grouped = group_roads_by_id(self.ROADS)
        with_cache = roads_per_second(positions, headings, self.ROADS,
                                      tolerance_mi=0.05, grace_secs=2,
                                      roads_by_id=grouped, distance_cache={})
        self.assertEqual(without, with_cache)

    def test_all_perf_params_together_match_brute_force(self):
        positions = [(35.0, -89.9), (40.0, -89.7), (35.0, -89.5),
                    (35.05, -89.4), (35.0, -89.3)]
        headings = [90.0] * 5
        without = roads_per_second(positions, headings, self.ROADS,
                                   tolerance_mi=0.05, grace_secs=2)
        index = build_road_segment_index(self.ROADS)
        grouped = group_roads_by_id(self.ROADS)
        with_all = roads_per_second(positions, headings, self.ROADS,
                                    tolerance_mi=0.05, grace_secs=2,
                                    point_cache={}, road_index=index,
                                    roads_by_id=grouped, distance_cache={})
        self.assertEqual(without, with_all)

    def test_progress_label_does_not_crash_or_change_result(self):
        positions = [(35.0, -89.9), (35.0, -89.8)]
        headings = [90.0, 90.0]
        without = roads_per_second(positions, headings, self.ROADS,
                                   tolerance_mi=0.05, grace_secs=2)
        with_progress = roads_per_second(positions, headings, self.ROADS,
                                         tolerance_mi=0.05, grace_secs=2,
                                         progress_label="test",
                                         progress_every_secs=0.0)
        self.assertEqual(without, with_progress)


class TestNoGpsSeconds(unittest.TestCase):
    def test_no_spans_all_false(self):
        self.assertEqual(no_gps_seconds([], 4), [False] * 4)

    def test_masks_full_seconds_in_span(self):
        # end=5.0 exactly means second index 5 (time [5,6)) is NOT dark.
        mask = no_gps_seconds([(2.0, 5.0)], 8)
        self.assertEqual(mask, [False, False, True, True, True, False, False, False])

    def test_partial_second_rounds_up_conservatively(self):
        # Any second touched at all by the span counts as dark - safer to
        # over-mask than silently show stale data for a fractional second.
        mask = no_gps_seconds([(2.5, 4.2)], 6)
        self.assertEqual(mask, [False, False, True, True, True, False])

    def test_multiple_spans(self):
        mask = no_gps_seconds([(0.0, 1.0), (4.0, 6.0)], 6)
        self.assertEqual(mask, [True, False, False, False, True, True])

    def test_span_beyond_total_secs_is_clipped(self):
        mask = no_gps_seconds([(2.0, 100.0)], 4)
        self.assertEqual(mask, [False, False, True, True])


class TestSplitSpansForGaps(unittest.TestCase):
    def test_no_dark_spans_returns_unchanged(self):
        spans = [(0.0, 10.0, "A")]
        self.assertEqual(split_spans_for_gaps(spans, [], "X"), spans)

    def test_dark_stretch_in_middle_of_one_span(self):
        spans = [(0.0, 10.0, "A")]
        result = split_spans_for_gaps(spans, [(4.0, 6.0)], "X")
        self.assertEqual(result, [(0.0, 4.0, "A"), (4.0, 6.0, "X"), (6.0, 10.0, "A")])

    def test_fill_text_none_drops_dark_portion(self):
        # Town-label use case: no honest name to show, so just go blank
        # rather than displaying a placeholder string.
        spans = [(0.0, 10.0, "A")]
        result = split_spans_for_gaps(spans, [(4.0, 6.0)], None)
        self.assertEqual(result, [(0.0, 4.0, "A"), (6.0, 10.0, "A")])

    def test_span_entirely_inside_dark_span(self):
        spans = [(4.0, 5.0, "A")]
        result = split_spans_for_gaps(spans, [(0.0, 10.0)], "X")
        self.assertEqual(result, [(4.0, 5.0, "X")])

    def test_dark_span_spanning_a_span_boundary(self):
        spans = [(0.0, 5.0, "A"), (5.0, 10.0, "B")]
        result = split_spans_for_gaps(spans, [(3.0, 7.0)], "X")
        self.assertEqual(result, [(0.0, 3.0, "A"), (3.0, 5.0, "X"),
                                  (5.0, 7.0, "X"), (7.0, 10.0, "B")])

    def test_non_overlapping_dark_span_ignored(self):
        spans = [(0.0, 5.0, "A")]
        result = split_spans_for_gaps(spans, [(10.0, 12.0)], "X")
        self.assertEqual(result, spans)


class TestLoadGaps(unittest.TestCase):
    def test_missing_file_returns_empty(self):
        self.assertEqual(load_gaps(Path("/nonexistent/gaps.csv")), [])

    def test_parses_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "gaps.csv"
            path.write_text("start_sec,end_sec,clip\n12.0,45.5,foo.MP4\n",
                            encoding="utf-8")
            self.assertEqual(load_gaps(path), [(12.0, 45.5, "foo.MP4")])


class TestBezierPoints(unittest.TestCase):
    def test_endpoints_match_anchors(self):
        pts = _bezier_points((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0), n=10)
        self.assertEqual(pts[0], (0.0, 0.0))
        self.assertEqual(pts[-1], (0.0, 1.0))
        self.assertEqual(len(pts), 11)

    def test_stays_within_control_point_bounds(self):
        # cubic bezier curves lie within the convex hull of their control points
        pts = _bezier_points((0.06, 0.14), (0.30, 0.06), (0.70, 0.06), (0.94, 0.14))
        for x, y in pts:
            self.assertTrue(0.06 <= x <= 0.94)
            self.assertTrue(0.06 <= y <= 0.14)


class TestShieldOutline(unittest.TestCase):
    def test_closed_polygon_within_canvas(self):
        outline = _shield_outline(92, 100)
        self.assertGreater(len(outline), 10)
        for x, y in outline:
            self.assertTrue(0 <= x <= 92)
            self.assertTrue(0 <= y <= 100)

    def test_reaches_bottom_point(self):
        outline = _shield_outline(92, 100)
        # bottom point is normalized (0.50, 1.00)
        self.assertIn((46.0, 100.0), outline)


class TestRenderShield(unittest.TestCase):
    def test_interstate_shield_smoke(self):
        img = render_shield("I-30", "interstate", height_px=52)
        self.assertEqual(img.height, 52)
        self.assertEqual(img.mode, "RGBA")

    def test_us_route_shield_smoke(self):
        img = render_shield("US 82", "us_route", height_px=52)
        self.assertEqual(img.height, 52)
        self.assertEqual(img.mode, "RGBA")

    def test_multi_digit_route_number_extraction(self):
        # doesn't crash on longer numbers / different id formats
        for route_id in ("I-635", "US 82", "I-30"):
            img = render_shield(route_id, "interstate", height_px=30)
            self.assertEqual(img.height, 30)


class TestInfoTextPerSecond(unittest.TestCase):
    def test_forward_fill_and_format(self):
        cfg = configparser.ConfigParser()
        # speed_source=raw: this test is about forward-fill/format-string
        # mechanics, not the average-speed feature - the fixture's lat/lon
        # deltas aren't physically realistic for a 2-real-second gap, which
        # would blow up under the default "average" speed_source.
        cfg.read_dict({"info": {"format": "{speed:.0f} mph {dist:.0f} mi",
                                "speed_source": "raw"}})
        rows = [
            {"valid": "1", "global_sec": "0", "lat": "35.0", "lon": "-90.0",
             "state": "", "timestamp_utc": "2022-05-23 12:00:00", "speed_mph": "60"},
            {"valid": "1", "global_sec": "2", "lat": "35.1", "lon": "-90.0",
             "state": "", "timestamp_utc": "2022-05-23 12:00:02", "speed_mph": "65"},
        ]
        texts = info_text_per_second(rows, cfg, 4)
        joined = ["".join(t for t, _ in segs) for segs in texts]
        self.assertEqual(joined[0], "60 mph 0 mi")
        self.assertEqual(joined[1], "60 mph 0 mi")  # forward-filled, no row at sec 1
        self.assertTrue(joined[2].startswith("65 mph"))
        self.assertEqual(joined[3], joined[2])  # forward-filled past the end

    def test_empty_rows_raises(self):
        cfg = configparser.ConfigParser()
        with self.assertRaises(ValueError):
            info_text_per_second([], cfg, 3)


class TestLetterboxPad(unittest.TestCase):
    def test_content_already_matches_target_aspect_no_padding(self):
        extra_lon, extra_lat = _letterbox_pad(
            lon_span=1.0, lat_span=1.0, mean_lat=0.0, width_px=100, height_px=100)
        self.assertAlmostEqual(extra_lon, 0.0, places=6)
        self.assertAlmostEqual(extra_lat, 0.0, places=6)

    def test_content_wider_than_target_pads_latitude(self):
        # Route spans twice as much longitude as latitude, but the target
        # box is square - needs vertical (latitude) margin, not horizontal.
        extra_lon, extra_lat = _letterbox_pad(
            lon_span=2.0, lat_span=1.0, mean_lat=0.0, width_px=100, height_px=100)
        self.assertAlmostEqual(extra_lon, 0.0, places=6)
        self.assertAlmostEqual(extra_lat, 0.5, places=6)

    def test_content_taller_than_target_pads_longitude(self):
        extra_lon, extra_lat = _letterbox_pad(
            lon_span=1.0, lat_span=2.0, mean_lat=0.0, width_px=100, height_px=100)
        self.assertAlmostEqual(extra_lon, 0.5, places=6)
        self.assertAlmostEqual(extra_lat, 0.0, places=6)

    def test_high_latitude_needs_more_longitude_padding(self):
        # At 60 degrees N, cos(60)=0.5, so 1 degree of longitude covers
        # half the ground distance of 1 degree of latitude - an equal
        # lon/lat span needs extra longitude padding to look square.
        extra_lon, extra_lat = _letterbox_pad(
            lon_span=1.0, lat_span=1.0, mean_lat=60.0, width_px=100, height_px=100)
        self.assertAlmostEqual(extra_lon, 0.5, places=6)
        self.assertAlmostEqual(extra_lat, 0.0, places=6)

    def test_non_square_target_box(self):
        # 480x373 (the project's own default) with a perfectly square
        # route: the box is wider than tall, so longitude needs padding.
        extra_lon, extra_lat = _letterbox_pad(
            lon_span=1.0, lat_span=1.0, mean_lat=0.0, width_px=480, height_px=373)
        self.assertGreater(extra_lon, 0.0)
        self.assertAlmostEqual(extra_lat, 0.0, places=6)


class TestSplitLeadingZeros(unittest.TestCase):
    def test_single_leading_zero(self):
        self.assertEqual(_split_leading_zeros("072"), ("0", "72"))

    def test_all_zeros_keeps_last_digit_bright(self):
        self.assertEqual(_split_leading_zeros("0000"), ("000", "0"))

    def test_no_leading_zeros(self):
        self.assertEqual(_split_leading_zeros("999"), ("", "999"))

    def test_single_digit_zero_has_no_dim_part(self):
        self.assertEqual(_split_leading_zeros("0"), ("", "0"))

    def test_negative_sign_preserved_in_dim_part(self):
        self.assertEqual(_split_leading_zeros("-007"), ("-00", "7"))


class TestInfoSegmentsByPoint(unittest.TestCase):
    ROW = [{"valid": "1", "global_sec": "0", "lat": "35.0", "lon": "-90.0",
           "state": "", "timestamp_utc": "2022-05-23 12:00:00", "speed_mph": "7"}]

    def test_zero_padded_speed_splits_dim_and_bright(self):
        cfg = configparser.ConfigParser()
        cfg.read_dict({"info": {"format": "{speed:03.0f} mph"}})
        segs = _info_segments_by_point(self.ROW, cfg)[0][1]
        self.assertIn(("00", True), segs)
        self.assertIn(("7", False), segs)

    def test_non_dim_field_never_split(self):
        cfg = configparser.ConfigParser()
        cfg.read_dict({"info": {"format": "{time}"}})
        segs = _info_segments_by_point(self.ROW, cfg)[0][1]
        self.assertTrue(all(not is_dim for _, is_dim in segs))

    def test_literal_text_preserved_between_fields(self):
        cfg = configparser.ConfigParser()
        cfg.read_dict({"info": {"format": "{speed:03.0f} mph traveled"}})
        segs = _info_segments_by_point(self.ROW, cfg)[0][1]
        joined = "".join(t for t, _ in segs)
        self.assertEqual(joined, "007 mph traveled")

    def test_speed_source_raw_uses_device_reported_value(self):
        cfg = configparser.ConfigParser()
        cfg.read_dict({"info": {"format": "{speed:.0f}", "speed_source": "raw"}})
        row = [{"valid": "1", "global_sec": "0", "lat": "35.0", "lon": "-90.0",
               "state": "", "timestamp_utc": "2022-05-23 12:00:00",
               "speed_mph": "42"}]
        segs = _info_segments_by_point(row, cfg)[0][1]
        self.assertEqual("".join(t for t, _ in segs), "42")

    def test_speed_source_defaults_to_average_not_raw(self):
        cfg = configparser.ConfigParser()
        cfg.read_dict({"info": {"format": "{speed:.0f}"}})
        rows = [
            {"valid": "1", "global_sec": "0", "lat": "35.0", "lon": "-90.0",
             "state": "", "timestamp_utc": "2022-05-23 12:00:00",
             "speed_mph": "55"},
            {"valid": "1", "global_sec": "1", "lat": "35.0001", "lon": "-90.0",
             "state": "", "timestamp_utc": "2022-05-23 12:01:00",
             "speed_mph": "55"},
        ]
        # tiny displacement over 60 real seconds -> average speed should
        # be near 0, nothing like the raw 55 mph both rows report.
        segs = _info_segments_by_point(rows, cfg)[1][1]
        self.assertNotEqual("".join(t for t, _ in segs), "55")


class TestAverageSpeedsMph(unittest.TestCase):
    def test_first_valid_fix_falls_back_to_raw(self):
        rows = [{"valid": "1", "lat": "35.0", "lon": "-90.0",
                "timestamp_utc": "2022-05-23 12:00:00", "speed_mph": "42.0"}]
        self.assertEqual(_average_speeds_mph(rows), [42.0])

    def test_stationary_between_fixes_gives_low_average_despite_raw_spike(self):
        # Mirrors the real trip data: raw device speed glitches high while
        # the vehicle barely moved between two fixes 60 real seconds apart
        # (Apple Mountain Lake, VA - raw 53.5 mph, actual ~4.2 mph).
        rows = [
            {"valid": "1", "lat": "38.906331", "lon": "-78.002401",
             "timestamp_utc": "2022-05-21 18:05:32", "speed_mph": "55.5"},
            {"valid": "1", "lat": "38.907202", "lon": "-78.003068",
             "timestamp_utc": "2022-05-21 18:06:32", "speed_mph": "53.5"},
        ]
        out = _average_speeds_mph(rows)
        self.assertLess(out[1], 10.0)

    def test_invalid_row_uses_raw_and_resets_chain(self):
        rows = [
            {"valid": "1", "lat": "35.0", "lon": "-90.0",
             "timestamp_utc": "2022-05-23 12:00:00", "speed_mph": "40.0"},
            {"valid": "0", "lat": "35.0", "lon": "-90.0",
             "timestamp_utc": "2022-05-23 12:01:00", "speed_mph": "0.0"},
            {"valid": "1", "lat": "35.01", "lon": "-90.0",
             "timestamp_utc": "2022-05-23 12:02:00", "speed_mph": "40.0"},
        ]
        out = _average_speeds_mph(rows)
        self.assertEqual(out[1], 0.0)  # invalid row: raw fallback
        # no usable "previous" fix right after the chain reset - raw again
        self.assertEqual(out[2], 40.0)

    def test_non_positive_elapsed_time_falls_back_to_raw(self):
        rows = [
            {"valid": "1", "lat": "35.0", "lon": "-90.0",
             "timestamp_utc": "2022-05-23 12:00:00", "speed_mph": "40.0"},
            {"valid": "1", "lat": "35.01", "lon": "-90.0",
             "timestamp_utc": "2022-05-23 12:00:00", "speed_mph": "99.0"},
        ]
        out = _average_speeds_mph(rows)
        self.assertEqual(out[1], 99.0)


class TestBuildInfoSpans(unittest.TestCase):
    ROWS = [
        {"valid": "1", "global_sec": "0", "lat": "35.0", "lon": "-90.0",
         "state": "", "timestamp_utc": "2022-05-23 12:00:00", "speed_mph": "60"},
        {"valid": "1", "global_sec": "5", "lat": "35.1", "lon": "-90.0",
         "state": "", "timestamp_utc": "2022-05-23 12:00:05", "speed_mph": "65"},
    ]

    def test_last_span_defaults_to_plus_one_second(self):
        cfg = configparser.ConfigParser()
        spans = build_info_spans(self.ROWS, cfg)
        self.assertEqual(spans[-1][:2], (5.0, 6.0))

    def test_last_span_extends_to_end_time(self):
        # Round 4 fix: a trailing GPS-dark clip can push the true video end
        # well past the old start+1.0 guess - the span must reach it, or
        # split_spans_for_gaps() has nothing to paint the no-GPS-lock text
        # onto for that stretch.
        cfg = configparser.ConfigParser()
        spans = build_info_spans(self.ROWS, cfg, end_time=20.0)
        self.assertEqual(spans[-1][:2], (5.0, 20.0))

    def test_end_time_earlier_than_plus_one_second_is_ignored(self):
        cfg = configparser.ConfigParser()
        spans = build_info_spans(self.ROWS, cfg, end_time=5.2)
        self.assertEqual(spans[-1][:2], (5.0, 6.0))

    def test_non_last_spans_unaffected_by_end_time(self):
        cfg = configparser.ConfigParser()
        spans = build_info_spans(self.ROWS, cfg, end_time=20.0)
        self.assertEqual(spans[0][:2], (0.0, 5.0))


class TestShieldAlphaPerSecond(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(shield_alpha_per_second([]), [])

    def test_instant_with_fade_secs_one(self):
        route_ids = [None, None, "I-30", "I-30", None]
        self.assertEqual(shield_alpha_per_second(route_ids, fade_secs=1),
                         [0.0, 0.0, 1.0, 1.0, 0.0])

    def test_two_second_ramp(self):
        route_ids = ["I-30", "I-30", "I-30", None, None, None]
        result = shield_alpha_per_second(route_ids, fade_secs=2)
        self.assertEqual(result, [0.5, 1.0, 1.0, 0.5, 0.0, 0.0])

    def test_swap_between_two_roads_restarts_ramp(self):
        route_ids = ["I-30", "I-30", "US 82", "US 82"]
        result = shield_alpha_per_second(route_ids, fade_secs=2)
        self.assertEqual(result, [0.5, 1.0, 0.5, 1.0])


class TestRouteLabel(unittest.TestCase):
    def test_builds_wb_style_label(self):
        self.assertEqual(route_label("I-30", "W"), "I-30 WB")
        self.assertEqual(route_label("US 82", "N"), "US 82 NB")

    def test_none_if_unmatched(self):
        self.assertIsNone(route_label(None, "W"))
        self.assertIsNone(route_label("I-30", None))
        self.assertIsNone(route_label(None, None))

    def test_single_extra_id_joined_with_slash(self):
        self.assertEqual(route_label("I-10", "W", ["US 70"]), "I-10 / US 70 WB")

    def test_multiple_extra_ids_joined_in_order(self):
        self.assertEqual(route_label("I-40", "S", ["I-25", "US 66"]),
                         "I-40 / I-25 / US 66 SB")

    def test_empty_extra_ids_list_same_as_none(self):
        self.assertEqual(route_label("I-30", "W", []), "I-30 WB")

    def test_none_if_unmatched_even_with_extra_ids(self):
        self.assertIsNone(route_label(None, "W", ["US 70"]))


class TestConcurrentRoadIdsIndexed(unittest.TestCase):
    # Same real-world case as TestNearestRoad's concurrent fixture - I-10
    # and US-70 running physically concurrent near Deming, NM - plus a
    # distant road to confirm the index doesn't just return everything.
    ROADS = [
        {"route_id": "US 70", "route_type": "us_route",
         "geometry": [(35.0, -90.0), (35.0, -89.5), (35.0, -89.0)]},
        {"route_id": "I-10", "route_type": "interstate",
         "geometry": [(35.0001, -90.0), (35.0001, -89.5), (35.0001, -89.0)]},
        {"route_id": "Far Away Rd", "route_type": "state_route",
         "geometry": [(45.0, -90.0), (45.0, -89.0)]},
    ]

    def test_finds_the_concurrent_route_excluding_primary(self):
        index = build_road_segment_index(self.ROADS)
        extras = concurrent_road_ids_indexed(35.0, -89.7, index, tolerance_mi=0.05,
                                             exclude_id="I-10")
        self.assertEqual(extras, ["US 70"])

    def test_excludes_the_primary_route_itself(self):
        index = build_road_segment_index(self.ROADS)
        extras = concurrent_road_ids_indexed(35.0001, -89.7, index, tolerance_mi=0.05,
                                             exclude_id="I-10")
        self.assertNotIn("I-10", extras)

    def test_empty_when_nothing_else_within_tolerance(self):
        index = build_road_segment_index(self.ROADS)
        extras = concurrent_road_ids_indexed(45.0, -89.5, index, tolerance_mi=0.05,
                                             exclude_id="Far Away Rd")
        self.assertEqual(extras, [])

    def test_max_extra_caps_the_result(self):
        roads = self.ROADS + [
            {"route_id": "I-40", "route_type": "interstate",
             "geometry": [(35.0002, -90.0), (35.0002, -89.0)]},
        ]
        index = build_road_segment_index(roads)
        extras = concurrent_road_ids_indexed(35.0, -89.7, index, tolerance_mi=0.05,
                                             exclude_id=None, max_extra=1)
        self.assertEqual(len(extras), 1)

    def test_sorted_by_route_type_tier_then_distance(self):
        # I-40 (interstate, tier 0) should sort ahead of US 70 (us_route,
        # tier 1) regardless of raw distance, same tie-break as
        # nearest_road()/_ROUTE_TYPE_PRIORITY.
        roads = self.ROADS + [
            {"route_id": "I-40", "route_type": "interstate",
             "geometry": [(35.0002, -90.0), (35.0002, -89.0)]},
        ]
        index = build_road_segment_index(roads)
        extras = concurrent_road_ids_indexed(35.0, -89.7, index, tolerance_mi=0.05,
                                             exclude_id="I-10", max_extra=2)
        self.assertEqual(extras, ["I-40", "US 70"])

    def test_multiple_segments_of_same_route_id_deduped(self):
        # A route split into several separate LineString features (common
        # in real TIGER data) must still only produce ONE entry for that
        # route_id, not one per segment.
        roads = [
            {"route_id": "US 70", "route_type": "us_route",
             "geometry": [(35.0, -90.0), (35.0, -89.6)]},
            {"route_id": "US 70", "route_type": "us_route",
             "geometry": [(35.0, -89.6), (35.0, -89.0)]},
        ]
        index = build_road_segment_index(roads)
        extras = concurrent_road_ids_indexed(35.0, -89.7, index, tolerance_mi=0.05,
                                             exclude_id=None)
        self.assertEqual(extras, ["US 70"])


class TestConcurrentDesignationsPerSecond(unittest.TestCase):
    ROADS = [
        {"route_id": "US 70", "route_type": "us_route",
         "geometry": [(35.0, -90.0), (35.0, -89.5), (35.0, -89.0)]},
        {"route_id": "I-10", "route_type": "interstate",
         "geometry": [(35.0001, -90.0), (35.0001, -89.5), (35.0001, -89.0)]},
    ]

    def test_finds_extras_only_on_matched_seconds(self):
        index = build_road_segment_index(self.ROADS)
        positions = [(35.0, -89.7), (36.0, -89.7)]  # 2nd point far from anything
        matches = [("I-10", "W"), (None, None)]
        result = concurrent_designations_per_second(positions, matches, index,
                                                     tolerance_mi=0.05)
        self.assertEqual(result, [["US 70"], []])

    def test_freeze_while_stopped_repeats_last_extras(self):
        index = build_road_segment_index(self.ROADS)
        # Second point would normally find nothing (far away), but a low
        # speed should freeze the previous second's extras instead of
        # recomputing.
        positions = [(35.0, -89.7), (36.0, -89.7)]
        matches = [("I-10", "W"), ("I-10", "W")]
        result = concurrent_designations_per_second(
            positions, matches, index, tolerance_mi=0.05,
            speeds=[10.0, 1.0], freeze_below_mph=3.0)
        self.assertEqual(result, [["US 70"], ["US 70"]])

    def test_max_extra_passthrough(self):
        roads = self.ROADS + [
            {"route_id": "I-40", "route_type": "interstate",
             "geometry": [(35.0002, -90.0), (35.0002, -89.0)]},
        ]
        index = build_road_segment_index(roads)
        result = concurrent_designations_per_second(
            [(35.0, -89.7)], [("I-10", "W")], index, tolerance_mi=0.05, max_extra=2)
        self.assertEqual(result, [["I-40", "US 70"]])

    def test_mismatched_speeds_length_raises(self):
        index = build_road_segment_index(self.ROADS)
        with self.assertRaises(ValueError):
            concurrent_designations_per_second(
                [(35.0, -89.7)], [("I-10", "W")], index, tolerance_mi=0.05,
                speeds=[1.0, 2.0])


class TestRenderInfoFrames(unittest.TestCase):
    def test_smoke_writes_one_png_per_second_with_shield_slot(self):
        cfg = configparser.ConfigParser()
        cfg.read_dict({"roads": {"shield_height_px": "40",
                                 "text_zone_width_px": "300", "shield_gap_px": "8",
                                 "route_label_width_px": "100", "route_label_gap_px": "4"},
                       "info": {"font_size": "20"}})
        shields = {"I-30": render_shield("I-30", "interstate", 40)}
        texts = [[("70 mph 10 mi", False)], [("70 mph 11 mi", False)],
                [("70 mph 12 mi", False)]]
        matches = [("I-30", "W"), ("I-30", "W"), (None, None)]
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "info"
            w, h = render_info_frames(texts, matches, shields, cfg, out_dir,
                                      video_width_px=960)
            files = sorted(out_dir.glob("*.png"))
            self.assertEqual(len(files), 3)
            self.assertEqual(files[0].name, "000000.png")
            self.assertEqual(w, 960)
            self.assertGreater(h, 0)

    def test_frame_offset_shifts_filenames(self):
        cfg = configparser.ConfigParser()
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "info"
            render_info_frames([[("70 mph", False)]], [(None, None)], {}, cfg,
                               out_dir, video_width_px=960, frame_offset=50)
            self.assertTrue((out_dir / "000050.png").exists())

    def test_local_road_match_draws_label_without_shield(self):
        """Round 3 regression test: a matched rid that is NOT in `shields`
        (a local road - shields is only ever built from [roads]' highway
        list) must still render successfully. Before the Round 3
        restructuring, the route-label draw lived entirely inside
        `if rid in shields`, so this exact case silently drew no label."""
        cfg = configparser.ConfigParser()
        cfg.read_dict({"roads": {"shield_height_px": "40",
                                 "text_zone_width_px": "300", "shield_gap_px": "8",
                                 "route_label_width_px": "160", "route_label_gap_px": "4"},
                       "info": {"font_size": "20"}})
        texts = [[("70 mph 10 mi", False)]]
        matches = [("Maple Street", "W")]  # not in shields -> local-road fallback
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "info"
            render_info_frames(texts, matches, {}, cfg, out_dir, video_width_px=960)
            files = sorted(out_dir.glob("*.png"))
            self.assertEqual(len(files), 1)

    def test_local_road_label_actually_renders_pixels(self):
        """Stronger than the smoke test above: confirms the label pixels
        are really drawn (not just "doesn't crash") by comparing opaque
        pixel counts against an unmatched baseline frame."""
        cfg = configparser.ConfigParser()
        cfg.read_dict({"roads": {"shield_height_px": "40",
                                 "text_zone_width_px": "300", "shield_gap_px": "8",
                                 "route_label_width_px": "160", "route_label_gap_px": "4"},
                       "info": {"font_size": "20"}})
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp) / "baseline"
            render_info_frames([[("70 mph", False)]], [(None, None)], {}, cfg,
                               base_dir, video_width_px=960)
            baseline_alpha = sum(
                Image.open(base_dir / "000000.png").getchannel("A").tobytes())

            local_dir = Path(tmp) / "local"
            render_info_frames([[("70 mph", False)]], [("Maple Street", "W")], {},
                               cfg, local_dir, video_width_px=960)
            local_alpha = sum(
                Image.open(local_dir / "000000.png").getchannel("A").tobytes())

        self.assertGreater(local_alpha, baseline_alpha)

    def test_extras_widen_the_route_label(self):
        """Concurrent designations ("I-10 / US 70 WB") must actually widen
        the rendered label, not just get silently ignored - compares opaque
        pixel counts with vs. without an extras list for the same match."""
        cfg = configparser.ConfigParser()
        cfg.read_dict({"roads": {"shield_height_px": "40",
                                 "text_zone_width_px": "300", "shield_gap_px": "8",
                                 "route_label_width_px": "220", "route_label_gap_px": "4"},
                       "info": {"font_size": "20"}})
        with tempfile.TemporaryDirectory() as tmp:
            single_dir = Path(tmp) / "single"
            render_info_frames([[("70 mph", False)]], [("I-10", "W")], {}, cfg,
                               single_dir, video_width_px=960)
            single_alpha = sum(
                Image.open(single_dir / "000000.png").getchannel("A").tobytes())

            concurrent_dir = Path(tmp) / "concurrent"
            render_info_frames([[("70 mph", False)]], [("I-10", "W")], {}, cfg,
                               concurrent_dir, video_width_px=960,
                               extras=[["US 70"]])
            concurrent_alpha = sum(
                Image.open(concurrent_dir / "000000.png").getchannel("A").tobytes())

        self.assertGreater(concurrent_alpha, single_alpha)

    def test_dim_segment_renders_lower_alpha_than_bright(self):
        """The whole point of the dimmed-leading-zero feature: a dim
        segment must actually be drawn less opaque than the same glyph
        drawn bright, not just tagged and ignored."""
        cfg = configparser.ConfigParser()
        cfg.read_dict({"info": {"font_size": "20"}})
        with tempfile.TemporaryDirectory() as tmp:
            bright_dir = Path(tmp) / "bright"
            render_info_frames([[("0", False)]], [(None, None)], {}, cfg,
                               bright_dir, video_width_px=960)
            bright_alpha = sum(
                Image.open(bright_dir / "000000.png").getchannel("A").tobytes())

            dim_dir = Path(tmp) / "dim"
            render_info_frames([[("0", True)]], [(None, None)], {}, cfg,
                               dim_dir, video_width_px=960)
            dim_alpha = sum(
                Image.open(dim_dir / "000000.png").getchannel("A").tobytes())

        self.assertGreater(bright_alpha, dim_alpha)
        self.assertGreater(dim_alpha, 0)  # still drawn, just dimmer - not invisible

    def test_multi_segment_split_does_not_shift_centering(self):
        """Sean's hard layout rule, extended to the dim/bright split: the
        same visible glyphs drawn as one segment vs. split into dim+bright
        segments must land at the exact same horizontal position."""
        cfg = configparser.ConfigParser()
        cfg.read_dict({"info": {"font_size": "20"}})
        with tempfile.TemporaryDirectory() as tmp:
            single_dir = Path(tmp) / "single"
            render_info_frames([[("072", False)]], [(None, None)], {}, cfg,
                               single_dir, video_width_px=960)
            multi_dir = Path(tmp) / "multi"
            render_info_frames([[("0", True), ("72", False)]], [(None, None)],
                               {}, cfg, multi_dir, video_width_px=960)
            single_bbox = Image.open(single_dir / "000000.png").getchannel("A").getbbox()
            multi_bbox = Image.open(multi_dir / "000000.png").getchannel("A").getbbox()

        self.assertIsNotNone(single_bbox)
        self.assertIsNotNone(multi_bbox)
        self.assertEqual(single_bbox[0], multi_bbox[0])
        self.assertEqual(single_bbox[2], multi_bbox[2])

    def test_compass_disabled_by_default(self):
        cfg = configparser.ConfigParser()
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "info"
            render_info_frames([[("70 mph", False)]], [(None, None)], {}, cfg,
                               out_dir, video_width_px=960,
                               compass=[(225.0, "SW")])
            self.assertTrue((out_dir / "000000.png").exists())

    def test_compass_enabled_with_reading_draws_extra_pixels(self):
        cfg = configparser.ConfigParser()
        cfg.read_dict({"compass": {"enabled": "true", "size_px": "40"},
                       "roads": {"text_zone_width_px": "300"}})
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp) / "base"
            render_info_frames([[("70 mph", False)]], [(None, None)], {}, cfg,
                               base_dir, video_width_px=960, compass=[None])
            base_alpha = sum(
                Image.open(base_dir / "000000.png").getchannel("A").tobytes())

            comp_dir = Path(tmp) / "compass"
            render_info_frames([[("70 mph", False)]], [(None, None)], {}, cfg,
                               comp_dir, video_width_px=960, compass=[(225.0, "SW")])
            comp_alpha = sum(
                Image.open(comp_dir / "000000.png").getchannel("A").tobytes())

        self.assertGreater(comp_alpha, base_alpha)

    def test_glow_disabled_by_default(self):
        cfg = configparser.ConfigParser()
        cfg.read_dict({"roads": {"shield_height_px": "40",
                                 "text_zone_width_px": "300", "shield_gap_px": "8",
                                 "route_label_width_px": "100", "route_label_gap_px": "4"},
                       "info": {"font_size": "20"}})
        shields = {"I-30": render_shield("I-30", "interstate", 40)}
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "info"
            render_info_frames([[("70 mph", False)]], [("I-30", "W")], shields, cfg,
                               out_dir, video_width_px=960)
            self.assertTrue((out_dir / "000000.png").exists())

    def test_glow_enabled_draws_extra_pixels(self):
        cfg_kwargs = {"roads": {"shield_height_px": "40",
                                "text_zone_width_px": "300", "shield_gap_px": "8",
                                "route_label_width_px": "100", "route_label_gap_px": "4"},
                     "info": {"font_size": "20"}}
        shields = {"I-30": render_shield("I-30", "interstate", 40)}
        with tempfile.TemporaryDirectory() as tmp:
            base_cfg = configparser.ConfigParser()
            base_cfg.read_dict(cfg_kwargs)
            base_dir = Path(tmp) / "base"
            render_info_frames([[("70 mph", False)]], [("I-30", "W")], shields, base_cfg,
                               base_dir, video_width_px=960)
            base_alpha = sum(
                Image.open(base_dir / "000000.png").getchannel("A").tobytes())

            glow_cfg = configparser.ConfigParser()
            glow_kwargs = {k: dict(v) for k, v in cfg_kwargs.items()}
            glow_kwargs["roads"]["shield_glow_enabled"] = "true"
            glow_kwargs["roads"]["shield_glow_radius_px"] = "6"
            glow_kwargs["roads"]["shield_glow_alpha"] = "170"
            glow_cfg.read_dict(glow_kwargs)
            glow_dir = Path(tmp) / "glow"
            render_info_frames([[("70 mph", False)]], [("I-30", "W")], shields, glow_cfg,
                               glow_dir, video_width_px=960)
            glow_alpha = sum(
                Image.open(glow_dir / "000000.png").getchannel("A").tobytes())

        self.assertGreater(glow_alpha, base_alpha)

    def test_glow_does_not_crash_with_no_shield_match(self):
        # Glow enabled but no highway match this second - must not error
        # (there's no shield to glow behind).
        cfg = configparser.ConfigParser()
        cfg.read_dict({"roads": {"shield_glow_enabled": "true"},
                       "info": {"font_size": "20"}})
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "info"
            render_info_frames([[("70 mph", False)]], [(None, None)], {}, cfg,
                               out_dir, video_width_px=960)
            self.assertTrue((out_dir / "000000.png").exists())

    def test_compass_glow_disabled_by_default(self):
        cfg = configparser.ConfigParser()
        cfg.read_dict({"compass": {"enabled": "true", "size_px": "40"},
                       "roads": {"text_zone_width_px": "300"}})
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "info"
            render_info_frames([[("70 mph", False)]], [(None, None)], {}, cfg,
                               out_dir, video_width_px=960, compass=[(225.0, "SW")])
            self.assertTrue((out_dir / "000000.png").exists())

    def test_compass_glow_enabled_draws_extra_pixels(self):
        base_kwargs = {"compass": {"enabled": "true", "size_px": "40"},
                      "roads": {"text_zone_width_px": "300"}}
        with tempfile.TemporaryDirectory() as tmp:
            base_cfg = configparser.ConfigParser()
            base_cfg.read_dict(base_kwargs)
            base_dir = Path(tmp) / "base"
            render_info_frames([[("70 mph", False)]], [(None, None)], {}, base_cfg,
                               base_dir, video_width_px=960, compass=[(225.0, "SW")])
            base_alpha = sum(
                Image.open(base_dir / "000000.png").getchannel("A").tobytes())

            glow_kwargs = {k: dict(v) for k, v in base_kwargs.items()}
            glow_kwargs["compass"]["glow_enabled"] = "true"
            glow_kwargs["compass"]["glow_radius_px"] = "6"
            glow_kwargs["compass"]["glow_alpha"] = "170"
            glow_cfg = configparser.ConfigParser()
            glow_cfg.read_dict(glow_kwargs)
            glow_dir = Path(tmp) / "glow"
            render_info_frames([[("70 mph", False)]], [(None, None)], {}, glow_cfg,
                               glow_dir, video_width_px=960, compass=[(225.0, "SW")])
            glow_alpha = sum(
                Image.open(glow_dir / "000000.png").getchannel("A").tobytes())

        self.assertGreater(glow_alpha, base_alpha)


class TestShieldGlowCache(unittest.TestCase):
    def test_one_glow_per_distinct_shield(self):
        shields = {"I-30": render_shield("I-30", "interstate", 40),
                  "US 82": render_shield("US 82", "us_route", 40)}
        glows = shield_glow_cache_for(shields, radius_px=6, alpha=170, color=(0, 0, 0))
        self.assertEqual(set(glows.keys()), {"I-30", "US 82"})

    def test_glow_image_padded_beyond_shield_size(self):
        shields = {"I-30": render_shield("I-30", "interstate", 40)}
        glows = shield_glow_cache_for(shields, radius_px=6, alpha=170, color=(0, 0, 0))
        glow_img, pad = glows["I-30"]
        sw, sh = shields["I-30"].size
        self.assertEqual(glow_img.size, (sw + pad * 2, sh + pad * 2))
        self.assertGreater(pad, 0)
        self.assertEqual(glow_img.mode, "RGBA")

    def test_empty_shields_yields_empty_cache(self):
        self.assertEqual(shield_glow_cache_for({}, radius_px=6, alpha=170, color=(0, 0, 0)), {})


class TestGaussianGlow(unittest.TestCase):
    """_gaussian_glow() - the primitive shared by shield_glow_cache_for()
    (per-route, cached) and render_compass_rose()'s glow (per-frame)."""

    def test_padded_size_and_mode(self):
        shield = render_shield("I-30", "interstate", 40)
        glow = _gaussian_glow(shield, pad=15, radius_px=6, alpha=170, color=(0, 0, 0))
        w, h = shield.size
        self.assertEqual(glow.size, (w + 30, h + 30))
        self.assertEqual(glow.mode, "RGBA")

    def test_matches_shield_glow_cache_for_output(self):
        """shield_glow_cache_for() is now a thin wrapper - confirm it
        produces the exact same result as calling the primitive directly,
        so the refactor didn't silently change behavior."""
        shields = {"I-30": render_shield("I-30", "interstate", 40)}
        glows = shield_glow_cache_for(shields, radius_px=6, alpha=170, color=(0, 0, 0))
        glow_img, pad = glows["I-30"]
        direct = _gaussian_glow(shields["I-30"], pad, 6, 170, (0, 0, 0))
        self.assertEqual(glow_img.size, direct.size)
        self.assertEqual(list(glow_img.getdata()), list(direct.getdata()))


class TestRenderCompassRose(unittest.TestCase):
    """render_compass_rose() - 2026-07-14 redesign per Sean's request for
    supersampling/glow parity with the shield, a traditional two-tone
    needle, and NSEW tick labels."""

    def test_returns_correct_size_and_mode(self):
        img = render_compass_rose(45.0, 40)
        self.assertEqual(img.size, (40, 40))
        self.assertEqual(img.mode, "RGBA")

    def test_needle_color_appears_in_render(self):
        needle = (211, 33, 33, 255)
        img = render_compass_rose(0.0, 60, needle_color=needle)
        close = [p for p in img.getdata()
                if abs(p[0] - needle[0]) < 15 and abs(p[1] - needle[1]) < 15
                and abs(p[2] - needle[2]) < 15 and p[3] > 200]
        self.assertGreater(len(close), 0)

    def test_custom_tail_color_appears_in_render(self):
        tail = (10, 200, 10, 255)
        img = render_compass_rose(0.0, 60, tail_color=tail)
        close = [p for p in img.getdata()
                if abs(p[0] - tail[0]) < 15 and abs(p[1] - tail[1]) < 15
                and abs(p[2] - tail[2]) < 15 and p[3] > 200]
        self.assertGreater(len(close), 0)

    def test_different_heading_changes_the_render(self):
        img_a = render_compass_rose(0.0, 60)
        img_b = render_compass_rose(180.0, 60)
        self.assertNotEqual(list(img_a.getdata()), list(img_b.getdata()))

    def test_smoke_across_all_quadrants(self):
        # no crash across a full rotation, including exact cardinal angles
        for heading in (0.0, 45.0, 90.0, 135.0, 180.0, 225.0, 270.0, 315.0, 359.9):
            img = render_compass_rose(heading, 44)
            self.assertEqual(img.mode, "RGBA")


class TestLoadRoads(unittest.TestCase):
    def test_parses_linestring_and_converts_lon_lat_order(self):
        geo = {"type": "FeatureCollection", "features": [{
            "type": "Feature",
            "properties": {"route_id": "I-30", "route_type": "interstate"},
            "geometry": {"type": "LineString",
                        "coordinates": [[-90.0, 35.0], [-89.0, 35.5]]},
        }]}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "roads.geojson"
            path.write_text(json.dumps(geo), encoding="utf-8")
            roads = load_roads(path)
        self.assertEqual(len(roads), 1)
        self.assertEqual(roads[0]["route_id"], "I-30")
        self.assertEqual(roads[0]["route_type"], "interstate")
        self.assertEqual(roads[0]["geometry"], [(35.0, -90.0), (35.5, -89.0)])

    def test_missing_file_warns_and_returns_empty(self):
        self.assertEqual(load_roads(Path("/nonexistent/roads.geojson")), [])

    def test_skips_feature_without_route_id(self):
        geo = {"type": "FeatureCollection", "features": [{
            "type": "Feature", "properties": {},
            "geometry": {"type": "LineString",
                        "coordinates": [[-90.0, 35.0], [-89.0, 35.5]]},
        }]}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "roads.geojson"
            path.write_text(json.dumps(geo), encoding="utf-8")
            self.assertEqual(load_roads(path), [])


class TestHaversineMilesExtract(unittest.TestCase):
    """_haversine_miles is a deliberate stage-1-local duplicate of
    render_overlay.haversine_miles - see the EARTH_RADIUS_MI comment in
    extract_gps.py. Tested separately so the two copies can't silently
    drift apart undetected."""

    def test_zero_distance(self):
        self.assertEqual(_haversine_miles(36.85, -76.29, 36.85, -76.29), 0.0)

    def test_one_degree_latitude(self):
        # ~69 mi per degree of latitude anywhere on the globe
        self.assertAlmostEqual(_haversine_miles(34.0, -93.0, 35.0, -93.0),
                               69.0, delta=0.5)


class TestFindTimeGaps(unittest.TestCase):
    def _pt(self, clip, ts, valid=True, lat=38.9, lon=-77.3):
        return GpsPoint(clip=clip, sec_in_clip=0, timestamp_utc=ts, valid=valid,
                        lat=lat, lon=lon, speed_mph=65.0, heading=90.0)

    def test_no_gap_below_threshold(self):
        pts = [self._pt("a.MP4", "2022-05-23 10:00:00"),
               self._pt("a.MP4", "2022-05-23 10:05:00")]
        self.assertEqual(find_time_gaps(pts, 10.0), [])

    def test_gap_above_threshold(self):
        pts = [self._pt("a.MP4", "2022-05-23 10:00:00", lat=38.90, lon=-77.30),
               self._pt("b.MP4", "2022-05-23 11:30:00", lat=38.88, lon=-77.10)]
        gaps = find_time_gaps(pts, 10.0)
        self.assertEqual(len(gaps), 1)
        g = gaps[0]
        self.assertEqual(g["before_clip"], "a.MP4")
        self.assertEqual(g["after_clip"], "b.MP4")
        self.assertEqual(g["gap_minutes"], 90.0)
        self.assertGreater(g["straight_line_mi"], 0)

    def test_invalid_points_excluded(self):
        pts = [self._pt("a.MP4", "2022-05-23 10:00:00"),
               self._pt("a.MP4", "2022-05-23 10:01:00", valid=False),
               self._pt("b.MP4", "2022-05-23 11:30:00")]
        gaps = find_time_gaps(pts, 10.0)
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]["before_ts"], "2022-05-23 10:00:00")

    def test_unsorted_input_is_sorted_by_timestamp(self):
        pts = [self._pt("b.MP4", "2022-05-23 11:30:00"),
               self._pt("a.MP4", "2022-05-23 10:00:00")]
        gaps = find_time_gaps(pts, 10.0)
        self.assertEqual(gaps[0]["before_clip"], "a.MP4")
        self.assertEqual(gaps[0]["after_clip"], "b.MP4")

    def test_custom_threshold(self):
        pts = [self._pt("a.MP4", "2022-05-23 10:00:00"),
               self._pt("a.MP4", "2022-05-23 10:03:00")]
        self.assertEqual(find_time_gaps(pts, 10.0), [])
        gaps = find_time_gaps(pts, 2.0)
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]["gap_minutes"], 3.0)

    def test_i66_virginia_style_gap(self):
        # Roughly along I-66 near Manassas/Front Royal, VA - a fairly
        # straight stretch, so straight-line vs. actual route distance
        # should be close (context: Sean's real gap was on I-66 in VA).
        pts = [self._pt("a.MP4", "2022-05-23 14:00:00", lat=38.7509, lon=-77.4753),
               self._pt("b.MP4", "2022-05-23 15:45:00", lat=38.9012, lon=-78.1948)]
        gaps = find_time_gaps(pts, 10.0)
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]["gap_minutes"], 105.0)
        self.assertGreater(gaps[0]["straight_line_mi"], 30)
        self.assertLess(gaps[0]["straight_line_mi"], 50)


class TestResolveTownLabels(unittest.TestCase):
    """resolve_town_labels() - 2026-07-13 fix for the Praxedis Guerrero,
    Chihuahua (MX) mislabel on real I-10 points solidly inside Texas. See
    its docstring in extract_gps.py and CLAUDE.md for the full case."""

    def _us(self, name, admin1):
        return {"name": name, "admin1": admin1, "cc": "US"}

    def _mx(self, name, admin1):
        return {"name": name, "admin1": admin1, "cc": "MX"}

    def test_all_us_matches_pass_through(self):
        raw = [self._us("Van Horn", "Texas"), self._us("Sierra Blanca", "Texas")]
        self.assertEqual(resolve_town_labels(raw),
                         [("Van Horn", "TX"), ("Sierra Blanca", "TX")])

    def test_none_yields_blank_and_does_not_touch_carry_state(self):
        raw = [self._us("Van Horn", "TX"), None, self._us("Sierra Blanca", "Texas")]
        result = resolve_town_labels(raw)
        self.assertEqual(result[0], ("Van Horn", "TX"))
        self.assertEqual(result[1], ("", ""))
        self.assertEqual(result[2], ("Sierra Blanca", "TX"))

    def test_non_us_match_carries_forward_last_us_town(self):
        # Real case: real I-10 points near Fabens/Tornillo, TX resolved to
        # the nearer Mexican town in the offline dataset even though the
        # vehicle never left the US.
        raw = [self._us("Fabens", "Texas"),
               self._mx("Praxedis Guerrero", "Chihuahua"),
               self._mx("Praxedis Guerrero", "Chihuahua"),
               self._us("Tornillo", "Texas")]
        self.assertEqual(resolve_town_labels(raw),
                         [("Fabens", "TX"), ("Fabens", "TX"),
                          ("Fabens", "TX"), ("Tornillo", "TX")])

    def test_leading_non_us_before_any_us_match_yields_blank(self):
        raw = [self._mx("Praxedis Guerrero", "Chihuahua"), self._us("El Paso", "Texas")]
        self.assertEqual(resolve_town_labels(raw),
                         [("", ""), ("El Paso", "TX")])

    def test_el_paso_border_city_is_a_confident_us_match_not_suppressed(self):
        # Acid test per Sean: a real US border city right at the Mexico
        # line must still show normally, not get treated as suspect just
        # for being close to the border - only an actual cc != 'US' match
        # triggers carry-forward.
        raw = [self._us("El Paso", "Texas"), self._us("El Paso", "Texas")]
        self.assertEqual(resolve_town_labels(raw),
                         [("El Paso", "TX"), ("El Paso", "TX")])

    def test_unmapped_admin1_falls_back_to_raw_string(self):
        # Mirrors geocode()'s old fallback behavior for an admin1 value
        # not present in US_STATE_ABBR (shouldn't happen for cc=='US' in
        # practice, but the fallback path itself is worth covering).
        raw = [self._us("Somewhere", "Not A Real State")]
        self.assertEqual(resolve_town_labels(raw), [("Somewhere", "Not A Real State")])


# --- tools/fetch_tiger_roads.py -----------------------------------------
#
# fetch_tiger_roads.py imports `shapefile` (pyshp) at module level (unlike
# extract_gps.py's reverse_geocoder, which is a deferred import specifically
# so the rest of the test suite doesn't need it) - pyshp is a small pure-
# Python package with none of reverse_geocoder's size/crash concerns (see
# CLAUDE.md's Windows pagefile note), and it's already an unconditional
# requirements.txt entry, so this is a reasonable trade rather than the
# same deferred-import treatment.

def _write_track_csv(path: Path, rows: list[dict[str, str]]) -> None:
    """Minimal track.csv - only the columns state_bboxes_from_track()/
    sample_points() actually read (valid, lat, lon, state)."""
    lines = ["valid,lat,lon,state"]
    for r in rows:
        lines.append(f"{r['valid']},{r['lat']},{r['lon']},{r.get('state', '')}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class _FakeShape:
    """Stand-in for shapefile.Reader's shape objects - real code only
    touches .points and .parts."""
    def __init__(self, points: list[tuple[float, float]], parts: list[int]):
        self.points = points
        self.parts = parts


class _FakeRecord:
    """Stand-in for shapefile.Reader's record objects - real code only
    calls .as_dict()."""
    def __init__(self, fields: dict[str, str]):
        self._fields = fields

    def as_dict(self) -> dict[str, str]:
        return self._fields


class _FakeShapeRecord:
    def __init__(self, fields: dict[str, str],
                points: list[tuple[float, float]], parts: list[int] = (0,)):
        self.record = _FakeRecord(fields)
        self.shape = _FakeShape(points, list(parts))


class _FakeShapefileReader:
    """Stand-in for shapefile.Reader - real code only calls
    .iterShapeRecords()."""
    def __init__(self, records: list[_FakeShapeRecord]):
        self._records = records

    def iterShapeRecords(self):
        return iter(self._records)


class TestNormalizeHighwayId(unittest.TestCase):
    def test_interstate_extracts_number(self):
        # The real bug this guards against: a naive last-token split on
        # "I- 395 Hov" would read "Hov" instead of "395".
        self.assertEqual(normalize_highway_id("I- 395 Hov", "I"), "I-395")

    def test_us_route_extracts_number(self):
        self.assertEqual(normalize_highway_id("US Hwy 11", "U"), "US 11")

    def test_no_digits_returns_none(self):
        self.assertIsNone(normalize_highway_id("Ramp", "I"))

    def test_empty_string_returns_none(self):
        self.assertIsNone(normalize_highway_id("", "U"))

    def test_none_fullname_returns_none(self):
        self.assertIsNone(normalize_highway_id(None, "I"))


class TestInBbox(unittest.TestCase):
    BBOX = (30.0, 40.0, -95.0, -85.0)  # lat_min, lat_max, lon_min, lon_max

    def test_point_inside(self):
        self.assertTrue(in_bbox(35.0, -90.0, self.BBOX))

    def test_point_outside_latitude(self):
        self.assertFalse(in_bbox(45.0, -90.0, self.BBOX))

    def test_point_outside_longitude(self):
        self.assertFalse(in_bbox(35.0, -100.0, self.BBOX))

    def test_boundary_is_inclusive(self):
        self.assertTrue(in_bbox(30.0, -95.0, self.BBOX))
        self.assertTrue(in_bbox(40.0, -85.0, self.BBOX))


class TestSplitShapeParts(unittest.TestCase):
    def test_single_part_returns_all_points(self):
        shape = _FakeShape([(0, 0), (1, 1), (2, 2)], [0])
        self.assertEqual(split_shape_parts(shape), [[(0, 0), (1, 1), (2, 2)]])

    def test_multi_part_splits_at_part_boundaries(self):
        shape = _FakeShape([(0, 0), (1, 1), (2, 2), (3, 3)], [0, 2])
        self.assertEqual(split_shape_parts(shape),
                         [[(0, 0), (1, 1)], [(2, 2), (3, 3)]])

    def test_three_parts(self):
        shape = _FakeShape([(0, 0), (1, 1), (2, 2), (3, 3), (4, 4)], [0, 2, 4])
        self.assertEqual(split_shape_parts(shape),
                         [[(0, 0), (1, 1)], [(2, 2), (3, 3)], [(4, 4)]])


class TestStateBboxesFromTrack(unittest.TestCase):
    def test_computes_padded_bbox_per_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            track = Path(tmp) / "track.csv"
            _write_track_csv(track, [
                {"valid": "1", "lat": "36.0", "lon": "-84.0", "state": "TN"},
                {"valid": "1", "lat": "36.5", "lon": "-84.5", "state": "TN"},
            ])
            boxes = state_bboxes_from_track(track, pad_deg=0.1)
            self.assertEqual(set(boxes), {"TN"})
            lat_min, lat_max, lon_min, lon_max = boxes["TN"]
            self.assertAlmostEqual(lat_min, 35.9)
            self.assertAlmostEqual(lat_max, 36.6)
            self.assertAlmostEqual(lon_min, -84.6)
            self.assertAlmostEqual(lon_max, -83.9)

    def test_invalid_and_blank_state_rows_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            track = Path(tmp) / "track.csv"
            _write_track_csv(track, [
                {"valid": "0", "lat": "36.0", "lon": "-84.0", "state": "TN"},
                {"valid": "1", "lat": "36.0", "lon": "-84.0", "state": ""},
            ])
            self.assertEqual(state_bboxes_from_track(track), {})

    def test_non_us_state_tag_skipped_not_errored(self):
        # Real case: a reverse-geocoding artifact near the Mexico border
        # tags a point "Chihuahua" - not in STATE_FIPS, must not crash the
        # whole run (see the El Paso / Praxedis Guerrero note in CLAUDE.md).
        with tempfile.TemporaryDirectory() as tmp:
            track = Path(tmp) / "track.csv"
            _write_track_csv(track, [
                {"valid": "1", "lat": "31.4", "lon": "-106.0", "state": "Chihuahua"},
                {"valid": "1", "lat": "31.8", "lon": "-106.5", "state": "TX"},
            ])
            boxes = state_bboxes_from_track(track)
            self.assertEqual(set(boxes), {"TX"})

    def test_no_valid_us_points_yields_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            track = Path(tmp) / "track.csv"
            _write_track_csv(track, [{"valid": "0", "lat": "0", "lon": "0", "state": ""}])
            self.assertEqual(state_bboxes_from_track(track), {})


class TestSamplePoints(unittest.TestCase):
    def test_every_nth_valid_point(self):
        with tempfile.TemporaryDirectory() as tmp:
            track = Path(tmp) / "track.csv"
            rows = [{"valid": "1", "lat": str(i), "lon": str(-i), "state": "TX"}
                    for i in range(10)]
            _write_track_csv(track, rows)
            pts = sample_points(track, every_n=3)
            self.assertEqual(pts, [(0.0, 0.0), (3.0, -3.0), (6.0, -6.0), (9.0, -9.0)])

    def test_invalid_points_excluded_before_striding(self):
        with tempfile.TemporaryDirectory() as tmp:
            track = Path(tmp) / "track.csv"
            _write_track_csv(track, [
                {"valid": "0", "lat": "1", "lon": "-1", "state": "TX"},
                {"valid": "1", "lat": "2", "lon": "-2", "state": "TX"},
            ])
            self.assertEqual(sample_points(track, every_n=1), [(2.0, -2.0)])

    def test_empty_track_yields_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            track = Path(tmp) / "track.csv"
            _write_track_csv(track, [])
            self.assertEqual(sample_points(track), [])


class TestWriteGeojson(unittest.TestCase):
    def test_writes_valid_feature_collection(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "roads.geojson"
            features = [{"type": "Feature", "properties": {"route_id": "I-30"},
                        "geometry": {"type": "LineString", "coordinates": [[0, 0], [1, 1]]}}]
            write_geojson(features, out)
            data = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(data["type"], "FeatureCollection")
            self.assertEqual(data["features"], features)

    def test_creates_parent_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "nested" / "map_data" / "roads.geojson"
            write_geojson([], out)
            self.assertTrue(out.exists())


class TestBuildHighwayFeatures(unittest.TestCase):
    BBOX = (30.0, 40.0, -95.0, -85.0)

    def test_filters_to_interstate_and_us_route_only(self):
        records = [
            _FakeShapeRecord({"RTTYP": "I", "FULLNAME": "I- 30"}, [(-90, 35), (-89, 35)]),
            _FakeShapeRecord({"RTTYP": "M", "FULLNAME": "Some County Road"},
                            [(-90, 35), (-89, 35)]),
        ]
        feats = build_highway_features(_FakeShapefileReader(records), self.BBOX)
        self.assertEqual(len(feats), 1)
        self.assertEqual(feats[0]["properties"]["route_id"], "I-30")

    def test_route_type_matches_rttyp(self):
        records = [
            _FakeShapeRecord({"RTTYP": "I", "FULLNAME": "I-30"}, [(-90, 35), (-89, 35)]),
            _FakeShapeRecord({"RTTYP": "U", "FULLNAME": "US 70"}, [(-90, 35), (-89, 35)]),
        ]
        feats = build_highway_features(_FakeShapefileReader(records), self.BBOX)
        types = {f["properties"]["route_id"]: f["properties"]["route_type"] for f in feats}
        self.assertEqual(types, {"I-30": "interstate", "US 70": "us_route"})

    def test_skipped_when_no_route_number_found(self):
        records = [_FakeShapeRecord({"RTTYP": "I", "FULLNAME": "Ramp"},
                                    [(-90, 35), (-89, 35)])]
        self.assertEqual(build_highway_features(_FakeShapefileReader(records), self.BBOX), [])

    def test_skipped_when_entirely_outside_bbox(self):
        records = [_FakeShapeRecord({"RTTYP": "I", "FULLNAME": "I-30"},
                                    [(-120, 50), (-119, 50)])]
        self.assertEqual(build_highway_features(_FakeShapefileReader(records), self.BBOX), [])

    def test_multi_part_geometry_yields_multiple_features(self):
        records = [_FakeShapeRecord(
            {"RTTYP": "I", "FULLNAME": "I-30"},
            [(-90, 35), (-89, 35), (-88, 36), (-87, 36)], parts=[0, 2])]
        feats = build_highway_features(_FakeShapefileReader(records), self.BBOX)
        self.assertEqual(len(feats), 2)
        self.assertTrue(all(f["properties"]["route_id"] == "I-30" for f in feats))


class TestBuildLocalFeatures(unittest.TestCase):
    BBOX = (30.0, 40.0, -95.0, -85.0)

    def test_filters_to_street_and_road_mtfcc_only(self):
        records = [
            _FakeShapeRecord({"MTFCC": "S1400", "FULLNAME": "Maple St"},
                            [(-90, 35), (-89, 35)]),
            _FakeShapeRecord({"MTFCC": "S1500", "FULLNAME": "Farm Track"},
                            [(-90, 35), (-89, 35)]),
        ]
        feats = build_local_features(_FakeShapefileReader(records), self.BBOX)
        self.assertEqual(len(feats), 1)
        self.assertEqual(feats[0]["properties"]["route_id"], "Maple St")
        self.assertEqual(feats[0]["properties"]["route_type"], "local")

    def test_skipped_when_name_blank(self):
        records = [_FakeShapeRecord({"MTFCC": "S1200", "FULLNAME": ""},
                                    [(-90, 35), (-89, 35)])]
        self.assertEqual(build_local_features(_FakeShapefileReader(records), self.BBOX), [])

    def test_skipped_when_entirely_outside_bbox(self):
        records = [_FakeShapeRecord({"MTFCC": "S1200", "FULLNAME": "Maple St"},
                                    [(-120, 50), (-119, 50)])]
        self.assertEqual(build_local_features(_FakeShapefileReader(records), self.BBOX), [])

    def test_single_point_part_skipped(self):
        # len(part) < 2 can't form a LineString.
        records = [_FakeShapeRecord({"MTFCC": "S1200", "FULLNAME": "Maple St"},
                                    [(-90, 35)])]
        self.assertEqual(build_local_features(_FakeShapefileReader(records), self.BBOX), [])


if __name__ == "__main__":
    unittest.main()
