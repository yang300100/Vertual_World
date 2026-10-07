param(
    [ValidateRange(1, 65535)]
    [int]$Port = 8000
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$VenvRoot = Join-Path $ProjectRoot ".venv"
$PidFile = Join-Path $ProjectRoot "run\tray.pid"
$StopScript = Join-Path $PSScriptRoot "stop_world.ps1"

# 同一项目即使被同时双击启动，也只能保留一个托盘图标。
$rootBytes = [System.Text.Encoding]::UTF8.GetBytes($ProjectRoot.ToUpperInvariant())
$sha = [System.Security.Cryptography.SHA256]::Create()
try {
    $rootHash = [System.BitConverter]::ToString($sha.ComputeHash($rootBytes)).Replace("-", "").Substring(0, 24)
}
finally {
    $sha.Dispose()
}
$trayMutex = [System.Threading.Mutex]::new($false, "Local\VirtualWorldTray_$rootHash")
$ownsMutex = $false
try {
    try {
        $ownsMutex = $trayMutex.WaitOne(0)
    }
    catch [System.Threading.AbandonedMutexException] {
        $ownsMutex = $true
    }
    if (-not $ownsMutex) { return }

    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -AssemblyName System.Drawing

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

    $script:trayIcon = [System.Windows.Forms.NotifyIcon]::new()
    $script:trayMenu = [System.Windows.Forms.ContextMenuStrip]::new()
    $script:serviceTimer = [System.Windows.Forms.Timer]::new()
    $script:trayIcon.Icon = [System.Drawing.SystemIcons]::Application
    $script:trayIcon.Text = "虚拟世界"

    $openItem = $script:trayMenu.Items.Add("打开虚拟世界")
    $openItem.add_Click({ Start-Process "http://127.0.0.1:$Port/" })
    [void]$script:trayMenu.Items.Add([System.Windows.Forms.ToolStripSeparator]::new())
    $stopItem = $script:trayMenu.Items.Add("停止运行")
    $stopItem.add_Click({
        $script:serviceTimer.Stop()
        $stopItem.Enabled = $false
        try {
            & $StopScript
            [System.Windows.Forms.Application]::ExitThread()
        }
        catch {
            $script:serviceTimer.Start()
            $stopItem.Enabled = $true
            [void][System.Windows.Forms.MessageBox]::Show(
                "停止虚拟世界失败：$($_.Exception.Message)",
                "虚拟世界"
            )
        }
    })
    $script:trayIcon.ContextMenuStrip = $script:trayMenu
    $script:trayIcon.add_DoubleClick({ Start-Process "http://127.0.0.1:$Port/" })

    # 外部停止脚本也能使托盘自动退出，不留下失效图标。
    $script:serviceTimer.Interval = 2500
    $script:serviceTimer.add_Tick({
        if ((@(Get-ProjectProcesses "world_engine.api:app").Count -eq 0) -and
            (@(Get-ProjectProcesses "world_engine.worker").Count -eq 0)) {
            [System.Windows.Forms.Application]::ExitThread()
        }
    })

    [System.IO.Directory]::CreateDirectory((Split-Path -Parent $PidFile)) | Out-Null
    [System.IO.File]::WriteAllText(
        $PidFile,
        [string]$PID,
        [System.Text.UTF8Encoding]::new($false)
    )
    $script:trayIcon.Visible = $true
    $script:serviceTimer.Start()
    [System.Windows.Forms.Application]::Run()
}
finally {
    if ($script:serviceTimer) { $script:serviceTimer.Dispose() }
    if ($script:trayIcon) {
        $script:trayIcon.Visible = $false
        $script:trayIcon.Dispose()
    }
    if ($script:trayMenu) { $script:trayMenu.Dispose() }
    if (Test-Path -LiteralPath $PidFile) {
        $storedId = [System.IO.File]::ReadAllText($PidFile).Trim()
        if ($storedId -eq [string]$PID) {
            Remove-Item -LiteralPath $PidFile -Force
        }
    }
    if ($ownsMutex) { $trayMutex.ReleaseMutex() }
    $trayMutex.Dispose()
}
