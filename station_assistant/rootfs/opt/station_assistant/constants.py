"""
constants.py
Centralized constants for Station Assistant.
Avoids magic numbers scattered across the codebase.
"""

# ── Application ──────────────────────────────────────────────────────────────

APP_VERSION = "2.1.0"
MAX_SEQUENCES = 5

# ── Decoder / Goertzel ───────────────────────────────────────────────────────

CONFIRM_RATIO = 0.70
INTER_TONE_TIMEOUT = 4.0
DROPOUT_TOLERANCE = 0.25
AUDIO_QUEUE_MAXSIZE = 20
STREAM_QUEUE_MAXSIZE = 200
RMS_SILENCE_THRESHOLD = 0.005
FFT_MIN_HZ = 100
FFT_MAX_HZ = 4000
FFT_MIN_MAGNITUDE = 0.01
GAIN_MAX = 20.0

# ── Timers ───────────────────────────────────────────────────────────────────

PURGE_INTERVAL_SECONDS = 3600
WATCHDOG_INTERVAL_SECONDS = 60
SSE_KEEPALIVE_TIMEOUT = 25

# ── HA Client ────────────────────────────────────────────────────────────────

HA_REQUEST_TIMEOUT = 10
HA_POLL_TIMEOUT = 3
HA_IDLE_GRACE_SECONDS = 5.0
NORMALIZE_SAMPLE_RATE = 44100
COMBINED_ALERT_FILENAME = "_combined_alert.mp3"

# ── Transcoder ───────────────────────────────────────────────────────────────

MP3_BITRATE = "128k"
TRANSCODER_QUEUE_MAXSIZE = 200
