<#
.SYNOPSIS
    VB-CABLE 官方虚拟声卡交互式下载与安装向导
.DESCRIPTION
    带安全确认提示，用户主动确认后从 VB-Audio 官方源下载 VBCABLE_Driver_Pack46.zip
    （若失败自动回退 VBCABLE_Driver_Pack43.zip）至 data\vbcable_setup\，
    解压后通过管理员权限 UAC 启动 VBCABLE_Setup_x64.exe，并提示音频服务重载或重启。
#>
param()

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$ScriptDir = $PSScriptRoot
if (-not $ScriptDir) {
    $ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
}
$ProjectRoot = Split-Path -Parent $ScriptDir
$SetupDir = Join-Path $ProjectRoot "data\vbcable_setup"
$TrashDir = Join-Path $ProjectRoot "_待删除"

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "   VB-CABLE 虚拟声卡官方驱动 — 交互式安装向导" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "【为什么需要安装 VB-CABLE？】" -ForegroundColor Yellow
Write-Host "  1. 彻底杜绝回声串麦：若使用传统立体声混音，游戏音效和帮派队友说话声"
Write-Host "     会被再次录进麦克风推回帮派语音，造成严重回声啸叫。"
Write-Host "  2. 双路物理隔离：VB-CABLE 会创建一对纯数字虚拟通道："
Write-Host "     [点歌台推流] -> CABLE Input ===> CABLE Output -> [天刀/模拟器麦克风]"
Write-Host "     而游戏声音和本地监听走您的物理耳机，两路完全隔离，0 回声、0 串麦。"
Write-Host ""

# 检测是否已安装 VB-CABLE
$ExistingCable = @()
try {
    $Endpoints = Get-PnpDevice -Class AudioEndpoint -Status OK -ErrorAction SilentlyContinue
    if ($Endpoints) {
        $ExistingCable = @($Endpoints | Where-Object { $_.FriendlyName -match "CABLE Input|CABLE Output|VB-Audio Virtual Cable" })
    }
} catch {
    $ExistingCable = @()
}

if ($ExistingCable.Count -gt 0) {
    Write-Host "[检测结果] 系统中已检测到以下 VB-CABLE 音频端点：" -ForegroundColor Green
    foreach ($Ep in $ExistingCable) {
        Write-Host "  - $($Ep.FriendlyName)" -ForegroundColor Green
    }
    Write-Host "如果您希望重新安装或修复驱动，仍可继续执行本向导。" -ForegroundColor Gray
    Write-Host ""
} else {
    Write-Host "[检测结果] 当前系统尚未检测到 CABLE Input / CABLE Output 虚拟声卡。" -ForegroundColor DarkYellow
    Write-Host ""
}

Write-Host "【安全说明】" -ForegroundColor Cyan
Write-Host "  - 官方主下载地址 : https://download.vb-audio.com/Download_CABLE/VBCABLE_Driver_Pack46.zip"
Write-Host "  - 官方备用地址   : https://download.vb-audio.com/Download_CABLE/VBCABLE_Driver_Pack43.zip"
Write-Host "  - 本地保存目录   : $SetupDir"
Write-Host "  - 解压后将请求 Windows UAC 管理员权限打开官方安装界面 VBCABLE_Setup_x64.exe。"
Write-Host ""

$UserConfirm = Read-Host "确认现在下载并启动 VB-CABLE 官方安装向导吗？请输入 Y 确认继续（其他任意键取消）"
if ($UserConfirm -ne "Y" -and $UserConfirm -ne "y") {
    Write-Host ""
    Write-Host "[已取消] 未执行下载或驱动安装操作。" -ForegroundColor Yellow
    exit 0
}

New-Item -ItemType Directory -Force -Path $SetupDir | Out-Null
New-Item -ItemType Directory -Force -Path $TrashDir | Out-Null

# 启用 TLS 1.2 保障 PowerShell 5.1 HTTPS 下载兼容性
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$PrimaryUrl = "https://download.vb-audio.com/Download_CABLE/VBCABLE_Driver_Pack46.zip"
$FallbackUrl = "https://download.vb-audio.com/Download_CABLE/VBCABLE_Driver_Pack43.zip"

$PrimaryZip = Join-Path $SetupDir "VBCABLE_Driver_Pack46.zip"
$FallbackZip = Join-Path $SetupDir "VBCABLE_Driver_Pack43.zip"
$DownloadedZip = $null
$PackTag = ""

