"""
learn.py
Learn Mode — automatically detect two-tone page frequencies and durations
by listening to actual dispatched tones.

The user puts the system in Learn Mode, the dispatcher sends the page
(ideally 3 times), and the system extracts exact tone frequencies,
durations, and magnitudes.  The learned values are then used to
auto-populate a new paging sequence.

Architecture:
  LearnSession subscribes to the decoder's AudioStreamBus for raw PCM,
  runs FFT to find the dominant frequency each chunk, and drives a
  state machine:

    LISTENING → TONE_A → TONE_B → CAPTURED  (per sample)

  After 3 samples, averages are computed and returned.
"""

import time
import queue
import logging
import threading

import numpy as np

logger = logging.getLogger(__name__)

# ── Learn mode constants ────────────────────────────────────────────────────

TONE_MIN_HZ = 200          # ignore frequencies below this
TONE_MAX_HZ = 3500         # ignore frequencies above this
TONE_MIN_MAGNITUDE = 0.02  # minimum FFT magnitude to consider a tone present
TONE_STABLE_TIME = 0.15    # seconds a frequency must be stable to confirm a tone
TONE_FREQ_TOLERANCE = 15.0 # Hz — max frequency drift within a single tone
SILENCE_TIME = 0.30        # seconds of silence to confirm tone ended
MIN_TONE_DURATION = 0.3    # minimum tone duration to accept (reject transients)
MAX_SAMPLES = 3            # number of samples to collect

# States
IDLE = "idle"
LISTENING = "listening"
TONE_A = "tone_a"
TONE_B = "tone_b"
CAPTURED = "captured"
COMPLETE = "complete"


class LearnSample:
    """One captured two-tone sample."""
    def __init__(self):
        self.tone_a_freq: float = 0.0
        self.tone_a_duration: float = 0.0
        self.tone_a_magnitude: float = 0.0
        self.tone_b_freq: float = 0.0
        self.tone_b_duration: float = 0.0
        self.tone_b_magnitude: float = 0.0

    def to_dict(self) -> dict:
        return {
            "tone_a_freq": round(self.tone_a_freq, 1),
            "tone_a_duration": round(self.tone_a_duration, 2),
            "tone_a_magnitude": round(self.tone_a_magnitude, 5),
            "tone_b_freq": round(self.tone_b_freq, 1),
            "tone_b_duration": round(self.tone_b_duration, 2),
            "tone_b_magnitude": round(self.tone_b_magnitude, 5),
        }


