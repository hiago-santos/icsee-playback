<p align="center">
  <img src="https://raw.githubusercontent.com/hiago-santos/icsee-playback/main/icon.png" alt="iCSee Playback" width="128" height="128">
</p>

# iCSee Playback

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![GitHub release](https://img.shields.io/github/v/release/hiago-santos/icsee-playback?display_name=tag&label=release)](https://github.com/hiago-santos/icsee-playback/releases)

Home Assistant custom integration that lists and plays **SD-card recordings** from [iCSee](https://www.icsee.com/) / Xiongmai cameras (Sofia / DVRIP, TCP port **34567**).

It does **not** replace the live camera entity. It adds a Media Browser source and a small REST API so the Home Assistant UI — or a companion app — can browse videos and snapshots already stored on the camera.

[Português](#português) · [English](#english)

---

## English

### What it does

- Browses the last **7 days** of files on the camera SD card (video + JPEG snapshots).
- Plays clips in **Media Browser** (`media-source://icsee_playback/...`).
- Streams through Home Assistant as **fMP4**:
  - **bridge** — remux only (`ffmpeg -c:v copy`), keeps the camera codec (often HEVC).
  - **transcode** — H.264 fallback for browsers that cannot play HEVC.
- Serves **thumbnails** (first video frame / cached JPEG), capped at 64 MB and 10 days.
- Optional seek by restarting DVRIP from `BeginTime + t` seconds (`t=` query).

The camera stays the DVR. Home Assistant is a DVRIP → HTTP bridge; it does not re-record the stream to disk.

### Requirements

- Home Assistant 2024.8 or newer
- [FFmpeg](https://www.home-assistant.io/integrations/ffmpeg/) (HA OS / Supervised already include it)
- Camera reachable on the LAN at DVRIP port `34567` (default iCSee / XMEye)
- Username and password of the camera (often `admin` / `admin`)

Tested with hardware like `XM530V200_X2C-WQ_8M` (model **X2C-WQ**). Other Sofia/Xiongmai firmwares that speak DVRIP `OPFileQuery` (1440) and `OPPlayBack` (1424/1420) should work.

### HACS install

Add it as a **custom repository** (it is not in the default HACS store yet). HACS installs **GitHub Releases** (`0.3.x`), not a commit hash.

1. HACS → Integrations → ⋮ → **Custom repositories**
2. URL: `https://github.com/hiago-santos/icsee-playback`
3. Category: **Integration**
4. Download **iCSee Playback** (latest release) and restart Home Assistant
5. Settings → Devices & services → **Add integration** → **iCSee Playback**

### Manual install

Copy `custom_components/icsee_playback` into `<config>/custom_components/icsee_playback`, restart, then add the integration from the UI.

### Configuration

| Field | Default | Notes |
| --- | --- | --- |
| Name | iCSee Camera | Label in Media Browser |
| IP address | — | Camera LAN address |
| DVRIP port | `34567` | Not the HTTP/ONVIF port |
| Username / password | `admin` | Camera credentials |
| Channel | `0` | First camera / first channel |

Add one config entry per camera.

### Media Browser

**Media** → **iCSee Playback** → camera → **Videos** or **Photos** → day → clip.

### REST API

Bearer token (same as the HA REST API) is required to **list**. Playback URLs are HMAC-signed so a `<video>` tag or ExoPlayer can open them without a Bearer header.

| Method | Path | Auth |
| --- | --- | --- |
| `GET` | `/api/icsee_playback/cameras` | Bearer |
| `GET` | `/api/icsee_playback/{entry_id}/clips?start=YYYY-MM-DD&end=YYYY-MM-DD&kind=all\|video\|photo` | Bearer |
| `POST` | `/api/icsee_playback/{entry_id}/stop` | Bearer |
| `GET` | `/api/icsee_playback/{entry_id}/play?filename=&start=&end=&exp=&sig=` | HMAC (`mode=bridge` optional, `t=` seek seconds) |
| `GET` | `/api/icsee_playback/{entry_id}/thumb?filename=&start=&end=&exp=&sig=` | HMAC |

Clip JSON includes `play_path` (transcode), `bridge_path` (copy), `thumb_path`, `kind` (`video` / `photo`), times, duration and size.

### Limits

- One DVRIP download per camera at a time (the camera firmware is single-session).
- Thumbnail cache lives under `.storage/icsee_playback_thumbs` (max 64 MB, 10 days).
- File list is cached for 5 minutes.

### Debugging

Enable logs in `configuration.yaml`:

```yaml
logger:
  default: info
  logs:
    custom_components.icsee_playback: debug
```

---

## Português

Integração custom do Home Assistant para **listar e reproduzir gravações do cartão SD** de câmeras iCSee / Xiongmai (protocolo Sofia / DVRIP, porta **34567**).

Não substitui a câmera ao vivo. O Home Assistant só faz a ponte DVRIP → HTTP: o cartão continua na câmera.

### Instalar pelo HACS

Ainda não está no catálogo padrão do HACS: cadastre o **repositório personalizado**. As atualizações usam os **GitHub Releases** (`0.3.x`), não o hash do commit.

1. HACS → Integrações → ⋮ → **Repositórios personalizados**
2. URL: `https://github.com/hiago-santos/icsee-playback`
3. Categoria: **Integration**
4. Baixe **iCSee Playback** (último release) e reinicie o Home Assistant
5. Configurações → Dispositivos e serviços → **Adicionar integração** → **iCSee Playback**

Informe IP, porta `34567`, usuário e senha da câmera. Canal `0` é o primeiro.

No **Navegador de mídia** as gravações aparecem em iCSee Playback → câmera → Vídeos / Fotos.

### API

Quem lista usa o token Bearer do HA. Quem toca usa URL assinada (`play_path` / `bridge_path` / `thumb_path`) — o player não precisa enviar o token.

### Licença

MIT. Issues e PRs: [github.com/hiago-santos/icsee-playback](https://github.com/hiago-santos/icsee-playback).
