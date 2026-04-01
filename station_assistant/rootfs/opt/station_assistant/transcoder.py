"""
transcoder.py
Pre-spawnable ffmpeg PCM-to-MP3 transcoder for Line In relay.

Accepts an AudioStreamBus instance (from the decoder) and transcodes
raw PCM audio to an MP3 stream that media players can consume.
Each connected client gets its own subscriber queue so multiple
devices receive a complete copy of the stream.
"""

import logging
import queue
import subprocess
import threading

from constants import MP3_BITRATE, TRANSCODER_QUEUE_MAXSIZE

logger = logging.getLogger(__name__)


class LiveTranscoder:
    """Pre-spawnable ffmpeg PCM-to-MP3 transcoder.

    Call ``start()`` early (e.g. when alert sounds begin) so ffmpeg is
    warmed up and producing MP3 data by the time media players connect.

    Each client subscribes via ``subscribe()`` and receives a dedicated
    queue with a full copy of the MP3 stream.  This avoids consumers
    stealing chunks from each other when multiple devices connect.
    """

    def __init__(self, stream_bus):
        self._stream_bus = stream_bus
        self._proc = None
        self._stop = None
        self._sub_q = None
        self._feed_t = None
        self._read_t = None
        self._lock = threading.Lock()
        self._subscribers: list[queue.Queue] = []
        self._sub_lock = threading.Lock()
        # Ring buffer: keep last ~3 seconds of MP3 data so new subscribers
        # immediately get audio instead of waiting for ffmpeg to produce more.
        # At 128kbps, 3 seconds ≈ 48KB ≈ 12 chunks of 4096 bytes.
        self._backlog: list[bytes] = []
        self._backlog_max = 15  # ~3-4 seconds of MP3 at 128kbps

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def clear_backlog(self):
        """Clear buffered audio. Call before starting Live PA relay."""
        with self._sub_lock:
            self._backlog.clear()

    def subscribe(self) -> queue.Queue:
        """Add a per-client consumer queue, pre-filled with recent audio."""
        q: queue.Queue = queue.Queue(maxsize=TRANSCODER_QUEUE_MAXSIZE)
        with self._sub_lock:
            # Pre-fill with buffered data so the client gets audio immediately
            for chunk in self._backlog:
                try:
                    q.put_nowait(chunk)
                except queue.Full:
                    break
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._sub_lock:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass

    def _publish(self, data: bytes) -> None:
        """Copy MP3 data to every subscriber queue and maintain backlog."""
        with self._sub_lock:
            # Maintain ring buffer of recent MP3 data
            self._backlog.append(data)
            if len(self._backlog) > self._backlog_max:
                self._backlog.pop(0)

            dead = []
            for q in self._subscribers:
                try:
                    q.put_nowait(data)
                except queue.Full:
                    dead.append(q)
            for q in dead:
                self._subscribers.remove(q)

    def start(self):
        """Spawn ffmpeg and begin transcoding.  Idempotent."""
        with self._lock:
            if self.running:
                return
            self._do_start()

    def stop(self):
        """Shut down the transcoder and free resources."""
        with self._lock:
            self._do_stop()

    def _do_start(self):
        sr = self._stream_bus.sample_rate
        self._sub_q = self._stream_bus.subscribe()
        self._stop = threading.Event()

        self._proc = subprocess.Popen(
            [
                "ffmpeg",
                "-hide_banner", "-loglevel", "warning",
                "-f", "s16le", "-ar", str(sr), "-ac", "1",
                "-i", "pipe:0",
                "-b:a", MP3_BITRATE, "-f", "mp3", "pipe:1",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        def stderr_logger():
            try:
                for line in self._proc.stderr:
                    msg = line.decode("utf-8", errors="replace").rstrip()
                    if msg:
                        logger.warning("ffmpeg line-in: %s", msg)
            except Exception:
                pass

        def feeder():
            try:
                while not self._stop.is_set():
                    try:
                        chunk = self._sub_q.get(timeout=1.0)
                    except queue.Empty:
                        chunk = b'\x00\x00' * 128
                    try:
                        self._proc.stdin.write(chunk)
                        self._proc.stdin.flush()
                    except (BrokenPipeError, OSError):
                        break
            except Exception:
                pass
            finally:
                try:
                    self._proc.stdin.close()
                except Exception:
                    pass

        def reader():
            try:
                while not self._stop.is_set():
                    data = self._proc.stdout.read(4096)
                    if not data:
                        break
                    self._publish(data)
            except Exception:
                pass

        threading.Thread(target=stderr_logger, daemon=True, name="ffmpeg-err").start()
        self._feed_t = threading.Thread(target=feeder, daemon=True, name="ffmpeg-feed")
        self._read_t = threading.Thread(target=reader, daemon=True, name="ffmpeg-read")
        self._feed_t.start()
        self._read_t.start()
        logger.info("Live transcoder started (pre-warming)")

    def _do_stop(self):
        if self._stop:
            self._stop.set()
        if self._sub_q:
            self._stream_bus.unsubscribe(self._sub_q)
            self._sub_q = None
        if self._proc:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=3)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None
        with self._sub_lock:
            self._subscribers.clear()
            self._backlog.clear()
        logger.info("Live transcoder stopped")
