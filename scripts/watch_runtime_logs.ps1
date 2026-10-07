param(
    [Parameter(Mandatory = $true)]
    [string]$LogDirectory,
    [string]$UiUrl = "http://127.0.0.1:8000/"
)

$ErrorActionPreference = "Stop"
# Windows PowerShell 5.1 的控制台输出也切到 UTF-8，避免日志中的中文变成乱码。
$utf8Console = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = $utf8Console
$global:OutputEncoding = $utf8Console
try { [Console]::InputEncoding = $utf8Console } catch { }
chcp.com 65001 > $null
$utf8 = [System.Text.UTF8Encoding]::new($false)

$sources = @(
    @{ Name = "API"; Path = (Join-Path $LogDirectory "api.stdout.log") },
    @{ Name = "API"; Path = (Join-Path $LogDirectory "api.stderr.log") },
    @{ Name = "Worker"; Path = (Join-Path $LogDirectory "worker.stdout.log") },
    @{ Name = "Worker"; Path = (Join-Path $LogDirectory "worker.stderr.log") }
)
$states = @{}

function Write-LogLine {
    param([string]$Source, [string]$Line)
    $time = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Write-Host "[$time][$Source] $Line"
}

function Initialize-LogSource {
    param($Source)
    $state = @{
        Offset = [long]0
        Length = [long]0
        Created = [datetime]::MinValue
        Pending = [byte[]]@()
    }
    if (Test-Path -LiteralPath $Source.Path) {
        $item = Get-Item -LiteralPath $Source.Path
        $state.Created = $item.CreationTimeUtc
        # 启动时回显最近日志，随后从文件末尾接着跟随。
        foreach ($line in (Get-Content -LiteralPath $Source.Path -Tail 30 -Encoding UTF8 -ErrorAction SilentlyContinue)) {
            Write-LogLine -Source $Source.Name -Line ([string]$line)
        }
        $state.Offset = (Get-Item -LiteralPath $Source.Path).Length
        $state.Length = $state.Offset
    }
    return $state
}

function Read-LogSource {
    param($Source, $State)
    if (-not (Test-Path -LiteralPath $Source.Path)) { return }

    $item = Get-Item -LiteralPath $Source.Path
    if ($item.Length -lt $State.Length -or $item.CreationTimeUtc -ne $State.Created) {
        $State.Offset = 0
        $State.Pending = [byte[]]@()
        $State.Created = $item.CreationTimeUtc
        Write-LogLine -Source $Source.Name -Line "日志文件已重置，继续跟随新内容。"
    }
    if ($item.Length -le $State.Offset) { return }

    $share = [System.IO.FileShare]::ReadWrite -bor [System.IO.FileShare]::Delete
    $stream = $null
    try {
        $stream = [System.IO.FileStream]::new(
            $Source.Path,
            [System.IO.FileMode]::Open,
            [System.IO.FileAccess]::Read,
            $share
        )
        [void]$stream.Seek($State.Offset, [System.IO.SeekOrigin]::Begin)
        $buffer = [byte[]]::new(8192)
        $count = $stream.Read($buffer, 0, $buffer.Length)
        $State.Offset = $stream.Position
        $State.Length = $item.Length
    }
    catch [System.IO.IOException] {
        return
    }
    finally {
        if ($null -ne $stream) { $stream.Dispose() }
    }
    if ($count -le 0) { return }

    $combined = [byte[]]::new($State.Pending.Length + $count)
    if ($State.Pending.Length -gt 0) {
        [Array]::Copy($State.Pending, 0, $combined, 0, $State.Pending.Length)
    }
    [Array]::Copy($buffer, 0, $combined, $State.Pending.Length, $count)

    $lineStart = 0
    for ($index = 0; $index -lt $combined.Length; $index++) {
        if ($combined[$index] -ne 10) { continue }
        $lineLength = $index - $lineStart
        if ($lineLength -gt 0 -and $combined[$index - 1] -eq 13) { $lineLength-- }
        $line = $utf8.GetString($combined, $lineStart, $lineLength)
        Write-LogLine -Source $Source.Name -Line $line
        $lineStart = $index + 1
    }
    $pendingLength = $combined.Length - $lineStart
    if ($pendingLength -gt 0) {
        $State.Pending = [byte[]]::new($pendingLength)
        [Array]::Copy($combined, $lineStart, $State.Pending, 0, $pendingLength)
    }
    else {
        $State.Pending = [byte[]]@()
    }
}

Write-Host "虚拟世界运行日志：$UiUrl"
Write-Host "日志文件：$LogDirectory"
Write-Host "本地调试输出可能包含角色私密内容。关闭本窗口只停止查看日志，不会停止世界服务。"
Write-Host "按 Ctrl+C 关闭日志查看窗口。"

if (-not (Test-Path -LiteralPath $LogDirectory)) {
    [System.IO.Directory]::CreateDirectory($LogDirectory) | Out-Null
}

foreach ($source in $sources) {
    $states[$source.Path] = Initialize-LogSource -Source $source
}

try {
    while ($true) {
        foreach ($source in $sources) {
            Read-LogSource -Source $source -State $states[$source.Path]
        }
        Start-Sleep -Milliseconds 300
    }
}
finally {
    foreach ($source in $sources) {
        $state = $states[$source.Path]
        if ($state.Pending.Length -gt 0) {
            Write-LogLine -Source $source.Name -Line $utf8.GetString($state.Pending)
        }
    }
}
