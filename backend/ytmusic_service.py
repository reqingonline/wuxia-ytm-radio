"""YouTube Music search, playlist management, authentication, and non-destructive audio caching service."""

from __future__ import annotations

from difflib import SequenceMatcher
import json
from pathlib import Path
import re
import shutil
import threading
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

import requests
import yt_dlp
import ytmusicapi
from ytmusicapi import YTMusic

try:
    import zhconv  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover
    zhconv = None


# Built-in Traditional -> Simplified CJK lookup for Mandopop/Cantopop titles and artists
_T2S_MAP: dict[str, str] = {
    "華": "华", "話": "话", "說": "说", "語": "语", "詩": "诗", "詞": "词", "聲": "声", "樂": "乐",
    "響": "响", "聽": "听", "嘆": "叹", "問": "问", "謝": "谢", "講": "讲", "談": "谈", "認": "认",
    "識": "识", "記": "记", "憶": "忆", "覺": "觉", "愛": "爱", "戀": "恋", "緣": "缘", "願": "愿",
    "憐": "怜", "傷": "伤", "歡": "欢", "淚": "泪", "驚": "惊", "夢": "梦", "懷": "怀", "擁": "拥",
    "牽": "牵", "離": "离", "別": "别", "歸": "归", "來": "来", "還": "还", "過": "过", "錯": "错",
    "對": "对", "虛": "虚", "實": "实", "癡": "痴", "瘋": "疯", "單": "单", "獨": "独", "雙": "双",
    "兩": "两", "倆": "俩", "們": "们", "個": "个", "這": "这", "裡": "里", "後": "后", "邊": "边",
    "頭": "头", "臉": "脸", "腳": "脚", "髮": "发", "體": "体", "紅": "红", "藍": "蓝", "綠": "绿",
    "黃": "黄", "銀": "银", "陰": "阴", "陽": "阳", "雲": "云", "霧": "雾", "電": "电", "閃": "闪",
    "潮": "潮", "峰": "峰", "嶺": "岭", "島": "岛", "橋": "桥", "車": "车", "鎮": "镇", "鄉": "乡",
    "國": "国", "間": "间", "時": "时", "歲": "岁", "曉": "晓", "樹": "树", "葉": "叶",
    "鳥": "鸟", "鶴": "鹤", "鳳": "凤", "龍": "龙", "魚": "鱼", "劍": "剑", "槍": "枪", "戰": "战",
    "鬥": "斗", "爭": "争", "勝": "胜", "敗": "败", "俠": "侠", "義": "义", "禮": "礼", "誠": "诚",
    "諾": "诺", "約": "约", "題": "题", "書": "书", "紙": "纸", "筆": "笔", "畫": "画", "寫": "写",
    "讀": "读", "見": "见", "尋": "寻", "護": "护", "幫": "帮", "飛": "飞", "飄": "飘", "墜": "坠",
    "燒": "烧", "滅": "灭", "開": "开", "關": "关", "閉": "闭", "鎖": "锁", "結": "结", "續": "续",
    "斷": "断", "毀": "毁", "給": "给", "與": "与", "為": "为", "爲": "为", "無": "无", "沒": "没",
    "總": "总", "難": "难", "輕": "轻", "長": "长", "遠": "远", "淺": "浅", "熱": "热", "溫": "温",
    "涼": "凉", "靜": "静", "鬧": "闹", "醜": "丑", "麗": "丽", "艷": "艳", "艶": "艳", "榮": "荣",
    "貴": "贵", "貧": "贫", "窮": "穷", "強": "强", "舊": "旧", "數": "数", "幾": "几", "該": "该",
    "當": "当", "應": "应", "須": "须", "讓": "让", "稱": "称", "號": "号", "誰": "谁", "麼": "么",
    "嗎": "吗", "隻": "只", "條": "条", "張": "张", "顆": "颗", "塊": "块", "節": "节", "調": "调",
    "韻": "韵", "彈": "弹", "動": "动", "變": "变", "轉": "转", "輪": "轮", "迴": "回", "塵": "尘",
    "煙": "烟", "燈": "灯", "燭": "烛", "輝": "辉", "燦": "灿", "爛": "烂", "爍": "烁", "恆": "恒",
    "經": "经", "將": "将", "準": "准", "備": "备", "滿": "满", "盡": "尽", "絕": "绝", "終": "终",
    "萬": "万", "億": "亿", "貳": "贰", "參": "参", "陸": "陆", "親": "亲", "師": "师", "兒": "儿",
    "寶": "宝", "貝": "贝", "聖": "圣", "軍": "军", "眾": "众", "門": "门", "閣": "阁", "樓": "楼",
    "臺": "台", "灣": "湾", "滬": "沪", "漢": "汉", "晉": "晋",
    "媽": "妈", "點": "点", "進": "进", "選": "选", "際": "际", "壞": "坏", "從": "从",
    "鐵": "铁", "鈴": "铃", "東": "东", "亞": "亚", "歐": "欧", "潔": "洁", "櫻": "樱",
    "楓": "枫", "雞": "鸡", "鴨": "鸭", "貓": "猫", "豬": "猪", "蟬": "蝉", "螢": "萤",
    "爺": "爷", "幫": "帮", "帶": "带", "歸": "归", "畫": "画",
    # Popular artist / song characters
    "傑": "杰", "倫": "伦", "鄧": "邓", "齊": "齐", "學": "学", "劉": "刘", "陳": "陈", "孫": "孙",
    "許": "许", "謙": "谦", "齡": "龄", "羅": "罗", "楊": "杨", "緯": "纬", "憲": "宪", "韋": "韦",
    "吳": "吴", "蘇": "苏", "盧": "卢", "廣": "广", "蕭": "萧", "騰": "腾", "瑋": "玮", "霆": "霆",
    "鋒": "锋", "衛": "卫", "蘭": "兰", "樑": "梁", "傳": "传", "賢": "贤", "達": "达", "純": "纯",
    "嶽": "岳", "嵐": "岚", "鄭": "郑", "莊": "庄", "譚": "谭", "詠": "咏", "葉": "叶", "費": "费",
    "趙": "赵", "關": "关", "馬": "马", "馮": "冯", "鍾": "钟", "蔣": "蒋", "沈": "沈", "韓": "韩",
    "朱": "朱", "週": "周",
}
_T2S_TRANS = str.maketrans(_T2S_MAP)


