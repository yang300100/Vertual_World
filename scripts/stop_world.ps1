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
    # Start-Process 在部分 Windows 会留下同命令的父进程；只结束监听子进程会
    # 让父进程再次拉起它。沿父链找到同一项目的根进程后按树停止。
    $allProcesses = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)
    $roots = @{}
    foreach ($processInfo in $processes) {
        $root = $processInfo
        while ($root.ParentProcessId) {
            $parent = @($allProcesses | Where-Object {
                $_.ProcessId -eq $root.ParentProcessId -and
                $_.CommandLine -and $_.CommandLine.Contains($CommandNeedle)
            }) | Select-Object -First 1
            if (-not $parent) { break }
            $root = $parent
        }
        $roots[[int]$root.ProcessId] = $root
    }
    foreach ($processInfo in $roots.Values) {
        $processId = [int]$processInfo.ProcessId
        Write-Host "Stopping $Label process tree $processId..."
        & taskkill.exe /PID $processId /T /F 2>$null | Out-Null
    }
    return $roots.Count
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
