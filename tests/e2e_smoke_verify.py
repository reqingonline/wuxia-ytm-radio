"""End-to-end real-stack smoke verification for the 24h Cloud Jukebox.

Starts a real uvicorn server on port 18765 with JUKEBOX_DUMMY_SINK=1 and an isolated
temporary config directory, runs HTTP + WebSocket + real YouTube Music + OCR checks,
and guarantees clean shutdown with zero orphan processes or deleted files.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from typing import Any

import httpx
import pytest
import websockets


PROJECT_ROOT = Path(__file__).resolve().parent.parent
TRASH_ROOT = PROJECT_ROOT / "_待删除"
TEST_HOST = "127.0.0.1"
TEST_PORT = 18765
BASE_URL = f"http://{TEST_HOST}:{TEST_PORT}"
WS_URL = f"ws://{TEST_HOST}:{TEST_PORT}/ws"


def _is_port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    """Return True if a TCP connection succeeds on host:port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


def _wait_for_port_release(host: str, port: int, timeout_sec: float = 10.0) -> bool:
    """Wait until TCP port stops accepting connections."""
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if not _is_port_open(host, port, timeout=0.25):
            return True
        time.sleep(0.2)
    return not _is_port_open(host, port, timeout=0.25)


def _archive_temp_dir(tmp_dir: Path) -> None:
    """Move temporary test workspace to _待删除/ without deleting any files."""
    if not tmp_dir.exists():
        return
    TRASH_ROOT.mkdir(parents=True, exist_ok=True)
    dest = TRASH_ROOT / f"{tmp_dir.name}_{int(time.time() * 1000)}"
    try:
        shutil.move(str(tmp_dir), str(dest))
    except Exception:
        pass


