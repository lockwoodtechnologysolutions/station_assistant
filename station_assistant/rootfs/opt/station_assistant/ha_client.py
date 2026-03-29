"""
ha_client.py
Home Assistant REST API client.
Uses the Supervisor-injected SUPERVISOR_TOKEN to authenticate.
All calls go through the Supervisor proxy at http://supervisor/core/api.
"""

import os
import struct
import logging
import requests
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

HA_BASE_URL = "http://supervisor/core/api"

# ── Shared session (lazy init so SUPERVISOR_TOKEN is read at first use) ───────

_session: requests.Session | None = None


def _get_session() -> requests.Session:
    """Return a module-level persistent session, created on first call."""
    global _session
    if _session is None:
        _session = requests.Session()
        token = os.environ.get("SUPERVISOR_TOKEN", "")
        _session.headers.update({
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        })
    return _session


def _get(path: str, timeout: int = 10) -> Optional[dict | list]:
    try:
        r = _get_session().get(f"{HA_BASE_URL}{path}", timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.error("HA GET %s failed: %s", path, e)
        return None


def _post(path: str, payload: dict) -> Optional[dict]:
    try:
        r = _get_session().post(f"{HA_BASE_URL}{path}", json=payload, timeout=10)
        r.raise_for_status()
        try:
            return r.json()
        except ValueError:
            return {"ok": True}
    except Exception as e:
        logger.error("HA POST %s failed: %s", path, e)
        return None


def _delete(path: str) -> bool:
    try:
        r = _get_session().delete(f"{HA_BASE_URL}{path}", timeout=10)
        r.raise_for_status()
        return True
    except requests.RequestException as e:
        logger.error("HA DELETE %s failed: %s", path, e)
        return False


# ── Addon URL discovery (for media player stream URLs) ───────────────────────

_cached_stream_base: str = ""


def get_addon_stream_url() -> str:
    """Return the HTTP base URL that media players can use to reach this addon.

    Tries to derive the host IP from HA's internal_url config so media player
    devices on the LAN can reach the addon's mapped port (8099).  Falls back
    to ``homeassistant.local:8099``.
    """
    global _cached_stream_base
    if _cached_stream_base:
        return _cached_stream_base

    try:
        config = _get("/config")
        if config:
            internal_url = config.get("internal_url", "")
            if internal_url:
                from urllib.parse import urlparse
                host = urlparse(internal_url).hostname
                if host:
                    _cached_stream_base = f"http://{host}:8099"
                    logger.info("Addon stream base URL: %s", _cached_stream_base)
                    return _cached_stream_base
    except Exception as e:
        logger.debug("get_addon_stream_url: config lookup failed: %s", e)

    _cached_stream_base = "http://homeassistant.local:8099"
    logger.info("Addon stream base URL (fallback): %s", _cached_stream_base)
    return _cached_stream_base


# ── Audio file duration helpers ───────────────────────────────────────────────

# MPEG bitrate lookup tables (kbps).  Index 0 is "free", 15 is "bad".
_BITRATES_V1_L3 = [0,32,40,48,56,64,80,96,112,128,160,192,224,256,320,0]
_BITRATES_V2_L3 = [0,8,16,24,32,40,48,56,64,80,96,112,128,144,160,0]
_SAMPLE_RATES_V1 = [44100, 48000, 32000, 0]
_SAMPLE_RATES_V2 = [22050, 24000, 16000, 0]
_SAMPLE_RATES_V25 = [11025, 12000, 8000, 0]

# Directories where sound files can be found (bundled + user-uploaded).
_SOUND_DIRS = [
    Path("/opt/station_assistant/sounds"),
    Path("/media/station_assistant"),
    Path("/config/www/station_assistant/sounds"),
]


def _find_sound_file(filename: str) -> Optional[Path]:
    """Locate a sound file on the local filesystem."""
    for d in _SOUND_DIRS:
        p = d / filename
        if p.is_file():
            return p
    logger.warning(
        "_find_sound_file: %s not found in any of: %s",
        filename, [str(d) for d in _SOUND_DIRS],
    )
    return None


def _get_wav_duration(path: Path) -> Optional[float]:
    """Return duration of a WAV file in seconds, or None on failure."""
    try:
        with open(path, "rb") as f:
            riff = f.read(12)
            if len(riff) < 12 or riff[:4] != b"RIFF" or riff[8:12] != b"WAVE":
                return None

            fmt_found = False
            byte_rate = 0
            data_size = 0

            while True:
                chunk_hdr = f.read(8)
                if len(chunk_hdr) < 8:
                    break
                chunk_id = chunk_hdr[:4]
                chunk_size = struct.unpack_from("<I", chunk_hdr, 4)[0]

                if chunk_id == b"fmt ":
                    fmt_data = f.read(min(chunk_size, 16))
                    if len(fmt_data) < 16:
                        return None
                    channels = struct.unpack_from("<H", fmt_data, 2)[0]
                    sample_rate = struct.unpack_from("<I", fmt_data, 4)[0]
                    byte_rate = struct.unpack_from("<I", fmt_data, 8)[0]
                    if byte_rate == 0 and sample_rate > 0:
                        bits_per_sample = struct.unpack_from("<H", fmt_data, 14)[0]
                        byte_rate = sample_rate * channels * bits_per_sample // 8
                    remaining = chunk_size - 16
                    if remaining > 0:
                        f.seek(remaining, 1)
                    fmt_found = True
                elif chunk_id == b"data":
                    data_size = chunk_size
                    break
                else:
                    f.seek(chunk_size, 1)

            if fmt_found and byte_rate > 0 and data_size > 0:
                return round(data_size / byte_rate, 2)

    except Exception as e:
        logger.debug("_get_wav_duration(%s): %s", path, e)
    return None


def _get_mp3_duration(path: Path) -> Optional[float]:
    """Return duration of an MP3 file in seconds, or None on failure."""
    try:
        file_size = path.stat().st_size
        with open(path, "rb") as f:
            header = f.read(10)
            if len(header) < 10:
                return None
            if header[:3] == b"ID3":
                tag_size = (
                    (header[6] & 0x7F) << 21
                    | (header[7] & 0x7F) << 14
                    | (header[8] & 0x7F) << 7
                    | (header[9] & 0x7F)
                )
                f.seek(10 + tag_size)
            else:
                f.seek(0)

            buf = f.read(4)
            attempts = 0
            while len(buf) == 4 and attempts < 8192:
                if buf[0] == 0xFF and (buf[1] & 0xE0) == 0xE0:
                    ver_bits = (buf[1] >> 3) & 0x03
                    br_index = (buf[2] >> 4) & 0x0F
                    sr_index = (buf[2] >> 2) & 0x03

                    if ver_bits == 0x03:
                        bitrate = _BITRATES_V1_L3[br_index]
                        sr = _SAMPLE_RATES_V1[sr_index]
                    elif ver_bits == 0x02:
                        bitrate = _BITRATES_V2_L3[br_index]
                        sr = _SAMPLE_RATES_V2[sr_index]
                    elif ver_bits == 0x00:
                        bitrate = _BITRATES_V2_L3[br_index]
                        sr = _SAMPLE_RATES_V25[sr_index]
                    else:
                        bitrate = 0
                        sr = 0

                    if bitrate > 0 and sr > 0:
                        audio_bytes = file_size - f.tell() + 4
                        duration = audio_bytes / (bitrate * 125)
                        return round(duration, 2)

                f.seek(-3, 1)
                buf = f.read(4)
                attempts += 1

    except Exception as e:
        logger.debug("_get_mp3_duration(%s): %s", path, e)
    return None


def get_sound_duration(filename: str) -> Optional[float]:
    """Return the duration in seconds of a local sound file (MP3 or WAV)."""
    path = _find_sound_file(filename)
    if path is None:
        return None

    suffix = path.suffix.lower()
    if suffix == ".wav":
        return _get_wav_duration(path)
    return _get_mp3_duration(path)


def concatenate_sounds(filenames: list[str]) -> Optional[str]:
    """Concatenate multiple MP3 files into a single temporary file.

    Returns the filename of the combined file, or None on failure.
    """
    if not filenames:
        return None

    logger.info("concatenate_sounds: merging %s", filenames)

    paths = []
    for fn in filenames:
        p = _find_sound_file(fn)
        if p is None:
            logger.error("concatenate_sounds: FAILED — file not found: %s", fn)
            return None
        paths.append(p)

    if len(paths) == 1:
        logger.debug("concatenate_sounds: only 1 file, skipping merge")
        return None

    out_dir = Path("/media/station_assistant")
    out_dir.mkdir(parents=True, exist_ok=True)

    out_path = out_dir / "_combined_alert.mp3"
    concat_list = out_dir / "_concat_list.txt"
    try:
        import subprocess

        # Write ffmpeg concat demuxer file list
        with open(concat_list, "w") as f:
            for p in paths:
                # Escape single quotes in paths for ffmpeg
                safe = str(p).replace("'", "'\\''")
                f.write(f"file '{safe}'\n")

        # Use ffmpeg to merge files, always re-encoding to a uniform sample
        # rate.  Stream-copy (-c copy) would be faster but silently produces
        # broken output when files have different sample rates — LinkPlay
        # devices read the rate from the first frame and apply it to all
        # subsequent frames, causing a chipmunk effect.
        result = subprocess.run(
            [
                "ffmpeg", "-y",
                "-f", "concat",
                "-safe", "0",
                "-i", str(concat_list),
                "-ar", "44100",         # normalize sample rate
                "-ac", "1",             # mono
                "-b:a", "128k",         # constant bitrate
                str(out_path),
            ],
            capture_output=True, text=True, timeout=60,
        )

        if result.returncode != 0:
            logger.error(
                "concatenate_sounds: ffmpeg failed: %s",
                result.stderr[-300:] if result.stderr else "unknown error",
            )
            return None

        logger.info(
            "concatenate_sounds: merged %d files → %s (%.1f KB)",
            len(paths), out_path.name, out_path.stat().st_size / 1024,
        )
        return out_path.name

    except Exception as e:
        logger.error("concatenate_sounds failed: %s", e)
        if out_path.exists():
            out_path.unlink()
        return None
    finally:
        if concat_list.exists():
            concat_list.unlink()


def cleanup_combined_sound() -> None:
    """Remove the temporary combined alert file after playback."""
    combined = Path("/media/station_assistant/_combined_alert.mp3")
    try:
        if combined.exists():
            combined.unlink()
    except Exception as e:
        logger.debug("cleanup_combined_sound: %s", e)


# ── Media action helpers ───────────────────────────────────────────────────────

def _build_media_actions(seq: dict) -> list:
    """
    Media playback is handled directly by Station Assistant's audio engine
    (stack_manager._play_audio) after the Page Sequence Gap timer expires.
    The HA automation retains only user-added custom actions.
    """
    return []


# ── Direct media playback helpers (called by stack_manager audio thread) ───────

def play_sound(entities, sound_filename: str) -> bool:
    """
    Play a sound file on one or more HA media_player entities simultaneously.
    """
    if isinstance(entities, str):
        entity_id = entities
    elif isinstance(entities, list) and len(entities) == 1:
        entity_id = entities[0]
    else:
        entity_id = entities
    media_content_id = (
        f"media-source://media_source/local/station_assistant/{sound_filename}"
    )
    mime = "audio/wav" if sound_filename.lower().endswith(".wav") else "audio/mpeg"
    payload = {
        "entity_id":          entity_id,
        "media_content_id":   media_content_id,
        "media_content_type": mime,
    }
    result = _post("/services/media_player/play_media", payload)
    targets = entity_id if isinstance(entity_id, str) else ", ".join(entity_id)
    if result is not None:
        logger.info("play_sound: %s → %s", sound_filename, targets)
        return True
    logger.warning("play_sound failed: %s → %s", sound_filename, targets)
    return False


def play_url(entities, url: str, mime: str = "audio/wav") -> bool:
    """Play an arbitrary HTTP URL on one or more HA media_player entities."""
    if isinstance(entities, str):
        entity_id = entities
    elif isinstance(entities, list) and len(entities) == 1:
        entity_id = entities[0]
    else:
        entity_id = entities
    payload = {
        "entity_id":          entity_id,
        "media_content_id":   url,
        "media_content_type": mime,
    }
    result = _post("/services/media_player/play_media", payload)
    targets = entity_id if isinstance(entity_id, str) else ", ".join(entity_id)
    if result is not None:
        logger.info("play_url: %s → %s", url, targets)
        return True
    logger.warning("play_url failed: %s → %s", url, targets)
    return False


def stop_media(entities) -> bool:
    """Stop playback on one or more HA media_player entities."""
    if isinstance(entities, str):
        entity_id = entities
    elif isinstance(entities, list) and len(entities) == 1:
        entity_id = entities[0]
    else:
        entity_id = entities
    result = _post("/services/media_player/media_stop", {"entity_id": entity_id})
    return result is not None


def wait_until_idle(entity_id: str, timeout: float = 120.0,
                    known_duration: Optional[float] = None) -> bool:
    """
    Wait for a media_player to finish playing, then return.

    If *known_duration* is provided (seconds), sleeps for that duration
    plus a small buffer instead of polling the player state.
    """
    import time as _time

    if known_duration is not None and known_duration > 0:
        sleep_for = known_duration + 0.3
        logger.debug(
            "wait_until_idle: %s sleeping %.1fs (known_duration=%.1f)",
            entity_id, sleep_for, known_duration,
        )
        _time.sleep(sleep_for)
        return True

    # Fallback: poll player state
    deadline        = _time.time() + timeout
    playing_seen    = False
    playing_grace   = _time.time() + 5.0

    _time.sleep(0.3)

    while _time.time() < deadline:
        state_data = _get(f"/states/{entity_id}", timeout=3)
        if state_data:
            state = state_data.get("state", "")
            if state == "playing":
                playing_seen = True
            elif playing_seen:
                return True
            elif not playing_seen and _time.time() > playing_grace:
                logger.debug(
                    "wait_until_idle: %s never entered playing state, continuing",
                    entity_id,
                )
                return True
        _time.sleep(0.3)

    logger.warning("wait_until_idle: %s timed out after %.0fs", entity_id, timeout)
    return False


def _split_user_actions(actions: list) -> tuple:
    """
    Split an action list into (our_media_actions, user_custom_actions).
    """
    if not actions:
        return [], []
    first_custom = len(actions)
    for i, action in enumerate(actions):
        svc = action.get("service", action.get("action", ""))
        if not svc.startswith("media_player."):
            first_custom = i
            break
    return actions[:first_custom], actions[first_custom:]


# ── Automation config builder ──────────────────────────────────────────────────

def _automation_config(seq: dict, preserve_actions: list = None) -> dict:
    """
    Build a HA automation config dict for a given sequence.
    """
    return {
        "id": seq["ha_automation_id"],
        "alias": f"Two-Tone Paging Sequence: {seq['name']}",
        "description": (
            f"Auto-created by Two-Tone Decoder add-on.\n"
            f"Fires when paging tones for \"{seq['name']}\" are decoded.\n\n"
            f"Tone 1: {seq['tone1_hz']} Hz  ({seq['tone1_duration']}s)\n"
            f"Tone 2: {seq['tone2_hz']} Hz  ({seq['tone2_duration']}s)\n\n"
            f"Add your station response actions in the action block below.\n"
            f"Event data available in templates:\n"
            f"  {{{{ trigger.event.data.sequence_name }}}}\n"
            f"  {{{{ trigger.event.data.confidence }}}}\n"
            f"  {{{{ trigger.event.data.detected_at }}}}"
        ),
        "trigger": [
            {
                "platform": "event",
                "event_type": "two_tone_decoded",
                "event_data": {"slug": seq["slug"]},
            }
        ],
        "condition": [],
        "action": preserve_actions if preserve_actions is not None else [],
        "mode": "single",
    }


# ── Automation CRUD ────────────────────────────────────────────────────────────

def get_automation_config(auto_id: str) -> Optional[dict]:
    """Retrieve the full automation config including the user's action block."""
    result = _get(f"/config/automation/config/{auto_id}")
    if isinstance(result, dict):
        return result
    return None


def create_or_update_automation(seq: dict) -> bool:
    """Create or update the HA automation for a sequence."""
    auto_id = seq["ha_automation_id"]

    existing = get_automation_config(auto_id)
    user_actions = []
    if existing and isinstance(existing, dict):
        _, user_actions = _split_user_actions(existing.get("action", []))
        if user_actions:
            logger.info("Preserving %d user action(s) for automation %s",
                        len(user_actions), auto_id)

    media_actions = _build_media_actions(seq)
    all_actions = media_actions + user_actions

    config = _automation_config(seq, preserve_actions=all_actions)
    result = _post(f"/config/automation/config/{auto_id}", config)
    if result is not None:
        logger.info("Automation created/updated: automation.%s (media steps: %d)",
                    auto_id, len(media_actions))
        return True
    return False


def rename_automation(seq: dict, old_auto_id: str) -> bool:
    """Handle a sequence rename where the slug changed."""
    existing = get_automation_config(old_auto_id)
    preserved_actions = []
    if existing and isinstance(existing, dict):
        preserved_actions = existing.get("action", [])
        if preserved_actions:
            logger.info(
                "Rename: preserving %d action(s) from %s → %s",
                len(preserved_actions), old_auto_id, seq["ha_automation_id"]
            )

    delete_automation(old_auto_id)

    config = _automation_config(seq, preserve_actions=preserved_actions)
    new_id = seq["ha_automation_id"]
    result = _post(f"/config/automation/config/{new_id}", config)
    if result is not None:
        logger.info("Renamed automation %s → %s (actions preserved: %s)",
                    old_auto_id, new_id, bool(preserved_actions))
        return True
    return False


def delete_automation(auto_id: str) -> bool:
    """Delete a HA automation by its config ID."""
    ok = _delete(f"/config/automation/config/{auto_id}")
    if ok:
        logger.info("Automation deleted: automation.%s", auto_id)
    return ok


def trigger_automation(seq: dict) -> bool:
    """Test a sequence by firing the two_tone_decoded event directly."""
    from datetime import datetime, timezone
    detected_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return fire_two_tone_event(seq, confidence=1.0, detected_at=detected_at)


def fire_health_event(status: str, message: str) -> bool:
    """Fire a two_tone_decoder_health event on the HA event bus."""
    from datetime import datetime, timezone
    payload = {
        "status": status,
        "message": message,
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    result = _post("/events/two_tone_decoder_health", payload)
    if result is not None:
        logger.info("Health event fired: status=%s message=%s", status, message)
        return True
    logger.warning("Failed to fire health event: status=%s", status)
    return False


def push_decoder_sensor(status: str, error: str = "", extra: dict | None = None) -> bool:
    """Write decoder state directly to a persistent HA sensor entity."""
    from datetime import datetime, timezone
    attrs = {
        "friendly_name": "Station Assistant Decoder",
        "icon": "mdi:radio-tower",
        "error": error or "none",
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if extra:
        attrs.update(extra)
    result = _post("/states/sensor.station_assistant_decoder", {
        "state": status,
        "attributes": attrs,
    })
    return result is not None


def push_watchdog_sensor(app_version: str = "") -> bool:
    """Push a heartbeat to sensor.station_assistant_watchdog."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    result = _post("/states/sensor.station_assistant_watchdog", {
        "state": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "attributes": {
            "friendly_name": "Station Assistant Watchdog",
            "icon": "mdi:heart-pulse",
            "app_version": app_version,
            "device_class": "timestamp",
        },
    })
    return result is not None


def fire_two_tone_event(seq: dict, confidence: float, detected_at: str) -> bool:
    """Fire the two_tone_decoded event on the HA event bus."""
    payload = {
        "slug":          seq["slug"],
        "sequence_name": seq["name"],
        "tone1_hz":      seq["tone1_hz"],
        "tone2_hz":      seq["tone2_hz"],
        "confidence":    round(confidence, 3),
        "detected_at":   detected_at,
    }
    result = _post("/events/two_tone_decoded", payload)
    if result is not None:
        logger.info("Event fired: two_tone_decoded slug=%s confidence=%.2f",
                    seq["slug"], confidence)
        return True
    return False


def reload_automations() -> bool:
    """Call HA's automation.reload service."""
    result = _post("/services/automation/reload", {})
    if result is not None:
        logger.info("automation.reload service called — automations refreshed")
        return True
    logger.warning("automation.reload service call failed")
    return False


# ── Automation status ──────────────────────────────────────────────────────────

def get_automation_state(auto_id: str) -> Optional[str]:
    """Return the state of a HA automation entity ('on', 'off', or None)."""
    entity_id = f"automation.{auto_id}"
    states = _get("/states")
    if not states:
        return None
    for state in states:
        if state.get("entity_id") == entity_id:
            return state.get("state")
    return None


def get_all_automation_states() -> dict:
    """Return a dict indexed by entity_id and config id."""
    states = _get("/states", timeout=2)
    if not states:
        return {}
    result = {}
    for s in states:
        if not isinstance(s, dict):
            continue
        entity_id = s.get("entity_id", "")
        if not entity_id.startswith("automation."):
            continue
        state = s.get("state")
        result[entity_id] = state
        config_id = s.get("attributes", {}).get("id")
        if config_id:
            result[config_id] = state
    return result


def get_all_automations() -> list:
    """Return all automation entities from HA."""
    states = _get("/states")
    if not states:
        return []
    result = []
    for state in states:
        if state.get("entity_id", "").startswith("automation."):
            attrs = state.get("attributes", {})
            result.append({
                "entity_id": state["entity_id"],
                "alias":     attrs.get("friendly_name", state["entity_id"]),
                "state":     state.get("state", "unknown"),
            })
    return sorted(result, key=lambda x: x["alias"].lower())


def check_ha_connection() -> tuple[bool, str]:
    """Verify connectivity to the HA API. Returns (ok, message)."""
    try:
        r = _get_session().get(f"{HA_BASE_URL}/", timeout=5)
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}"
        r2 = _get_session().get(f"{HA_BASE_URL}/config", timeout=5)
        if r2.status_code == 200:
            version = r2.json().get("version", "")
            if version:
                return True, f"Connected — HA {version}"
        return True, "Connected"
    except requests.RequestException as e:
        return False, str(e)
