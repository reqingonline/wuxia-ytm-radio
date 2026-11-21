"""Pydantic data models and request/response schemas for the 24h Cloud Jukebox API."""

from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field


class TrackMeta(BaseModel):
    """Normalized metadata for a music track."""

    model_config = ConfigDict(extra="allow")

    video_id: str
    title: str
    artist: str = "未知歌手"
    duration_sec: int = 0
    thumbnail_url: str = ""


class QueueItemModel(BaseModel):
    """A single song request entry in the priority queue."""

    model_config = ConfigDict(extra="allow")

    item_id: str
    track: dict[str, Any]
    requester: str = "少侠"
    source: Literal["ocr", "web", "fallback"] | str = "web"
    added_at: float
    status: Literal["queued", "caching", "ready"] | str = "queued"


class QueueAddRequest(BaseModel):
    """Request payload for POST /api/queue/add."""

    model_config = ConfigDict(extra="allow")

    query: str | None = None
    video_id: str | None = None
    title: str | None = None
    artist: str | None = None
    requester: str = "少侠"
    pin_top: bool = False
    source: str = "web"
    duration_sec: int | float | None = None
    thumbnail_url: str | None = None
    track: dict[str, Any] | None = None


class QueueItemActionRequest(BaseModel):
    """Request payload for POST /api/queue/remove and POST /api/queue/pin."""

    model_config = ConfigDict(extra="allow")

    item_id: str


class QueueClearRequest(BaseModel):
    """Request payload for POST /api/queue/clear."""

    model_config = ConfigDict(extra="allow")


class PlayerControlRequest(BaseModel):
    """Request payload for POST /api/player/control."""

    model_config = ConfigDict(extra="allow")

    action: str = Field(
        ...,
        description="One of: play, pause, resume, skip, stop, test_tone, toggle_autodj, seek",
    )
    seek_sec: float | None = None
    position_sec: float | None = None
    enabled: bool | None = None
    duration_sec: float | None = None


class AudioConfigRequest(BaseModel):
    """Request payload for POST /api/audio/config."""

    model_config = ConfigDict(extra="allow")

    virtual_out_device_id: int | None = None
    monitor_out_device_id: int | None = None
    mic_in_device_id: int | None = None
    mic_device_id: int | None = None
    broadcast_volume: float | None = None
    monitor_enabled: bool | None = None
    monitor_volume: float | None = None
    mic_bridge_enabled: bool | None = None
    mic_volume: float | None = None
    ducking_enabled: bool | None = None
    duck_threshold_db: float | None = None
    ducking_threshold_db: float | None = None
    ducking_db: float | None = None
    duck_db: float | None = None


class YTMusicAuthRequest(BaseModel):
    """Request payload for POST /api/ytmusic/auth."""

    model_config = ConfigDict(extra="allow")

    proxy: str | None = None
    raw_headers_or_cookie: str | None = None
    cookies_txt_content: str | None = None
    headers_raw: str | None = None
    cookie: str | None = None
    cookies_txt: str | None = None
    cache_max_mb: int | None = None


class YTMusicFallbackRequest(BaseModel):
    """Request payload for POST /api/ytmusic/fallback."""

    model_config = ConfigDict(extra="allow")

    fallback_source: str = "search:天涯明月刀 古风 经典"
    shuffle: bool | None = None
    fallback_shuffle: bool | None = None


class OcrConfigRequest(BaseModel):
    """Request payload for POST /api/ocr/config."""

    model_config = ConfigDict(extra="allow")

    enabled: bool | None = None
    roi: dict[str, Any] | None = None
    interval_sec: float | None = None
    trigger_mode: str | None = None
    cooldown_sec: float | None = None
    allow_skip_cmd: bool | None = None
    left: int | None = None
    top: int | None = None
    width: int | None = None
    height: int | None = None


class OcrTestRequest(BaseModel):
    """Request payload for POST /api/ocr/test."""

    model_config = ConfigDict(extra="allow")

    synthetic_messages: list[str] | None = None
    messages: list[str] | None = None
    enqueue: bool = False


class BroadcastScheduleRequest(BaseModel):
    """Request payload for POST /api/schedule/config."""

    model_config = ConfigDict(extra="allow")

    enabled: bool | None = None
    days: list[int] | None = None
    start: str | None = None
    end: str | None = None
    clear_queue_on_end: bool | None = None


class ScheduleOverrideRequest(BaseModel):
    """Request payload for POST /api/schedule/override."""

    model_config = ConfigDict(extra="allow")

    active: bool
