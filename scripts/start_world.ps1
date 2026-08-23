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
}
else {
    Write-Host "Starting the world worker..."
    $workerProcess = Start-Process `
        -FilePath $PythonPath `
        -ArgumentList @("-m", "world_engine.worker") `
        -WorkingDirectory $ProjectRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $WorkerStdout `
        -RedirectStandardError $WorkerStderr `
        -PassThru
    Write-PidFile -Path $WorkerPidFile -ProcessId $workerProcess.Id
    Start-Sleep -Milliseconds 800
    if ($workerProcess.HasExited) {
        $errorTail = Get-LogTail $WorkerStderr
        throw "Worker startup failed.`n$errorTail"
    }
}

Write-Host "Virtual World is ready: $UiUrl"
Write-Host "Runtime logs: $LogDirectory"

if (-not $NoBrowser) {
    Start-Process $UiUrl
}