Write-Host ""
Write-Host "[1/3] 正在从官方主源下载 VBCABLE_Driver_Pack46.zip ..." -ForegroundColor Cyan
try {
    if (Test-Path -LiteralPath $PrimaryZip) {
        $Stamp = Get-Date -Format "yyyyMMdd_HHmmss"
        $ArchivePath = Join-Path $TrashDir "VBCABLE_Driver_Pack46_$Stamp.zip"
        Move-Item -LiteralPath $PrimaryZip -Destination $ArchivePath -Force
    }
    Invoke-WebRequest -Uri $PrimaryUrl -OutFile $PrimaryZip -UseBasicParsing -TimeoutSec 45
    if ((Test-Path -LiteralPath $PrimaryZip) -and ((Get-Item -LiteralPath $PrimaryZip).Length -gt 102400)) {
        $DownloadedZip = $PrimaryZip
        $PackTag = "Pack46"
        Write-Host "[成功] 已下载 VBCABLE_Driver_Pack46.zip ($(([math]::Round((Get-Item -LiteralPath $PrimaryZip).Length / 1KB, 1))) KB)" -ForegroundColor Green
    } else {
        throw "下载文件体积异常"
    }
} catch {
    Write-Host "[提示] 主源 Pack46 下载未成功 ($($_.Exception.Message))，正在切换备用包 VBCABLE_Driver_Pack43.zip ..." -ForegroundColor Yellow
    try {
        if (Test-Path -LiteralPath $FallbackZip) {
            $Stamp = Get-Date -Format "yyyyMMdd_HHmmss"
            $ArchivePath = Join-Path $TrashDir "VBCABLE_Driver_Pack43_$Stamp.zip"
            Move-Item -LiteralPath $FallbackZip -Destination $ArchivePath -Force
        }
        Invoke-WebRequest -Uri $FallbackUrl -OutFile $FallbackZip -UseBasicParsing -TimeoutSec 45
        if ((Test-Path -LiteralPath $FallbackZip) -and ((Get-Item -LiteralPath $FallbackZip).Length -gt 102400)) {
            $DownloadedZip = $FallbackZip
            $PackTag = "Pack43"
            Write-Host "[成功] 已下载备用包 VBCABLE_Driver_Pack43.zip ($(([math]::Round((Get-Item -LiteralPath $FallbackZip).Length / 1KB, 1))) KB)" -ForegroundColor Green
        } else {
            throw "备用包下载体积异常"
        }
    } catch {
        Write-Host "[错误] 官方主源与备用源均下载失败: $($_.Exception.Message)" -ForegroundColor Red
        Write-Host "请检查网络连接，或使用浏览器手动下载并解压至: $SetupDir" -ForegroundColor Yellow
        exit 1
    }
}

$ExtractDir = Join-Path $SetupDir "extracted_$PackTag"
New-Item -ItemType Directory -Force -Path $ExtractDir | Out-Null

Write-Host "[2/3] 正在解压驱动安装包至 $ExtractDir ..." -ForegroundColor Cyan
Expand-Archive -LiteralPath $DownloadedZip -DestinationPath $ExtractDir -Force

$SetupExe = Get-ChildItem -LiteralPath $ExtractDir -Filter "VBCABLE_Setup_x64.exe" -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $SetupExe) {
    $SetupExe = Get-ChildItem -LiteralPath $ExtractDir -Filter "VBCABLE_Setup.exe" -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
}

if (-not $SetupExe) {
    Write-Host "[错误] 解压目录中未找到 VBCABLE_Setup_x64.exe！" -ForegroundColor Red
    exit 1
}

Write-Host "[3/3] 即将以管理员权限 (UAC) 启动官方安装器: $($SetupExe.FullName)" -ForegroundColor Cyan
Write-Host "  -> 请在弹出的 Windows 权限确认框点击【是】" -ForegroundColor Yellow
Write-Host "  -> 在 VB-CABLE 窗口中点击【Install Driver】按钮完成驱动安装" -ForegroundColor Yellow
Write-Host ""

try {
    Start-Process -FilePath $SetupExe.FullName -WorkingDirectory $SetupExe.DirectoryName -Verb RunAs -Wait
} catch {
    Write-Host "[提示] 管理员授权被取消或启动安装器异常: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "============================================================" -ForegroundColor Green
Write-Host "   VB-CABLE 安装向导已执行完毕 — 必看后续三步配置" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Green
Write-Host "  1. 【重启或重载音频服务】" -ForegroundColor Cyan
Write-Host "     强烈建议重启一次 Windows 系统，使虚拟声卡 48000 Hz (48 kHz) 通道彻底稳定；"
Write-Host "     若暂不方便重启，可在管理员 PowerShell 中运行以下命令重载 Windows 音频服务："
Write-Host "     Restart-Service Audiosrv -Force" -ForegroundColor White
Write-Host ""
Write-Host "  2. 【切回系统默认耳机与物理麦】" -ForegroundColor Cyan
Write-Host "     驱动刚装完时，Windows 可能会自动把系统默认播放/录制设备切到 CABLE Input/Output。"
Write-Host "     请点击任务栏右下角小喇叭："
Write-Host "     - 将系统默认【输出设备】切回您的物理耳机或扬声器（如 Realtek Audio）"
Write-Host "     - 将系统默认【输入设备】切回您的物理麦克风（如 Intel 智音麦克风阵列）"
Write-Host ""
Write-Host "  3. 【游戏内绑定与体检】" -ForegroundColor Cyan
Write-Host "     - 将天刀桌面端或模拟器的麦克风输入设为 CABLE Output (VB-Audio Virtual Cable)"
Write-Host "     - 运行体检脚本确认全部通过: .\scripts\audio_doctor.ps1" -ForegroundColor White
Write-Host ""
