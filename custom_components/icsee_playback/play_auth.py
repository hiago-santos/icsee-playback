"""Short-lived HMAC so <video> can fetch without HA Bearer token."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from urllib.parse import urlencode

from homeassistant.core import HomeAssistant

from .const import DOMAIN

PLAY_TTL_SEC = 3600
THUMB_TTL_SEC = 7 * 86400


def ensure_secret(hass: HomeAssistant) -> bytes:
    secret = hass.data[DOMAIN].get("secret")
    if not isinstance(secret, bytes):
        secret = secrets.token_bytes(32)
        hass.data[DOMAIN]["secret"] = secret
    return secret


def _signature(
    hass: HomeAssistant,
    entry_id: str,
    filename: str,
    start: str,
    end: str,
    exp: int,
) -> str:
    msg = f"{entry_id}|{filename}|{start}|{end}|{exp}".encode()
    return hmac.new(ensure_secret(hass), msg, hashlib.sha256).hexdigest()


def signed_play_url(
    hass: HomeAssistant,
    entry_id: str,
    filename: str,
    start: str,
    end: str,
) -> str:
    exp = int(time.time()) + PLAY_TTL_SEC
    query = urlencode(
        {
            "filename": filename,
            "start": start,
            "end": end,
            "exp": str(exp),
            "sig": _signature(hass, entry_id, filename, start, end, exp),
        }
    )
    return f"/api/icsee_playback/{entry_id}/play?{query}"


def signed_thumb_url(
    hass: HomeAssistant,
    entry_id: str,
    filename: str,
    start: str,
    end: str,
) -> str:
    """Stable for a week so Coil can cache by URL across list refreshes."""
    exp = ((int(time.time()) // THUMB_TTL_SEC) + 1) * THUMB_TTL_SEC
    query = urlencode(
        {
            "filename": filename,
            "start": start,
            "end": end,
            "exp": str(exp),
            "sig": _signature(hass, entry_id, filename, start, end, exp),
        }
    )
    return f"/api/icsee_playback/{entry_id}/thumb?{query}"


def verify_play_query(hass: HomeAssistant, entry_id: str, query) -> bool:
    filename = query.get("filename")
    start = query.get("start")
    end = query.get("end")
    exp_raw = query.get("exp")
    sig = query.get("sig")
    if not filename or not start or not end or not exp_raw or not sig:
        return False
    try:
        exp = int(exp_raw)
    except (TypeError, ValueError):
        return False
    if exp < int(time.time()):
        return False
    expected = _signature(hass, entry_id, filename, start, end, exp)
    return hmac.compare_digest(expected, sig)
