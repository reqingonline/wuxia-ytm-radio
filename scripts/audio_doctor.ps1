<#
.SYNOPSIS
    天刀·帮派点歌台 — 声卡、游戏进程与网络链路全面体检脚本
.DESCRIPTION
    只读无副作用诊断（零发声、零弹窗）：
    1. 检测 Windows 音频输入输出端点，核查是否存在 CABLE Input / CABLE Output；
    2. 检测天刀桌面端 wuxia.exe 或主流安卓模拟器进程与安装路径；
    3. 测试本地代理 127.0.0.1:10808 与 music.youtube.com 连通性；
    4. 调用 .venv 检查 sounddevice 设备枚举及 48000 Hz 采样率支持；
    5. 输出清晰中文诊断结论与下一步操作建议。
#>
param(
    [string]$LocalProxyHost = "127.0.0.1",
    [int]$LocalProxyPort = 10808
)

$ErrorActionPreference = "Continue"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$ScriptDir = $PSScriptRoot
if (-not $ScriptDir) {
    $ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
}
$ProjectRoot = Split-Path -Parent $ScriptDir
$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "   天刀·帮派 24h 云音坊 — 声卡与游戏链路体检报告" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""

$HasCableInput = $false
$HasCableOutput = $false
$GameRunning = $false
$GameInstalled = $false
$ProxyOpen = $false
$YtmDirectOk = $false
$YtmProxyOk = $false
$SoundDevice48kOk = $false

# ------------------------------------------------------------
# 1. 检测 Windows 音频输入与输出端点
# ------------------------------------------------------------
Write-Host "【1/4】检测 Windows 音频输入/输出端点 ..." -ForegroundColor Cyan
$EndpointNames = @()
try {
    $PnpEndpoints = Get-PnpDevice -Class AudioEndpoint -Status OK -ErrorAction SilentlyContinue
    if ($PnpEndpoints) {
        foreach ($Ep in $PnpEndpoints) {
            if ($Ep.FriendlyName) {
                $EndpointNames += $Ep.FriendlyName
            }
        }
    }
} catch {
    # 忽略 PnP 权限差异，稍后还会结合 Win32_SoundDevice 与 sounddevice 核查
}

if ($EndpointNames.Count -eq 0) {
    try {
        $SoundCards = Get-CimInstance Win32_SoundDevice -ErrorAction SilentlyContinue
        foreach ($Sc in $SoundCards) {
            if ($Sc.Name) {
                $EndpointNames += $Sc.Name
            }
        }
    } catch {
        $EndpointNames = @()
    }
}

foreach ($Name in $EndpointNames) {
    Write-Host "  - 音频端点: $Name" -ForegroundColor Gray
    if ($Name -match "CABLE Input") {
        $HasCableInput = $true
    }
    if ($Name -match "CABLE Output") {
        $HasCableOutput = $true
    }
}

if ($HasCableInput -and $HasCableOutput) {
    Write-Host "  [通过] 已完整检测到 VB-CABLE 虚拟推流端 (CABLE Input) 与虚拟麦克风端 (CABLE Output)。" -ForegroundColor Green
} elseif ($HasCableInput -or $HasCableOutput) {
    Write-Host "  [警告] 仅检测到单侧 VB-CABLE 端点 (Input=$HasCableInput, Output=$HasCableOutput)，建议在声音控制面板检查是否被禁用。" -ForegroundColor Yellow
} else {
    Write-Host "  [待处理] 未检测到 CABLE Input / CABLE Output 虚拟声卡（当前仅物理声卡）。" -ForegroundColor Yellow
}
Write-Host ""

# ------------------------------------------------------------
# 2. 检测天刀桌面端 wuxia.exe 与主流模拟器进程
# ------------------------------------------------------------
Write-Host "【2/4】检测天刀客户端与模拟器环境 ..." -ForegroundColor Cyan
$DefaultWuxiaPath = "D:\tiandaoshouyou\WuxiaMobile\wuxia.exe"
if (Test-Path -LiteralPath $DefaultWuxiaPath) {
    $GameInstalled = $true
    Write-Host "  [发现安装] 天刀官方桌面端已安装: $DefaultWuxiaPath" -ForegroundColor Green
} else {
    Write-Host "  [提示] 默认路径未找到 $DefaultWuxiaPath（若安装在其他磁盘或使用模拟器可忽略）。" -ForegroundColor Gray
}

