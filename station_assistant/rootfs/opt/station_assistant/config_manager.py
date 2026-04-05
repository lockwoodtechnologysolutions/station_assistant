"""
config_manager.py
Manages add-on configuration (from /data/options.json written by HA Supervisor)
and tone sequence persistence (in /data/sequences.json).
"""

import json
import os
import uuid
import re
import logging
from threading import Lock
from typing import Optional

logger = logging.getLogger(__name__)

OPTIONS_PATH = "/data/options.json"
SEQUENCES_PATH = "/data/sequences.json"
RUNTIME_PATH = "/data/runtime_settings.json"

_seq_lock = Lock()

# ── Default add-on options ─────────────────────────────────────────────────────

DEFAULT_OPTIONS = {
    "audio_device_index": -1,
    "sample_rate": 44100,
    "chunk_size": 2048,
    "log_retention_days": 30,
    "input_gain": 5,
}


def _load_runtime() -> dict:
    """Load addon-managed runtime settings (not overwritten by HA Supervisor)."""
    try:
        with open(RUNTIME_PATH, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_runtime(key: str, value) -> None:
    """Persist a runtime setting to /data/runtime_settings.json.

    This file is managed by the addon, not by HA Supervisor.
    HA overwrites /data/options.json on every restart, so settings
    like input_gain that the user adjusts via the UI must be stored here.
    """
    data = _load_runtime()
    data[key] = value
    try:
        os.makedirs(os.path.dirname(RUNTIME_PATH), exist_ok=True)
        with open(RUNTIME_PATH, "w") as f:
            json.dump(data, f, indent=2)
        logger.info("Runtime setting saved: %s = %s", key, value)
    except Exception as e:
        logger.error("Failed to save runtime setting %s: %s", key, e)


def get_options() -> dict:
    """Read add-on options, with runtime settings taking priority.

    Merge order: defaults < HA options.json < runtime_settings.json
    This ensures user-adjusted values (like input_gain) survive HA restarts.
    """
    try:
        with open(OPTIONS_PATH, "r") as f:
            opts = json.load(f)
        merged = {**DEFAULT_OPTIONS, **opts}
    except FileNotFoundError:
        logger.warning("options.json not found, using defaults")
        merged = DEFAULT_OPTIONS.copy()
    except json.JSONDecodeError as e:
        logger.error("Failed to parse options.json: %s", e)
        merged = DEFAULT_OPTIONS.copy()
    # Runtime settings override HA-managed values
    runtime = _load_runtime()
    merged.update(runtime)
    return merged


# ── Slug generation ────────────────────────────────────────────────────────────

def name_to_slug(name: str) -> str:
    """
    Convert a human-readable name to a safe slug for HA automation IDs and event data.
    "Engine 1"  →  "engine_1"
    "Ladder Co. #2"  →  "ladder_co_2"
    """
    slug = name.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "_", slug)
    slug = slug.strip("_")
    return slug or "sequence"


def slug_to_automation_id(slug: str) -> str:
    """two_tone_{slug}  →  used as the HA automation config ID."""
    return f"two_tone_{slug}"


# ── Sequence CRUD ──────────────────────────────────────────────────────────────

def _coerce_players(value) -> list:
    """Normalize any input to a clean list of entity ID strings."""
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _normalize_players(seq: dict) -> dict:
    """
    Migrate old single-string media_player_entity → media_players list.
    Idempotent: if media_players already exists, returns seq unchanged.
    """
    if "media_players" not in seq:
        old = seq.get("media_player_entity", "")
        seq = dict(seq)
        seq["media_players"] = [old] if old else []
    return seq


def _load_raw() -> list:
    try:
        with open(SEQUENCES_PATH, "r") as f:
            data = json.load(f)
        if not isinstance(data, list):
            return []
        return [_normalize_players(s) for s in data]
    except FileNotFoundError:
        return []
    except json.JSONDecodeError as e:
        logger.error("Failed to parse sequences.json: %s", e)
        return []


def _save_raw(sequences: list) -> None:
    os.makedirs(os.path.dirname(SEQUENCES_PATH), exist_ok=True)
    with open(SEQUENCES_PATH, "w") as f:
        json.dump(sequences, f, indent=2)


def get_sequences() -> list:
    """Return all configured tone sequences (thread-safe)."""
    with _seq_lock:
        return _load_raw()


