"""FastAPI application entrypoint, REST API routes, WebSocket meter/state streaming, and static frontend hosting."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import logging
from pathlib import Path
import time
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

from backend.jukebox_controller import JukeboxController
from backend.models import (
    AudioConfigRequest,
    BroadcastScheduleRequest,
    OcrConfigRequest,
    OcrTestRequest,
    PlayerControlRequest,
    QueueAddRequest,
    QueueItemActionRequest,
    ScheduleOverrideRequest,
    YTMusicAuthRequest,
    YTMusicFallbackRequest,
)

logger = logging.getLogger("jukebox.main")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIR = PROJECT_ROOT / "frontend"

_controller: JukeboxController | None = None


def get_controller() -> JukeboxController:
    """Return the active JukeboxController singleton, initializing it if needed."""
    global _controller
    if _controller is None or _controller._stop_event.is_set():
        _controller = JukeboxController()
    return _controller


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage startup and graceful shutdown of the 24-hour JukeboxController."""
    global _controller
    ctrl = get_controller()
    app.state.controller = ctrl
    try:
        yield
    finally:
        if _controller is not None:
            try:
                _controller.shutdown()
            except Exception as exc:
                logger.warning("Error during controller shutdown: %s", exc)
            _controller = None


app = FastAPI(
    title="天刀·帮派定时点歌台控制台（周四/周日 19:30–20:20）",
    description="天涯明月刀帮派驻地定时点歌台与防串麦虚拟声卡推流引擎",
    version="1.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Orig-Width", "X-Orig-Height", "X-Mon-Left", "X-Mon-Top"],
)


@app.get("/api/health")
async def api_health() -> dict[str, Any]:
    """Health check endpoint reporting service readiness and dummy_sink mode."""
    ctrl = get_controller()
    return {
        "status": "ok",
        "dummy_sink": bool(ctrl.dummy_sink),
    }


@app.get("/api/state")
async def api_get_state() -> dict[str, Any]:
    """Return a full snapshot of player, audio_devices, queue, history, fallback, ytmusic, ocr, and auto_dj_enabled."""
    ctrl = get_controller()
    return await asyncio.to_thread(ctrl.get_state)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    """Realtime WebSocket stream: pushes state on connect, meter every 250ms, and immediate state/event updates."""
    await websocket.accept()
    ctrl = get_controller()
    loop = asyncio.get_running_loop()
    msg_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=128)

    def _enqueue_msg(payload: dict[str, Any]) -> None:
        if not msg_queue.full():
            msg_queue.put_nowait(payload)

    def _on_controller_msg(payload: dict[str, Any]) -> None:
        try:
            loop.call_soon_threadsafe(_enqueue_msg, payload)
        except Exception:
            pass

    ctrl.register_ws_listener(_on_controller_msg)
    recv_task: asyncio.Task[Any] | None = None
    try:
        initial_state = await asyncio.to_thread(ctrl.get_state)
        await websocket.send_json({"type": "state", "data": initial_state})

        recv_task = asyncio.create_task(websocket.receive())
        last_meter_ts = 0.0

        while True:
            now = time.monotonic()
            elapsed = now - last_meter_ts
            wait_sec = max(0.01, 0.25 - elapsed)

            q_task = asyncio.create_task(msg_queue.get())
            done, _ = await asyncio.wait(
                {recv_task, q_task},
                timeout=wait_sec,
                return_when=asyncio.FIRST_COMPLETED,
            )

            if q_task in done:
                payload = q_task.result()
                await websocket.send_json(payload)
                while not msg_queue.empty():
                    next_payload = msg_queue.get_nowait()
                    await websocket.send_json(next_payload)
            else:
                q_task.cancel()

            if recv_task in done:
                msg = recv_task.result()
                if msg.get("type") == "websocket.disconnect":
                    break
                recv_task = asyncio.create_task(websocket.receive())

            now = time.monotonic()
            if (now - last_meter_ts) >= 0.23:
                last_meter_ts = now
                meter_data = ctrl.get_player_status()
                await websocket.send_json({"type": "meter", "data": meter_data})
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.debug("WebSocket connection closed: %s", exc)
    finally:
        ctrl.unregister_ws_listener(_on_controller_msg)
        if recv_task is not None and not recv_task.done():
            recv_task.cancel()


@app.post("/api/queue/add")
async def api_queue_add(req: QueueAddRequest) -> dict[str, Any]:
    """Search or resolve a song and add it to the priority queue."""
    ctrl = get_controller()
    try:
        return await asyncio.to_thread(
            ctrl.add_to_queue,
            query=req.query,
            video_id=req.video_id,
            title=req.title,
            artist=req.artist,
            requester=req.requester,
            pin_top=req.pin_top,
            source=req.source,
            duration_sec=req.duration_sec,
            thumbnail_url=req.thumbnail_url,
            track=req.track,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"点歌入队失败: {exc}") from exc


