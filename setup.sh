#!/usr/bin/env bash
# =============================================================================
# setup.sh — RoadBurner First-Run Setup
# =============================================================================
#
# Interactive first-run wizard for Linux/WSL/macOS: checks prerequisites
# (Python, ffmpeg), installs Python dependencies, and creates config.ini
# from the tracked example template. Safe to re-run any time — it only
# touches config.ini and never your footage, work folder, or rendered
# output.
#
# This is a convenience layer only. Every underlying tool still works fine
# invoked directly (see README.md) — nothing here is required.
#
# Steps:
#   1. Prerequisites
#   2. Python dependencies
#   3. Configuration
#
# Usage: ./setup.sh
#        ./setup.sh --dry-run   # walk through every step, print what would
#                                # happen, but don't install packages or
#                                # write config.ini
# =============================================================================

set -euo pipefail

DRY_RUN=false
if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN=true
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

LOG_FILE="${SCRIPT_DIR}/setup.log"
exec > >(tee -a "${LOG_FILE}") 2>&1

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
info()    { echo -e "\n\e[1;36m[INFO]\e[0m  $*"; }
success() { echo -e "\e[1;32m[OK]\e[0m    $*"; }
warn()    { echo -e "\e[1;33m[WARN]\e[0m  $*"; }
error()   { echo -e "\e[1;31m[ERROR]\e[0m $*" >&2; exit 1; }

prompt() {
  local var="$1" label="$2" default="$3" placeholder="${4:-}"
  local input
  echo -en "\n  ${label}"
  if [[ -n "${default}" ]]; then
    echo -en " \e[2m[${default}]\e[0m: "
  elif [[ -n "${placeholder}" ]]; then
    echo -en " \e[2m(e.g. ${placeholder})\e[0m: "
  else
    echo -en ": "
  fi
  read -r input
  if [[ -n "${input}" ]]; then
    printf -v "${var}" '%s' "${input}"
  else
    printf -v "${var}" '%s' "${default}"
  fi
}

confirm() {
  local default="${2:-N}" answer
  local suffix="[y/N]"
  [[ "${default}" == "Y" ]] && suffix="[Y/n]"
  echo -en "\n  $1 ${suffix}: "
  read -r answer
  [[ -z "${answer}" ]] && answer="${default}"
  [[ "${answer,,}" == "y" || "${answer,,}" == "yes" ]]
}

# -----------------------------------------------------------------------------
# Banner
# -----------------------------------------------------------------------------
echo
echo "============================================================"
echo "  RoadBurner — First-Run Setup"
echo "  github.com/sfaith/RoadBurner"
echo "============================================================"
echo
echo "  This will:"
echo "    1. Check prerequisites (Python, ffmpeg)"
echo "    2. Install Python dependencies"
echo "    3. Create config.ini and set your clip folder"
echo
echo "  Setup output is being logged to: ${LOG_FILE}"
if [[ "${DRY_RUN}" == "true" ]]; then
  echo
  echo -e "  \e[1;35m*** DRY RUN MODE - nothing will be installed or written ***\e[0m"
fi

# -----------------------------------------------------------------------------
# Step 1 — Prerequisites
# -----------------------------------------------------------------------------
info "Step 1/3 — Prerequisites"

PYTHON_BIN=""
for candidate in python3 python; do
  if command -v "${candidate}" &>/dev/null; then
    PYTHON_BIN="${candidate}"
    break
  fi
done
if [[ -z "${PYTHON_BIN}" ]]; then
  error "Python not found on PATH. Install Python 3.10+ and re-run setup.sh."
fi

PY_VERSION="$(${PYTHON_BIN} --version 2>&1)"
PY_MAJOR="$(${PYTHON_BIN} -c 'import sys; print(sys.version_info[0])')"
PY_MINOR="$(${PYTHON_BIN} -c 'import sys; print(sys.version_info[1])')"
if [[ "${PY_MAJOR}" -gt 3 || ( "${PY_MAJOR}" -eq 3 && "${PY_MINOR}" -ge 10 ) ]]; then
  success "${PY_VERSION} found ($(command -v "${PYTHON_BIN}"))"
else
  error "${PY_VERSION} found, but RoadBurner needs Python 3.10 or later."
fi

if command -v ffmpeg &>/dev/null && command -v ffprobe &>/dev/null; then
  success "ffmpeg and ffprobe found ($(command -v ffmpeg))"
else
  warn "ffmpeg/ffprobe not found on PATH."
  echo "    Debian/Ubuntu: sudo apt install ffmpeg"
  echo "    macOS (Homebrew): brew install ffmpeg"
  if ! confirm "Continue setup without ffmpeg? (you'll need it before rendering)"; then
    error "Install ffmpeg, then re-run setup.sh."
  fi