class LearnSession:
    """Manages a learn-mode session.

    Usage:
        session = LearnSession(stream_bus)
        session.start()
        # ... wait for samples to be captured ...
        result = session.get_result()
        session.stop()
    """

    def __init__(self, stream_bus):
        self._stream_bus = stream_bus
        self._sub_q: queue.Queue | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

        # Session state
        self._state: str = IDLE
        self._samples: list[LearnSample] = []
        self._current_sample: LearnSample | None = None

        # Per-tone tracking
        self._tone_freq_accum: list[float] = []  # frequency readings for current tone
        self._tone_mag_accum: list[float] = []    # magnitude readings for current tone
        self._tone_start: float = 0.0
        self._silence_start: float | None = None
        self._stable_freq: float = 0.0

        # Live status for the UI
        self._live_freq: float = 0.0
        self._live_mag: float = 0.0
        self._status_message: str = "Waiting to start..."

    @property
    def state(self) -> str:
        return self._state

    @property
    def sample_count(self) -> int:
        return len(self._samples)

    def start(self):
        """Begin learning — subscribe to audio bus and start analysis thread."""
        if self._thread and self._thread.is_alive():
            return

        self._stop_event.clear()
        self._samples = []
        self._state = LISTENING
        self._status_message = "Listening... have dispatch send the two-tone page."
        self._sub_q = self._stream_bus.subscribe()

        self._thread = threading.Thread(target=self._run, daemon=True, name="learn-mode")
        self._thread.start()
        logger.info("Learn mode started")

    def stop(self):
        """Stop learning and clean up."""
        self._stop_event.set()
        if self._sub_q:
            self._stream_bus.unsubscribe(self._sub_q)
            self._sub_q = None
        if self._thread:
            self._thread.join(timeout=3)
            self._thread = None
        self._state = IDLE
        self._status_message = "Stopped."
        logger.info("Learn mode stopped")

    def get_status(self) -> dict:
        """Return current status for the UI."""
        return {
            "state": self._state,
            "sample_count": len(self._samples),
            "max_samples": MAX_SAMPLES,
            "samples": [s.to_dict() for s in self._samples],
            "live_freq": round(self._live_freq, 1),
            "live_mag": round(self._live_mag, 5),
            "message": self._status_message,
            "current_tone": self._get_current_tone_info(),
        }

    def get_result(self) -> dict | None:
        """Compute averaged result from collected samples."""
        if not self._samples:
            return None

        n = len(self._samples)
        avg_a_freq = sum(s.tone_a_freq for s in self._samples) / n
        avg_a_dur = sum(s.tone_a_duration for s in self._samples) / n
        avg_a_mag = sum(s.tone_a_magnitude for s in self._samples) / n
        avg_b_freq = sum(s.tone_b_freq for s in self._samples) / n
        avg_b_dur = sum(s.tone_b_duration for s in self._samples) / n
        avg_b_mag = sum(s.tone_b_magnitude for s in self._samples) / n

        # Suggest threshold at ~25% of the weaker tone's average magnitude
        weaker_mag = min(avg_a_mag, avg_b_mag)
        suggested_threshold = round(weaker_mag * 0.25, 4)
        # Clamp to reasonable range
        suggested_threshold = max(0.005, min(0.5, suggested_threshold))

        return {
            "tone1_hz": round(avg_a_freq, 1),
            "tone1_duration": round(avg_a_dur, 1),
            "tone2_hz": round(avg_b_freq, 1),
            "tone2_duration": round(avg_b_dur, 1),
            "suggested_threshold": suggested_threshold,
            "avg_magnitude_a": round(avg_a_mag, 5),
            "avg_magnitude_b": round(avg_b_mag, 5),
            "samples": [s.to_dict() for s in self._samples],
            "sample_count": n,
        }

    def _get_current_tone_info(self) -> dict | None:
        """Return info about the tone currently being tracked."""
        if self._state == TONE_A and self._tone_start > 0:
            return {
                "tone": "A",
                "freq": round(self._stable_freq, 1),
                "elapsed": round(time.time() - self._tone_start, 2),
            }
        elif self._state == TONE_B and self._tone_start > 0:
            return {
                "tone": "B",
                "freq": round(self._stable_freq, 1),
                "elapsed": round(time.time() - self._tone_start, 2),
            }
        return None

    # ── Audio processing thread ──────────────────────────────────────────────

    def _run(self):
        """Main analysis loop — runs in a background thread."""
        sample_rate = self._stream_bus.sample_rate

        while not self._stop_event.is_set():
            try:
                raw = self._sub_q.get(timeout=0.5)
            except queue.Empty:
                continue

            # Convert 16-bit PCM back to float32 for FFT
            samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32767.0

            # Find the dominant frequency via FFT
            freq, mag = self._find_peak_frequency(samples, sample_rate)
            self._live_freq = freq
            self._live_mag = mag

            now = time.time()
            tone_present = mag >= TONE_MIN_MAGNITUDE and TONE_MIN_HZ <= freq <= TONE_MAX_HZ

            self._process_state(freq, mag, tone_present, now)

            if self._state == COMPLETE:
                break

        logger.info("Learn mode analysis thread finished (%d samples)", len(self._samples))

    def _find_peak_frequency(self, samples: np.ndarray, sample_rate: int) -> tuple[float, float]:
        """Run FFT and return (peak_frequency_hz, normalized_magnitude)."""
        n = len(samples)
        if n == 0:
            return 0.0, 0.0

        fft_data = np.fft.rfft(samples)
        fft_mag = np.abs(fft_data)

        freq_per_bin = sample_rate / n
        min_bin = max(1, int(TONE_MIN_HZ / freq_per_bin))
        max_bin = min(len(fft_mag) - 1, int(TONE_MAX_HZ / freq_per_bin))

        if min_bin >= max_bin:
            return 0.0, 0.0

        search_range = fft_mag[min_bin:max_bin + 1]
        peak_bin = int(np.argmax(search_range)) + min_bin
        peak_mag = float(fft_mag[peak_bin])

        # Normalize
        norm_mag = peak_mag / (n / 2)

        if norm_mag < TONE_MIN_MAGNITUDE:
            return 0.0, 0.0

        peak_freq = peak_bin * freq_per_bin

        # Quadratic interpolation for sub-bin accuracy
        if 1 < peak_bin < len(fft_mag) - 1:
            alpha = float(fft_mag[peak_bin - 1])
            beta = float(fft_mag[peak_bin])
            gamma = float(fft_mag[peak_bin + 1])
            if beta > 0:
                denom = alpha - 2 * beta + gamma
                if abs(denom) > 1e-10:
                    correction = 0.5 * (alpha - gamma) / denom
                    peak_freq = (peak_bin + correction) * freq_per_bin

        return peak_freq, norm_mag

    def _process_state(self, freq: float, mag: float, tone_present: bool, now: float):
        """Drive the learn state machine."""

        if self._state == LISTENING:
            if tone_present:
                # A tone appeared — start tracking Tone A
                self._current_sample = LearnSample()
                self._tone_freq_accum = [freq]
                self._tone_mag_accum = [mag]
                self._tone_start = now
                self._stable_freq = freq
                self._silence_start = None
                self._state = TONE_A
                self._status_message = f"Tone A detected ({freq:.0f} Hz)... listening..."
                logger.info("Learn: Tone A started at %.1f Hz", freq)

        elif self._state == TONE_A:
            if tone_present:
                self._silence_start = None

                if abs(freq - self._stable_freq) <= TONE_FREQ_TOLERANCE:
                    # Same tone — accumulate readings
                    self._tone_freq_accum.append(freq)
                    self._tone_mag_accum.append(mag)
                    elapsed = now - self._tone_start
                    self._status_message = (
                        f"Tone A: {self._stable_freq:.0f} Hz ({elapsed:.1f}s)..."
                    )
                else:
                    # Frequency shifted — this is the transition to Tone B
                    elapsed_a = now - self._tone_start
                    if elapsed_a >= MIN_TONE_DURATION:
                        # Finalize Tone A
                        self._current_sample.tone_a_freq = float(np.median(self._tone_freq_accum))
                        self._current_sample.tone_a_duration = elapsed_a
                        self._current_sample.tone_a_magnitude = float(np.mean(self._tone_mag_accum))

                        # Start tracking Tone B
                        self._tone_freq_accum = [freq]
                        self._tone_mag_accum = [mag]
                        self._tone_start = now
                        self._stable_freq = freq
                        self._state = TONE_B
                        self._status_message = (
                            f"Tone B detected ({freq:.0f} Hz)... listening..."
                        )
                        logger.info(
                            "Learn: Tone A = %.1f Hz, %.2fs → Tone B started at %.1f Hz",
                            self._current_sample.tone_a_freq,
                            self._current_sample.tone_a_duration,
                            freq,
                        )
                    else:
                        # Tone A was too short — probably a transient, reset
                        self._state = LISTENING
                        self._status_message = "Transient detected, still listening..."
                        logger.debug("Learn: Tone A too short (%.2fs), resetting", elapsed_a)
            else:
                # Silence during Tone A
                if self._silence_start is None:
                    self._silence_start = now
                elif (now - self._silence_start) >= SILENCE_TIME:
                    # Tone A ended without a Tone B — not a two-tone page
                    self._state = LISTENING
                    self._silence_start = None
                    self._status_message = "Tone ended without second tone. Still listening..."
                    logger.debug("Learn: Tone A ended without Tone B, resetting")

        elif self._state == TONE_B:
            if tone_present:
                self._silence_start = None

                if abs(freq - self._stable_freq) <= TONE_FREQ_TOLERANCE:
                    # Same tone — accumulate
                    self._tone_freq_accum.append(freq)
                    self._tone_mag_accum.append(mag)
                    elapsed = now - self._tone_start
                    self._status_message = (
                        f"Tone B: {self._stable_freq:.0f} Hz ({elapsed:.1f}s)..."
                    )
                else:
                    # Frequency shifted again — unusual, but finalize if Tone B was long enough
                    self._try_finalize_tone_b(now)
            else:
                # Silence — Tone B may have ended
                if self._silence_start is None:
                    self._silence_start = now
                elif (now - self._silence_start) >= SILENCE_TIME:
                    self._try_finalize_tone_b(now)

    def _try_finalize_tone_b(self, now: float):
        """Finalize Tone B and capture the sample."""
        elapsed_b = now - self._tone_start
        if elapsed_b >= MIN_TONE_DURATION and self._current_sample:
            self._current_sample.tone_b_freq = float(np.median(self._tone_freq_accum))
            self._current_sample.tone_b_duration = elapsed_b
            self._current_sample.tone_b_magnitude = float(np.mean(self._tone_mag_accum))

            self._samples.append(self._current_sample)
            sample_num = len(self._samples)

            logger.info(
                "Learn: Sample %d captured — A=%.1f Hz/%.2fs, B=%.1f Hz/%.2fs",
                sample_num,
                self._current_sample.tone_a_freq,
                self._current_sample.tone_a_duration,
                self._current_sample.tone_b_freq,
                self._current_sample.tone_b_duration,
            )

            if sample_num >= MAX_SAMPLES:
                self._state = COMPLETE
                self._status_message = f"All {MAX_SAMPLES} samples captured!"
            else:
                self._state = LISTENING
                self._status_message = (
                    f"Sample {sample_num} of {MAX_SAMPLES} captured. "
                    f"Send the page again ({MAX_SAMPLES - sample_num} remaining)."
                )

            self._current_sample = None
            self._silence_start = None
        else:
            # Tone B too short — reset
            self._state = LISTENING
            self._silence_start = None
            self._status_message = "Tone B too short. Still listening..."
            logger.debug("Learn: Tone B too short (%.2fs), resetting", elapsed_b)