@app.post("/api/queue/remove")
async def api_queue_remove(req: QueueItemActionRequest) -> dict[str, Any]:
    """Remove a song from the priority queue by item_id."""
    ctrl = get_controller()
    return await asyncio.to_thread(ctrl.remove_from_queue, req.item_id)


@app.post("/api/queue/pin")
async def api_queue_pin(req: QueueItemActionRequest) -> dict[str, Any]:
    """Pin a queued song to the top of the priority queue."""
    ctrl = get_controller()
    return await asyncio.to_thread(ctrl.pin_queue_item, req.item_id)


@app.post("/api/queue/clear")
async def api_queue_clear(
    _payload: dict[str, Any] | None = Body(default=None),
) -> dict[str, Any]:
    """Clear all songs in the priority queue."""
    ctrl = get_controller()
    return await asyncio.to_thread(ctrl.clear_queue)


@app.post("/api/player/control")
async def api_player_control(req: PlayerControlRequest) -> dict[str, Any]:
    """Control player actions: play, pause, resume, skip, stop, test_tone, toggle_autodj, seek."""
    ctrl = get_controller()
    seek_val = req.seek_sec if req.seek_sec is not None else req.position_sec
    try:
        return await asyncio.to_thread(
            ctrl.player_control,
            action=req.action,
            seek_sec=seek_val,
            enabled=req.enabled,
            duration_sec=req.duration_sec,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/audio/devices")
async def api_audio_devices() -> dict[str, Any]:
    """Real-time enumeration of system audio output and input endpoints."""
    ctrl = get_controller()
    return await asyncio.to_thread(ctrl.list_audio_devices)


@app.post("/api/audio/config")
async def api_audio_config(req: AudioConfigRequest, request: Request) -> dict[str, Any]:
    """Hot-update virtual output, monitor output, physical mic, volumes, and auto-ducking settings."""
    ctrl = get_controller()
    try:
        raw_body = await request.json()
        if not isinstance(raw_body, dict):
            raw_body = req.model_dump(exclude_unset=True)
    except Exception:
        raw_body = req.model_dump(exclude_unset=True)
    return await asyncio.to_thread(ctrl.update_audio_config, raw_body)


@app.get("/api/ytmusic/search")
async def api_ytmusic_search(
    q: str = Query(default="", description="Search query keyword"),
    query: str | None = Query(default=None, description="Alias for search query keyword"),
    limit: int = Query(default=8, ge=1, le=50),
) -> list[dict[str, Any]]:
    """Search YouTube Music tracks and videos."""
    ctrl = get_controller()
    search_term = (q or query or "").strip()
    if not search_term:
        return []
    return await asyncio.to_thread(ctrl.search_ytmusic, search_term, limit)


@app.post("/api/ytmusic/auth")
async def api_ytmusic_auth(req: YTMusicAuthRequest) -> dict[str, Any]:
    """Configure YouTube Music authentication headers/cookies and optional HTTP proxy."""
    ctrl = get_controller()
    proxy = req.proxy if req.proxy is not None else ""
    headers_or_cookie = (
        req.raw_headers_or_cookie
        or req.headers_raw
        or req.cookie
        or ""
    )
    cookies_txt = (
        req.cookies_txt_content
        or req.cookies_txt
        or ""
    )
    return await asyncio.to_thread(
        ctrl.configure_ytmusic_auth,
        proxy=proxy,
        raw_headers_or_cookie=headers_or_cookie,
        cookies_txt_content=cookies_txt,
        cache_max_mb=req.cache_max_mb,
    )


@app.get("/api/ytmusic/playlists")
async def api_ytmusic_playlists() -> list[dict[str, Any]]:
    """List user playlists (when authenticated) or built-in fallback seeds."""
    ctrl = get_controller()
    return await asyncio.to_thread(ctrl.get_ytmusic_playlists)


@app.post("/api/ytmusic/fallback")
async def api_ytmusic_fallback(req: YTMusicFallbackRequest) -> dict[str, Any]:
    """Configure 24-hour fallback source and shuffle mode, immediately refreshing the fallback pool."""
    ctrl = get_controller()
    shuffle_flag = (
        req.shuffle
        if req.shuffle is not None
        else (req.fallback_shuffle if req.fallback_shuffle is not None else True)
    )
    return await asyncio.to_thread(
        ctrl.update_ytmusic_fallback,
        fallback_source=req.fallback_source,
        shuffle=bool(shuffle_flag),
    )


@app.get("/api/ocr/windows")
async def api_ocr_windows() -> list[dict[str, Any]]:
    """List visible desktop windows with game/emulator candidates sorted first."""
    ctrl = get_controller()
    return await asyncio.to_thread(ctrl.list_ocr_windows)


@app.get("/api/ocr/screenshot")
async def api_ocr_screenshot(
    monitor: int = Query(default=1, ge=0, description="Monitor index for mss capture"),
) -> Response:
    """Capture desktop monitor screenshot in memory and return JPEG with geometry headers."""
    ctrl = get_controller()
    jpeg_bytes, meta = await asyncio.to_thread(ctrl.capture_ocr_screenshot, monitor)
    headers = {
        "X-Orig-Width": str(meta.get("orig_width", 1920)),
        "X-Orig-Height": str(meta.get("orig_height", 1080)),
        "X-Mon-Left": str(meta.get("left", 0)),
        "X-Mon-Top": str(meta.get("top", 0)),
        "Cache-Control": "no-store",
    }
    return Response(content=jpeg_bytes, media_type="image/jpeg", headers=headers)


@app.get("/api/ocr/roi-preview")
async def api_ocr_roi_preview() -> Response:
    """Capture current OCR ROI region in memory and return PNG preview bytes."""
    ctrl = get_controller()
    png_bytes = await asyncio.to_thread(ctrl.capture_ocr_roi_preview)
    return Response(
        content=png_bytes,
        media_type="image/png",
        headers={"Cache-Control": "no-store"},
    )


@app.post("/api/ocr/config")
async def api_ocr_config(req: OcrConfigRequest, request: Request) -> dict[str, Any]:
    """Save and hot-apply OCR ROI and recognition parameters."""
    ctrl = get_controller()
    try:
        raw_body = await request.json()
        if not isinstance(raw_body, dict):
            raw_body = req.model_dump(exclude_unset=True)
    except Exception:
        raw_body = req.model_dump(exclude_unset=True)
    return await asyncio.to_thread(ctrl.update_ocr_config, raw_body)


@app.post("/api/schedule/config")
async def api_schedule_config(req: BroadcastScheduleRequest, request: Request) -> dict[str, Any]:
    """Save and hot-apply scheduled broadcast window parameters."""
    ctrl = get_controller()
    try:
        raw_body = await request.json()
        if not isinstance(raw_body, dict):
            raw_body = req.model_dump(exclude_unset=True)
    except Exception:
        raw_body = req.model_dump(exclude_unset=True)
    return await asyncio.to_thread(ctrl.update_schedule_config, raw_body)


@app.post("/api/schedule/override")
async def api_schedule_override(req: ScheduleOverrideRequest) -> dict[str, Any]:
    """Enable or disable temporary manual live override outside scheduled hours."""
    ctrl = get_controller()
    return await asyncio.to_thread(ctrl.set_schedule_override, req.active)


@app.post("/api/ocr/test")
async def api_ocr_test(
    req: OcrTestRequest | None = Body(default=None),
) -> dict[str, Any]:
    """Run one-shot OCR self-test on synthetic messages or current ROI, optionally enqueuing recognized songs."""
    ctrl = get_controller()
    synthetic_msgs = None
    enqueue_flag = False
    if req is not None:
        synthetic_msgs = req.synthetic_messages if req.synthetic_messages is not None else req.messages
        enqueue_flag = bool(req.enqueue)
    return await asyncio.to_thread(
        ctrl.test_ocr,
        synthetic_messages=synthetic_msgs,
        enqueue=enqueue_flag,
    )


@app.get("/", include_in_schema=False)
async def serve_frontend_index() -> Response:
    """Serve frontend/index.html or a non-crashing standby page if frontend is not yet generated."""
    index_file = FRONTEND_DIR / "index.html"
    if index_file.is_file():
        return FileResponse(index_file, media_type="text/html; charset=utf-8")
    fallback_html = """<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>天刀·帮派 24h 云音坊控制台</title></head>
<body style="background:#0f141c;color:#e6edf3;font-family:sans-serif;padding:2rem;">
  <h1>天刀·帮派 24h 云音坊控制台</h1>
  <p>后端 API 服务已就绪，前端静态资源加载中...</p>
</body>
</html>"""
    return HTMLResponse(content=fallback_html, status_code=200)


@app.get("/styles.css", include_in_schema=False)
async def serve_frontend_css() -> Response:
    """Serve frontend/styles.css when available."""
    css_file = FRONTEND_DIR / "styles.css"
    if css_file.is_file():
        return FileResponse(css_file, media_type="text/css; charset=utf-8")
    return Response(content="/* styles loading */", media_type="text/css; charset=utf-8")


@app.get("/app.js", include_in_schema=False)
async def serve_frontend_js() -> Response:
    """Serve frontend/app.js when available."""
    js_file = FRONTEND_DIR / "app.js"
    if js_file.is_file():
        return FileResponse(js_file, media_type="application/javascript; charset=utf-8")
    return Response(content="// app loading", media_type="application/javascript; charset=utf-8")


FRONTEND_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True, check_dir=False), name="frontend")