def get_sequence(seq_id: str) -> Optional[dict]:
    """Return a single sequence by ID, or None if not found."""
    with _seq_lock:
        for seq in _load_raw():
            if seq.get("id") == seq_id:
                return seq
    return None


def validate_sequence(data: dict) -> tuple[bool, str]:
    """Validate sequence fields. Returns (ok, error_message)."""
    name = str(data.get("name", "")).strip()
    if not name:
        return False, "Name is required"
    if len(name) > 64:
        return False, "Name must be 64 characters or fewer"

    for field in ("tone1_hz", "tone2_hz", "tone1_duration", "tone2_duration"):
        try:
            val = float(data.get(field, 0))
            if val <= 0:
                return False, f"{field} must be a positive number"
        except (TypeError, ValueError):
            return False, f"{field} must be a valid number"

    tone1 = float(data["tone1_hz"])
    tone2 = float(data["tone2_hz"])
    if not (100.0 <= tone1 <= 4000.0):
        return False, "Tone 1 frequency must be between 100 and 4000 Hz"
    if not (100.0 <= tone2 <= 4000.0):
        return False, "Tone 2 frequency must be between 100 and 4000 Hz"

    # Tone A and Tone B within the same sequence must not overlap.
    # The decoder uses a ±15 Hz frequency window, so two frequencies
    # within 30 Hz of each other would be indistinguishable.
    if abs(tone1 - tone2) < 30.0:
        return False, (
            f"Tone A ({tone1} Hz) and Tone B ({tone2} Hz) are too close — "
            f"must be at least 30 Hz apart (detector uses a ±15 Hz window)"
        )

    try:
        threshold = float(data.get("threshold", 0.05))
        if not (0.001 <= threshold <= 1.0):
            return False, "Threshold must be between 0.001 and 1.0"
    except (TypeError, ValueError):
        return False, "Threshold must be a valid number"

    try:
        confirm_ratio = float(data.get("confirm_ratio", 0.70))
        if not (0.10 <= confirm_ratio <= 1.0):
            return False, "Tone duration tolerance must be between 0.10 and 1.0"
    except (TypeError, ValueError):
        return False, "Tone duration tolerance must be a valid number"

    try:
        reset = int(data.get("auto_reset_seconds", 30))
        if reset < 5:
            return False, "Auto-reset must be at least 5 seconds"
    except (TypeError, ValueError):
        return False, "Auto-reset seconds must be a valid integer"

    return True, ""


def _check_frequency_overlap(tone1: float, tone2: float,
                              existing: list, exclude_id: str = "") -> str:
    """Check if new tone frequencies overlap with existing sequences.

    The decoder uses a ±15 Hz frequency window, so any two tone
    frequencies within 30 Hz of each other could cross-trigger.
    Returns an error message string, or empty string if no overlap.
    """
    for seq in existing:
        if seq["id"] == exclude_id:
            continue
        for label, new_f in [("A", tone1), ("B", tone2)]:
            for elabel, ef in [("A", seq["tone1_hz"]), ("B", seq["tone2_hz"])]:
                if abs(new_f - ef) < 30.0 and new_f != ef:
                    return (
                        f"Tone {label} ({new_f} Hz) is within 30 Hz of "
                        f"\"{seq['name']}\" Tone {elabel} ({ef} Hz) — "
                        f"this may cause false triggers (detector uses ±15 Hz window)"
                    )
    return ""


