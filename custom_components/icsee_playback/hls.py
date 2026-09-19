"""HLS VOD sessions so the HA player has real duration and seeking."""

from __future__ import annotations

import logging
import math
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant

from .const import DOMAIN, HLS_IDLE_SEC, HLS_SEGMENT_SEC
from .dvrip import format_camera_time, parse_camera_time, prepare_video_es
from .helper import CameraRuntime, clip_duration_sec, ffmpeg_hls_cmd

_LOGGER = logging.getLogger(__name__)


def _sessions(hass: HomeAssistant) -> dict[str, "HlsSession"]:
    return hass.data[DOMAIN].setdefault("hls", {})


class HlsSession:
    def __init__(
        self,
        runtime: CameraRuntime,
        file_info: dict[str, Any],
        ffmpeg_bin: str,
    ) -> None:
        self.id = secrets.token_urlsafe(16)
        self.runtime = runtime
        self.entry_id = runtime.entry.entry_id
        self.file_info = dict(file_info)
        self.ffmpeg_bin = ffmpeg_bin
        self.duration = clip_duration_sec(
            str(file_info.get("BeginTime") or ""),
            str(file_info.get("EndTime") or ""),
        )
        self.segment_time = float(HLS_SEGMENT_SEC)
        self.n_segments = max(1, math.ceil(self.duration / self.segment_time))
        self.dir = Path(tempfile.mkdtemp(prefix="icsee_hls_"))
        self._stop = threading.Event()
        self._seek_event = threading.Event()
        self._seek_index = 0
        self._span_start: int | None = None
        self._proc = None
        self._error: Exception | None = None
        self._idle = False
        self._started = threading.Event()
        self._worker: threading.Thread | None = None
        self._last_access = time.monotonic()
        self._hass: HomeAssistant | None = None

    def touch(self) -> None:
        self._last_access = time.monotonic()

    def playlist(self) -> str:
        self.touch()
        lines = [
            "#EXTM3U",
            "#EXT-X-VERSION:3",
            f"#EXT-X-TARGETDURATION:{max(1, int(self.segment_time) + 1)}",
            "#EXT-X-MEDIA-SEQUENCE:0",
            "#EXT-X-PLAYLIST-TYPE:VOD",
            "#EXT-X-INDEPENDENT-SEGMENTS",
        ]
        remaining = self.duration
        for index in range(self.n_segments):
            inf = min(self.segment_time, remaining)
            if inf < 0.25:
                inf = self.segment_time
            lines.append(f"#EXTINF:{inf:.3f},")
            lines.append(f"seg{index:04d}.ts")
            remaining -= self.segment_time
        lines.append("#EXT-X-ENDLIST")
        return "\n".join(lines) + "\n"

    def start(self) -> None:
        self._worker = threading.Thread(
            target=self._worker_loop,
            name=f"icsee-hls-{self.id[:8]}",
            daemon=True,
        )
        self._worker.start()
        threading.Thread(
            target=self._watch_idle,
            name=f"icsee-hls-idle-{self.id[:8]}",
            daemon=True,
        ).start()

    def stop(self) -> None:
        self._stop.set()
        self._seek_event.set()
        proc = self._proc
        if proc:
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
        self.runtime.abort()

    def _watch_idle(self) -> None:
        while not self._stop.wait(2):
            if time.monotonic() - self._last_access < HLS_IDLE_SEC:
                continue
            _LOGGER.warning("Playback stopped after leaving the player")
            if self._hass is not None:
                drop_hls_session(self._hass, self.id)
            else:
                self.stop()
            return

    def cleanup(self) -> None:
        self.stop()
        try:
            shutil.rmtree(self.dir, ignore_errors=True)
        except Exception:  # noqa: BLE001
            pass

    def _seg_path(self, index: int) -> Path:
        return self.dir / f"seg{index:04d}.ts"

    def _segment_ready(self, index: int) -> bool:
        path = self._seg_path(index)
        if not path.exists() or path.stat().st_size < 188:
            return False
        if index >= self.n_segments - 1:
            return self._idle or self._proc is None
        return self._seg_path(index + 1).exists()

    def ensure_producing(self, index: int) -> None:
        if self._segment_ready(index):
            return
        with self.runtime._ctrl:
            current = self._span_start
            proc = self._proc
        latest = -1
        for probe in range(self.n_segments):
            if self._seg_path(probe + 1).exists():
                latest = probe
            else:
                break
        anchor = latest if latest >= 0 else (current if current is not None else -1)
        # hls.js preloads a few segments; only jump on a real seek.
        if index <= anchor + 20:
            return
        _LOGGER.warning("HLS seek to segment %s / %s", index, self.n_segments)
        self._seek_index = index
        self._seek_event.set()
        if proc:
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass

    def wait_segment(self, index: int, timeout: float = 70) -> bytes:
        if index < 0 or index >= self.n_segments:
            raise FileNotFoundError(index)
        self.touch()
        self._started.wait(timeout=15)
        self.ensure_producing(index)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.touch()
            if self._stop.is_set():
                break
            if self._error and not self._seg_path(index).exists():
                raise self._error
            if self._segment_ready(index):
                return self._seg_path(index).read_bytes()
            time.sleep(0.12)
        raise TimeoutError(f"Segmento {index} não ficou pronto a tempo")

    def _worker_loop(self) -> None:
        if not self.runtime.lock.acquire(timeout=20):
            self._error = RuntimeError("A câmera já está em playback.")
            self._started.set()
            return
        self._started.set()
        try:
            while not self._stop.is_set():
                index = max(0, min(self._seek_index, self.n_segments - 1))
                self._seek_event.clear()
                self._idle = False
                self._span_start = index
                try:
                    self._run_span(index)
                except Exception as err:  # noqa: BLE001
                    if not self._stop.is_set() and not self._seek_event.is_set():
                        _LOGGER.exception("HLS span failed")
                        self._error = err
                        break
                self._proc = None
                self.runtime._proc = None
                if self._stop.is_set():
                    break
                if self._seek_event.is_set():
                    continue
                self._idle = True
                self._seek_event.wait()
                self._idle = False
        finally:
            self._proc = None
            self.runtime._proc = None
            self.runtime.lock.release()

    def _file_info_from(self, index: int) -> dict[str, Any]:
        info = dict(self.file_info)
        begin = parse_camera_time(str(info["BeginTime"]))
        start = begin + timedelta(seconds=index * self.segment_time)
        end = parse_camera_time(str(info["EndTime"]))
        if start >= end:
            start = end - timedelta(seconds=1)
        info["BeginTime"] = format_camera_time(start)
        info.setdefault("Channel", self.runtime.channel)
        return info

    def _run_span(self, start_index: int) -> None:
        file_info = self._file_info_from(start_index)
        _LOGGER.warning(
            "HLS span seg=%s start=%s file=%s",
            start_index,
            file_info.get("BeginTime"),
            file_info.get("FileName"),
        )
        dvr = None
        stream = None
        proc = None
        try:
            dvr = self.runtime._session()
            stream = dvr.iter_file_stream(file_info)
            first_es, demuxer = next(stream)
            first_es = prepare_video_es(first_es, demuxer.codec or "hevc")
            if not first_es:
                raise RuntimeError("Clipe sem NAL de vídeo reconhecível.")
            cmd = ffmpeg_hls_cmd(
                self.ffmpeg_bin,
                demuxer.codec or "hevc",
                str(self.dir / "seg%04d.ts"),
                start_index,
                self.segment_time,
                demuxer.fps or 12,
            )
            _LOGGER.warning("ffmpeg hls: %s", " ".join(cmd))
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
            self._proc = proc
            self.runtime._proc = proc
            self.runtime._active_stop = self._stop

            def drain_stderr() -> None:
                try:
                    assert proc.stderr is not None
                    for line in proc.stderr:
                        text = line.decode("utf-8", "replace").rstrip()
                        if text:
                            _LOGGER.warning("ffmpeg: %s", text)
                except Exception:  # noqa: BLE001
                    pass

            threading.Thread(target=drain_stderr, daemon=True).start()
            try:
                if proc.stdin:
                    proc.stdin.write(first_es)
                    proc.stdin.flush()
                for chunk, _demuxer in stream:
                    if self._stop.is_set() or self._seek_event.is_set():
                        break
                    if proc.stdin and chunk:
                        proc.stdin.write(chunk)
                if proc.stdin:
                    proc.stdin.close()
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("HLS feed failed: %s", err)
                try:
                    if proc.stdin:
                        proc.stdin.close()
                except Exception:  # noqa: BLE001
                    pass
            if self._stop.is_set() or self._seek_event.is_set():
                proc.kill()
            else:
                proc.wait()
        finally:
            if proc and proc.poll() is None:
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass
            try:
                if stream is not None:
                    stream.close()
            except Exception:  # noqa: BLE001
                pass
            if dvr:
                dvr.close()