def _to_simplified(text: str) -> str:
    """Normalize Traditional Chinese characters to Simplified Chinese."""
    raw = text or ""
    if not raw:
        return ""
    if zhconv is not None:
        try:
            return str(zhconv.convert(raw, "zh-hans"))
        except Exception:
            pass
    return raw.translate(_T2S_TRANS)


DEFAULT_FALLBACK_QUERY = "天涯明月刀 古风 经典"

DEFAULT_SEED_PLAYLISTS: list[dict[str, Any]] = [
    {
        "playlist_id": "search:天涯明月刀 古风 经典",
        "title": "天刀国风·明月天涯经典曲库",
        "count": 30,
    },
    {
        "playlist_id": "search:华语经典流行",
        "title": "华语流行·金曲怀旧电台",
        "count": 30,
    },
    {
        "playlist_id": "search:纯音乐 古筝 笛子",
        "title": "国风纯音·古筝竹笛雅集",
        "count": 25,
    },
    {
        "playlist_id": "search:古风 燃曲 战歌",
        "title": "江湖热血·帮派联赛战歌",
        "count": 25,
    },
]

_TEMP_SUFFIXES = {".part", ".tmp", ".ytdl", ".temp"}


def _parse_duration_sec(raw_val: Any) -> int:
    """Convert duration representation (int seconds, 'MM:SS', 'HH:MM:SS') into integer seconds."""
    if raw_val is None:
        return 0
    if isinstance(raw_val, (int, float)):
        return max(0, int(raw_val))
    text = str(raw_val).strip()
    if not text:
        return 0
    if text.isdigit():
        return max(0, int(text))
    parts = text.split(":")
    try:
        nums = [int(p.strip()) for p in parts]
    except ValueError:
        return 0
    if len(nums) == 2:
        return max(0, nums[0] * 60 + nums[1])
    if len(nums) == 3:
        return max(0, nums[0] * 3600 + nums[1] * 60 + nums[2])
    return 0


def _normalize_text(text: str) -> str:
    """Normalize text for fuzzy title/artist matching (case-folded and Simplified CJK)."""
    cleaned = _to_simplified(text or "").lower().strip()
    cleaned = re.sub(
        r"[\s\-_—–·•‧．・/\\、~～,，.。!！?？:：;；'\"“”‘’()（）\[\]【】《》<>]+",
        "",
        cleaned,
    )
    return cleaned


_EDITION_PAREN_RE = re.compile(
    r"(?:version|ver\.?|live|remix|remaster|official|audio|video|mv|acoustic|mixtape|feat\.?|ft\.?|explicit|clean|radio|edit|demo|cover|instrumental|karaoke|ktv|主题曲|片尾曲|片头曲|插曲|合作|原声|现场|版|演唱会|伴奏)",
    re.IGNORECASE,
)


def _strip_edition_parens(text: str) -> str:
    """Strip parenthesized edition/metadata tags while keeping bilingual subtitles like '(DDU-DU DDU-DU)'."""
    def _repl(m: re.Match[str]) -> str:
        inner = m.group(1) or ""
        return " " if _EDITION_PAREN_RE.search(inner) else m.group(0)

    return re.sub(r"\s*[\(（\[【]([^\)）\]】]*)[\)）\]】]", _repl, text or "").strip()


def _tokenize_words(text: str) -> set[str]:
    """Extract lowercase alphanumeric words and Simplified CJK characters for token-overlap recall."""
    low = _to_simplified(text or "").lower()
    words = set(re.findall(r"[a-z0-9]+", low))
    cjk = set(re.findall(r"[\u4e00-\u9fff]", low))
    return words | cjk


