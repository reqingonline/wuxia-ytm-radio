<#
.SYNOPSIS
    天刀·帮派定时点歌台一键停止脚本
.DESCRIPTION
    查找并停止监听指定端口（默认 8765）的点歌台 Python 后台服务。
#>
param(
    [int]$Port = 8765
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$Listeners = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if (-not $Listeners) {
    Write-Host "[提示] 端口 $Port 当前没有运行中的点歌台服务。" -ForegroundColor DarkCyan
    Start-Sleep -Milliseconds 1200
    exit 0
}

$StoppedCount = 0
$NonPythonFound = $false
$OwnerPids = $Listeners | Select-Object -ExpandProperty OwningProcess -Unique

foreach ($ProcId in $OwnerPids) {
    $Proc = Get-Process -Id $ProcId -ErrorAction SilentlyContinue
    if ($Proc -and ($Proc.ProcessName -match '^python')) {
        Write-Host "[操作] 正在停止点歌台后台进程 PID=$ProcId ($($Proc.ProcessName))..." -ForegroundColor Yellow
        Stop-Process -Id $ProcId -Force
        $StoppedCount += 1
    } else {
        $ProcLabel = if ($Proc) { $Proc.ProcessName } else { "未知进程" }
        Write-Host "[警告] 端口 $Port 被非 Python 进程占用: PID=$ProcId ($ProcLabel)，已跳过。" -ForegroundColor Red
        $NonPythonFound = $true
    }
}

Start-Sleep -Seconds 1
$Remaining = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue

if (-not $Remaining) {
    Write-Host "[完成] 天刀点歌台服务已关闭（共停止 $StoppedCount 个进程，端口 $Port 已释放）。" -ForegroundColor Green
    Start-Sleep -Milliseconds 1200
    exit 0
}

if ($NonPythonFound) {
    Write-Host "[失败] 端口 $Port 仍被非 Python 进程占用，未强制关闭。" -ForegroundColor Red
    Start-Sleep -Milliseconds 1500
    exit 1
}

Write-Host "[警告] 端口 $Port 仍处于监听状态，请稍后重试或手动检查。" -ForegroundColor Red
Start-Sleep -Milliseconds 1500
exit 1