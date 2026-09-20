"""Blocking camera access for iCSee Playback."""

from __future__ import annotations

import hashlib
import logging
import queue
import subprocess
import threading
import time
from datetime import date, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.core import HomeAssistant

from .const import (
    CACHE_TTL,
    CONF_CHANNEL,
    DEFAULT_CHANNEL,
    DEFAULT_PORT,
    PLAY_STALE_SECONDS,
    THUMB_WORKERS,
)
from .dvrip import (
    DVRIP,
    annexb_has_keyframe,
    format_camera_time,
    parse_camera_time,
    prepare_video_es,
)

_LOGGER = logging.getLogger(__name__)


class CannotConnect(Exception):
    """Camera did not accept the TCP/DVRIP session."""


class InvalidAuth(Exception):
    """Username or password was rejected."""


class PlaySuperseded(Exception):
    """A newer play request replaced this one."""


class PlayBusy(Exception):
    """Camera lock could not be taken in time."""


def _clip_span_key(item: dict[str, Any]) -> tuple:
    """Same recording, listed twice. Vídeo: o início já basta como identidade."""
    name = str(item.get("FileName") or "").lower()
    photo = name.endswith((".jpg", ".jpeg"))
    if photo:
        return (item.get("BeginTime"), item.get("EndTime"), True)
    return (item.get("BeginTime"), False)


def _clip_name_rank(item: dict[str, Any]) -> int:
    name = str(item.get("FileName") or "").lower()
    if name.endswith((".h264", ".mp4", ".jpg", ".jpeg")):
        return 2
    return 1


def _clip_beats_duplicate(item: dict[str, Any], prev: dict[str, Any]) -> bool:
    rank = _clip_name_rank(item)
    prev_rank = _clip_name_rank(prev)
    if rank != prev_rank:
        return rank > prev_rank
    return str(item.get("EndTime") or "") > str(prev.get("EndTime") or "")


def _map_thumb_error(err: Exception) -> Exception:
    if isinstance(err, (PlayBusy, PlaySuperseded)):
        return err
    text = str(err).lower()
    if "recusou o claim" in text or "downloadstart recusado" in text:
        return PlayBusy("Câmera ocupada (miniatura)")
    return err


class _ThumbWaiter:
    """Coalesce concurrent thumbnail requests; nothing is written to disk."""

    def __init__(self) -> None:
        self.event = threading.Event()
        self.data: bytes | None = None
        self.error: BaseException | None = None


class _DvripPool:
    """Keep a few logged-in DVRIP sockets so thumbs skip TCP+login each time."""

    def __init__(self, factory, size: int) -> None:
        self._factory = factory
        self._size = size
        self._idle: list[tuple[float, DVRIP]] = []
        self._lock = threading.Lock()

    def borrow(self) -> DVRIP:
        now = time.monotonic()
        with self._lock:
            while self._idle:
                stamp, dvr = self._idle.pop()
                if now - stamp < 25 and dvr.sock is not None:
                    return dvr
                try:
                    dvr.close()
                except Exception:  # noqa: BLE001
                    pass
        return self._factory()

    def give(self, dvr: DVRIP, ok: bool) -> None:
        if not ok or dvr.sock is None:
            try:
                dvr.close()
            except Exception:  # noqa: BLE001
                pass
            return
        try:
            dvr.sock.settimeout(8)
        except OSError:
            try:
                dvr.close()
            except Exception:  # noqa: BLE001
                pass
            return
        with self._lock:
            if len(self._idle) >= self._size:
                try:
                    dvr.close()
                except Exception:  # noqa: BLE001
                    pass
                return
            self._idle.append((time.monotonic(), dvr))

    def discard_idle(self) -> None:
        with self._lock:
            leftover = self._idle
            self._idle = []
        for _stamp, dvr in leftover:
            try:
                dvr.close()
            except Exception:  # noqa: BLE001
                pass


_ENCODER_CACHE: dict[str, str] = {}