async def _collect_ws_meter_during_test_tone(
    http_client: httpx.Client,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Connect to /ws, verify initial state, trigger test_tone, and collect meter frames."""
    initial_state_msg: dict[str, Any] | None = None
    meter_frames: list[dict[str, Any]] = []

    async with websockets.connect(WS_URL, open_timeout=10, close_timeout=5) as ws:
        first_raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
        first_msg = json.loads(first_raw)
        assert first_msg.get("type") == "state", f"Expected initial state frame, got: {first_msg}"
        initial_state_msg = first_msg

        # Trigger 440 Hz test_tone in silent JUKEBOX_DUMMY_SINK=1 mode
        ctrl_resp = await asyncio.to_thread(
            http_client.post,
            f"{BASE_URL}/api/player/control",
            json={"action": "test_tone", "duration_sec": 3.0},
        )
        assert ctrl_resp.status_code == 200, f"test_tone failed: {ctrl_resp.status_code} {ctrl_resp.text}"

        deadline = time.monotonic() + 3.5
        while time.monotonic() < deadline:
            remaining = max(0.1, deadline - time.monotonic())
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=min(1.0, remaining))
            except asyncio.TimeoutError:
                continue
            msg = json.loads(raw)
            if msg.get("type") == "meter":
                meter_frames.append(msg)
                data = msg.get("data") or {}
                if float(data.get("broadcast_rms_db", -100.0)) > -60.0 and len(meter_frames) >= 2:
                    break

    assert initial_state_msg is not None
    return initial_state_msg, meter_frames


def run_e2e_smoke_suite() -> dict[str, Any]:
    """Execute Steps A-F against a live uvicorn server and return verification summary."""
    assert not _is_port_open(TEST_HOST, TEST_PORT), (
        f"Test port {TEST_PORT} is already in use before starting E2E verification"
    )

    tmp_dir = Path(tempfile.mkdtemp(prefix="jukebox_e2e_"))
    tmp_data_dir = tmp_dir / "data"
    tmp_trash_dir = tmp_dir / "trash"
    tmp_data_dir.mkdir(parents=True, exist_ok=True)
    tmp_trash_dir.mkdir(parents=True, exist_ok=True)
    log_path = tmp_dir / "uvicorn_e2e.log"

    env = os.environ.copy()
    env["JUKEBOX_DUMMY_SINK"] = "1"
    env["JUKEBOX_DATA_DIR"] = str(tmp_data_dir)
    env["JUKEBOX_CONFIG_PATH"] = str(tmp_data_dir / "config.json")
    env["JUKEBOX_TRASH_DIR"] = str(tmp_trash_dir)
    env["PYTHONIOENCODING"] = "utf-8"

    summary: dict[str, Any] = {}
    proc: subprocess.Popen[str] | None = None
    log_handle = log_path.open("w", encoding="utf-8", errors="replace")

    try:
        # ── Step A: Start real uvicorn backend with JUKEBOX_DUMMY_SINK=1 ─────────
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "backend.main:app",
                "--host",
                TEST_HOST,
                "--port",
                str(TEST_PORT),
                "--log-level",
                "info",
            ],
            cwd=str(PROJECT_ROOT),
            env=env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

        with httpx.Client(timeout=30.0) as client:
            health_data: dict[str, Any] | None = None
            boot_deadline = time.monotonic() + 25.0
            while time.monotonic() < boot_deadline:
                if proc.poll() is not None:
                    raise RuntimeError(f"uvicorn exited prematurely with code {proc.returncode}")
                try:
                    resp = client.get(f"{BASE_URL}/api/health")
                    if resp.status_code == 200:
                        health_data = resp.json()
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.25)

            assert health_data is not None, "Timed out waiting for /api/health"
            assert health_data.get("status") == "ok", f"Unexpected health status: {health_data}"
            assert health_data.get("dummy_sink") is True, f"dummy_sink must be True: {health_data}"
            summary["step_a_health"] = health_data

            # ── Step B: Static assets & core GET endpoints ───────────────────────
            r_index = client.get(f"{BASE_URL}/")
            assert r_index.status_code == 200
            for kw in ("天刀", "云音坊", "点歌台", "声卡路由", "OCR"):
                assert kw in r_index.text, f"Missing Chinese UI element '{kw}' in /"

            r_css = client.get(f"{BASE_URL}/styles.css")
            assert r_css.status_code == 200
            assert "color-scheme" in r_css.text and "--bg-page" in r_css.text

            r_js = client.get(f"{BASE_URL}/app.js")
            assert r_js.status_code == 200
            assert "/api/queue/add" in r_js.text and "/ws" in r_js.text

            r_state = client.get(f"{BASE_URL}/api/state")
            assert r_state.status_code == 200
            state_data = r_state.json()
            for key in (
                "player",
                "audio_devices",
                "queue",
                "history",
                "fallback",
                "ytmusic",
                "ocr",
                "auto_dj_enabled",
            ):
                assert key in state_data, f"Missing '{key}' in /api/state"
            assert state_data.get("dummy_sink") is True

            r_devs = client.get(f"{BASE_URL}/api/audio/devices")
            assert r_devs.status_code == 200
            devs_data = r_devs.json()
            for key in (
                "outputs",
                "inputs",
                "vbcable_installed",
                "recommended_virtual_out_id",
                "recommended_monitor_out_id",
                "recommended_mic_in_id",
            ):
                assert key in devs_data, f"Missing '{key}' in /api/audio/devices"

            r_wins = client.get(f"{BASE_URL}/api/ocr/windows")
            assert r_wins.status_code == 200
            wins_data = r_wins.json()
            assert isinstance(wins_data, list)
            if wins_data:
                for key in ("hwnd", "title", "left", "top", "width", "height", "is_game_candidate"):
                    assert key in wins_data[0], f"Missing '{key}' in window entry"

            summary["step_b_static_and_devices"] = {
                "outputs_count": len(devs_data["outputs"]),
                "inputs_count": len(devs_data["inputs"]),
                "vbcable_installed": devs_data["vbcable_installed"],
                "windows_count": len(wins_data),
            }

            # ── Step C: Real network YouTube Music search ────────────────────────
            r_search = client.get(
                f"{BASE_URL}/api/ytmusic/search",
                params={"q": "晴天 周杰伦", "limit": 8},
            )
            assert r_search.status_code == 200, f"Search failed: {r_search.text}"
            search_results = r_search.json()
            assert isinstance(search_results, list) and len(search_results) > 0, (
                "YouTube Music search returned empty results for '晴天 周杰伦'"
            )
            first_hit = search_results[0]
            assert isinstance(first_hit.get("video_id"), str) and len(first_hit["video_id"]) >= 5
            assert isinstance(first_hit.get("title"), str) and len(first_hit["title"]) > 0
            assert isinstance(first_hit.get("duration_sec"), int) and first_hit["duration_sec"] > 0
            summary["step_c_ytm_search"] = {
                "count": len(search_results),
                "first_video_id": first_hit["video_id"],
                "first_title": first_hit["title"],
                "first_duration_sec": first_hit["duration_sec"],
            }

            # ── Step D: Queue add + silent test_tone + real WebSocket meter ──────
            # Disable Auto-DJ temporarily so queued items remain deterministically inspectable
            r_dj_off = client.post(
                f"{BASE_URL}/api/player/control",
                json={"action": "toggle_autodj", "enabled": False},
            )
            assert r_dj_off.status_code == 200
            assert r_dj_off.json().get("auto_dj_enabled") is False

            r_add = client.post(
                f"{BASE_URL}/api/queue/add",
                json={
                    "video_id": first_hit["video_id"],
                    "title": first_hit["title"],
                    "artist": first_hit.get("artist", "周杰伦"),
                    "duration_sec": first_hit["duration_sec"],
                    "thumbnail_url": first_hit.get("thumbnail_url", ""),
                    "requester": "端到端验收少侠",
                    "pin_top": False,
                },
            )
            assert r_add.status_code == 200, f"queue/add failed: {r_add.text}"

            state_after_add = client.get(f"{BASE_URL}/api/state").json()
            queued_ids = [item.get("video_id") or item.get("track", {}).get("video_id") for item in state_after_add["queue"]]
            assert first_hit["video_id"] in queued_ids, f"Added track missing from queue: {state_after_add['queue']}"

            ws_state, meter_frames = asyncio.run(_collect_ws_meter_during_test_tone(client))
            assert ws_state.get("type") == "state"
            max_rms_db = max(
                float((f.get("data") or {}).get("broadcast_rms_db", -100.0))
                for f in meter_frames
            ) if meter_frames else -100.0
            assert len(meter_frames) > 0, "No meter frames received over /ws during test_tone"
            assert max_rms_db > -60.0, f"Expected broadcast_rms_db > -60.0 during test_tone, got {max_rms_db}"

            # Stop test_tone cleanly
            r_stop = client.post(f"{BASE_URL}/api/player/control", json={"action": "stop"})
            assert r_stop.status_code == 200
            summary["step_d_ws_meter"] = {
                "meter_frames_received": len(meter_frames),
                "max_broadcast_rms_db": max_rms_db,
            }

            # ── Step E: OCR synthetic chat auto-enqueue & ROI PNG preview ────────
            r_clear = client.post(f"{BASE_URL}/api/queue/clear", json={})
            assert r_clear.status_code == 200

            r_ocr = client.post(
                f"{BASE_URL}/api/ocr/test",
                json={
                    "synthetic_messages": [
                        "[帮派]明月心：晴天-周杰伦",
                        "[帮派]唐青枫：坐标 123-456",
                    ],
                    "enqueue": True,
                },
            )
            assert r_ocr.status_code == 200, f"ocr/test failed: {r_ocr.text}"
            ocr_data = r_ocr.json()
            cmds = ocr_data.get("commands") or []
            song_cmds = [c for c in cmds if c.get("type") == "song"]
            assert len(song_cmds) == 1, f"Expected exactly 1 song command, got: {cmds}"
            assert "晴天" in song_cmds[0].get("title", "") and "周杰伦" in song_cmds[0].get("artist", ""), (
                f"Unexpected OCR song command: {song_cmds[0]}"
            )
            for c in cmds:
                combined = f"{c.get('title', '')} {c.get('artist', '')} {c.get('raw_text', '')}"
                assert "123-456" not in combined, f"Coordinate noise 123-456 leaked into commands: {c}"

            assert ocr_data.get("enqueued_count") == 1, f"Expected 1 enqueued song, got: {ocr_data}"
            state_after_ocr = client.get(f"{BASE_URL}/api/state").json()
            assert any(
                item.get("source") == "ocr" and "晴天" in (item.get("title") or item.get("track", {}).get("title", ""))
                for item in state_after_ocr["queue"]
            ), f"OCR song missing from queue: {state_after_ocr['queue']}"

            r_roi = client.get(f"{BASE_URL}/api/ocr/roi-preview")
            assert r_roi.status_code == 200
            assert "image/png" in r_roi.headers.get("content-type", "")
            assert r_roi.content.startswith(b"\x89PNG"), "ROI preview response is not a valid PNG"
            summary["step_e_ocr"] = {
                "commands_count": len(cmds),
                "enqueued_title": song_cmds[0].get("title"),
                "enqueued_artist": song_cmds[0].get("artist"),
                "roi_png_bytes": len(r_roi.content),
            }

            # ── Step F: Fallback pool, Auto-DJ toggle, and clean shutdown ────────
            r_fb = client.post(
                f"{BASE_URL}/api/ytmusic/fallback",
                json={"fallback_source": "search:天涯明月刀 古风 经典", "shuffle": True},
            )
            assert r_fb.status_code == 200, f"ytmusic/fallback failed: {r_fb.text}"
            fb_data = r_fb.json()
            assert fb_data.get("ok") is True
            assert int(fb_data.get("tracks_count", 0)) > 0, f"Empty fallback pool: {fb_data}"

            r_dj_on = client.post(
                f"{BASE_URL}/api/player/control",
                json={"action": "toggle_autodj", "enabled": True},
            )
            assert r_dj_on.status_code == 200 and r_dj_on.json().get("auto_dj_enabled") is True

            r_dj_off_final = client.post(
                f"{BASE_URL}/api/player/control",
                json={"action": "toggle_autodj", "enabled": False},
            )
            assert r_dj_off_final.status_code == 200 and r_dj_off_final.json().get("auto_dj_enabled") is False

            client.post(f"{BASE_URL}/api/player/control", json={"action": "stop"})
            summary["step_f_fallback_autodj"] = {
                "fallback_tracks_count": fb_data.get("tracks_count"),
                "autodj_toggled": True,
            }
    finally:
        if proc is not None:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=8.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5.0)
        log_handle.close()

        port_released = _wait_for_port_release(TEST_HOST, TEST_PORT, timeout_sec=8.0)
        log_text = ""
        if log_path.exists():
            log_text = log_path.read_text(encoding="utf-8", errors="replace")

        _archive_temp_dir(tmp_dir)

        if proc is not None:
            assert proc.poll() is not None, "Server process did not terminate cleanly"
        assert port_released, f"Port {TEST_PORT} was not released after shutdown"
        assert "Traceback (most recent call last):" not in log_text, (
            f"Server emitted Python traceback:\n{log_text}"
        )

    summary["shutdown_clean"] = True
    summary["port_released"] = True
    return summary


def test_e2e_full_stack_smoke() -> None:
    """Pytest integration test entrypoint for full-stack real E2E smoke verification."""
    summary = run_e2e_smoke_suite()
    assert summary["shutdown_clean"] is True
    assert summary["port_released"] is True


if __name__ == "__main__":
    result = run_e2e_smoke_suite()
    print(json.dumps(result, ensure_ascii=False, indent=2))
