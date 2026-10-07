param(
    [ValidateRange(1, 65535)]
    [int]$Port = 8000,
    [switch]$NoBrowser,
    [switch]$SkipInstall
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$VenvRoot = Join-Path $ProjectRoot ".venv"
$PythonPath = Join-Path $VenvRoot "Scripts\python.exe"
$RunDirectory = Join-Path $ProjectRoot "run"
$LogDirectory = Join-Path $ProjectRoot "logs\runtime"
$ApiPidFile = Join-Path $RunDirectory "api.pid"
$WorkerPidFile = Join-Path $RunDirectory "worker.pid"
$TrayPidFile = Join-Path $RunDirectory "tray.pid"
$TrayScript = Join-Path $PSScriptRoot "tray_world.ps1"
$ViewerScript = Join-Path $PSScriptRoot "watch_runtime_logs.ps1"
$WorkerDebugFile = Join-Path $RunDirectory "worker-debug.json"
$ApiStdout = Join-Path $LogDirectory "api.stdout.log"
$ApiStderr = Join-Path $LogDirectory "api.stderr.log"
$WorkerStdout = Join-Path $LogDirectory "worker.stdout.log"
$WorkerStderr = Join-Path $LogDirectory "worker.stderr.log"
$UiUrl = "http://127.0.0.1:$Port/"

function Write-PidFile {
    param([string]$Path, [int]$ProcessId)
    [System.IO.Directory]::CreateDirectory((Split-Path -Parent $Path)) | Out-Null
    [System.IO.File]::WriteAllText(
        $Path,
        [string]$ProcessId,
        [System.Text.UTF8Encoding]::new($false)
    )
}

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

function Test-ApiReady {
    try {
        $health = Invoke-RestMethod `
            -Uri "http://127.0.0.1:$Port/api/health" `
            -Method Get `
            -TimeoutSec 2
        return $health.service -eq "virtual-world-core" -and $health.status -eq "ok"
    }
    catch {
        return $false
    }
}

function Get-LogTail {
    param([string]$Path)
    if (Test-Path -LiteralPath $Path) {
        return (Get-Content -LiteralPath $Path -Tail 20 -ErrorAction SilentlyContinue) -join "`n"
    }
    return "No error log was created."
}

function Test-TrayRunning {
    if (-not (Test-Path -LiteralPath $TrayPidFile)) { return $false }
    $trayId = 0
    $storedId = [System.IO.File]::ReadAllText($TrayPidFile).Trim()
    if (-not [int]::TryParse($storedId, [ref]$trayId)) { return $false }
    $trayProcess = Get-CimInstance Win32_Process -Filter "ProcessId = $trayId" -ErrorAction SilentlyContinue
    return $null -ne $trayProcess -and $trayProcess.CommandLine -and
        $trayProcess.CommandLine.IndexOf($TrayScript, [System.StringComparison]::OrdinalIgnoreCase) -ge 0
}

Set-Location -LiteralPath $ProjectRoot
[System.IO.Directory]::CreateDirectory($RunDirectory) | Out-Null
[System.IO.Directory]::CreateDirectory($LogDirectory) | Out-Null

if (-not (Test-Path -LiteralPath $PythonPath)) {
    $systemPython = Get-Command python -ErrorAction SilentlyContinue
    if (-not $systemPython) {
        throw "Python was not found. Install Python 3.11 or newer first."
    }
    Write-Host "Creating the project virtual environment..."
    & $systemPython.Source -m venv $VenvRoot
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to create the virtual environment."
    }
}

if (-not $SkipInstall) {
    & $PythonPath -c "import fastapi, uvicorn, world_engine" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Installing project dependencies..."
        & $PythonPath -m pip install -e $ProjectRoot
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to install project dependencies."
        }
    }
}

$apiProcess = $null
if (Test-ApiReady) {
    Write-Host "API is already running on port $Port."
}
else {
    $existingApi = @(Get-ProjectProcesses "world_engine.api:app")
    if ($existingApi.Count -gt 0) {
        throw "A project API process exists but port $Port is not healthy. Check logs/runtime."
    }
    Write-Host "Starting the API..."
    $hadDebugSetting = Test-Path Env:\WORLD_DEBUG_MODEL_IO
    $previousDebugSetting = [Environment]::GetEnvironmentVariable("WORLD_DEBUG_MODEL_IO", "Process")
    try {
        $env:WORLD_DEBUG_MODEL_IO = "1"
        $apiProcess = Start-Process `
            -FilePath $PythonPath `
            -ArgumentList @(
                "-m", "uvicorn", "world_engine.api:app",
                "--host", "127.0.0.1", "--port", [string]$Port
            ) `
            -WorkingDirectory $ProjectRoot `
            -WindowStyle Hidden `
            -RedirectStandardOutput $ApiStdout `
            -RedirectStandardError $ApiStderr `
            -PassThru
    }
    finally {
        if ($hadDebugSetting) {
            $env:WORLD_DEBUG_MODEL_IO = $previousDebugSetting
        }
        else {
            Remove-Item Env:\WORLD_DEBUG_MODEL_IO -ErrorAction SilentlyContinue
        }
    }
    Write-PidFile -Path $ApiPidFile -ProcessId $apiProcess.Id

    $ready = $false
    for ($attempt = 0; $attempt -lt 60; $attempt++) {
        if ($apiProcess.HasExited) {
            break
        }
        if (Test-ApiReady) {
            $ready = $true
            break
        }
        Start-Sleep -Milliseconds 500
    }
    if (-not $ready) {
        $errorTail = Get-LogTail $ApiStderr
        throw "API startup failed.`n$errorTail"
    }
}

