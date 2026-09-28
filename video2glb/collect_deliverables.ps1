# collect_deliverables.ps1
# ============================================================
# Collects all .glb and .metadata.json files from the output
# directory into a single delivery folder for easy handoff.
# ============================================================
#
# USAGE
#   .\collect_deliverables.ps1                          # default: output\delivery_<timestamp>
#   .\collect_deliverables.ps1 -DestDir "D:\my_delivery"  # custom destination
#   .\collect_deliverables.ps1 -Copy                    # copy instead of hardlink

param(
    [string]$OutputDir = ".\output",
    [string]$DestDir   = "",
    [switch]$Copy
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# ---- Resolve paths ----
$OutputDir = Resolve-Path $OutputDir

if ($DestDir -eq "") {
    $timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
    $DestDir = Join-Path $OutputDir "delivery_$timestamp"
}

$glbDir  = Join-Path $DestDir "glb"
$metaDir = Join-Path $DestDir "metadata"

New-Item -ItemType Directory -Force -Path $glbDir  | Out-Null
New-Item -ItemType Directory -Force -Path $metaDir  | Out-Null

Write-Host ""
Write-Host "=== Collect Deliverables ===" -ForegroundColor Cyan
Write-Host "Source : $OutputDir"
Write-Host "Dest   : $DestDir"
Write-Host "Mode   : $(if ($Copy) {'Copy'} else {'Hardlink (use -Copy to copy instead)'})"
Write-Host ""

# ---- Scan output subfolders ----
$folders = Get-ChildItem -Path $OutputDir -Directory |
    Where-Object { $_.Name -notmatch "^(\.batch_control|batch_runs|batch_runner_tests|review|delivery_|review_full_tests|review_integration_tests|BATCH_)" }

$glbCount    = 0
$metaCount   = 0
$skipCount   = 0
$errors      = @()

foreach ($folder in $folders) {
    $glbFiles  = Get-ChildItem -Path $folder.FullName -Filter "*.glb" -File 2>$null
    $metaFiles = Get-ChildItem -Path $folder.FullName -Filter "*.metadata.json" -File 2>$null

    if (-not $glbFiles -and -not $metaFiles) {
        $skipCount++
        continue
    }

    foreach ($glb in $glbFiles) {
        $destPath = Join-Path $glbDir $glb.Name
        try {
            if ($Copy) {
                Copy-Item -Path $glb.FullName -Destination $destPath -Force
            } else {
                if (Test-Path $destPath) { Remove-Item $destPath -Force }
                New-Item -ItemType HardLink -Path $destPath -Target $glb.FullName | Out-Null
            }
            $glbCount++
        } catch {
            # Hardlink may fail across drives; fall back to copy
            try {
                Copy-Item -Path $glb.FullName -Destination $destPath -Force
                $glbCount++
            } catch {
                $errors += "FAIL: $($glb.FullName) -> $_"
            }
        }
    }

    foreach ($meta in $metaFiles) {
        $destPath = Join-Path $metaDir $meta.Name
        try {
            if ($Copy) {
                Copy-Item -Path $meta.FullName -Destination $destPath -Force
            } else {
                if (Test-Path $destPath) { Remove-Item $destPath -Force }
                New-Item -ItemType HardLink -Path $destPath -Target $meta.FullName | Out-Null
            }
            $metaCount++
        } catch {
            try {
                Copy-Item -Path $meta.FullName -Destination $destPath -Force
                $metaCount++
            } catch {
                $errors += "FAIL: $($meta.FullName) -> $_"
            }
        }
    }
}

# ---- Summary ----
Write-Host ""
Write-Host "=== Collection Complete ===" -ForegroundColor Green
Write-Host "GLB files      : $glbCount  -> $glbDir"
Write-Host "Metadata files : $metaCount -> $metaDir"
Write-Host "Skipped folders: $skipCount (no .glb or .metadata.json)"

if ($errors.Count -gt 0) {
    Write-Host ""
    Write-Host "=== Errors ===" -ForegroundColor Red
    foreach ($err in $errors) {
        Write-Host "  $err" -ForegroundColor Red
    }
    exit 1
}

Write-Host ""
Write-Host "Delivery folder: $DestDir" -ForegroundColor Yellow
exit 0