$CandidateProcessPatterns = @(
    @{ Pattern = "^wuxia$"; Label = "天涯明月刀官方桌面端 (wuxia.exe)" },
    @{ Pattern = "MuMuPlayer|MuMuNxMain|NemuPlayer"; Label = "MuMu 模拟器" },
    @{ Pattern = "dnplayer|LDPlayer"; Label = "雷电模拟器 (LDPlayer)" },
    @{ Pattern = "MEmu"; Label = "逍遥模拟器 (MEmu)" },
    @{ Pattern = "Nox"; Label = "夜神模拟器 (Nox)" },
    @{ Pattern = "HD-Player|Bluestacks"; Label = "蓝叠模拟器 (BlueStacks)" },
    @{ Pattern = "AndroidEmulator"; Label = "腾讯手游助手" }
)

$RunningMatches = @()
$AllProcs = Get-Process -ErrorAction SilentlyContinue
foreach ($Rule in $CandidateProcessPatterns) {
    $Matched = @($AllProcs | Where-Object { $_.ProcessName -match $Rule.Pattern })
    if ($Matched.Count -gt 0) {
        $GameRunning = $true
        $FirstPid = $Matched[0].Id
        $RunningMatches += "$($Rule.Label) [PID=$FirstPid]"
    }
}

if ($GameRunning) {
    foreach ($Desc in $RunningMatches) {
        Write-Host "  [运行中] 已检测到游戏/模拟器进程: $Desc" -ForegroundColor Green
    }
} else {
    Write-Host "  [未启动] 当前未检测到正在运行的 wuxia.exe 或常见安卓模拟器进程（开播或框选聊天区前启动即可）。" -ForegroundColor DarkYellow
}
Write-Host ""

# ------------------------------------------------------------
# 3. 测试本地代理 127.0.0.1:10808 与 music.youtube.com 连通性
# ------------------------------------------------------------
Write-Host "【3/4】测试本地代理 ($($LocalProxyHost):$LocalProxyPort) 与 music.youtube.com 连通性 ..." -ForegroundColor Cyan
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

# 3.1 TCP 探测本地代理端口
try {
    $Tcp = New-Object System.Net.Sockets.TcpClient
    $AsyncRes = $Tcp.BeginConnect($LocalProxyHost, $LocalProxyPort, $null, $null)
    $Connected = $AsyncRes.AsyncWaitHandle.WaitOne(1500, $false)
    if ($Connected -and $Tcp.Connected) {
        $ProxyOpen = $true
        $Tcp.EndConnect($AsyncRes)
    }
    $Tcp.Close()
} catch {
    $ProxyOpen = $false
}

if ($ProxyOpen) {
    Write-Host "  [通过] 本地代理端口 $($LocalProxyHost):$LocalProxyPort 处于开启监听状态。" -ForegroundColor Green
} else {
    Write-Host "  [提示] 本地代理端口 $($LocalProxyHost):$LocalProxyPort 未开启（若您的网络支持直连或使用软路由透明代理则属正常）。" -ForegroundColor Gray
}

# 3.2 测试直连 music.youtube.com
try {
    $DirectResp = Invoke-WebRequest -Uri "https://music.youtube.com" -Method Head -UseBasicParsing -TimeoutSec 4 -ErrorAction Stop
    if ($DirectResp.StatusCode -ge 200 -and $DirectResp.StatusCode -lt 500) {
        $YtmDirectOk = $true
        Write-Host "  [通过] 直连 https://music.youtube.com 成功 (HTTP $($DirectResp.StatusCode))。" -ForegroundColor Green
    }
} catch {
    Write-Host "  [提示] 直连 https://music.youtube.com 超时或不可达。" -ForegroundColor DarkYellow
}

