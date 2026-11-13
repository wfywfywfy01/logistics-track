param(
    [switch]$Register,
    [string]$PythonPath = (Get-Command python -ErrorAction Stop).Source
)
# UPS office-browser fallback. Requires this Windows user to be logged in.
# Register the task explicitly; ordinary invocation executes one batch only.
$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$taskName = 'Logistics-UPS-Office-Fallback'

if ($Register) {
    $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $arguments = '-NoProfile -NonInteractive -WindowStyle Hidden -File "{0}" -PythonPath "{1}"' -f $PSCommandPath, $PythonPath
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arguments -WorkingDirectory $repo
    $triggers = @(
        (New-ScheduledTaskTrigger -Daily -At '08:00'),
        (New-ScheduledTaskTrigger -AtLogOn -User $identity)
    )
    $principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit (New-TimeSpan -Hours 3) -RestartCount 2 `
        -RestartInterval (New-TimeSpan -Minutes 30) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $triggers `
        -Principal $principal -Settings $settings -Description 'Refresh active UPS waybills using office Chrome and safely apply official results.' -Force | Out-Null
    Write-Output "Registered: $taskName"
    exit 0
}

Set-Location -LiteralPath $repo
$logDir = Join-Path $repo 'logs'
New-Item -ItemType Directory -Path $logDir -Force | Out-Null
$logFile = Join-Path $logDir ('ups_task_{0}.log' -f (Get-Date -Format 'yyyyMMdd-HHmmss'))
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUNBUFFERED = '1'
"start $([DateTime]::UtcNow.ToString('o'))" | Out-File -FilePath $logFile -Encoding UTF8
try {
    # No chat/email send here. The existing server notification worker owns delivery.
    & $PythonPath 'tools/local_ups_channel.py' --refresh-active --retry-failed 2>&1 |
        Out-File -FilePath $logFile -Append -Encoding UTF8
    $resultCode = $LASTEXITCODE
    "end $([DateTime]::UtcNow.ToString('o')) exit=$resultCode" |
        Out-File -FilePath $logFile -Append -Encoding UTF8
    exit $resultCode
} catch {
    "failed $([DateTime]::UtcNow.ToString('o')) $($_.Exception.GetType().Name)" |
        Out-File -FilePath $logFile -Append -Encoding UTF8
    exit 1
}
