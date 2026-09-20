import json
import hashlib
import socket
import struct
from datetime import datetime, timedelta


OK_RET = {100, 110, 111}

CODEC_BY_MEDIA = {
    1: "mpeg4",
    2: "h264",
    3: "hevc",
}


def sofia_hash(password):
    md5 = hashlib.md5(password.encode("utf-8")).digest()
    chars = (
        "0123456789"
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        "abcdefghijklmnopqrstuvwxyz"
    )
    return "".join(chars[(a + b) % 62] for a, b in zip(md5[::2], md5[1::2]))


def parse_camera_time(value):
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")


def format_camera_time(value):
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _step_camera_time(raw: str, seconds: int) -> str | None:
    try:
        return format_camera_time(parse_camera_time(raw) + timedelta(seconds=seconds))
    except Exception:  # noqa: BLE001
        return None


def parse_file_length_kb(value):
    try:
        if isinstance(value, str) and value.lower().startswith("0x"):
            return int(value, 16)
        return int(value)
    except (TypeError, ValueError):
        return 0


class FrameDemuxer:
    """Extrai o elementary stream H.264/H.265 de pacotes DVRIP (0x1FC/0x1FD)."""

    def __init__(self):
        self.buf = bytearray()
        self.codec = None
        self.width = None
        self.height = None
        self.fps = None

    def push(self, data):
        self.buf.extend(data)
        out = bytearray()
        while True:
            frame = self._pop_frame()
            if frame is None:
                break
            out.extend(frame)
        return bytes(out)

    def _pop_frame(self):
        buf = self.buf
        if len(buf) < 8:
            return None

        data_type = struct.unpack(">I", buf[:4])[0]

        if data_type in (0x1FC, 0x1FE):
            if len(buf) < 16:
                return None
            media, fps, width, height, _dt, length = struct.unpack(
                "BBBBII",
                buf[4:16],
            )
            header = 16
            if self.codec is None:
                self.codec = CODEC_BY_MEDIA.get(media, "hevc")
                self.fps = fps
                self.width = width * 8
                self.height = height * 8
        elif data_type == 0x1FD:
            (length,) = struct.unpack("I", buf[4:8])
            header = 8
        elif data_type in (0x1FA, 0x1F9):
            if data_type == 0x1FA:
                _media, _rate, length = struct.unpack("BBH", buf[4:8])
            else:
                _media, _n, length = struct.unpack("BBH", buf[4:8])
            header = 8
            total = header + length
            if len(buf) < total:
                return None
            del buf[:total]
            return b""
        else:
            idx = self._resync()
            if idx is None:
                if len(buf) > 1_000_000:
                    del buf[: len(buf) - 3]
                return None
            del buf[:idx]
            return b""

        total = header + length
        if len(buf) < total:
            return None
        frame = bytes(buf[header:total])
        del buf[:total]
        return _strip_leading_junk(frame)

    def _resync(self):
        marker = b"\x00\x00\x01"
        start = 1
        while True:
            idx = self.buf.find(marker, start)
            if idx < 0:
                return None
            kind = self.buf[idx + 3] if idx + 3 < len(self.buf) else None
            if kind in (0xFC, 0xFD, 0xFE, 0xFA, 0xF9):
                return idx
            start = idx + 1


def _strip_leading_junk(frame: bytes) -> bytes:
    """Drop padding / leftover DVRIP bytes before the first NAL."""
    if not frame:
        return frame
    if frame.startswith(b"\x00\x00\x01") or frame.startswith(b"\x00\x00\x00\x01"):
        return frame
    start = frame.find(b"\x00\x00\x01")
    if start > 0:
        return frame[start:]
    return frame


def prepare_video_es(data: bytes, codec: str) -> bytes:
    """Make sure ffmpeg sees Annex-B starting at VPS/SPS, not leftover headers."""
    if not data:
        return data
    converted = _length_prefixed_to_annexb(data)
    if converted is not None:
        data = converted
    if codec == "hevc":
        markers = (
            b"\x00\x00\x00\x01\x40",
            b"\x00\x00\x01\x40",
            b"\x00\x00\x00\x01\x42",
            b"\x00\x00\x01\x42",
        )
    else:
        markers = (
            b"\x00\x00\x00\x01\x67",
            b"\x00\x00\x01\x67",
            b"\x00\x00\x00\x01\x27",
            b"\x00\x00\x01\x27",
        )
    best = -1
    for marker in markers:
        idx = data.find(marker)
        if idx >= 0 and (best < 0 or idx < best):
            best = idx
    if best > 0:
        return data[best:]
    if best < 0 and codec == "hevc":
        nal_type = (data[0] >> 1) & 0x3F
        if nal_type in (32, 33, 34, 19, 20, 21):
            return b"\x00\x00\x00\x01" + data
    return data