def detect_video_encoder(binary: str) -> str:
    cached = _ENCODER_CACHE.get(binary)
    if cached:
        return cached
    text = ""
    try:
        result = subprocess.run(
            [binary, "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
        text = f"{result.stdout}\n{result.stderr}"
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("Could not list ffmpeg encoders: %s", err)
    for name in (
        "libx264",
        "h264_v4l2m2m",
        "h264_qsv",
        "h264_nvenc",
        "h264_vaapi",
    ):
        if name in text:
            _ENCODER_CACHE[binary] = name
            _LOGGER.warning("Using ffmpeg encoder %s", name)
            return name
    _ENCODER_CACHE[binary] = "libx264"
    _LOGGER.warning("ffmpeg encoder list empty; defaulting to libx264")
    return "libx264"


PLAY_MODE_TRANSCODE = "transcode"
PLAY_MODE_BRIDGE = "bridge"


def normalize_play_mode(raw: str | None) -> str:
    mode = (raw or PLAY_MODE_TRANSCODE).strip().lower()
    if mode in ("bridge", "copy", "passthrough", "raw"):
        return PLAY_MODE_BRIDGE
    return PLAY_MODE_TRANSCODE


def apply_play_seek(start: str, end: str, t_raw: str | None) -> str:
    """Move BeginTime forward by t seconds so the app can scrub the clip."""
    try:
        offset = int(float(t_raw or "0"))
    except (TypeError, ValueError):
        return start
    if offset <= 0:
        return start
    try:
        begin = parse_camera_time(start)
        finish = parse_camera_time(end)
    except Exception:  # noqa: BLE001
        return start
    sought = begin + timedelta(seconds=offset)
    latest = finish - timedelta(seconds=2)
    if latest <= begin:
        return start
    if sought > latest:
        sought = latest
    if sought < begin:
        return start
    return format_camera_time(sought)


def ffmpeg_cmd(binary: str, codec: str, mode: str = PLAY_MODE_TRANSCODE) -> list[str]:
    fmt = "hevc" if codec == "hevc" else "h264"
    common = [
        binary,
        "-hide_banner",
        "-loglevel",
        "warning",
        "-fflags",
        "+genpts+discardcorrupt",
        "-err_detect",
        "ignore_err",
        "-f",
        fmt,
        "-i",
        "pipe:0",
        "-an",
    ]
    mux = [
        "-movflags",
        "frag_keyframe+empty_moov+default_base_moof",
        "-f",
        "mp4",
        "pipe:1",
    ]
    if normalize_play_mode(mode) == PLAY_MODE_BRIDGE:
        tag = "hvc1" if codec == "hevc" else "avc1"
        return [*common, "-c:v", "copy", "-tag:v", tag, *mux]
    encoder = detect_video_encoder(binary)
    cmd = [*common, "-c:v", encoder, "-pix_fmt", "yuv420p", *mux]
    if encoder == "libx264":
        idx = cmd.index(encoder) + 1
        cmd[idx:idx] = [
            "-preset",
            "ultrafast",
            "-tune",
            "zerolatency",
        ]
    return cmd


def get_ffmpeg_binary(hass: HomeAssistant) -> str:
    try:
        from homeassistant.components.ffmpeg import get_ffmpeg_manager

        return get_ffmpeg_manager(hass).binary
    except Exception:  # noqa: BLE001
        return "ffmpeg"


def clip_duration_sec(start: str, end: str) -> float:
    try:
        begin = parse_camera_time(start)
        finish = parse_camera_time(end)
        seconds = (finish - begin).total_seconds()
    except Exception:  # noqa: BLE001
        return 60.0
    if seconds < 1:
        return 10.0
    return min(seconds, 6 * 3600)


def ffmpeg_hls_cmd(
    binary: str,
    codec: str,
    out_pattern: str,
    start_number: int,
    segment_time: float,
    fps: int,
) -> list[str]:
    fmt = "hevc" if codec == "hevc" else "h264"
    encoder = detect_video_encoder(binary)
    gop = max(12, int((fps or 12) * segment_time))
    cmd = [
        binary,
        "-hide_banner",
        "-loglevel",
        "warning",
        "-fflags",
        "+genpts+discardcorrupt",
        "-err_detect",
        "ignore_err",
        "-f",
        fmt,
        "-i",
        "pipe:0",
        "-an",
        "-c:v",
        encoder,
        "-pix_fmt",
        "yuv420p",
        "-g",
        str(gop),
        "-keyint_min",
        str(gop),
        "-sc_threshold",
        "0",
        "-force_key_frames",
        f"expr:gte(t,n_forced*{segment_time})",
        "-f",
        "segment",
        "-segment_time",
        str(segment_time),
        "-segment_format",
        "mpegts",
        "-segment_start_number",
        str(start_number),
        "-reset_timestamps",
        "1",
        out_pattern,
    ]
    if encoder == "libx264":
        idx = cmd.index(encoder) + 1
        cmd[idx:idx] = [
            "-preset",
            "ultrafast",
            "-tune",
            "zerolatency",
        ]
    return cmd


def offer(out_queue: queue.Queue, item: Any, timeout: float = 5.0) -> bool:
    """Put on the client queue without ever blocking forever.

    A `put()` with no timeout is a camera lock that never comes back: when the
    HTTP client goes away the queue stays full, the producer thread parks on the
    put while still holding the lock, and from then on every playback and every
    thumbnail answers 409 until Home Assistant restarts.
    """
    try:
        out_queue.put(item, timeout=timeout)
        return True
    except queue.Full:
        return False
    except Exception:  # noqa: BLE001
        return False


def validate_login(host: str, port: int, username: str, password: str) -> str:
    """Try DVRIP login. Returns camera clock or raises."""
    dvr = DVRIP(host, port, timeout=12)
    try:
        dvr.connect()
        if not dvr.login(username, password):
            raise InvalidAuth
        info = dvr.get_named(1452, "OPTimeQuery") or {}
        return str(info.get("OPTimeQuery") or "")
    except InvalidAuth:
        raise
    except Exception as err:
        raise CannotConnect from err
    finally:
        dvr.close()


class CameraRuntime:
    """Per-config-entry camera client with a single DVRIP lock."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.lock = threading.Lock()
        self._query_lock = threading.Lock()
        self._ctrl = threading.Lock()
        self._gen = 0
        self._play_beat = 0.0
        self._active_stop: threading.Event | None = None
        self._cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        self._proc = None
        self._play_dvr: DVRIP | None = None
        self._thumb_guard = threading.Lock()
        self._thumb_inflight: dict[str, _ThumbWaiter] = {}
        self._thumb_live: dict[int, DVRIP] = {}
        self._thumb_slots = threading.Semaphore(THUMB_WORKERS)
        self._thumb_pool = _DvripPool(self._session, THUMB_WORKERS)

    @property
    def name(self) -> str:
        return self.entry.title

    @property
    def host(self) -> str:
        return self.entry.data[CONF_HOST]

    @property
    def port(self) -> int:
        return int(self.entry.data.get(CONF_PORT, DEFAULT_PORT))

    @property
    def username(self) -> str:
        return self.entry.data[CONF_USERNAME]

    @property
    def password(self) -> str:
        return self.entry.data[CONF_PASSWORD]

    @property
    def channel(self) -> int:
        return int(self.entry.data.get(CONF_CHANNEL, DEFAULT_CHANNEL))

    def _session(self, attempts: int = 2) -> DVRIP:
        last_error: Exception | None = None
        for attempt in range(attempts):
            dvr = DVRIP(self.host, self.port, timeout=8)
            try:
                dvr.connect()
                if dvr.login(self.username, self.password):
                    return dvr
                dvr.close()
                last_error = InvalidAuth()
            except InvalidAuth:
                raise
            except Exception as err:  # noqa: BLE001
                last_error = err
                try:
                    dvr.close()
                except Exception:  # noqa: BLE001
                    pass
            time.sleep(0.4 * (attempt + 1))
        if isinstance(last_error, InvalidAuth):
            raise last_error
        raise CannotConnect from last_error

    def list_files_for_day(self, day: date) -> list[dict[str, Any]]:
        return [
            item
            for item in self.list_recent_files(day, day)
            if str(item.get("BeginTime") or "").startswith(day.isoformat())
        ]

    def list_recent_files(self, first_day: date, last_day: date) -> list[dict[str, Any]]:
        key = f"{first_day.isoformat()}_{last_day.isoformat()}"
        now = time.monotonic()
        cached = self._cache.get(key)
        if cached and now - cached[0] < CACHE_TTL:
            return cached[1]

        start = f"{first_day.isoformat()} 00:00:00"
        end = f"{last_day.isoformat()} 23:59:59"
        by_name: dict[str, dict[str, Any]] = {}
        last_error: Exception | None = None
        if not self._query_lock.acquire(timeout=10):
            if cached:
                _LOGGER.warning("Using cached clip list for %s (query busy)", key)
                return cached[1]
            raise RuntimeError("Câmera ocupada (listagem em andamento)")
        try:
            dvr = self._session()
            try:
                for file_type in ("h264", "*", "jpg"):
                    try:
                        batch = dvr.query_files(
                            start, end, self.channel, file_type
                        )
                    except Exception as err:  # noqa: BLE001
                        last_error = err
                        _LOGGER.warning(
                            "File query type=%s failed: %s", file_type, err
                        )
                        continue
                    _LOGGER.warning(
                        "Listed %s clip(s) %s..%s type=%s",
                        len(batch),
                        first_day.isoformat(),
                        last_day.isoformat(),
                        file_type,
                    )
                    for item in batch:
                        name = item.get("FileName")
                        if name and name not in by_name:
                            by_name[name] = item
            finally:
                dvr.close()
        except Exception as err:  # noqa: BLE001
            if cached:
                _LOGGER.warning("Using cached clip list for %s: %s", key, err)
                return cached[1]
            raise
        finally:
            self._query_lock.release()
        by_span: dict[tuple, dict[str, Any]] = {}
        for item in by_name.values():
            span = _clip_span_key(item)
            prev = by_span.get(span)
            if prev is None or _clip_beats_duplicate(item, prev):
                by_span[span] = item
        files = sorted(
            by_span.values(),
            key=lambda item: item.get("BeginTime") or "",
            reverse=True,
        )
        if not files and last_error is not None:
            raise last_error
        self._cache[key] = (now, files)
        return files

    def stream_clip(
        self,
        file_info: dict[str, Any],
        out_queue: queue.Queue,
        stop: threading.Event,
        ffmpeg_bin: str,
        mode: str = PLAY_MODE_TRANSCODE,
    ) -> None:
        """DVRIP download → ffmpeg → queue of bytes, then None.

        transcode: HEVC/H.264 → H.264 fMP4 (navegador / Media Browser).
        bridge: só remux (-c:v copy). O HA não reencoda; o app toca o codec da câmera.
        """
        mode = normalize_play_mode(mode)
        proc = None
        dvr = None
        stream = None
        my_gen = 0
        file_info = dict(file_info)
        file_info.setdefault("Channel", self.channel)

        with self._ctrl:
            self._gen += 1
            my_gen = self._gen
            if self._active_stop is not None:
                self._active_stop.set()
            old_proc = self._proc
            old_dvr = self._play_dvr
            self._proc = None
            self._play_dvr = None
            self._active_stop = stop
        if old_proc:
            try:
                old_proc.kill()
            except Exception:  # noqa: BLE001
                pass
        if old_dvr is not None:
            old_dvr.interrupt()

        # O pedido anterior já foi mandado embora acima; se a trava ainda está
        # de pé é porque ninguém lê aquele playback — solta antes de desistir.
        self.release_stale_playback()
        if not self.lock.acquire(timeout=8):
            self.release_stale_playback()
            if not self.lock.acquire(timeout=8):
                _LOGGER.warning(
                    "Playback busy: lock held, last beat %.1fs ago",
                    time.monotonic() - self._play_beat if self._play_beat else -1,
                )
                offer(out_queue, PlayBusy("A câmera já está em playback."))
                offer(out_queue, None)
                return
        self._play_beat = time.monotonic()
        self.interrupt_all_thumbs()
        self._thumb_pool.discard_idle()
        if stop.is_set() or my_gen != self._gen:
            self._play_beat = 0.0
            self.lock.release()
            offer(out_queue, PlaySuperseded("Playback substituído por outro pedido."))
            offer(out_queue, None)
            return

        try:
            filename = file_info.get("FileName")
            _LOGGER.warning("Playback connecting %s", filename)
            dvr = self._session()
            with self._ctrl:
                self._play_dvr = dvr
            stream = dvr.iter_file_stream(file_info)
            try:
                first_es, demuxer = next(stream)
            except StopIteration as err:
                raise RuntimeError("A câmera não enviou dados do clipe.") from err
            if not first_es:
                raise RuntimeError("Clipe vazio na câmera.")
            codec = demuxer.codec or "hevc"
            trimmed = prepare_video_es(first_es, codec)
            _LOGGER.warning(
                "Playback stream codec=%s %sx%s@%s first=%sB trim=%sB head=%s file=%s",
                codec,
                demuxer.width,
                demuxer.height,
                demuxer.fps,
                len(first_es),
                len(trimmed),
                trimmed[:24].hex(),
                filename,
            )
            first_es = trimmed
            if not first_es:
                raise RuntimeError("Clipe sem NAL de vídeo reconhecível.")

            cmd = ffmpeg_cmd(ffmpeg_bin, codec, mode)
            _LOGGER.warning("ffmpeg mode=%s cmd: %s", mode, " ".join(cmd))
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
            self._proc = proc

            def watch_stop() -> None:
                stop.wait()
                if proc:
                    try:
                        proc.kill()
                    except Exception:  # noqa: BLE001
                        pass
                if dvr:
                    dvr.interrupt()

            def drain_stderr() -> None:
                count = 0
                try:
                    assert proc.stderr is not None
                    for line in proc.stderr:
                        text = line.decode("utf-8", "replace").rstrip()
                        if not text:
                            continue
                        count += 1
                        if count <= 40:
                            _LOGGER.warning("ffmpeg: %s", text)
                        else:
                            _LOGGER.debug("ffmpeg: %s", text)
                except Exception:  # noqa: BLE001
                    pass

            def feed() -> None:
                try:
                    if proc.stdin:
                        proc.stdin.write(first_es)
                        proc.stdin.flush()
                    for chunk, _chunk_demuxer in stream:
                        if stop.is_set():
                            break
                        if proc.stdin and chunk:
                            proc.stdin.write(chunk)
                    if proc.stdin:
                        proc.stdin.close()
                except Exception as err:  # noqa: BLE001
                    _LOGGER.warning("Playback feed failed: %s", err)
                    try:
                        if proc.stdin:
                            proc.stdin.close()
                    except Exception:  # noqa: BLE001
                        pass
                finally:
                    try:
                        if stream is not None:
                            stream.close()
                    except Exception:  # noqa: BLE001
                        pass

            threading.Thread(target=watch_stop, daemon=True, name="icsee-stop").start()
            threading.Thread(target=drain_stderr, daemon=True, name="icsee-ffmpeg-err").start()
            feeder = threading.Thread(target=feed, daemon=True, name="icsee-ffmpeg-in")
            feeder.start()
            assert proc.stdout is not None
            pending = bytearray()
            sent_header = False
            stalled = 0

            def hand_over(payload: bytes) -> bool:
                """Passa bytes ao cliente. False = ninguém está lendo mais."""
                nonlocal stalled
                if not offer(out_queue, payload, timeout=8):
                    stalled += 1
                    return stalled < 3
                stalled = 0
                self._play_beat = time.monotonic()
                return True

            while not stop.is_set():
                data = proc.stdout.read(64 * 1024)
                if not data:
                    break
                if not sent_header:
                    pending.extend(data)
                    if b"moov" in pending and len(pending) >= 64:
                        _LOGGER.warning(
                            "Playback ffmpeg init segment %sB", len(pending)
                        )
                    elif len(pending) > 2_000_000:
                        _LOGGER.warning(
                            "Playback ffmpeg oversized buffer %sB", len(pending)
                        )
                    else:
                        continue
                    if not hand_over(bytes(pending)):
                        break
                    pending.clear()
                    sent_header = True
                    continue
                if not hand_over(data):
                    # Cliente sumiu ou está parado: soltar a câmera vale mais
                    # do que guardar o clipe, senão as miniaturas nunca voltam.
                    _LOGGER.warning("Playback sem leitor, encerrando %s", filename)
                    break
            feeder.join(timeout=2)
            if not sent_header:
                offer(
                    out_queue,
                    RuntimeError(
                        "ffmpeg encerrou sem gerar MP4 (veja as linhas ffmpeg no log)"
                    ),
                )
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("Playback failed")
            offer(out_queue, err)
        finally:
            stop.set()
            if proc:
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass
            if my_gen == self._gen:
                self._proc = None
                if self._play_dvr is dvr:
                    self._play_dvr = None
            try:
                if stream is not None:
                    stream.close()
            except Exception:  # noqa: BLE001
                pass
            if dvr:
                dvr.close()
            self._play_beat = 0.0
            self.lock.release()
            offer(out_queue, None)

    def _bind_thumb(self, stop: threading.Event, dvr: DVRIP) -> None:
        with self._thumb_guard:
            self._thumb_live[id(stop)] = dvr

    def _unbind_thumb(self, stop: threading.Event) -> None:
        with self._thumb_guard:
            self._thumb_live.pop(id(stop), None)

    def interrupt_thumb(self, stop: threading.Event) -> None:
        """Drop the DVRIP socket for a cancelled thumbnail/snapshot HTTP client."""
        stop.set()
        with self._thumb_guard:
            dvr = self._thumb_live.pop(id(stop), None)
        if dvr is not None:
            dvr.interrupt()

    def playback_active(self) -> bool:
        """True while a playback holds the camera *and* is still moving bytes.

        The lock alone is not enough: a client that goes away mid-clip used to
        leave it taken forever, and then every thumbnail got a 409 on the spot.
        """
        if not self.lock.locked():
            return False
        beat = self._play_beat
        return bool(beat) and (time.monotonic() - beat) < PLAY_STALE_SECONDS

    def release_stale_playback(self) -> None:
        """Kill a playback nobody is reading so the camera comes back."""
        if not self.lock.locked() or self.playback_active():
            return
        _LOGGER.warning("Playback parado sem leitor: liberando a câmera")
        self.abort()

    def interrupt_all_thumbs(self) -> None:
        """Playback is starting: drop in-flight thumbnail sockets."""
        with self._thumb_guard:
            live = list(self._thumb_live.values())
            self._thumb_live.clear()
        for dvr in live:
            try:
                dvr.interrupt()
            except Exception:  # noqa: BLE001
                pass

    def download_snapshot(
        self,
        file_info: dict[str, Any],
        max_bytes: int = 8_000_000,
        stop: threading.Event | None = None,
    ) -> bytes:
        """Pull a JPEG from the camera without treating it as video."""
        file_info = dict(file_info)
        file_info.setdefault("Channel", self.channel)
        stop = stop or threading.Event()
        if not self._thumb_slots.acquire(timeout=8):
            raise RuntimeError("Câmera ocupada (playback em andamento)")
        dvr = None
        ok = False
        try:
            if stop.is_set():
                raise PlaySuperseded("Foto cancelada")
            dvr = self._thumb_pool.borrow()
            self._bind_thumb(stop, dvr)
            data = self._download_snapshot_unlocked(
                file_info, max_bytes, dvr=dvr, stop=stop
            )
            ok = True
            return data
        finally:
            self._unbind_thumb(stop)
            if dvr is not None:
                self._thumb_pool.give(dvr, ok and not stop.is_set())
            self._thumb_slots.release()

    def _thumb_key(self, file_info: dict[str, Any]) -> str:
        raw = f"{file_info.get('FileName')}|{file_info.get('BeginTime')}"
        return hashlib.sha1(raw.encode()).hexdigest()

    def get_thumbnail(
        self,
        file_info: dict[str, Any],
        ffmpeg_bin: str,
        stop: threading.Event | None = None,
    ) -> bytes:
        """JPEG for the grid. The app caches on device; HA only coalesces inflight pulls."""
        file_info = dict(file_info)
        file_info.setdefault("Channel", self.channel)
        stop = stop or threading.Event()
        key = self._thumb_key(file_info)

        with self._thumb_guard:
            waiter = self._thumb_inflight.get(key)
            owner = waiter is None
            if owner:
                waiter = _ThumbWaiter()
                self._thumb_inflight[key] = waiter

        if not owner:
            deadline = time.monotonic() + 8
            while not waiter.event.wait(timeout=0.15):
                if stop.is_set():
                    raise PlaySuperseded("Miniatura cancelada")
                if time.monotonic() > deadline:
                    raise RuntimeError("Miniatura ocupada")
            if waiter.error is not None:
                raise waiter.error
            if waiter.data:
                return waiter.data
            raise RuntimeError("Miniatura ocupada")

        try:
            self.release_stale_playback()
            if self.playback_active():
                raise PlayBusy("A câmera já está em playback.")
            deadline = time.monotonic() + 8
            while not self._thumb_slots.acquire(timeout=0.15):
                if stop.is_set():
                    raise PlaySuperseded("Miniatura cancelada")
                if time.monotonic() > deadline:
                    raise PlayBusy("Câmera ocupada (miniatura)")
            dvr = None
            ok = False
            try:
                if stop.is_set():
                    raise PlaySuperseded("Miniatura cancelada")
                dvr = self._thumb_pool.borrow()
                self._bind_thumb(stop, dvr)
                name = str(file_info.get("FileName") or "")
                if name.lower().endswith((".jpg", ".jpeg")):
                    jpeg = self._download_snapshot_unlocked(
                        file_info, max_bytes=1_500_000, dvr=dvr, timeout=12, stop=stop
                    )
                else:
                    jpeg = self._video_thumb_unlocked(
                        file_info, ffmpeg_bin, dvr=dvr, stop=stop
                    )
                    jpeg = self._downscale_thumb(jpeg, ffmpeg_bin)
                if stop.is_set():
                    raise PlaySuperseded("Miniatura cancelada")
                ok = True
            finally:
                self._unbind_thumb(stop)
                if dvr is not None:
                    self._thumb_pool.give(dvr, ok and not stop.is_set())
                self._thumb_slots.release()
            waiter.data = jpeg
            return jpeg
        except Exception as err:
            mapped = _map_thumb_error(err)
            waiter.error = mapped
            raise mapped from err
        finally:
            waiter.event.set()
            with self._thumb_guard:
                self._thumb_inflight.pop(key, None)

    def _video_thumb_unlocked(
        self,
        file_info: dict[str, Any],
        ffmpeg_bin: str,
        dvr: DVRIP | None = None,
        stop: threading.Event | None = None,
    ) -> bytes:
        """First video frame. Caller owns the DVRIP session if [dvr] is set."""
        owned = dvr is None
        if owned:
            dvr = self._session()
        buf = bytearray()
        codec = "hevc"
        try:
            for es, demuxer in dvr.iter_file_stream(file_info, timeout=8):
                if stop is not None and stop.is_set():
                    raise PlaySuperseded("Miniatura cancelada")
                buf.extend(es)
                if demuxer.codec:
                    codec = demuxer.codec
                if len(buf) >= 16_384:
                    prepared = prepare_video_es(bytes(buf), codec)
                    if annexb_has_keyframe(prepared, codec):
                        buf = bytearray(prepared)
                        break
                if len(buf) >= 220_000:
                    break
        finally:
            if owned and dvr:
                dvr.close()
        if len(buf) < 64:
            raise RuntimeError("A câmera não enviou vídeo para a miniatura.")
        es_in = prepare_video_es(bytes(buf), codec)
        fmt = "hevc" if codec == "hevc" else "h264"
        proc = subprocess.run(
            [
                ffmpeg_bin,
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                fmt,
                "-i",
                "pipe:0",
                "-frames:v",
                "1",
                "-vf",
                "scale=480:-2",
                "-q:v",
                "6",
                "-f",
                "image2",
                "pipe:1",
            ],
            input=es_in,
            capture_output=True,
            timeout=20,
            check=False,
        )
        jpeg = proc.stdout or b""
        if proc.returncode != 0 or jpeg[:2] != b"\xff\xd8":
            err = (proc.stderr or b"").decode("utf-8", "ignore")[-240:]
            raise RuntimeError(err or "ffmpeg não gerou a miniatura")
        _LOGGER.debug(
            "Video thumb %sB from %s (%s %sB es)",
            len(jpeg),
            file_info.get("FileName"),
            codec,
            len(es_in),
        )
        return jpeg

    def _downscale_thumb(self, jpeg: bytes, ffmpeg_bin: str) -> bytes:
        """Grid tiles are ~480px; sending the camera's 8MP JPEG is wasted work."""
        if len(jpeg) < 48_000 or jpeg[:2] != b"\xff\xd8":
            return jpeg
        proc = subprocess.run(
            [
                ffmpeg_bin,
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "image2pipe",
                "-i",
                "pipe:0",
                "-vf",
                "scale=480:-2",
                "-q:v",
                "7",
                "-f",
                "image2",
                "pipe:1",
            ],
            input=jpeg,
            capture_output=True,
            timeout=8,
            check=False,
        )
        out = proc.stdout or b""
        if proc.returncode == 0 and out[:2] == b"\xff\xd8" and len(out) < len(jpeg):
            return out
        return jpeg

    def _download_snapshot_unlocked(
        self,
        file_info: dict[str, Any],
        max_bytes: int = 8_000_000,
        dvr: DVRIP | None = None,
        timeout: float = 12,
        stop: threading.Event | None = None,
    ) -> bytes:
        owned = dvr is None
        if owned:
            dvr = self._session()
        buf = bytearray()
        soi = -1
        try:
            for chunk in dvr.iter_raw_download(file_info, timeout=timeout):
                if stop is not None and stop.is_set():
                    raise PlaySuperseded("Foto cancelada")
                if not chunk:
                    continue
                buf.extend(chunk)
                if soi < 0:
                    soi = buf.find(b"\xff\xd8")
                    if soi < 0:
                        if len(buf) > 8:
                            del buf[:-1]
                        continue
                    if soi:
                        del buf[:soi]
                        soi = 0
                lookback = max(0, len(buf) - len(chunk) - 1)
                eoi = buf.find(b"\xff\xd9", lookback)
                if eoi >= 0:
                    jpeg = bytes(buf[: eoi + 2])
                    _LOGGER.debug(
                        "Snapshot extracted %sB from %s",
                        len(jpeg),
                        file_info.get("FileName"),
                    )
                    return jpeg
                if len(buf) >= max_bytes:
                    break
        finally:
            if owned and dvr:
                dvr.close()
        data = bytes(buf)
        start = data.find(b"\xff\xd8")
        if start < 0:
            _LOGGER.warning(
                "Snapshot had no JPEG marker (%sB) head=%s file=%s",
                len(data),
                data[:24].hex() if data else "",
                file_info.get("FileName"),
            )
            raise RuntimeError("A câmera não enviou uma foto JPEG neste arquivo.")
        end = data.rfind(b"\xff\xd9")
        jpeg = data[start : end + 2] if end > start else data[start:]
        _LOGGER.warning("Snapshot fallback %sB from %s", len(jpeg), file_info.get("FileName"))
        return jpeg

    def abort(self) -> None:
        """Stop an in-flight ffmpeg/DVRIP download."""
        with self._ctrl:
            if self._active_stop is not None:
                self._active_stop.set()
            proc = self._proc
            dvr = self._play_dvr
        if proc:
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
        if dvr is not None:
            dvr.interrupt()
        self._thumb_pool.discard_idle()
