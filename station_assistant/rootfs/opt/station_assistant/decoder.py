"""
decoder.py
Audio capture and two-tone detection engine.

Architecture:
  AudioCapture  — PyAudio callback pushes raw chunks into a queue
  SequenceMachine — per-sequence state machine tracking tone progression
  DecoderService  — orchestrates everything, emits SocketIO events, fires HA events
"""

import time
import queue
import logging
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

logger = logging.getLogger(__name__)

# ── State machine states ───────────────────────────────────────────────────────

IDLE            = "idle"
TONE1_DETECTING = "tone1_detecting"
TONE1_CONFIRMED = "tone1_confirmed"
TONE2_DETECTING = "tone2_detecting"
COOLDOWN        = "cooldown"

# How much of the configured duration must be heard to confirm (70%)
CONFIRM_RATIO = 0.70
# Maximum gap between tone1 ending and tone2 starting (seconds)
INTER_TONE_TIMEOUT = 4.0
# How long a tone may drop below threshold before detection resets (seconds).
# Brief signal dips from fading, interference, or AGC oscillation are ignored
# within this window so users don't need to set thresholds near the noise floor.
DROPOUT_TOLERANCE = 0.25


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

def list_audio_devices() -> list:
    """
    Return a list of available input audio devices.
    Each item: {"index": int, "name": str, "channels": int, "sample_rate": int}
    """
    if not PYAUDIO_AVAILABLE:
        return []
    try:
        pa = pyaudio.PyAudio()
        devices = []
        for i in range(pa.get_device_count()):
            info = pa.get_device_info_by_index(i)
            if info.get("maxInputChannels", 0) > 0:
                devices.append({
                    "index": i,
                    "name": info["name"],
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

    def __init__(self, sse_bus):
        self.sse_bus = sse_bus
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
        self._last_peak_freq: float = 0.0
        self._last_peak_mag: float = 0.0

    @property
    def input_gain(self) -> float:
        """Current input gain as a float 0.0-1.0."""
        return self._input_gain

    @input_gain.setter
    def input_gain(self, value: float):
        self._input_gain = max(0.0, min(1.0, float(value)))

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
        self._input_gain = opts.get("input_gain", 50) / 100.0  # 0-100 → 0.0-1.0
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

        audio_queue: queue.Queue = queue.Queue(maxsize=20)

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
            logger.info("Audio stream opened: device=%s rate=%d chunk=%d",
                        device_index, sample_rate, chunk_size)

            self._emit_status()
            fire_health_event("started", f"Decoder started: device={device_index} rate={sample_rate}")

            # Run the purge loop periodically alongside audio processing
            last_purge = time.time()
            last_watchdog = time.time()
            PURGE_INTERVAL = 3600  # purge old records every hour
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
        samples = samples * self._input_gain
        sequences = get_sequences()
        now = time.time()
        self._last_healthy = now

        # Compute and emit RMS level
        rms = rms_level(samples)
        self._last_rms = float(rms)
        self.sse_bus.emit("audio_level", {"rms": round(float(rms), 4)})

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

    def _emit_peak_frequency(self, samples: np.ndarray, sample_rate: int, rms: float):
        """Run FFT to find the dominant frequency in the audio chunk."""
        # Skip if signal is too quiet (avoid reporting noise as a tone)
        if rms < 0.005:
            self._last_peak_freq = 0.0
            self._last_peak_mag = 0.0
            self.sse_bus.emit("peak_frequency", {"freq": 0, "magnitude": 0})
            return

        n = len(samples)
        fft_data = np.fft.rfft(samples)
        fft_mag = np.abs(fft_data)

        # Ignore DC (bin 0) and frequencies below 100 Hz / above 4000 Hz
        freq_per_bin = sample_rate / n
        min_bin = max(1, int(100 / freq_per_bin))
        max_bin = min(len(fft_mag) - 1, int(4000 / freq_per_bin))

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
        if norm_mag < 0.01:
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