def start_hls_session(
    hass: HomeAssistant,
    runtime: CameraRuntime,
    file_info: dict[str, Any],
    ffmpeg_bin: str,
) -> HlsSession:
    store = _sessions(hass)
    for sid, existing in list(store.items()):
        if existing.entry_id == runtime.entry.entry_id:
            existing.cleanup()
            store.pop(sid, None)
    session = HlsSession(runtime, file_info, ffmpeg_bin)
    session._hass = hass
    store[session.id] = session
    session.start()
    return session


def drop_hls_session(hass: HomeAssistant, session_id: str) -> None:
    session = _sessions(hass).pop(session_id, None)
    if session is not None:
        session.cleanup()


def stop_playback_for_entry(
    hass: HomeAssistant,
    entry_id: str,
    keep_day: str | None = None,
) -> None:
    """Release the camera when the user leaves the player or opens another day."""
    store = _sessions(hass)
    stopped = False
    for sid, existing in list(store.items()):
        if existing.entry_id != entry_id:
            continue
        playing_day = str(existing.file_info.get("BeginTime") or "")[:10]
        if keep_day and playing_day == keep_day:
            continue
        existing.cleanup()
        store.pop(sid, None)
        stopped = True
    runtime = hass.data.get(DOMAIN, {}).get("entries", {}).get(entry_id)
    if runtime is None:
        return
    if keep_day and not stopped:
        return
    if runtime._active_stop is not None:
        runtime._active_stop.set()
    runtime.abort()


def get_hls_session(hass: HomeAssistant, session_id: str) -> HlsSession | None:
    return _sessions(hass).get(session_id)
