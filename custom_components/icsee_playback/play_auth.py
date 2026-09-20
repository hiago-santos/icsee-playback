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
SECRET_STORAGE_VERSION = 1
SECRET_STORAGE_KEY = f"{DOMAIN}_secret"


async def async_load_secret(hass: HomeAssistant) -> None:
    """Keep the HMAC key across restarts so listed clip URLs stay valid."""
    from homeassistant.helpers.storage import Store

    store = Store(hass, SECRET_STORAGE_VERSION, SECRET_STORAGE_KEY)
    data = await store.async_load()
    raw = data.get("key") if isinstance(data, dict) else None
    if isinstance(raw, str) and len(raw) >= 32:
        try:
            hass.data[DOMAIN]["secret"] = bytes.fromhex(raw)
            return
        except ValueError:
            pass
    secret = secrets.token_bytes(32)
    hass.data[DOMAIN]["secret"] = secret
    await store.async_save({"key": secret.hex()})


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


def verify_play_query(hass: HomeAssistant, entry_id: str, query) -> str | None:
    """None if the HMAC is valid. Otherwise a short reason for the log."""
    filename = query.get("filename")
    start = query.get("start")
    end = query.get("end")
    exp_raw = query.get("exp")
    sig = query.get("sig")
    if not filename or not start or not end or not exp_raw or not sig:
        return "campos faltando"
    try:
        exp = int(exp_raw)
    except (TypeError, ValueError):
        return "exp inválido"
    if exp < int(time.time()):
        return "link expirado"
    expected = _signature(hass, entry_id, filename, start, end, exp)
    if not hmac.compare_digest(expected, sig):
        return "HMAC não confere"
    return None
