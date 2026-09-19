"""REST API for the Casa app: list cameras/clips and stop playback.

Play URLs stay HMAC-signed so ExoPlayer can stream without a Bearer header.
Listing uses the same Home Assistant token the app already sends.

Each clip has play_path (transcode, for browsers) and bridge_path (remux
copy: HA does not re-encode; the app plays the camera codec).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, timedelta

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .clip_info import clip_dict, is_photo
from .const import BROWSE_DAYS, DOMAIN
from .hls import stop_playback_for_entry

_LOGGER = logging.getLogger(__name__)


def _entries(hass: HomeAssistant) -> dict:
    return hass.data.get(DOMAIN, {}).get("entries", {})


def _parse_day(raw: str | None, fallback: date) -> date:
    if not raw:
        return fallback
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return fallback


class IcseeCamerasView(HomeAssistantView):
    """GET /api/icsee_playback/cameras"""

    url = "/api/icsee_playback/cameras"
    name = "api:icsee_playback:cameras"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        cameras = [
            {
                "id": entry_id,
                "name": runtime.name,
                "host": runtime.host,
                "playback_modes": ["transcode", "bridge"],
            }
            for entry_id, runtime in _entries(hass).items()
        ]
        return self.json({"cameras": cameras})


class IcseeClipsView(HomeAssistantView):
    """GET /api/icsee_playback/{entry_id}/clips?start=YYYY-MM-DD&end=YYYY-MM-DD&kind=all|video|photo"""

    url = "/api/icsee_playback/{entry_id}/clips"
    name = "api:icsee_playback:clips"
    requires_auth = True

    async def get(self, request: web.Request, entry_id: str) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        runtime = _entries(hass).get(entry_id)
        if runtime is None:
            raise web.HTTPNotFound(text="Camera not found")

        today = dt_util.now().date()
        first = _parse_day(
            request.query.get("start"), today - timedelta(days=BROWSE_DAYS - 1)
        )
        last = _parse_day(request.query.get("end"), today)
        if last < first:
            first, last = last, first
        kind = (request.query.get("kind") or "all").lower()
        if kind not in ("all", "video", "photo"):
            raise web.HTTPBadRequest(text="kind must be all, video or photo")

        try:
            files = await asyncio.wait_for(
                hass.async_add_executor_job(
                    runtime.list_recent_files, first, last
                ),
                timeout=25,
            )
        except TimeoutError as err:
            raise web.HTTPGatewayTimeout(text="A câmera não respondeu a tempo") from err
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("API list clips failed")
            raise web.HTTPBadGateway(text=str(err)) from err

        clips = []
        for item in files:
            photo = is_photo(str(item.get("FileName") or ""))
            if kind == "video" and photo:
                continue
            if kind == "photo" and not photo:
                continue
            clips.append(clip_dict(hass, entry_id, item))

        return self.json(
            {
                "camera_id": entry_id,
                "name": runtime.name,
                "start": first.isoformat(),
                "end": last.isoformat(),
                "playback_modes": ["transcode", "bridge"],
                "clips": clips,
            }
        )


class IcseeStopView(HomeAssistantView):
    """POST /api/icsee_playback/{entry_id}/stop"""

    url = "/api/icsee_playback/{entry_id}/stop"
    name = "api:icsee_playback:stop"
    requires_auth = True

    async def post(self, request: web.Request, entry_id: str) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        if entry_id not in _entries(hass):
            raise web.HTTPNotFound(text="Camera not found")
        stop_playback_for_entry(hass, entry_id)
        return self.json({"ok": True})