def _length_prefixed_to_annexb(data: bytes) -> bytes | None:
    if len(data) < 8:
        return None
    if data.startswith((b"\x00\x00\x00\x01", b"\x00\x00\x01")):
        return None
    out = bytearray()
    i = 0
    n = len(data)
    nal_count = 0
    while i + 4 <= n:
        length = int.from_bytes(data[i : i + 4], "big")
        if length < 2 or length > 1_500_000 or i + 4 + length > n:
            return None
        out.extend(b"\x00\x00\x00\x01")
        out.extend(data[i + 4 : i + 4 + length])
        i += 4 + length
        nal_count += 1
        if nal_count > 4000:
            break
    if nal_count < 2 or i < int(n * 0.85):
        return None
    if i < n:
        out.extend(data[i:])
    return bytes(out)


def _annexb_nal_types(data: bytes, codec: str) -> set[int]:
    seen: set[int] = set()
    i = 0
    n = len(data)
    hevc = codec == "hevc"
    while i + 3 < n:
        if data[i : i + 4] == b"\x00\x00\x00\x01":
            start = i + 4
        elif data[i : i + 3] == b"\x00\x00\x01":
            start = i + 3
        else:
            i += 1
            continue
        if start >= n:
            break
        nal = data[start]
        seen.add((nal >> 1) & 0x3F if hevc else nal & 0x1F)
        i = start + 1
    return seen


def annexb_has_params(data: bytes, codec: str) -> bool:
    """True when the buffer includes the parameter sets ffmpeg needs to decode."""
    types = _annexb_nal_types(data, codec)
    if codec == "hevc":
        return 32 in types and 33 in types and 34 in types
    return 7 in types and 8 in types


def annexb_has_keyframe(data: bytes, codec: str) -> bool:
    """True once the Annex-B buffer includes an IDR/CRA (enough for one JPEG)."""
    types = _annexb_nal_types(data, codec)
    if codec == "hevc":
        return bool(types & {19, 20, 21})
    return 5 in types


