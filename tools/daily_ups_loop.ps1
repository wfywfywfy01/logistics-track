param([switch]$RunNow)
# 每天 08:00 跑一次 UPS 本机通道(导出 -> 本机 Chrome 抓 -> 回传 -> 应用)。
# 桌面端没有可用的计划任务(子进程被沙箱限制写文件),因此用常驻循环;
# 进程重启后若当天还没跑过且已过 08:00,会立即补跑一次。
# 手动跑一次: powershell -File tools\daily_ups_loop.ps1 -RunNow
$repo = 'D:\vpsDSH\物流追踪skill-review'
$channel = 'e02a0a05-7997-4d50-b0b9-cad9754c0bdc'
Set-Location $repo

function Invoke-UpsChannel {
    $logDir = Join-Path $repo 'logs'
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
    $stamp = Get-Date -Format 'yyyyMMdd'
    $log = Join-Path $logDir ("ups_channel_$stamp.log")
    $env:PYTHONIOENCODING = 'utf-8'
    "=== start $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ===" | Out-File -Append -Encoding UTF8 $log
    & python 'tools\local_ups_channel.py' --refresh-active 2>&1 |
        Out-File -Append -Encoding UTF8 $log
    $exitCode = $LASTEXITCODE
    "=== end exit=$exitCode $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ===" |
        Out-File -Append -Encoding UTF8 $log

    $failed = $exitCode -ne 0
    if (-not $failed) {
        $summary = Select-String -Path $log -Pattern '抓取完成:本轮 (\d+) 单,成功 (\d+)' |
            Select-Object -Last 1
        if ($summary -and [int]$summary.Matches[0].Groups[2].Value -eq 0) { $failed = $true }
    }
    if ($failed) {
        $body = "【物流小助手】本机 UPS 通道本次执行异常(exit=$exitCode)，请查看 logs\ups_channel_$stamp.log"
        & vps-work im +agent-notify --target im --agent-slug logistics-track --agent-name 物流小助手 --bot-name 物流小助手 --channel-id $channel --body $body 2>&1 |
            Out-File -Append -Encoding UTF8 $log
    } else {
        Set-Content -Path (Join-Path $logDir '.last_run_date') -Value (Get-Date -Format 'yyyy-MM-dd') -Encoding ASCII
    }
    return $failed
}

if ($RunNow) { $null = Invoke-UpsChannel; exit 0 }

# 启动补跑:当天还没成功跑过且已过 08:00,立即补一次(防止会话重启漏跑)
$mark = Join-Path $repo 'logs\.last_run_date'
$today = Get-Date -Format 'yyyy-MM-dd'
$last = if (Test-Path $mark) { (Get-Content $mark -Raw).Trim() } else { '' }
if ($last -ne $today -and (Get-Date).Hour -ge 8) {
    "catch-up run at $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')" |
        Out-File -Append -Encoding UTF8 (Join-Path $repo 'logs\ups_channel_loop.log')
    $null = Invoke-UpsChannel
}

while ($true) {
    $now = Get-Date
    $next = $now.Date.AddHours(8)
    if ($next -le $now) { $next = $next.AddDays(1) }
    Start-Sleep -Seconds ([int](($next - $now).TotalSeconds))
    $null = Invoke-UpsChannel
}