class YTMusicService:
    """YouTube Music service providing search, playlist resolution, auth management, and audio caching."""

    def __init__(
        self,
        data_dir: Path | str,
        trash_dir: Path | str,
        proxy: str = "",
        cache_max_mb: int | float = 1024,
    ) -> None:
        self.data_dir = Path(data_dir).resolve()
        self.cache_dir = self.data_dir / "cache"
        self.trash_dir = Path(trash_dir).resolve()

        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.trash_dir.mkdir(parents=True, exist_ok=True)

        self.headers_path = self.data_dir / "ytmusic_headers.json"
        self.cookies_txt_path = self.data_dir / "cookies.txt"

        self.proxy: str = (proxy or "").strip()
        self.cache_max_mb: float = float(cache_max_mb) if cache_max_mb is not None else 1024.0

        self._lock = threading.RLock()
        self.authenticated: bool = False
        self.mode: str = "guest"
        self.message: str = "免登录访客模式 (Guest Mode)"
        self.ytmusic: YTMusic = self._init_client()

    def _move_to_trash(self, file_path: Path) -> Path | None:
        """Safely archive a file into trash_dir using shutil.move with timestamp conflict resolution."""
        if not file_path.exists():
            return None
        self.trash_dir.mkdir(parents=True, exist_ok=True)
        dest = self.trash_dir / file_path.name
        if dest.exists():
            ts = int(time.time() * 1000)
            dest = self.trash_dir / f"{file_path.stem}_{ts}{file_path.suffix}"
            counter = 1
            while dest.exists():
                dest = self.trash_dir / f"{file_path.stem}_{ts}_{counter}{file_path.suffix}"
                counter += 1
        shutil.move(str(file_path), str(dest))
        return dest

    def _build_requests_session(self) -> requests.Session | None:
        """Create a requests.Session configured with proxy if self.proxy is non-empty."""
        proxy_url = (self.proxy or "").strip()
        if not proxy_url:
            return None
        session = requests.Session()
        session.proxies.update(
            {
                "http": proxy_url,
                "https": proxy_url,
            }
        )
        return session

    def _create_ytmusic_instance(self, auth_arg: str | dict[str, Any] | None = None) -> YTMusic:
        """Instantiate YTMusic client with optional auth and optional proxy session."""
        session = self._build_requests_session()
        kwargs: dict[str, Any] = {"language": "zh_CN"}
        if auth_arg is not None:
            kwargs["auth"] = auth_arg
        if session is not None:
            kwargs["requests_session"] = session
        try:
            return YTMusic(**kwargs)
        except Exception:
            # Fallback to default language if zh_CN locale table is unavailable
            kwargs.pop("language", None)
            return YTMusic(**kwargs)

    def _init_client(self) -> YTMusic:
        """Initialize or re-initialize the YTMusic client based on saved headers and proxy."""
        with self._lock:
            if self.headers_path.exists() and self.headers_path.stat().st_size > 0:
                try:
                    client = self._create_ytmusic_instance(auth_arg=str(self.headers_path))
                    self.authenticated = True
                    self.mode = "authenticated"
                    self.message = "已加载 YouTube Music 认证凭据"
                    return client
                except Exception as exc:
                    self.authenticated = False
                    self.mode = "guest"
                    self.message = f"认证凭据加载失败，已回退免登录模式: {exc}"

            client = self._create_ytmusic_instance(auth_arg=None)
            self.authenticated = False
            self.mode = "guest"
            if not self.message or self.message.startswith("已加载"):
                self.message = "免登录访客模式 (Guest Mode)"
            return client

    @staticmethod
    def _normalize_raw_headers(raw_input: str) -> str:
        """Normalize raw F12 request headers, JSON headers, or pure Cookie string for ytmusicapi.setup."""
        text = raw_input.strip()
        headers_map: dict[str, str] = {}
        lower_to_orig: dict[str, str] = {}

        def _set_header(k: str, v: str, overwrite: bool = True) -> None:
            k_clean = k.strip().lstrip(":")
            v_clean = v.strip()
            if not k_clean or not v_clean:
                return
            k_low = k_clean.lower()
            if k_low in ("authority", "method", "path", "scheme", "content-length", "host"):
                return
            if k_low in lower_to_orig:
                if overwrite:
                    headers_map[lower_to_orig[k_low]] = v_clean
            else:
                lower_to_orig[k_low] = k_clean
                headers_map[k_clean] = v_clean

        # 0. Check if Chrome DevTools "Copy as fetch" / "Copy as fetch (Node.js)" snippet
        fetch_match = re.search(
            r"fetch\s*\([^,]+,\s*(\{.*\})\s*\)\s*;?\s*$",
            text,
            re.S,
        )
        if fetch_match:
            try:
                parsed_fetch = json.loads(fetch_match.group(1))
                if isinstance(parsed_fetch, dict):
                    fetch_hdrs = parsed_fetch.get("headers")
                    if isinstance(fetch_hdrs, dict):
                        for k, v in fetch_hdrs.items():
                            if isinstance(v, (str, int, float)):
                                _set_header(str(k), str(v))
            except Exception:
                pass

        # 0b. Check if Chrome DevTools "Copy as cURL" snippet
        if not headers_map and text.lstrip().startswith("curl "):
            for h_match in re.finditer(r"(?:-H|--header)\s+(['\"])(.*?)\1", text, re.S):
                hdr_pair = h_match.group(2)
                if ":" in hdr_pair:
                    hk, hv = hdr_pair.split(":", 1)
                    _set_header(hk, hv)
            for b_match in re.finditer(r"(?:-b|--cookie)\s+(['\"])(.*?)\1", text, re.S):
                cookie_str = b_match.group(2).strip()
                if cookie_str:
                    _set_header("cookie", cookie_str)

        # 1. Check if JSON object
        if not headers_map and text.startswith("{") and text.endswith("}"):
            try:
                parsed_json = json.loads(text)
                if isinstance(parsed_json, dict):
                    if "headers" in parsed_json and isinstance(parsed_json["headers"], dict):
                        parsed_json = parsed_json["headers"]
                    for k, v in parsed_json.items():
                        if isinstance(v, (str, int, float)):
                            _set_header(str(k), str(v))
            except Exception:
                pass

        # 2. Parse line-by-line or raw cookie
        if not headers_map:
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            if len(lines) == 1 and ":" not in lines[0]:
                # Pure cookie value without 'Cookie:' prefix
                _set_header("cookie", lines[0])
            elif len(lines) == 1 and lines[0].lower().startswith("cookie:"):
                _set_header("cookie", lines[0].split(":", 1)[1])
            else:
                pending_key: str | None = None
                for ln in lines:
                    if ln.startswith(":"):
                        continue
                    if ":" in ln:
                        k, v = ln.split(":", 1)
                        # Check if it actually looks like a header key
                        if re.match(r"^[A-Za-z0-9\-_]+$", k.strip()):
                            _set_header(k, v)
                            pending_key = None
                            continue
                    if pending_key is not None:
                        _set_header(pending_key, ln)
                        pending_key = None
                    elif re.match(r"^[A-Za-z0-9\-_]+$", ln):
                        pending_key = ln
                    elif "=" in ln and ";" in ln and "cookie" not in lower_to_orig:
                        _set_header("cookie", ln)

            # Still no cookie found? If raw input contains '=' use it as cookie
            if "cookie" not in lower_to_orig and "=" in text:
                first_line = lines[0] if lines else text
                if first_line.lower().startswith("cookie:"):
                    _set_header("cookie", first_line.split(":", 1)[1])
                else:
                    _set_header("cookie", text.replace("\r", " ").replace("\n", "; "))

        # Ensure mandatory headers for ytmusicapi.setup
        defaults = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Content-Type": "application/json",
            "Origin": "https://music.youtube.com",
            "Referer": "https://music.youtube.com/",
            "X-Goog-AuthUser": "0",
            "x-origin": "https://music.youtube.com",
        }
        for dk, dv in defaults.items():
            _set_header(dk, dv, overwrite=False)

        return "\n".join(f"{k}: {v}" for k, v in headers_map.items())

    @staticmethod
    def _cookie_header_to_netscape(cookie_header: str) -> str:
        """Convert a HTTP Cookie header string ('k1=v1; k2=v2') into deduplicated Netscape cookies.txt."""
        lines: list[str] = [
            "# Netscape HTTP Cookie File",
            "# Generated automatically from YouTube Music Cookie header",
        ]
        seen: set[str] = set()
        for part in (cookie_header or "").split(";"):
            seg = part.strip()
            if not seg or "=" not in seg:
                continue
            name, value = seg.split("=", 1)
            name_clean = name.strip().replace("\t", "").replace("\r", "").replace("\n", "")
            val_clean = value.strip().replace("\t", "").replace("\r", "").replace("\n", "")
            if not name_clean or name_clean.startswith("__Host-") or name_clean in seen:
                continue
            seen.add(name_clean)
            lines.append(f".youtube.com\tTRUE\t/\tTRUE\t2147483647\t{name_clean}\t{val_clean}")
        return "\n".join(lines) + "\n"

    @staticmethod
    def _detect_js_runtimes() -> dict[str, Any]:
        """Locate local Node.js runtime for yt-dlp EJS challenge solving."""
        candidates: list[Path] = []
        which_node = shutil.which("node")
        if which_node:
            candidates.append(Path(which_node))
        candidates.append(Path(r"E:\appdate\nodejs\node.exe"))
        for cand in candidates:
            try:
                if cand.exists() and cand.is_file():
                    return {"node": {"path": str(cand)}}
            except OSError:
                continue
        return {}

    def configure(
        self,
        proxy: str = "",
        raw_headers_or_cookie: str = "",
        cookies_txt_content: str = "",
    ) -> dict[str, Any]:
        """Configure proxy, YouTube Music request headers/cookie, and yt-dlp cookies.txt."""
        with self._lock:
            if proxy is not None:
                self.proxy = str(proxy).strip()

            messages: list[str] = []
            auth_error: str | None = None
            has_explicit_cookies_txt = bool(cookies_txt_content and cookies_txt_content.strip())

            if has_explicit_cookies_txt:
                try:
                    if self.cookies_txt_path.exists():
                        self._move_to_trash(self.cookies_txt_path)
                    self.cookies_txt_path.write_text(
                        cookies_txt_content.strip() + "\n",
                        encoding="utf-8",
                    )
                    messages.append("cookies.txt 已更新")
                except Exception as exc:
                    messages.append(f"cookies.txt 写入失败: {exc}")

            if raw_headers_or_cookie and raw_headers_or_cookie.strip():
                try:
                    normalized_raw = self._normalize_raw_headers(raw_headers_or_cookie)
                    if self.headers_path.exists():
                        self._move_to_trash(self.headers_path)
                    ytmusicapi.setup(
                        filepath=str(self.headers_path),
                        headers_raw=normalized_raw,
                    )
                    messages.append("YouTube Music 认证凭据已生成")

                    if not has_explicit_cookies_txt:
                        extracted_cookie = ""
                        for hdr_line in normalized_raw.splitlines():
                            if ":" in hdr_line:
                                h_k, h_v = hdr_line.split(":", 1)
                                if h_k.strip().lower() == "cookie":
                                    extracted_cookie = h_v.strip()
                                    break
                        if extracted_cookie:
                            netscape_txt = self._cookie_header_to_netscape(extracted_cookie)
                            if self.cookies_txt_path.exists():
                                self._move_to_trash(self.cookies_txt_path)
                            self.cookies_txt_path.write_text(netscape_txt, encoding="utf-8")
                            messages.append("已同步生成 yt-dlp cookies.txt")
                except Exception as exc:
                    auth_error = f"解析 Headers/Cookie 失败: {exc}"
                    messages.append(auth_error)

            self.ytmusic = self._init_client()

            if auth_error:
                self.message = auth_error
            elif messages:
                self.message = "；".join(messages)
            elif self.authenticated:
                self.message = "已认证 (Authenticated)"
            else:
                self.message = "免登录访客模式 (Guest Mode)"

            return self.get_auth_status()

    def get_auth_status(self) -> dict[str, Any]:
        """Return current authentication, cookies.txt, and proxy status dictionary."""
        with self._lock:
            has_cookies = bool(
                self.cookies_txt_path.exists() and self.cookies_txt_path.stat().st_size > 0
            )
            return {
                "authenticated": bool(self.authenticated),
                "mode": str(self.mode),
                "has_cookies_txt": has_cookies,
                "proxy": str(self.proxy),
                "message": str(self.message),
            }

    @staticmethod
    def _normalize_track_item(item: dict[str, Any]) -> dict[str, Any] | None:
        """Convert a raw ytmusicapi or yt-dlp track dict into the contract format."""
        if not isinstance(item, dict):
            return None
        video_id = (
            item.get("videoId")
            or item.get("video_id")
            or item.get("id")
            or ""
        )
        video_id = str(video_id).strip()
        if not video_id or len(video_id) < 5:
            return None

        title = str(item.get("title") or "").strip()
        if not title:
            return None

        # Extract artist name(s)
        artist = ""
        artists_raw = item.get("artists")
        if isinstance(artists_raw, list) and artists_raw:
            names = [
                str(a.get("name", "")).strip()
                for a in artists_raw
                if isinstance(a, dict) and str(a.get("name", "")).strip()
            ]
            artist = " / ".join(names)
        if not artist:
            artist = str(
                item.get("artist")
                or item.get("author")
                or item.get("uploader")
                or item.get("channel")
                or "未知歌手"
            ).strip()

        # Extract duration_sec
        duration_sec = _parse_duration_sec(
            item.get("duration_seconds")
            if item.get("duration_seconds") is not None
            else (
                item.get("duration_sec")
                if item.get("duration_sec") is not None
                else (
                    item.get("duration")
                    if item.get("duration") is not None
                    else item.get("lengthSeconds")
                )
            )
        )

        # Extract thumbnail_url
        thumbnail_url = ""
        thumbs = item.get("thumbnails")
        if isinstance(thumbs, list) and thumbs:
            for th in reversed(thumbs):
                if isinstance(th, dict) and th.get("url"):
                    thumbnail_url = str(th["url"]).strip()
                    break
        if not thumbnail_url:
            thumbnail_url = str(
                item.get("thumbnail_url")
                or item.get("thumbnail")
                or f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
            ).strip()

        return {
            "video_id": video_id,
            "title": title,
            "artist": artist or "未知歌手",
            "duration_sec": int(duration_sec),
            "thumbnail_url": thumbnail_url,
        }

    def _fallback_ytdlp_search(self, query: str, limit: int) -> list[dict[str, Any]]:
        """Fallback flat search via yt-dlp when ytmusicapi fails due to regional or network quirks."""
        ydl_opts: dict[str, Any] = {
            "extract_flat": "in_playlist",
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
        }
        if self.proxy:
            ydl_opts["proxy"] = self.proxy
        if self.cookies_txt_path.exists() and self.cookies_txt_path.stat().st_size > 0:
            ydl_opts["cookiefile"] = str(self.cookies_txt_path)

        results: list[dict[str, Any]] = []
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(f"ytsearch{max(limit, 5)}:{query}", download=False)
                entries = info.get("entries", []) if isinstance(info, dict) else []
                for entry in entries:
                    norm = self._normalize_track_item(entry)
                    if norm:
                        results.append(norm)
                        if len(results) >= limit:
                            break
        except Exception:
            pass
        return results

    def search_songs(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        """Search songs first with filter='songs', then supplement with filter='videos' and deduplicate."""
        q = (query or "").strip()
        if not q:
            return []
        limit = max(1, int(limit))

        results: list[dict[str, Any]] = []
        seen_ids: set[str] = set()

        def _collect(raw_items: list[Any], result_type: str) -> None:
            for raw in raw_items:
                norm = self._normalize_track_item(raw)
                if not norm:
                    continue
                vid = norm["video_id"]
                if vid in seen_ids:
                    continue
                seen_ids.add(vid)
                norm["result_type"] = result_type
                results.append(norm)
                if len(results) >= limit:
                    break

        try:
            songs_raw = self.ytmusic.search(q, filter="songs", limit=max(limit * 2, 10))
            if isinstance(songs_raw, list):
                _collect(songs_raw, "song")
        except Exception:
            pass

        if len(results) < limit:
            try:
                videos_raw = self.ytmusic.search(q, filter="videos", limit=max(limit * 2, 10))
                if isinstance(videos_raw, list):
                    _collect(videos_raw, "video")
            except Exception:
                pass

        if not results:
            for item in self._fallback_ytdlp_search(q, limit=limit):
                vid = item["video_id"]
                if vid not in seen_ids:
                    seen_ids.add(vid)
                    item.setdefault("result_type", "video")
                    results.append(item)
                    if len(results) >= limit:
                        break

        return results[:limit]

    def search_best_match(self, title: str, artist: str = "") -> dict[str, Any] | None:
        """Select the best matching single track supporting both 歌名-歌手 and 歌手-歌名 with typo tolerance."""
        clean_title = (title or "").strip()
        clean_artist = (artist or "").strip()
        if not clean_title:
            return None

        query = f"{clean_title} {clean_artist}".strip() if clean_artist else clean_title
        candidates = self.search_songs(query, limit=10)
        if not candidates and clean_artist:
            candidates = self.search_songs(clean_title, limit=10)
        if not candidates:
            return None

        valid_pool = [
            c for c in candidates if 45 <= int(c.get("duration_sec", 0)) <= 900
        ]
        if not valid_pool:
            return candidates[0]

        cjk_penalties = ("伴奏", "1小时", "合集", "串烧", "翻唱", "慢摇", "钢琴版")
        ascii_penalty_res = (
            re.compile(r"\binstrumental\b"),
            re.compile(r"\bkaraoke\b"),
            re.compile(r"\bktv\b"),
            re.compile(r"\bcover\b"),
            re.compile(r"\bdj\b"),
            re.compile(r"\bremix\b"),
            re.compile(r"\bsped up\b"),
            re.compile(r"\bnightcore\b"),
            re.compile(r"\bpiano\b"),
        )
        live_re = re.compile(r"\blive\b")
        query_low = query.lower()

        def _score_pair(cand: dict[str, Any], idx: int, t_title_raw: str, t_artist_raw: str) -> float:
            cand_title_raw = str(cand.get("title", ""))
            cand_artist_raw = str(cand.get("artist", ""))
            base_cand_title_raw = _strip_edition_parens(cand_title_raw) or cand_title_raw

            norm_t_title = _normalize_text(t_title_raw)
            norm_t_artist = _normalize_text(t_artist_raw)
            base_c_title = _normalize_text(base_cand_title_raw) or _normalize_text(cand_title_raw)
            sub_c_titles = [
                _normalize_text(g)
                for g in re.findall(r"[\(（\[【]([^\)）\]】]*)[\)）\]】]", cand_title_raw)
                if g and not _EDITION_PAREN_RE.search(g)
            ]
            cand_forms = [f for f in [base_c_title] + sub_c_titles if f]
            norm_c_artist = _normalize_text(cand_artist_raw)

            score = 0.0

            # Title similarity scoring against edition-stripped core title and non-edition subtitles
            if norm_t_title and cand_forms:
                best_form_score = 0.0
                for form in cand_forms:
                    f_score = 0.0
                    if norm_t_title == form:
                        f_score += 110.0
                    elif len(norm_t_title) >= 2 and (norm_t_title in form or form in norm_t_title):
                        f_score += 75.0
                    f_score += SequenceMatcher(None, norm_t_title, form).ratio() * 65.0
                    if f_score > best_form_score:
                        best_form_score = f_score
                score += best_form_score

                t_tokens = _tokenize_words(t_title_raw)
                c_tokens = _tokenize_words(base_cand_title_raw)
                if t_tokens and c_tokens:
                    score += (len(t_tokens & c_tokens) / len(t_tokens)) * 65.0

            # Artist similarity scoring + explicit artist mismatch guard
            if norm_t_artist:
                artist_score = 0.0
                if norm_c_artist and (
                    norm_t_artist in norm_c_artist or norm_c_artist in norm_t_artist
                ):
                    artist_score = 65.0
                else:
                    a_tok_t = _tokenize_words(t_artist_raw)
                    a_tok_c = _tokenize_words(cand_artist_raw)
                    if a_tok_t and a_tok_c:
                        overlap_score = (len(a_tok_t & a_tok_c) / len(a_tok_t)) * 35.0
                    else:
                        overlap_score = 0.0
                    seq_score = SequenceMatcher(None, norm_t_artist, norm_c_artist).ratio() * 35.0
                    artist_score = max(overlap_score, seq_score)
                    if (
                        len(norm_t_artist) >= 2
                        and norm_t_artist in _normalize_text(cand_title_raw)
                    ):
                        artist_score += 15.0
                score += artist_score
                if artist_score < 8.0:
                    score -= 25.0

            # Official track boost & cover/remix/instrumental/DJ penalties
            if cand.get("result_type") == "song":
                score += 15.0

            cand_low = cand_title_raw.lower()
            artist_low = cand_artist_raw.lower()
            for kw in cjk_penalties:
                if kw in cand_low and kw not in query_low:
                    score -= 55.0
            for rx in ascii_penalty_res:
                if rx.search(cand_low) and not rx.search(query_low):
                    score -= 55.0
            dj_rx = ascii_penalty_res[4]
            if dj_rx.search(artist_low) and not dj_rx.search(query_low):
                score -= 25.0
            if ("现场" in cand_low and "现场" not in query_low) or (
                live_re.search(cand_low) and not live_re.search(query_low)
            ):
                score -= 12.0

            dur = int(cand.get("duration_sec", 0))
            if 90 <= dur <= 420:
                score += 8.0
            score -= idx * 1.5
            return score

        best_item = valid_pool[0]
        best_score = -1e9
        for idx, cand in enumerate(valid_pool):
            s_normal = _score_pair(cand, idx, clean_title, clean_artist)
            if clean_title and clean_artist:
                s_swapped = _score_pair(cand, idx, clean_artist, clean_title) - 2.0
                score = max(s_normal, s_swapped)
            else:
                score = s_normal
            if score > best_score:
                best_score = score
                best_item = cand

        return best_item

    def get_user_playlists(self) -> list[dict[str, Any]]:
        """Return user library playlists (including 'LM' Liked Music) when authenticated, or seed playlists otherwise."""
        if not self.authenticated:
            return [dict(item) for item in DEFAULT_SEED_PLAYLISTS]

        playlists: list[dict[str, Any]] = [
            {
                "playlist_id": "LM",
                "title": "我喜欢的音乐 (Liked Music)",
                "count": 100,
            }
        ]
        seen_pids: set[str] = {"LM"}

        try:
            raw_list = self.ytmusic.get_library_playlists(limit=50)
            if isinstance(raw_list, list):
                for item in raw_list:
                    if not isinstance(item, dict):
                        continue
                    pid = str(item.get("playlistId") or item.get("id") or "").strip()
                    if not pid:
                        continue
                    raw_count = item.get("count")
                    count_val = 0
                    if isinstance(raw_count, (int, float)):
                        count_val = int(raw_count)
                    elif raw_count is not None:
                        digits = re.sub(r"[^\d]", "", str(raw_count))
                        count_val = int(digits) if digits else 0

                    if pid == "LM":
                        if count_val > 0:
                            playlists[0]["count"] = count_val
                        continue
                    if pid in seen_pids:
                        continue
                    seen_pids.add(pid)
                    playlists.append(
                        {
                            "playlist_id": pid,
                            "title": str(item.get("title") or "未命名歌单").strip(),
                            "count": count_val,
                        }
                    )
        except Exception:
            pass

        for seed in DEFAULT_SEED_PLAYLISTS:
            if seed["playlist_id"] not in seen_pids:
                playlists.append(dict(seed))

        return playlists

    def get_playlist_tracks(self, playlist_id_or_url: str, limit: int = 100) -> list[dict[str, Any]]:
        """Fetch tracks from 'LM', a playlist URL, a playlist ID, or a 'search:keyword' expression."""
        raw = (playlist_id_or_url or "").strip()
        limit = max(1, int(limit))
        if not raw:
            raw = f"search:{DEFAULT_FALLBACK_QUERY}"

        if raw.lower().startswith("search:"):
            kw = raw[len("search:") :].strip() or DEFAULT_FALLBACK_QUERY
            return self.search_songs(kw, limit=limit)

        # Check if URL with list=...
        pid = raw
        if "://" in raw or "list=" in raw:
            try:
                parsed = urlparse(raw)
                qs = parse_qs(parsed.query)
                if "list" in qs and qs["list"]:
                    pid = qs["list"][0].strip()
            except Exception:
                pass

        tracks: list[dict[str, Any]] = []
        seen_ids: set[str] = set()

        def _extract_tracks(raw_tracks: Any) -> None:
            if not isinstance(raw_tracks, list):
                return
            for item in raw_tracks:
                norm = self._normalize_track_item(item)
                if not norm:
                    continue
                vid = norm["video_id"]
                if vid in seen_ids:
                    continue
                seen_ids.add(vid)
                tracks.append(norm)
                if len(tracks) >= limit:
                    break

        if pid.upper() == "LM":
            if self.authenticated:
                try:
                    res = self.ytmusic.get_liked_songs(limit=limit)
                    if isinstance(res, dict):
                        _extract_tracks(res.get("tracks", []))
                except Exception:
                    pass
            if not tracks:
                return self.search_songs(DEFAULT_FALLBACK_QUERY, limit=limit)
            return tracks[:limit]

        # Normal playlist ID
        candidate_pids = [pid]
        if pid.startswith("VL") and len(pid) > 2:
            candidate_pids.append(pid[2:])

        for cand_pid in candidate_pids:
            try:
                res = self.ytmusic.get_playlist(cand_pid, limit=limit)
                if isinstance(res, dict):
                    _extract_tracks(res.get("tracks", []))
                if tracks:
                    break
            except Exception:
                continue

        if not tracks:
            return self.search_songs(raw, limit=limit)

        return tracks[:limit]

    def _find_cached_file(self, video_id: str) -> Path | None:
        """Find a completed cached audio file for video_id, ignoring partial/temp files."""
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        matches: list[Path] = []
        for p in self.cache_dir.glob(f"{video_id}.*"):
            if not p.is_file():
                continue
            if p.suffix.lower() in _TEMP_SUFFIXES:
                continue
            if any(p.name.lower().endswith(suf) for suf in _TEMP_SUFFIXES):
                continue
            try:
                if p.stat().st_size > 0:
                    matches.append(p)
            except OSError:
                continue
        if not matches:
            return None
        matches.sort(key=lambda x: x.stat().st_mtime, reverse=True)
        return matches[0]

    def enforce_cache_limit(self, protect_file: Path | None = None) -> list[Path]:
        """Evict oldest cached files to trash_dir via shutil.move when cache exceeds cache_max_mb."""
        with self._lock:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            self.trash_dir.mkdir(parents=True, exist_ok=True)
            max_bytes = int(float(self.cache_max_mb) * 1024 * 1024)

            files_info: list[tuple[Path, float, int]] = []
            total_bytes = 0
            for p in self.cache_dir.iterdir():
                if not p.is_file():
                    continue
                if p.suffix.lower() in _TEMP_SUFFIXES:
                    continue
                try:
                    st = p.stat()
                    files_info.append((p, st.st_mtime, st.st_size))
                    total_bytes += st.st_size
                except OSError:
                    continue

            if total_bytes <= max_bytes:
                return []

            # Sort from oldest mtime to newest mtime
            files_info.sort(key=lambda item: item[1])
            moved_paths: list[Path] = []
            protect_resolved = protect_file.resolve() if protect_file and protect_file.exists() else None

            for file_path, _mtime, fsize in files_info:
                if total_bytes <= max_bytes:
                    break
                if protect_resolved is not None and file_path.resolve() == protect_resolved:
                    continue
                moved = self._move_to_trash(file_path)
                if moved is not None:
                    moved_paths.append(moved)
                    total_bytes -= fsize

            return moved_paths

    def resolve_audio_source(self, video_id: str) -> dict[str, Any]:
        """Resolve local audio file for video_id from cache or download via yt-dlp, enforcing cache limit."""
        vid = (video_id or "").strip()
        if not vid:
            raise ValueError("video_id must not be empty")

        cached = self._find_cached_file(vid)
        if cached is not None:
            self.enforce_cache_limit(protect_file=cached)
            return {
                "video_id": vid,
                "file_path": str(cached.resolve()),
                "ext": cached.suffix.lstrip("."),
                "cached": True,
            }

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        ydl_opts: dict[str, Any] = {
            "format": "bestaudio[ext=m4a]/bestaudio/best",
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "outtmpl": str(self.cache_dir / f"{vid}.%(ext)s"),
            "overwrites": False,
            "nopart": True,
            "cachedir": False,
        }
        if self.proxy:
            ydl_opts["proxy"] = self.proxy
        if self.cookies_txt_path.exists() and self.cookies_txt_path.stat().st_size > 0:
            ydl_opts["cookiefile"] = str(self.cookies_txt_path)
        js_runtimes = self._detect_js_runtimes()
        if js_runtimes:
            ydl_opts["js_runtimes"] = js_runtimes
            ydl_opts["remote_components"] = ["ejs:github"]

        url = f"https://www.youtube.com/watch?v={vid}"
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info(url, download=True)

        downloaded = self._find_cached_file(vid)
        if downloaded is None:
            raise RuntimeError(f"Failed to resolve or locate downloaded audio file for video_id={vid}")

        self.enforce_cache_limit(protect_file=downloaded)
        return {
            "video_id": vid,
            "file_path": str(downloaded.resolve()),
            "ext": downloaded.suffix.lstrip("."),
            "cached": False,
        }
