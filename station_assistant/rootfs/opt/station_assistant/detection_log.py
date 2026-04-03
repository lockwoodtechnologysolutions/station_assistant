"""
detection_log.py
SQLite-backed log of all decoded paging tone detections.
Stored at /data/detections.db with configurable retention.
"""

import sqlite3
import logging
from datetime import datetime, timedelta, timezone
from threading import Lock

logger = logging.getLogger(__name__)

DB_PATH = "/data/detections.db"
_lock = Lock()
_conn: sqlite3.Connection | None = None


def _get_conn() -> sqlite3.Connection:
    """Return a persistent connection, creating it on first use."""
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
    return _conn


def init_db() -> None:
    """Create tables if they don't exist."""
    with _lock:
        conn = _get_conn()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS detections (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                detected_at TEXT    NOT NULL,
                seq_id      TEXT    NOT NULL,
                seq_name    TEXT    NOT NULL,
                slug        TEXT    NOT NULL,
                tone1_hz    REAL    NOT NULL,
                tone2_hz    REAL    NOT NULL,
                confidence  REAL    NOT NULL,
                source      TEXT    NOT NULL DEFAULT 'decoded'
            )
        """)
        # Add source column to existing databases (migration)
        try:
            conn.execute("ALTER TABLE detections ADD COLUMN source TEXT NOT NULL DEFAULT 'decoded'")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # column already exists
        conn.commit()


def log_detection(seq: dict, confidence: float, detected_at: str,
                  source: str = "decoded") -> None:
    """Insert a detection record.

    Args:
        source: 'decoded' for real two-tone detections, 'test' for manual triggers.
    """
    with _lock:
        try:
            conn = _get_conn()
            conn.execute(
                """INSERT INTO detections
                   (detected_at, seq_id, seq_name, slug, tone1_hz, tone2_hz, confidence, source)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    detected_at,
                    seq["id"],
                    seq["name"],
                    seq["slug"],
                    seq["tone1_hz"],
                    seq["tone2_hz"],
                    round(confidence, 3),
                    source,
                ),
            )
            conn.commit()
        except sqlite3.Error as e:
            logger.error("DB insert error: %s", e)


def get_recent_detections(limit: int = 200) -> list:
    """Return the most recent detections, newest first."""
    with _lock:
        try:
            conn = _get_conn()
            rows = conn.execute(
                "SELECT * FROM detections ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]
        except sqlite3.Error as e:
            logger.error("DB read error: %s", e)
            return []


def get_detection_count() -> int:
    """Return the total number of detection records."""
    with _lock:
        try:
            conn = _get_conn()
            row = conn.execute("SELECT COUNT(*) FROM detections").fetchone()
            return row[0] if row else 0
        except sqlite3.Error as e:
            logger.error("DB count error: %s", e)
            return 0


def clear_log() -> int:
    """Delete all detection records. Returns number of rows deleted."""
    with _lock:
        try:
            conn = _get_conn()
            cursor = conn.execute("DELETE FROM detections")
            count = cursor.rowcount
            conn.commit()
            return count
        except sqlite3.Error as e:
            logger.error("DB clear error: %s", e)
            return 0


def purge_old_records(retention_days: int) -> int:
    """Delete records older than retention_days. Returns count deleted."""
    if retention_days <= 0:
        return 0
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
    with _lock:
        try:
            conn = _get_conn()
            cursor = conn.execute(
                "DELETE FROM detections WHERE detected_at < ?", (cutoff,)
            )
            count = cursor.rowcount
            conn.commit()
            if count:
                conn.execute("VACUUM")  # reclaim disk space
                logger.info("Purged %d old detection records", count)
            return count
        except sqlite3.Error as e:
            logger.error("DB purge error: %s", e)
            return 0


