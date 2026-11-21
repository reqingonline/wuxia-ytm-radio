"""Core 24-hour Auto-DJ scheduler, preload engine, and subsystem coordinator."""

from __future__ import annotations

from collections import deque
import copy
from datetime import datetime, timedelta
import difflib
import inspect
import json
import logging
import os
from pathlib import Path
import random
import re
import threading
import time
from typing import Any, Callable
import uuid

from backend.config_store import ConfigStore

logger = logging.getLogger("jukebox.controller")


def _filter_kwargs(callable_obj: Any, kwargs: dict[str, Any]) -> dict[str, Any]:
    """Filter a keyword dictionary to only parameters accepted by callable_obj."""
    try:
        sig = inspect.signature(callable_obj)
        for param in sig.parameters.values():
            if param.kind == inspect.Parameter.VAR_KEYWORD:
                return kwargs
        return {k: v for k, v in kwargs.items() if k in sig.parameters}
    except Exception:
        return kwargs


class JukeboxController:
    """Integrates ConfigStore, YTMusicService, AudioEngine, and OcrChatWatcher with a 24h Auto-DJ daemon."""

    def __init__(
        self,
        config_store: ConfigStore | None = None,
        dummy_sink: bool | None = None,
        start_daemons: bool = True,
    ) -> None:
        env_dummy = os.environ.get("JUKEBOX_DUMMY_SINK", "").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
        self.dummy_sink: bool = bool(dummy_sink) if dummy_sink is not None else env_dummy

        self.config_store: ConfigStore = config_store or ConfigStore()
        self.data_dir: Path = self.config_store.data_dir
        self.trash_dir: Path = self.config_store.trash_dir

        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._autodj_wakeup = threading.Event()
        self._preload_wakeup = threading.Event()

        self._boot_time: float = time.time()
        self._queue: list[dict[str, Any]] = []
        self._history: deque[dict[str, Any]] = deque(maxlen=50)
        self._events: deque[dict[str, Any]] = deque(maxlen=40)

        self._fallback_tracks: list[dict[str, Any]] = []
        self._fallback_index: int = 0
        self._fallback_loading: bool = False
        self._last_fallback_fetch_ts: float = 0.0

        self._resolved_cache: dict[str, str] = {}
        self._failed_video_ids: dict[str, float] = {}
        self._consecutive_failures: int = 0
        self._max_consecutive_failures: int = 5
        self._ytdlp_auth_blocked: bool = False

        cfg = self.config_store.get()

        # Persistent 24h already-played ledger so heard songs are not re-added via OCR
        self.played_dedup_sec: float = max(0.0, float(cfg.get("played_dedup_sec", 86400.0)))
        self._played_history_path: Path | None = (
            None if self.dummy_sink else (self.data_dir / "played_history.json")
        )
        self._played_video_ids: dict[str, float] = {}
        self._played_title_keys: dict[str, float] = {}
        self._load_played_history()

        self._current_item: dict[str, Any] | None = None
        self._track_just_finished: bool = False
        self._force_play_next: bool = False
        self._manual_stop: bool = False
        self._manual_override_active: bool = False
        self._last_schedule_active: bool | None = None
        self._last_in_window: bool = False
        self._test_tone_until: float = 0.0

        self._ws_listeners: list[Callable[[dict[str, Any]], None]] = []
        self._ws_lock = threading.RLock()

        from backend.ytmusic_service import YTMusicService
        from backend.audio_engine import AudioEngine
        from backend.ocr_watcher import OcrChatWatcher

        proxy_val = str(cfg.get("proxy") or cfg.get("ytmusic", {}).get("proxy") or "")
        ytm_kwargs = _filter_kwargs(
            YTMusicService.__init__,
            {
                "data_dir": self.data_dir,
                "trash_dir": self.trash_dir,
                "proxy": proxy_val,
                "cache_max_mb": int(cfg.get("cache_max_mb", 1024)),
            },
        )
        self.ytmusic = YTMusicService(**ytm_kwargs)
        if hasattr(self.ytmusic, "cache_max_mb"):
            try:
                setattr(self.ytmusic, "cache_max_mb", int(cfg.get("cache_max_mb", 1024)))
            except Exception:
                pass

        audio_init_kwargs = _filter_kwargs(
            AudioEngine.__init__,
            {
                "sample_rate": 48000,
                "channels": 2,
                "blocksize": 1024,
                "dummy_sink": self.dummy_sink,
            },
        )
        self.audio_engine = AudioEngine(**audio_init_kwargs)
        self._apply_audio_config_to_engine(cfg.get("audio", {}), keep_recommended_if_none=True)
        if hasattr(self.audio_engine, "sanitize_routing_ids") and self.audio_engine.sanitize_routing_ids():
            healed_audio = {
                "virtual_out_device_id": self.audio_engine.virtual_out_device_id,
                "monitor_out_device_id": self.audio_engine.monitor_out_device_id,
                "mic_in_device_id": self.audio_engine.mic_in_device_id,
            }
            self.config_store.update({"audio": healed_audio})
            logger.info("Detected PortAudio device ID drift; auto-healed audio routing to %s", healed_audio)

        ocr_init_kwargs = _filter_kwargs(
            OcrChatWatcher.__init__,
            {
                "on_command": self._on_ocr_command,
                "seen_store_path": None if self.dummy_sink else (self.data_dir / "ocr_seen.json"),
            },
        )
        self.ocr_watcher = OcrChatWatcher(**ocr_init_kwargs)
        self._apply_ocr_config_to_watcher(cfg.get("ocr", {}))

        self._scan_existing_disk_cache()

        self._autodj_thread: threading.Thread | None = None
        self._preload_thread: threading.Thread | None = None

        if start_daemons:
            self._start_background_daemons()

    @property
    def lock(self) -> threading.RLock:
        """Expose controller re-entrant lock for thread-safety inspection."""
        return self._lock

    @property
    def auto_dj_enabled(self) -> bool:
        """Return whether 24h Auto-DJ mode is currently enabled."""
        return bool(self.config_store.get().get("auto_dj_enabled", True))

    def _scan_existing_disk_cache(self) -> None:
        """Populate in-memory resolved cache map from existing files in data/cache."""
        cache_dir = self.data_dir / "cache"
        if not cache_dir.exists():
            return
        try:
            for entry in cache_dir.iterdir():
                if not entry.is_file():
                    continue
                if entry.suffix.lower() in (".part", ".tmp", ".ytdl"):
                    continue
                if entry.stat().st_size > 1024:
                    with self._lock:
                        self._resolved_cache[entry.stem] = str(entry.resolve())
        except Exception:
            pass

    def _start_background_daemons(self) -> None:
        """Launch the scheduled Auto-DJ scheduler and asynchronous preload daemon threads."""
        sched = self._sync_schedule_state(emit_events=False)
        self._preload_thread = threading.Thread(
            target=self._preload_loop,
            name="JukeboxPreloadDaemon",
            daemon=True,
        )
        self._autodj_thread = threading.Thread(
            target=self._autodj_loop,
            name="JukeboxAutoDJDaemon",
            daemon=True,
        )
        self._preload_thread.start()
        self._autodj_thread.start()

        ocr_cfg = self.config_store.get().get("ocr", {})
        if ocr_cfg.get("enabled", False) and sched.get("is_active", True):
            try:
                self.ocr_watcher.start()
            except Exception as exc:
                logger.warning("Failed to auto-start OCR watcher: %s", exc)
        elif not sched.get("is_active", True):
            try:
                self.ocr_watcher.stop()
            except Exception:
                pass
            self.emit_event(
                "info",
                f"当前为非播音时段（静音待机，0 扫描）；下次开播：{sched.get('next_start_label', '')}",
            )

    def register_ws_listener(self, listener: Callable[[dict[str, Any]], None]) -> None:
        """Register a callback to receive realtime state and event payloads."""
        with self._ws_lock:
            if listener not in self._ws_listeners:
                self._ws_listeners.append(listener)

    def unregister_ws_listener(self, listener: Callable[[dict[str, Any]], None]) -> None:
        """Unregister a previously registered WebSocket listener callback."""
        with self._ws_lock:
            self._ws_listeners = [cb for cb in self._ws_listeners if cb != listener]

    def _notify_ws(self, payload: dict[str, Any]) -> None:
        """Dispatch a message payload to all registered WebSocket listeners."""
        with self._ws_lock:
            listeners = list(self._ws_listeners)
        for callback in listeners:
            try:
                callback(payload)
            except Exception:
                pass

    def emit_event(self, level: str, message: str) -> None:
        """Record a system event and broadcast it over WebSocket."""
        evt = {
            "type": "event",
            "level": level if level in ("info", "warn", "error") else "info",
            "message": str(message),
            "timestamp": time.time(),
        }
        with self._lock:
            self._events.appendleft(evt)
        self._notify_ws(evt)

    def broadcast_state(self) -> None:
        """Broadcast a complete state snapshot to all connected WebSocket clients."""
        try:
            snapshot = self.get_state()
            self._notify_ws({"type": "state", "data": snapshot})
        except Exception as exc:
            logger.debug("Broadcast state error: %s", exc)

    def _apply_audio_config_to_engine(
        self,
        audio_cfg: dict[str, Any],
        keep_recommended_if_none: bool = False,
    ) -> None:
        """Forward audio routing and ducking configuration to AudioEngine."""
        if not hasattr(self.audio_engine, "update_config"):
            return
        try:
            payload = copy.deepcopy(audio_cfg)
            if keep_recommended_if_none:
                for dev_key in ("virtual_out_device_id", "monitor_out_device_id", "mic_in_device_id", "mic_device_id"):
                    if payload.get(dev_key) is None:
                        payload.pop(dev_key, None)
            if "mic_in_device_id" in payload and "mic_device_id" not in payload:
                payload["mic_device_id"] = payload["mic_in_device_id"]
            if "duck_threshold_db" in payload and "ducking_threshold_db" not in payload:
                payload["ducking_threshold_db"] = payload["duck_threshold_db"]
            if "ducking_db" in payload and "duck_db" not in payload:
                payload["duck_db"] = payload["ducking_db"]
            filtered = _filter_kwargs(self.audio_engine.update_config, payload)
            self.audio_engine.update_config(**filtered)
        except Exception as exc:
            logger.warning("Failed to apply audio config to engine: %s", exc)

    def evaluate_broadcast_schedule(self, now: datetime | None = None) -> dict[str, Any]:
        """Evaluate scheduled broadcast window, active mode, and next start countdown."""
        cfg = self.config_store.get()
        raw_sched = cfg.get("broadcast_schedule")
        sched_cfg = raw_sched if isinstance(raw_sched, dict) else {}

        enabled = bool(sched_cfg.get("enabled", True))
        raw_days = sched_cfg.get("days", [3, 6])
        days: list[int] = []
        if isinstance(raw_days, (list, tuple, set)):
            for d in raw_days:
                try:
                    di = int(d)
                except (TypeError, ValueError):
                    continue
                if 0 <= di <= 6 and di not in days:
                    days.append(di)
            days.sort()
        else:
            days = [3, 6]

        start_str = str(sched_cfg.get("start") or "19:30").strip()
        end_str = str(sched_cfg.get("end") or "20:20").strip()
        clear_queue_on_end = bool(sched_cfg.get("clear_queue_on_end", True))

        cur = now if now is not None else datetime.now()
        try:
            sh, sm = (int(x) for x in start_str.split(":", 1))
            eh, em = (int(x) for x in end_str.split(":", 1))
            start_dt = cur.replace(hour=sh, minute=sm, second=0, microsecond=0)
            end_dt = cur.replace(hour=eh, minute=em, second=0, microsecond=0)
            start_str = f"{sh:02d}:{sm:02d}"
            end_str = f"{eh:02d}:{em:02d}"
        except Exception:
            sh, sm = 19, 30
            start_dt = cur.replace(hour=19, minute=30, second=0, microsecond=0)
            end_dt = cur.replace(hour=20, minute=20, second=0, microsecond=0)
            start_str, end_str = "19:30", "20:20"

        valid_window = end_dt > start_dt
        in_window = bool(valid_window and cur.weekday() in days and start_dt <= cur < end_dt)

        with self._lock:
            manual_override = bool(self._manual_override_active)

        is_active = bool((not enabled) or in_window or manual_override)
        if not enabled:
            mode = "always_on"
        elif in_window:
            mode = "live_scheduled"
        elif manual_override:
            mode = "live_override"
        else:
            mode = "standby"

        day_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        days_label = (
            "每" + "、".join(day_names[d] for d in days if 0 <= d <= 6)
            if days
            else "未设置播音日"
        )
        next_start_iso = ""
        next_start_label = "未设置播音时段"
        seconds_until_next_start = 0.0
        seconds_until_end = float(round(max(0.0, (end_dt - cur).total_seconds()), 1)) if in_window else 0.0

        if valid_window and days:
            next_dt: datetime | None = None
            for offset in range(0, 8):
                cand_day = cur + timedelta(days=offset)
                if cand_day.weekday() not in days:
                    continue
                cand_start = cand_day.replace(hour=sh, minute=sm, second=0, microsecond=0)
                if cand_start > cur:
                    next_dt = cand_start
                    break
            if next_dt is not None:
                next_start_iso = next_dt.strftime("%Y-%m-%d %H:%M")
                w_name = day_names[next_dt.weekday()]
                day_diff = (next_dt.date() - cur.date()).days
                if day_diff == 0:
                    next_start_label = f"今天 {start_str}（{w_name}）"
                elif day_diff == 1:
                    next_start_label = f"明天 {start_str}（{w_name}）"
                else:
                    next_start_label = f"{w_name} {start_str}"
                if not in_window:
                    seconds_until_next_start = float(round(max(0.0, (next_dt - cur).total_seconds()), 1))

        return {
            "enabled": enabled,
            "days": days,
            "start": start_str,
            "end": end_str,
            "clear_queue_on_end": clear_queue_on_end,
            "in_window": in_window,
            "manual_override": manual_override,
            "is_active": is_active,
            "mode": mode,
            "days_label": days_label,
            "next_start_iso": next_start_iso,
            "next_start_label": next_start_label,
            "seconds_until_next_start": seconds_until_next_start,
            "seconds_until_end": seconds_until_end,
        }

    def is_broadcast_active(self, now: datetime | None = None) -> bool:
        """Return True if broadcast playback and OCR watching should be active right now."""
        return bool(self.evaluate_broadcast_schedule(now=now).get("is_active", True))

    def _sync_schedule_state(
        self,
        now: datetime | None = None,
        *,
        emit_events: bool = True,
    ) -> dict[str, Any]:
        """Synchronize playback and OCR daemon lifecycles on broadcast schedule transitions."""
        sched = self.evaluate_broadcast_schedule(now=now)
        should_init_stop = False
        should_activate = False
        should_deactivate = False
        left_window_cleanly = False
        cleared_count = 0

        with self._lock:
            if sched["in_window"] and self._manual_override_active:
                self._manual_override_active = False
                sched["manual_override"] = False

            prev_active = self._last_schedule_active
            prev_in_window = self._last_in_window
            cur_active = bool(sched["is_active"])
            self._last_schedule_active = cur_active
            self._last_in_window = bool(sched["in_window"])

            if prev_active is None and not cur_active:
                should_init_stop = True
            elif prev_active is False and cur_active is True:
                should_activate = True
            elif prev_active is True and cur_active is False:
                should_deactivate = True
                self._current_item = None
                self._force_play_next = False
                self._track_just_finished = False
                left_window_cleanly = bool(prev_in_window and not sched["in_window"])
                if sched.get("clear_queue_on_end", True) and left_window_cleanly:
                    cleared_count = len(self._queue)
                    self._queue = []

        if should_init_stop:
            try:
                self.ocr_watcher.stop()
            except Exception:
                pass
        elif should_activate:
            self._manual_stop = False
            self._preload_wakeup.set()
            self._autodj_wakeup.set()
            ocr_cfg = self.config_store.get().get("ocr", {})
            if ocr_cfg.get("enabled", False):
                try:
                    self.ocr_watcher.start()
                except Exception as exc:
                    logger.warning("Failed to start OCR watcher on schedule activation: %s", exc)
            if emit_events:
                if sched["mode"] == "live_scheduled":
                    msg = f"定时播音时段已开启（{sched['days_label']} {sched['start']}–{sched['end']}），已唤醒播放与 OCR 识别"
                elif sched["mode"] == "always_on":
                    msg = "定时限制已关闭，点歌台保持全天候运行"
                else:
                    msg = "已开启临时试播，正在唤醒播放器与 OCR 识别"
                self.emit_event("info", msg)
                self.broadcast_state()
        elif should_deactivate:
            try:
                self.audio_engine.stop()
            except Exception:
                pass
            try:
                self.ocr_watcher.stop()
            except Exception:
                pass
            self._preload_wakeup.set()
            if emit_events:
                if left_window_cleanly:
                    extra = f"，已自动清空剩余队列 {cleared_count} 首" if cleared_count else ""
                    msg = (
                        f"定时播音时段已结束（{sched['end']}），已静音待机并暂停 OCR 识别{extra}；"
                        f"下次开播：{sched['next_start_label']}"
                    )
                else:
                    msg = f"已结束试播并进入静音待机（0 播放 / 0 OCR）；下次开播：{sched['next_start_label']}"
                self.emit_event("info", msg)
                self.broadcast_state()

        return sched

    def _apply_ocr_config_to_watcher(self, ocr_cfg: dict[str, Any]) -> None:
        """Forward OCR configuration to OcrChatWatcher and manage active-window start/stop."""
        if not hasattr(self.ocr_watcher, "update_config"):
            return
        try:
            cfg_copy = copy.deepcopy(ocr_cfg)
            filtered = _filter_kwargs(self.ocr_watcher.update_config, cfg_copy)
            if "enabled" in cfg_copy:
                cfg_enabled = bool(cfg_copy["enabled"])
                filtered["enabled"] = bool(cfg_enabled and self.is_broadcast_active())
            self.ocr_watcher.update_config(**filtered)
        except Exception as exc:
            logger.warning("Failed to apply OCR config to watcher: %s", exc)

    def update_schedule_config(self, config_patch: dict[str, Any]) -> dict[str, Any]:
        """Save and hot-apply scheduled broadcast window configuration."""
        updated_cfg = self.config_store.update_schedule(config_patch)
        self._apply_ocr_config_to_watcher(updated_cfg.get("ocr", {}))
        sched = self._sync_schedule_state(emit_events=True)
        self.emit_event(
            "info",
            f"定时播音设置已保存（{sched['days_label']} {sched['start']}–{sched['end']}，{'仅定时开播' if sched['enabled'] else '全天允许'}）",
        )
        self.broadcast_state()
        state = self.get_state()
        return {
            "ok": True,
            "status": "ok",
            "schedule": state.get("schedule", sched),
            "ocr": state.get("ocr"),
        }

    def set_schedule_override(self, active: bool) -> dict[str, Any]:
        """Enable or disable temporary manual live override outside scheduled hours."""
        with self._lock:
            self._manual_override_active = bool(active)
        if active:
            self._manual_stop = False
            self._preload_wakeup.set()
            self._autodj_wakeup.set()
        sched = self._sync_schedule_state(emit_events=True)
        self.broadcast_state()
        return {
            "ok": True,
            "status": "ok",
            "schedule": sched,
            "player": self.get_player_status(),
        }

    def _normalize_track(
        self,
        raw_track: dict[str, Any],
        requested_title: str | None = None,
        requested_artist: str | None = None,
    ) -> dict[str, Any]:
        """Ensure track metadata dictionary contains all canonical fields."""
        video_id = str(
            raw_track.get("video_id")
            or raw_track.get("videoId")
            or f"track_{uuid.uuid4().hex[:8]}"
        ).strip()
        raw_title = str(raw_track.get("title") or "").strip()
        raw_artist = str(raw_track.get("artist") or "").strip()
        is_real_yt_id = bool(video_id and not video_id.startswith(("ocr_", "track_")))

        title = raw_title or (requested_title or "").strip() or "未知曲目"
        artist = raw_artist or (requested_artist or "").strip() or "未知歌手"

        if not is_real_yt_id:
            if requested_title and requested_title.strip() and requested_title.strip() not in title:
                title = f"{requested_title.strip()} ({title})"
            if requested_artist and requested_artist.strip() and requested_artist.strip() not in artist:
                artist = f"{requested_artist.strip()} ({artist})" if artist and artist != "未知歌手" else requested_artist.strip()

        try:
            duration_sec = int(float(raw_track.get("duration_sec") or raw_track.get("duration") or 0))
        except (TypeError, ValueError):
            duration_sec = 0

        thumbnail_url = str(
            raw_track.get("thumbnail_url")
            or raw_track.get("thumbnail")
            or ""
        ).strip()

        return {
            "video_id": video_id,
            "title": title,
            "artist": artist,
            "duration_sec": max(0, duration_sec),
            "thumbnail_url": thumbnail_url,
        }

    def enqueue_track(
        self,
        track: dict[str, Any],
        requester: str = "少侠",
        source: str = "web",
        pin_top: bool = False,
        requested_title: str | None = None,
        requested_artist: str | None = None,
        dedup_queue: bool = False,
    ) -> dict[str, Any] | None:
        """Thread-safely add a normalized track into the priority request queue."""
        norm_track = self._normalize_track(
            track,
            requested_title=requested_title,
            requested_artist=requested_artist,
        )
        vid = norm_track["video_id"]

        player_state = "stopped"
        if dedup_queue and not pin_top:
            try:
                player_state = str((self.audio_engine.get_status() or {}).get("state", "stopped"))
            except Exception:
                player_state = "stopped"

        with self._lock:
            if dedup_queue:
                if (
                    not pin_top
                    and player_state in ("playing", "paused")
                    and self._current_item is not None
                    and self._current_item.get("video_id") == vid
                ):
                    dup_msg = f"《{norm_track['title']}》正在播放中，已跳过重复点歌"
                    dup_item = None
                elif not pin_top and any(q.get("video_id") == vid for q in self._queue):
                    dup_msg = f"《{norm_track['title']}》已在点歌队列中，已跳过重复添加"
                    dup_item = None
                elif not pin_top and self._is_track_already_played(vid, norm_track["title"]):
                    dup_msg = f"《{norm_track['title']}》24小时内已听过，已跳过不再加入列表"
                    dup_item = None
                elif pin_top and any(q.get("video_id") == vid for q in self._queue):
                    matched_item = None
                    rest_items: list[dict[str, Any]] = []
                    for q in self._queue:
                        if q.get("video_id") == vid and matched_item is None:
                            matched_item = q
                        else:
                            rest_items.append(q)
                    assert matched_item is not None
                    self._queue = [matched_item] + rest_items
                    dup_msg = f"已插播置顶：《{norm_track['title']}》- {norm_track['artist']}"
                    dup_item = copy.deepcopy(matched_item)
                else:
                    dup_msg = ""
                    dup_item = False  # sentinel: proceed with normal append
            else:
                dup_msg = ""
                dup_item = False

            if dup_item is not False:
                item_copy = dup_item
            else:
                is_cached = vid in self._resolved_cache and Path(self._resolved_cache[vid]).exists()
                item: dict[str, Any] = {
                    "item_id": uuid.uuid4().hex[:10],
                    "track": norm_track,
                    "video_id": norm_track["video_id"],
                    "title": norm_track["title"],
                    "artist": norm_track["artist"],
                    "duration_sec": norm_track["duration_sec"],
                    "thumbnail_url": norm_track["thumbnail_url"],
                    "requester": str(requester or "少侠").strip() or "少侠",
                    "source": str(source if source in ("ocr", "web", "fallback") else "web"),
                    "added_at": time.time(),
                    "status": "ready" if is_cached else "queued",
                }
                if pin_top:
                    self._queue.insert(0, item)
                else:
                    self._queue.append(item)
                item_copy = copy.deepcopy(item)

        if dup_msg:
            self._preload_wakeup.set()
            self.emit_event("info", dup_msg)
            self.broadcast_state()
            return item_copy

        self._preload_wakeup.set()
        self._autodj_wakeup.set()
        self.emit_event(
            "info",
            f"已加入点歌队列：《{norm_track['title']}》- {norm_track['artist']}（来自 {item['requester']}）",
        )
        self.broadcast_state()
        return item_copy

    def _resolve_track_from_request(
        self,
        query: str | None = None,
        video_id: str | None = None,
        title: str | None = None,
        artist: str | None = None,
        duration_sec: int | float | None = None,
        thumbnail_url: str | None = None,
        track: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, str | None, str | None]:
        """Resolve a track metadata dict from direct fields or YouTube Music search."""
        if isinstance(track, dict) and (track.get("video_id") or track.get("videoId")):
            return (
                self._normalize_track(track, requested_title=title, requested_artist=artist),
                title,
                artist,
            )

        if video_id and title:
            return (
                self._normalize_track(
                    {
                        "video_id": video_id,
                        "title": title,
                        "artist": artist or "未知歌手",
                        "duration_sec": duration_sec or 0,
                        "thumbnail_url": thumbnail_url or "",
                    },
                    requested_title=title,
                    requested_artist=artist,
                ),
                title,
                artist,
            )

        if video_id and not title:
            try:
                results = self.ytmusic.search_songs(video_id, limit=3)
                for candidate in results:
                    if candidate.get("video_id") == video_id:
                        return candidate, None, artist
                if results:
                    return results[0], None, artist
            except Exception:
                pass
            return (
                {
                    "video_id": video_id,
                    "title": title or video_id,
                    "artist": artist or "未知歌手",
                    "duration_sec": int(duration_sec or 0),
                    "thumbnail_url": thumbnail_url or "",
                },
                title,
                artist,
            )

        search_title = (title or "").strip()
        search_artist = (artist or "").strip()
        raw_query = (query or "").strip()

        if not search_title and raw_query:
            for sep in ("-", "—", "–", "－"):
                if sep in raw_query:
                    parts = [p.strip() for p in raw_query.split(sep, 1)]
                    if parts[0]:
                        search_title = parts[0]
                        if len(parts) > 1 and parts[1] and not search_artist:
                            search_artist = parts[1]
                    break
            if not search_title:
                search_title = raw_query

        if search_title:
            try:
                best = self.ytmusic.search_best_match(search_title, search_artist)
                if best:
                    return best, search_title, search_artist
            except Exception as exc:
                logger.warning("search_best_match failed for %s - %s: %s", search_title, search_artist, exc)

            try:
                combined_q = f"{search_title} {search_artist}".strip()
                results = self.ytmusic.search_songs(combined_q, limit=5)
                if results:
                    return results[0], search_title, search_artist
            except Exception as exc:
                logger.warning("search_songs fallback failed for %s: %s", search_title, exc)

        return None, search_title or None, search_artist or None

    def add_to_queue(
        self,
        query: str | None = None,
        video_id: str | None = None,
        title: str | None = None,
        artist: str | None = None,
        requester: str = "少侠",
        pin_top: bool = False,
        source: str = "web",
        duration_sec: int | float | None = None,
        thumbnail_url: str | None = None,
        track: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Resolve and add a song to the priority queue."""
        resolved_track, req_title, req_artist = self._resolve_track_from_request(
            query=query,
            video_id=video_id,
            title=title,
            artist=artist,
            duration_sec=duration_sec,
            thumbnail_url=thumbnail_url,
            track=track,
        )
        if not resolved_track:
            raise ValueError("未找到匹配的歌曲，请尝试提供更准确的歌名或歌手")

        item = self.enqueue_track(
            resolved_track,
            requester=requester,
            source=source,
            pin_top=pin_top,
            requested_title=req_title,
            requested_artist=req_artist,
        )
        return {
            "ok": True,
            "status": "ok",
            "item": item,
            "queue": self.get_queue(),
        }

    def remove_from_queue(self, item_id: str) -> dict[str, Any]:
        """Remove a specific item from the priority queue by item_id."""
        removed_item: dict[str, Any] | None = None
        with self._lock:
            new_queue: list[dict[str, Any]] = []
            for item in self._queue:
                if item.get("item_id") == item_id and removed_item is None:
                    removed_item = item
                else:
                    new_queue.append(item)
            self._queue = new_queue

        if removed_item is not None:
            self._preload_wakeup.set()
            self.emit_event("info", f"已从队列移除：《{removed_item['track']['title']}》")
            self.broadcast_state()

        return {
            "ok": removed_item is not None,
            "status": "ok" if removed_item is not None else "not_found",
            "removed": removed_item,
            "queue": self.get_queue(),
        }

    def pin_queue_item(self, item_id: str) -> dict[str, Any]:
        """Move a specific item in the priority queue to index 0 (top priority)."""
        target_item: dict[str, Any] | None = None
        with self._lock:
            remaining: list[dict[str, Any]] = []
            for item in self._queue:
                if item.get("item_id") == item_id and target_item is None:
                    target_item = item
                else:
                    remaining.append(item)
            if target_item is not None:
                self._queue = [target_item] + remaining

        if target_item is not None:
            self._preload_wakeup.set()
            self.emit_event("info", f"已置顶曲目：《{target_item['track']['title']}》")
            self.broadcast_state()

        return {
            "ok": target_item is not None,
            "status": "ok" if target_item is not None else "not_found",
            "pinned": target_item,
            "queue": self.get_queue(),
        }

    def clear_queue(self) -> dict[str, Any]:
        """Clear all pending items in the priority request queue."""
        with self._lock:
            count = len(self._queue)
            self._queue = []
        self.emit_event("info", f"已清空点歌队列（共清理 {count} 首）")
        self.broadcast_state()
        return {
            "ok": True,
            "status": "ok",
            "cleared_count": count,
            "queue": [],
        }

    def get_queue(self) -> list[dict[str, Any]]:
        """Return a deep copy of the current priority queue."""
        with self._lock:
            return copy.deepcopy(self._queue)

    def get_history(self) -> list[dict[str, Any]]:
        """Return a deep copy of the recent playback history (up to 50 tracks)."""
        with self._lock:
            return copy.deepcopy(list(self._history))

    def get_player_status(self) -> dict[str, Any]:
        """Return normalized player status merged with active track and routing config."""
        raw_status = {}
        try:
            raw_status = self.audio_engine.get_status() or {}
        except Exception as exc:
            logger.debug("audio_engine.get_status error: %s", exc)

        cfg = self.config_store.get()
        audio_cfg = copy.deepcopy(cfg.get("audio", {}))
        engine_routing = raw_status.get("routing")
        if isinstance(engine_routing, dict):
            audio_cfg.update({k: v for k, v in engine_routing.items() if v is not None})

        with self._lock:
            current_item = copy.deepcopy(self._current_item)

        track_info = raw_status.get("track")
        if track_info is None and current_item is not None and raw_status.get("state") in ("playing", "paused"):
            track_info = copy.deepcopy(current_item.get("track"))
        if isinstance(track_info, dict) and current_item is not None:
            track_info = copy.deepcopy(track_info)
            track_info.setdefault("requester", current_item.get("requester", "帮派电台"))
            track_info.setdefault("source", current_item.get("source", "fallback"))
            track_info.setdefault("item_id", current_item.get("item_id", ""))

        sched = self.evaluate_broadcast_schedule()
        status = {
            "state": str(raw_status.get("state", "stopped")),
            "track": track_info,
            "current_item": current_item,
            "position_sec": float(raw_status.get("position_sec", 0.0) or 0.0),
            "duration_sec": float(
                raw_status.get("duration_sec")
                or (track_info.get("duration_sec", 0.0) if isinstance(track_info, dict) else 0.0)
                or 0.0
            ),
            "broadcast_rms_db": float(raw_status.get("broadcast_rms_db", -100.0)),
            "peak_db": float(raw_status.get("peak_db", -100.0)),
            "mic_rms_db": float(raw_status.get("mic_rms_db", -100.0)),
            "ducking_active": bool(raw_status.get("ducking_active", False)),
            "routing": audio_cfg,
            "vbcable_installed": bool(raw_status.get("vbcable_installed", False)),
            "auto_dj_enabled": bool(cfg.get("auto_dj_enabled", True)),
            "schedule_mode": sched["mode"],
            "standby": bool(sched["mode"] == "standby"),
            "dummy_sink": self.dummy_sink,
        }
        return status

    def _get_fallback_snapshot(self) -> dict[str, Any]:
        """Return a thread-safe snapshot of the 24-hour fallback pool."""
        cfg = self.config_store.get()
        fb_source = str(cfg.get("fallback_source") or "search:天涯明月刀 古风 经典")
        fb_shuffle = bool(cfg.get("fallback_shuffle", True))
        with self._lock:
            total = len(self._fallback_tracks)
            idx = self._fallback_index
            preview_tracks = copy.deepcopy(self._fallback_tracks[:50])
            next_track = (
                copy.deepcopy(self._fallback_tracks[idx % total])
                if total > 0
                else None
            )
        return {
            "fallback_source": fb_source,
            "source": fb_source,
            "shuffle": fb_shuffle,
            "fallback_shuffle": fb_shuffle,
            "total_count": total,
            "count": total,
            "current_index": idx,
            "tracks": preview_tracks,
            "next_track": next_track,
        }

    def get_state(self) -> dict[str, Any]:
        """Build a complete state snapshot for GET /api/state and WebSocket state frames."""
        cfg = self.config_store.get()
        player_status = self.get_player_status()

        try:
            devices_info = self.audio_engine.list_devices() or {}
        except Exception:
            devices_info = {
                "outputs": [],
                "inputs": [],
                "vbcable_installed": False,
                "recommended_virtual_out_id": None,
                "recommended_monitor_out_id": None,
                "recommended_mic_in_id": None,
            }
        devices_info["config"] = copy.deepcopy(cfg.get("audio", {}))

        try:
            ytm_status = self.ytmusic.get_auth_status() or {}
        except Exception as exc:
            ytm_status = {
                "authenticated": False,
                "mode": "guest",
                "has_cookies_txt": False,
                "proxy": cfg.get("proxy", ""),
                "message": str(exc),
            }
        ytm_status["fallback_source"] = cfg.get("fallback_source", "search:天涯明月刀 古风 经典")
        ytm_status["fallback_shuffle"] = bool(cfg.get("fallback_shuffle", True))
        ytm_status["shuffle"] = bool(cfg.get("fallback_shuffle", True))
        ytm_status["cache_max_mb"] = int(cfg.get("cache_max_mb", 1024))

        try:
            ocr_status = self.ocr_watcher.get_status() or {}
        except Exception:
            ocr_status = {}
        sched = self.evaluate_broadcast_schedule()
        cfg_ocr_enabled = bool(cfg.get("ocr", {}).get("enabled", False))
        merged_ocr = copy.deepcopy(cfg.get("ocr", {}))
        merged_ocr.update({k: v for k, v in ocr_status.items() if v is not None})
        merged_ocr["enabled"] = cfg_ocr_enabled
        merged_ocr["standby_paused"] = bool(cfg_ocr_enabled and not sched["is_active"])
        recent_logs = merged_ocr.get("recent_logs") or merged_ocr.get("logs") or []
        merged_ocr["recent_logs"] = recent_logs
        merged_ocr["logs"] = recent_logs

        with self._lock:
            queue_copy = copy.deepcopy(self._queue)
            history_copy = copy.deepcopy(list(self._history))
            events_copy = copy.deepcopy(list(self._events))

        return {
            "auto_dj_enabled": bool(cfg.get("auto_dj_enabled", True)),
            "dummy_sink": self.dummy_sink,
            "schedule": sched,
            "player": player_status,
            "audio_devices": devices_info,
            "audio_config": copy.deepcopy(cfg.get("audio", {})),
            "queue": queue_copy,
            "history": history_copy,
            "fallback": self._get_fallback_snapshot(),
            "ytmusic": ytm_status,
            "ocr": merged_ocr,
            "events": events_copy,
            "config": cfg,
        }

    def player_control(
        self,
        action: str,
        seek_sec: float | None = None,
        enabled: bool | None = None,
        duration_sec: float | None = None,
    ) -> dict[str, Any]:
        """Execute playback or Auto-DJ control actions."""
        act = (action or "").strip().lower()

        if act == "toggle_autodj":
            current = self.auto_dj_enabled
            new_val = bool(enabled) if enabled is not None else (not current)
            self.config_store.update({"auto_dj_enabled": new_val})
            if new_val:
                self._manual_stop = False
                self._autodj_wakeup.set()
                self._preload_wakeup.set()
            self.emit_event("info", f"24h Auto-DJ 自动巡航已{'开启' if new_val else '关闭'}")
            self.broadcast_state()
            return {"ok": True, "status": "ok", "auto_dj_enabled": new_val, "player": self.get_player_status()}

        if act == "test_tone":
            dur = float(duration_sec if duration_sec is not None else 3.0)
            self._test_tone_until = time.time() + dur + 0.3
            self.audio_engine.play_test_tone(duration_sec=dur)
            self.emit_event("info", f"正在发送 {dur:.1f} s 440 Hz 测试音")
            self.broadcast_state()
            return {"ok": True, "status": "ok", "player": self.get_player_status()}

        if act == "pause":
            self.audio_engine.pause()
            self.broadcast_state()
            return {"ok": True, "status": "ok", "player": self.get_player_status()}

        if act == "resume":
            self._manual_stop = False
            self.audio_engine.resume()
            self.broadcast_state()
            return {"ok": True, "status": "ok", "player": self.get_player_status()}

        if act == "seek":
            if seek_sec is not None:
                self.audio_engine.seek(float(seek_sec))
            self.broadcast_state()
            return {"ok": True, "status": "ok", "player": self.get_player_status()}

        if act == "stop":
            self._manual_stop = True
            self.audio_engine.stop()
            if self._manual_override_active:
                with self._lock:
                    self._manual_override_active = False
                self._sync_schedule_state(emit_events=True)
            else:
                self.broadcast_state()
            return {"ok": True, "status": "ok", "player": self.get_player_status()}

        if act == "skip":
            self.skip_track(reason="手动切歌")
            return {"ok": True, "status": "ok", "player": self.get_player_status()}

        if act == "play":
            sched = self.evaluate_broadcast_schedule()
            if sched.get("enabled") and not sched.get("in_window") and not self._manual_override_active:
                with self._lock:
                    self._manual_override_active = True
                self._sync_schedule_state(emit_events=True)
            self._manual_stop = False
            if seek_sec is not None:
                st = self.audio_engine.get_status() or {}
                if st.get("state") in ("playing", "paused"):
                    self.audio_engine.seek(float(seek_sec))
                    if st.get("state") == "paused":
                        self.audio_engine.resume()
                    self.broadcast_state()
                    return {"ok": True, "status": "ok", "player": self.get_player_status()}

            st = self.audio_engine.get_status() or {}
            if st.get("state") == "paused":
                self.audio_engine.resume()
            elif st.get("state") == "stopped":
                self._force_play_next = True
                self._autodj_wakeup.set()
            self.broadcast_state()
            return {"ok": True, "status": "ok", "player": self.get_player_status()}

        raise ValueError(f"Unsupported player control action: {action}")

    def skip_track(self, reason: str = "切歌") -> None:
        """Stop current track and trigger immediate transition to the next track."""
        self._manual_stop = False
        self._test_tone_until = 0.0
        self._force_play_next = True
        try:
            self.audio_engine.stop()
        except Exception:
            pass
        self.emit_event("info", f"已触发切歌（{reason}）")
        self._autodj_wakeup.set()
        self.broadcast_state()

    def list_audio_devices(self) -> dict[str, Any]:
        """Refresh and return available audio input/output devices."""
        devices = self.audio_engine.list_devices() or {}
        devices["config"] = self.config_store.get().get("audio", {})
        return devices

    def update_audio_config(self, config_patch: dict[str, Any]) -> dict[str, Any]:
        """Hot-update audio routing, volumes, and auto-ducking parameters and persist to disk."""
        updated_cfg = self.config_store.update_audio(config_patch)
        audio_section = updated_cfg.get("audio", {})
        self._apply_audio_config_to_engine(audio_section)
        self.emit_event("info", "音频路由与防炸麦自动闪避配置已更新")
        self.broadcast_state()
        return {
            "ok": True,
            "status": "ok",
            "audio": audio_section,
            "player": self.get_player_status(),
        }

    def search_ytmusic(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        """Search songs on YouTube Music."""
        q = (query or "").strip()
        if not q:
            return []
        return self.ytmusic.search_songs(q, limit=max(1, min(int(limit), 30)))

    def configure_ytmusic_auth(
        self,
        proxy: str = "",
        raw_headers_or_cookie: str = "",
        cookies_txt_content: str = "",
        cache_max_mb: int | None = None,
    ) -> dict[str, Any]:
        """Configure YouTube Music authentication headers, cookies.txt, and proxy."""
        ytm_patch: dict[str, Any] = {"proxy": proxy}
        if cache_max_mb is not None:
            ytm_patch["cache_max_mb"] = int(cache_max_mb)
            if hasattr(self.ytmusic, "cache_max_mb"):
                try:
                    setattr(self.ytmusic, "cache_max_mb", int(cache_max_mb))
                except Exception:
                    pass
        self.config_store.update_ytmusic(ytm_patch)

        auth_res = self.ytmusic.configure(
            proxy=proxy,
            raw_headers_or_cookie=raw_headers_or_cookie,
            cookies_txt_content=cookies_txt_content,
        )
        with self._lock:
            self._ytdlp_auth_blocked = False
            self._consecutive_failures = 0
            self._failed_video_ids.clear()
        self._preload_wakeup.set()
        self._autodj_wakeup.set()
        self.emit_event("info", f"YouTube Music 认证配置已更新：{auth_res.get('message', '')}")
        self.broadcast_state()
        return auth_res

    def get_ytmusic_playlists(self) -> list[dict[str, Any]]:
        """Return user playlists (if authenticated) or recommended fallback seeds."""
        return self.ytmusic.get_user_playlists()

    def refresh_fallback_tracks(
        self,
        fallback_source: str | None = None,
        shuffle: bool | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch and optionally shuffle the 24-hour fallback track pool."""
        cfg = self.config_store.get()
        src = str(fallback_source if fallback_source is not None else cfg.get("fallback_source", "search:天涯明月刀 古风 经典")).strip()
        if not src:
            src = "search:天涯明月刀 古风 经典"
        do_shuffle = bool(shuffle if shuffle is not None else cfg.get("fallback_shuffle", True))

        with self._lock:
            self._fallback_loading = True
            self._last_fallback_fetch_ts = time.time()

        tracks: list[dict[str, Any]] = []
        try:
            raw_tracks = self.ytmusic.get_playlist_tracks(src, limit=100) or []
            tracks = [self._normalize_track(t) for t in raw_tracks if isinstance(t, dict) and (t.get("video_id") or t.get("videoId"))]
            if do_shuffle and len(tracks) > 1:
                random.shuffle(tracks)
        except Exception as exc:
            logger.warning("Failed to refresh fallback tracks from %s: %s", src, exc)
        finally:
            with self._lock:
                self._fallback_loading = False
                if tracks:
                    self._fallback_tracks = tracks
                    self._fallback_index = 0

        self._preload_wakeup.set()
        self._autodj_wakeup.set()
        self.broadcast_state()
        return copy.deepcopy(tracks)

    def update_ytmusic_fallback(
        self,
        fallback_source: str,
        shuffle: bool = True,
    ) -> dict[str, Any]:
        """Update fallback playlist source and shuffle flag, then immediately refresh the fallback pool."""
        src = (fallback_source or "search:天涯明月刀 古风 经典").strip() or "search:天涯明月刀 古风 经典"
        self.config_store.update_ytmusic(
            {
                "fallback_source": src,
                "fallback_shuffle": bool(shuffle),
            }
        )
        tracks = self.refresh_fallback_tracks(fallback_source=src, shuffle=bool(shuffle))
        self.emit_event(
            "info",
            f"已切换 24h 兜底曲库：{src}（共加载 {len(tracks)} 首，{'随机洗牌' if shuffle else '顺序播放'}）",
        )
        return {
            "ok": True,
            "status": "ok",
            "fallback": self._get_fallback_snapshot(),
            "tracks_count": len(tracks),
        }

    def list_ocr_windows(self) -> list[dict[str, Any]]:
        """Enumerate visible desktop windows with game/emulator candidates prioritized."""
        return self.ocr_watcher.list_windows()

    def capture_ocr_screenshot(self, monitor: int = 1) -> tuple[bytes, dict[str, Any]]:
        """Capture monitor screenshot in memory as JPEG bytes plus geometry metadata."""
        return self.ocr_watcher.capture_monitor_jpeg(monitor_index=int(monitor))

    def capture_ocr_roi_preview(self) -> bytes:
        """Capture current configured ROI region in memory as PNG bytes."""
        ocr_cfg = self.config_store.get().get("ocr", {})
        if ocr_cfg.get("auto_track_window", False):
            return self.ocr_watcher.capture_roi_png(roi=None)
        roi = ocr_cfg.get("roi")
        return self.ocr_watcher.capture_roi_png(roi=roi)

    def update_ocr_config(self, config_patch: dict[str, Any]) -> dict[str, Any]:
        """Save and hot-apply OCR configuration."""
        updated_cfg = self.config_store.update_ocr(config_patch)
        ocr_section = updated_cfg.get("ocr", {})
        self._apply_ocr_config_to_watcher(ocr_section)
        self.emit_event(
            "info",
            f"帮派聊天框 OCR 配置已保存（状态：{'开启' if ocr_section.get('enabled') else '关闭'}）",
        )
        self.broadcast_state()
        ocr_state = self.get_state().get("ocr", ocr_section)
        return {
            "ok": True,
            "status": "ok",
            "ocr": ocr_state,
        }

    def _resolve_ocr_track(self, title: str, artist: str) -> dict[str, Any]:
        """Resolve an OCR song title/artist via YTMusic search with clean offline fallback."""
        matched_track: dict[str, Any] | None = None
        try:
            matched_track = self.ytmusic.search_best_match(title, artist)
        except Exception as exc:
            logger.warning("OCR search_best_match failed for %s - %s: %s", title, artist, exc)

        if not matched_track:
            try:
                q = f"{title} {artist}".strip()
                results = self.ytmusic.search_songs(q, limit=5)
                if results:
                    matched_track = results[0]
            except Exception:
                pass

        if not matched_track:
            matched_track = {
                "video_id": f"ocr_{uuid.uuid4().hex[:8]}",
                "title": title,
                "artist": artist or "未知歌手",
                "duration_sec": 240,
                "thumbnail_url": "",
            }
        return matched_track

    @staticmethod
    def _normalize_match_text(text: str) -> str:
        """Normalize song titles for fuzzy comparison against currently playing tracks."""
        cleaned = re.sub(r"\s*[\(（\[【].*?[\)）\]】]", "", str(text or ""))
        return re.sub(r"[\s\(\)（）\[\]【】《》\-—–－·.,，。'\"!！?？]+", "", cleaned).lower()

    def _matches_current_track(
        self,
        matched_track: dict[str, Any],
        req_title: str,
        req_artist: str,
    ) -> tuple[bool, str]:
        """Return (True, current_title) if matched_track/req_title is the currently active track."""
        try:
            state = str((self.audio_engine.get_status() or {}).get("state", "stopped"))
        except Exception:
            state = "stopped"
        if state not in ("playing", "paused"):
            return False, ""

        with self._lock:
            cur_item = copy.deepcopy(self._current_item)
        if not isinstance(cur_item, dict):
            return False, ""

        cur_vid = str(cur_item.get("video_id") or "")
        cur_title = str(cur_item.get("title") or "")
        if matched_track.get("video_id") and matched_track.get("video_id") == cur_vid:
            return True, cur_title

        norm_cur = self._normalize_match_text(cur_title)
        if not norm_cur:
            return False, ""

        candidate_titles = [
            self._normalize_match_text(matched_track.get("title", "")),
            self._normalize_match_text(req_title),
            self._normalize_match_text(req_artist),
        ]
        for norm_cand in candidate_titles:
            if not norm_cand or len(norm_cand) < 2:
                continue
            if (
                norm_cand in norm_cur
                or norm_cur in norm_cand
                or difflib.SequenceMatcher(None, norm_cand, norm_cur).ratio() >= 0.78
            ):
                return True, cur_title
        return False, ""

    def _enqueue_from_ocr_command(self, cmd: dict[str, Any]) -> dict[str, Any] | None:
        """Search and enqueue a song from an OCR command dictionary with queue deduplication."""
        title = str(cmd.get("title") or "").strip()
        artist = str(cmd.get("artist") or "").strip()
        requester = str(cmd.get("requester") or "帮派少侠").strip() or "帮派少侠"
        if not title:
            return None

        matched_track = self._resolve_ocr_track(title, artist)
        return self.enqueue_track(
            matched_track,
            requester=requester,
            source="ocr",
            pin_top=False,
            requested_title=title,
            requested_artist=artist,
            dedup_queue=True,
        )

    def _handle_ocr_skip_to(self, cmd: dict[str, Any]) -> dict[str, Any] | None:
        """Handle '切歌：歌名-歌手': skip current if same song, otherwise pre-cache, pin top, and cut in."""
        title = str(cmd.get("title") or "").strip()
        artist = str(cmd.get("artist") or "").strip()
        requester = str(cmd.get("requester") or "帮派少侠").strip() or "帮派少侠"
        if not title:
            return None

        matched_track = self._resolve_ocr_track(title, artist)
        is_current, cur_title = self._matches_current_track(matched_track, title, artist)
        if is_current:
            self.skip_track(reason=f"帮派切歌：跳过当前《{cur_title}》")
            return {"action": "skipped_current", "title": cur_title}

        item = self.enqueue_track(
            matched_track,
            requester=requester,
            source="ocr",
            pin_top=True,
            requested_title=title,
            requested_artist=artist,
            dedup_queue=True,
        )
        if item is not None:
            vid = item["video_id"]
            with self._lock:
                already_ready = vid in self._resolved_cache and Path(self._resolved_cache[vid]).exists()
            if not already_ready and not (self.dummy_sink and self._is_unmocked_ytdlp()):
                try:
                    res = self.ytmusic.resolve_audio_source(vid)
                    file_path = str(res.get("file_path") or "")
                    if file_path and Path(file_path).exists():
                        with self._lock:
                            self._resolved_cache[vid] = file_path
                            for q_item in self._queue:
                                if q_item["track"]["video_id"] == vid:
                                    q_item["status"] = "ready"
                except Exception as exc:
                    logger.warning("Pre-resolve for skip_to failed (%s): %s", vid, exc)
            self.skip_track(reason=f"帮派切歌插播：《{item['title']}》- {item['artist']}")
        return item

    def _on_ocr_command(self, cmd: dict[str, Any]) -> None:
        """Callback invoked by OcrChatWatcher background thread when a chat command is recognized."""
        if not isinstance(cmd, dict) or self._stop_event.is_set() or not self.is_broadcast_active():
            return

        cmd_type = str(cmd.get("type") or "").strip().lower()
        requester = str(cmd.get("requester") or "帮派少侠").strip() or "帮派少侠"

        if cmd_type == "skip":
            allow_skip = bool(self.config_store.get().get("ocr", {}).get("allow_skip_cmd", True))
            if allow_skip:
                self.skip_track(reason=f"帮派聊天 [{requester}] 发送切歌指令")
            return

        if cmd_type == "skip_to":
            allow_skip = bool(self.config_store.get().get("ocr", {}).get("allow_skip_cmd", True))
            if allow_skip:
                worker = threading.Thread(
                    target=self._handle_ocr_skip_to,
                    args=(copy.deepcopy(cmd),),
                    name="OcrSkipToWorker",
                    daemon=True,
                )
                worker.start()
            return

        if cmd_type == "song":
            worker = threading.Thread(
                target=self._enqueue_from_ocr_command,
                args=(copy.deepcopy(cmd),),
                name="OcrSongEnqueueWorker",
                daemon=True,
            )
            worker.start()

    def test_ocr(
        self,
        synthetic_messages: list[str] | None = None,
        enqueue: bool = False,
    ) -> dict[str, Any]:
        """Run a one-shot OCR test on synthetic chat lines or current screen ROI."""
        test_kwargs = _filter_kwargs(
            self.ocr_watcher.test_once,
            {
                "synthetic_messages": synthetic_messages,
                "bypass_dedup": not bool(enqueue),
            },
        )
        result = self.ocr_watcher.test_once(**test_kwargs) or {}
        commands = result.get("commands") or []
        enqueued_items: list[dict[str, Any]] = []

        if enqueue and isinstance(commands, list):
            for cmd in commands:
                if not isinstance(cmd, dict):
                    continue
                if cmd.get("type") == "song":
                    item = self._enqueue_from_ocr_command(cmd)
                    if item is not None:
                        enqueued_items.append(item)
                elif cmd.get("type") == "skip_to":
                    allow_skip = bool(self.config_store.get().get("ocr", {}).get("allow_skip_cmd", True))
                    if allow_skip:
                        res_item = self._handle_ocr_skip_to(cmd)
                        if isinstance(res_item, dict) and res_item.get("item_id"):
                            enqueued_items.append(res_item)
                elif cmd.get("type") == "skip":
                    allow_skip = bool(self.config_store.get().get("ocr", {}).get("allow_skip_cmd", True))
                    if allow_skip:
                        self.skip_track(reason=f"OCR 测试 [{cmd.get('requester', '帮派少侠')}] 切歌")

        result["enqueued"] = enqueued_items
        result["enqueued_count"] = len(enqueued_items)
        result["queue"] = self.get_queue()
        self.broadcast_state()
        return result

    def _on_track_finished(self) -> None:
        """Callback invoked by AudioEngine when a track completes playback."""
        if self._stop_event.is_set():
            return
        with self._lock:
            self._track_just_finished = True
        self._autodj_wakeup.set()

    def _peek_next_fallback_track(self) -> dict[str, Any] | None:
        """Peek at the next track in the fallback pool without advancing the index."""
        with self._lock:
            if not self._fallback_tracks:
                return None
            idx = self._fallback_index % len(self._fallback_tracks)
            return copy.deepcopy(self._fallback_tracks[idx])

    def _pop_next_fallback_item(self) -> dict[str, Any] | None:
        """Advance the fallback index (reshuffling on wrap-around if configured) and return a queue-like item."""
        cfg = self.config_store.get()
        do_shuffle = bool(cfg.get("fallback_shuffle", True))
        with self._lock:
            if not self._fallback_tracks:
                return None
            if self._fallback_index >= len(self._fallback_tracks):
                if do_shuffle and len(self._fallback_tracks) > 1:
                    random.shuffle(self._fallback_tracks)
                self._fallback_index = 0
            track = copy.deepcopy(self._fallback_tracks[self._fallback_index])
            self._fallback_index += 1

        return {
            "item_id": f"fb_{uuid.uuid4().hex[:8]}",
            "track": track,
            "video_id": track["video_id"],
            "title": track["title"],
            "artist": track["artist"],
            "duration_sec": track["duration_sec"],
            "thumbnail_url": track["thumbnail_url"],
            "requester": "帮派电台",
            "source": "fallback",
            "added_at": time.time(),
            "status": "ready",
        }

    def _preload_loop(self) -> None:
        """Background daemon that pre-resolves audio files for queue[0] or next fallback track."""
        while not self._stop_event.is_set():
            try:
                self._preload_step()
            except Exception as exc:
                logger.debug("Preload loop error: %s", exc)
            self._preload_wakeup.wait(timeout=0.5)
            self._preload_wakeup.clear()

    def _is_unmocked_ytdlp(self) -> bool:
        """Check whether ytmusic.resolve_audio_source is the unmocked production method."""
        try:
            from backend.ytmusic_service import YTMusicService

            fn = getattr(self.ytmusic, "resolve_audio_source", None)
            raw_func = getattr(fn, "__func__", None)
            return raw_func is YTMusicService.resolve_audio_source
        except Exception:
            return False

    def _preload_step(self) -> None:
        """Single iteration of the preload engine."""
        if self._stop_event.is_set():
            return

        if not self.is_broadcast_active():
            return

        if self._ytdlp_auth_blocked and not self.ytmusic.get_auth_status().get("has_cookies_txt"):
            return

        unmocked_ytdlp = self._is_unmocked_ytdlp()

        target_item: dict[str, Any] | None = None
        with self._lock:
            if self._queue:
                target_item = self._queue[0]
                vid = target_item["track"]["video_id"]
                if vid in self._resolved_cache and Path(self._resolved_cache[vid]).exists():
                    if target_item.get("status") != "ready":
                        target_item["status"] = "ready"
                        self._autodj_wakeup.set()
                    return

                if self.dummy_sink and unmocked_ytdlp:
                    find_fn = getattr(self.ytmusic, "_find_cached_file", None)
                    cached_file = find_fn(vid) if callable(find_fn) else None
                    if cached_file is not None and Path(cached_file).exists():
                        self._resolved_cache[vid] = str(Path(cached_file).resolve())
                        target_item["status"] = "ready"
                        self._autodj_wakeup.set()
                    return

                if target_item.get("status") != "caching":
                    target_item["status"] = "caching"
                target_copy = copy.deepcopy(target_item)
            else:
                target_copy = None

        if target_copy is not None:
            self.broadcast_state()
            vid = target_copy["track"]["video_id"]
            try:
                res = self.ytmusic.resolve_audio_source(vid)
                file_path = str(res.get("file_path") or "")
                if not file_path or not Path(file_path).exists():
                    raise RuntimeError(f"Audio cache file missing for {vid}")

                with self._lock:
                    self._resolved_cache[vid] = file_path
                    self._consecutive_failures = 0
                    for q_item in self._queue:
                        if q_item["track"]["video_id"] == vid:
                            q_item["status"] = "ready"

                self._autodj_wakeup.set()
                self.broadcast_state()
                return
            except Exception as exc:
                err_str = str(exc)
                if "Sign in to confirm" in err_str or "not a bot" in err_str:
                    with self._lock:
                        self._ytdlp_auth_blocked = True
                        for q_item in self._queue:
                            if q_item.get("item_id") == target_copy.get("item_id"):
                                q_item["status"] = "queued"
                    self.emit_event(
                        "warn",
                        "YouTube 需要账号 Cookie 验证：请在【YouTube Music 与兜底电台】标签页粘贴一次您的 music.youtube.com Cookie / Request Headers，保存后即自动开播",
                    )
                    self.broadcast_state()
                    return

                with self._lock:
                    self._consecutive_failures += 1
                    fail_count = self._consecutive_failures
                    self._failed_video_ids[vid] = time.time()
                    self._queue = [
                        q for q in self._queue if q.get("item_id") != target_copy.get("item_id")
                    ]

                self.emit_event(
                    "warn",
                    f"曲目预加载失败，已自动跳过：《{target_copy['track'].get('title', vid)}》({exc})",
                )
                self.broadcast_state()
                backoff = min(30.0, 1.0 * (2 ** min(fail_count - 1, 4)))
                self._stop_event.wait(timeout=backoff)
                return

        if not self.auto_dj_enabled:
            return

        with self._lock:
            need_refresh = (
                len(self._fallback_tracks) == 0
                and not self._fallback_loading
                and (time.time() - self._last_fallback_fetch_ts) > 15.0
            )

        if need_refresh and (time.time() - self._boot_time) >= 1.0:
            if not (self.dummy_sink and unmocked_ytdlp):
                self.refresh_fallback_tracks()
            return

        if self.dummy_sink and unmocked_ytdlp:
            return

        next_fb = self._peek_next_fallback_track()
        if next_fb is not None:
            vid = next_fb["video_id"]
            with self._lock:
                already_cached = vid in self._resolved_cache and Path(self._resolved_cache[vid]).exists()
                recently_failed = (time.time() - self._failed_video_ids.get(vid, 0.0)) < 120.0

            if not already_cached and not recently_failed:
                try:
                    res = self.ytmusic.resolve_audio_source(vid)
                    file_path = str(res.get("file_path") or "")
                    if file_path and Path(file_path).exists():
                        with self._lock:
                            self._resolved_cache[vid] = file_path
                            self._consecutive_failures = 0
                        self._autodj_wakeup.set()
                except Exception as exc:
                    err_str = str(exc)
                    if "Sign in to confirm" in err_str or "not a bot" in err_str:
                        with self._lock:
                            self._ytdlp_auth_blocked = True
                        self.emit_event(
                            "warn",
                            "YouTube 需要账号 Cookie 验证：请在【YouTube Music 与兜底电台】标签页粘贴一次您的 music.youtube.com Cookie / Request Headers，保存后即自动开播",
                        )
                        self.broadcast_state()
                        return

                    with self._lock:
                        self._consecutive_failures += 1
                        fail_count = self._consecutive_failures
                        self._failed_video_ids[vid] = time.time()
                        self._fallback_index += 1
                    self.emit_event("warn", f"兜底电台曲目缓存失败，自动切换下一首：{next_fb.get('title', vid)} ({exc})")
                    backoff = min(30.0, 1.5 * (2 ** min(fail_count - 1, 4)))
                    self._stop_event.wait(timeout=backoff)

    def _autodj_loop(self) -> None:
        """24-hour Auto-DJ scheduling daemon loop."""
        while not self._stop_event.is_set():
            try:
                self._autodj_step()
            except Exception as exc:
                logger.debug("Auto-DJ loop error: %s", exc)
            self._autodj_wakeup.wait(timeout=0.25)
            self._autodj_wakeup.clear()

    def _autodj_step(self) -> None:
        """Single evaluation step of the scheduled Auto-DJ scheduler."""
        if self._stop_event.is_set():
            return

        now = time.time()
        try:
            eng_status = self.audio_engine.get_status() or {}
        except Exception:
            return

        state = eng_status.get("state", "stopped")
        sched = self._sync_schedule_state()
        if not sched.get("is_active", True):
            if state in ("playing", "paused") and now >= self._test_tone_until:
                try:
                    self.audio_engine.stop()
                except Exception:
                    pass
                with self._lock:
                    self._current_item = None
                    self._force_play_next = False
                    self._track_just_finished = False
            return

        if now < self._test_tone_until:
            return

        if state in ("playing", "paused") and not self._force_play_next:
            return

        if self._manual_stop and not self._force_play_next:
            return

        if not self.auto_dj_enabled and not self._force_play_next:
            return

        next_item: dict[str, Any] | None = None
        file_path: str | None = None

        with self._lock:
            force_now = self._force_play_next or self._track_just_finished
            min_age = 2.0 if (self.dummy_sink and self._is_unmocked_ytdlp()) else 0.05
            if self._queue:
                head = self._queue[0]
                vid = head["track"]["video_id"]
                cached_path = self._resolved_cache.get(vid)
                is_ready = bool(cached_path and Path(cached_path).exists())
                age_sec = now - float(head.get("added_at", now))

                if is_ready and (force_now or age_sec >= min_age):
                    next_item = self._queue.pop(0)
                    file_path = cached_path
                    self._force_play_next = False
                    self._track_just_finished = False
                else:
                    self._preload_wakeup.set()
                    return

        if next_item is None and (self.auto_dj_enabled or self._force_play_next):
            next_fb = self._peek_next_fallback_track()
            if next_fb is not None:
                vid = next_fb["video_id"]
                with self._lock:
                    cached_path = self._resolved_cache.get(vid)
                if cached_path and Path(cached_path).exists():
                    next_item = self._pop_next_fallback_item()
                    file_path = cached_path
                    with self._lock:
                        self._force_play_next = False
                        self._track_just_finished = False
                else:
                    self._preload_wakeup.set()
                    return

        if next_item is not None and file_path:
            self._start_playback(next_item, file_path)

    def _load_played_history(self) -> None:
        """Load 24-hour already-played video_id and normalized title ledger from disk."""
        if self._played_history_path is None or not self._played_history_path.exists():
            return
        try:
            raw = json.loads(self._played_history_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return
            now_ts = time.time()
            ttl = max(0.0, float(self.played_dedup_sec))
            vids = raw.get("video_ids") or {}
            titles = raw.get("titles") or {}
            with self._lock:
                self._played_video_ids = {
                    str(k): float(v)
                    for k, v in vids.items()
                    if isinstance(v, (int, float)) and (now_ts - float(v)) < ttl
                }
                self._played_title_keys = {
                    str(k): float(v)
                    for k, v in titles.items()
                    if isinstance(v, (int, float)) and (now_ts - float(v)) < ttl
                }
        except Exception as exc:
            logger.debug("Failed to load played history ledger: %s", exc)

    def _save_played_history(self) -> None:
        """Atomically persist 24-hour already-played ledger via temp file + os.replace."""
        if self._played_history_path is None:
            return
        try:
            now_ts = time.time()
            ttl = max(0.0, float(self.played_dedup_sec))
            with self._lock:
                payload = {
                    "video_ids": {
                        k: v for k, v in self._played_video_ids.items() if (now_ts - v) < ttl
                    },
                    "titles": {
                        k: v for k, v in self._played_title_keys.items() if (now_ts - v) < ttl
                    },
                }
            self._played_history_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self._played_history_path.with_suffix(".tmp")
            tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp_path, self._played_history_path)
        except Exception as exc:
            logger.debug("Failed to save played history ledger: %s", exc)

    @staticmethod
    def _played_title_key(title: str) -> str:
        """Normalize song title into a compact Traditional/Simplified-invariant dedup key."""
        norm = ""
        try:
            from backend.ytmusic_service import _normalize_text, _strip_edition_parens

            norm = _normalize_text(_strip_edition_parens(title) or title)
        except Exception:
            norm = JukeboxController._normalize_match_text(title)
        return norm

    def _record_played_track(self, track_meta: dict[str, Any]) -> None:
        """Record a track that actually began playing into the 24-hour already-played ledger."""
        vid = str(track_meta.get("video_id") or "").strip()
        title_key = self._played_title_key(str(track_meta.get("title") or ""))
        now_ts = time.time()
        with self._lock:
            if vid:
                self._played_video_ids[vid] = now_ts
            if len(title_key) >= 2:
                self._played_title_keys[title_key] = now_ts
        self._save_played_history()

    def _is_track_already_played(
        self,
        vid: str,
        title: str,
        now_ts: float | None = None,
    ) -> bool:
        """Return True if vid or normalized core title was already played within played_dedup_sec."""
        if self.played_dedup_sec <= 0:
            return False
        cur_ts = time.time() if now_ts is None else float(now_ts)
        title_key = self._played_title_key(title)
        with self._lock:
            if vid and vid in self._played_video_ids:
                if (cur_ts - self._played_video_ids[vid]) < self.played_dedup_sec:
                    return True
            if len(title_key) >= 2 and title_key in self._played_title_keys:
                if (cur_ts - self._played_title_keys[title_key]) < self.played_dedup_sec:
                    return True
        return False

    def _start_playback(self, item: dict[str, Any], file_path: str) -> None:
        """Start playing a resolved audio file on AudioEngine and record history."""
        track_meta = copy.deepcopy(item["track"])
        track_meta["requester"] = item.get("requester", "少侠")
        track_meta["source"] = item.get("source", "web")
        track_meta["item_id"] = item.get("item_id", "")

        try:
            self.audio_engine.play_file(
                file_path=file_path,
                track_meta=track_meta,
                on_finished=self._on_track_finished,
            )
            history_entry = copy.deepcopy(item)
            history_entry["played_at"] = time.time()
            with self._lock:
                self._current_item = item
                self._history.appendleft(history_entry)
                self._consecutive_failures = 0
            self._record_played_track(track_meta)

            self._preload_wakeup.set()
            self.emit_event(
                "info",
                f"正在播放：《{track_meta['title']}》- {track_meta['artist']}（点歌：{track_meta['requester']}）",
            )
            self.broadcast_state()
        except Exception as exc:
            with self._lock:
                self._consecutive_failures += 1
                fail_count = self._consecutive_failures
            self.emit_event("error", f"播放启动失败：《{track_meta.get('title')}》({exc})")
            backoff = min(15.0, 0.5 * (2 ** min(fail_count - 1, 4)))
            self._stop_event.wait(timeout=backoff)

    def shutdown(self) -> None:
        """Gracefully stop all background daemons, OCR watcher, and audio engine streams."""
        self._stop_event.set()
        self._autodj_wakeup.set()
        self._preload_wakeup.set()

        try:
            self.ocr_watcher.stop()
        except Exception:
            pass

        try:
            if hasattr(self.audio_engine, "shutdown"):
                self.audio_engine.shutdown()
            else:
                self.audio_engine.stop()
        except Exception:
            pass

        for thread in (self._autodj_thread, self._preload_thread):
            if thread is not None and thread.is_alive():
                try:
                    thread.join(timeout=2.0)
                except Exception:
                    pass
