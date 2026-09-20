"""HTTP stream for iCSee recordings.

The media <video> tag cannot send HA's Bearer token, so this view is
opened by a short-lived HMAC on the query string instead of requires_auth.
"""

from __future__ import annotations

import asyncio
import logging
import queue
import threading

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .helper import (
    PlayBusy,
    PlaySuperseded,
    apply_play_seek,
    get_ffmpeg_binary,
    normalize_play_mode,
)
from .hls import get_hls_session
from .play_auth import verify_play_query

_LOGGER = logging.getLogger(__name__)


def _ffmpeg_binary(hass: HomeAssistant) -> str:
    return get_ffmpeg_binary(hass)


def _is_jpeg(filename: str) -> bool:
    return filename.lower().endswith((".jpg", ".jpeg"))


class IcseePlaybackView(HomeAssistantView):
    """GET /api/icsee_playback/{entry_id}/play?...&exp=&sig="""

    url = "/api/icsee_playback/{entry_id}/play"
    name = "api:icsee_playback:play"
    requires_auth = False

    async def get(self, request: web.Request, entry_id: str) -> web.StreamResponse:
        hass: HomeAssistant = request.app["hass"]
        if not verify_play_query(hass, entry_id, request.query) and request.get(
            "hass_user"
        ) is None:
            _LOGGER.warning(
                "Rejected play (invalid sig) entry=%s keys=%s",
                entry_id,
                list(request.query.keys()),
            )
            raise web.HTTPUnauthorized(text="Invalid or expired playback link")

        runtime = hass.data.get(DOMAIN, {}).get("entries", {}).get(entry_id)
        if runtime is None:
            raise web.HTTPNotFound(text="Camera not found")

        filename = request.query.get("filename", "")
        start = request.query.get("start", "")
        end = request.query.get("end", "")
        start = apply_play_seek(start, end, request.query.get("t"))
        file_info = {
            "FileName": filename,
            "BeginTime": start,
            "EndTime": end,
            "Channel": runtime.channel,
        }

        _LOGGER.warning(
            "Play request %s t=%s begin=%s",
            filename,
            request.query.get("t"),
            start,
        )

        if _is_jpeg(filename):
            stop = threading.Event()

            def _pull_jpeg() -> bytes:
                return runtime.download_snapshot(file_info, stop=stop)

            try:
                data = await asyncio.wait_for(
                    hass.async_add_executor_job(_pull_jpeg),
                    timeout=20,
                )
            except asyncio.CancelledError:
                stop.set()
                runtime.interrupt_thumb(stop)
                raise
            except TimeoutError as err:
                stop.set()
                runtime.interrupt_thumb(stop)
                _LOGGER.error("Snapshot timeout: %s", filename)
                raise web.HTTPGatewayTimeout(text="A câmera não enviou a foto a tempo") from err
            except PlaySuperseded as err:
                raise web.HTTPConflict(text=str(err)) from err
            except Exception as err:  # noqa: BLE001
                if stop.is_set():
                    raise web.HTTPConflict(text="Foto cancelada") from err
                _LOGGER.exception("Snapshot download failed")
                raise web.HTTPBadGateway(text=str(err)) from err
            if not data:
                raise web.HTTPBadGateway(text="Foto vazia")
            _LOGGER.warning("Snapshot ok %sB %s", len(data), filename)
            return web.Response(
                body=data,
                content_type="image/jpeg",
                headers={"Cache-Control": "no-store"},
            )

        mode = normalize_play_mode(request.query.get("mode"))
        stop = threading.Event()
        chunks: queue.Queue = queue.Queue(maxsize=32)
        ffmpeg_bin = _ffmpeg_binary(hass)
        _LOGGER.warning("Playback ffmpeg binary=%s mode=%s", ffmpeg_bin, mode)

        producer = threading.Thread(
            target=runtime.stream_clip,
            args=(file_info, chunks, stop, ffmpeg_bin, mode),
            name=f"icsee-play-{entry_id[:8]}",
            daemon=True,
        )
        producer.start()

        try:
            first = await asyncio.to_thread(chunks.get, True, 60)
        except queue.Empty:
            stop.set()
            _LOGGER.error("Playback timeout waiting for first MP4 bytes: %s", filename)
            raise web.HTTPGatewayTimeout(
                text="A câmera/ffmpeg não enviou vídeo a tempo"
            ) from None

        if isinstance(first, PlaySuperseded):
            raise web.HTTPConflict(text=str(first))
        if isinstance(first, PlayBusy):
            raise web.HTTPConflict(text=str(first))
        if isinstance(first, Exception):
            stop.set()
            _LOGGER.error("Playback error: %s", first)
            raise web.HTTPBadGateway(text=str(first)) from first
        if not first:
            stop.set()
            raise web.HTTPBadGateway(text="ffmpeg não gerou vídeo")

        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "video/mp4",
                "Cache-Control": "no-store, no-cache",
                "X-Accel-Buffering": "no",
                "Accept-Ranges": "none",
                "X-Icsee-Mode": mode,
            },
        )
        await response.prepare(request)
        await response.write(first)

        try:
            while True:
                try:
                    chunk = await asyncio.to_thread(chunks.get, True, 30)
                except queue.Empty:
                    break
                if chunk is None:
                    break
                if isinstance(chunk, Exception):
                    _LOGGER.error("Playback error: %s", chunk)
                    break
                await response.write(chunk)
        except (asyncio.CancelledError, ConnectionResetError, ConnectionAbortedError):
            stop.set()
            raise
        finally:
            stop.set()
            try:
                await asyncio.to_thread(producer.join, 8)
            except Exception:  # noqa: BLE001
                pass

        return response


