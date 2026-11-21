from __future__ import annotations

import ctypes
import difflib
import io
import json
import logging
import os
import re
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import mss
import numpy as np
from PIL import Image, ImageDraw, ImageFont

try:
    import win32gui
except ImportError:  # pragma: no cover
    win32gui = None  # type: ignore[assignment]

try:
    import win32ui
except ImportError:  # pragma: no cover
    win32ui = None  # type: ignore[assignment]

try:
    from rapidocr_onnxruntime import RapidOCR
except ImportError:  # pragma: no cover
    RapidOCR = None  # type: ignore[assignment,misc]

logger = logging.getLogger(__name__)

# 游戏及常见安卓模拟器窗口标题候选关键字
GAME_WINDOW_KEYWORDS: tuple[str, ...] = (
    "天涯明月刀",
    "wuxia",
    "MuMu",
    "雷电",
    "LDPlayer",
    "逍遥",
    "夜神",
    "BlueStacks",
    "腾讯手游助手",
)

# 常见硬件型号、编码、技术代号或游戏内非点歌术语黑名单（全部小写匹配）
TECH_AND_NOISE_TOKENS: frozenset[str] = frozenset(
    {
        "utf",
        "utf8",
        "utf-8",
        "gbk",
        "gb2312",
        "ascii",
        "unicode",
        "iso",
        "rtx",
        "gtx",
        "rx",
        "arc",
        "intel",
        "amd",
        "nvidia",
        "cpu",
        "gpu",
        "ram",
        "ssd",
        "nvme",
        "hdd",
        "wifi",
        "wi-fi",
        "wlan",
        "lan",
        "vpn",
        "dns",
        "tcp",
        "udp",
        "http",
        "https",
        "win",
        "win7",
        "win10",
        "win11",
        "windows",
        "linux",
        "mac",
        "macos",
        "ios",
        "android",
        "type-c",
        "usb",
        "usb-c",
        "hdmi",
        "dp",
        "pcie",
        "pci-e",
        "dlss",
        "fsr",
        "gsync",
        "g-sync",
        "freesync",
        "fps",
        "hz",
        "khz",
        "mhz",
        "ghz",
        "mb",
        "gb",
        "tb",
        "kbps",
        "mbps",
        "ping",
        "ms",
        "dps",
        "pve",
        "pvp",
        "gvg",
        "afk",
        "npc",
        "boss",
        "buff",
        "debuff",
        "cd",
        "ot",
        "mt",
        "st",
    }
)

# 明显属于游戏内日常聊天/指挥而非点歌的干扰词
CHAT_NOISE_KEYWORDS: tuple[str, ...] = (
    "坐标",
    "帧率",
    "帧数",
    "集合",
    "速来",
    "进组",
    "组队",
    "申请",
    "副本",
    "话本",
    "开团",
    "帮战",
    "联赛",
    "拍卖",
    "分红",
    "金兰",
    "押镖",
    "劫镖",
    "缉拿",
    "暗杀",
    "悬赏",
    "修为",
    "功力",
    "战力",
    "输出",
    "秒伤",
    "显卡",
    "配置",
    "延迟",
    "掉线",
    "卡顿",
    "闪退",
    "更新",
    "维护",
    "补丁",
    "服务器",
    "排队",
    "频道",
    "分线",
)

# 频道前缀正则：兼容 [帮派]、【帮派】、(帮派)、[帮]、帮派 叶开：、帮派叶开：（OCR 吞空格）等
_CHANNEL_PREFIX_RE = re.compile(
    r"^\s*(?:\d{1,2}:\d{2}(?::\d{2})?\s*)?"
    r"(?:[\[\]【】\(\)（）\{\}\|I1l]*\s*(?:帮派|帮会|帮|联盟|团队|队伍|世界|当前|附近|地图|密语)\s*[\[\]【】\(\)（）\{\}\|I1l]+\s*"
    r"|(?:帮派|帮会|联盟|团队|世界|当前)\s+(?=[^:：\s]{1,14}\s*[:：])"
    r"|(?:帮派|帮会)(?=[^:：\s]{1,14}\s*[:：]))"
)

# 发言人提取正则：匹配 "发言人：内容" 或 "发言人: 内容"
_SPEAKER_RE = re.compile(r"^\s*([^:：\s]{1,16})\s*[:：]\s*(.+)$")

# 切歌指令集合与正则
_SKIP_COMMANDS: frozenset[str] = frozenset(
    {
        "切歌",
        "下一首",
        "下首歌",
        "换歌",
        "#切歌",
        "#下一首",
        "!skip",
        "！skip",
        "/skip",
        "#skip",
        "!切歌",
        "！切歌",
    }
)

# 点歌前缀正则：支持 "点歌：歌名-歌手"、"点歌 歌名-歌手"、"#点歌 ..." 等（用于 both 模式）
_SONG_PREFIX_RE = re.compile(
    r"^\s*[#!！/]*\s*(?:点歌|点曲|点播)\s*[:：;；\s]*(.+)$",
    re.IGNORECASE,
)

# 严格冒号点歌前缀正则（用于 prefix 模式：必须显式带冒号，彻底杜绝 "点歌怎么用"/"点歌台好卡" 等闲聊误触）
_STRICT_SONG_PREFIX_RE = re.compile(
    r"^\s*[#!！/]*\s*(?:点歌|点曲|点播)\s*[:：;；]\s*(.+)$",
    re.IGNORECASE,
)

# 指定切歌/插播前缀正则：支持 "切歌：歌名-歌手"、"切歌:歌名-歌手"、"换歌：..."、"插播：..."
_SKIP_PREFIX_RE = re.compile(
    r"^\s*[#!！/]*\s*(?:切歌|换歌|插播)\s*[:：;；\s]+(.+)$",
    re.IGNORECASE,
)

# 严格冒号切歌前缀正则（用于 prefix 模式：必须显式带冒号）
_STRICT_SKIP_PREFIX_RE = re.compile(
    r"^\s*[#!！/]*\s*(?:切歌|换歌|插播)\s*[:：;；]\s*(.+)$",
    re.IGNORECASE,
)

# 严格冒号指令里 '-' 被宋体误判为 '一' 时的量词防误切黑名单
_MEASURE_WORDS_AFTER_YI: frozenset[str] = frozenset(
    "个首张次下点些直起样定会儿天年岁生世切般步边路句段场篇套份只条颗朵把座栋件位名"
)

# 多行折行拼接：识别指令首行与下条独立发言起始行
_CMD_ROW_START_RE = re.compile(
    r"^\s*(?:[\[\]【】][^\]】]{1,8}[\]】]\s*)?(?:[^:：\s]{1,12}\s*[:：]\s*)?[#！!/]*(?:点歌|点曲|点播|切歌|换歌|插播)\s*[:：;；]",
    re.IGNORECASE,
)
_NEXT_ROW_BLOCK_START_RE = re.compile(
    r"^\s*(?:[\[\]【】\(\)（）]|蘭|Lan\b|[#!！/]*(?:点歌|点曲|点播|切歌|换歌|插播)\s*[:：;；])",
    re.IGNORECASE,
)

# 歌名-歌手分隔符正则（连字符、破折号、波浪号等）
_SONG_SEP_RE = re.compile(r"\s*[-—–－―~～]+\s*")

# 纯数字/坐标/比分/时间/等级/帧率区间正则
_NUMERIC_OR_RANGE_RE = re.compile(
    r"^(?:\d+|\d+\s*[-—–－:/~～vV×xX]\s*\d+)\s*(?:级|线|帧|分|秒|人|层|号|服|区|w|W|万|k|K|fps|FPS|hz|Hz|ms|MS)?$"
)

# 文本内含数字区间或硬件型号代号（如 123-456、60-120、RTX-4060、UTF-8、Win-11、i7-13700K）
_INLINE_COORD_OR_MODEL_RE = re.compile(
    r"(?:\d+\s*[-—–－]\s*\d+|[A-Za-z]{1,10}\d*\s*[-—–－]\s*\d+[A-Za-z0-9]*|\d+\s*[-—–－]\s*[A-Za-z]{1,10})",
    re.IGNORECASE,
)