# 3.3 若本地代理端口开放，同时测试经本地代理连接 music.youtube.com
if ($ProxyOpen) {
    $ProxyUri = "http://$($LocalProxyHost):$LocalProxyPort"
    try {
        $ProxyResp = Invoke-WebRequest -Uri "https://music.youtube.com" -Method Head -Proxy $ProxyUri -UseBasicParsing -TimeoutSec 5 -ErrorAction Stop
        if ($ProxyResp.StatusCode -ge 200 -and $ProxyResp.StatusCode -lt 500) {
            $YtmProxyOk = $true
            Write-Host "  [通过] 经代理 $ProxyUri 访问 https://music.youtube.com 成功 (HTTP $($ProxyResp.StatusCode))。" -ForegroundColor Green
        }
    } catch {
        Write-Host "  [警告] 经代理 $ProxyUri 访问 https://music.youtube.com 失败: $($_.Exception.Message)" -ForegroundColor Yellow
    }
}
Write-Host ""

# ------------------------------------------------------------
# 4. 使用 .venv 检查 sounddevice 与 48000 Hz 采样率支持
# ------------------------------------------------------------
Write-Host "【4/4】检查 .venv 音频引擎 sounddevice 与 48000 Hz (48 kHz) 采样率支持 ..." -ForegroundColor Cyan
if (-not (Test-Path -LiteralPath $VenvPython)) {
    Write-Host "  [错误] 未找到虚拟环境解释器: $VenvPython" -ForegroundColor Red
} else {
    $PyCheckCode = @"
import json, sys
import sounddevice as sd

apis = [a.get('name', '') for a in sd.query_hostapis()]
devs = sd.query_devices()
wasapi_idx = next((i for i, n in enumerate(apis) if 'WASAPI' in n.upper()), None)

cable_in_ids = []
cable_out_ids = []
out_48k_ok = False
in_48k_ok = False
checked_out_name = ''
checked_in_name = ''

for idx, d in enumerate(devs):
    name = d.get('name', '')
    if d.get('max_output_channels', 0) >= 2 and 'cable input' in name.lower():
        cable_in_ids.append(idx)
    if d.get('max_input_channels', 0) >= 1 and 'cable output' in name.lower():
        cable_out_ids.append(idx)

default_out = sd.default.device[1]
target_out = cable_in_ids[0] if cable_in_ids else default_out
if target_out is not None and target_out >= 0 and target_out < len(devs):
    try:
        sd.check_output_settings(device=target_out, channels=2, samplerate=48000)
        out_48k_ok = True
        checked_out_name = devs[target_out].get('name', '')
    except Exception:
        pass

if not out_48k_ok:
    for idx, d in enumerate(devs):
        if d.get('max_output_channels', 0) >= 2:
            try:
                sd.check_output_settings(device=idx, channels=2, samplerate=48000)
                out_48k_ok = True
                checked_out_name = d.get('name', '')
                break
            except Exception:
                continue

default_in = sd.default.device[0]
target_in = default_in
if target_in is not None and target_in >= 0 and target_in < len(devs):
    ch = min(2, max(1, int(devs[target_in].get('max_input_channels', 1))))
    try:
        sd.check_input_settings(device=target_in, channels=ch, samplerate=48000)
        in_48k_ok = True
        checked_in_name = devs[target_in].get('name', '')
    except Exception:
        pass

print(json.dumps({
    'device_count': len(devs),
    'has_wasapi': wasapi_idx is not None,
    'cable_in_found': len(cable_in_ids) > 0,
    'cable_out_found': len(cable_out_ids) > 0,
    'out_48k_ok': out_48k_ok,
    'in_48k_ok': in_48k_ok,
    'checked_out_name': checked_out_name,
    'checked_in_name': checked_in_name,
}, ensure_ascii=False))
"@
    try {
        $RawJson = & $VenvPython -X utf8 -c $PyCheckCode 2>$null
        if ($LASTEXITCODE -eq 0 -and $RawJson) {
            $SdReport = $RawJson | Select-Object -Last 1 | ConvertFrom-Json
            if ($SdReport.cable_in_found) { $HasCableInput = $true }
            if ($SdReport.cable_out_found) { $HasCableOutput = $true }
            Write-Host "  - sounddevice 识别音频端点总数 : $($SdReport.device_count) 个 (WASAPI 支持: $($SdReport.has_wasapi))" -ForegroundColor Gray
            if ($SdReport.out_48k_ok) {
                $SoundDevice48kOk = $true
                Write-Host "  [通过] 输出端点 48000 Hz 双声道检测通过 (设备: $($SdReport.checked_out_name))。" -ForegroundColor Green
            } else {
                Write-Host "  [警告] 未找到支持 48000 Hz 双声道的可用音频输出设备！" -ForegroundColor Red
            }
            if ($SdReport.in_48k_ok) {
                Write-Host "  [通过] 物理麦克风输入 48000 Hz 检测通过 (设备: $($SdReport.checked_in_name))。" -ForegroundColor Green
            } else {
                Write-Host "  [提示] 默认麦克风输入未通过 48000 Hz 检查（若仅播放音乐不开启人声混音可忽略）。" -ForegroundColor DarkYellow
            }
        } else {
            Write-Host "  [错误] 调用 .venv 执行 sounddevice 检查返回异常。" -ForegroundColor Red
        }
    } catch {
        Write-Host "  [错误] 执行 sounddevice 48000 Hz 检查失败: $($_.Exception.Message)" -ForegroundColor Red
    }
}

