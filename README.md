# DashCam GPS Overlay

Turns raw dashcam footage into a single video with a burned-in GPS overlay:
town/state label, speed/distance/local-time info line, a live route map
inset, and (optionally) highway shields and local road names as you drive.

Built around one real use case - a ~2000mi cross-country road trip - but
the pipeline itself is generic: point it at any folder of dashcam clips
that record Novatek-style `freeGPS` NMEA data and it will extract a full
route and render an overlay video.

## How it works

Two stages, run in order:

1. **`extract_gps.py`** - scans every `.MP4` in your clip folder, pulls the
   embedded GPS chunks, reverse-geocodes each point to a town/state
   (offline, via `reverse_geocoder`), and writes intermediate files to your
   work folder: `track.csv` (the full route), `labels.csv` (deduped
   town-label spans), `concat.txt` (ffmpeg concat list, in clip order),
   `gaps.csv` (spans with zero GPS signal - camera keeps recording, GPS
   just didn't lock), and `duration_sec`.
2. **`render_overlay.py`** - builds a subtitle track and map-inset frames
   from those intermediate files, then does a single ffmpeg pass to
   concatenate every clip and burn in the overlay.

Clips with no GPS signal at all (tunnels, parking garages, cold start
before satellite lock) are never dropped - the video keeps that footage
and shows a configurable "no GPS lock" indicator instead of guessing.

## Requirements

- Python 3.10+
- [ffmpeg / ffprobe](https://ffmpeg.org/) on your `PATH`
- Python packages: `pip install -r requirements.txt`
  (`reverse_geocoder`, `matplotlib`, `Pillow`)

## Setup

```
git clone <this repo>
cd DashCam
pip install -r requirements.txt
copy config.example.ini config.ini      # PowerShell: Copy-Item
```

Edit `config.ini` - never the `.py` scripts - to point `clip_folder` at
your dashcam footage and adjust label/info/map settings. `config.ini` is
gitignored so your personal paths and settings never get committed.

## Running

```
python extract_gps.py --config config.ini
python render_overlay.py --config config.ini
```

Set `preview_scale` under `[video]` (e.g. `960x540`) for fast low-res test
renders before committing to a full-resolution run.

## Road names (optional)

`[roads]` (highway shields) and `[local_roads]` (local street names, e.g.
"Maple Street WB") are both disabled by default. They need real Census
TIGER road data, which isn't shipped in this repo - the two `.geojson`
fixtures under `map_data/synthetic_*` are hand-built test data for the
unit tests only, not real roads, and are not suitable for a real render.

To use real road data:

1. Download [Census TIGER/Line](https://www.census.gov/geographies/mapping-files/time-series/geo/tiger-line-file.html)
   data for the states/counties your route covers:
   - Highways: **Primary Roads** (one national file, filter to `RTTYP`
     `I` or `U` for Interstates/US routes) → convert to
     `map_data/roads.geojson`.
   - Local streets: **All Roads** (shipped per-county, not per-state) →
     convert to `map_data/local_roads.geojson`.
2. Set `enabled = true` under `[roads]` and/or `[local_roads]` in
   `config.ini` and point `roads_file`/`roads_file` at your converted
   files.

A helper script to automate this (deriving the needed states/counties
from your own extracted `track.csv` and doing the TIGER download +
GeoJSON conversion) is planned but not yet built - see open items in
`CLAUDE.md` (gitignored, local dev notes only).

## Running tests

```
python -m pytest tests/ -q
```

Tests cover the pure logic (GPS parsing, road matching, span/gap
handling, label formatting) with synthetic fixtures - no real footage or
real road data required.

## Project layout

```
extract_gps.py       Stage 1: GPS extraction + reverse geocoding
render_overlay.py     Stage 2: overlay rendering
config.example.ini    Template - copy to config.ini and edit
tests/                 Unit tests (synthetic fixtures only)
map_data/              Borders/cities reference data + synthetic test fixtures
                        (real roads.geojson/local_roads.geojson are gitignored)
```

Your own footage, work folders, and rendered output (`real_cam/`, `work*/`,
`trip_samples/`, etc.) are gitignored - see `.gitignore`.

## License

MIT - see `LICENSE`.
