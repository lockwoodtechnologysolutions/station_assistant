"""
sse.py
Thread-safe Server-Sent Events (SSE) publish/subscribe bus.
Replaces Flask-SocketIO for real-time event delivery to the browser.
Works reliably through HA Ingress since SSE is just a long-lived HTTP response.
"""

import json
import queue
import threading
import logging

logger = logging.getLogger(__name__)


class SSEBus:
    """Thread-safe pub/sub for Server-Sent Events."""

    def __init__(self):
        self._lock = threading.Lock()
        self._subscribers: list[queue.Queue] = []

    def subscribe(self) -> queue.Queue:
        """Register a new SSE client. Returns a queue to read events from."""
        q = queue.Queue(maxsize=50)
        with self._lock:
            self._subscribers.append(q)
        logger.debug("SSE client subscribed (%d total)", len(self._subscribers))
        return q

    def unsubscribe(self, q: queue.Queue):
        """Remove an SSE client when it disconnects."""
        with self._lock:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass
        logger.debug("SSE client unsubscribed (%d remaining)", len(self._subscribers))

    def emit(self, event: str, data: dict):
        """Broadcast an event to all connected SSE clients.

        Non-blocking: if a client's queue is full (slow consumer), the event
        is dropped for that client rather than blocking the decoder thread.
        """
        msg = f"event: {event}\ndata: {json.dumps(data)}\n\n"
        with self._lock:
            for q in self._subscribers:
                try:
                    q.put_nowait(msg)
                except queue.Full:
                    pass  # slow client, drop the event
