"""iCSee Playback — recordings from the camera SD card in Home Assistant."""

from __future__ import annotations

import logging
import secrets
import shutil
from pathlib import Path

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .api import IcseeCamerasView, IcseeClipsView, IcseeStopView
from .const import DOMAIN
from .helper import CameraRuntime
from .hls import stop_playback_for_entry
from .play_auth import async_load_secret
from .views import IcseeHlsView, IcseePlaybackView, IcseeThumbView

_LOGGER = logging.getLogger(__name__)


def _ensure_store(hass: HomeAssistant) -> None:
    hass.data.setdefault(
        DOMAIN,
        {
            "entries": {},
            "view_registered": False,
            "secret": secrets.token_bytes(32),
        },
    )
    hass.data[DOMAIN].setdefault("secret", secrets.token_bytes(32))
    hass.data[DOMAIN].setdefault("entries", {})
    hass.data[DOMAIN].setdefault("hls", {})


def _purge_legacy_thumb_cache(hass: HomeAssistant) -> None:
    folder = Path(hass.config.path(".storage", "icsee_playback_thumbs"))
    if folder.is_dir():
        shutil.rmtree(folder, ignore_errors=True)


async def async_setup(hass: HomeAssistant, _config: dict) -> bool:
    """YAML setup is not supported."""
    _ensure_store(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up one camera from a config entry."""
    _ensure_store(hass)
    await async_load_secret(hass)
    await hass.async_add_executor_job(_purge_legacy_thumb_cache, hass)
    runtime = CameraRuntime(hass, entry)
    hass.data[DOMAIN]["entries"][entry.entry_id] = runtime

    if not hass.data[DOMAIN]["view_registered"]:
        hass.http.register_view(IcseePlaybackView())
        hass.http.register_view(IcseeThumbView())
        hass.http.register_view(IcseeHlsView())
        hass.http.register_view(IcseeCamerasView())
        hass.http.register_view(IcseeClipsView())
        hass.http.register_view(IcseeStopView())
        hass.data[DOMAIN]["view_registered"] = True

    _LOGGER.info("iCSee Playback ready for %s (%s)", entry.title, runtime.host)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Remove a camera."""
    stop_playback_for_entry(hass, entry.entry_id)
    hass.data.get(DOMAIN, {}).get("entries", {}).pop(entry.entry_id, None)
    return True