def create_sequence(data: dict) -> tuple[Optional[dict], str]:
    """
    Create a new sequence. Returns (sequence_dict, error_message).
    error_message is empty string on success.
    """
    ok, err = validate_sequence(data)
    if not ok:
        return None, err

    name = str(data["name"]).strip()
    slug = name_to_slug(name)

    with _seq_lock:
        sequences = _load_raw()

        # Warn if tone frequencies overlap with existing sequences
        overlap = _check_frequency_overlap(
            float(data["tone1_hz"]), float(data["tone2_hz"]), sequences
        )
        if overlap:
            return None, overlap

        # Ensure unique slug — append numeric suffix if needed
        existing_slugs = {s["slug"] for s in sequences}
        base_slug = slug
        counter = 2
        while slug in existing_slugs:
            slug = f"{base_slug}_{counter}"
            counter += 1

        seq = {
            "id": str(uuid.uuid4()),
            "name": name,
            "slug": slug,
            "tone1_hz": float(data["tone1_hz"]),
            "tone2_hz": float(data["tone2_hz"]),
            "tone1_duration": float(data["tone1_duration"]),
            "tone2_duration": float(data["tone2_duration"]),
            "threshold": float(data.get("threshold", 0.05)),
            "confirm_ratio": float(data.get("confirm_ratio", 0.70)),
            "auto_reset_seconds": int(data.get("auto_reset_seconds", 30)),
            "enabled": bool(data.get("enabled", True)),
            "ha_automation_id": slug_to_automation_id(slug),
            # Media playback config (set via setup wizard or settings)
            "sound_1": str(data.get("sound_1", "")),
            "sound_2": str(data.get("sound_2", "")),
            "sound_3": str(data.get("sound_3", "")),
            "media_players": _coerce_players(data.get("media_players") or data.get("media_player_entity", "")),
            "alert_color": str(data.get("alert_color", "#8b1a1a")),
            "icon": str(data.get("icon", "")),
        }
        sequences.append(seq)
        _save_raw(sequences)
        return seq, ""


def update_sequence(seq_id: str, data: dict) -> tuple[Optional[dict], Optional[dict], str]:
    """
    Update an existing sequence.
    Returns (updated_sequence, old_sequence, error_message).
    On success: (updated, old, "").  On failure: (None, None, error_message).
    """
    ok, err = validate_sequence(data)
    if not ok:
        return None, None, err

    with _seq_lock:
        sequences = _load_raw()
        idx = next((i for i, s in enumerate(sequences) if s["id"] == seq_id), None)
        if idx is None:
            return None, None, "Sequence not found"

        # Warn if tone frequencies overlap with other sequences
        overlap = _check_frequency_overlap(
            float(data["tone1_hz"]), float(data["tone2_hz"]),
            sequences, exclude_id=seq_id,
        )
        if overlap:
            return None, None, overlap

        old = sequences[idx]
        new_name = str(data["name"]).strip()
        new_slug = name_to_slug(new_name)

        # If slug changed, ensure uniqueness
        if new_slug != old["slug"]:
            existing_slugs = {s["slug"] for s in sequences if s["id"] != seq_id}
            base_slug = new_slug
            counter = 2
            while new_slug in existing_slugs:
                new_slug = f"{base_slug}_{counter}"
                counter += 1

        updated = {
            **old,
            "name": new_name,
            "slug": new_slug,
            "tone1_hz": float(data["tone1_hz"]),
            "tone2_hz": float(data["tone2_hz"]),
            "tone1_duration": float(data["tone1_duration"]),
            "tone2_duration": float(data["tone2_duration"]),
            "threshold": float(data.get("threshold", old["threshold"])),
            "confirm_ratio": float(data.get("confirm_ratio", old.get("confirm_ratio", 0.70))),
            "auto_reset_seconds": int(data.get("auto_reset_seconds", old["auto_reset_seconds"])),
            "enabled": bool(data.get("enabled", old["enabled"])),
            "ha_automation_id": slug_to_automation_id(new_slug),
            # Media playback config — update if provided, else keep existing
            "sound_1": str(data.get("sound_1", old.get("sound_1", ""))),
            "sound_2": str(data.get("sound_2", old.get("sound_2", ""))),
            "sound_3": str(data.get("sound_3", old.get("sound_3", ""))),
            "media_players": _coerce_players(
                data.get("media_players") if "media_players" in data
                else data.get("media_player_entity") if "media_player_entity" in data
                else old.get("media_players") or old.get("media_player_entity", "")
            ),
            "alert_color": str(data.get("alert_color", old.get("alert_color", "#8b1a1a"))),
            "icon": str(data.get("icon", old.get("icon", ""))),
        }
        sequences[idx] = updated
        _save_raw(sequences)
        return updated, old, ""


def delete_sequence(seq_id: str) -> tuple[Optional[dict], str]:
    """Delete a sequence. Returns (deleted_sequence, error_message)."""
    with _seq_lock:
        sequences = _load_raw()
        idx = next((i for i, s in enumerate(sequences) if s["id"] == seq_id), None)
        if idx is None:
            return None, "Sequence not found"
        deleted = sequences.pop(idx)
        _save_raw(sequences)
        return deleted, ""
