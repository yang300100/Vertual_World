param()

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$VenvRoot = Join-Path $ProjectRoot ".venv"
$RunDirectory = Join-Path $ProjectRoot "run"

function Get-ProjectProcesses {
    param([string]$CommandNeedle)
    $venvPrefix = $VenvRoot.TrimEnd("\") + "\"
    @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.CommandLine -and
        $_.CommandLine.Contains($CommandNeedle) -and
        $_.ExecutablePath -and
        $_.ExecutablePath.StartsWith(
            $venvPrefix,
            [System.StringComparison]::OrdinalIgnoreCase
        )
    })
}

function Stop-ProjectProcesses {
    param([string]$CommandNeedle, [string]$Label)
    $processes = @(Get-ProjectProcesses $CommandNeedle)
    foreach ($processInfo in $processes) {
        $processId = [int]$processInfo.ProcessId
        Write-Host "Stopping $Label process $processId..."
        Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
    }
    return $processes.Count
}

$workerCount = Stop-ProjectProcesses "world_engine.worker" "worker"
$apiCount = Stop-ProjectProcesses "world_engine.api:app" "API"

foreach ($pidName in @("api.pid", "worker.pid")) {
    $pidPath = Join-Path $RunDirectory $pidName
    if (Test-Path -LiteralPath $pidPath) {
        Remove-Item -LiteralPath $pidPath -Force
    }
}

if (($workerCount + $apiCount) -eq 0) {
    Write-Host "Virtual World was not running."
}
else {
    Write-Host "Virtual World has stopped."
}
