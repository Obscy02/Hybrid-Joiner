<#
Registers Connector-Agent.ps1 as a Windows Scheduled Task: starts at
system boot, restarts automatically if it ever stops, runs under a
specific service account rather than whoever happens to be logged in.

Run this ONCE, as Administrator, on the machine that will run the
connector (a domain-joined server or workstation with the ActiveDirectory
PowerShell module installed).

Usage:
    .\Register-ConnectorTask.ps1 `
        -BackendUrl "https://your-backend-host" `
        -ConnectorKey "<the connector key from scripts/configure_backend.py>" `
        -ServiceAccount "YOURDOMAIN\svc-joiner-connector"

You'll be prompted for the service account's password interactively -
it's stored by Windows in the Task Scheduler's own encrypted credential
store, not written anywhere by this script.

That service account should hold ONLY the AD rights this connector
actually needs (create users in the relevant OUs, manage membership in
specific groups) - not Domain Admin. Setting that delegation up is a
separate, one-time AD administration task; ask whoever manages your AD
delegation model to scope it, the same way you wouldn't reuse a
Domain Admin account for a scheduled task that only needs to do one thing.
#>
param(
    [Parameter(Mandatory = $true)][string]$BackendUrl,
    [Parameter(Mandatory = $true)][string]$ConnectorKey,
    [Parameter(Mandatory = $true)][string]$ServiceAccount,
    [string]$TaskName = "Hybrid Joiner Connector",
    [int]$PollSeconds = 30
)

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Error "Run this script as Administrator - registering a scheduled task under a different account requires it."
    exit 1
}

$ScriptPath = Join-Path $PSScriptRoot "Connector-Agent.ps1"
if (-not (Test-Path $ScriptPath)) {
    Write-Error "Could not find Connector-Agent.ps1 next to this script at: $ScriptPath"
    exit 1
}

$Credential = Get-Credential -UserName $ServiceAccount -Message "Password for the connector's service account"

$Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$ScriptPath`" -BackendUrl `"$BackendUrl`" -ConnectorKey `"$ConnectorKey`" -PollSeconds $PollSeconds"

$Action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $Arguments
$Trigger = New-ScheduledTaskTrigger -AtStartup
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Days 0)  # 0 = no time limit; this is meant to run forever

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -User $Credential.UserName `
    -Password $Credential.GetNetworkCredential().Password `
    -RunLevel Limited `
    -Force

Write-Host "Registered scheduled task '$TaskName'."
Write-Host "Starting it now so you don't have to reboot to test it..."
Start-ScheduledTask -TaskName $TaskName

Start-Sleep -Seconds 3
$Info = Get-ScheduledTaskInfo -TaskName $TaskName
Write-Host "Last run result: $($Info.LastTaskResult) (0 = still running/success so far - check back in a minute)"
Write-Host ""
Write-Host "To check on it later:"
Write-Host "  Get-ScheduledTaskInfo -TaskName `"$TaskName`""
Write-Host "To stop it:"
Write-Host "  Stop-ScheduledTask -TaskName `"$TaskName`""
Write-Host "To remove it entirely:"
Write-Host "  Unregister-ScheduledTask -TaskName `"$TaskName`" -Confirm:`$false"
