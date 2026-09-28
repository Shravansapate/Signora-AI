# run_batch4.ps1
# ============================================================
# Batch 4 pipeline runner — drops all MP4s from input\BATCH_4
# into the standard video2glb pipeline and writes outputs to
# output\<GLOSS_NAME>\ for each sign.
# ============================================================
#
# USAGE
#   .\run_batch4.ps1
#   .\run_batch4.ps1 -Workers 2        # parallel jobs
#   .\run_batch4.ps1 -SaveDebug        # force debug video
#   .\run_batch4.ps1 -SingleFile ".\input\BATCH_4\A.mp4"  # one file only

param(
    [string]$SingleFile = "",
    [int]$Workers       = 1,
    [switch]$SaveDebug
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$python  = ".\.venv\Scripts\python.exe"
$script  = "convert.py"
$inputDir = ".\input\BATCH_4"

if (-not (Test-Path $python)) {
    Write-Error "Virtual environment not found. Run:  python -m venv .venv && .\.venv\Scripts\pip install -r requirements.txt"
    exit 1
}

$debugFlag = if ($SaveDebug) { "--save-debug" } else { "" }

if ($SingleFile -ne "") {
    # ---- Single file mode ----
    Write-Host "=== Processing single file: $SingleFile ===" -ForegroundColor Cyan
    $cmd = @($python, $script, "--video", $SingleFile)
    if ($debugFlag) { $cmd += $debugFlag }
    & $cmd[0] $cmd[1..($cmd.Length-1)]
    exit $LASTEXITCODE
}

# ---- Batch mode ----
$mp4s = Get-ChildItem -Path $inputDir -Filter "*.mp4" | Select-Object -ExpandProperty Name

if ($mp4s.Count -eq 0) {
    Write-Warning "No .mp4 files found in $inputDir — copy your videos there first."
    exit 0
}

Write-Host ""
Write-Host "=== Batch 4 Pipeline (Alphabet A-Z) ===" -ForegroundColor Cyan
Write-Host "Input folder : $inputDir"
Write-Host "Videos found : $($mp4s.Count)"
Write-Host "Workers      : $Workers"
Write-Host ""

$cmd = @($python, $script,
    "--batch",
    "--input-dir", $inputDir,
    "--batch-workers", $Workers
)
if ($debugFlag) { $cmd += $debugFlag }

Write-Host "Running: $cmd" -ForegroundColor Yellow
& $cmd[0] $cmd[1..($cmd.Length-1)]
exit $LASTEXITCODE
