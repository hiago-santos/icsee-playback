"""Constants for iCSee Playback."""

DOMAIN = "icsee_playback"

CONF_CHANNEL = "channel"

DEFAULT_PORT = 34567
DEFAULT_USERNAME = "admin"
DEFAULT_CHANNEL = 0
DEFAULT_NAME = "iCSee Camera"

BROWSE_DAYS = 7
CACHE_TTL = 300
THUMB_WORKERS = 3
# Playback sem ninguém lendo por tanto tempo é considerado abandonado: a câmera
# volta a servir miniaturas em vez de responder 409 para sempre.
PLAY_STALE_SECONDS = 25
HLS_SEGMENT_SEC = 2
HLS_IDLE_SEC = 30
