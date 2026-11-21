<#
.SYNOPSIS
    天刀·帮派定时点歌台一键启动脚本
.DESCRIPTION
    检查 .venv 虚拟环境与端口 8765 占用状态，展示本机与局域网访问地址，
    启动 FastAPI + Uvicorn 服务；支持 -LiveNow 随时立即开播识别、-NoBrowser 静默启动。
#>
param(
    [switch]$NoBrowser,
    [switch]$LiveNow,
    [int]$Port = 8765
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$ProjectRoot = $PSScriptRoot
if (-not $ProjectRoot) {
    $ProjectRoot = (Get-Location).Path
}
Set-Location -LiteralPath $ProjectRoot

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "   天刀·帮派定时点歌台（周四/周日 19:30–20:20）— 启动向导" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan

# 1. 检查虚拟环境 .venv
$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $VenvPython)) {
    Write-Host "[错误] 未找到虚拟环境解释器: $VenvPython" -ForegroundColor Red
    Write-Host "请先创建虚拟环境并安装项目依赖后再重新运行 start.ps1。" -ForegroundColor Yellow
    exit 1
}
Write-Host "[通过] 已就绪虚拟环境: $VenvPython" -ForegroundColor Green

# 确保运行期目录存在
$CacheDir = Join-Path $ProjectRoot "data\cache"
$TrashDir = Join-Path $ProjectRoot "_待删除"
New-Item -ItemType Directory -Force -Path $CacheDir | Out-Null
New-Item -ItemType Directory -Force -Path $TrashDir | Out-Null

# 2. 检查端口是否被占用
$PortOccupied = $false
$OccupyingInfo = ""

try {
    $Listeners = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if ($Listeners) {
        $PortOccupied = $true
        $OwnerPid = ($Listeners | Select-Object -First 1).OwningProcess
        $Proc = Get-Process -Id $OwnerPid -ErrorAction SilentlyContinue
        if ($Proc) {
            $OccupyingInfo = "PID=$OwnerPid ($($Proc.ProcessName))"
        } else {
            $OccupyingInfo = "PID=$OwnerPid"
        }
    }
} catch {
    # 回退使用 TcpListener 探测端口可用性
    try {
        $TestListener = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, $Port)
        $TestListener.Start()
        $TestListener.Stop()
    } catch {
        $PortOccupied = $true
        $OccupyingInfo = "端口已被本机其他进程监听"
    }
}

if ($PortOccupied) {
    $IsJukeboxRunning = $false
    try {
        $HealthResp = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/api/health" -UseBasicParsing -TimeoutSec 2 -ErrorAction Stop
        if ($HealthResp.StatusCode -eq 200) {
            $HealthObj = $HealthResp.Content | ConvertFrom-Json
            if ($null -ne $HealthObj -and $HealthObj.status -eq "ok") {
                $IsJukeboxRunning = $true
            }
        }
    } catch {
        $IsJukeboxRunning = $false
    }

    if ($IsJukeboxRunning) {
        Write-Host "[提示] 点歌台服务已在运行 ($OccupyingInfo) -> http://127.0.0.1:$Port" -ForegroundColor Green
        if ($LiveNow) {
            try {
                Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:$Port/api/schedule/override" -ContentType "application/json" -Body '{"active":true}' -TimeoutSec 5 | Out-Null
                Write-Host "[通过] 已立即激活播音与帮派聊天 OCR 识别！" -ForegroundColor Yellow
            } catch {
                Write-Host "[警告] 激活立即开播失败: $($_.Exception.Message)" -ForegroundColor Red
                exit 1
            }
        }
        if (-not $NoBrowser) {
            Start-Process "http://127.0.0.1:$Port" | Out-Null
        }
        exit 0
    }

    Write-Host "[错误] 端口 $Port 当前已被其他进程占用 ($OccupyingInfo)！" -ForegroundColor Red
    Write-Host "  -> 若需改用其他端口，可执行: .\start.ps1 -Port 8766" -ForegroundColor Yellow
    exit 1
}
Write-Host "[通过] 端口 $Port 空闲可用" -ForegroundColor Green

