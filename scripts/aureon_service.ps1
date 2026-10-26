<#
  Aureon MT5 supervisor (v2.7.2). See docs/SERVICE.md.

    .\scripts\aureon_service.ps1 -Action install            # service (NSSM) or, without NSSM, a logon Scheduled Task
    .\scripts\aureon_service.ps1 -Action uninstall
    .\scripts\aureon_service.ps1 -Action status
    .\scripts\aureon_service.ps1 -Action run                # the launcher loop itself (what the service / task runs)
    .\scripts\aureon_service.ps1 -Action install-fallback   # Task Scheduler: scripts\deadman_fallback.py every 5 min
    .\scripts\aureon_service.ps1 -Action restart-mt5        # v2.18.1: stop + start terminal64 (used by /repair)

  The launcher loop:
    * runs `python main.py` (mode / symbols come from .env — do NOT pass --mode/--symbols, /mode could not change them)
    * restarts on ANY exit; exit code 75 = requested restart (/restart, auto-update, self-heal) → 5 s, no escalation
    * crash backoff 5 s → 30 s → 2 min (reset after 10 min of healthy running)
    * hang detection: logs\heartbeat.txt older than 5 min (after a 5 min start grace) → kill the process tree → restart
    * stdout/stderr → logs\service.log, rotated at 10 MB (5 files kept)
#>
param(
    [ValidateSet("install", "uninstall", "status", "run", "install-fallback", "restart-mt5")]
    [string]$Action = "run",
    [string]$Python = "",
    [string]$Root = "",
    [string]$Name = "Aureon"
)

$ErrorActionPreference = "Stop"
if (-not $Root) { $Root = Split-Path -Parent $PSScriptRoot }
if (-not $Python) {
    $venv = Join-Path $Root ".venv\Scripts\python.exe"
    $Python = if (Test-Path $venv) { $venv } else { (Get-Command python -ErrorAction SilentlyContinue).Source }
}
if (-not $Python) { throw "python not found — pass -Python C:\path\to\python.exe" }

$Logs = Join-Path $Root "logs"
$ServiceLog = Join-Path $Logs "service.log"
$Heartbeat = Join-Path $Logs "heartbeat.txt"
$Backoff = @(5, 30, 120)
$HealthyResetSec = 600
$HangSec = 300
$StartGraceSec = 300
$RotateBytes = 10MB
$RotateKeep = 5
$ExitRestart = 75

New-Item -ItemType Directory -Force -Path $Logs | Out-Null

function Write-SvcLog([string]$msg) {
    $line = "{0:yyyy-MM-dd HH:mm:ss} [supervisor] {1}" -f (Get-Date), $msg
    Add-Content -Path $ServiceLog -Value $line -Encoding UTF8
    Write-Host $line
}

function Rotate-Log {
    if ((Test-Path $ServiceLog) -and ((Get-Item $ServiceLog).Length -gt $RotateBytes)) {
        for ($i = $RotateKeep - 1; $i -ge 1; $i--) {
            $src = "$ServiceLog.$i"; $dst = "$ServiceLog.$($i + 1)"
            if (Test-Path $src) { Move-Item -Force $src $dst }
        }
        Move-Item -Force $ServiceLog "$ServiceLog.1"
    }
}

function Get-Mt5Path {
    # v2.10.1: AUREON_MT5_PATH (environment or .env), else the path the bot cached from terminal_info() last run
    $p = $env:AUREON_MT5_PATH
    if (-not $p) {
        $envFile = Join-Path $Root ".env"
        if (Test-Path $envFile) {
            $line = Get-Content $envFile | Where-Object { $_ -match '^\s*AUREON_MT5_PATH\s*=' } | Select-Object -First 1
            if ($line) { $p = ($line -split '=', 2)[1].Trim().Trim('"') }
        }
    }
    if (-not $p) {
        $cache = Join-Path $Logs "mt5_path.txt"
        if (Test-Path $cache) { $p = (Get-Content $cache -Raw).Trim() }
    }
    return $p
}

function Ensure-Mt5 {
    # MT5 before the bot: start terminal64.exe when none is running for that path. The bot itself waits for
    # MetaTrader5.initialize() (AUREON_MT5_BOOT_WAIT, 5 s → 30 s backoff) and exits 75 if it never comes up.
    $path = Get-Mt5Path
    if (-not $path -or -not (Test-Path $path)) { Write-SvcLog "MT5 path unknown (set AUREON_MT5_PATH) — not starting the terminal"; return }
    $running = Get-Process terminal64 -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $path }
    if (-not $running) {
        Write-SvcLog "starting MT5: $path"
        Start-Process -FilePath $path -WorkingDirectory (Split-Path $path) | Out-Null
        Start-Sleep -Seconds 10
    }
}

