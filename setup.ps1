#Requires -Version 5.1
<#
    setup.ps1  |  RoadBurner First-Run Setup

    Interactive first-run wizard: checks prerequisites (Python, ffmpeg),
    installs Python dependencies, and creates config.ini from the tracked
    example template. Safe to re-run any time - it only touches config.ini
    and never your footage, work folder, or rendered output.

    This is a convenience layer only. Every underlying tool still works
    fine invoked directly (see README.md) - nothing here is required.

    Usage: .\setup.ps1
            .\setup.ps1 -DryRun   # walk through every step, print what
                                   # would happen, but don't install
                                   # packages or write config.ini
#>

param(
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

$LogFile = Join-Path $ScriptDir "setup.log"
Start-Transcript -Path $LogFile -Append | Out-Null

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
function Write-Info    { param([string]$Message) Write-Host "`n[INFO]  $Message" -ForegroundColor Cyan }
function Write-Success { param([string]$Message) Write-Host "[OK]    $Message" -ForegroundColor Green }
function Write-Warn    { param([string]$Message) Write-Host "[WARN]  $Message" -ForegroundColor Yellow }
function Write-Fatal   { param([string]$Message) Write-Host "[ERROR] $Message" -ForegroundColor Red; Stop-Transcript | Out-Null; exit 1 }

function Read-Prompt {
    param(
        [Parameter(Mandatory)][string]$Label,
        [string]$Default = "",
        [string]$Placeholder = ""
    )
    if ($Default -ne "") {
        $suffix = " [$Default]"
    } elseif ($Placeholder -ne "") {
        $suffix = " (e.g. $Placeholder)"
    } else {
        $suffix = ""
    }
    $response = Read-Host -Prompt "  $Label$suffix"
    if ([string]::IsNullOrWhiteSpace($response)) { return $Default }
    return $response
}

function Confirm-Action {
    param([Parameter(Mandatory)][string]$Message, [string]$Default = "N")
    $suffix = if ($Default -eq "Y") { "[Y/n]" } else { "[y/N]" }
    $response = Read-Host -Prompt "  $Message $suffix"
    if ([string]::IsNullOrWhiteSpace($response)) { $response = $Default }
    return ($response -match '^(y|yes)$')
}

# -----------------------------------------------------------------------------
# Banner
# -----------------------------------------------------------------------------
Write-Host ""
Write-Host "============================================================"
Write-Host "  RoadBurner - First-Run Setup"
Write-Host "  github.com/sfaith/RoadBurner"
Write-Host "============================================================"
Write-Host ""
Write-Host "  This will:"
Write-Host "    1. Check prerequisites (Python, ffmpeg)"
Write-Host "    2. Install Python dependencies"
Write-Host "    3. Create config.ini and set your clip folder"
Write-Host ""
Write-Host "  Setup output is being logged to: $LogFile"
Write-Host ""
if ($DryRun) {
    Write-Host "  *** DRY RUN MODE - nothing will be installed or written ***" -ForegroundColor Magenta
    Write-Host ""
}

# -----------------------------------------------------------------------------
# Step 1 - Prerequisites
# -----------------------------------------------------------------------------
Write-Info "Step 1/3 - Prerequisites"

$pythonCmd = Get-Command python -ErrorAction SilentlyContinue
if (-not $pythonCmd) {
    Write-Fatal "Python not found on PATH. Install Python 3.10+ from https://python.org (check 'Add python.exe to PATH' during install) and re-run setup.ps1."
}
$versionOutput = (& python --version) 2>&1
if ($versionOutput -match 'Python (\d+)\.(\d+)') {
    $major = [int]$Matches[1]
    $minor = [int]$Matches[2]
    if ($major -gt 3 -or ($major -eq 3 -and $minor -ge 10)) {
        Write-Success "$versionOutput found ($($pythonCmd.Source))"
    } else {
        Write-Fatal "$versionOutput found, but RoadBurner needs Python 3.10 or later."
    }
} else {
    Write-Warn "Could not parse Python version from '$versionOutput' - continuing anyway."
}

$ffmpegCmd = Get-Command ffmpeg -ErrorAction SilentlyContinue
$ffprobeCmd = Get-Command ffprobe -ErrorAction SilentlyContinue
if ($ffmpegCmd -and $ffprobeCmd) {
    Write-Success "ffmpeg and ffprobe found ($($ffmpegCmd.Source))"
} else {
    Write-Warn "ffmpeg/ffprobe not found on PATH."
    Write-Host "    Install with: winget install Gyan.FFmpeg"
    Write-Host "    (or download from https://ffmpeg.org and add its bin\ folder to PATH)"
    if (-not (Confirm-Action "Continue setup without ffmpeg? (you'll need it before rendering)")) {
        Write-Fatal "Install ffmpeg, then re-run setup.ps1."
    }
}

# -----------------------------------------------------------------------------
# Step 2 - Python dependencies
# -----------------------------------------------------------------------------
Write-Info "Step 2/3 - Python dependencies"

$reqFile = Join-Path $ScriptDir "requirements.txt"
if (-not (Test-Path $reqFile)) {
    Write-Fatal "requirements.txt not found - run setup.ps1 from the cloned repo directory."
}

if (Confirm-Action "Install/update Python dependencies now (pip install -r requirements.txt)?" -Default "Y") {
    if ($DryRun) {
        Write-Host "  [DRY RUN] Would run: python -m pip install --upgrade pip"
        Write-Host "  [DRY RUN] Would run: python -m pip install -r $reqFile"
    } else {
        & python -m pip install --upgrade pip --quiet
        & python -m pip install -r $reqFile
        if ($LASTEXITCODE -ne 0) {
            Write-Fatal "pip install failed - see output above."
        }
        Write-Success "Dependencies installed."
    }
} else {
    Write-Warn "Skipping dependency install - run 'pip install -r requirements.txt' manually before using RoadBurner."
}

# -----------------------------------------------------------------------------
# Step 3 - config.ini
# -----------------------------------------------------------------------------
Write-Info "Step 3/3 - Configuration"

$exampleConfig = Join-Path $ScriptDir "config.example.ini"
$config = Join-Path $ScriptDir "config.ini"

if (-not (Test-Path $exampleConfig)) {
    Write-Fatal "config.example.ini not found - run setup.ps1 from the cloned repo directory."
}

$configExists = Test-Path $config
$writeClipFolder = $true

if ($configExists) {
    Write-Warn "config.ini already exists."
    if (-not (Confirm-Action "Update its clip_folder setting? (everything else in config.ini is left alone)")) {
        $writeClipFolder = $false
        Write-Host "    Leaving config.ini untouched."
    }
} elseif ($DryRun) {
    Write-Host "  [DRY RUN] Would create config.ini from config.example.ini"
} else {
    Copy-Item -Path $exampleConfig -Destination $config
    Write-Success "Created config.ini from config.example.ini"
}

if ($writeClipFolder) {
    Write-Host ""
    Write-Host "  RoadBurner needs the folder containing your dashcam's .MP4 clips."
    $clipFolder = Read-Prompt -Label "Clip folder" -Default "real_cam" -Placeholder "D:\Dashcam\2024TripFootage"

    if ($DryRun) {
        Write-Host "  [DRY RUN] Would set clip_folder = $clipFolder in config.ini"
    } else {
        # config.ini only exists on disk here if it already existed before
        # this run, or we just created it above - either way it's safe to
        # read/rewrite now.
        $lines = Get-Content -Path $config
        $newLines = foreach ($line in $lines) {
            if ($line -match '^\s*clip_folder\s*=') {
                "clip_folder = $clipFolder"
            } else {
                $line
            }
        }
        Set-Content -Path $config -Value $newLines
        Write-Success "clip_folder set to: $clipFolder"
    }

    if (-not (Test-Path $clipFolder)) {
        Write-Warn "That folder doesn't exist yet - copy your dashcam clips there before running extract_gps.py."
    } else {
        # Non-recursive, case-insensitive .MP4 count - mirrors exactly what
        # extract_gps.py itself will scan, so a typo'd path or an empty/
        # wrong folder shows up here instead of failing later.
        $clipCount = (Get-ChildItem -Path $clipFolder -File -ErrorAction SilentlyContinue |
            Where-Object { $_.Extension -ieq ".mp4" }).Count
        if ($clipCount -gt 0) {
            Write-Success "Found $clipCount .MP4 file(s) in that folder."
        } else {
            Write-Warn "No .MP4 files found directly in that folder. extract_gps.py doesn't look in subfolders - check the path and that your clips are copied there."
        }
    }
}

# -----------------------------------------------------------------------------
# Done
# -----------------------------------------------------------------------------
Write-Host ""
Write-Host "============================================================"
Write-Host "  Setup complete."
Write-Host "============================================================"
Write-Host ""
Write-Host "  Review config.ini for label/map/road/compass settings, then run:"
Write-Host ""
Write-Host "    python extract_gps.py --config config.ini"
Write-Host "    python render_overlay.py --config config.ini"
Write-Host ""
Write-Host "  Optional: real highway/local-road names need Census TIGER data -"
Write-Host "  see the 'Road names' section in README.md for"
Write-Host "  tools\fetch_tiger_roads.py."
Write-Host ""
Write-Host "  Run tests any time with:"
Write-Host "    python -m unittest discover tests"
Write-Host ""

Stop-Transcript | Out-Null