_CHINESE_CHAR_RE = re.compile(r"[\u4e00-\u9fff]")
_ASCII_ONLY_RE = re.compile(r"^[\x00-\x7f]+$")
_SENTENCE_PUNCT_RE = re.compile(r"[，。？！；、,.?!;…“”‘’\"'（）()\[\]【】<>《》]")
_STRICT_PREFIX_SENTENCE_PUNCT_RE = re.compile(r"[。；;]")


def _load_chat_font(font_size: int = 22) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """优先加载 Windows 微软雅黑 msyh.ttc，回退 simhei.ttf 或默认字体。"""
    font_candidates = (
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/msyhbd.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "C:/Windows/Fonts/simsun.ttc",
        "C:/Windows/Fonts/Deng.ttf",
    )
    for path in font_candidates:
        try:
            return ImageFont.truetype(path, font_size)
        except Exception:
            continue
    return ImageFont.load_default()


def render_synthetic_chat_image(
    messages: list[str],
    width: int = 640,
    height: int = 360,
) -> np.ndarray:
    """在深色半透明风格背景图上清晰绘制帮派聊天消息，返回 RGB uint8 np.ndarray。"""
    width = max(320, int(width))
    height = max(160, int(height))

    img = Image.new("RGB", (width, height), (18, 22, 30))
    draw = ImageDraw.Draw(img)

    # 绘制顶部与外框微弱半透明装饰边（避开文字区域，确保 RapidOCR 零干扰）
    draw.rectangle([(4, 4), (width - 5, height - 5)], outline=(42, 52, 68), width=1)
    draw.rectangle([(6, 6), (width - 7, height - 7)], fill=(20, 25, 34))

    if not messages:
        return np.asarray(img, dtype=np.uint8)

    num_msgs = len(messages)
    usable_height = height - 28
    line_step = min(48, max(24, usable_height // max(1, num_msgs)))
    base_font_size = min(23, max(15, int(line_step * 0.56)))

    font = _load_chat_font(base_font_size)
    # 若某行文字过长超出宽度，自动微调字号确保完整绘制不裁切
    max_text_width = width - 36
    for sz in range(base_font_size, 13, -1):
        candidate_font = _load_chat_font(sz)
        fits = True
        for msg in messages:
            bbox = draw.textbbox((0, 0), str(msg), font=candidate_font)
            if (bbox[2] - bbox[0]) > max_text_width:
                fits = False
                break
        if fits:
            font = candidate_font
            break

    y_cursor = 16
    for msg in messages:
        text = str(msg).strip()
        if not text:
            continue
        # 将容易与汉字“一”混淆的过窄连字符两侧保持清晰对比度绘制
        draw.text((18, y_cursor), text, fill=(242, 246, 252), font=font)
        y_cursor += line_step
        if y_cursor > height - 18:
            break

    return np.asarray(img, dtype=np.uint8)


class OcrChatWatcher:
    """天刀帮派聊天框视觉识别与防误触点歌监控器（100% 内存截图与推理）。"""

    _ocr_singleton: Any = None
    _ocr_singleton_lock = threading.Lock()

    def __init__(
        self,
        on_command: Callable[[dict[str, Any]], None] | None = None,
        *,
        enabled: bool = False,
        roi: dict[str, int] | None = None,
        interval_sec: float = 2.0,
        trigger_mode: str = "both",
        cooldown_sec: float = 300.0,
        allow_skip_cmd: bool = True,
        auto_track_window: bool = True,
        peak_schedule: dict[str, Any] | None = None,
        seen_store_path: str | Path | None = None,
        **kwargs: Any,
    ) -> None:
        self.on_command = on_command
        self._config_lock = threading.RLock()
        self._dedup_lock = threading.RLock()

        self.enabled: bool = bool(enabled)
        self.roi: dict[str, int] | None = self._sanitize_roi(roi)
        self.interval_sec: float = max(0.5, float(interval_sec))
        self.trigger_mode: str = "prefix" if trigger_mode == "prefix" else "both"
        self.cooldown_sec: float = max(0.0, float(cooldown_sec))
        self.allow_skip_cmd: bool = bool(allow_skip_cmd)
        self.auto_track_window: bool = bool(auto_track_window)
        self.peak_schedule: dict[str, Any] | None = self._sanitize_peak_schedule(peak_schedule)

        # 冷却去重、静止屏常驻账本、24h 上下文持久化台账
        self._seen_store_path: Path | None = Path(seen_store_path) if seen_store_path else None
        self._song_cooldowns: dict[str, float] = {}
        self._skip_last_ts: float = 0.0
        self._recent_line_history: deque[tuple[str, str, float]] = deque(maxlen=50)
        self._prev_visible_counts: dict[str, int] = {}
        self._seen_context_ledger: deque[tuple[str, str, float]] = deque(maxlen=300)
        self._load_seen_ledger()

        # 最近 30 条识别日志与统计状态
        self.recent_logs: list[dict[str, Any]] = []
        self._last_raw_lines: list[str] = []
        self._last_scan_at: float | None = None
        self._last_elapsed_ms: float = 0.0
        self._total_scans: int = 0
        self._total_commands: int = 0
        self._last_error: str | None = None

        self._stop_event = threading.Event()
        self._wakeup_event = threading.Event()
        self._worker_thread: threading.Thread | None = None

        if self.enabled:
            self.start()

    @classmethod
    def _get_ocr_engine(cls) -> Any:
        """线程安全地懒加载 RapidOCR 单例。"""
        if cls._ocr_singleton is None:
            with cls._ocr_singleton_lock:
                if cls._ocr_singleton is None:
                    if RapidOCR is None:
                        raise RuntimeError("RapidOCR (rapidocr_onnxruntime) 未安装或不可用")
                    cls._ocr_singleton = RapidOCR()
        return cls._ocr_singleton

    @staticmethod
    def _sanitize_roi(roi: dict[str, Any] | None) -> dict[str, int] | None:
        if not isinstance(roi, dict):
            return None
        try:
            left = int(roi.get("left", 0))
            top = int(roi.get("top", 0))
            width = int(roi.get("width", 0))
            height = int(roi.get("height", 0))
        except (TypeError, ValueError):
            return None
        if width <= 10 or height <= 10:
            return None
        return {"left": left, "top": top, "width": width, "height": height}

    def _load_seen_ledger(self) -> None:
        """从磁盘加载 24h 内已入队的聊天指令上下文台账（防止重启或关开聊天框重复点歌）。"""
        if self._seen_store_path is None or not self._seen_store_path.exists():
            return
        try:
            raw = json.loads(self._seen_store_path.read_text(encoding="utf-8"))
            if not isinstance(raw, list):
                return
            now_ts = time.time()
            with self._dedup_lock:
                self._seen_context_ledger.clear()
                for entry in raw[-300:]:
                    if not isinstance(entry, dict):
                        continue
                    ts = float(entry.get("ts", 0.0) or 0.0)
                    if (now_ts - ts) >= 86400.0:
                        continue
                    ctx_val = str(entry.get("ctx", ""))
                    cmd_val = str(entry.get("cmd", ""))
                    if cmd_val:
                        self._seen_context_ledger.append((ctx_val, cmd_val, ts))
        except Exception as exc:
            logger.debug("Failed to load OCR seen ledger: %s", exc)

    def _save_seen_ledger(self) -> None:
        """原子持久化 24h 已识别上下文台账（禁止删除旧文件，采用 os.replace 替换）。"""
        if self._seen_store_path is None:
            return
        try:
            now_ts = time.time()
            with self._dedup_lock:
                payload = [
                    {"ctx": ctx, "cmd": cmd, "ts": ts}
                    for ctx, cmd, ts in self._seen_context_ledger
                    if (now_ts - ts) < 86400.0
                ]
            self._seen_store_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self._seen_store_path.with_suffix(".tmp")
            tmp_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp_path, self._seen_store_path)
        except Exception as exc:
            logger.debug("Failed to save OCR seen ledger: %s", exc)

    def reset_dedup(self) -> None:
        """清空冷却记录、静止屏计数与滑动窗口历史（供测试或重置状态使用）。"""
        with self._dedup_lock:
            self._song_cooldowns.clear()
            self._skip_last_ts = 0.0
            self._recent_line_history.clear()
            self._prev_visible_counts.clear()
            self._seen_context_ledger.clear()

    def list_windows(self) -> list[dict[str, Any]]:
        """枚举所有可见且有标题的非最小化有效窗口，游戏/模拟器候选窗口置顶。"""
        windows: list[dict[str, Any]] = []
        if win32gui is None:
            return windows

        def _enum_cb(hwnd: int, _extra: Any) -> bool:
            try:
                if not win32gui.IsWindowVisible(hwnd):
                    return True
                if win32gui.IsIconic(hwnd):
                    return True
                title = (win32gui.GetWindowText(hwnd) or "").strip()
                if not title:
                    return True
                left, top, right, bottom = win32gui.GetWindowRect(hwnd)
                width = int(right - left)
                height = int(bottom - top)
                if width <= 40 or height <= 40:
                    return True

                title_lower = title.lower()
                is_candidate = any(kw.lower() in title_lower for kw in GAME_WINDOW_KEYWORDS)
                windows.append(
                    {
                        "hwnd": int(hwnd),
                        "title": title,
                        "left": int(left),
                        "top": int(top),
                        "width": width,
                        "height": height,
                        "is_game_candidate": bool(is_candidate),
                    }
                )
            except Exception:
                pass
            return True

        try:
            win32gui.EnumWindows(_enum_cb, None)
        except Exception as exc:
            logger.warning("EnumWindows failed: %s", exc)

        windows.sort(key=lambda w: (not w["is_game_candidate"], w["title"].lower()))
        return windows

    def capture_monitor_jpeg(
        self,
        monitor_index: int = 1,
        max_width: int = 1600,
    ) -> tuple[bytes, dict[str, int]]:
        """使用 mss 在内存中截取指定显示器画面并按需等比缩放，返回 (jpeg_bytes, meta)。"""
        max_width = max(160, int(max_width))
        try:
            with mss.mss() as sct:
                monitors = sct.monitors
                idx = int(monitor_index)
                if idx < 0 or idx >= len(monitors):
                    idx = 1 if len(monitors) > 1 else 0
                mon = monitors[idx]
                sct_img = sct.grab(mon)
                pil_img = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")
                mon_left = int(mon.get("left", 0))
                mon_top = int(mon.get("top", 0))
        except Exception as exc:
            logger.warning("mss monitor capture fallback triggered: %s", exc)
            arr = render_synthetic_chat_image(
                ["[系统提示] 当前屏幕处于息屏或锁屏状态，亮屏后自动恢复实时捕获"],
                width=1280,
                height=720,
            )
            pil_img = Image.fromarray(arr)
            mon_left, mon_top = 0, 0

        orig_width, orig_height = pil_img.size
        if orig_width > max_width:
            scale = max_width / float(orig_width)
            scaled_width = max_width
            scaled_height = max(1, int(round(orig_height * scale)))
            pil_img = pil_img.resize((scaled_width, scaled_height), Image.Resampling.LANCZOS)
        else:
            scaled_width, scaled_height = orig_width, orig_height

        buf = io.BytesIO()
        pil_img.save(buf, format="JPEG", quality=85)
        meta = {
            "orig_width": int(orig_width),
            "orig_height": int(orig_height),
            "scaled_width": int(scaled_width),
            "scaled_height": int(scaled_height),
            "left": int(mon_left),
            "top": int(mon_top),
        }
        return buf.getvalue(), meta

    @staticmethod
    def _compute_game_window_rel_roi(w_width: int, w_height: int) -> tuple[int, int, int, int]:
        """计算《天涯明月刀·世界》展开态【帮派】聊天面板相对于游戏窗口的 ROI (x, y, w, h)。"""
        rel_left = int(w_width * 0.155)
        rel_top = int(w_height * 0.050)
        rel_width = max(160, int(w_width * 0.300))
        rel_height = max(120, int(w_height * 0.795))
        return rel_left, rel_top, rel_width, rel_height

    def _capture_window_roi_printwindow(
        self,
        hwnd: int,
        rel_left: int,
        rel_top: int,
        rel_width: int,
        rel_height: int,
    ) -> np.ndarray | None:
        """通过 Windows DWM PrintWindow (只读离屏合成) 截取指定窗口局部 ROI，防遮挡且全程不落盘。"""
        if win32gui is None or win32ui is None or not hasattr(ctypes, "windll"):
            return None

        hwnd_int = int(hwnd)
        hwnd_dc = None
        mfc_dc = None
        save_dc = None
        bmp = None
        orig_obj = None

        try:
            left, top, right, bottom = win32gui.GetWindowRect(hwnd_int)
            w = int(right - left)
            h = int(bottom - top)
            if w < 640 or h < 360:
                return None

            hwnd_dc = win32gui.GetWindowDC(hwnd_int)
            mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
            save_dc = mfc_dc.CreateCompatibleDC()
            bmp = win32ui.CreateBitmap()
            bmp.CreateCompatibleBitmap(mfc_dc, w, h)
            orig_obj = save_dc.SelectObject(bmp)

            user32 = ctypes.windll.user32
            res = int(user32.PrintWindow(hwnd_int, save_dc.GetSafeHdc(), 2))
            if res != 1:
                res = int(user32.PrintWindow(hwnd_int, save_dc.GetSafeHdc(), 0))
            if res != 1:
                return None

            bmpinfo = bmp.GetInfo()
            bmp_w = int(bmpinfo.get("bmWidth", w))
            bmp_h = int(bmpinfo.get("bmHeight", h))
            if bmp_w <= 0 or bmp_h <= 0:
                return None
            bmpstr = bmp.GetBitmapBits(True)
            full_img = Image.frombuffer(
                "RGB",
                (bmp_w, bmp_h),
                bmpstr,
                "raw",
                "BGRX",
                0,
                1,
            )

            x0 = max(0, min(bmp_w - 1, int(rel_left)))
            y0 = max(0, min(bmp_h - 1, int(rel_top)))
            x1 = max(x0 + 1, min(bmp_w, x0 + int(rel_width)))
            y1 = max(y0 + 1, min(bmp_h, y0 + int(rel_height)))
            crop_arr = np.asarray(full_img.crop((x0, y0, x1, y1)), dtype=np.uint8)
            if crop_arr.size == 0 or float(np.mean(crop_arr)) < 1.0:
                return None
            return crop_arr
        except Exception as exc:
            logger.debug("PrintWindow capture skipped for hwnd=%s: %s", hwnd, exc)
            return None
        finally:
            if save_dc is not None and orig_obj is not None:
                try:
                    save_dc.SelectObject(orig_obj)
                except Exception:
                    pass
            if bmp is not None:
                try:
                    win32gui.DeleteObject(bmp.GetHandle())
                except Exception:
                    pass
            if save_dc is not None:
                try:
                    save_dc.DeleteDC()
                except Exception:
                    pass
            if mfc_dc is not None:
                try:
                    mfc_dc.DeleteDC()
                except Exception:
                    pass
            if hwnd_dc is not None:
                try:
                    win32gui.ReleaseDC(hwnd_int, hwnd_dc)
                except Exception:
                    pass

    def _resolve_effective_roi(
        self,
        roi: dict[str, Any] | None,
        monitors: list[dict[str, int]],
        *,
        allow_auto_track: bool = True,
    ) -> dict[str, int]:
        """解析有效 ROI；启用窗口跟随时优先动态跟踪天刀/模拟器窗口展开态【帮派】聊天区。"""
        if allow_auto_track and self.auto_track_window:
            try:
                for win in self.list_windows():
                    if not win.get("is_game_candidate"):
                        continue
                    w_width = int(win.get("width", 0))
                    w_height = int(win.get("height", 0))
                    if w_width < 640 or w_height < 360:
                        continue
                    w_left = int(win.get("left", 0))
                    w_top = int(win.get("top", 0))
                    rel_left, rel_top, rel_width, rel_height = self._compute_game_window_rel_roi(
                        w_width, w_height
                    )
                    raw_left = w_left + rel_left
                    raw_top = w_top + rel_top
                    raw_right = raw_left + rel_width
                    raw_bottom = raw_top + rel_height

                    desk = monitors[0] if monitors else {"left": 0, "top": 0, "width": 1920, "height": 1080}
                    d_left = int(desk.get("left", 0))
                    d_top = int(desk.get("top", 0))
                    d_right = d_left + max(640, int(desk.get("width", 1920)))
                    d_bottom = d_top + max(360, int(desk.get("height", 1080)))

                    c_left = max(d_left, raw_left)
                    c_top = max(d_top, raw_top)
                    c_right = min(d_right, raw_right)
                    c_bottom = min(d_bottom, raw_bottom)
                    if (c_right - c_left) > 10 and (c_bottom - c_top) > 10:
                        return {
                            "left": int(c_left),
                            "top": int(c_top),
                            "width": int(c_right - c_left),
                            "height": int(c_bottom - c_top),
                        }
            except Exception:
                pass

        clean_roi = self._sanitize_roi(roi) or self._sanitize_roi(self.roi)
        mon = monitors[1] if len(monitors) > 1 else monitors[0]
        mon_left = int(mon.get("left", 0))
        mon_top = int(mon.get("top", 0))
        mon_w = max(640, int(mon.get("width", 1920)))
        mon_h = max(360, int(mon.get("height", 1080)))

        if clean_roi is not None:
            return clean_roi

        default_w = min(560, max(280, int(mon_w * 0.28)))
        default_h = min(320, max(160, int(mon_h * 0.28)))
        default_left = mon_left + int(mon_w * 0.015)
        default_top = mon_top + mon_h - default_h - int(mon_h * 0.08)
        return {
            "left": int(default_left),
            "top": int(default_top),
            "width": int(default_w),
            "height": int(default_h),
        }

    def _capture_roi_array(
        self,
        roi: dict[str, Any] | None = None,
        *,
        allow_auto_track: bool = True,
        allow_synthetic_fallback: bool = True,
    ) -> np.ndarray | None:
        """在内存中截取 ROI 并返回 RGB uint8 numpy 数组（优先使用只读 DWM PrintWindow 防遮挡，绝不落盘）。"""
        if allow_auto_track and self.auto_track_window:
            try:
                for win in self.list_windows():
                    if not win.get("is_game_candidate"):
                        continue
                    w_width = int(win.get("width", 0))
                    w_height = int(win.get("height", 0))
                    if w_width < 640 or w_height < 360:
                        continue
                    rel_left, rel_top, rel_width, rel_height = self._compute_game_window_rel_roi(
                        w_width, w_height
                    )
                    arr = self._capture_window_roi_printwindow(
                        int(win["hwnd"]),
                        rel_left,
                        rel_top,
                        rel_width,
                        rel_height,
                    )
                    if arr is not None:
                        return arr
            except Exception:
                pass

        clean_roi = self._sanitize_roi(roi) or self._sanitize_roi(self.roi)
        try:
            with mss.mss() as sct:
                target_roi = self._resolve_effective_roi(
                    clean_roi,
                    sct.monitors,
                    allow_auto_track=allow_auto_track,
                )
                sct_img = sct.grab(target_roi)
                pil_img = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")
                return np.asarray(pil_img, dtype=np.uint8)
        except Exception as exc:
            if not allow_synthetic_fallback:
                logger.debug("mss ROI capture skipped (screen off or locked): %s", exc)
                return None
            logger.warning("mss ROI capture fallback triggered: %s", exc)
            fb_w = int(clean_roi["width"]) if clean_roi else 520
            fb_h = int(clean_roi["height"]) if clean_roi else 260
            return render_synthetic_chat_image(
                ["[系统提示] 当前屏幕处于息屏或锁屏状态，亮屏后自动恢复实时捕获"],
                width=fb_w,
                height=fb_h,
            )

    def capture_roi_png(self, roi: dict[str, Any] | None = None) -> bytes:
        """截取指定 ROI（或主屏左下角默认帮派聊天区）并在内存中编码为 PNG 字节流。"""
        arr = self._capture_roi_array(
            roi,
            allow_auto_track=(roi is None),
            allow_synthetic_fallback=True,
        )
        assert arr is not None
        pil_img = Image.fromarray(arr)
        buf = io.BytesIO()
        pil_img.save(buf, format="PNG")
        return buf.getvalue()

    @staticmethod
    def _extract_speaker_and_body(raw_line: str) -> tuple[str, str]:
        """剥离帮派频道前缀与发言人，返回 (requester, message_body)。"""
        text = raw_line.strip()
        if not text:
            return "帮派少侠", ""

        has_channel_tag = bool(_CHANNEL_PREFIX_RE.match(text))
        if has_channel_tag:
            text = _CHANNEL_PREFIX_RE.sub("", text, count=1).strip()

        # 剥离气泡左侧头像边缘被并入的等级数字（如 "14 点歌：晴天-周杰伦"）
        text = re.sub(
            r"^\s*\d{1,3}\s+(?=(?:点歌|点曲|点播|切歌|换歌|插播)\s*[:：;；])",
            "",
            text,
        ).strip()

        speaker_match = _SPEAKER_RE.match(text)
        if speaker_match:
            candidate_speaker = speaker_match.group(1).strip()
            candidate_body = speaker_match.group(2).strip()
            # 清理发言人两侧可能残留的括号
            candidate_speaker = candidate_speaker.strip("[]【】()（）{}|")

            # 避免将 "点歌：晴天-周杰伦" / "切歌：晴天-周杰伦" 或 "坐标: 123-456" 里的指令词当作发言人
            non_speaker_words = {
                "点歌", "#点歌", "!点歌", "！点歌", "点曲", "点播",
                "切歌", "#切歌", "!切歌", "！切歌", "换歌", "插播",
                "坐标", "帧率", "时间",
            }
            if candidate_speaker.lower() not in non_speaker_words and candidate_speaker:
                if has_channel_tag or (
                    len(candidate_speaker) <= 12
                    and not _SONG_SEP_RE.search(candidate_speaker)
                    and not candidate_speaker.isdigit()
                ):
                    return candidate_speaker, candidate_body

        return "帮派少侠", text

    @staticmethod
    def _is_valid_song_and_artist(
        title: str,
        artist: str,
        *,
        has_explicit_prefix: bool,
        full_body: str,
    ) -> bool:
        """严格防误触校验：拒绝坐标/数字区间/硬件型号/ASCII代号/长句闲聊。"""
        title = title.strip()
        artist = artist.strip()

        # 1. 长度约束：显式前缀放宽至歌名 1..48 字、歌手 0..36 字；免前缀维持 24/20 字
        max_title_len = 48 if has_explicit_prefix else 24
        max_artist_len = 36 if has_explicit_prefix else 20
        if not (1 <= len(title) <= max_title_len):
            return False
        if len(artist) > max_artist_len:
            return False

        # 2. 原文或标题/歌手命中聊天干扰词（如 坐标、帧率、集合、速来 等）
        for kw in CHAT_NOISE_KEYWORDS:
            if kw in full_body or kw in title or kw in artist:
                return False

        # 3. 拒绝明显闲聊句末标点；显式前缀模式允许英文歌名/歌手里的 '.' / '\'' / '()'
        punct_re = _STRICT_PREFIX_SENTENCE_PUNCT_RE if has_explicit_prefix else _SENTENCE_PUNCT_RE
        if punct_re.search(title) or punct_re.search(artist):
            return False

        # 4. 拒绝纯数字、坐标、比分、等级区间、时间（如 123-456、3-2、80-90级、10:30、60-120帧）
        #    注：显式前缀下允许数字歌名配真人歌手（如 2002-Anne-Marie / 1989-Taylor Swift）
        artist_has_alpha = bool(artist and re.search(r"[A-Za-z\u4e00-\u9fff]{2,}", artist))
        if _NUMERIC_OR_RANGE_RE.match(title):
            if not (has_explicit_prefix and title.isdigit() and artist_has_alpha):
                return False
        if artist and _NUMERIC_OR_RANGE_RE.match(artist):
            return False
        if _NUMERIC_OR_RANGE_RE.match(full_body):
            return False
        if re.match(r"^\d{1,2}:\d{2}(?::\d{2})?$", full_body):
            return False

        # 5. 免前缀模式下拒绝内联数字区间/硬件型号；显式前缀允许 21 Guns / 1989 等歌名
        if not has_explicit_prefix and _INLINE_COORD_OR_MODEL_RE.search(full_body):
            return False

        # 6. 歌手名不能为纯数字或游戏单位（免前缀模式下也不允许数字开头）
        if artist:
            if artist.isdigit() or (not has_explicit_prefix and artist[0].isdigit()):
                return False
            if re.search(r"\d+\s*(?:级|线|帧|分|秒|hz|fps)\b", artist, re.IGNORECASE):
                return False

        # 7. 歌名不能是数字结尾加空格（如 "坐标 123"、"帧率 60"）或包含闲聊空格结构
        if _CHINESE_CHAR_RE.search(title) and re.search(r"\s+\d+$", title):
            return False
        if not has_explicit_prefix:
            # 非前缀自由模式下，若含中文，则歌名和歌手内部不应出现空格分词（排除 "坐标 123-456 速来" 等句子）
            if _CHINESE_CHAR_RE.search(title) and " " in title:
                return False
            if _CHINESE_CHAR_RE.search(artist) and " " in artist:
                return False

        # 8. 检查硬件型号、编码、技术代号黑名单（如 UTF-8, RTX-4060, Wi-Fi, Win-11）
        combined_lower = f"{title}-{artist}".lower() if artist else title.lower()
        tokens = [t for t in re.split(r"[\s\-—–－_/:：]+", combined_lower) if t]
        for tok in tokens:
            if tok in TECH_AND_NOISE_TOKENS:
                return False
            # 拒绝形如 rtx4060, win11, utf8, i7, gtx1060 的字母+数字编码
            if re.match(r"^[a-z]{1,6}\d{1,5}[a-z0-9]*$", tok):
                return False

        # 9. 拒绝全 ASCII 纯代码/纯数字组合，但显式前缀兼容 21 Guns、Maroon 5、1989-Taylor Swift 等英文歌
        has_chinese = bool(_CHINESE_CHAR_RE.search(title) or _CHINESE_CHAR_RE.search(artist))
        if not has_chinese:
            # 无中文时：在自由模式（无 "点歌" 前缀）下直接拒绝全 ASCII 短横线组合，杜绝 Wi-Fi / UTF-8 等误触
            if not has_explicit_prefix:
                return False
            combined_ascii = f"{title} {artist}".strip()
            if len(title.replace(" ", "")) < 2:
                return False
            if not re.findall(r"[A-Za-z]{2,}", combined_ascii):
                return False
            if title.isdigit() and not (artist and re.findall(r"[A-Za-z]{2,}", artist)):
                return False
            if artist and (artist.isdigit() or not re.findall(r"[A-Za-z0-9]+", artist)):
                return False

        return True

    @staticmethod
    def _normalize_for_similarity(text: str) -> str:
        """标准化聊天行文本供 SequenceMatcher 静止画面去重比对。"""
        s = text.strip().lower()
        s = re.sub(r"\s+", "", s)
        s = s.replace("：", ":").replace("【", "[").replace("】", "]").replace("—", "-").replace("－", "-")
        return s

    @staticmethod
    def _fuzzy_bucket_increment(counts: dict[str, int], norm_raw: str) -> int:
        """统计当前帧中与 norm_raw 相似的出现次序（1-based），兼容 OCR 细微抖动。"""
        for bucket in counts:
            if difflib.SequenceMatcher(None, norm_raw, bucket).ratio() >= 0.85:
                counts[bucket] += 1
                return counts[bucket]
        counts[norm_raw] = 1
        return 1

    @staticmethod
    def _fuzzy_bucket_get(counts: dict[str, int], norm_raw: str) -> int:
        """查询上一帧或常驻账本中与 norm_raw 相似指令的出现次数。"""
        best = 0
        for bucket, cnt in counts.items():
            if difflib.SequenceMatcher(None, norm_raw, bucket).ratio() >= 0.85:
                if cnt > best:
                    best = cnt
        return best

    def _is_duplicate_or_on_cooldown(
        self,
        *,
        cmd_type: str,
        title: str,
        artist: str,
        raw_text: str,
        now_ts: float,
    ) -> bool:
        """检查滑动窗口静止行重复（SequenceMatcher >= 0.85）与单曲冷却去重（cooldown_sec）。"""
        norm_raw = self._normalize_for_similarity(raw_text)
        if cmd_type == "song":
            song_key = f"{title.strip().lower()}|{artist.strip().lower()}"
        elif cmd_type == "skip_to":
            song_key = f"skip_to|{title.strip().lower()}|{artist.strip().lower()}"
        else:
            song_key = "__skip__"

        with self._dedup_lock:
            # 1. 针对歌曲/指定切歌检查 cooldown_sec 冷却时间
            if cmd_type in ("song", "skip_to") and self.cooldown_sec > 0:
                last_ts = self._song_cooldowns.get(song_key)
                if last_ts is not None and (now_ts - last_ts) < self.cooldown_sec:
                    return True

            # 2. 针对静止聊天框残留历史消息：最近 50 条历史使用 SequenceMatcher >= 0.85 过滤
            for prev_norm_raw, prev_key, prev_ts in self._recent_line_history:
                if self.cooldown_sec > 0 and (now_ts - prev_ts) >= max(self.cooldown_sec, 60.0):
                    continue
                ratio = difflib.SequenceMatcher(None, norm_raw, prev_norm_raw).ratio()
                if ratio >= 0.85:
                    if cmd_type == "skip" and prev_key == "__skip__":
                        if (now_ts - prev_ts) < max(self.cooldown_sec, 5.0):
                            return True
                    elif cmd_type in ("song", "skip_to") and prev_key != "__skip__":
                        prev_is_skip_to = prev_key.startswith("skip_to|")
                        if (cmd_type == "skip_to") != prev_is_skip_to:
                            continue
                        prev_parts = prev_key.split("|")
                        prev_title = prev_parts[1] if (prev_is_skip_to and len(prev_parts) >= 2) else prev_parts[0]
                        title_ratio = difflib.SequenceMatcher(
                            None, title.strip().lower(), prev_title
                        ).ratio()
                        if title_ratio >= 0.6:
                            return True

            # 记录到历史与冷却表
            if cmd_type in ("song", "skip_to"):
                self._song_cooldowns[song_key] = now_ts
            else:
                self._skip_last_ts = now_ts
            self._recent_line_history.append((norm_raw, song_key, now_ts))
            return False

    def _extract_song_fields(
        self,
        payload: str,
        *,
        has_explicit_prefix: bool,
        full_body: str,
        require_separator: bool = False,
    ) -> tuple[str, str] | None:
        """从指令载荷中拆分并校验 (歌名, 歌手)，供点歌与切歌插播共用。"""
        clean_payload = payload.strip()
        if not clean_payload:
            return None
        sep_parts = _SONG_SEP_RE.split(clean_payload)
        if len(sep_parts) == 2:
            title_part, artist_part = sep_parts[0].strip(), sep_parts[1].strip()
            if require_separator and (not title_part or not artist_part):
                return None
        elif len(sep_parts) > 2 and has_explicit_prefix:
            # 显式前缀兼容歌名/歌手自带连字符（如 blackpink-DDU-DU DDU-DU、2002-Anne-Marie）
            first_split = _SONG_SEP_RE.split(clean_payload, maxsplit=1)
            title_part, artist_part = first_split[0].strip(), first_split[1].strip()
            if not title_part or not artist_part:
                return None
        elif has_explicit_prefix and clean_payload.count("一") == 1:
            # 显式冒号前缀下，兼容游戏宋体 '-' 被 RapidOCR 误认成单个汉字 '一'（如 "点歌：本兮一情花"）
            left_p, right_p = (p.strip() for p in clean_payload.split("一", 1))
            if (
                1 <= len(left_p) <= 24
                and 1 <= len(right_p) <= 20
                and right_p[0] not in _MEASURE_WORDS_AFTER_YI
            ):
                title_part, artist_part = left_p, right_p
            else:
                return None
        elif len(sep_parts) == 1 and has_explicit_prefix and not require_separator:
            title_part, artist_part = sep_parts[0].strip(), ""
        else:
            return None

        if not self._is_valid_song_and_artist(
            title_part,
            artist_part,
            has_explicit_prefix=has_explicit_prefix,
            full_body=full_body,
        ):
            return None
        return title_part, artist_part

    def _is_seen_in_context_ledger(
        self,
        ctx_norm: str,
        norm_raw: str,
        now_ts: float,
    ) -> bool:
        """Layer 2: 24h 上下文台账去重（覆盖滚屏至顶行无上下文及关闭重开聊天框/重启服务）。"""
        with self._dedup_lock:
            for p_ctx, p_raw, p_ts in self._seen_context_ledger:
                if (now_ts - p_ts) >= 86400.0:
                    continue
                if difflib.SequenceMatcher(None, norm_raw, p_raw).ratio() < 0.85:
                    continue
                # 滚到最顶行或视口上边界扩展导致上文发言人变化/为空时，仍识别为历史旧气泡
                if not ctx_norm or not p_ctx:
                    return True
                if difflib.SequenceMatcher(None, ctx_norm, p_ctx).ratio() >= 0.78:
                    return True
        return False

    def parse_chat_lines(
        self,
        lines: list[str],
        trigger_mode: str = "both",
        allow_skip_cmd: bool = True,
        *,
        bypass_dedup: bool = False,
    ) -> list[dict[str, Any]]:
        """解析 OCR 识别出的多行文本，提取点歌与切歌指令并执行 3 层防重复与防误触过滤。"""
        effective_mode = "prefix" if trigger_mode == "prefix" else "both"
        results: list[dict[str, Any]] = []
        now_ts = time.time()
        current_visible_counts: dict[str, int] = {}
        dirty_ledger = False

        with self._dedup_lock:
            prev_visible_snapshot = dict(self._prev_visible_counts)

        for idx, line in enumerate(lines):
            if not isinstance(line, str):
                continue
            raw_text = line.strip()
            if not raw_text:
                continue

            requester, body = self._extract_speaker_and_body(raw_text)
            body_clean = body.strip()
            if not body_clean:
                continue

            cmd_type = ""
            title_part = ""
            artist_part = ""
            strict_prefix = effective_mode == "prefix"

            # 1. 指定切歌/插播格式："切歌：歌名-歌手"
            skip_re = _STRICT_SKIP_PREFIX_RE if strict_prefix else _SKIP_PREFIX_RE
            skip_prefix_match = skip_re.match(body_clean)
            if skip_prefix_match is not None:
                if not allow_skip_cmd:
                    continue
                fields = self._extract_song_fields(
                    skip_prefix_match.group(1),
                    has_explicit_prefix=True,
                    full_body=body_clean,
                    require_separator=strict_prefix,
                )
                if fields is None:
                    continue
                cmd_type = "skip_to"
                title_part, artist_part = fields
            elif body_clean.lower() in _SKIP_COMMANDS:
                # 2. 裸"切歌"/"下一首"：严格 prefix 模式下禁用，防聊天误触切歌
                if not allow_skip_cmd or strict_prefix:
                    continue
                cmd_type = "skip"
            else:
                # 3. 点歌指令识别：prefix 模式必须 "点歌：歌名-歌手"；both 模式兼容无冒号/免前缀
                song_re = _STRICT_SONG_PREFIX_RE if strict_prefix else _SONG_PREFIX_RE
                prefix_match = song_re.match(body_clean)
                has_explicit_prefix = prefix_match is not None
                if strict_prefix and not has_explicit_prefix:
                    continue
                song_payload = prefix_match.group(1) if prefix_match else body_clean
                fields = self._extract_song_fields(
                    song_payload,
                    has_explicit_prefix=has_explicit_prefix,
                    full_body=body_clean,
                    require_separator=strict_prefix,
                )
                if fields is None:
                    continue
                cmd_type = "song"
                title_part, artist_part = fields

            if not bypass_dedup:
                norm_raw = self._normalize_for_similarity(raw_text)
                occ_idx = self._fuzzy_bucket_increment(current_visible_counts, norm_raw)
                prev_occ = self._fuzzy_bucket_get(prev_visible_snapshot, norm_raw)

                # Layer 1: 静止屏常驻防重（气泡一直挂在屏幕上，哪怕过了冷却期也不再重读）
                if occ_idx <= prev_occ:
                    continue

                # Layer 2: 24h 上下文指纹防重（防关闭重开聊天面板/重启服务重读旧消息）
                ctx_lines = [
                    self._normalize_for_similarity(lines[j])
                    for j in range(max(0, idx - 2), idx)
                    if isinstance(lines[j], str) and lines[j].strip()
                ]
                ctx_norm = "|".join(ctx_lines)
                if self._is_seen_in_context_ledger(ctx_norm, norm_raw, now_ts):
                    continue

                # Layer 3: 单曲冷却与最近历史相似度防抖
                if self._is_duplicate_or_on_cooldown(
                    cmd_type=cmd_type,
                    title=title_part,
                    artist=artist_part,
                    raw_text=raw_text,
                    now_ts=now_ts,
                ):
                    continue

                with self._dedup_lock:
                    self._seen_context_ledger.append((ctx_norm, norm_raw, now_ts))
                dirty_ledger = True

            results.append(
                {
                    "type": cmd_type,
                    "title": title_part,
                    "artist": artist_part,
                    "requester": requester,
                    "raw_text": raw_text,
                    "timestamp": now_ts,
                }
            )

        if not bypass_dedup:
            with self._dedup_lock:
                self._prev_visible_counts = current_visible_counts
            if dirty_ledger:
                self._save_seen_ledger()

        return results

    @staticmethod
    def _prepare_image_for_ocr(img: np.ndarray) -> np.ndarray:
        """规范化输入图像数组，必要时适度放大（1.5x~2x）以提升中文与标点识别准确率。"""
        arr = np.asarray(img)
        if arr.ndim == 2:
            arr = np.stack([arr, arr, arr], axis=-1)
        elif arr.ndim == 3 and arr.shape[2] == 4:
            arr = arr[:, :, :3]
        if arr.dtype != np.uint8:
            arr = np.clip(arr, 0, 255).astype(np.uint8)

        h, w = arr.shape[:2]
        if h < 220 or w < 420:
            scale = 2.0 if (h < 140 or w < 280) else 1.5
            pil_img = Image.fromarray(arr)
            new_w = max(1, int(round(w * scale)))
            new_h = max(1, int(round(h * scale)))
            pil_img = pil_img.resize((new_w, new_h), Image.Resampling.LANCZOS)
            arr = np.asarray(pil_img, dtype=np.uint8)

        # 为边缘气泡保留边距，防止 RapidOCR DBNet 贴边挤压符号（如 '-' 误判为 '一'）
        if arr.size > 0:
            arr = np.pad(arr, ((16, 24), (16, 16), (0, 0)), mode="edge")
        return arr

    @staticmethod
    def _merge_ocr_boxes_into_lines(ocr_result: Any) -> list[str]:
        """按文本框 Y 轴中心聚类同行被空格拆分的检测框，并按 X 轴从左到右拼接为完整聊天行。"""
        if not ocr_result:
            return []

        items: list[dict[str, Any]] = []
        for entry in ocr_result:
            if not entry or len(entry) < 2:
                continue
            box, text = entry[0], str(entry[1] or "").strip()
            if not text:
                continue
            try:
                xs = [float(pt[0]) for pt in box]
                ys = [float(pt[1]) for pt in box]
                x_min = min(xs)
                y_min, y_max = min(ys), max(ys)
                y_center = (y_min + y_max) / 2.0
                box_h = max(8.0, y_max - y_min)
            except Exception:
                x_min, y_min, y_max, y_center, box_h = 0.0, 0.0, 20.0, 0.0, 20.0

            items.append(
                {
                    "text": text,
                    "x_min": x_min,
                    "y_min": y_min,
                    "y_max": y_max,
                    "y_center": y_center,
                    "box_h": box_h,
                }
            )

        if not items:
            return []

        items.sort(key=lambda it: (it["y_center"], it["x_min"]))
        rows: list[list[dict[str, Any]]] = []
        for it in items:
            if not rows:
                rows.append([it])
                continue
            last_row = rows[-1]
            row_y = sum(x["y_center"] for x in last_row) / len(last_row)
            row_h = sum(x["box_h"] for x in last_row) / len(last_row)
            threshold = max(row_h, it["box_h"]) * 0.55
            if abs(it["y_center"] - row_y) <= threshold:
                last_row.append(it)
            else:
                rows.append([it])

        row_objs: list[dict[str, Any]] = []
        for row in rows:
            row.sort(key=lambda it: it["x_min"])
            parts: list[str] = []
            for it in row:
                seg = it["text"]
                if not parts:
                    parts.append(seg)
                else:
                    prev = parts[-1]
                    if prev.endswith((":", "：", "[", "【")) or seg.startswith((":", "：", "]", "】")):
                        parts.append(seg)
                    else:
                        parts.append(" " + seg)
            line_str = "".join(parts).strip()
            if line_str:
                row_objs.append(
                    {
                        "text": line_str,
                        "x_min": min(it["x_min"] for it in row),
                        "y_min": min(it["y_min"] for it in row),
                        "y_max": max(it["y_max"] for it in row),
                        "box_h": sum(it["box_h"] for it in row) / len(row),
                    }
                )

        # 气泡内自动折行（2~3 排长句）无损拼接：仅当首行以点歌/切歌前缀开头，且后续紧贴、左对齐时缝合
        stitched_lines: list[str] = []
        idx = 0
        total_rows = len(row_objs)
        while idx < total_rows:
            cur = dict(row_objs[idx])
            if _CMD_ROW_START_RE.match(cur["text"]):
                stitched_count = 0
                while stitched_count < 2 and (idx + 1) < total_rows:
                    nxt = row_objs[idx + 1]
                    gap = nxt["y_min"] - cur["y_max"]
                    ref_h = max(cur["box_h"], nxt["box_h"])
                    if (
                        -cur["box_h"] * 0.2 <= gap <= ref_h * 0.75
                        and abs(nxt["x_min"] - cur["x_min"]) <= max(50.0, cur["box_h"] * 1.6)
                        and not _NEXT_ROW_BLOCK_START_RE.match(nxt["text"])
                    ):
                        cur["text"] = OcrChatWatcher._join_wrapped_text(cur["text"], nxt["text"])
                        cur["y_max"] = max(cur["y_max"], nxt["y_max"])
                        cur["box_h"] = max(cur["box_h"], nxt["box_h"])
                        idx += 1
                        stitched_count += 1
                    else:
                        break
            stitched_lines.append(cur["text"])
            idx += 1

        return stitched_lines

    @staticmethod
    def _join_wrapped_text(prev: str, nxt: str) -> str:
        """智能拼接同一气泡内折行的前后两排文本（处理英文单词腰斩如 were i + n trouble）。"""
        p = prev.rstrip()
        n = nxt.lstrip()
        if not p:
            return n
        if not n:
            return p
        # 英文小写单词在气泡边缘被腰斩（如 "were i" + "n trouble"）直接无缝拼接
        if re.search(r"[A-Za-z]$", p) and re.match(r"^[a-z]", n):
            return p + n
        if (
            _CHINESE_CHAR_RE.match(p[-1])
            or _CHINESE_CHAR_RE.match(n[0])
            or p[-1] in "-—–－~～:："
            or n[0] in "-—–－~～:："
        ):
            return p + n
        return p + " " + n

    def _run_ocr_and_parse(
        self,
        img: np.ndarray,
        *,
        bypass_dedup: bool = False,
    ) -> tuple[list[str], list[dict[str, Any]], float]:
        """执行内存图像 RapidOCR 推理与聊天指令解析，并写入 recent_logs（保留最近 30 条）。"""
        t0 = time.perf_counter()
        prepared = self._prepare_image_for_ocr(img)
        engine = self._get_ocr_engine()
        ocr_result, _ = engine(prepared)
        raw_lines = self._merge_ocr_boxes_into_lines(ocr_result)

        with self._config_lock:
            trigger_mode = self.trigger_mode
            allow_skip_cmd = self.allow_skip_cmd

        commands = self.parse_chat_lines(
            raw_lines,
            trigger_mode=trigger_mode,
            allow_skip_cmd=allow_skip_cmd,
            bypass_dedup=bypass_dedup,
        )
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        now_ts = time.time()
        time_str = datetime.fromtimestamp(now_ts).strftime("%H:%M:%S")

        cmd_by_raw: dict[str, dict[str, Any]] = {c["raw_text"]: c for c in commands}

        with self._config_lock:
            self._last_raw_lines = list(raw_lines)
            self._last_scan_at = now_ts
            self._last_elapsed_ms = round(elapsed_ms, 2)
            self._total_scans += 1
            self._total_commands += len(commands)
            self._last_error = None

            for rline in raw_lines:
                matched_cmd = cmd_by_raw.get(rline)
                if matched_cmd is not None:
                    if matched_cmd["type"] == "skip":
                        msg_summary = f"[切歌] {matched_cmd['requester']} 触发切歌"
                    elif matched_cmd["type"] == "skip_to":
                        artist_str = f" - {matched_cmd['artist']}" if matched_cmd["artist"] else ""
                        msg_summary = f"[切歌插播] {matched_cmd['requester']}: {matched_cmd['title']}{artist_str}"
                    else:
                        artist_str = f" - {matched_cmd['artist']}" if matched_cmd["artist"] else ""
                        msg_summary = f"[点歌] {matched_cmd['requester']}: {matched_cmd['title']}{artist_str}"
                    log_entry = {
                        "timestamp": now_ts,
                        "time_str": time_str,
                        "raw_text": rline,
                        "matched": True,
                        "type": matched_cmd["type"],
                        "title": matched_cmd["title"],
                        "artist": matched_cmd["artist"],
                        "requester": matched_cmd["requester"],
                        "command": matched_cmd,
                        "message": msg_summary,
                    }
                else:
                    log_entry = {
                        "timestamp": now_ts,
                        "time_str": time_str,
                        "raw_text": rline,
                        "matched": False,
                        "type": "ignored",
                        "title": "",
                        "artist": "",
                        "requester": "",
                        "command": None,
                        "message": f"[忽略] {rline}",
                    }
                self.recent_logs.append(log_entry)

            if len(self.recent_logs) > 30:
                self.recent_logs = self.recent_logs[-30:]

        return raw_lines, commands, round(elapsed_ms, 2)

    def recognize_image_array(
        self,
        img: np.ndarray,
        bypass_dedup: bool = False,
    ) -> list[dict[str, Any]]:
        """对内存图像数组执行 RapidOCR 推理，经 parse_chat_lines 返回解析结果并记录日志。"""
        _, commands, _ = self._run_ocr_and_parse(img, bypass_dedup=bypass_dedup)
        return commands

    def test_once(
        self,
        synthetic_messages: list[str] | None = None,
        *,
        bypass_dedup: bool = False,
    ) -> dict[str, Any]:
        """若传 synthetic_messages 则生成合成聊天图实测 OCR，否则截取当前 ROI 实测。"""
        if synthetic_messages is not None and len(synthetic_messages) > 0:
            img = render_synthetic_chat_image(synthetic_messages)
        else:
            img = self._capture_roi_array(self.roi)

        raw_lines, commands, elapsed_ms = self._run_ocr_and_parse(
            img,
            bypass_dedup=bypass_dedup,
        )
        return {
            "raw_lines": raw_lines,
            "commands": commands,
            "elapsed_ms": elapsed_ms,
        }

    @staticmethod
    def _sanitize_peak_schedule(sched: dict[str, Any] | None) -> dict[str, Any] | None:
        """校验并规范化帮派活动分时段自适应提速配置。"""
        if not isinstance(sched, dict):
            return None
        enabled = bool(sched.get("enabled", True))
        raw_days = sched.get("days", [3, 6])
        days: list[int] = []
        if isinstance(raw_days, (list, tuple, set)):
            for d in raw_days:
                try:
                    di = int(d)
                except (TypeError, ValueError):
                    continue
                if 0 <= di <= 6 and di not in days:
                    days.append(di)

        def _norm_hm(val: Any, fallback: str) -> str:
            txt = str(val or "").strip()
            m = re.match(r"^(\d{1,2}):(\d{1,2})$", txt)
            if not m:
                return fallback
            hh, mm = int(m.group(1)), int(m.group(2))
            if not (0 <= hh <= 23 and 0 <= mm <= 59):
                return fallback
            return f"{hh:02d}:{mm:02d}"

        start = _norm_hm(sched.get("start"), "19:30")
        end = _norm_hm(sched.get("end"), "20:20")
        try:
            active_iv = max(0.5, float(sched.get("active_interval_sec", 8.0)))
        except (TypeError, ValueError):
            active_iv = 8.0

        return {
            "enabled": enabled,
            "days": days,
            "start": start,
            "end": end,
            "active_interval_sec": active_iv,
        }

    def compute_effective_interval(self, now: datetime | None = None) -> tuple[float, bool]:
        """计算当前时刻应使用的 OCR 扫描间隔 (秒) 与是否处于活动高峰档。"""
        with self._config_lock:
            base_iv = max(0.5, float(self.interval_sec))
            sched = dict(self.peak_schedule) if isinstance(self.peak_schedule, dict) else None

        if not sched or not sched.get("enabled"):
            return base_iv, False

        cur = now if now is not None else datetime.now()
        days = sched.get("days") or []
        if cur.weekday() not in days:
            return base_iv, False

        try:
            sh, sm = (int(x) for x in str(sched["start"]).split(":", 1))
            eh, em = (int(x) for x in str(sched["end"]).split(":", 1))
            start_dt = cur.replace(hour=sh, minute=sm, second=0, microsecond=0)
            end_dt = cur.replace(hour=eh, minute=em, second=0, microsecond=0)
        except Exception:
            return base_iv, False

        if end_dt <= start_dt:
            return base_iv, False

        if start_dt <= cur < end_dt:
            return max(0.5, float(sched.get("active_interval_sec", 8.0))), True

        if cur < start_dt:
            until_start = (start_dt - cur).total_seconds()
            if until_start < base_iv:
                return max(1.0, until_start), False

        return base_iv, False

    def update_config(
        self,
        enabled: bool | None = None,
        roi: dict[str, Any] | None = None,
        interval_sec: float | None = None,
        trigger_mode: str | None = None,
        cooldown_sec: float | None = None,
        allow_skip_cmd: bool | None = None,
        auto_track_window: bool | None = None,
        peak_schedule: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """热更新 OCR 监听配置参数，并根据 enabled 自动启停后台守护线程。"""
        should_start = False
        should_stop = False

        with self._config_lock:
            if roi is not None:
                sanitized = self._sanitize_roi(roi)
                if sanitized is not None:
                    self.roi = sanitized
                    if auto_track_window is None:
                        self.auto_track_window = False
            if auto_track_window is not None:
                self.auto_track_window = bool(auto_track_window)
            if interval_sec is not None:
                self.interval_sec = max(0.5, float(interval_sec))
            if trigger_mode is not None:
                self.trigger_mode = "prefix" if trigger_mode == "prefix" else "both"
            if cooldown_sec is not None:
                self.cooldown_sec = max(0.0, float(cooldown_sec))
            if allow_skip_cmd is not None:
                self.allow_skip_cmd = bool(allow_skip_cmd)
            if peak_schedule is not None:
                self.peak_schedule = self._sanitize_peak_schedule(peak_schedule)
            if enabled is not None:
                new_enabled = bool(enabled)
                if new_enabled and not self.is_running():
                    should_start = True
                elif not new_enabled:
                    self.enabled = False
                    should_stop = True

        self._wakeup_event.set()
        if should_start:
            self.start()
        elif should_stop:
            self.stop()

        return self.get_status()

    def is_running(self) -> bool:
        t = self._worker_thread
        return bool(t is not None and t.is_alive() and not self._stop_event.is_set())

    def _watch_loop(self) -> None:
        logger.info("OcrChatWatcher background thread started (interval=%.2fs)", self.interval_sec)
        while not self._stop_event.is_set():
            if not self.enabled:
                break
            try:
                img = self._capture_roi_array(self.roi, allow_synthetic_fallback=False)
                if img is None:
                    with self._config_lock:
                        self._last_error = "当前屏幕息屏或锁屏，亮屏后自动恢复识别"
                else:
                    commands = self.recognize_image_array(img, bypass_dedup=False)
                    if commands and self.on_command is not None:
                        for cmd in commands:
                            try:
                                self.on_command(cmd)
                            except Exception as cb_exc:
                                logger.error("OcrChatWatcher on_command callback error: %s", cb_exc)
            except Exception as exc:
                logger.warning("OcrChatWatcher scan error: %s", exc)
                with self._config_lock:
                    self._last_error = str(exc)

            wait_sec, _ = self.compute_effective_interval()
            deadline = time.monotonic() + max(0.5, float(wait_sec))
            while not self._stop_event.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0.0:
                    break
                if self._wakeup_event.wait(timeout=min(1.0, remaining)):
                    self._wakeup_event.clear()
                    break
        logger.info("OcrChatWatcher background thread stopped")

    def start(self) -> None:
        """启动后台定时截图与 OCR 守护线程。"""
        with self._config_lock:
            self.enabled = True
            if self.is_running():
                return
            self._stop_event.clear()
            self._wakeup_event.clear()
            self._worker_thread = threading.Thread(
                target=self._watch_loop,
                name="OcrChatWatcherThread",
                daemon=True,
            )
            self._worker_thread.start()

    def stop(self) -> None:
        """停止后台定时截图与 OCR 守护线程。"""
        with self._config_lock:
            self.enabled = False
            self._stop_event.set()
            self._wakeup_event.set()
            thread = self._worker_thread

        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=3.0)

    def get_status(self) -> dict[str, Any]:
        """获取当前 OCR 监控器完整状态快照。"""
        effective_iv, peak_active = self.compute_effective_interval()
        with self._config_lock:
            return {
                "enabled": bool(self.enabled),
                "running": self.is_running(),
                "roi": dict(self.roi) if isinstance(self.roi, dict) else None,
                "auto_track_window": bool(self.auto_track_window),
                "interval_sec": float(self.interval_sec),
                "peak_schedule": dict(self.peak_schedule) if isinstance(self.peak_schedule, dict) else None,
                "effective_interval_sec": float(round(effective_iv, 2)),
                "peak_mode_active": bool(peak_active),
                "trigger_mode": str(self.trigger_mode),
                "cooldown_sec": float(self.cooldown_sec),
                "allow_skip_cmd": bool(self.allow_skip_cmd),
                "recent_logs": list(self.recent_logs),
                "last_raw_lines": list(self._last_raw_lines),
                "last_scan_at": self._last_scan_at,
                "last_elapsed_ms": float(self._last_elapsed_ms),
                "total_scans": int(self._total_scans),
                "total_commands": int(self._total_commands),
                "last_error": self._last_error,
            }
