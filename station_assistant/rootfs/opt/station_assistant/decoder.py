"""
decoder.py
Audio capture and two-tone detection engine.

Architecture:
  AudioCapture  — PyAudio callback pushes raw chunks into a queue
  SequenceMachine — per-sequence state machine tracking tone progression
  DecoderService  — orchestrates everything, emits SocketIO events, fires HA events
"""

import re
import time
import queue
import logging
import struct
import subprocess
import threading
from datetime import datetime, timezone

import numpy as np

try:
    import pyaudio
    PYAUDIO_AVAILABLE = True
except ImportError:
    PYAUDIO_AVAILABLE = False

from goertzel import goertzel_magnitude, rms_level, batch_goertzel
from config_manager import get_options, get_sequences
from ha_client import fire_two_tone_event, fire_health_event, push_decoder_sensor, push_watchdog_sensor
from detection_log import log_detection, purge_old_records
from constants import (
    CONFIRM_RATIO, INTER_TONE_TIMEOUT, DROPOUT_TOLERANCE,
    AUDIO_QUEUE_MAXSIZE, STREAM_QUEUE_MAXSIZE, GAIN_MAX,
    RMS_SILENCE_THRESHOLD, FFT_MIN_HZ, FFT_MAX_HZ, FFT_MIN_MAGNITUDE,
    PURGE_INTERVAL_SECONDS, WATCHDOG_INTERVAL_SECONDS,
)

logger = logging.getLogger(__name__)

# ── State machine states ───────────────────────────────────────────────────────

IDLE            = "idle"
TONE1_DETECTING = "tone1_detecting"
TONE1_CONFIRMED = "tone1_confirmed"
TONE2_DETECTING = "tone2_detecting"
COOLDOWN        = "cooldown"


# ── Live audio streaming bus ─────────────────────────────────────────────────