$existingWorkers = @(Get-ProjectProcesses "world_engine.worker")
if ($existingWorkers.Count -gt 0) {
    Write-Host "Worker is already running."
    Write-PidFile -Path $WorkerPidFile -ProcessId $existingWorkers[0].ProcessId
    $workerDebugEnabled = $null
    if (Test-Path -LiteralPath $WorkerDebugFile) {
        try {
            $workerDebugRecord = Get-Content -LiteralPath $WorkerDebugFile -Raw | ConvertFrom-Json
            if ([int]$workerDebugRecord.process_id -eq [int]$existingWorkers[0].ProcessId) {
                $workerDebugEnabled = [bool]$workerDebugRecord.debug_model_io
            }
        }
        catch {
            $workerDebugEnabled = $null
        }
    }
    if ($null -eq $workerDebugEnabled) {
        Write-Warning "Worker 已运行，但无法确认它是否启用了完整模型调试；请停止后重新启动。"
    }
    elseif (-not $workerDebugEnabled) {
        Write-Warning "Worker 未启用完整模型调试；请停止后重新启动。"
    }
}
else {
    Write-Host "Starting the world worker..."
    $hadDebugSetting = Test-Path Env:\WORLD_DEBUG_MODEL_IO
    $previousDebugSetting = [Environment]::GetEnvironmentVariable("WORLD_DEBUG_MODEL_IO", "Process")
    try {
        $env:WORLD_DEBUG_MODEL_IO = "1"
        $workerProcess = Start-Process `
            -FilePath $PythonPath `
            -ArgumentList @("-m", "world_engine.worker") `
            -WorkingDirectory $ProjectRoot `
            -WindowStyle Hidden `
            -RedirectStandardOutput $WorkerStdout `
            -RedirectStandardError $WorkerStderr `
            -PassThru
    }
    finally {
        if ($hadDebugSetting) {
            $env:WORLD_DEBUG_MODEL_IO = $previousDebugSetting
        }
        else {
            Remove-Item Env:\WORLD_DEBUG_MODEL_IO -ErrorAction SilentlyContinue
        }
    }
    Write-PidFile -Path $WorkerPidFile -ProcessId $workerProcess.Id
    Start-Sleep -Milliseconds 800
    if ($workerProcess.HasExited) {
        $errorTail = Get-LogTail $WorkerStderr
        throw "Worker startup failed.`n$errorTail"
    }
    $workerDebugRecord = @{
        process_id = $workerProcess.Id
        debug_model_io = $true
    } | ConvertTo-Json -Compress
    [System.IO.File]::WriteAllText(
        $WorkerDebugFile,
        $workerDebugRecord,
        [System.Text.UTF8Encoding]::new($false)
    )
}

$apiDebugEnabled = $false
try {
    $apiHealth = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/health" -Method Get -TimeoutSec 2
    $apiHasDebugFlag = @($apiHealth.PSObject.Properties.Name) -contains "debug_model_io"
    $apiDebugEnabled = $apiHasDebugFlag -and [bool]$apiHealth.debug_model_io
    if ($apiDebugEnabled) {
        Write-Host "API model prompt and response debugging: enabled."
    }
    elseif (-not $apiHasDebugFlag) {
        Write-Warning "API debug status unavailable; restart it to enable full prompt logging."
    }
    else {
        Write-Warning "API debug mode is off; restart the service to log prompts and responses."
    }
}
catch {
    Write-Warning "Could not read API debug status:$($_.Exception.Message)"
}

Write-Host "Virtual World is ready: $UiUrl"
Write-Host "Runtime logs: $LogDirectory"

if (Test-TrayRunning) {
    Write-Host "Tray icon is already running."
}
else {
    $trayProcess = Start-Process `
        -FilePath "powershell.exe" `
        -ArgumentList @(
            "-NoProfile", "-ExecutionPolicy", "Bypass", "-Sta",
            "-File", "`"$TrayScript`"", "-Port", [string]$Port
        ) `
        -WorkingDirectory $ProjectRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $LogDirectory "tray.stdout.log") `
        -RedirectStandardError (Join-Path $LogDirectory "tray.stderr.log") `
        -PassThru
    $trayReady = $false
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        if ($trayProcess.HasExited) { break }
        if (Test-TrayRunning) {
            $trayReady = $true
            break
        }
        Start-Sleep -Milliseconds 100
    }
    if (-not $trayReady -and -not (Test-TrayRunning)) {
        Write-Warning 'Tray icon could not start. See logs/runtime/tray.stderr.log.'
    }
}

if (-not $NoBrowser) {
    Start-Process $UiUrl
}

& $ViewerScript -LogDirectory $LogDirectory -UiUrl $UiUrl
