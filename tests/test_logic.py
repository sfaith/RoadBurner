"""Logic tests for the pure functions in extract_gps.py and render_overlay.py.

Run from the project root:  python -m pytest tests/ -q
(or: python -m unittest discover tests)
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
                         nmea_to_decimal, parse_freegps, _haversine_miles)
from render_overlay import (ass_time, build_info_spans, dates_per_second,
                            day_segments, format_local_date, format_local_time,
                            haversine_miles, heading_to_cardinal, headings_per_second,
                            hex_to_ass_color, info_text_per_second, layout_city_labels,
                            load_gaps, load_roads, local_date_for, merge_road_matches,
                            nearest_road, no_gps_seconds, overlay_xy,
                            point_to_polyline_miles, positions_per_second,
                            render_info_frames, render_shield, roads_per_second,
                            route_label, select_cities, shield_alpha_per_second,
                            split_spans_for_gaps, tz_name_for, _bezier_points,
                            _shield_outline)


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
        cfg.read_dict({"info": {"format": "{speed:.0f} mph {dist:.0f} mi"}})
        rows = [
            {"valid": "1", "global_sec": "0", "lat": "35.0", "lon": "-90.0",
             "state": "", "timestamp_utc": "2022-05-23 12:00:00", "speed_mph": "60"},
            {"valid": "1", "global_sec": "2", "lat": "35.1", "lon": "-90.0",
             "state": "", "timestamp_utc": "2022-05-23 12:00:02", "speed_mph": "65"},
        ]
        texts = info_text_per_second(rows, cfg, 4)
        self.assertEqual(texts[0], "60 mph 0 mi")
        self.assertEqual(texts[1], "60 mph 0 mi")  # forward-filled, no row at sec 1
        self.assertTrue(texts[2].startswith("65 mph"))
        self.assertEqual(texts[3], texts[2])  # forward-filled past the end

    def test_empty_rows_raises(self):
        cfg = configparser.ConfigParser()
        with self.assertRaises(ValueError):
            info_text_per_second([], cfg, 3)


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


class TestRenderInfoFrames(unittest.TestCase):
    def test_smoke_writes_one_png_per_second_with_shield_slot(self):
        cfg = configparser.ConfigParser()
        cfg.read_dict({"roads": {"shield_height_px": "40",
                                 "text_zone_width_px": "300", "shield_gap_px": "8",
                                 "route_label_width_px": "100", "route_label_gap_px": "4"},
                       "info": {"font_size": "20"}})
        shields = {"I-30": render_shield("I-30", "interstate", 40)}
        texts = ["70 mph 10 mi", "70 mph 11 mi", "70 mph 12 mi"]
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
            render_info_frames(["70 mph"], [(None, None)], {}, cfg, out_dir,
                               video_width_px=960, frame_offset=50)
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
        texts = ["70 mph 10 mi"]
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
            render_info_frames(["70 mph"], [(None, None)], {}, cfg, base_dir,
                               video_width_px=960)
            baseline_alpha = sum(
                Image.open(base_dir / "000000.png").getchannel("A").tobytes())

            local_dir = Path(tmp) / "local"
            render_info_frames(["70 mph"], [("Maple Street", "W")], {}, cfg,
                               local_dir, video_width_px=960)
            local_alpha = sum(
                Image.open(local_dir / "000000.png").getchannel("A").tobytes())

        self.assertGreater(local_alpha, baseline_alpha)


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


if __name__ == "__main__":
    unittest.main()