class AudioStreamBus:
    """Pub/sub for raw PCM audio chunks — supports multiple stream consumers.

    The decoder publishes 16-bit PCM bytes each chunk.  Each subscriber
    (e.g. the /api/audio/live endpoint) gets its own queue and receives a
    copy of every chunk while subscribed.
    """

    def __init__(self):
        self._subscribers: list[queue.Queue] = []
        self._lock = threading.Lock()
        self.sample_rate: int = 44100  # updated by decoder on stream open

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=STREAM_QUEUE_MAXSIZE)
        with self._lock:
            self._subscribers.append(q)
        logger.debug("AudioStreamBus: subscriber added (%d total)", len(self._subscribers))
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass
        logger.debug("AudioStreamBus: subscriber removed (%d remaining)", len(self._subscribers))

    def publish(self, pcm_bytes: bytes) -> None:
        with self._lock:
            dead: list[queue.Queue] = []
            for q in self._subscribers:
                try:
                    q.put_nowait(pcm_bytes)
                except queue.Full:
                    dead.append(q)
            for q in dead:
                self._subscribers.remove(q)

    @property
    def has_subscribers(self) -> bool:
        with self._lock:
            return len(self._subscribers) > 0

    @staticmethod
    def wav_header(sample_rate: int, bits: int = 16, channels: int = 1) -> bytes:
        """Build a WAV header for an infinite-length stream."""
        byte_rate = sample_rate * channels * (bits // 8)
        block_align = channels * (bits // 8)
        # Use 0xFFFFFFFF for unknown/streaming length
        data_size = 0xFFFFFFFF - 44
        file_size = 0xFFFFFFFF - 8
        hdr = struct.pack(
            '<4sI4s'       # RIFF chunk
            '4sIHHIIHH'   # fmt  chunk
            '4sI',         # data chunk header
            b'RIFF', file_size, b'WAVE',
            b'fmt ', 16, 1, channels, sample_rate, byte_rate, block_align, bits,
            b'data', data_size,
        )
        return hdr


class SequenceMachine:
    """
    Per-sequence detection state machine.
    Tracks whether tone1 then tone2 have been heard for the required durations.
    """

    def __init__(self, seq: dict):
        self.seq = seq
        self.state = IDLE
        self.tone1_start: float = 0.0
        self.tone2_start: float = 0.0
        self.inter_tone_start: float = 0.0
        self.cooldown_start: float = 0.0
        self.tone1_drop_start: float | None = None
        self.tone2_drop_start: float | None = None
        self.last_t1_mag: float = 0.0
        self.last_t2_mag: float = 0.0
        self.last_confidence: float = 0.0

    def process(self, t1_mag: float, t2_mag: float, now: float) -> bool:
        """
        Feed current Goertzel magnitudes and current timestamp.
        Returns True if the full sequence was just detected this tick.
        """
        self.last_t1_mag = t1_mag
        self.last_t2_mag = t2_mag
        threshold = self.seq["threshold"]
        confirm_ratio = self.seq.get("confirm_ratio", CONFIRM_RATIO)
        t1_active = t1_mag >= threshold
        t2_active = t2_mag >= threshold

        if self.state == IDLE:
            if t1_active:
                self.state = TONE1_DETECTING
                self.tone1_start = now
                self.tone1_drop_start = None

        elif self.state == TONE1_DETECTING:
            if not t1_active:
                # Allow brief dropouts before resetting — real radio audio
                # has momentary dips from fading and interference.
                if self.tone1_drop_start is None:
                    self.tone1_drop_start = now
                elif (now - self.tone1_drop_start) >= DROPOUT_TOLERANCE:
                    self.state = IDLE
            else:
                self.tone1_drop_start = None
                elapsed = now - self.tone1_start
                required = self.seq["tone1_duration"] * confirm_ratio
                if elapsed >= required:
                    self.state = TONE1_CONFIRMED
                    self.inter_tone_start = now
                    logger.debug("[%s] Tone 1 confirmed (%.2fs)", self.seq["name"], elapsed)

        elif self.state == TONE1_CONFIRMED:
            if t2_active:
                self.state = TONE2_DETECTING
                self.tone2_start = now
                self.tone2_drop_start = None
            elif (now - self.inter_tone_start) > INTER_TONE_TIMEOUT:
                logger.debug("[%s] Inter-tone timeout, resetting", self.seq["name"])
                self.state = IDLE

        elif self.state == TONE2_DETECTING:
            if not t2_active:
                # Allow brief dropouts before resetting — real radio audio
                # has momentary dips from fading and interference.
                if self.tone2_drop_start is None:
                    self.tone2_drop_start = now
                elif (now - self.tone2_drop_start) >= DROPOUT_TOLERANCE:
                    logger.debug("[%s] Tone 2 dropped before confirmation, resetting", self.seq["name"])
                    self.state = IDLE
            else:
                self.tone2_drop_start = None
                elapsed = now - self.tone2_start
                required = self.seq["tone2_duration"] * confirm_ratio
                if elapsed >= required:
                    # ✅ FULL SEQUENCE DETECTED
                    self.last_confidence = self._calculate_confidence(
                        now - self.tone1_start, elapsed
                    )
                    self.state = COOLDOWN
                    self.cooldown_start = now
                    logger.info(
                        "[%s] DETECTED! confidence=%.2f", self.seq["name"], self.last_confidence
                    )
                    return True

        elif self.state == COOLDOWN:
            if (now - self.cooldown_start) >= self.seq["auto_reset_seconds"]:
                self.state = IDLE
                logger.debug("[%s] Cooldown expired, re-armed", self.seq["name"])

        return False

    def _calculate_confidence(self, t1_elapsed: float, t2_elapsed: float) -> float:
        """
        Confidence score based on how close detected durations are to expected.
        Capped at 1.0.
        """
        t1_score = min(t1_elapsed / self.seq["tone1_duration"], 1.0)
        t2_score = min(t2_elapsed / self.seq["tone2_duration"], 1.0)
        return (t1_score + t2_score) / 2.0

    def get_confidence_estimate(self) -> float:
        """Return a live confidence estimate based on current magnitudes."""
        threshold = self.seq["threshold"]
        if threshold == 0:
            return 0.0
        t1_ratio = min(self.last_t1_mag / threshold, 2.0) / 2.0
        t2_ratio = min(self.last_t2_mag / threshold, 2.0) / 2.0
        return (t1_ratio + t2_ratio) / 2.0

    def reset(self):
        self.state = IDLE
        self.tone1_drop_start = None
        self.tone2_drop_start = None

    def update_sequence(self, seq: dict):
        """Hot-reload sequence parameters without losing state."""
        self.seq = seq


# ── Audio device enumeration ───────────────────────────────────────────────────

def _get_alsa_card_names() -> dict:
    """Get audio device names from ALSA or PulseAudio.

    Tries multiple methods to find real hardware names:
    1. arecord -l (ALSA capture devices)
    2. /proc/asound/cards
    3. pactl list sources (PulseAudio)

    Returns a dict mapping card number to friendly name.
    """
    names = {}

    # Method 1: arecord -l
    try:
        result = subprocess.run(
            ["arecord", "-l"],
            capture_output=True, text=True, timeout=3,
        )
        logger.debug("arecord -l exit=%d stdout=%s", result.returncode, result.stdout[:200])
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                m = re.match(r"card\s+(\d+):\s+\S+\s+\[(.+?)\]", line)
                if m:
                    names[int(m.group(1))] = m.group(2)
            if names:
                logger.info("ALSA card names (arecord): %s", names)
                return names
    except Exception as e:
        logger.debug("arecord -l failed: %s", e)

    # Method 2: /proc/asound/cards
    try:
        with open("/proc/asound/cards", "r") as f:
            for line in f:
                line = line.strip()
                if line and line[0].isdigit():
                    parts = line.split(":")
                    card_num = int(line.split()[0])
                    if len(parts) >= 2:
                        names[card_num] = parts[-1].strip()
        if names:
            logger.info("ALSA card names (/proc): %s", names)
            return names
    except Exception as e:
        logger.debug("/proc/asound/cards failed: %s", e)

    # Method 3: pactl list sources short — parse PulseAudio source names
    # Lines like: "1	alsa_input.usb-C-Media...	s16le 1ch 44100Hz	RUNNING"
    try:
        result = subprocess.run(
            ["pactl", "list", "sources", "short"],
            capture_output=True, text=True, timeout=3,
        )
        logger.debug("pactl sources exit=%d stdout=%s", result.returncode, result.stdout[:300])
        if result.returncode == 0:
            idx = 0
            for line in result.stdout.splitlines():
                parts = line.split("\t")
                if len(parts) >= 2:
                    source_name = parts[1]
                    # Extract a readable name from the PulseAudio source name
                    # e.g. "alsa_input.usb-C-Media_Electronics_Inc._USB_Audio_Device-00.mono-fallback"
                    if "alsa_input" in source_name:
                        # Strip prefix and clean up
                        friendly = source_name.replace("alsa_input.", "")
                        friendly = friendly.replace("_", " ").replace("-", " ")
                        # Remove trailing "mono fallback" etc.
                        for suffix in ("mono fallback", "analog stereo", "analog mono"):
                            friendly = friendly.replace(suffix, "").strip()
                        friendly = re.sub(r"\s+", " ", friendly).strip(". ")
                        if friendly:
                            names[idx] = friendly
                            idx += 1
            if names:
                logger.info("PulseAudio source names: %s", names)
                return names
    except Exception as e:
        logger.debug("pactl failed: %s", e)

    logger.warning("Could not determine hardware audio device names")
    return names


def list_audio_devices() -> list:
    """
    Return a list of available input audio devices.
    Each item: {"index": int, "name": str, "channels": int, "sample_rate": int}

    Attempts to enrich PulseAudio device names with real hardware names.
    If name enrichment fails, returns the original PyAudio names.
    """
    if not PYAUDIO_AVAILABLE:
        return []
    try:
        pa = pyaudio.PyAudio()

        # Try to get hardware names, but don't let it block audio startup
        alsa_names = {}
        try:
            alsa_names = _get_alsa_card_names()
        except Exception as e:
            logger.debug("Device name enrichment skipped: %s", e)

        devices = []
        for i in range(pa.get_device_count()):
            info = pa.get_device_info_by_index(i)
            if info.get("maxInputChannels", 0) > 0:
                name = info["name"]
                if name.lower() in ("pulse", "default") and alsa_names:
                    for card_num, card_name in sorted(alsa_names.items()):
                        if card_name:
                            name = f"{card_name} (hw:{card_num},0)"
                            break
                elif "hw:" in name.lower() and alsa_names:
                    m = re.search(r"hw:(\d+)", name)
                    if m:
                        card_num = int(m.group(1))
                        if card_num in alsa_names:
                            name = f"{alsa_names[card_num]} - {name}"

                devices.append({
                    "index": i,
                    "name": name,
                    "channels": info["maxInputChannels"],
                    "sample_rate": int(info["defaultSampleRate"]),
                })
        pa.terminate()
        return devices
    except Exception as e:
        logger.error("Failed to enumerate audio devices: %s", e)
        return []


# ── Main decoder service ───────────────────────────────────────────────────────

class DecoderService:
    """
    Runs in a background thread.
    Captures audio → runs Goertzel on all configured frequencies →
    drives per-sequence state machines → fires HA events on detection.
    """

    def __init__(self, sse_bus, on_detection_callback=None):
        self.sse_bus = sse_bus
        self.stream_bus = AudioStreamBus()
        self._on_detection_callback = on_detection_callback
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._running = False
        self.machines: dict[str, SequenceMachine] = {}
        self._audio_error: str = ""
        self._cached_devices: list = []
        self._input_gain: float = 0.5
        self._last_healthy: float | None = None
        self._started_at: float | None = None
        self._restart_backoff: float = 5.0
        self._intentional_stop: bool = False
        self._total_detections: int = 0
        self._last_rms: float = 0.0
        self._last_rms_post: float = 0.0
        self._last_peak_freq: float = 0.0
        self._last_peak_mag: float = 0.0

    @property
    def input_gain(self) -> float:
        """Current input gain as a float 0.0-GAIN_MAX."""
        return self._input_gain

    @input_gain.setter
    def input_gain(self, value: float):
        self._input_gain = max(0.0, min(GAIN_MAX, float(value)))

    def start(self):
        if self._running:
            return
        self._stop_event.clear()
        self._intentional_stop = False
        self._started_at = time.time()
        self._thread = threading.Thread(target=self._run, daemon=True, name="decoder")
        self._thread.start()
        self._running = True
        logger.info("Decoder service started")

    def stop(self):
        self._intentional_stop = True
        self._stop_event.set()
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("Decoder service stopped")
        fire_health_event("stopped", "Decoder service stopped")

    def restart(self):
        self.stop()
        time.sleep(0.5)
        self.start()

    @property
    def audio_error(self) -> str:
        return self._audio_error

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def cached_devices(self) -> list:
        return self._cached_devices

    @property
    def last_healthy(self) -> float | None:
        """Epoch timestamp of the last successfully processed audio chunk."""
        return self._last_healthy

    @property
    def uptime(self) -> float:
        """Seconds since the decoder was started, or 0 if not running."""
        if self._started_at is None:
            return 0.0
        return time.time() - self._started_at

    @property
    def total_detections(self) -> int:
        """Total number of detections since the decoder was started."""
        return self._total_detections

    # ── Internal ───────────────────────────────────────────────────────────────

    def _run(self):
        opts = get_options()
        sample_rate = opts["sample_rate"]
        chunk_size = opts["chunk_size"]
        device_index = opts["audio_device_index"]
        self._input_gain = opts.get("input_gain", 5) / 100.0 * 20.0  # 0-100 → 0.0x-20.0x (default 5 = 1.0x unity gain)
        if device_index < 0:
            device_index = None  # PyAudio uses system default

        if not PYAUDIO_AVAILABLE:
            self._audio_error = "PyAudio not available — audio processing disabled"
            logger.error(self._audio_error)
            self._emit_status()
            return

        # Cache device list BEFORE opening the stream — a second PyAudio
        # instance will deadlock on Linux/ALSA once the stream is active.
        self._cached_devices = list_audio_devices()
        logger.info("Audio devices found: %d input(s)", len(self._cached_devices))

        # Auto-detect native sample rate from the target device if available.
        # Avoids PulseAudio resampling issues with USB sound cards.
        if self._cached_devices:
            target = None
            if device_index is not None:
                target = next((d for d in self._cached_devices if d["index"] == device_index), None)
            if target is None:
                target = self._cached_devices[0]
            native_rate = target.get("sample_rate", sample_rate)
            if native_rate != sample_rate:
                logger.info("Overriding sample_rate %d → %d (device native rate)", sample_rate, native_rate)
                sample_rate = native_rate

        audio_queue: queue.Queue = queue.Queue(maxsize=AUDIO_QUEUE_MAXSIZE)

        def audio_callback(in_data, frame_count, time_info, status):
            if not audio_queue.full():
                audio_queue.put_nowait(in_data)
            return (None, pyaudio.paContinue)

        pa = pyaudio.PyAudio()
        stream = None
        try:
            stream = pa.open(
                format=pyaudio.paFloat32,
                channels=1,
                rate=sample_rate,
                input=True,
                input_device_index=device_index,
                frames_per_buffer=chunk_size,
                stream_callback=audio_callback,
            )
            stream.start_stream()
            self._audio_error = ""
            self._restart_backoff = 5.0  # Reset backoff on successful open
            self.stream_bus.sample_rate = sample_rate
            logger.info("Audio stream opened: device=%s rate=%d chunk=%d",
                        device_index, sample_rate, chunk_size)

            self._emit_status()
            fire_health_event("started", f"Decoder started: device={device_index} rate={sample_rate}")

            # Run the purge loop periodically alongside audio processing
            last_purge = time.time()
            last_watchdog = time.time()
            PURGE_INTERVAL = PURGE_INTERVAL_SECONDS
            WATCHDOG_INTERVAL = 60  # push heartbeat every 60 seconds

            while not self._stop_event.is_set() and stream.is_active():
                try:
                    raw = audio_queue.get(timeout=0.5)
                except queue.Empty:
                    continue

                samples = np.frombuffer(raw, dtype=np.float32)
                self._process_chunk(samples, sample_rate)

                # Periodic log purge
                now = time.time()
                if now - last_purge > PURGE_INTERVAL:
                    opts = get_options()
                    purge_old_records(opts["log_retention_days"])
                    last_purge = now

                # Watchdog heartbeat → sensor.station_assistant_watchdog
                if now - last_watchdog > WATCHDOG_INTERVAL:
                    push_watchdog_sensor()
                    last_watchdog = now

        except OSError as e:
            self._audio_error = f"Audio device error: {e}"
            logger.error(self._audio_error)
            self._emit_status()
            fire_health_event("audio_lost", self._audio_error)
        except Exception as e:
            self._audio_error = f"Decoder error: {e}"
            logger.exception("Unexpected decoder error")
            self._emit_status()
            fire_health_event("error", self._audio_error)
        finally:
            if stream:
                try:
                    stream.stop_stream()
                    stream.close()
                except Exception:
                    pass
            pa.terminate()
            self._running = False
            # Auto-restart with exponential backoff unless intentionally stopped.
            # Always retry even if no devices found — PulseAudio route may not
            # be ready immediately on container start.
            if not self._intentional_stop and not self._stop_event.is_set():
                if not self._cached_devices:
                    logger.warning(
                        "Watchdog: no audio input devices found — will retry in %.0fs",
                        self._restart_backoff,
                    )
                    self._audio_error = "No audio input devices detected. Retrying..."
                    self._emit_status()
                self._watchdog_restart()

    def _process_chunk(self, samples: np.ndarray, sample_rate: int):
        """Process one audio chunk against all configured sequences."""
        sequences = get_sequences()
        now = time.time()
        self._last_healthy = now

        # Compute pre-gain RMS (true input level from hardware)
        rms = rms_level(samples)
        self._last_rms = float(rms)

        # Publish raw audio to the live stream bus (pre-gain, true signal).
        # Only convert to PCM bytes when there are active subscribers to
        # avoid wasting CPU when nobody is listening.
        if self.stream_bus.has_subscribers:
            pcm16 = np.clip(samples * 32767.0, -32768, 32767).astype(np.int16)
            self.stream_bus.publish(pcm16.tobytes())

        # Apply input gain before Goertzel analysis to boost weak signals
        # for tone detection without affecting the input VU meter reading.
        samples = samples * self._input_gain

        # Compute post-gain RMS (what the decoder actually analyzes)
        rms_post = rms_level(samples)
        self._last_rms_post = float(rms_post)

        # Emit both levels so the UI can show dual VU meters
        self.sse_bus.emit("audio_level", {
            "rms": round(float(rms), 4),
            "rms_post": round(float(rms_post), 4),
        })

        # FFT peak frequency detection — shows the dominant tone in the audio
        # regardless of whether it matches a configured sequence.
        self._emit_peak_frequency(samples, sample_rate, rms)

        # Collect all unique frequencies needed across all enabled sequences
        freq_set = set()
        for seq in sequences:
            if seq.get("enabled", True):
                freq_set.add(seq["tone1_hz"])
                freq_set.add(seq["tone2_hz"])

        if not freq_set:
            return

        # Run batch Goertzel for all needed frequencies in one pass
        magnitudes = batch_goertzel(samples, list(freq_set), sample_rate)

        # Build live magnitude update for the UI monitor
        live_mags = {}
        for seq in sequences:
            if seq.get("enabled", True):
                machine = self.machines.get(seq["id"])
                live_mags[seq["id"]] = {
                    "tone1_mag": round(float(magnitudes.get(seq["tone1_hz"], 0.0)), 5),
                    "tone2_mag": round(float(magnitudes.get(seq["tone2_hz"], 0.0)), 5),
                    "threshold": seq["threshold"],
                    "state": machine.state if machine else IDLE,
                }
        self.sse_bus.emit("goertzel_update", live_mags)

        # Drive each sequence state machine
        for seq in sequences:
            if not seq.get("enabled", True):
                continue

            seq_id = seq["id"]
            if seq_id not in self.machines:
                self.machines[seq_id] = SequenceMachine(seq)
            else:
                self.machines[seq_id].update_sequence(seq)

            machine = self.machines[seq_id]
            t1_mag = magnitudes.get(seq["tone1_hz"], 0.0)
            t2_mag = magnitudes.get(seq["tone2_hz"], 0.0)
            detected = machine.process(t1_mag, t2_mag, now)

            if detected:
                self._on_detection(seq, machine)

        # Remove machines for deleted sequences
        active_ids = {s["id"] for s in sequences}
        for dead_id in list(self.machines.keys()):
            if dead_id not in active_ids:
                del self.machines[dead_id]

    def _on_detection(self, seq: dict, machine: SequenceMachine):
        """Handle a confirmed detection: log it, fire HA event, emit to UI."""
        self._total_detections += 1
        confidence = machine.last_confidence
        detected_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        # Log to SQLite
        log_detection(seq, confidence, detected_at)

        # Fire HA event (triggers the auto-created automation)
        fire_two_tone_event(seq, confidence, detected_at)

        # Notify the UI in real time
        self.sse_bus.emit("detection", {
            "seq_id": seq["id"],
            "name": seq["name"],
            "slug": seq["slug"],
            "tone1_hz": seq["tone1_hz"],
            "tone2_hz": seq["tone2_hz"],
            "confidence": round(confidence, 3),
            "detected_at": detected_at,
        })

        logger.info("Detection fired: %s (confidence=%.2f)", seq["name"], confidence)

        # External callback (e.g. stack manager wiring in main.py)
        if self._on_detection_callback:
            try:
                self._on_detection_callback(seq, confidence, detected_at)
            except Exception as e:
                logger.error("Detection callback error: %s", e)

    def _emit_peak_frequency(self, samples: np.ndarray, sample_rate: int, rms: float):
        """Run FFT to find the dominant frequency in the audio chunk."""
        # Skip if signal is too quiet (avoid reporting noise as a tone)
        if rms < RMS_SILENCE_THRESHOLD:
            self._last_peak_freq = 0.0
            self._last_peak_mag = 0.0
            self.sse_bus.emit("peak_frequency", {"freq": 0, "magnitude": 0})
            return

        n = len(samples)
        fft_data = np.fft.rfft(samples)
        fft_mag = np.abs(fft_data)

        # Ignore DC (bin 0) and frequencies below 100 Hz / above 4000 Hz
        freq_per_bin = sample_rate / n
        min_bin = max(1, int(FFT_MIN_HZ / freq_per_bin))
        max_bin = min(len(fft_mag) - 1, int(FFT_MAX_HZ / freq_per_bin))

        if min_bin >= max_bin:
            self._last_peak_freq = 0.0
            self._last_peak_mag = 0.0
            self.sse_bus.emit("peak_frequency", {"freq": 0, "magnitude": 0})
            return

        search_range = fft_mag[min_bin:max_bin + 1]
        peak_bin = int(np.argmax(search_range)) + min_bin
        peak_mag = float(fft_mag[peak_bin])

        # Normalize magnitude relative to chunk size
        norm_mag = peak_mag / (n / 2)

        # Only report if magnitude is meaningful
        if norm_mag < FFT_MIN_MAGNITUDE:
            self._last_peak_freq = 0.0
            self._last_peak_mag = 0.0
            self.sse_bus.emit("peak_frequency", {"freq": 0, "magnitude": 0})
            return

        peak_freq = peak_bin * freq_per_bin

        # Quadratic interpolation for sub-bin frequency accuracy
        if 1 < peak_bin < len(fft_mag) - 1:
            alpha = float(fft_mag[peak_bin - 1])
            beta = float(fft_mag[peak_bin])
            gamma = float(fft_mag[peak_bin + 1])
            if beta > 0:
                correction = 0.5 * (alpha - gamma) / (alpha - 2 * beta + gamma)
                peak_freq = (peak_bin + correction) * freq_per_bin

        self._last_peak_freq = round(peak_freq, 1)
        self._last_peak_mag = round(norm_mag, 4)
        self.sse_bus.emit("peak_frequency", {
            "freq": self._last_peak_freq,
            "magnitude": self._last_peak_mag,
        })

    def _watchdog_restart(self):
        """Auto-restart the decoder with exponential backoff after a crash."""
        delay = self._restart_backoff
        logger.warning("Watchdog: decoder died, restarting in %.0fs (backoff)", delay)
        # Wait in a loop so we can bail out if stop() is called during the wait
        waited = 0.0
        while waited < delay:
            if self._stop_event.is_set() or self._intentional_stop:
                logger.info("Watchdog: stop requested during backoff, aborting restart")
                return
            time.sleep(min(0.5, delay - waited))
            waited += 0.5
        # Double the backoff for next time, cap at 60s
        self._restart_backoff = min(self._restart_backoff * 2, 60.0)
        logger.info("Watchdog: attempting auto-restart (next backoff=%.0fs)", self._restart_backoff)
        self.start()
        # If we successfully restarted, fire audio_restored
        if self._running:
            fire_health_event("audio_restored", "Decoder auto-restarted by watchdog")

    def _emit_status(self):
        self.sse_bus.emit("decoder_status", {
            "running": self._running,
            "error": self._audio_error,
        })
        # Push persistent HA sensor — created automatically on first call
        status = "running" if self._running else ("error" if self._audio_error else "stopped")
        push_decoder_sensor(status, error=self._audio_error)