function Run-Loop {
    $attempt = 0
    $env:PYTHONUNBUFFERED = "1"
    $env:PYTHONIOENCODING = "utf-8"
    $env:AUREON_SUPERVISED = "1"
    while ($true) {
        Rotate-Log
        Ensure-Mt5
        Write-SvcLog "starting: $Python main.py (cwd $Root)"
        $started = Get-Date
        # cmd.exe does the appending redirection; the hang check kills the whole tree
        $cmd = "`"$Python`" main.py >> `"$ServiceLog`" 2>&1"
        $p = Start-Process -FilePath "cmd.exe" -ArgumentList "/c", "`"$cmd`"" -WorkingDirectory $Root -PassThru -WindowStyle Hidden
        $hung = $false
        while (-not $p.HasExited) {
            Start-Sleep -Seconds 15
            $up = ((Get-Date) - $started).TotalSeconds
            if ($up -gt $StartGraceSec) {
                $age = if (Test-Path $Heartbeat) { ((Get-Date) - (Get-Item $Heartbeat).LastWriteTime).TotalSeconds } else { 1e9 }
                if ($age -gt $HangSec) {
                    Write-SvcLog ("HANG: heartbeat.txt is {0:N0} s old — killing process tree {1}" -f $age, $p.Id)
                    & taskkill.exe /PID $p.Id /T /F | Out-Null
                    $hung = $true
                    break
                }
            }
        }
        $p.WaitForExit()
        $code = if ($hung) { -1 } else { $p.ExitCode }
        $ran = ((Get-Date) - $started).TotalSeconds
        if ($code -eq $ExitRestart) {
            $attempt = 0; $delay = 5
            Write-SvcLog "exit 75 (requested restart) after $([int]$ran) s — restarting in $delay s"
        } else {
            if ($ran -ge $HealthyResetSec) { $attempt = 0 }
            $delay = $Backoff[[Math]::Min($attempt, $Backoff.Count - 1)]
            $attempt++
            Write-SvcLog "exit $code after $([int]$ran) s — restart #$attempt in $delay s"
        }
        Start-Sleep -Seconds $delay
    }
}

$nssm = (Get-Command nssm -ErrorAction SilentlyContinue).Source
$launcherArgs = "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Action run -Python `"$Python`" -Root `"$Root`""

switch ($Action) {
    "run" { Run-Loop }
    "restart-mt5" {
        # v2.18.1: /repair's restart_mt5_terminal — stop terminal64 for this MT5 path, start it again (Ensure-Mt5)
        $path = Get-Mt5Path
        Get-Process terminal64 -ErrorAction SilentlyContinue | Where-Object { -not $path -or $_.Path -eq $path } | Stop-Process -Force
        Start-Sleep -Seconds 3
        Ensure-Mt5
        Write-SvcLog "restart-mt5 done"
    }
    "install" {
        if ($nssm) {
            & $nssm install $Name "powershell.exe" $launcherArgs
            & $nssm set $Name AppDirectory $Root
            & $nssm set $Name Start SERVICE_AUTO_START          # start at boot
            & $nssm set $Name AppExit Default Restart           # if the launcher itself dies, NSSM restarts it
            & $nssm set $Name AppRestartDelay 5000
            & $nssm set $Name AppStdout (Join-Path $Logs "supervisor.log")
            & $nssm set $Name AppStderr (Join-Path $Logs "supervisor.log")
            & $nssm set $Name AppRotateFiles 1
            & $nssm set $Name AppRotateOnline 1
            & $nssm set $Name AppRotateBytes 10485760
            & $nssm set $Name Description "Aureon MT5 (launcher loop: backoff + hang restart)"
            & $nssm start $Name
            Write-Host "installed NSSM service '$Name' (boot start). MT5 must be able to start in the service account — see docs/SERVICE.md."
        } else {
            $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $launcherArgs -WorkingDirectory $Root
            $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
            $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
                -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
                -MultipleInstances IgnoreNew
            Register-ScheduledTask -TaskName $Name -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
            Start-ScheduledTask -TaskName $Name
            Write-Host "NSSM not found — registered Scheduled Task '$Name' (at logon, launcher loop). Enable auto-logon for boot start."
        }
    }
    "uninstall" {
        if ($nssm -and (Get-Service $Name -ErrorAction SilentlyContinue)) {
            & $nssm stop $Name; & $nssm remove $Name confirm
        }
        if (Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue) {
            Stop-ScheduledTask -TaskName $Name; Unregister-ScheduledTask -TaskName $Name -Confirm:$false
        }
        Write-Host "removed '$Name' (the running python, if any, is left to exit with its parent)"
    }
    "status" {
        $svc = Get-Service $Name -ErrorAction SilentlyContinue
        if ($svc) { Write-Host "service: $($svc.Status)" }
        $task = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
        if ($task) { Write-Host "scheduled task: $($task.State)" }
        if (Test-Path $Heartbeat) {
            $age = ((Get-Date) - (Get-Item $Heartbeat).LastWriteTime).TotalSeconds
            Write-Host ("heartbeat ({0:N0} s old): {1}" -f $age, (Get-Content $Heartbeat -Raw).Trim())
        } else { Write-Host "no logs\heartbeat.txt yet" }
    }
    "install-fallback" {
        $script = Join-Path $Root "scripts\deadman_fallback.py"
        $action = New-ScheduledTaskAction -Execute $Python -Argument "`"$script`"" -WorkingDirectory $Root
        $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5)
        Register-ScheduledTask -TaskName "$Name-deadman-fallback" -Action $action -Trigger $trigger -Force | Out-Null
        Write-Host "registered '$Name-deadman-fallback' (every 5 min): Telegram alert if logs\heartbeat.txt is older than 5 min"
    }
}
