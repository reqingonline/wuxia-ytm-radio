"""Thread-safe configuration persistence store for data/config.json."""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = PROJECT_ROOT / "data"
DEFAULT_TRASH_DIR = PROJECT_ROOT / "_待删除"
DEFAULT_CONFIG_PATH = DEFAULT_DATA_DIR / "config.json"


DEFAULT_CONFIG: dict[str, Any] = {
    "auto_dj_enabled": True,
    "fallback_source": "search:天涯明月刀 古风 经典",
    "fallback_shuffle": True,
    "cache_max_mb": 1024,
    "proxy": "",
    "audio": {
        "virtual_out_device_id": None,
        "monitor_out_device_id": None,
        "mic_in_device_id": None,
        "broadcast_volume": 0.85,
        "monitor_enabled": True,
        "monitor_volume": 0.70,
        "mic_bridge_enabled": False,
        "mic_volume": 1.0,
        "ducking_enabled": True,
        "duck_threshold_db": -38.0,
        "ducking_db": -12.0,
    },
    "ocr": {
        "enabled": False,
        "roi": {
            "left": 20,
            "top": 680,
            "width": 520,
            "height": 280,
        },
        "auto_track_window": True,
        "interval_sec": 120.0,
        "peak_schedule": {
            "enabled": True,
            "days": [3, 6],
            "start": "19:30",
            "end": "20:20",
            "active_interval_sec": 8.0,
        },
        "trigger_mode": "both",
        "cooldown_sec": 300.0,
        "allow_skip_cmd": True,
    },
    "ytmusic": {
        "proxy": "",
        "fallback_source": "search:天涯明月刀 古风 经典",
        "fallback_shuffle": True,
        "cache_max_mb": 1024,
    },
    "broadcast_schedule": {
        "enabled": True,
        "days": [3, 6],
        "start": "19:30",
        "end": "20:20",
        "clear_queue_on_end": True,
    },
}

_AUDIO_KEYS = set(DEFAULT_CONFIG["audio"].keys())
_OCR_KEYS = set(DEFAULT_CONFIG["ocr"].keys())
_YTMUSIC_SYNC_KEYS = {"proxy", "fallback_source", "fallback_shuffle", "cache_max_mb"}


def _sanitize_broadcast_schedule(sched: dict[str, Any] | None) -> dict[str, Any]:
    """Validate and normalize scheduled broadcast window parameters."""
    raw = sched if isinstance(sched, dict) else {}
    enabled = bool(raw.get("enabled", True))
    if "days" in raw and isinstance(raw["days"], (list, tuple, set)):
        days: list[int] = []
        for d in raw["days"]:
            try:
                di = int(d)
            except (TypeError, ValueError):
                continue
            if 0 <= di <= 6 and di not in days:
                days.append(di)
        days.sort()
    else:
        days = [3, 6]

    def _norm_hm(val: Any, fallback: str) -> str:
        txt = str(val or "").strip()
        m = re.match(r"^(\d{1,2}):(\d{1,2})$", txt)
        if not m:
            return fallback
        hh, mm = int(m.group(1)), int(m.group(2))
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            return fallback
        return f"{hh:02d}:{mm:02d}"

    start = _norm_hm(raw.get("start"), "19:30")
    end = _norm_hm(raw.get("end"), "20:20")
    clear_queue_on_end = bool(raw.get("clear_queue_on_end", True))
    return {
        "enabled": enabled,
        "days": days,
        "start": start,
        "end": end,
        "clear_queue_on_end": clear_queue_on_end,
    }


