"""Real-time dual-bus audio engine with FFmpeg decoding, -2.0 dBFS soft limiter, and auto-ducking."""

from __future__ import annotations

from collections import deque
import math
import os
from pathlib import Path
import re
import subprocess
import threading
import time
from typing import Any, Callable

import imageio_ffmpeg
import numpy as np
import sounddevice as sd


# -2.0 dBFS linear ceiling: 10 ** (-2.0 / 20.0) = 0.7943282347
LIMIT_DBFS = -2.0
LIMIT_LINEAR = 0.7943
SOFT_KNEE_LINEAR = 0.7800
SILENCE_FLOOR_DB = -100.0


def _rms_db(block: np.ndarray | None) -> float:
    """Compute RMS level in dBFS for a floating-point audio block."""
    if block is None or block.size == 0:
        return SILENCE_FLOOR_DB
    rms = float(np.sqrt(np.mean(np.square(block.astype(np.float64)))))
    if rms <= 1e-5:
        return SILENCE_FLOOR_DB
    return float(max(SILENCE_FLOOR_DB, 20.0 * math.log10(rms)))


def _peak_db(block: np.ndarray | None) -> float:
    """Compute peak level in dBFS for a floating-point audio block."""
    if block is None or block.size == 0:
        return SILENCE_FLOOR_DB
    peak = float(np.max(np.abs(block.astype(np.float64))))
    if peak <= 1e-5:
        return SILENCE_FLOOR_DB
    return float(max(SILENCE_FLOOR_DB, 20.0 * math.log10(peak)))


def soft_limit_minus_2dbfs(signal: np.ndarray) -> np.ndarray:
    """Apply C1-continuous soft-knee tanh limiting so absolute peak never exceeds -2.0 dBFS (0.7943)."""
    arr = np.asarray(signal, dtype=np.float64)
    abs_x = np.abs(arr)
    headroom = LIMIT_LINEAR - SOFT_KNEE_LINEAR
    over = np.maximum(0.0, abs_x - SOFT_KNEE_LINEAR)
    compressed = SOFT_KNEE_LINEAR + headroom * np.tanh(over / headroom)
    limited_mag = np.where(abs_x <= SOFT_KNEE_LINEAR, abs_x, compressed)
    signed = np.copysign(limited_mag, arr)
    return np.clip(signed, -LIMIT_LINEAR, LIMIT_LINEAR).astype(np.float32)


