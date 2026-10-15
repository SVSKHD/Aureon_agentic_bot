<#
  Aureon MT5 — the box belongs to Aureon (v2.10.1). See docs/SERVICE.md.

    # once, from an ELEVATED PowerShell on the trading machine:
    .\scripts\aureon_server.ps1 -Action harden [-ActiveHours "05-23"] [-AutoLogonUser mt5user -AutoLogonPassword ...]
    .\scripts\aureon_server.ps1 -Action plan       # print what harden would change, change nothing

  harden:
    * Windows Update: active hours over the trading day; no auto-reboot while a user is logged on
      (NoAutoRebootWithLoggedOnUsers=1, AUAllowAutoRebootWithLoggedOnUsers=0) — Aureon reboots itself in its maintenance window
    * auto-logon for the MT5 user (AUREON_SERVER_AUTOLOGON=1) so the Scheduled-Task variant and MT5 come back after a reboot
    * sleep / hibernate / display-off disabled; High performance power plan
    * w32tm resync at boot (time.windows.com) — session gates and news windows depend on the clock
    * Windows Defender exclusions for the MT5 folder and the repo
  Prints every change made and every change that failed. Idempotent.
#>
param(
    [ValidateSet("harden", "plan")] [string]$Action = "plan",
    [string]$ActiveHours = $env:AUREON_SERVER_ACTIVE_HOURS,
    [string]$AutoLogonUser = $env:USERNAME,
    [string]$AutoLogonPassword = "",
    [string]$Mt5Path = $env:AUREON_MT5_PATH,
    [string]$Root = ""
)

$ErrorActionPreference = "Continue"
if (-not $Root) { $Root = Split-Path -Parent $PSScriptRoot }
if (-not $ActiveHours) { $ActiveHours = "05-23" }
$start, $end = ($ActiveHours -split "-") | ForEach-Object { [int]$_ }
$autologon = if ($env:AUREON_SERVER_AUTOLOGON) { $env:AUREON_SERVER_AUTOLOGON -eq "1" } else { $true }
if (-not $Mt5Path) {
    $cache = Join-Path $Root "logs\mt5_path.txt"
    if (Test-Path $cache) { $Mt5Path = (Get-Content $cache -Raw).Trim() }
}
$changed = @(); $failed = @()

function Step([string]$what, [scriptblock]$do) {
    if ($Action -eq "plan") { Write-Host "PLAN  $what"; return }
    try { & $do; $script:changed += $what; Write-Host "OK    $what" }
    catch { $script:failed += "$what — $($_.Exception.Message)"; Write-Host "FAIL  $what — $($_.Exception.Message)" }
}

$wu = "HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU"
$ux = "HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings"
Step "Windows Update active hours $($start):00–$($end):00" {
    New-Item -Path $ux -Force | Out-Null
    Set-ItemProperty -Path $ux -Name ActiveHoursStart -Value $start -Type DWord
    Set-ItemProperty -Path $ux -Name ActiveHoursEnd -Value $end -Type DWord
}
Step "no automatic reboot while a user is logged on (Aureon reboots in its maintenance window)" {
    New-Item -Path $wu -Force | Out-Null
    Set-ItemProperty -Path $wu -Name NoAutoRebootWithLoggedOnUsers -Value 1 -Type DWord
    Set-ItemProperty -Path $wu -Name AUAllowAutoRebootWithLoggedOnUsers -Value 0 -Type DWord
}
if ($autologon) {
    Step "auto-logon for $AutoLogonUser (needed after a reboot: MT5 runs in that session)" {
        $wl = "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon"
        Set-ItemProperty -Path $wl -Name AutoAdminLogon -Value "1"
        Set-ItemProperty -Path $wl -Name DefaultUserName -Value $AutoLogonUser
        if ($AutoLogonPassword) { Set-ItemProperty -Path $wl -Name DefaultPassword -Value $AutoLogonPassword }
        else { throw "no -AutoLogonPassword given (or use Sysinternals Autologon to store it encrypted)" }
    }
}
Step "sleep / hibernate / display-off disabled" {
    powercfg /change standby-timeout-ac 0 | Out-Null
    powercfg /change hibernate-timeout-ac 0 | Out-Null
    powercfg /change monitor-timeout-ac 0 | Out-Null
    powercfg /hibernate off | Out-Null
}
Step "High performance power plan" { powercfg /setactive SCHEME_MIN | Out-Null }
Step "w32tm: time.windows.com + resync at boot" {
    w32tm /config /manualpeerlist:"time.windows.com,0x9" /syncfromflags:manual /update | Out-Null
    $a = New-ScheduledTaskAction -Execute "w32tm.exe" -Argument "/resync /force"
    $t = New-ScheduledTaskTrigger -AtStartup
    Register-ScheduledTask -TaskName "Aureon-time-resync" -Action $a -Trigger $t -User "SYSTEM" -RunLevel Highest -Force | Out-Null
}
Step "Defender exclusions: repo $Root" { Add-MpPreference -ExclusionPath $Root }
if ($Mt5Path) { Step "Defender exclusions: MT5 $(Split-Path $Mt5Path)" { Add-MpPreference -ExclusionPath (Split-Path $Mt5Path) } }
else { $failed += "MT5 Defender exclusion — MT5 path unknown (set AUREON_MT5_PATH or run the bot once)" }

Write-Host ""
Write-Host "changed: $($changed.Count)"; $changed | ForEach-Object { Write-Host "  + $_" }
Write-Host "could not: $($failed.Count)"; $failed | ForEach-Object { Write-Host "  - $_" }