def _deep_merge(base: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge incoming dict into a copy of base dict."""
    result = copy.deepcopy(base)
    for key, val in incoming.items():
        if (
            key in result
            and isinstance(result[key], dict)
            and isinstance(val, dict)
        ):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = copy.deepcopy(val)
    return result


class ConfigStore:
    """Thread-safe persistent JSON configuration store with zero-deletion atomic writes."""

    def __init__(
        self,
        config_path: Path | str | None = None,
        trash_dir: Path | str | None = None,
    ) -> None:
        env_data_dir = os.environ.get("JUKEBOX_DATA_DIR")
        env_config_path = os.environ.get("JUKEBOX_CONFIG_PATH")
        env_trash_dir = os.environ.get("JUKEBOX_TRASH_DIR")

        if config_path is not None:
            self.config_path = Path(config_path).resolve()
        elif env_config_path:
            self.config_path = Path(env_config_path).resolve()
        elif env_data_dir:
            self.config_path = (Path(env_data_dir).resolve() / "config.json")
        else:
            self.config_path = DEFAULT_CONFIG_PATH.resolve()

        self.data_dir = self.config_path.parent

        if trash_dir is not None:
            self.trash_dir = Path(trash_dir).resolve()
        elif env_trash_dir:
            self.trash_dir = Path(env_trash_dir).resolve()
        else:
            self.trash_dir = DEFAULT_TRASH_DIR.resolve()

        self._lock = threading.RLock()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.trash_dir.mkdir(parents=True, exist_ok=True)
        self._config: dict[str, Any] = copy.deepcopy(DEFAULT_CONFIG)
        self.load()

    @property
    def lock(self) -> threading.RLock:
        """Expose re-entrant lock for external inspection or synchronized blocks."""
        return self._lock

    def _normalize_sync(self, cfg: dict[str, Any]) -> dict[str, Any]:
        """Keep top-level and nested section fields consistent."""
        merged = _deep_merge(DEFAULT_CONFIG, cfg)

        ytm = merged.setdefault("ytmusic", {})
        for key in _YTMUSIC_SYNC_KEYS:
            if key in cfg and cfg[key] is not None:
                merged[key] = cfg[key]
                ytm[key] = cfg[key]
            elif key in cfg.get("ytmusic", {}) and cfg["ytmusic"][key] is not None:
                merged[key] = cfg["ytmusic"][key]
                ytm[key] = cfg["ytmusic"][key]
            else:
                ytm[key] = merged[key]

        audio = merged.setdefault("audio", {})
        for key in _AUDIO_KEYS:
            if key in cfg and key not in ("audio", "ocr", "ytmusic"):
                audio[key] = cfg[key]

        ocr = merged.setdefault("ocr", {})
        for key in _OCR_KEYS:
            if key in cfg and key not in ("audio", "ocr", "ytmusic"):
                if key == "roi" and isinstance(cfg[key], dict):
                    ocr["roi"] = _deep_merge(DEFAULT_CONFIG["ocr"]["roi"], cfg[key])
                else:
                    ocr[key] = cfg[key]

        merged["broadcast_schedule"] = _sanitize_broadcast_schedule(merged.get("broadcast_schedule"))
        return merged

    def load(self) -> dict[str, Any]:
        """Load config from disk, or initialize defaults and persist if missing."""
        with self._lock:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            self.trash_dir.mkdir(parents=True, exist_ok=True)
            if self.config_path.exists():
                try:
                    raw_text = self.config_path.read_text(encoding="utf-8-sig")
                    loaded = json.loads(raw_text) if raw_text.strip() else {}
                    if isinstance(loaded, dict):
                        self._config = self._normalize_sync(loaded)
                    else:
                        self._config = copy.deepcopy(DEFAULT_CONFIG)
                except Exception:
                    self._config = copy.deepcopy(DEFAULT_CONFIG)
            else:
                self._config = copy.deepcopy(DEFAULT_CONFIG)
                self.save()
            return copy.deepcopy(self._config)

    def save(self, config: dict[str, Any] | None = None) -> dict[str, Any]:
        """Atomically persist configuration using temporary file replacement and trash archiving."""
        with self._lock:
            if config is not None and isinstance(config, dict):
                self._config = self._normalize_sync(config)
            else:
                self._config = self._normalize_sync(self._config)

            self.data_dir.mkdir(parents=True, exist_ok=True)
            self.trash_dir.mkdir(parents=True, exist_ok=True)

            serialized = json.dumps(self._config, ensure_ascii=False, indent=2)
            stamp = f"{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
            tmp_path = self.data_dir / f".config_{stamp}.tmp"

            tmp_path.write_text(serialized, encoding="utf-8")

            try:
                if self.config_path.exists():
                    backup_path = self.trash_dir / f"config_backup_{stamp}.json"
                    shutil.move(str(self.config_path), str(backup_path))
                shutil.move(str(tmp_path), str(self.config_path))
            except Exception:
                self.config_path.write_text(serialized, encoding="utf-8")
                if tmp_path.exists():
                    fallback_trash = self.trash_dir / f"config_tmp_{stamp}.json"
                    try:
                        shutil.move(str(tmp_path), str(fallback_trash))
                    except Exception:
                        pass

            return copy.deepcopy(self._config)

    def get(self) -> dict[str, Any]:
        """Return a thread-safe deep copy of the current configuration."""
        with self._lock:
            return copy.deepcopy(self._config)

    def get_config(self) -> dict[str, Any]:
        """Alias of get()."""
        return self.get()

    def to_dict(self) -> dict[str, Any]:
        """Alias of get()."""
        return self.get()

    def update(self, patch: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        """Merge updates into current config and persist atomically."""
        with self._lock:
            combined: dict[str, Any] = {}
            if patch and isinstance(patch, dict):
                combined.update(patch)
            if kwargs:
                combined.update(kwargs)

            merged = _deep_merge(self._config, combined)
            for key in _YTMUSIC_SYNC_KEYS:
                if key in combined and combined[key] is not None:
                    merged[key] = combined[key]
                    merged.setdefault("ytmusic", {})[key] = combined[key]
                elif (
                    "ytmusic" in combined
                    and isinstance(combined["ytmusic"], dict)
                    and key in combined["ytmusic"]
                    and combined["ytmusic"][key] is not None
                ):
                    merged[key] = combined["ytmusic"][key]
                    merged.setdefault("ytmusic", {})[key] = combined["ytmusic"][key]

            for key in _AUDIO_KEYS:
                if key in combined and key not in ("audio", "ocr", "ytmusic"):
                    merged.setdefault("audio", {})[key] = combined[key]

            for key in _OCR_KEYS:
                if key in combined and key not in ("audio", "ocr", "ytmusic"):
                    if key == "roi" and isinstance(combined[key], dict):
                        merged.setdefault("ocr", {})["roi"] = _deep_merge(
                            merged.get("ocr", {}).get("roi", DEFAULT_CONFIG["ocr"]["roi"]),
                            combined[key],
                        )
                    else:
                        merged.setdefault("ocr", {})[key] = combined[key]

            self._config = merged
            return self.save()

    def update_audio(self, patch: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        """Update audio routing and ducking configuration section and persist."""
        with self._lock:
            combined: dict[str, Any] = {}
            if patch and isinstance(patch, dict):
                combined.update(patch)
            if kwargs:
                combined.update(kwargs)

            filtered = {k: v for k, v in combined.items() if v is not None}
            if "mic_device_id" in filtered and "mic_in_device_id" not in filtered:
                filtered["mic_in_device_id"] = filtered.pop("mic_device_id")
            if "ducking_threshold_db" in filtered and "duck_threshold_db" not in filtered:
                filtered["duck_threshold_db"] = filtered.pop("ducking_threshold_db")
            if "duck_db" in filtered and "ducking_db" not in filtered:
                filtered["ducking_db"] = filtered.pop("duck_db")

            if "virtual_out_device_id" in combined and combined["virtual_out_device_id"] is None:
                filtered["virtual_out_device_id"] = None
            if "monitor_out_device_id" in combined and combined["monitor_out_device_id"] is None:
                filtered["monitor_out_device_id"] = None
            if "mic_in_device_id" in combined and combined["mic_in_device_id"] is None:
                filtered["mic_in_device_id"] = None

            return self.update({"audio": filtered})

    def update_ocr(self, patch: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        """Update OCR ROI and recognition parameters section and persist."""
        with self._lock:
            combined: dict[str, Any] = {}
            if patch and isinstance(patch, dict):
                combined.update(patch)
            if kwargs:
                combined.update(kwargs)

            filtered = {k: v for k, v in combined.items() if v is not None}
            roi_patch: dict[str, int] = {}
            for coord_key in ("left", "top", "width", "height"):
                if coord_key in filtered:
                    try:
                        roi_patch[coord_key] = int(filtered.pop(coord_key))
                    except (TypeError, ValueError):
                        filtered.pop(coord_key, None)
            if roi_patch:
                existing_roi = copy.deepcopy(self._config.get("ocr", {}).get("roi", DEFAULT_CONFIG["ocr"]["roi"]))
                if isinstance(filtered.get("roi"), dict):
                    existing_roi.update(filtered["roi"])
                existing_roi.update(roi_patch)
                filtered["roi"] = existing_roi

            return self.update({"ocr": filtered})

    def update_ytmusic(self, patch: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        """Update YouTube Music proxy, cache, and fallback settings and persist."""
        with self._lock:
            combined: dict[str, Any] = {}
            if patch and isinstance(patch, dict):
                combined.update(patch)
            if kwargs:
                combined.update(kwargs)

            filtered = {k: v for k, v in combined.items() if v is not None}
            if "shuffle" in filtered and "fallback_shuffle" not in filtered:
                filtered["fallback_shuffle"] = bool(filtered.pop("shuffle"))

            top_sync: dict[str, Any] = {"ytmusic": filtered}
            for key in _YTMUSIC_SYNC_KEYS:
                if key in filtered:
                    top_sync[key] = filtered[key]
            return self.update(top_sync)

    def update_schedule(self, patch: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        """Update broadcast_schedule and keep ocr.peak_schedule window synchronized."""
        with self._lock:
            combined: dict[str, Any] = {}
            if patch and isinstance(patch, dict):
                combined.update(patch)
            if kwargs:
                combined.update(kwargs)

            cur_sched = copy.deepcopy(
                self._config.get("broadcast_schedule", DEFAULT_CONFIG["broadcast_schedule"])
            )
            cur_sched.update({k: v for k, v in combined.items() if v is not None})
            sanitized = _sanitize_broadcast_schedule(cur_sched)

            peak_patch = {
                "days": list(sanitized["days"]),
                "start": sanitized["start"],
                "end": sanitized["end"],
            }
            return self.update(
                {
                    "broadcast_schedule": sanitized,
                    "ocr": {"peak_schedule": peak_patch},
                }
            )
