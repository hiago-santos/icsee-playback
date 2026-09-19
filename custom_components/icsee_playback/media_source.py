"""Media Source: browse camera days/clips and play them in HA."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from urllib.parse import unquote
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .clip_info import clip_title, decode_clip_id, encode_clip_id, is_photo
from .const import BROWSE_DAYS, DOMAIN
from .helper import CameraRuntime
from .hls import stop_playback_for_entry
from .play_auth import signed_play_url

try:
    from homeassistant.components.media_player import BrowseError, MediaClass, MediaType
except ImportError:  # pragma: no cover
    from homeassistant.components.media_player.const import MediaClass, MediaType
    from homeassistant.components.media_player.errors import BrowseError

try:
    from homeassistant.components.media_source import (
        BrowseMediaSource,
        MediaSource,
        MediaSourceItem,
        PlayMedia,
        Unresolvable,
    )
except ImportError:  # pragma: no cover
    from homeassistant.components.media_source.error import Unresolvable
    from homeassistant.components.media_source.models import (
        BrowseMediaSource,
        MediaSource,
        MediaSourceItem,
        PlayMedia,
    )

_LOGGER = logging.getLogger(__name__)


async def async_get_media_source(hass: HomeAssistant) -> MediaSource:
    """Set up the iCSee playback media source."""
    return IcseePlaybackMediaSource(hass)


def _entries(hass: HomeAssistant) -> dict[str, CameraRuntime]:
    return hass.data.get(DOMAIN, {}).get("entries", {})


def _encode_clip(entry_id: str, item: dict[str, Any]) -> str:
    return f"clip/{encode_clip_id(entry_id, item)}"


def _decode_clip(identifier: str) -> dict[str, str]:
    return decode_clip_id(identifier)


def _clip_title(item: dict[str, Any], photo: bool = False) -> str:
    return clip_title(item, photo=photo)


def _message_item(identifier: str, title: str) -> BrowseMediaSource:
    return BrowseMediaSource(
        domain=DOMAIN,
        identifier=identifier,
        media_class=MediaClass.DIRECTORY,
        media_content_type="",
        title=title,
        can_play=False,
        can_expand=False,
    )


class IcseePlaybackMediaSource(MediaSource):
    """Browse recordings stored on iCSee cameras."""

    name = "iCSee Playback"

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(DOMAIN)
        self.hass = hass

    def _normalize(self, identifier: str) -> str:
        identifier = unquote(identifier or "")
        if identifier.startswith("cam|"):
            return identifier.split("|", 1)[1]
        if identifier.startswith("day|"):
            _, entry_id, day = identifier.split("|", 2)
            return f"{entry_id}/{day}"
        if identifier.startswith("clip|"):
            parts = identifier.split("|")
            if len(parts) == 5:
                return _encode_clip(
                    parts[1],
                    {
                        "FileName": parts[2],
                        "BeginTime": parts[3],
                        "EndTime": parts[4],
                    },
                )
        return identifier

    async def async_resolve_media(self, item: MediaSourceItem) -> PlayMedia:
        identifier = self._normalize(item.identifier or "")
        if not identifier.startswith("clip/"):
            raise Unresolvable("Item de mídia inválido")
        try:
            clip = _decode_clip(identifier)
        except Exception as err:  # noqa: BLE001
            raise Unresolvable("Clipe inválido") from err
        if clip["id"] not in _entries(self.hass):
            raise Unresolvable("Câmera não encontrada")
        url = signed_play_url(
            self.hass,
            clip["id"],
            clip["filename"],
            clip["start"],
            clip["end"],
        )
        mime = (
            "image/jpeg"
            if clip["filename"].lower().endswith((".jpg", ".jpeg"))
            else "video/mp4"
        )
        try:
            from datetime import timedelta as td

            from homeassistant.components.http.auth import async_sign_path

            url = async_sign_path(self.hass, url, td(hours=1))
        except Exception:  # noqa: BLE001
            pass
        return PlayMedia(url, mime)

    async def async_browse_media(self, item: MediaSourceItem) -> BrowseMediaSource:
        identifier = self._normalize(item.identifier or "")
        entries = _entries(self.hass)
        _LOGGER.debug("Browse identifier=%s", identifier)

        if not identifier:
            return self._browse_root(entries)
        if identifier.startswith("clip/"):
            raise BrowseError("Este item é um vídeo, não uma pasta")

        parts = identifier.split("/")
        if len(parts) == 1:
            return await self._browse_kinds(parts[0], entries)
        if len(parts) == 2:
            return await self._browse_clips(parts[0], parts[1], entries)

        raise BrowseError("Pasta desconhecida")

    def _browse_root(self, entries: dict[str, CameraRuntime]) -> BrowseMediaSource:
        children = [
            BrowseMediaSource(
                domain=DOMAIN,
                identifier=entry_id,
                media_class=MediaClass.DIRECTORY,
                media_content_type="",
                title=runtime.name,
                can_play=False,
                can_expand=True,
            )
            for entry_id, runtime in entries.items()
        ]
        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=None,
            media_class=MediaClass.APP,
            media_content_type="",
            title="iCSee Playback",
            can_play=False,
            can_expand=True,
            children_media_class=MediaClass.DIRECTORY,
            children=children,
        )

    async def _load_recent(self, runtime: CameraRuntime) -> list[dict[str, Any]]:
        today = dt_util.now().date()
        first = today - timedelta(days=BROWSE_DAYS - 1)
        return await asyncio.wait_for(
            self.hass.async_add_executor_job(
                runtime.list_recent_files, first, today
            ),
            timeout=25,
        )

    def _is_photo(self, item: dict[str, Any]) -> bool:
        return is_photo(str(item.get("FileName") or ""))

    async def _browse_kinds(
        self, entry_id: str, entries: dict[str, CameraRuntime]
    ) -> BrowseMediaSource:
        runtime = entries.get(entry_id)
        if runtime is None:
            raise BrowseError("Câmera não encontrada")
        stop_playback_for_entry(self.hass, entry_id)

        error_title = None
        files: list[dict[str, Any]] = []
        try:
            files = await self._load_recent(runtime)
        except TimeoutError:
            error_title = "A câmera não respondeu a tempo"
            _LOGGER.error("Timeout listing recordings")
        except RuntimeError as err:
            error_title = str(err)
            _LOGGER.warning("Failed to list recordings: %s", err)
        except Exception as err:  # noqa: BLE001
            error_title = f"Erro ao listar: {err}"
            _LOGGER.exception("Failed to list recordings")

        children: list[BrowseMediaSource] = []
        if error_title:
            children.append(_message_item(f"{entry_id}/error", error_title))
        else:
            videos = [item for item in files if not self._is_photo(item)]
            photos = [item for item in files if self._is_photo(item)]
            if videos:
                children.append(
                    BrowseMediaSource(
                        domain=DOMAIN,
                        identifier=f"{entry_id}/videos",
                        media_class=MediaClass.DIRECTORY,
                        media_content_type="",
                        title=f"Vídeos ({len(videos)})",
                        can_play=False,
                        can_expand=True,
                    )
                )
            if photos:
                children.append(
                    BrowseMediaSource(
                        domain=DOMAIN,
                        identifier=f"{entry_id}/photos",
                        media_class=MediaClass.DIRECTORY,
                        media_content_type="",
                        title=f"Fotos ({len(photos)})",
                        can_play=False,
                        can_expand=True,
                    )
                )
            if not children:
                children.append(
                    _message_item(
                        f"{entry_id}/empty",
                        f"Nenhuma gravação nos últimos {BROWSE_DAYS} dias",
                    )
                )
        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=entry_id,
            media_class=MediaClass.DIRECTORY,
            media_content_type="",
            title=runtime.name,
            can_play=False,
            can_expand=True,
            children_media_class=MediaClass.DIRECTORY,
            children=children,
        )

    async def _browse_clips(
        self,
        entry_id: str,
        kind: str,
        entries: dict[str, CameraRuntime],
    ) -> BrowseMediaSource:
        runtime = entries.get(entry_id)
        if runtime is None:
            raise BrowseError("Câmera não encontrada")
        if kind == "photos":
            stop_playback_for_entry(self.hass, entry_id)

        want_photo = kind == "photos"
        if kind not in ("videos", "photos"):
            raise BrowseError("Pasta desconhecida")

        error_title = None
        files: list[dict[str, Any]] = []
        try:
            files = [
                item
                for item in await self._load_recent(runtime)
                if self._is_photo(item) == want_photo
            ]
        except TimeoutError:
            error_title = "A câmera não respondeu a tempo"
            _LOGGER.error("Timeout listing %s", kind)
        except RuntimeError as err:
            error_title = str(err)
            _LOGGER.warning("Failed to list %s: %s", kind, err)
        except Exception as err:  # noqa: BLE001
            error_title = f"Erro ao listar: {err}"
            _LOGGER.exception("Failed to list %s", kind)

        children: list[BrowseMediaSource] = []
        if error_title:
            children.append(_message_item(f"{entry_id}/{kind}/error", error_title))
        elif not files:
            children.append(
                _message_item(
                    f"{entry_id}/{kind}/empty",
                    "Nenhum item neste período",
                )
            )
        else:
            for clip in files:
                children.append(
                    BrowseMediaSource(
                        domain=DOMAIN,
                        identifier=_encode_clip(entry_id, clip),
                        media_class=MediaClass.IMAGE if want_photo else MediaClass.VIDEO,
                        media_content_type="image/jpeg" if want_photo else "video/mp4",
                        title=_clip_title(clip, photo=want_photo),
                        can_play=True,
                        can_expand=False,
                    )
                )

        title = "Fotos" if want_photo else "Vídeos"
        if files:
            title = f"{title} · {len(files)}"
        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=f"{entry_id}/{kind}",
            media_class=MediaClass.DIRECTORY,
            media_content_type="",
            title=title,
            can_play=False,
            can_expand=True,
            children_media_class=MediaClass.IMAGE if want_photo else MediaClass.VIDEO,
            children=children,
        )