class IcseeThumbView(HomeAssistantView):
    """GET /api/icsee_playback/{entry_id}/thumb?...&exp=&sig="""

    url = "/api/icsee_playback/{entry_id}/thumb"
    name = "api:icsee_playback:thumb"
    requires_auth = False

    async def get(self, request: web.Request, entry_id: str) -> web.StreamResponse:
        hass: HomeAssistant = request.app["hass"]
        if not verify_play_query(hass, entry_id, request.query) and request.get(
            "hass_user"
        ) is None:
            raise web.HTTPUnauthorized(text="Invalid or expired thumbnail link")

        runtime = hass.data.get(DOMAIN, {}).get("entries", {}).get(entry_id)
        if runtime is None:
            raise web.HTTPNotFound(text="Camera not found")

        filename = request.query.get("filename", "")
        start = request.query.get("start", "")
        end = request.query.get("end", "")
        file_info = {
            "FileName": filename,
            "BeginTime": start,
            "EndTime": end,
            "Channel": runtime.channel,
        }
        ffmpeg_bin = _ffmpeg_binary(hass)
        stop = threading.Event()

        def _pull_thumb() -> bytes:
            return runtime.get_thumbnail(file_info, ffmpeg_bin, stop)

        try:
            data = await asyncio.wait_for(
                hass.async_add_executor_job(_pull_thumb),
                timeout=55,
            )
        except asyncio.CancelledError:
            stop.set()
            runtime.interrupt_thumb(stop)
            raise
        except TimeoutError as err:
            stop.set()
            runtime.interrupt_thumb(stop)
            raise web.HTTPGatewayTimeout(text="A câmera não enviou a miniatura a tempo") from err
        except PlayBusy as err:
            raise web.HTTPConflict(text=str(err)) from err
        except PlaySuperseded as err:
            raise web.HTTPConflict(text=str(err)) from err
        except Exception as err:  # noqa: BLE001
            if stop.is_set():
                raise web.HTTPConflict(text="Miniatura cancelada") from err
            _LOGGER.warning("Thumbnail failed %s: %s", filename, err)
            raise web.HTTPBadGateway(text=str(err)) from err
        if not data:
            raise web.HTTPBadGateway(text="Miniatura vazia")
        return web.Response(
            body=data,
            content_type="image/jpeg",
            headers={
                "Cache-Control": "public, max-age=604800, immutable",
            },
        )


class IcseeHlsView(HomeAssistantView):
    """GET /api/icsee_playback/{entry_id}/hls/{session_id}/index.m3u8|segNNNN.ts"""

    url = "/api/icsee_playback/{entry_id}/hls/{session_id}/{name}"
    name = "api:icsee_playback:hls"
    requires_auth = False

    async def get(
        self, request: web.Request, entry_id: str, session_id: str, name: str
    ) -> web.StreamResponse:
        hass: HomeAssistant = request.app["hass"]
        session = get_hls_session(hass, session_id)
        if session is None or session.entry_id != entry_id:
            raise web.HTTPNotFound(text="Playback session not found")

        if name == "index.m3u8":
            return web.Response(
                text=session.playlist(),
                content_type="application/vnd.apple.mpegurl",
                headers={"Cache-Control": "no-cache"},
            )

        if name.startswith("seg") and name.endswith(".ts"):
            try:
                index = int(name[3:-3])
            except ValueError as err:
                raise web.HTTPNotFound(text="Invalid segment") from err
            try:
                data = await asyncio.to_thread(session.wait_segment, index)
            except TimeoutError as err:
                raise web.HTTPGatewayTimeout(text=str(err)) from err
            except Exception as err:  # noqa: BLE001
                _LOGGER.exception("HLS segment %s failed", name)
                raise web.HTTPBadGateway(text=str(err)) from err
            return web.Response(
                body=data,
                content_type="video/mp2t",
                headers={"Cache-Control": "no-store"},
            )

        raise web.HTTPNotFound(text="Unknown HLS resource")
