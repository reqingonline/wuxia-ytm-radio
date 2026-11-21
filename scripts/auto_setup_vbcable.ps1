<#
.SYNOPSIS
    VB-CABLE 一键自动安装与系统默认声卡防抢占恢复脚本
.DESCRIPTION
    检测 VB-CABLE 是否已安装；未安装时使用 data\vbcable_setup\extracted_Pack46 中的
    官方安装器单次提权安装并重启 Windows 音频服务，随后自动将系统默认播放和录制设备
    恢复为安装前的物理耳机与物理麦克风。
#>
param(
    [switch]$RestoreOnly
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$ScriptDir = $PSScriptRoot
if (-not $ScriptDir) {
    $ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
}
$ProjectRoot = Split-Path -Parent $ScriptDir
$SetupDir = Join-Path $ProjectRoot "data\vbcable_setup"
$ExtractDir = Join-Path $SetupDir "extracted_Pack46"
$LogPath = Join-Path $SetupDir "install_result.txt"

New-Item -ItemType Directory -Force -Path $SetupDir | Out-Null

$AudioPolicyCs = @'
using System;
using System.Runtime.InteropServices;

public static class WinAudioPolicyHelper {
    [ComImport, Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")]
    private class MMDeviceEnumerator {}

    [Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IMMDeviceEnumerator {
        int EnumAudioEndpoints(int dataFlow, int dwStateMask, out IMMDeviceCollection ppDevices);
        int GetDefaultAudioEndpoint(int dataFlow, int role, out IMMDevice ppEndpoint);
    }

    [Guid("0BD7A1BE-7A1A-44DB-8397-CC5392387B5E"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IMMDeviceCollection {
        int GetCount(out int pcDevices);
        int Item(int nDevice, out IMMDevice ppDevice);
    }

    [Guid("D666063F-1587-4E43-81F1-B948E807363F"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IMMDevice {
        int Activate(ref Guid iid, int dwClsCtx, IntPtr pActivationParams, [MarshalAs(UnmanagedType.IUnknown)] out object ppInterface);
        int OpenPropertyStore(int stgmAccess, out IPropertyStore ppProperties);
        int GetId([MarshalAs(UnmanagedType.LPWStr)] out string ppstrId);
    }

    [Guid("886d8eeb-8cf2-4446-8d02-cdba1dbdcf99"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IPropertyStore {
        int GetCount(out int cProps);
        int GetAt(int iProp, out PROPERTYKEY pkey);
        int GetValue(ref PROPERTYKEY key, out PROPVARIANT pv);
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct PROPERTYKEY {
        public Guid fmtid;
        public int pid;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct PROPVARIANT {
        public ushort vt;
        public ushort r1, r2, r3;
        public IntPtr p1;
        public IntPtr p2;
    }

    [ComImport, Guid("870AF99C-171D-4F9E-AF0D-E63DF40C2BC9")]
    private class PolicyConfigClient {}

    [Guid("F8679F50-850A-41CF-9C72-430F290290C8"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IPolicyConfig {
        int GetMixFormat(string pszDeviceName, IntPtr ppFormat);
        int GetDeviceFormat(string pszDeviceName, int bDefault, IntPtr ppFormat);
        int ResetDeviceFormat(string pszDeviceName);
        int SetDeviceFormat(string pszDeviceName, IntPtr pEndpointFormat, IntPtr mixFormat);
        int GetProcessingPeriod(string pszDeviceName, int bDefault, IntPtr pmftDefaultPeriod, IntPtr pmftMinimumPeriod);
        int SetProcessingPeriod(string pszDeviceName, IntPtr pmftPeriod);
        int GetShareMode(string pszDeviceName, IntPtr pMode);
        int SetShareMode(string pszDeviceName, IntPtr mode);
        int GetPropertyValue(string pszDeviceName, int bFxStore, IntPtr key, IntPtr pv);
        int SetPropertyValue(string pszDeviceName, int bFxStore, IntPtr key, IntPtr pv);
        int SetDefaultEndpoint([MarshalAs(UnmanagedType.LPWStr)] string pszDeviceName, int role);
        int SetEndpointVisibility([MarshalAs(UnmanagedType.LPWStr)] string pszDeviceName, int bVisible);
    }

    [DllImport("ole32.dll")]
    private static extern int PropVariantClear(ref PROPVARIANT pvar);

    private static string GetName(IMMDevice dev) {
        IPropertyStore store;
        dev.OpenPropertyStore(0, out store);
        var key = new PROPERTYKEY { fmtid = new Guid("a45c254e-df1c-4efd-8020-67d146a850e0"), pid = 14 };
        PROPVARIANT pv;
        store.GetValue(ref key, out pv);
        string name = "";
        if (pv.vt == 31) {
            if (pv.p1 != IntPtr.Zero) {
                name = Marshal.PtrToStringUni(pv.p1);
            }
        }
        PropVariantClear(ref pv);
        return name ?? "";
    }

    public static string GetDefaultEndpointId(int flow) {
        var enumObj = (IMMDeviceEnumerator)new MMDeviceEnumerator();
        IMMDevice dev;
        int hr = enumObj.GetDefaultAudioEndpoint(flow, 0, out dev);
        if (hr != 0 || dev == null) return "";
        string id;
        dev.GetId(out id);
        return id ?? "";
    }

    public static string GetDefaultEndpointName(int flow) {
        var enumObj = (IMMDeviceEnumerator)new MMDeviceEnumerator();
        IMMDevice dev;
        int hr = enumObj.GetDefaultAudioEndpoint(flow, 0, out dev);
        if (hr != 0 || dev == null) return "";
        return GetName(dev);
    }

    public static bool SetDefaultById(string deviceId) {
        if (string.IsNullOrEmpty(deviceId)) return false;
        var pc = (IPolicyConfig)new PolicyConfigClient();
        int hr0 = pc.SetDefaultEndpoint(deviceId, 0);
        int hr1 = pc.SetDefaultEndpoint(deviceId, 1);
        int hr2 = pc.SetDefaultEndpoint(deviceId, 2);
        return (hr0 == 0 || hr1 == 0 || hr2 == 0);
    }

    public static string SetDefaultByKeyword(int flow, string includeRegex, string excludeRegex) {
        var enumObj = (IMMDeviceEnumerator)new MMDeviceEnumerator();
        IMMDeviceCollection col;
        enumObj.EnumAudioEndpoints(flow, 1, out col);
        int count;
        col.GetCount(out count);
        var inc = new System.Text.RegularExpressions.Regex(includeRegex, System.Text.RegularExpressions.RegexOptions.IgnoreCase);
        var exc = string.IsNullOrEmpty(excludeRegex) ? null : new System.Text.RegularExpressions.Regex(excludeRegex, System.Text.RegularExpressions.RegexOptions.IgnoreCase);
        for (int i = 0; i < count; i++) {
            IMMDevice dev;
            col.Item(i, out dev);
            string name = GetName(dev);
            if (inc.IsMatch(name)) {
                bool blocked = (exc != null) ? exc.IsMatch(name) : false;
                if (!blocked) {
                    string id;
                    dev.GetId(out id);
                    var pc = (IPolicyConfig)new PolicyConfigClient();
                    pc.SetDefaultEndpoint(id, 0);
                    pc.SetDefaultEndpoint(id, 1);
                    pc.SetDefaultEndpoint(id, 2);
                    return name;
                }
            }
        }
        return "";
    }
}
'@

$ComReady = $false
try {
    Add-Type -TypeDefinition $AudioPolicyCs -ErrorAction Stop
    $ComReady = $true
} catch {
    $ComReady = $false
    Write-Host "[警告] 音频策略 COM 组件加载失败: $($_.Exception.Message)" -ForegroundColor Yellow
}

$OrigPlayId = ""
$OrigPlayName = ""
$OrigCapId = ""
$OrigCapName = ""
if ($ComReady) {
    try {
        $OrigPlayId = [WinAudioPolicyHelper]::GetDefaultEndpointId(0)
        $OrigPlayName = [WinAudioPolicyHelper]::GetDefaultEndpointName(0)
        $OrigCapId = [WinAudioPolicyHelper]::GetDefaultEndpointId(1)
        $OrigCapName = [WinAudioPolicyHelper]::GetDefaultEndpointName(1)
        Write-Host "[记录] 当前默认播放: $OrigPlayName" -ForegroundColor Gray
        Write-Host "[记录] 当前默认录制: $OrigCapName" -ForegroundColor Gray
    } catch {
        Write-Host "[提示] 读取默认音频端点异常: $($_.Exception.Message)" -ForegroundColor Yellow
    }
}

$ExistingCable = @()
try {
    $ExistingCable = @(Get-PnpDevice -Class AudioEndpoint -Status OK -ErrorAction SilentlyContinue | Where-Object {
        $_.FriendlyName -match "CABLE Input|CABLE Output|VB-Audio"
    })
} catch {
    $ExistingCable = @()
}

if (-not $RestoreOnly -and $ExistingCable.Count -lt 2) {
    if (-not (Test-Path -LiteralPath $ExtractDir)) {
        $ZipPath = Join-Path $SetupDir "VBCABLE_Driver_Pack46.zip"
        if (-not (Test-Path -LiteralPath $ZipPath)) {
            [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
            Invoke-WebRequest -Uri "https://download.vb-audio.com/Download_CABLE/VBCABLE_Driver_Pack46.zip" -OutFile $ZipPath -UseBasicParsing -TimeoutSec 45
        }
        New-Item -ItemType Directory -Force -Path $ExtractDir | Out-Null
        Expand-Archive -LiteralPath $ZipPath -DestinationPath $ExtractDir -Force
    }

    $SetupExe = Join-Path $ExtractDir "VBCABLE_Setup_x64.exe"
    $InfFile = Join-Path $ExtractDir "vbMmeCable64_win10.inf"
    if (-not (Test-Path -LiteralPath $SetupExe) -and -not (Test-Path -LiteralPath $InfFile)) {
        Write-Host "[错误] 未找到 VBCABLE_Setup_x64.exe 或 vbMmeCable64_win10.inf" -ForegroundColor Red
        exit 2
    }

    $ElevatedScript = Join-Path $SetupDir "elevated_install.ps1"
    $EscapedExtract = $ExtractDir.Replace("'", "''")
    $EscapedSetup = $SetupExe.Replace("'", "''")
    $EscapedInf = $InfFile.Replace("'", "''")
    $EscapedLog = $LogPath.Replace("'", "''")

    $ElevatedBody = @"
`$ErrorActionPreference = 'Continue'
`$log = '$EscapedLog'
"START `$(Get-Date -Format o)" | Out-File -FilePath `$log -Encoding utf8
`$proc = Start-Process -FilePath '$EscapedSetup' -ArgumentList '-i','-h' -WorkingDirectory '$EscapedExtract' -Wait -PassThru
"SETUP_EXIT=`$(`$proc.ExitCode)" | Out-File -FilePath `$log -Append -Encoding utf8
Start-Sleep -Seconds 2
`$eps = @(Get-PnpDevice -Class AudioEndpoint -Status OK -ErrorAction SilentlyContinue | Where-Object { `$_.FriendlyName -match 'CABLE Input|CABLE Output|VB-Audio' })
if (`$eps.Count -lt 2 -and (Test-Path -LiteralPath '$EscapedInf')) {
    `$pnpOut = & pnputil.exe /add-driver '$EscapedInf' /install 2>&1 | Out-String
    "PNPUTIL:`n`$pnpOut" | Out-File -FilePath `$log -Append -Encoding utf8
}
try {
    Restart-Service Audiosrv -Force -ErrorAction Stop
    "AUDIOSRV_RESTARTED=OK" | Out-File -FilePath `$log -Append -Encoding utf8
} catch {
    "AUDIOSRV_RESTART_ERR=`$(`$_.Exception.Message)" | Out-File -FilePath `$log -Append -Encoding utf8
}
Start-Sleep -Seconds 2
`$epsAfter = @(Get-PnpDevice -Class AudioEndpoint -Status OK -ErrorAction SilentlyContinue | Where-Object { `$_.FriendlyName -match 'CABLE Input|CABLE Output|VB-Audio' })
"ENDPOINTS_FOUND=`$(`$epsAfter.Count)" | Out-File -FilePath `$log -Append -Encoding utf8
"@

    $Utf8Bom = New-Object System.Text.UTF8Encoding($true)
    [System.IO.File]::WriteAllText($ElevatedScript, $ElevatedBody, $Utf8Bom)

    Write-Host "[提示] 即将请求一次 Windows UAC 管理员权限以安装 VB-CABLE 虚拟声卡..." -ForegroundColor Cyan
    try {
        $RunProc = Start-Process -FilePath "powershell.exe" -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $ElevatedScript) -Verb RunAs -Wait -PassThru
        Write-Host "[完成] 提权安装脚本结束，退出码: $($RunProc.ExitCode)" -ForegroundColor Green
    } catch {
        Write-Host "[UAC_CANCELLED] 用户取消了管理员授权或未能提权: $($_.Exception.Message)" -ForegroundColor Yellow
    }
    Start-Sleep -Seconds 3
} else {
    Write-Host "[跳过] 检测到 VB-CABLE 已安装或以 -RestoreOnly 模式运行。" -ForegroundColor Green
}

if ($ComReady) {
    try {
        $CurPlay = [WinAudioPolicyHelper]::GetDefaultEndpointName(0)
        $CurCap = [WinAudioPolicyHelper]::GetDefaultEndpointName(1)
        if (($CurPlay -match "CABLE|VB-Audio") -or ($CurPlay -eq "" -and $OrigPlayId)) {
            $RestoredPlay = $false
            if ($OrigPlayId -and ($OrigPlayName -notmatch "CABLE|VB-Audio")) {
                $RestoredPlay = [WinAudioPolicyHelper]::SetDefaultById($OrigPlayId)
            }
            if (-not $RestoredPlay) {
                $NamePlay = [WinAudioPolicyHelper]::SetDefaultByKeyword(0, "Realtek|扬声器|Speaker|Headphone|耳机", "CABLE|VB-Audio")
                Write-Host "[恢复播放] 已将系统默认输出切回: $NamePlay" -ForegroundColor Green
            } else {
                Write-Host "[恢复播放] 已将系统默认输出切回原设备: $OrigPlayName" -ForegroundColor Green
            }
        } else {
            Write-Host "[保持播放] 当前系统默认输出正常: $CurPlay" -ForegroundColor Green
        }

        if (($CurCap -match "CABLE|VB-Audio") -or ($CurCap -eq "" -and $OrigCapId)) {
            $RestoredCap = $false
            if ($OrigCapId -and ($OrigCapName -notmatch "CABLE|VB-Audio")) {
                $RestoredCap = [WinAudioPolicyHelper]::SetDefaultById($OrigCapId)
            }
            if (-not $RestoredCap) {
                $NameCap = [WinAudioPolicyHelper]::SetDefaultByKeyword(1, "麦克风|Microphone|Mic|英特尔|Realtek", "CABLE|VB-Audio|立体声混音|Stereo Mix")
                Write-Host "[恢复录制] 已将系统默认麦克风切回: $NameCap" -ForegroundColor Green
            } else {
                Write-Host "[恢复录制] 已将系统默认麦克风切回原设备: $OrigCapName" -ForegroundColor Green
            }
        } else {
            Write-Host "[保持录制] 当前系统默认麦克风正常: $CurCap" -ForegroundColor Green
        }
    } catch {
        Write-Host "[警告] 恢复默认声卡时出错: $($_.Exception.Message)" -ForegroundColor Yellow
    }
}

$FinalCable = @(Get-PnpDevice -Class AudioEndpoint -Status OK -ErrorAction SilentlyContinue | Where-Object {
    $_.FriendlyName -match "CABLE Input|CABLE Output|VB-Audio"
})
Write-Host "VBCABLE_ENDPOINT_COUNT=$($FinalCable.Count)"
foreach ($Ep in $FinalCable) {
    Write-Host "  - $($Ep.FriendlyName)"
}