# 3. 获取本机与局域网 IPv4 地址
$LanIps = @()
try {
    $NetIps = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object {
        $_.IPAddress -notlike "127.*" -and
        $_.IPAddress -notlike "169.254.*" -and
        $_.PrefixOrigin -ne "WellKnown"
    }
    foreach ($Item in $NetIps) {
        if ($LanIps -notcontains $Item.IPAddress) {
            $LanIps += $Item.IPAddress
        }
    }
} catch {
    try {
        $HostAddrs = [System.Net.Dns]::GetHostAddresses([System.Net.Dns]::GetHostName())
        foreach ($Addr in $HostAddrs) {
            if ($Addr.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetwork) {
                $IpStr = $Addr.IPAddressToString
                if ($IpStr -notlike "127.*" -and $IpStr -notlike "169.254.*") {
                    if ($LanIps -notcontains $IpStr) {
                        $LanIps += $IpStr
                    }
                }
            }
        }
    } catch {
        $LanIps = @()
    }
}

$LocalUrl = "http://127.0.0.1:$Port"
Write-Host ""
Write-Host "------------------------------------------------------------" -ForegroundColor DarkGray
Write-Host "  本机控制台地址 : $LocalUrl" -ForegroundColor Green
if ($LanIps.Count -gt 0) {
    foreach ($Ip in $LanIps) {
        Write-Host "  局域网共享地址 : http://$($Ip):$Port  (手机/同局域网队友点歌)" -ForegroundColor Cyan
    }
} else {
    Write-Host "  局域网共享地址 : 未检测到活动局域网 IPv4，仅限本机访问" -ForegroundColor DarkYellow
}
Write-Host "  音频链路体检   : .\scripts\audio_doctor.ps1" -ForegroundColor Gray
Write-Host "  按 Ctrl + C 可优雅停止点歌台服务" -ForegroundColor Gray
Write-Host "------------------------------------------------------------" -ForegroundColor DarkGray
Write-Host ""

# 4. 服务就绪后按需激活立即开播或自动打开浏览器
$HealthUrl = "$LocalUrl/api/health"
$OverrideUrl = "$LocalUrl/api/schedule/override"
if ($LiveNow -or (-not $NoBrowser)) {
    Start-Job -ScriptBlock {
        param($CheckUrl, $OpenUrl, $ApiOverrideUrl, $DoLiveNow, $SkipBrowser)
        $Ready = $false
        for ($i = 0; $i -lt 40; $i++) {
            Start-Sleep -Milliseconds 400
            try {
                $Resp = Invoke-WebRequest -Uri $CheckUrl -UseBasicParsing -TimeoutSec 1 -ErrorAction Stop
                if ($Resp.StatusCode -eq 200) {
                    $Ready = $true
                    break
                }
            } catch {
                # 等待 Uvicorn 完成初始化
            }
        }
        if ($Ready -and $DoLiveNow) {
            try {
                Invoke-RestMethod -Method Post -Uri $ApiOverrideUrl -ContentType "application/json" -Body '{"active":true}' -TimeoutSec 5 | Out-Null
            } catch {
                # 忽略偶发激活超时
            }
        }
        if (-not $SkipBrowser) {
            Start-Process $OpenUrl | Out-Null
        }
    } -ArgumentList $HealthUrl, $LocalUrl, $OverrideUrl, [bool]$LiveNow, [bool]$NoBrowser | Out-Null
    if ($LiveNow) {
        Write-Host "[模式] 已启用立即开播识别：服务就绪后将自动放歌并开启帮派 OCR 扫描。" -ForegroundColor Yellow
    } else {
        Write-Host "[模式] 已启用定时待机：仅每周四、周日 19:30–20:20 自动开播，平时静音待机。" -ForegroundColor DarkCyan
    }
    if (-not $NoBrowser) {
        Write-Host "[提示] 服务就绪后将自动为您打开浏览器（传入 -NoBrowser 可禁用自动弹窗）..." -ForegroundColor DarkCyan
    }
} else {
    Write-Host "[提示] 已启用 -NoBrowser 定时待机模式，不会自动弹出浏览器窗口。" -ForegroundColor DarkCyan
}

# 5. 启动 Uvicorn 服务
& $VenvPython -m uvicorn backend.main:app --host 0.0.0.0 --port $Port
$ExitCode = $LASTEXITCODE
exit $ExitCode
