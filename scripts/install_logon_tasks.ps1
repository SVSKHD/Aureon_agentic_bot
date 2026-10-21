<#
  Aureon MT5 — reboot recovery with no human (v2.15.0). See docs/SERVICE.md, "Reboot sequence".

    powershell -ExecutionPolicy Bypass -File .\scripts\install_logon_tasks.ps1                 # install / update both tasks
    powershell -ExecutionPolicy Bypass -File .\scripts\install_logon_tasks.ps1 -Uninstall      # remove both
    powershell -ExecutionPolicy Bypass -File .\scripts\install_logon_tasks.ps1 -Mt5Path "C:\Program Files\MetaTrader 5\terminal64.exe"

  Two Scheduled Tasks, at log on of the CURRENT user, running in the interactive session (MetaTrader 5 needs a desktop
  session — do not use NSSM / a Windows service for it):

    Aureon-MT5   starts terminal64.exe              delay 0 s
    Aureon       the launcher loop                  delay 30 s, restarted every minute if it ever stops
                 (scripts\aureon_service.ps1 -Action run: backoff, hang kill, exit-75 restarts; the bot's own pre-flight
                 then waits for MT5 + the account login)

  The tasks only fire at log on, so the box also needs:
    * Windows auto-logon for this user — Sysinternals Autologon (https://learn.microsoft.com/sysinternals/downloads/autologon)
    * never sleep:  powercfg -change -standby-timeout-ac 0   (and -hibernate-timeout-ac 0)
    * screensaver off (a locked screensaver can stall the terminal's UI thread):
      reg add "HKCU\Control Panel\Desktop" /v ScreenSaveActive /t REG_SZ /d 0 /f
#>
param(
    [string]$Mt5Path = "",
    [string]$Python = "",
    [string]$Root = "",
    [string]$Name = "Aureon",
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
if (-not $Root) { $Root = Split-Path -Parent $PSScriptRoot }
$Mt5Task = "$Name-MT5"
$User = "$env:USERDOMAIN\$env:USERNAME"

if ($Uninstall) {
    foreach ($t in @($Name, $Mt5Task)) {
        if (Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue) {
            Stop-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue
            Unregister-ScheduledTask -TaskName $t -Confirm:$false
            Write-Host "removed task '$t'"
        }
    }
    return
}

function Resolve-Mt5Path {
    # same rule as the launcher: -Mt5Path, AUREON_MT5_PATH (environment or .env), else logs\mt5_path.txt (cached by the bot)
    $p = $Mt5Path
    if (-not $p) { $p = $env:AUREON_MT5_PATH }
    if (-not $p) {
        $envFile = Join-Path $Root ".env"
        if (Test-Path $envFile) {
            $line = Get-Content $envFile | Where-Object { $_ -match '^\s*AUREON_MT5_PATH\s*=' } | Select-Object -First 1
            if ($line) { $p = ($line -split '=', 2)[1].Trim().Trim('"') }
        }
    }
    if (-not $p) {
        $cache = Join-Path $Root "logs\mt5_path.txt"
        if (Test-Path $cache) { $p = (Get-Content $cache -Raw).Trim() }
    }
    return $p
}

if (-not $Python) {
    $venv = Join-Path $Root ".venv\Scripts\python.exe"
    $Python = if (Test-Path $venv) { $venv } else { (Get-Command python -ErrorAction SilentlyContinue).Source }
}
if (-not $Python) { throw "python not found — pass -Python C:\path\to\python.exe" }

$svc = Get-Service $Name -ErrorAction SilentlyContinue
if ($svc) {
    Write-Warning "a Windows service '$Name' exists (installed with NSSM). Remove it first so two launchers do not run: .\scripts\aureon_service.ps1 -Action uninstall"
}

$common = @{
    AllowStartIfOnBatteries    = $true
    DontStopIfGoingOnBatteries = $true
    ExecutionTimeLimit         = [TimeSpan]::Zero
    MultipleInstances          = "IgnoreNew"
    StartWhenAvailable         = $true
}
$principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited

# ---- Aureon-MT5: the terminal, at log on, no delay
$exe = Resolve-Mt5Path
if ($exe -and (Test-Path $exe)) {
    $a = New-ScheduledTaskAction -Execute $exe -WorkingDirectory (Split-Path $exe)
    $t = New-ScheduledTaskTrigger -AtLogOn -User $User
    $s = New-ScheduledTaskSettingsSet @common
    Register-ScheduledTask -TaskName $Mt5Task -Action $a -Trigger $t -Settings $s -Principal $principal -Force `
        -Description "Aureon: start MetaTrader 5 at log on" | Out-Null
    Write-Host "task '$Mt5Task': $exe at log on (0 s)"
} else {
    Write-Warning "MT5 path unknown — task '$Mt5Task' NOT created. Pass -Mt5Path or set AUREON_MT5_PATH in .env; the launcher and the bot's pre-flight start the terminal only when they know the path."
}

# ---- Aureon: the launcher loop, at log on + 30 s, restarted every minute on failure
$launcher = Join-Path $Root "scripts\aureon_service.ps1"
$argsLine = "-NoProfile -ExecutionPolicy Bypass -File `"$launcher`" -Action run -Python `"$Python`" -Root `"$Root`""
$a = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $argsLine -WorkingDirectory $Root
$t = New-ScheduledTaskTrigger -AtLogOn -User $User
$t.Delay = "PT30S"
$s = New-ScheduledTaskSettingsSet @common -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName $Name -Action $a -Trigger $t -Settings $s -Principal $principal -Force `
    -Description "Aureon: launcher loop (backoff, hang kill, restarts) at log on + 30 s" | Out-Null
Write-Host "task '$Name': launcher loop at log on (+30 s), restart every 1 min on failure"

Write-Host ""
Write-Host "REMINDER — the tasks fire at LOG ON, so after a reboot this box must log in by itself and stay awake:"
Write-Host "  * Windows auto-logon for $User : Sysinternals Autologon (Autologon.exe → user, domain, password → Enable)"
Write-Host "  * never sleep:  powercfg -change -standby-timeout-ac 0 ; powercfg -change -hibernate-timeout-ac 0"
Write-Host "  * screensaver off:  reg add `"HKCU\Control Panel\Desktop`" /v ScreenSaveActive /t REG_SZ /d 0 /f"
Write-Host "Then test: restart Windows and wait for 'BACK ONLINE after restart' in Discord (docs/SERVICE.md, Reboot sequence)."