class DVRIP:
    def __init__(self, ip, port=34567, timeout=15):
        self.ip = ip
        self.port = port
        self.timeout = timeout
        self.sock = None
        self.session = 0
        self.packet_count = 0

    def connect(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect((self.ip, self.port))

    def close(self):
        if self.sock:
            try:
                if self.session:
                    self.send_packet(1002, {"Name": "OPLogout"})
            except OSError:
                pass
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def interrupt(self):
        """Unblock recv() from another thread. Skips logout on purpose."""
        sock = self.sock
        self.sock = None
        self.session = 0
        if sock is None:
            return
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass

    def session_hex(self):
        return f"0x{self.session:08X}"

    def recv_exact(self, size):
        sock = self.sock
        if sock is None:
            raise ConnectionError("A conexão com a câmera foi interrompida.")
        data = b""
        while len(data) < size:
            chunk = sock.recv(size - len(data))
            if not chunk:
                raise ConnectionError("A câmera fechou a conexão.")
            data += chunk
        return data

    def _encode_payload(self, payload, include_session=True):
        if payload is None:
            payload = {}
        if include_session and self.session and isinstance(payload, dict):
            payload = dict(payload)
            payload.setdefault("SessionID", self.session_hex())
        return json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")

    def send_packet(self, message_id, payload=None, include_session=True):
        sock = self.sock
        if sock is None:
            raise ConnectionError("A conexão com a câmera foi interrompida.")
        data = self._encode_payload(payload, include_session=include_session)
        header = struct.pack(
            "BB2xII2xHI",
            0xFF,
            0x00,
            self.session,
            self.packet_count,
            message_id,
            len(data) + 2,
        )
        sock.sendall(header + data + b"\x0a\x00")
        self.packet_count += 1

    def recv_packet(self):
        header = self.recv_exact(20)
        (
            _head,
            _version,
            session,
            sequence,
            response_id,
            data_length,
        ) = struct.unpack("BB2xII2xHI", header)
        payload = self.recv_exact(data_length) if data_length else b""
        self.session = session
        return response_id, sequence, payload

    def send_command(self, message_id, payload=None, include_session=True):
        self.send_packet(message_id, payload, include_session=include_session)
        _cmd, _seq, payload = self.recv_packet()
        if payload.endswith(b"\x0a\x00"):
            payload = payload[:-2]
        return payload.rstrip(b"\x00")

    def parse_json(self, raw):
        text = raw.decode("utf-8", errors="replace").strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            start = text.find("{")
            end = text.rfind("}")
            if start >= 0 and end > start:
                try:
                    return json.loads(text[start : end + 1])
                except json.JSONDecodeError:
                    return None
            return None

    def login(self, username, password, login_type="DVRIP-Web"):
        payload = {
            "EncryptType": "MD5",
            "LoginType": login_type,
            "PassWord": sofia_hash(password),
            "UserName": username,
        }
        data = self.parse_json(
            self.send_command(1000, payload, include_session=False)
        )
        if not data or data.get("Ret") not in OK_RET:
            return False
        session_id = data.get("SessionID")
        if session_id:
            self.session = int(session_id, 16)
        return True

    def get_named(self, message_id, name):
        raw = self.send_command(message_id, {"Name": name})
        return self.parse_json(raw)

    def query_files(self, start_time, end_time, channel=0, file_type="h264"):
        collected = []
        seen = set()
        cursor = start_time
        window_end = end_time
        origin_start = start_time

        for _ in range(48):
            payload = {
                "Name": "OPFileQuery",
                "OPFileQuery": {
                    "BeginTime": cursor,
                    "Channel": channel,
                    "DriverTypeMask": "0x0000FFFF",
                    "EndTime": window_end,
                    "Event": "*",
                    "StreamType": "0x00000000",
                    "Type": file_type,
                },
            }
            data = self.parse_json(self.send_command(1440, payload))
            if data is None:
                raise RuntimeError("OPFileQuery não retornou JSON")

            ret = data.get("Ret")
            if ret in (109, 119):
                break
            if ret not in OK_RET:
                raise RuntimeError(f"OPFileQuery recusado (Ret={ret})")

            batch = data.get("OPFileQuery") or []
            new_items = 0
            for item in batch:
                key = (
                    item.get("FileName"),
                    item.get("BeginTime"),
                    item.get("EndTime"),
                )
                if key in seen:
                    continue
                seen.add(key)
                collected.append(item)
                new_items += 1

            if len(batch) < 64 or not batch or new_items == 0:
                break

            first_b = str(batch[0].get("BeginTime") or "")
            last_b = str(batch[-1].get("BeginTime") or "")
            if last_b > first_b:
                nxt = _step_camera_time(last_b, 1)
                if not nxt or nxt <= cursor or nxt >= window_end:
                    break
                cursor = nxt
            else:
                if last_b <= origin_start:
                    break
                window_end = last_b
                cursor = origin_start

        return collected

    def playback_payload(self, action, file_info):
        parameter = {
            "PlayMode": "ByName",
            "FileName": file_info["FileName"],
            "StreamType": 0,
            "Value": 0,
            "TransMode": "TCP",
        }
        if "Channel" in file_info:
            parameter["Channel"] = file_info["Channel"]
        return {
            "Name": "OPPlayBack",
            "OPPlayBack": {
                "Action": action,
                "Parameter": parameter,
                "StartTime": file_info["BeginTime"],
                "EndTime": file_info["EndTime"],
            },
        }

    def iter_file_stream(self, file_info, timeout=25):
        demuxer = FrameDemuxer()
        claim = self.parse_json(
            self.send_command(1424, self.playback_payload("Claim", file_info))
        )
        if not claim or claim.get("Ret") not in OK_RET:
            raise RuntimeError("A câmera recusou o Claim de playback.")

        self.send_packet(1420, self.playback_payload("DownloadStart", file_info))
        self.sock.settimeout(timeout)
        json_seen = False

        try:
            while True:
                response_id, _seq, payload = self.recv_packet()
                if response_id == 1423 or not payload:
                    break

                stripped = payload[:-2] if payload.endswith(b"\x0a\x00") else payload
                stripped = stripped.rstrip(b"\x00")

                if not json_seen and stripped[:1] in (b"{", b"["):
                    parsed = self.parse_json(stripped)
                    json_seen = True
                    if parsed and parsed.get("Ret") not in OK_RET | {None}:
                        raise RuntimeError(
                            f"DownloadStart recusado (Ret={parsed.get('Ret')})."
                        )
                    continue

                es = demuxer.push(payload)
                if es:
                    yield es, demuxer
        finally:
            try:
                self.send_packet(
                    1420,
                    self.playback_payload("DownloadStop", file_info),
                )
            except (OSError, ConnectionError):
                pass

    def iter_raw_download(self, file_info, timeout=12):
        """Download file bytes without H.264/HEVC demux (snapshots)."""
        claim = self.parse_json(
            self.send_command(1424, self.playback_payload("Claim", file_info))
        )
        if not claim or claim.get("Ret") not in OK_RET:
            raise RuntimeError("A câmera recusou o Claim da foto.")

        self.send_packet(1420, self.playback_payload("DownloadStart", file_info))
        self.sock.settimeout(timeout)
        json_seen = False
        try:
            while True:
                response_id, _seq, payload = self.recv_packet()
                if response_id == 1423 or not payload:
                    break
                stripped = payload[:-2] if payload.endswith(b"\x0a\x00") else payload
                stripped = stripped.rstrip(b"\x00")
                if not json_seen and stripped[:1] in (b"{", b"["):
                    parsed = self.parse_json(stripped)
                    json_seen = True
                    if parsed and parsed.get("Ret") not in OK_RET | {None}:
                        raise RuntimeError(
                            f"DownloadStart recusado (Ret={parsed.get('Ret')})."
                        )
                    continue
                yield payload
        finally:
            try:
                self.send_packet(
                    1420,
                    self.playback_payload("DownloadStop", file_info),
                )
            except (OSError, ConnectionError):
                pass