# ------------------------------------------------------------
# 5. 输出最终中文诊断结论汇总
# ------------------------------------------------------------
Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "                    中文诊断结论与建议" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan

if ($HasCableInput -and $HasCableOutput) {
    Write-Host "  1. [虚拟声卡就绪] VB-CABLE (CABLE Input / CABLE Output) 已就绪，支持零回声双路隔离推流。" -ForegroundColor Green
} else {
    Write-Host "  1. [需安装虚拟声卡] 当前未安装 VB-CABLE。请手动运行 .\scripts\install_vbcable.ps1 按向导安装，" -ForegroundColor Yellow
    Write-Host "     然后在天刀或模拟器内将麦克风输入设为 CABLE Output，切勿使用立体声混音（会严重串麦回声）。" -ForegroundColor Yellow
}

if ($SoundDevice48kOk) {
    Write-Host "  2. [采样率达标] Python sounddevice 引擎 48000 Hz (48 kHz) 高音质立体声链路验证通过。" -ForegroundColor Green
} else {
    Write-Host "  2. [音频采样率异常] 请检查 Windows 声音控制面板播放设备是否启用并设置为 48000 Hz。" -ForegroundColor Red
}

if ($YtmDirectOk) {
    Write-Host "  3. [网络连通正常] 本机可直接连接 music.youtube.com，控制台代理栏保持留空即可流畅搜歌播歌。" -ForegroundColor Green
} elseif ($YtmProxyOk) {
    Write-Host "  3. [代理连通正常] 直连受限，但经本地代理 http://$($LocalProxyHost):$LocalProxyPort 可正常连接 YouTube Music！" -ForegroundColor Green
    Write-Host "     请在启动控制台后前往【YouTube Music 曲库】页填入代理: http://$($LocalProxyHost):$LocalProxyPort" -ForegroundColor Cyan
} else {
    Write-Host "  3. [网络待配置] 当前直连与本地 127.0.0.1:$LocalProxyPort 均无法访问 music.youtube.com，" -ForegroundColor Yellow
    Write-Host "     请先启动您的代理工具并在控制台配置代理地址后再在线点歌。" -ForegroundColor Yellow
}

if ($GameRunning) {
    Write-Host "  4. [游戏客户端在线] 已检测到天刀/模拟器运行中，启动 .\start.ps1 后可直接框选左下角帮派聊天框！" -ForegroundColor Green
} elseif ($GameInstalled) {
    Write-Host "  4. [游戏客户端已安装] 已识别本机天刀桌面端 ($DefaultWuxiaPath)，开播前启动游戏即可。" -ForegroundColor Cyan
} else {
    Write-Host "  4. [游戏客户端提示] 开播前请先启动天涯明月刀桌面端或安卓模拟器，并进入帮派语音频道。" -ForegroundColor Gray
}

Write-Host ""
Write-Host "  -> 一切准备就绪后，运行 .\start.ps1 即可启动控制台 (http://127.0.0.1:8765)" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""
