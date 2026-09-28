param([switch]$Browser, [switch]$Scale, [switch]$Voice)
$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot '..\..')
$python = '.\backend\.venv\Scripts\python.exe'
function Run-Evaluation([string]$script, [string[]]$extra = @()) {
    & $python "research_results/scripts/$script" @extra
    if ($LASTEXITCODE -ne 0) { throw "Evaluation step failed: $script" }
}
# Existing JSONL files are checkpoints. Preserve them; choose a clean checkout/result
# directory for an independent run. Raw credentials are never command arguments.
Run-Evaluation 'dataset.py'
Run-Evaluation 'audit_and_api.py'
Run-Evaluation 'component_experiments.py'
Run-Evaluation 'profile_backend.py'
Run-Evaluation 'safety_contract_probe.py'
Run-Evaluation 'transitions.py'
if ($Scale) { Run-Evaluation 'scalability.py' }
if ($Browser) {
    Run-Evaluation 'launch_browser.py'
    Run-Evaluation 'launch_browser.py' @('--diverse')
}
if ($Voice) { & "$PSScriptRoot/voice_smoke.ps1" }
Run-Evaluation 'analyse.py'
Run-Evaluation 'report.py'
Run-Evaluation 'graphs.py'
