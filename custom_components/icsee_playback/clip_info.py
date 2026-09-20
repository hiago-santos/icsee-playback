"""Shared clip metadata for Media Browser and the app REST API."""

from __future__ import annotations

import base64
import json
import time
from typing import Any

from homeassistant.core import HomeAssistant

from .dvrip import parse_camera_time, parse_file_length_kb
from .helper import PLAY_MODE_BRIDGE, PLAY_MODE_TRANSCODE, normalize_play_mode
from .play_auth import PLAY_TTL_SEC, signed_play_url, signed_thumb_url


def is_photo(filename: str) -> bool:
    return str(filename).lower().endswith((".jpg", ".jpeg"))


def encode_clip_id(entry_id: str, item: dict[str, Any]) -> str:
    payload = {
        "id": entry_id,
        "f": item.get("FileName") or "",
        "b": item.get("BeginTime") or "",
        "e": item.get("EndTime") or "",
    }
    return base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode()
    ).decode()


def decode_clip_id(token: str) -> dict[str, str]:
    if token.startswith("clip/"):
        token = token.split("/", 1)[1]
    payload = json.loads(base64.urlsafe_b64decode(token.encode()))
    return {
        "id": payload["id"],
        "filename": payload["f"],
        "start": payload["b"],
        "end": payload["e"],
    }


def clip_title(item: dict[str, Any], photo: bool | None = None) -> str:
    filename = str(item.get("FileName") or "")
    if photo is None:
        photo = is_photo(filename)
    begin = item.get("BeginTime") or "?"
    end = item.get("EndTime") or "?"
    try:
        start = parse_camera_time(begin)
        finish = parse_camera_time(end)
        day = start.strftime("%d/%m")
        if photo or (finish - start).total_seconds() <= 2:
            return f"{day} {start.strftime('%H:%M:%S')}"
        return f"{day} {start.strftime('%H:%M')} – {finish.strftime('%H:%M')}"
    except Exception:  # noqa: BLE001
        return str(begin)


def clip_span_sec(item: dict[str, Any]) -> float:
    try:
        begin = parse_camera_time(str(item.get("BeginTime") or ""))
        finish = parse_camera_time(str(item.get("EndTime") or ""))
        seconds = (finish - begin).total_seconds()
    except Exception:  # noqa: BLE001
        return 0.0
    if seconds < 0:
        return 0.0
    return min(seconds, 6 * 3600)


def signed_play_path(
    hass: HomeAssistant,
    entry_id: str,
    filename: str,
    start: str,
    end: str,
    mode: str = PLAY_MODE_TRANSCODE,
) -> tuple[str, int]:
    """Relative play path + unix expiry. Safe for <video> / ExoPlayer (no Bearer)."""
    path = signed_play_url(hass, entry_id, filename, start, end)
    if normalize_play_mode(mode) == PLAY_MODE_BRIDGE:
        path = f"{path}&mode={PLAY_MODE_BRIDGE}"
    expires = int(time.time()) + PLAY_TTL_SEC
    return path, expires


def clip_dict(hass: HomeAssistant, entry_id: str, item: dict[str, Any]) -> dict[str, Any]:
    filename = str(item.get("FileName") or "")
    photo = is_photo(filename)
    begin = str(item.get("BeginTime") or "")
    end = str(item.get("EndTime") or "")
    play_path, expires = signed_play_path(hass, entry_id, filename, begin, end)
    thumb_path = signed_thumb_url(hass, entry_id, filename, begin, end)
    if photo:
        bridge_path = play_path
    else:
        bridge_path, _ = signed_play_path(
            hass, entry_id, filename, begin, end, mode=PLAY_MODE_BRIDGE
        )
    duration = 0.0 if photo else clip_span_sec(item)
    return {
        "id": encode_clip_id(entry_id, item),
        "kind": "photo" if photo else "video",
        "filename": filename,
        "begin": begin,
        "end": end,
        "duration_sec": duration,
        "size_kb": parse_file_length_kb(item.get("FileLength")),
        "title": clip_title(item, photo=photo),
        "mime": "image/jpeg" if photo else "video/mp4",
        "play_path": play_path,
        "bridge_path": bridge_path,
        "thumb_path": thumb_path,
        "bridge_mime": "image/jpeg" if photo else "video/mp4",
        "modes": ["transcode", "bridge"] if not photo else ["transcode"],
        "expires": expires,
    }
