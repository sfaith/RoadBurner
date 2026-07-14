# Changelog

All notable changes to RoadBurner are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Added
- README now documents that `clip_folder` can point at a subfolder inside
  the clone, an absolute path, or a network share/mapped drive, and that
  scanning is non-recursive with no date-range or include/exclude
  filtering - organize the clips you want into one folder first.
- `setup.ps1`/`setup.sh` now count and report the `.MP4` files found at
  the `clip_folder` path you enter, instead of only checking that the
  folder exists - a wrong or empty path shows up immediately instead of
  failing later in `extract_gps.py`.

## [0.1.0] - 2026-07-14

### Added
- Road matching against `[local_roads]`/`[roads]` is dramatically faster:
  a spatial grid index over road segments plus a route-id grouping fix
  two O(N) scans that both scaled with the SIZE OF THE ROAD FILE (306K
  segments for local roads) rather than the length of the drive. Verified
  on real data: a 381-point real GPS track against the real
  215MB/306K-segment local-roads file dropped from ~870s to ~8s. A second
  new cache dimension (distance-to-current-road, keyed per GPS point)
  means a re-render after any clip-set change no longer re-evaluates
  points it's already seen. Purely an internal performance change - no
  config keys, no behavior/output change.
- Compass indicator: mirrors the highway-shield/route-label pair on the
  right of the info line - a compass rose plus an 8-point cardinal +
  degree readout (e.g. "SW 225°"). Uses real device-reported GPS heading
  (parsed from the Novatek freeGPS chunk, not a derived bearing), smoothed
  with a circular moving average and frozen while stopped, with its own
  hysteresis on the displayed cardinal label so it doesn't flicker at
  octant boundaries. Off by default (`[compass] enabled = false`). The
  rose itself is a traditional two-tone needle (red half toward heading,
  gray tail half opposite) with fixed N/E/S/W tick labels around the
  ring, anti-aliased via internal supersampling, with an optional dark
  drop-shadow/glow for contrast against bright backgrounds
  (`[compass] needle_color`/`tail_color`/`glow_enabled`/`glow_radius_px`/
  `glow_alpha`/`glow_color`).
- Highway shields are rendered with internal 4x supersampling (downscaled
  with Lanczos resampling) for anti-aliased edges instead of visibly
  jagged curves/borders at typical on-screen sizes, and an optional dark
  drop-shadow/glow behind each shield for contrast against bright
  backgrounds like open sky (`[roads] shield_glow_enabled`/
  `shield_glow_radius_px`/`shield_glow_alpha`/`shield_glow_color`, off by
  default).
- Concurrent highway designations: when two routes run physically
  concurrent (e.g. I-10 and US-70 near Deming, NM), the route label can
  show both ("I-10 / US 70 WB") instead of just the tier-priority winner.
  The shield graphic still shows only the primary route. Off by default
  (`[roads] show_concurrent_designations`/`max_concurrent_designations`).
- Day-title cards: an optional brief title card at the start of each
  local-day driving segment (e.g. "Day 2 - Bristol, VA to Texarkana, TX"),
  auto-derived from the town label at each segment's start/end. Off by
  default (`[day_title] enabled = false`).
- `tools/fetch_tiger_roads.py` - downloads and converts real Census
  TIGER/Line road data into the `roads.geojson`/`local_roads.geojson`
  files `render_overlay.py` expects. States for the highway fetch are
  derived from your own `track.csv` (not hardcoded); local-street
  counties are resolved per-point via the free FCC Census Block API, no
  county-boundary shapefile needed. Public-domain data, no API key.
- Reverse-geocoding no longer mislabels the town as a foreign country's
  nearest indexed city when a GPS point close to an international
  border resolves oddly in the offline dataset (e.g. a real point on
  I-10 in Texas nearest-matching a town across the Mexican border) - the
  last confident US match is carried forward instead of showing the
  incorrect country/town.
- Dimmed leading-zero styling for the info line's speed/dist/remain
  fields - classic digital-odometer look (e.g. speed shows as "007 mph"
  with the leading zeros at reduced brightness, "7" full white).
- Monospace font for the info line (Consolas on Windows; DejaVu Sans
  Mono, then Liberation Mono, on Linux), so the bar's width never drifts
  between digits.
- "TRIP MAP" / "SEGMENT MAP" captions on the map insets
  (`caption`/`caption_font_size`/`caption_color` under `[map_trip]` /
  `[map_day]`).
- Fixed/letterboxed map panel sizing: an optional `height` key under
  `[map_trip]`/`[map_day]` locks the panel to one pixel size for the
  whole render, padding the route's geographic bounding box (not
  stretching the image) to hit the target aspect ratio. Leave unset for
  the previous auto-fit-per-render behavior.
- Fade transition between local-day map segments (`[map_day] fade_secs`,
  default 2s) - the day-map inset now fades to transparent and back at
  each date change instead of hard-cutting.
- Point-level road-match cache: a second, GPS-point-keyed cache layer
  underneath the existing whole-track match cache, so adding, removing,
  or reordering a clip no longer forces a full re-match of every point -
  only genuinely new/changed GPS fixes get re-matched.
- `setup.ps1` / `setup.sh` / `setup.bat` - optional interactive first-run
  wizard: checks Python/ffmpeg prerequisites, installs dependencies, and
  creates `config.ini` from the tracked example template. Supports
  `-DryRun`/`--dry-run` to preview every step without installing or
  writing anything.
- Example screenshots in the README, pulled from a real full-resolution
  render.
- Unit test coverage for `tools/fetch_tiger_roads.py`'s pure functions
  (highway/local-road filtering, bbox padding, route-id normalization).

### Changed
- Speed in the info line is now zero-padded to 3 digits (previously
  unpadded), matching the existing zero-padding on dist/remain.
- Elapsed-mileage field now reads "NNNN mi traveled" (previously
  "NNNN mi"), to read more cohesively alongside "NNNN mi to go".

### Fixed
- Compass cardinal label could stall on the value from before a turn for
  the turn's entire duration, then snap late, when the smoothed heading
  swept continuously through several different octants in a row (each
  one different from the last never let the old repeat-count hysteresis
  fire). Now snaps once the displayed label has genuinely mismatched the
  current heading for long enough, regardless of whether the in-between
  candidates repeat.
- Highway shield and NB/EB/SB/WB direction suffix could flicker rapidly
  while the vehicle was stopped (e.g. cycling through several different
  roads/directions across a few seconds at a real highway junction) -
  both the road match and the direction suffix now freeze at their last
  displayed values below a configurable speed
  (`[roads] freeze_below_mph`, default 3.0 mph) instead of re-evaluating
  every second against inherently noisy stopped-vehicle GPS heading.
- Highway shields could show the wrong route number where two
  designations run physically concurrent (e.g. I-10 and US-70 near
  Deming, NM) - matching now prioritizes by route type (Interstate over
  US route) instead of picking whichever polyline happened to be a hair
  closer.
- Reverse geocoding could crash on Windows with a pagefile/commit-limit
  error even with ample free RAM, due to `reverse_geocoder`'s default
  multiprocessing mode spiking committed virtual memory. Now runs
  single-threaded.
- Map caption text could overlap the bottom-left city-label cluster -
  moved to top-center.
- ffmpeg's subtitles filter could fail to parse the burned-in label
  track when `work_folder` resolved to an absolute Windows path (the
  drive-letter colon collided with the filtergraph's own `:` separator).
  Paths are now escaped before being embedded in the filter string.
- README's test-run instructions referenced `pytest`, which isn't a
  project dependency - corrected to `python -m unittest discover tests`.

---