class AudioEngine:
    """Dual-output audio engine with VB-CABLE routing, mic auto-ducking, and silent dummy_sink test mode."""

    def __init__(
        self,
        sample_rate: int = 48000,
        channels: int = 2,
        blocksize: int = 1024,
        dummy_sink: bool = False,
    ) -> None:
        env_dummy = os.environ.get("JUKEBOX_DUMMY_SINK", "").strip().lower() in (
            "1",
            "true",
            "yes",
        )
        self.sample_rate: int = int(sample_rate)
        self.channels: int = int(channels)
        self.blocksize: int = int(blocksize)
        self.dummy_sink: bool = bool(dummy_sink or env_dummy)

        # Routing & DSP parameters
        self.virtual_out_device_id: int | None = None
        self.monitor_out_device_id: int | None = None
        self.mic_in_device_id: int | None = None

        self.broadcast_volume: float = 0.78
        self.monitor_enabled: bool = True
        self.monitor_volume: float = 0.70
        self.mic_bridge_enabled: bool = False
        self.mic_volume: float = 1.0

        self.ducking_enabled: bool = True
        self.duck_threshold_db: float = -38.0
        self.ducking_db: float = -12.0

        # Auto-ducking envelope constants: 30 ms attack, 600 ms hold, 500 ms release
        self.attack_sec: float = 0.030
        self.hold_sec: float = 0.600
        self.release_sec: float = 0.500

        # Envelope state
        self._duck_gain: float = 1.0
        self._hold_remaining_sec: float = 0.0
        self._ducking_active: bool = False

        # Real-time meter metrics
        self._broadcast_rms_db: float = SILENCE_FLOOR_DB
        self._peak_db: float = SILENCE_FLOOR_DB
        self._mic_rms_db: float = SILENCE_FLOOR_DB

        # Playback state
        self._lock = threading.RLock()
        self._state: str = "stopped"
        self._track: dict[str, Any] | None = None
        self._current_file_path: str | None = None
        self._position_sec: float = 0.0
        self._duration_sec: float = 0.0
        self._on_finished: Callable[[], None] | None = None

        self._play_token: int = 0
        self._worker_thread: threading.Thread | None = None
        self._ffmpeg_proc: subprocess.Popen[bytes] | None = None
        self._mic_queue: deque[np.ndarray] = deque(maxlen=16)
        self._mic_stream: Any = None

        # Cached device scan result
        self._vbcable_installed: bool = False
        try:
            dev_info = self.list_devices()
            self._vbcable_installed = bool(dev_info.get("vbcable_installed", False))
            if self.virtual_out_device_id is None:
                self.virtual_out_device_id = dev_info.get("recommended_virtual_out_id")
            if self.monitor_out_device_id is None:
                self.monitor_out_device_id = dev_info.get("recommended_monitor_out_id")
            if self.mic_in_device_id is None:
                self.mic_in_device_id = dev_info.get("recommended_mic_in_id")
        except Exception:
            pass

    def list_devices(self) -> dict[str, Any]:
        """Enumerate audio endpoints, prioritizing Windows WASAPI and detecting VB-CABLE endpoints."""
        outputs: list[dict[str, Any]] = []
        inputs: list[dict[str, Any]] = []

        try:
            raw_devices = sd.query_devices()
            raw_hostapis = sd.query_hostapis()
            default_dev = sd.default.device
            default_in_id = int(default_dev[0]) if default_dev and default_dev[0] is not None else -1
            default_out_id = int(default_dev[1]) if default_dev and default_dev[1] is not None else -1
            default_in_name = ""
            if 0 <= default_in_id < len(raw_devices) and isinstance(raw_devices[default_in_id], dict):
                default_in_name = str(raw_devices[default_in_id].get("name", "")).strip().lower()
            default_out_name = ""
            if 0 <= default_out_id < len(raw_devices) and isinstance(raw_devices[default_out_id], dict):
                default_out_name = str(raw_devices[default_out_id].get("name", "")).strip().lower()
        except Exception:
            return {
                "outputs": [],
                "inputs": [],
                "vbcable_installed": False,
                "recommended_virtual_out_id": None,
                "recommended_monitor_out_id": None,
                "recommended_mic_in_id": None,
            }

        hostapi_names: dict[int, str] = {}
        for idx, ha in enumerate(raw_hostapis):
            if isinstance(ha, dict):
                hostapi_names[idx] = str(ha.get("name", f"HostAPI {idx}"))

        def _hostapi_priority(ha_name: str) -> int:
            low = ha_name.lower()
            if "wasapi" in low:
                return 0
            if "mme" in low:
                return 1
            if "directsound" in low:
                return 2
            return 3

        for dev_id, dev in enumerate(raw_devices):
            if not isinstance(dev, dict):
                continue
            name = str(dev.get("name", f"Device {dev_id}")).strip()
            ha_idx = int(dev.get("hostapi", 0))
            ha_name = hostapi_names.get(ha_idx, "Unknown")
            max_out = int(dev.get("max_output_channels", 0))
            max_in = int(dev.get("max_input_channels", 0))
            name_low = name.lower()

            if max_out > 0:
                is_vb_out = ("cable input" in name_low) or ("vb-audio" in name_low and "cable" in name_low)
                outputs.append(
                    {
                        "id": dev_id,
                        "name": name,
                        "hostapi": ha_name,
                        "is_vbcable": bool(is_vb_out),
                        "is_default": bool(dev_id == default_out_id),
                    }
                )

            if max_in > 0:
                is_vb_in = ("cable output" in name_low) or ("vb-audio" in name_low and "cable" in name_low)
                inputs.append(
                    {
                        "id": dev_id,
                        "name": name,
                        "hostapi": ha_name,
                        "is_vbcable": bool(is_vb_in),
                        "is_default": bool(dev_id == default_in_id),
                    }
                )

        outputs.sort(
            key=lambda d: (
                not d["is_vbcable"],
                _hostapi_priority(d["hostapi"]),
                not d["is_default"],
                d["id"],
            )
        )
        inputs.sort(
            key=lambda d: (
                _hostapi_priority(d["hostapi"]),
                not d["is_default"],
                d["is_vbcable"],
                d["id"],
            )
        )

        vbcable_installed = bool(
            any(d["is_vbcable"] for d in outputs) or any(d["is_vbcable"] for d in inputs)
        )
        self._vbcable_installed = vbcable_installed

        # Recommended virtual output (CABLE Input)
        vb_outputs = [d for d in outputs if "cable input" in d["name"].lower() or d["is_vbcable"]]
        vb_outputs.sort(
            key=lambda d: (
                "cable input" not in d["name"].lower(),
                _hostapi_priority(d["hostapi"]),
                d["id"],
            )
        )
        recommended_virtual_out_id = vb_outputs[0]["id"] if vb_outputs else None

        # Recommended physical monitor output (non-VB-CABLE speaker/headphone)
        phys_outputs = [d for d in outputs if not d["is_vbcable"]]
        phys_outputs.sort(
            key=lambda d: (
                _hostapi_priority(d["hostapi"]),
                d["name"].strip().lower() != default_out_name,
                not d["is_default"],
                d["id"],
            )
        )
        recommended_monitor_out_id = phys_outputs[0]["id"] if phys_outputs else None

        # Recommended physical microphone input (non-VB-CABLE, non-Stereo Mix)
        phys_inputs = [
            d
            for d in inputs
            if not d["is_vbcable"]
            and "立体声混音" not in d["name"]
            and "stereo mix" not in d["name"].lower()
        ]
        phys_inputs.sort(
            key=lambda d: (
                _hostapi_priority(d["hostapi"]),
                d["name"].strip().lower() != default_in_name,
                not d["is_default"],
                d["id"],
            )
        )
        recommended_mic_in_id = phys_inputs[0]["id"] if phys_inputs else None

        return {
            "outputs": outputs,
            "inputs": inputs,
            "vbcable_installed": vbcable_installed,
            "recommended_virtual_out_id": recommended_virtual_out_id,
            "recommended_monitor_out_id": recommended_monitor_out_id,
            "recommended_mic_in_id": recommended_mic_in_id,
        }

    def sanitize_routing_ids(self) -> bool:
        """Validate stored audio device IDs against current PortAudio topology and heal any drift."""
        if self.dummy_sink:
            return False

        dev_info = self.list_devices()
        outputs_by_id = {d["id"]: d for d in dev_info.get("outputs", [])}
        inputs_by_id = {d["id"]: d for d in dev_info.get("inputs", [])}
        vbcable_installed = bool(dev_info.get("vbcable_installed", False))

        changed = False
        with self._lock:
            vid = self.virtual_out_device_id
            v_valid = (
                vid is not None
                and vid in outputs_by_id
                and (not vbcable_installed or "cable input" in outputs_by_id[vid]["name"].lower())
            )
            if not v_valid:
                new_vid = dev_info.get("recommended_virtual_out_id")
                if new_vid != vid:
                    self.virtual_out_device_id = new_vid
                    changed = True

            mid = self.monitor_out_device_id
            if mid is not None:
                m_valid = mid in outputs_by_id and not bool(outputs_by_id[mid].get("is_vbcable"))
                if not m_valid:
                    new_mid = dev_info.get("recommended_monitor_out_id")
                    if new_mid != mid:
                        self.monitor_out_device_id = new_mid
                        changed = True

            iid = self.mic_in_device_id
            if iid is not None:
                i_valid = iid in inputs_by_id and not bool(inputs_by_id[iid].get("is_vbcable"))
                if not i_valid:
                    new_iid = dev_info.get("recommended_mic_in_id")
                    if new_iid != iid:
                        self.mic_in_device_id = new_iid
                        changed = True

        return changed

    def _advance_ducking_envelope(self, mic_rms_db: float, dt_sec: float) -> float:
        """Advance the 30ms attack / 600ms hold / 500ms release auto-ducking state machine."""
        if not self.ducking_enabled:
            self._duck_gain = 1.0
            self._hold_remaining_sec = 0.0
            self._ducking_active = False
            return 1.0

        target_gain = float(10.0 ** (-abs(float(self.ducking_db)) / 20.0))
        dt = max(0.0, float(dt_sec))

        if mic_rms_db > float(self.duck_threshold_db):
            self._ducking_active = True
            self._hold_remaining_sec = float(self.hold_sec)
            attack_span = max(1e-4, float(self.attack_sec))
            down_step = ((1.0 - target_gain) / attack_span) * dt
            self._duck_gain = max(target_gain, self._duck_gain - down_step)
        else:
            if self._hold_remaining_sec > 0.0:
                if dt <= self._hold_remaining_sec:
                    self._hold_remaining_sec -= dt
                    release_dt = 0.0
                else:
                    release_dt = dt - self._hold_remaining_sec
                    self._hold_remaining_sec = 0.0
            else:
                release_dt = dt

            if release_dt > 0.0 and self._duck_gain < 1.0:
                release_span = max(1e-4, float(self.release_sec))
                up_step = ((1.0 - target_gain) / release_span) * release_dt
                self._duck_gain = min(1.0, self._duck_gain + up_step)

            if self._duck_gain >= 0.999 and self._hold_remaining_sec <= 0.0:
                self._duck_gain = 1.0
                self._ducking_active = False

        return float(self._duck_gain)

    def process_mix_block(
        self,
        music_block: np.ndarray,
        mic_block: np.ndarray | None = None,
    ) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
        """Process one audio block for dual-bus routing, auto-ducking, and -2.0 dBFS soft limiting.

        Returns:
            (broadcast_block, monitor_block, meta_dict)
            - broadcast_block: main virtual mic output (music * duck_gain * broadcast_volume
              + optional mic * mic_volume when mic_bridge_enabled=True), soft-limited to <= -2.0 dBFS.
            - monitor_block: local headphone monitor output (music * monitor_volume only;
              never mixes in physical microphone signal).
            - meta_dict: real-time DSP telemetry (ducking_active, duck_gain, duck_gain_db,
              broadcast_rms_db, peak_db, mic_rms_db).
        """
        music_arr = np.asarray(music_block, dtype=np.float32)
        num_frames = int(music_arr.shape[0]) if music_arr.ndim >= 1 else 0
        dt_sec = num_frames / float(self.sample_rate) if self.sample_rate > 0 else 0.02

        mic_arr: np.ndarray | None = None
        if mic_block is not None:
            mic_arr = np.asarray(mic_block, dtype=np.float32)
            if mic_arr.shape != music_arr.shape:
                if mic_arr.ndim == 1 and music_arr.ndim == 2:
                    mic_arr = np.column_stack([mic_arr] * music_arr.shape[1]).astype(np.float32)
                elif mic_arr.ndim == 2 and music_arr.ndim == 1:
                    mic_arr = np.mean(mic_arr, axis=1).astype(np.float32)

        # Process auto-ducking in sub-chunks if a large multi-block buffer (>25ms) is fed at once
        sub_step_frames = max(1, min(self.blocksize, int(self.sample_rate * 0.010)))
        if num_frames > sub_step_frames * 2:
            offset = 0
            duck_gain = self._duck_gain
            mic_rms = SILENCE_FLOOR_DB
            while offset < num_frames:
                end = min(num_frames, offset + sub_step_frames)
                sub_dt = (end - offset) / float(self.sample_rate)
                sub_mic = mic_arr[offset:end] if mic_arr is not None else None
                mic_rms = _rms_db(sub_mic)
                duck_gain = self._advance_ducking_envelope(mic_rms, sub_dt)
                offset = end
        else:
            mic_rms = _rms_db(mic_arr)
            duck_gain = self._advance_ducking_envelope(mic_rms, dt_sec)

        music_ducked = music_arr * float(duck_gain * self.broadcast_volume)

        if mic_arr is not None and self.mic_bridge_enabled and self.mic_volume > 0.0:
            raw_broadcast = music_ducked + mic_arr * float(self.mic_volume)
        else:
            raw_broadcast = music_ducked

        broadcast_block = soft_limit_minus_2dbfs(raw_broadcast)

        # Monitor bus strictly outputs pure music without microphone feed
        if self.monitor_enabled:
            monitor_block = (music_arr * float(self.monitor_volume)).astype(np.float32)
        else:
            monitor_block = np.zeros_like(music_arr, dtype=np.float32)

        b_rms_db = _rms_db(broadcast_block)
        b_peak_db = _peak_db(broadcast_block)
        duck_gain_db = float(20.0 * math.log10(max(duck_gain, 1e-6)))

        with self._lock:
            self._broadcast_rms_db = b_rms_db
            self._peak_db = b_peak_db
            self._mic_rms_db = mic_rms

        meta = {
            "ducking_active": bool(self._ducking_active),
            "duck_gain": float(duck_gain),
            "duck_gain_db": duck_gain_db,
            "broadcast_rms_db": float(b_rms_db),
            "peak_db": float(b_peak_db),
            "mic_rms_db": float(mic_rms),
            "music_ducked_rms_db": _rms_db(music_ducked),
        }
        return broadcast_block, monitor_block, meta

    def _probe_duration_sec(self, file_path: str, fallback_duration: float = 0.0) -> float:
        """Probe audio file duration using FFmpeg header inspection."""
        if fallback_duration and fallback_duration > 0:
            return float(fallback_duration)
        try:
            ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            proc = subprocess.run(
                [ffmpeg_exe, "-nostdin", "-hide_banner", "-i", str(file_path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=5.0,
                check=False,
                creationflags=creationflags,
            )
            stderr_text = proc.stderr.decode("utf-8", errors="ignore")
            m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", stderr_text)
            if m:
                hours = int(m.group(1))
                mins = int(m.group(2))
                secs = float(m.group(3))
                return float(hours * 3600 + mins * 60 + secs)
        except Exception:
            pass
        return float(max(0.0, fallback_duration))

    def _terminate_ffmpeg_locked(self) -> None:
        """Safely terminate active FFmpeg decoder subprocess if running."""
        proc = self._ffmpeg_proc
        self._ffmpeg_proc = None
        if proc is not None:
            try:
                if proc.stdout:
                    proc.stdout.close()
            except Exception:
                pass
            try:
                proc.terminate()
                proc.wait(timeout=1.0)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    def _open_ffmpeg_decoder(self, file_path: str, start_sec: float = 0.0) -> subprocess.Popen[bytes]:
        """Launch FFmpeg subprocess decoding input audio into 48000 Hz stereo f32le raw PCM."""
        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        cmd = [
            ffmpeg_exe,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
        ]
        if start_sec > 0.0:
            cmd.extend(["-ss", f"{start_sec:.3f}"])
        cmd.extend(
            [
                "-i",
                str(file_path),
                "-vn",
                "-f",
                "f32le",
                "-acodec",
                "pcm_f32le",
                "-ar",
                str(self.sample_rate),
                "-ac",
                str(self.channels),
                "pipe:1",
            ]
        )
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        return subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=self.blocksize * self.channels * 4 * 4,
            creationflags=creationflags,
        )

    @staticmethod
    def _is_wasapi_device(device_id: int) -> bool:
        """Return True if device_id belongs to a Windows WASAPI host API."""
        try:
            dev = sd.query_devices(device_id)
            ha_idx = int(dev.get("hostapi", -1)) if isinstance(dev, dict) else -1
            ha = sd.query_hostapis(ha_idx)
            ha_name = str(ha.get("name", "")).lower() if isinstance(ha, dict) else ""
            return "wasapi" in ha_name
        except Exception:
            return False

    def _open_sd_stream(self, stream_cls: Any, device_id: int, **kwargs: Any) -> Any:
        """Open and start a sounddevice stream, retrying WASAPI endpoints with auto_convert=True."""
        stream = None
        try:
            stream = stream_cls(device=device_id, **kwargs)
            stream.start()
            return stream
        except Exception:
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass
                stream = None
            if self._is_wasapi_device(device_id) and hasattr(sd, "WasapiSettings"):
                stream = stream_cls(
                    device=device_id,
                    extra_settings=sd.WasapiSettings(auto_convert=True),
                    **kwargs,
                )
                stream.start()
                return stream
            raise

    def _open_hardware_streams(self) -> tuple[Any, Any, Any]:
        """Open sounddevice output/input streams when dummy_sink is False."""
        if self.dummy_sink:
            return None, None, None

        virtual_stream = None
        monitor_stream = None
        mic_stream = None

        v_id = self.virtual_out_device_id
        m_id = self.monitor_out_device_id if self.monitor_enabled else None

        # Avoid double-opening the same physical/virtual output device
        if v_id is not None and m_id is not None and v_id == m_id:
            m_id = None

        try:
            if v_id is not None:
                virtual_stream = self._open_sd_stream(
                    sd.OutputStream,
                    v_id,
                    samplerate=self.sample_rate,
                    blocksize=self.blocksize,
                    channels=self.channels,
                    dtype="float32",
                )
        except Exception:
            virtual_stream = None

        try:
            if m_id is not None:
                monitor_stream = self._open_sd_stream(
                    sd.OutputStream,
                    m_id,
                    samplerate=self.sample_rate,
                    blocksize=self.blocksize,
                    channels=self.channels,
                    dtype="float32",
                )
        except Exception:
            monitor_stream = None

        # Open physical microphone input if mic bridge or auto-ducking is enabled with a valid mic device
        if (self.mic_bridge_enabled or self.ducking_enabled) and self.mic_in_device_id is not None:
            try:
                self._mic_queue.clear()

                def _mic_callback(indata: np.ndarray, _frames: int, _time_info: Any, _status: Any) -> None:
                    self._mic_queue.append(indata.copy())

                mic_stream = self._open_sd_stream(
                    sd.InputStream,
                    self.mic_in_device_id,
                    samplerate=self.sample_rate,
                    blocksize=self.blocksize,
                    channels=self.channels,
                    dtype="float32",
                    callback=_mic_callback,
                )
            except Exception:
                mic_stream = None

        return virtual_stream, monitor_stream, mic_stream

    @staticmethod
    def _close_hardware_streams(virtual_stream: Any, monitor_stream: Any, mic_stream: Any) -> None:
        """Safely stop and close sounddevice streams."""
        for stream in (virtual_stream, monitor_stream, mic_stream):
            if stream is not None:
                try:
                    stream.stop()
                except Exception:
                    pass
                try:
                    stream.close()
                except Exception:
                    pass

    def play_file(
        self,
        file_path: str,
        track_meta: dict[str, Any],
        on_finished: Callable[[], None] | None = None,
    ) -> None:
        """Decode and play an audio file at 48 kHz stereo f32le through the dual-bus DSP pipeline."""
        resolved_path = str(Path(file_path).resolve())
        if not Path(resolved_path).exists():
            raise FileNotFoundError(f"Audio file not found: {resolved_path}")

        meta_copy = dict(track_meta) if isinstance(track_meta, dict) else {"title": Path(resolved_path).stem}
        dur = self._probe_duration_sec(resolved_path, float(meta_copy.get("duration_sec") or 0.0))
        if dur > 0:
            meta_copy["duration_sec"] = int(round(dur))

        first_block_ready = threading.Event()

        with self._lock:
            self._play_token += 1
            token = self._play_token
            self._terminate_ffmpeg_locked()

            self._current_file_path = resolved_path
            self._track = meta_copy
            self._position_sec = 0.0
            self._duration_sec = dur
            self._on_finished = on_finished
            self._state = "playing"

            proc = self._open_ffmpeg_decoder(resolved_path, start_sec=0.0)
            self._ffmpeg_proc = proc

            worker = threading.Thread(
                target=self._file_playback_loop,
                args=(token, first_block_ready),
                name=f"AudioEngine-File-{token}",
                daemon=True,
            )
            self._worker_thread = worker
            worker.start()

        first_block_ready.wait(timeout=0.25)

    def _file_playback_loop(self, token: int, first_block_ready: threading.Event) -> None:
        """Background thread reading decoded f32le PCM blocks from FFmpeg and pushing to sinks."""
        bytes_per_frame = self.channels * 4
        chunk_bytes = self.blocksize * bytes_per_frame
        block_duration = self.blocksize / float(self.sample_rate)

        virtual_stream, monitor_stream, mic_stream = self._open_hardware_streams()
        natural_eof = False
        signaled_first = False

        try:
            next_tick = time.perf_counter()
            while True:
                with self._lock:
                    if token != self._play_token or self._state == "stopped":
                        break
                    is_paused = self._state == "paused"
                    proc = self._ffmpeg_proc

                if is_paused:
                    if not signaled_first:
                        signaled_first = True
                        first_block_ready.set()
                    with self._lock:
                        self._broadcast_rms_db = SILENCE_FLOOR_DB
                        self._peak_db = SILENCE_FLOOR_DB
                    time.sleep(0.02)
                    next_tick = time.perf_counter()
                    continue

                if proc is None or proc.stdout is None:
                    break

                raw = proc.stdout.read(chunk_bytes)
                if not raw:
                    natural_eof = True
                    break

                usable_len = (len(raw) // bytes_per_frame) * bytes_per_frame
                if usable_len == 0:
                    natural_eof = True
                    break

                pcm = np.frombuffer(raw[:usable_len], dtype=np.float32).reshape(-1, self.channels)
                frames = pcm.shape[0]

                mic_block: np.ndarray | None = None
                if not self.dummy_sink and self._mic_queue:
                    try:
                        cand_mic = self._mic_queue.popleft()
                        if cand_mic.shape[0] == frames:
                            mic_block = cand_mic
                    except IndexError:
                        mic_block = None

                broadcast_block, monitor_block, _meta = self.process_mix_block(pcm, mic_block)

                with self._lock:
                    if token == self._play_token:
                        self._position_sec += frames / float(self.sample_rate)
                        if self._duration_sec > 0 and self._position_sec > self._duration_sec:
                            self._duration_sec = self._position_sec

                if not signaled_first:
                    signaled_first = True
                    first_block_ready.set()

                if self.dummy_sink or (virtual_stream is None and monitor_stream is None):
                    next_tick += frames / float(self.sample_rate)
                    sleep_dur = next_tick - time.perf_counter()
                    if sleep_dur > 0:
                        time.sleep(min(sleep_dur, block_duration))
                    else:
                        next_tick = time.perf_counter()
                else:
                    if virtual_stream is not None:
                        try:
                            virtual_stream.write(broadcast_block)
                        except Exception:
                            pass
                    if monitor_stream is not None:
                        try:
                            monitor_stream.write(monitor_block)
                        except Exception:
                            pass
        finally:
            if not signaled_first:
                first_block_ready.set()
            self._close_hardware_streams(virtual_stream, monitor_stream, mic_stream)

            callback_to_run: Callable[[], None] | None = None
            with self._lock:
                if token == self._play_token:
                    self._terminate_ffmpeg_locked()
                    if natural_eof and self._state != "stopped":
                        self._state = "stopped"
                        self._broadcast_rms_db = SILENCE_FLOOR_DB
                        self._peak_db = SILENCE_FLOOR_DB
                        callback_to_run = self._on_finished

            if callback_to_run is not None:
                threading.Thread(
                    target=self._safe_invoke_finished,
                    args=(callback_to_run,),
                    name=f"AudioEngine-OnFinished-{token}",
                    daemon=True,
                ).start()

    @staticmethod
    def _safe_invoke_finished(cb: Callable[[], None]) -> None:
        try:
            cb()
        except Exception:
            pass

    def play_test_tone(self, duration_sec: float = 3.0) -> None:
        """Play a smooth 440 Hz sine test tone (default 3.0s) without touching hardware when dummy_sink=True."""
        dur = max(0.1, float(duration_sec))
        first_block_ready = threading.Event()

        with self._lock:
            self._play_token += 1
            token = self._play_token
            self._terminate_ffmpeg_locked()

            self._current_file_path = None
            self._track = {
                "video_id": "test_tone_440hz",
                "title": "440 Hz 音频链路测试音",
                "artist": "云音坊信号发生器",
                "duration_sec": int(round(dur)),
                "thumbnail_url": "",
            }
            self._position_sec = 0.0
            self._duration_sec = dur
            self._on_finished = None
            self._state = "playing"

            worker = threading.Thread(
                target=self._test_tone_loop,
                args=(token, dur, first_block_ready),
                name=f"AudioEngine-TestTone-{token}",
                daemon=True,
            )
            self._worker_thread = worker
            worker.start()

        # Wait for the first block to be processed so immediate get_status() reflects valid broadcast_rms_db
        first_block_ready.wait(timeout=0.25)

    def _test_tone_loop(self, token: int, duration_sec: float, first_block_ready: threading.Event) -> None:
        """Generate 440 Hz stereo sine wave blocks and pass them through process_mix_block."""
        total_frames = int(round(duration_sec * self.sample_rate))
        generated_frames = 0
        freq = 440.0
        amp = 0.50  # Soft -6 dBFS sine wave
        fade_frames = max(1, int(0.020 * self.sample_rate))

        virtual_stream, monitor_stream, mic_stream = self._open_hardware_streams()
        signaled_first = False

        try:
            next_tick = time.perf_counter()
            while generated_frames < total_frames:
                with self._lock:
                    if token != self._play_token or self._state == "stopped":
                        break
                    is_paused = self._state == "paused"

                if is_paused:
                    if not signaled_first:
                        signaled_first = True
                        first_block_ready.set()
                    time.sleep(0.02)
                    next_tick = time.perf_counter()
                    continue

                frames = min(self.blocksize, total_frames - generated_frames)
                idx = np.arange(generated_frames, generated_frames + frames, dtype=np.float64)
                sine = amp * np.sin(2.0 * math.pi * freq * idx / float(self.sample_rate))

                # Apply smooth 20 ms fade-in and fade-out envelope
                env = np.ones(frames, dtype=np.float64)
                for i in range(frames):
                    g_idx = generated_frames + i
                    if g_idx < fade_frames:
                        env[i] = g_idx / float(fade_frames)
                    elif total_frames - g_idx < fade_frames:
                        env[i] = max(0.0, (total_frames - g_idx) / float(fade_frames))
                mono = (sine * env).astype(np.float32)
                stereo_block = np.column_stack([mono] * self.channels).astype(np.float32)

                broadcast_block, monitor_block, _meta = self.process_mix_block(stereo_block, None)
                generated_frames += frames

                with self._lock:
                    if token == self._play_token:
                        self._position_sec = generated_frames / float(self.sample_rate)

                if not signaled_first:
                    signaled_first = True
                    first_block_ready.set()

                if self.dummy_sink or (virtual_stream is None and monitor_stream is None):
                    next_tick += frames / float(self.sample_rate)
                    sleep_dur = next_tick - time.perf_counter()
                    if sleep_dur > 0:
                        time.sleep(min(sleep_dur, frames / float(self.sample_rate)))
                    else:
                        next_tick = time.perf_counter()
                else:
                    if virtual_stream is not None:
                        try:
                            virtual_stream.write(broadcast_block)
                        except Exception:
                            pass
                    if monitor_stream is not None:
                        try:
                            monitor_stream.write(monitor_block)
                        except Exception:
                            pass
        finally:
            if not signaled_first:
                first_block_ready.set()
            self._close_hardware_streams(virtual_stream, monitor_stream, mic_stream)
            with self._lock:
                if token == self._play_token and generated_frames >= total_frames:
                    self._state = "stopped"
                    self._broadcast_rms_db = SILENCE_FLOOR_DB
                    self._peak_db = SILENCE_FLOOR_DB

    def pause(self) -> None:
        """Pause active playback."""
        with self._lock:
            if self._state == "playing":
                self._state = "paused"
                self._broadcast_rms_db = SILENCE_FLOOR_DB
                self._peak_db = SILENCE_FLOOR_DB

    def resume(self) -> None:
        """Resume paused playback."""
        with self._lock:
            if self._state == "paused":
                self._state = "playing"

    def stop(self) -> None:
        """Stop playback immediately and terminate any active FFmpeg decoder subprocess."""
        with self._lock:
            self._play_token += 1
            self._state = "stopped"
            self._position_sec = 0.0
            self._broadcast_rms_db = SILENCE_FLOOR_DB
            self._peak_db = SILENCE_FLOOR_DB
            self._mic_rms_db = SILENCE_FLOOR_DB
            self._ducking_active = False
            self._duck_gain = 1.0
            self._hold_remaining_sec = 0.0
            self._terminate_ffmpeg_locked()

    def seek(self, position_sec: float) -> None:
        """Seek to position_sec in the current audio file by restarting FFmpeg with -ss."""
        target = max(0.0, float(position_sec))
        with self._lock:
            if not self._current_file_path or not Path(self._current_file_path).exists():
                self._position_sec = target
                return
            if self._duration_sec > 0:
                target = min(target, max(0.0, self._duration_sec - 0.2))

            was_paused = self._state == "paused"
            self._play_token += 1
            token = self._play_token
            self._terminate_ffmpeg_locked()

            self._position_sec = target
            self._state = "paused" if was_paused else "playing"
            self._ffmpeg_proc = self._open_ffmpeg_decoder(self._current_file_path, start_sec=target)

            first_block_ready = threading.Event()
            worker = threading.Thread(
                target=self._file_playback_loop,
                args=(token, first_block_ready),
                name=f"AudioEngine-Seek-{token}",
                daemon=True,
            )
            self._worker_thread = worker
            worker.start()

    def update_config(self, **kwargs: Any) -> dict[str, Any]:
        """Hot-update routing devices, volume gains, and auto-ducking thresholds."""
        with self._lock:
            if "virtual_out_device_id" in kwargs:
                val = kwargs["virtual_out_device_id"]
                self.virtual_out_device_id = int(val) if val is not None and str(val) != "" else None
            if "monitor_out_device_id" in kwargs:
                val = kwargs["monitor_out_device_id"]
                self.monitor_out_device_id = int(val) if val is not None and str(val) != "" else None
            if "mic_in_device_id" in kwargs or "mic_device_id" in kwargs:
                val = kwargs.get("mic_in_device_id", kwargs.get("mic_device_id"))
                self.mic_in_device_id = int(val) if val is not None and str(val) != "" else None

            if kwargs.get("broadcast_volume") is not None:
                self.broadcast_volume = float(np.clip(float(kwargs["broadcast_volume"]), 0.0, 2.0))
            if kwargs.get("monitor_enabled") is not None:
                self.monitor_enabled = bool(kwargs["monitor_enabled"])
            if kwargs.get("monitor_volume") is not None:
                self.monitor_volume = float(np.clip(float(kwargs["monitor_volume"]), 0.0, 2.0))
            if kwargs.get("mic_bridge_enabled") is not None:
                self.mic_bridge_enabled = bool(kwargs["mic_bridge_enabled"])
            if kwargs.get("mic_volume") is not None:
                self.mic_volume = float(np.clip(float(kwargs["mic_volume"]), 0.0, 3.0))
            if kwargs.get("ducking_enabled") is not None:
                self.ducking_enabled = bool(kwargs["ducking_enabled"])
                if not self.ducking_enabled:
                    self._duck_gain = 1.0
                    self._ducking_active = False
                    self._hold_remaining_sec = 0.0

            thresh = kwargs.get("duck_threshold_db", kwargs.get("ducking_threshold_db"))
            if thresh is not None:
                self.duck_threshold_db = float(np.clip(float(thresh), -80.0, 0.0))

            depth = kwargs.get("ducking_db", kwargs.get("duck_db"))
            if depth is not None:
                val_db = float(depth)
                self.ducking_db = -abs(val_db) if val_db != 0 else 0.0

            return self.get_status()["routing"]

    def get_status(self) -> dict[str, Any]:
        """Return fixed status snapshot of playback state, meters, routing, and VB-CABLE status."""
        with self._lock:
            routing = {
                "virtual_out_device_id": self.virtual_out_device_id,
                "monitor_out_device_id": self.monitor_out_device_id,
                "mic_in_device_id": self.mic_in_device_id,
                "broadcast_volume": float(self.broadcast_volume),
                "monitor_enabled": bool(self.monitor_enabled),
                "monitor_volume": float(self.monitor_volume),
                "mic_bridge_enabled": bool(self.mic_bridge_enabled),
                "mic_volume": float(self.mic_volume),
                "ducking_enabled": bool(self.ducking_enabled),
                "duck_threshold_db": float(self.duck_threshold_db),
                "ducking_db": float(self.ducking_db),
                "dummy_sink": bool(self.dummy_sink),
            }
            return {
                "state": str(self._state),
                "track": dict(self._track) if isinstance(self._track, dict) else None,
                "position_sec": round(float(self._position_sec), 2),
                "duration_sec": round(float(self._duration_sec), 2),
                "broadcast_rms_db": round(float(self._broadcast_rms_db), 2),
                "peak_db": round(float(self._peak_db), 2),
                "mic_rms_db": round(float(self._mic_rms_db), 2),
                "ducking_active": bool(self._ducking_active),
                "routing": routing,
                "vbcable_installed": bool(self._vbcable_installed),
            }

    def shutdown(self) -> None:
        """Stop playback and release all subprocess and thread resources."""
        self.stop()
        worker = self._worker_thread
        if worker is not None and worker.is_alive():
            worker.join(timeout=0.5)
