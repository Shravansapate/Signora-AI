param(
    [ValidateSet('start', 'stop', 'status')][string]$Action = 'status',
    [string]$DataRoot = 'D:\SignoraData'
)

# Local interactive development only. No services, firewall rules or remote listeners.
$ErrorActionPreference = 'Stop'
$backendRoot = Split-Path $PSScriptRoot -Parent
$repositoryRoot = Split-Path $backendRoot -Parent
$frontendRoot = Join-Path $repositoryRoot 'frontend'
$pgCtl = Join-Path $backendRoot 'artifacts\postgres18\bin\pg_ctl.exe'
$clusterRoot = Join-Path $DataRoot 'postgres'
$statePath = Join-Path $DataRoot 'local-processes.json'
$logRoot = Join-Path $DataRoot 'logs'
if (-not (Test-Path -LiteralPath (Join-Path $clusterRoot 'PG_VERSION'))) {
    throw "The local PostgreSQL cluster has not been provisioned at $clusterRoot."
}
if (-not (Test-Path -LiteralPath (Join-Path $backendRoot '.env'))) {
    throw 'The backend local configuration is missing.'
}
$guard = [System.IO.File]::Open((Join-Path $DataRoot 'local-processes.lock'), 'OpenOrCreate', 'ReadWrite', 'None')
$states = @{}
if (Test-Path -LiteralPath $statePath) {
    $saved = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
    foreach ($property in $saved.PSObject.Properties) { $states[$property.Name] = $property.Value }
}

function Save-State {
    $temporary = $statePath + '.tmp'
    $states | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $temporary -Encoding UTF8
    Move-Item -LiteralPath $temporary -Destination $statePath -Force
}

function Find-OwnedProcess([string]$Name) {
    $record = $states[$Name]
    if (-not $record) { return $null }
    $process = Get-Process -Id $record.pid -ErrorAction SilentlyContinue
    if ($process -and $process.Path -eq $record.executable -and
        $process.StartTime.ToUniversalTime().Ticks.ToString() -eq $record.started_ticks) {
        return $process
    }
    return $null
}

function Wait-Http([string]$Url) {
    for ($attempt = 0; $attempt -lt 45; $attempt++) {
        try {
            $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 2
            if ($response.StatusCode -eq 200) { return }
        } catch { }
        Start-Sleep -Seconds 1
    }
    throw "Not ready at $Url. Inspect $logRoot; started processes are retained for diagnosis."
}

function Start-Component([string]$Name, [string]$Executable, [string[]]$Arguments,
                         [string]$Directory, [int]$Port = 0) {
    if (Find-OwnedProcess $Name) { Write-Output "$Name already running"; return }
    if ($Port -and (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)) {
        throw "Port $Port belongs to another process; it will not be stopped."
    }
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
    $process = Start-Process -FilePath $Executable -ArgumentList $Arguments -WorkingDirectory $Directory `
        -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $logRoot "$Name-$stamp.out.log") `
        -RedirectStandardError (Join-Path $logRoot "$Name-$stamp.err.log")
    Start-Sleep -Milliseconds 400
    $process.Refresh()
    if ($process.HasExited) { throw "$Name exited. Inspect $logRoot." }
    $states[$Name] = @{
        pid = $process.Id
        executable = $process.Path
        started_ticks = $process.StartTime.ToUniversalTime().Ticks.ToString()
    }
    Save-State
    Write-Output "$Name started (PID $($process.Id))"
}

try {
    if ($Action -eq 'start') {
        if (-not (Test-Path -LiteralPath (Join-Path $frontendRoot '.next\BUILD_ID'))) {
            throw 'Build the frontend first: cd frontend; npm run build'
        }
        & $pgCtl -D $clusterRoot status *> $null
        if ($LASTEXITCODE -ne 0) {
            & $pgCtl -D $clusterRoot -l (Join-Path $logRoot 'postgres.log') -w start
            if ($LASTEXITCODE -ne 0) { throw 'PostgreSQL did not start.' }
        }
        $python = Join-Path $backendRoot '.venv\Scripts\python.exe'
        Start-Component 'backend' $python @('-m', 'uvicorn', 'app.main:create_app', '--factory',
            '--host', '127.0.0.1', '--port', '8000', '--ws-max-size', '4096') $backendRoot 8000
        Wait-Http 'http://127.0.0.1:8000/health/ready'
        Start-Component 'worker' $python @('-m', 'app.worker', '--workers', '2') $backendRoot
        $node = (Get-Command node.exe -ErrorAction Stop).Source
        $next = '"' + (Join-Path $frontendRoot 'node_modules\next\dist\bin\next') + '"'
        Start-Component 'frontend' $node @($next, 'start', '--hostname', '127.0.0.1', '--port', '3000') $frontendRoot 3000
        Wait-Http 'http://127.0.0.1:3000'
        Write-Output 'Ready: http://127.0.0.1:3000 (content), /admin, /announcements, /display'
    } elseif ($Action -eq 'stop') {
        foreach ($name in @('frontend', 'worker', 'backend')) {
            $process = Find-OwnedProcess $name
            if ($process) {
                # Only recorded PID + executable + creation-time matches are eligible.
                & taskkill.exe /PID $process.Id /T /F | Out-Null
                if ($LASTEXITCODE -ne 0) { throw "Could not stop $name; database retained." }
                $process.WaitForExit(10000) | Out-Null
                Write-Output "$name stopped"
            }
            $states.Remove($name)
            Save-State
        }
        & $pgCtl -D $clusterRoot status *> $null
        if ($LASTEXITCODE -eq 0) {
            & $pgCtl -D $clusterRoot -m fast -w stop
            if ($LASTEXITCODE -ne 0) { throw 'PostgreSQL did not stop.' }
        }
    } else {
        & $pgCtl -D $clusterRoot status
        foreach ($name in @('backend', 'worker', 'frontend')) {
            $process = Find-OwnedProcess $name
            if ($process) { Write-Output "$name running (PID $($process.Id))" }
            else { Write-Output "$name stopped" }
        }
    }
} finally {
    $guard.Dispose()
}