fi

# -----------------------------------------------------------------------------
# Step 2 — Python dependencies
# -----------------------------------------------------------------------------
info "Step 2/3 — Python dependencies"

if [[ ! -f "${SCRIPT_DIR}/requirements.txt" ]]; then
  error "requirements.txt not found — run setup.sh from the cloned repo directory."
fi

if confirm "Install/update Python dependencies now (pip install -r requirements.txt)?" "Y"; then
  if [[ "${DRY_RUN}" == "true" ]]; then
    echo "  [DRY RUN] Would run: ${PYTHON_BIN} -m pip install --upgrade pip"
    echo "  [DRY RUN] Would run: ${PYTHON_BIN} -m pip install -r ${SCRIPT_DIR}/requirements.txt"
  else
    "${PYTHON_BIN}" -m pip install --upgrade pip --quiet
    "${PYTHON_BIN}" -m pip install -r "${SCRIPT_DIR}/requirements.txt"
    success "Dependencies installed."
  fi
else
  warn "Skipping dependency install — run 'pip install -r requirements.txt' manually before using RoadBurner."
fi

# -----------------------------------------------------------------------------
# Step 3 — config.ini
# -----------------------------------------------------------------------------
info "Step 3/3 — Configuration"

EXAMPLE_CONFIG="${SCRIPT_DIR}/config.example.ini"
CONFIG="${SCRIPT_DIR}/config.ini"

if [[ ! -f "${EXAMPLE_CONFIG}" ]]; then
  error "config.example.ini not found — run setup.sh from the cloned repo directory."
fi

CONFIG_EXISTS=false
[[ -f "${CONFIG}" ]] && CONFIG_EXISTS=true
WRITE_CLIP_FOLDER=true

if [[ "${CONFIG_EXISTS}" == "true" ]]; then
  warn "config.ini already exists."
  if ! confirm "Update its clip_folder setting? (everything else in config.ini is left alone)"; then
    WRITE_CLIP_FOLDER=false
    echo "    Leaving config.ini untouched."
  fi
elif [[ "${DRY_RUN}" == "true" ]]; then
  echo "  [DRY RUN] Would create config.ini from config.example.ini"
else
  cp "${EXAMPLE_CONFIG}" "${CONFIG}"
  success "Created config.ini from config.example.ini"
fi

if [[ "${WRITE_CLIP_FOLDER}" == "true" ]]; then
  echo
  echo "  RoadBurner needs the folder containing your dashcam's .MP4 clips."
  prompt CLIP_FOLDER "Clip folder" "real_cam" "/mnt/dashcam/2024TripFootage"

  if [[ "${DRY_RUN}" == "true" ]]; then
    echo "  [DRY RUN] Would set clip_folder = ${CLIP_FOLDER} in config.ini"
  else
    # config.ini only exists on disk here if it already existed before this
    # run, or we just created it above - either way it's safe to read/
    # rewrite now. Line-based replace (not sed's own regex substitution on
    # the value) so an arbitrary user path can't be misread as a sed
    # replacement pattern.
    TMP_CONFIG="$(mktemp)"
    while IFS= read -r line; do
      if [[ "${line}" =~ ^[[:space:]]*clip_folder[[:space:]]*= ]]; then
        echo "clip_folder = ${CLIP_FOLDER}"
      else
        echo "${line}"
      fi
    done < "${CONFIG}" > "${TMP_CONFIG}"
    mv "${TMP_CONFIG}" "${CONFIG}"
    success "clip_folder set to: ${CLIP_FOLDER}"
  fi

  if [[ ! -d "${CLIP_FOLDER}" ]]; then
    warn "That folder doesn't exist yet — copy your dashcam clips there before running extract_gps.py."
  fi
fi

# -----------------------------------------------------------------------------
# Done
# -----------------------------------------------------------------------------
echo
echo "============================================================"
echo "  Setup complete."
echo "============================================================"
echo
echo "  Review config.ini for label/map/road/compass settings, then run:"
echo
echo "    ${PYTHON_BIN} extract_gps.py --config config.ini"
echo "    ${PYTHON_BIN} render_overlay.py --config config.ini"
echo
echo "  Optional: real highway/local-road names need Census TIGER data —"
echo "  see the 'Road names' section in README.md for tools/fetch_tiger_roads.py."
echo
echo "  Run tests any time with:"
echo "    ${PYTHON_BIN} -m unittest discover tests"
echo
success "Done."
