"""
main.py  —  Station Assistant
Entry point. Wires together:
  - Existing battle-tested DecoderService (goertzel, PyAudio, state machines)
  - StackManager (stacked dispatch logic — NEW)
  - Flask/SocketIO web server (setup wizard, dashboard, settings, status)
  - HA API client (existing ha_client.py + SA event extensions)

Preserves all existing two_tone_decoded events so departments using
the original Two-Tone Decoder add-on automations continue to work.
Adds station_assistant_alert events for the new unified dashboard.
"""

# ── Eventlet monkey-patch — MUST be first, before all other imports ───────────
import eventlet
eventlet.monkey_patch()

import json
import logging
import math
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

# ── Flask + SocketIO ───────────────────────────────────────────────────────────
from flask import Flask, render_template, request, jsonify, redirect, url_for, send_from_directory, send_file
from flask_socketio import SocketIO, emit
from flask_cors import CORS

import detection_log as dl
import config_manager as cm
import ha_client as ha
from decoder import DecoderService, list_audio_devices
from sse import SSEBus
from sa_config import SAConfig
from stack_manager import StackManager
from transcoder import LiveTranscoder
from constants import APP_VERSION, MAX_SEQUENCES, SSE_KEEPALIVE_TIMEOUT

# ── App setup ──────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent

app = Flask(
    __name__,
    template_folder=str(BASE_DIR / "templates"),
    static_folder=str(BASE_DIR / "static"),
)
app.config["SECRET_KEY"] = os.urandom(24).hex()

CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")

# ── HA Ingress middleware (from existing webapp.py — handles path prefix) ──────
_cached_ingress_path: str = ""


class IngressMiddleware:
    """WSGI middleware to handle HA Ingress path prefix.

    HA sends HTTP_X_INGRESS_PATH on every proxied request.  We cache the last
    seen value so the Jinja2 context processor can also access it, but for the
    path-rewriting we ONLY apply the prefix when the current request actually
    carries the header — direct (non-Ingress) requests must not be affected.
    """
    def __init__(self, wsgi_app):
        self.app = wsgi_app

    def __call__(self, environ, start_response):
        global _cached_ingress_path
        ingress_path = environ.get("HTTP_X_INGRESS_PATH", "")
        if ingress_path:
            # Cache for other uses (e.g. health-check endpoints)
            _cached_ingress_path = ingress_path

        # Only rewrite paths for actual Ingress requests, never for direct ones.
        if ingress_path:
            environ["SCRIPT_NAME"] = ingress_path
            path = environ.get("PATH_INFO", "")
            if path.startswith(ingress_path):
                environ["PATH_INFO"] = path[len(ingress_path):] or "/"
        return self.app(environ, start_response)


app.wsgi_app = IngressMiddleware(app.wsgi_app)


# ── Prevent browser caching of HTML pages ─────────────────────────────────────
@app.after_request
def _no_cache_html(response):
    """Add no-store headers to HTML responses so browsers never serve stale pages."""
    ct = response.content_type or ""
    if "text/html" in ct:
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


@app.route("/favicon.ico")
def favicon():
    """Serve favicon.ico from static folder so browsers find it at the root path."""
    return send_from_directory(str(BASE_DIR / "static"), "favicon.ico", mimetype="image/x-icon")


# ── Direct-access guard ────────────────────────────────────────────────────────
@app.before_request
def _guard_direct_access():
    """When accessed directly (not via HA ingress), only the dashboard is allowed.

    Management pages, settings, setup wizard, and all configuration APIs are
    restricted to HA ingress so they are protected by Home Assistant authentication.
    The public dashboard page and its required assets remain accessible on the
    addon port for use on kiosk displays.
    """
    # Requests routed through HA ingress carry this header — allow everything.
    if request.environ.get("HTTP_X_INGRESS_PATH", ""):
        return

    path = request.path

    # Static assets, Socket.IO transport, and favicon are always public.
    if (path.startswith("/static/")
            or path.startswith("/socket.io")
            or path == "/favicon.ico"):
        return

    # Dashboard page and the API endpoints it depends on are public.
    _public_paths = {"/dashboard", "/api/health", "/api/weather", "/api/logo"}
    if path in _public_paths:
        return
    if (path.startswith("/api/stream")
            or path.startswith("/api/sounds/")
            or path == "/api/audio/live"
            or path == "/api/audio/monitor"):
        return

    # All other /api/* routes — configuration/management — return 403.
    if path.startswith("/api/"):
        return jsonify({"error": "Management access requires Home Assistant"}), 403

    # Any other page route (/, /setup, /settings, /status) — redirect to dashboard.
    return redirect(url_for("dashboard"))


# ── Jinja2 context processor: inject script_root into every template ──────────

@app.context_processor
def _inject_script_root():
    """Make {{ script_root }} and {{ app_version }} available in all templates."""
    return {
        "script_root": request.environ.get("HTTP_X_INGRESS_PATH", ""),
        "app_version": APP_VERSION,
    }

# ── Global services ────────────────────────────────────────────────────────────
sa_config = SAConfig()
sse_bus   = SSEBus()
stack_mgr = StackManager(sa_config)


def _on_decoder_detection(seq: dict, confidence: float, detected_at: str) -> None:
    """Called by DecoderService after each confirmed detection.

    The decoder already handles HA events, SQLite logging, and SSE emission.
    This callback only adds the stack manager layer for incident accumulation
    and dashboard broadcast.
    """
    stack_mgr.on_tone_detected(seq, confidence)


decoder = DecoderService(sse_bus, on_detection_callback=_on_decoder_detection)
_live_transcoder = LiveTranscoder(decoder.stream_bus)


# ── Stack manager callbacks → SocketIO broadcast ──────────────────────────────

def _on_stack_alert(payload: dict) -> None:
    """Broadcast alert state to all connected dashboard clients."""
    socketio.emit("alert", payload)
    logger.debug("SocketIO broadcast: alert — %s", payload.get("unit_label"))


def _on_stack_idle(payload: dict) -> None:
    """Broadcast idle state to all connected dashboard clients."""
    socketio.emit("alert", payload)
    logger.debug("SocketIO broadcast: idle")


stack_mgr.set_alert_callback(_on_stack_alert)
stack_mgr.set_idle_callback(_on_stack_idle)
stack_mgr.set_prewarm_callback(lambda: _live_transcoder.start())
stack_mgr.set_relay_done_callback(lambda: _live_transcoder.stop())


# ══════════════════════════════════════════════════════════════════════════════
# PAGE ROUTES
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    """Landing page — management UI if configured, setup wizard if first run."""
    if not SAConfig.is_setup_complete():
        return redirect(url_for("setup"))
    return render_template("manage.html")


@app.route("/setup")
def setup():
    if SAConfig.is_setup_complete():
        return redirect(url_for("index"))
    return render_template("setup.html")


@app.route("/dashboard")
def dashboard():
    if not SAConfig.is_setup_complete():
        return redirect(url_for("setup"))
    cfg  = sa_config.load()
    seqs = cm.get_sequences()

    # Make sure tone labels and sounds are in config for the dashboard template.
    # sa_config values win (they were saved by activate/settings); fall back to sequences.json.
    for i, seq in enumerate(seqs[:2], start=1):
        cfg.setdefault(f"tone_{i}_label", seq["name"])
        cfg.setdefault(f"tone_{i}_freq_a", seq["tone1_hz"])
        cfg.setdefault(f"tone_{i}_freq_b", seq["tone2_hz"])
        cfg.setdefault(f"tone_{i}_sound",  "engine.mp3" if i == 1 else "medic.mp3")

    cfg["has_logo"] = any(Path("/data").glob("dept_logo.*"))
    return render_template("dashboard.html", config=cfg, sequences=seqs)


@app.route("/settings")
def settings():
    if not SAConfig.is_setup_complete():
        return redirect(url_for("setup"))
    cfg  = sa_config.load()
    opts = cm.get_options()
    seqs = cm.get_sequences()

    # Merge first two sequence records into config so settings.html can
    # render tone fields (tone_1_label, tone_1_freq_a, etc.) correctly.
    # sa_config values take precedence if they were saved; fall back to sequences.json.
    for i, seq in enumerate(seqs[:2], start=1):
        cfg.setdefault(f"tone_{i}_label",    seq["name"])
        cfg.setdefault(f"tone_{i}_freq_a",   seq["tone1_hz"])
        cfg.setdefault(f"tone_{i}_freq_b",   seq["tone2_hz"])
        cfg.setdefault(f"tone_{i}_tolerance", 20)
        cfg.setdefault(f"tone_{i}_sound",    "engine.mp3" if i == 1 else "medic.mp3")
        cfg.setdefault(f"tone_{i}_timeout",  seq["auto_reset_seconds"])

    # Expose current audio device index for the settings select element
    cfg["audio_device"] = str(opts.get("audio_device_index", -1))

    return render_template("settings.html", config=cfg, options=opts, sequences=seqs)


@app.route("/status")
def status():
    if not SAConfig.is_setup_complete():
        return redirect(url_for("setup"))
    cfg = sa_config.load()
    return render_template("status.html", config=cfg)


# ══════════════════════════════════════════════════════════════════════════════
# SETUP API
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/setup/weather_entities")
def api_weather_entities():
    """Return all weather.* entities from HA."""
    try:
        states = ha._get("/states") or []
        entities = [
            {
                "entity_id":     s["entity_id"],
                "friendly_name": s.get("attributes", {}).get("friendly_name", s["entity_id"]),
            }
            for s in states
            if isinstance(s, dict) and s.get("entity_id", "").startswith("weather.")
        ]
        return jsonify({"status": "ok", "entities": sorted(entities, key=lambda x: x["friendly_name"])})
    except Exception as e:
        logger.error("weather_entities: %s", e)
        return jsonify({"status": "error", "entities": [], "message": str(e)})


@app.route("/api/setup/audio_devices")
def api_audio_devices():
    """Return available audio input devices — always enumerate fresh."""
    try:
        devices = list_audio_devices()  # always fresh; cache may predate USB attach
        result = [
            {
                "index": d["index"],
                "id":    str(d["index"]),
                "name":  d["name"],
            }
            for d in devices
        ]
        return jsonify({"status": "ok", "devices": result})
    except Exception as e:
        logger.error("audio_devices: %s", e)
        return jsonify({"status": "error", "devices": [], "message": str(e)})


@app.route("/api/setup/activate", methods=["POST"])
def api_activate():
    """
    Process setup wizard submission.
    Creates/updates two tone sequences, saves SA config, creates HA automations,
    copies alert sounds to /config/www/, marks setup complete, starts decoder.
    """
    try:
        data = request.get_json() or {}

        # ── 1. Build and save SA-level config (station) ─────────────────────
        sa_cfg = {
            "dept_name":      data.get("dept_name",      ""),
            "station_name":   data.get("station_name",   "Station 1"),
            "weather_entity": data.get("weather_entity", "weather.home"),
            "stack_window":   int(data.get("stack_window",   60)),
            "return_timeout": int(data.get("return_timeout", 45)),
        }
        sa_config.save(sa_cfg)

        # ── 2. Build tone_defs list — supports new 'tones' array (N sequences)
        #       AND legacy flat tone_N_* fields (old wizard format) ───────────
        tones_array = data.get("tones")  # new wizard sends [{label,freq_a,...},...]
        if tones_array and isinstance(tones_array, list):
            tone_defs = []
            for t in tones_array:
                tone_defs.append({
                    "name":               t.get("label", f"Sequence {len(tone_defs)+1}"),
                    "tone1_hz":           float(t.get("freq_a", 688.8)),
                    "tone2_hz":           float(t.get("freq_b", 440.0)),
                    "tone1_duration":     float(t.get("duration_a", 1.0)),
                    "tone2_duration":     float(t.get("duration_b", 3.0)),
                    "threshold":          float(t.get("threshold", 0.15)),
                    "auto_reset_seconds": int(t.get("timeout", 60)),
                    "enabled":            True,
                    "sound_1":            t.get("sound_1", ""),
                    "sound_2":            t.get("sound_2", ""),
                    "sound_3":            t.get("sound_3", ""),
                    "media_players":      _coerce_players(t.get("media_players") or t.get("media_player", "")),
                })
        else:
            # Legacy flat format: tone_1_label, tone_1_freq_a, …
            tone_count = int(data.get("tone_count", 2))
            defaults = [
                {"label": "ENGINE 1", "freq_a": 688.8, "freq_b": 440.0, "sound": "engine.mp3"},
                {"label": "MEDIC 1",  "freq_a": 712.0, "freq_b": 523.0, "sound": "medic.mp3"},
            ]
            tone_defs = []
            for i in range(1, tone_count + 1):
                dflt = defaults[i - 1] if i - 1 < len(defaults) else {"label": f"TONE {i}", "freq_a": 600.0, "freq_b": 400.0, "sound": ""}
                tone_defs.append({
                    "name":               data.get(f"tone_{i}_label",   dflt["label"]),
                    "tone1_hz":           float(data.get(f"tone_{i}_freq_a",  dflt["freq_a"])),
                    "tone2_hz":           float(data.get(f"tone_{i}_freq_b",  dflt["freq_b"])),
                    "tone1_duration":     float(data.get(f"tone_{i}_duration_a", 1.0)),
                    "tone2_duration":     float(data.get(f"tone_{i}_duration_b", 3.0)),
                    "threshold":          float(data.get(f"tone_{i}_threshold", 0.15)),
                    "auto_reset_seconds": int(data.get(f"tone_{i}_timeout", 60)),
                    "enabled":            True,
                    "sound_1":            data.get(f"tone_{i}_sound_1", dflt["sound"]),
                    "sound_2":            data.get(f"tone_{i}_sound_2", ""),
                    "sound_3":            data.get(f"tone_{i}_sound_3", ""),
                    "media_players":      _coerce_players(data.get(f"tone_{i}_media_players") or data.get(f"tone_{i}_media_player", "")),
                })

        for seq_data in tone_defs:
            # Hard limit: maximum 5 paging sequences
            if len(cm.get_sequences()) >= MAX_SEQUENCES:
                logger.warning("Sequence limit reached (5) — skipping additional sequences")
                break
            existing = cm.get_sequences()
            slug     = cm.name_to_slug(seq_data["name"])
            match    = next((s for s in existing if s["slug"] == slug), None)

            if match is None:
                # New sequence
                seq, err = cm.create_sequence(seq_data)
                if err:
                    logger.warning("Sequence create warning: %s", err)
                else:
                    ha.create_or_update_automation(seq)
                    logger.info("Created sequence + automation: %s", seq["name"])
            else:
                # Existing — update frequencies/durations in case user changed them
                updated, old, err = cm.update_sequence(match["id"], {**match, **seq_data})
                if err:
                    logger.warning("Sequence update warning: %s", err)
                else:
                    if updated["slug"] != old["slug"]:
                        ha.rename_automation(updated, old["ha_automation_id"])
                    else:
                        ha.create_or_update_automation(updated)
                    logger.info("Updated sequence + automation: %s", updated["name"])

        # ── 3. Save audio device index ────────────────────────────────────────
        try:
            device_index = int(data.get("audio_device") or -1)
        except (ValueError, TypeError):
            device_index = -1
        cm.save_runtime("audio_device_index", device_index)

        # ── 4. Copy alert sounds to /config/www/ and /media/ ─────────────────
        #    /config/www → available at /local/station_assistant/sounds/ in HA
        #    /media      → available in HA Media Browser under station_assistant/
        sounds_src = BASE_DIR / "sounds"
        sounds_dst_www   = Path("/config/www/station_assistant/sounds")
        sounds_dst_media = Path("/media/station_assistant")
        try:
            for dst in (sounds_dst_www, sounds_dst_media):
                dst.mkdir(parents=True, exist_ok=True)
                for snd in sounds_src.iterdir():
                    if snd.suffix.lower() in (".mp3", ".wav"):
                        shutil.copy2(snd, dst / snd.name)
            logger.info("Copied sounds to %s and %s", sounds_dst_www, sounds_dst_media)
        except Exception as e:
            logger.warning("Could not copy all sounds: %s", e)

        # ── 5. Reload HA automations so they appear in the UI ─────────────────
        ha.reload_automations()

        # ── 6. Mark setup complete ────────────────────────────────────────────
        SAConfig.mark_setup_complete()

        # ── 7. Restart decoder with new config ───────────────────────────────
        decoder.restart()

        logger.info("Setup activation complete for station: %s",
                    sa_cfg.get("station_name", "Station 1"))

        return jsonify({"status": "ok", "message": "Station Assistant activated"})

    except Exception as e:
        logger.error("Activation failed: %s", e, exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/setup/reset", methods=["POST"])
def api_reset():
    """Reset to first-run state."""
    try:
        decoder.stop()
        SAConfig.clear_setup()
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/setup/upload_logo", methods=["POST"])
def api_upload_logo():
    """Upload department logo/patch. Stored at /data/dept_logo.<ext>."""

    if "logo" not in request.files:
        return jsonify({"status": "error", "message": "No file provided"}), 400
    file = request.files["logo"]
    if not file or not file.filename:
        return jsonify({"status": "error", "message": "No file selected"}), 400
    allowed = {"image/png", "image/webp", "image/svg+xml", "image/jpeg"}
    if (file.content_type or "") not in allowed:
        return jsonify({"status": "error", "message": "Only PNG, WebP, JPEG, or SVG accepted"}), 400
    ext = Path(file.filename).suffix.lower()
    if ext not in {".png", ".webp", ".svg", ".jpg", ".jpeg"}:
        ext = ".png"
    for old in Path("/data").glob("dept_logo.*"):
        old.unlink(missing_ok=True)
    logo_path = Path("/data") / f"dept_logo{ext}"
    file.save(str(logo_path))
    logger.info("Department logo saved: %s", logo_path)
    return jsonify({"status": "ok", "filename": logo_path.name})


@app.route("/api/logo")
def api_logo():
    """Serve the stored department logo."""

    for ext in [".png", ".webp", ".jpg", ".jpeg", ".svg"]:
        p = Path("/data") / f"dept_logo{ext}"
        if p.exists():
            return send_file(str(p))
    return "", 404


@app.route("/api/setup/media_players")
def api_media_players():
    """Return all media_player.* entities from HA (for the setup wizard)."""
    try:
        states = ha._get("/states") or []
        entities = [
            {
                "entity_id":    s["entity_id"],
                "friendly_name": s.get("attributes", {}).get(
                    "friendly_name", s["entity_id"]
                ),
            }
            for s in states
            if isinstance(s, dict)
            and s.get("entity_id", "").startswith("media_player.")
        ]
        return jsonify({
            "status": "ok",
            "entities": sorted(entities, key=lambda x: x["friendly_name"].lower()),
        })
    except Exception as e:
        logger.error("api_media_players: %s", e)
        return jsonify({"status": "error", "entities": [], "message": str(e)})


@app.route("/api/setup/sounds")
def api_sounds_list():
    """Return sorted list of available MP3 sound files (bundled + uploaded)."""
    sounds: set = set()
    # Bundled sounds shipped with the addon
    for f in (BASE_DIR / "sounds").glob("*.mp3"):
        sounds.add(f.name)
    # Sounds uploaded by the user (mirrored to /media/station_assistant/)
    media_dir = Path("/media/station_assistant")
    if media_dir.exists():
        for f in media_dir.glob("*.mp3"):
            if not f.name.startswith("_"):  # skip internal files like _combined_alert.mp3
                sounds.add(f.name)
    return jsonify({"status": "ok", "sounds": sorted(sounds)})


@app.route("/api/setup/upload_sound", methods=["POST"])
def api_upload_sound():
    """Upload a custom sound file (MP3 or WAV).

    Re-encodes to 44.1kHz mono 128kbps MP3 on upload so all files in the
    library share the same format.  This guarantees instant stream-copy
    concatenation at alert time — no re-encoding delay.
    """
    if "sound" not in request.files:
        return jsonify({"status": "error", "message": "No file provided"}), 400
    file = request.files["sound"]
    if not file or not file.filename:
        return jsonify({"status": "error", "message": "No file selected"}), 400
    if not file.filename.lower().endswith((".mp3", ".wav")):
        return jsonify({"status": "error", "message": "Only MP3 and WAV files are accepted"}), 400

    # Strip path components and ensure .mp3 extension
    raw_name = Path(file.filename).stem
    filename = raw_name + ".mp3"
    safe_filename = Path(filename).name

    # Save the raw upload to a temp file
    tmp_raw = BASE_DIR / "sounds" / f"_upload_raw_{safe_filename}"
    file.save(str(tmp_raw))

    # Re-encode to normalized format (44.1kHz mono 128kbps)
    dst_bundled = BASE_DIR / "sounds" / safe_filename
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-y",
                "-i", str(tmp_raw),
                "-ar", "44100", "-ac", "1", "-b:a", "128k",
                str(dst_bundled),
            ],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            logger.error("upload_sound: ffmpeg re-encode failed: %s", result.stderr[-200:])
            tmp_raw.unlink(missing_ok=True)
            return jsonify({"status": "error", "message": "Failed to process audio file"}), 500
    except Exception as e:
        logger.error("upload_sound: re-encode error: %s", e)
        tmp_raw.unlink(missing_ok=True)
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        tmp_raw.unlink(missing_ok=True)

    # Mirror to /config/www/ and /media/
    for dst_dir in [
        Path("/config/www/station_assistant/sounds"),
        Path("/media/station_assistant"),
    ]:
        try:
            dst_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dst_bundled, dst_dir / safe_filename)
        except Exception as e:
            logger.warning("upload_sound: could not copy to %s: %s", dst_dir, e)

    logger.info("Sound uploaded and normalized: %s", safe_filename)
    return jsonify({"status": "ok", "filename": safe_filename})


@app.route("/api/sounds/<path:filename>")
def api_serve_sound(filename):
    """Serve a sound file directly so the browser can preview it."""
    safe_name = Path(filename).name
    bundled   = BASE_DIR / "sounds" / safe_name
    if bundled.exists():
        return send_file(str(bundled), mimetype="audio/mpeg")
    media = Path("/media/station_assistant") / safe_name
    if media.exists():
        return send_file(str(media), mimetype="audio/mpeg")
    return jsonify({"status": "error", "message": "Sound not found"}), 404


@app.route("/api/sounds/<path:filename>", methods=["DELETE"])
def api_delete_sound(filename):
    """Delete a custom sound file from the library."""
    safe_name = Path(filename).name
    if safe_name.startswith("_"):
        return jsonify({"status": "error", "message": "Cannot delete internal files"}), 400

    deleted = False
    for search_dir in [
        BASE_DIR / "sounds",
        Path("/config/www/station_assistant/sounds"),
        Path("/media/station_assistant"),
    ]:
        p = search_dir / safe_name
        if p.exists():
            p.unlink()
            deleted = True

    if deleted:
        logger.info("Sound deleted: %s", safe_name)
        return jsonify({"status": "ok"})
    return jsonify({"status": "error", "message": "Sound not found"}), 404


# ══════════════════════════════════════════════════════════════════════════════
# SETTINGS API
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/settings")
def api_settings_get():
    """Return current SA config + runtime options for the manage page settings tab."""
    cfg  = sa_config.load()
    opts = cm.get_options()
    return jsonify({"status": "ok", "config": cfg, "options": opts})


@app.route("/api/settings/save", methods=["POST"])
def api_settings_save():
    """Save SA config + update sequences + restart decoder."""
    try:
        data = request.get_json() or {}

        # ── Station-level + tone display config ───────────────────────────────
        # Merge partial update: load existing config first so we preserve
        # fields that aren't present in every settings form variant.
        existing_cfg = sa_config.load()
        new_cfg = dict(existing_cfg)
        # Always-writable scalar fields:
        scalar_keys = [
            "dept_name", "station_name", "weather_entity",
            "stack_window", "return_timeout", "multi_unit_sound", "multi_unit_color",
        ]
        for k in scalar_keys:
            if k in data:
                v = data[k]
                if k == "return_timeout":
                    try: v = int(v)
                    except (ValueError, TypeError): v = existing_cfg.get(k, 60)
                elif k == "stack_window":
                    try: v = float(v)
                    except (ValueError, TypeError): v = existing_cfg.get(k, 60)
                new_cfg[k] = v
        # Float fields
        if "page_sequence_gap" in data:
            try:    new_cfg["page_sequence_gap"] = float(data["page_sequence_gap"])
            except (ValueError, TypeError): new_cfg["page_sequence_gap"] = 3.0
        if "dupe_cooldown" in data:
            try:    new_cfg["dupe_cooldown"] = max(0.0, float(data["dupe_cooldown"]))
            except (ValueError, TypeError): new_cfg["dupe_cooldown"] = 120.0
        if "line_in_duration" in data:
            try:    new_cfg["line_in_duration"] = max(0.0, float(data["line_in_duration"]))
            except (ValueError, TypeError): new_cfg["line_in_duration"] = 120.0
        if "stream_base_url" in data:
            new_cfg["stream_base_url"] = str(data["stream_base_url"]).strip().rstrip("/")
            # Clear cached URL so the new value takes effect immediately
            ha._cached_stream_base = ""
        if "show_weather" in data:
            new_cfg["show_weather"] = bool(data["show_weather"])
        if "dashboard_audio" in data:
            new_cfg["dashboard_audio"] = bool(data["dashboard_audio"])
        if "live_pa_gain" in data:
            try:
                new_cfg["live_pa_gain"] = max(-20, min(40, int(data["live_pa_gain"])))
            except (ValueError, TypeError):
                new_cfg["live_pa_gain"] = 6
        sa_config.save(new_cfg)

        # ── Update tone sequences only if caller supplied tone_N_* fields ─────
        #    (settings page from manage.html does NOT send tone fields; they
        #     are managed exclusively through the sequence CRUD API.)
        seqs = cm.get_sequences()
        for i, seq in enumerate(seqs, start=1):
            prefix = f"tone_{i}_"
            if not any(k.startswith(prefix) for k in data):
                continue   # caller didn't send tone fields — skip
            patch = {**seq}
            if f"tone_{i}_label" in data:   patch["name"]               = data[f"tone_{i}_label"]
            if f"tone_{i}_freq_a" in data:  patch["tone1_hz"]           = float(data[f"tone_{i}_freq_a"])
            if f"tone_{i}_freq_b" in data:  patch["tone2_hz"]           = float(data[f"tone_{i}_freq_b"])
            if f"tone_{i}_timeout" in data: patch["auto_reset_seconds"] = int(data[f"tone_{i}_timeout"])
            updated, old, err = cm.update_sequence(seq["id"], patch)
            if err:
                logger.warning("Sequence update (tone %d): %s", i, err)
            elif updated["slug"] != old["slug"]:
                ha.rename_automation(updated, old["ha_automation_id"])
            else:
                ha.create_or_update_automation(updated)

        # ── Audio device ──────────────────────────────────────────────────────
        # Settings JS sends "audio_device"; some callers may send "audio_device_index".
        raw_device = data.get("audio_device_index") or data.get("audio_device", -1)
        try:
            device_index = int(raw_device)
        except (ValueError, TypeError):
            device_index = -1
        cm.save_runtime("audio_device_index", device_index)

        # ── Reload HA automations ─────────────────────────────────────────────
        ha.reload_automations()

        # ── Restart decoder with new config ───────────────────────────────────
        decoder.restart()

        return jsonify({"status": "ok", "message": "Settings saved"})
    except Exception as e:
        logger.error("Settings save: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


# ── Sequence CRUD (delegates to existing config_manager) ─────────────────────

@app.route("/api/sequences", methods=["GET"])
def api_sequences_list():
    return jsonify({"status": "ok", "sequences": cm.get_sequences()})


@app.route("/api/sequences", methods=["POST"])
def api_sequences_create():
    data = request.get_json() or {}
    # Hard limit: maximum 5 paging sequences
    if len(cm.get_sequences()) >= MAX_SEQUENCES:
        return jsonify({"status": "error",
                        "message": "Maximum of 5 paging sequences allowed."}), 400
    seq, err = cm.create_sequence(data)
    if err:
        return jsonify({"status": "error", "message": err}), 400
    ha.create_or_update_automation(seq)
    return jsonify({"status": "ok", "sequence": seq})


@app.route("/api/sequences/<seq_id>", methods=["PUT"])
def api_sequences_update(seq_id):
    data = request.get_json() or {}
    updated, old, err = cm.update_sequence(seq_id, data)
    if err:
        return jsonify({"status": "error", "message": err}), 400
    # Handle rename
    if updated["slug"] != old["slug"]:
        ha.rename_automation(updated, old["ha_automation_id"])
    else:
        ha.create_or_update_automation(updated)
    return jsonify({"status": "ok", "sequence": updated})


@app.route("/api/sequences/<seq_id>", methods=["DELETE"])
def api_sequences_delete(seq_id):
    deleted, err = cm.delete_sequence(seq_id)
    if err:
        return jsonify({"status": "error", "message": err}), 404
    ha.delete_automation(deleted["ha_automation_id"])
    return jsonify({"status": "ok"})


# ══════════════════════════════════════════════════════════════════════════════
# RUNTIME API
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/health")
def api_health():
    return jsonify({
        "status":          "ok",
        "version":         APP_VERSION,
        "setup_complete":  SAConfig.is_setup_complete(),
        "decoder_running": decoder.is_running,
    })


@app.route("/api/weather")
def api_weather():
    """Proxy weather entity from HA — dashboard never needs the token."""
    try:
        cfg    = sa_config.load()
        entity = cfg.get("weather_entity", "weather.home")
        state  = ha._get(f"/states/{entity}")
        if not state:
            return jsonify({"status": "error", "weather": None})
        attrs = state.get("attributes", {})
        # HA 2024.3+ removed forecast from state attributes.
        # Must use the get_forecasts response service with ?return_response=true.
        # Response shape: {"service_response": {"weather.home": {"forecast": [...]}}, "changed_states": []}
        forecast_raw = attrs.get("forecast", [])  # populated by older HA only
        if not forecast_raw:
            for fc_type in ("daily", "hourly"):
                try:
                    fc_resp = ha._post(
                        f"/services/weather/get_forecasts?return_response=true",
                        {"entity_id": entity, "type": fc_type},
                    )
                    if fc_resp and isinstance(fc_resp, dict):
                        svc_resp = fc_resp.get("service_response", fc_resp)
                        fc_data  = svc_resp.get(entity) or (
                            svc_resp.get(list(svc_resp.keys())[0]) if svc_resp else {}
                        )
                        forecast_raw = (fc_data or {}).get("forecast", [])
                    if forecast_raw:
                        break
                except Exception:
                    pass
        forecast = [
            {
                "label":       "NOW" if i == 0 else f"+{i}HR",
                "condition":   f.get("condition", state.get("state", "unknown")),
                "temperature": _round(f.get("temperature")),
            }
            for i, f in enumerate(forecast_raw[:4])
        ]
        # Derive today's high/low — daily forecasts have explicit templow;
        # hourly forecasts don't, so we take max/min across all entries.
        temp_high = temp_low = None
        if forecast_raw:
            if "templow" in forecast_raw[0]:      # daily forecast entry
                temp_high = _round(forecast_raw[0].get("temperature"))
                temp_low  = _round(forecast_raw[0].get("templow"))
            else:                                  # hourly — derive from range
                temps = [f.get("temperature") for f in forecast_raw if f.get("temperature") is not None]
                if temps:
                    temp_high = _round(max(temps))
                    temp_low  = _round(min(temps))
        weather = {
            "available":          True,
            "entity_id":          entity,
            "condition":          state.get("state", "unknown"),
            "temperature":        _round(attrs.get("temperature")),
            "temperature_unit":   attrs.get("temperature_unit", "°F"),
            "humidity":           _round(attrs.get("humidity")),
            "wind_speed":         _round(attrs.get("wind_speed")),
            "wind_bearing":       attrs.get("wind_bearing"),
            "wind_speed_unit":    attrs.get("wind_speed_unit", "mph"),
            "temp_high":          temp_high,
            "temp_low":           temp_low,
            "forecast":           forecast,
        }
        return jsonify({"status": "ok", "weather": weather})
    except Exception as e:
        logger.warning("Weather fetch: %s", e)
        return jsonify({"status": "error", "weather": None})


@app.route("/api/status")
def api_status():
    """Full system status for the status panel."""
    cfg  = sa_config.load()
    seqs = cm.get_sequences()
    opts = cm.get_options()
    detections = dl.get_recent_detections(limit=1)
    last = detections[0] if detections else None

    # Build tones list matching what status.html expects:
    # tones[0].label, tones[0].freq_a, tones[0].freq_b
    tones = [
        {
            "label":  s["name"],
            "freq_a": s["tone1_hz"],
            "freq_b": s["tone2_hz"],
            "slug":   s["slug"],
        }
        for s in seqs
    ]

    # Build last_alert from detection log record (DB column is seq_name, not name)
    last_alert = None
    if last:
        last_alert = {
            "label": last.get("seq_name") or last.get("name", "Unknown"),
            "time":  last.get("detected_at", ""),   # full ISO UTC: 2026-03-28T21:05:46Z
        }

    # Input level from decoder's last RMS reading
    rms = getattr(decoder, "_last_rms", 0.0) or 0.0
    input_level_dbfs = round(20 * math.log10(max(float(rms), 1e-6)), 1)

    ha_ok, ha_msg = ha.check_ha_connection()

    return jsonify({
        "status": "ok",
        "setup_complete": SAConfig.is_setup_complete(),
        "decoder": {
            "running":          decoder.is_running,
            "uptime_seconds":   decoder.uptime,
            "uptime":           _fmt_uptime(decoder.uptime),
            "error":            decoder.audio_error,
            "device":           str(opts.get("audio_device_index", -1)),
            "device_index":     opts.get("audio_device_index", -1),
            "sample_rate":      opts.get("sample_rate", 44100),
            "total_detections": decoder.total_detections,
            "last_healthy":     decoder.last_healthy,
            "input_level_dbfs": input_level_dbfs,
            # status.html expects these — populated from log
            "alerts_today":     None,
            "alerts_week":      None,
            "last_alert":       last_alert,
        },
        "sequences": seqs,
        "tones":     tones,
        "config":    cfg,
        "last_detection": last,
        "relay_remaining": round(stack_mgr.relay_remaining, 0),
        "ha": {
            "connected": ha_ok,
            "message":   ha_msg,
        },
    })


@app.route("/api/test/tone/<seq_id>", methods=["POST"])
def api_test_tone(seq_id):
    """
    Fire a test alert without needing audio input.
    Uses the existing ha_client.trigger_automation() which fires
    the two_tone_decoded event — triggers automations exactly like real audio.
    Also fires station_assistant_alert directly for the dashboard.
    """
    try:
        seq = cm.get_sequence(seq_id)
        if not seq:
            # Try matching by slug
            seqs = cm.get_sequences()
            seq  = next((s for s in seqs if s["slug"] == seq_id), None)
        if not seq:
            return jsonify({"status": "error", "message": "Sequence not found"}), 404

        # Fire two_tone_decoded (existing automation trigger)
        ha.trigger_automation(seq)

        # Also push directly to dashboard via stack manager
        stack_mgr.on_tone_detected(seq, confidence=1.0)

        # Log to DB so the Last Alert bar on the dashboard reflects the test
        dl.log_detection(seq, 1.0, _now_utc(), source="test")

        logger.info("Test tone fired: %s", seq["name"])
        return jsonify({"status": "ok", "message": f"Test alert: {seq['name']}"})
    except Exception as e:
        logger.error("Test tone: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/audio/level")
def api_audio_level():
    """Real-time input level for setup wizard calibration."""
    # We get RMS from the decoder's last emitted audio_level event
    # The SSE bus doesn't retain state, so we return the decoder's last_healthy
    # as a proxy for "is audio flowing" — actual level monitoring
    # happens via the SSE endpoint below.
    level = getattr(decoder, "_last_rms", 0.0)
    level_post = getattr(decoder, "_last_rms_post", 0.0)
    dbfs = 20 * math.log10(max(level, 1e-6)) if level > 0 else -96.0
    dbfs_post = 20 * math.log10(max(level_post, 1e-6)) if level_post > 0 else -96.0
    return jsonify({
        "status": "ok",
        "level_dbfs": round(dbfs, 1),
        "rms": round(float(level), 4),
        "level_dbfs_post": round(dbfs_post, 1),
        "rms_post": round(float(level_post), 4),
    })


@app.route("/api/audio/peak")
def api_audio_peak():
    """Real-time dominant frequency and magnitude from FFT peak detection."""
    return jsonify({
        "status": "ok",
        "freq": getattr(decoder, "_last_peak_freq", 0.0),
        "magnitude": getattr(decoder, "_last_peak_mag", 0.0),
    })


@app.route("/api/audio/gain", methods=["GET"])
def api_audio_gain_get():
    """Return current input gain (0-100)."""
    opts = cm.get_options()
    return jsonify({"status": "ok", "gain": opts.get("input_gain", 50)})


@app.route("/api/audio/gain", methods=["POST"])
def api_audio_gain_set():
    """Set input gain (0-100) and restart decoder."""
    try:
        data = request.get_json() or {}
        gain = max(0, min(100, int(data.get("gain", 50))))
        cm.save_runtime("input_gain", gain)
        decoder.restart()
        return jsonify({"status": "ok", "gain": gain})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/decoder/restart", methods=["POST"])
def api_decoder_restart():
    """Restart the audio decoder service."""
    try:
        decoder.restart()
        return jsonify({"status": "ok", "message": "Decoder restarted"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# ── SSE endpoint (kept for backwards compat with existing decoder UI) ─────────

@app.route("/api/stream")
def api_stream():
    """Server-Sent Events stream — real-time audio / detection updates."""
    client_queue = sse_bus.subscribe()

    def generate():
        try:
            # Push current decoder status immediately on connect
            initial = {
                "running": decoder.is_running,
                "error":   decoder.audio_error,
            }
            yield f"event: decoder_status\ndata: {json.dumps(initial)}\n\n"

            while True:
                try:
                    # Queue items are already formatted SSE strings
                    msg = client_queue.get(timeout=25)
                    yield msg
                except Exception:
                    yield ": keepalive\n\n"
        finally:
            sse_bus.unsubscribe(client_queue)

    return app.response_class(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control":     "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ── Live audio stream (Line In relay to media players) ────────────────────────

@app.route("/api/audio/live")
def api_audio_live():
    """Stream live Line In audio as MP3.

    Each media player device makes its own HTTP request, so each gets
    a dedicated subscriber queue with a full copy of the MP3 stream.
    """
    _live_transcoder.start()  # idempotent — no-op if already running
    client_q = _live_transcoder.subscribe()

    def generate():
        try:
            while True:
                try:
                    data = client_q.get(timeout=3.0)
                    yield data
                except queue.Empty:
                    if not _live_transcoder.running:
                        return
        except GeneratorExit:
            pass
        finally:
            _live_transcoder.unsubscribe(client_q)

    return app.response_class(
        generate(),
        mimetype="audio/mpeg",
        headers={
            "Cache-Control":       "no-store",
            "X-Accel-Buffering":   "no",
            "Connection":          "keep-alive",
            "icy-name":            "Station Assistant Line In",
        },
    )


# ── Browser Line In monitor (diagnostic, separate from Live PA) ───────────────

@app.route("/api/audio/monitor")
def api_audio_monitor():
    """Lightweight Line In stream for browser diagnostic listening.

    Completely separate from the Live PA transcoder — uses its own
    ffmpeg process and AudioStreamBus subscriber so it cannot interfere
    with media player streams.
    """
    sub_q = decoder.stream_bus.subscribe()
    sr = decoder.stream_bus.sample_rate

    # Read volume boost from config
    try:
        gain_db = int(sa_config.load().get("live_pa_gain", 6))
    except Exception:
        gain_db = 6

    cmd = [
        "ffmpeg",
        "-hide_banner", "-loglevel", "error",
        "-f", "s16le", "-ar", str(sr), "-ac", "1",
        "-i", "pipe:0",
    ]
    if gain_db != 0:
        cmd += ["-af", f"volume={gain_db}dB"]
    cmd += ["-b:a", "96k", "-f", "mp3", "pipe:1"]

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )

    mp3_q = queue.Queue(maxsize=200)
    stop = threading.Event()

    def feeder():
        try:
            while not stop.is_set():
                try:
                    chunk = sub_q.get(timeout=1.0)
                except queue.Empty:
                    chunk = b'\x00\x00' * 128
                try:
                    proc.stdin.write(chunk)
                    proc.stdin.flush()
                except (BrokenPipeError, OSError):
                    break
        except Exception:
            pass
        finally:
            try:
                proc.stdin.close()
            except Exception:
                pass

    def reader():
        try:
            while not stop.is_set():
                data = proc.stdout.read(4096)
                if not data:
                    break
                try:
                    mp3_q.put_nowait(data)
                except queue.Full:
                    try:
                        mp3_q.get_nowait()
                    except queue.Empty:
                        pass
                    mp3_q.put_nowait(data)
        except Exception:
            pass

    threading.Thread(target=feeder, daemon=True, name="mon-feed").start()
    threading.Thread(target=reader, daemon=True, name="mon-read").start()

    def generate():
        try:
            while True:
                try:
                    data = mp3_q.get(timeout=3.0)
                    yield data
                except queue.Empty:
                    if proc.poll() is not None:
                        return
        except GeneratorExit:
            pass
        finally:
            stop.set()
            decoder.stream_bus.unsubscribe(sub_q)
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    return app.response_class(
        generate(),
        mimetype="audio/mpeg",
        headers={
            "Cache-Control":     "no-store",
            "X-Accel-Buffering": "no",
            "Connection":        "keep-alive",
        },
    )


# ── Detection log API (existing) ──────────────────────────────────────────────

@app.route("/api/detections")
def api_detections():
    limit = int(request.args.get("limit", 100))
    return jsonify({
        "status": "ok",
        "detections": dl.get_recent_detections(limit),
        "total": dl.get_detection_count(),
    })


@app.route("/api/detections/clear", methods=["POST"])
def api_detections_clear():
    count = dl.clear_log()
    return jsonify({"status": "ok", "deleted": count})


# ══════════════════════════════════════════════════════════════════════════════
# WEBSOCKET EVENTS
# ══════════════════════════════════════════════════════════════════════════════

@socketio.on("connect")
def on_connect():
    logger.info("Dashboard client connected: %s", request.sid)
    # Send current config on connect
    cfg  = sa_config.load()
    seqs = cm.get_sequences()
    emit("config", {
        "station_name":    cfg.get("station_name", "Station 1"),
        "stack_window":    cfg.get("stack_window", 60),
        "return_timeout":  cfg.get("return_timeout", 45),
        "dashboard_audio": cfg.get("dashboard_audio", False),
        "sequences": [
            {
                "id":    s["id"],
                "slug":  s["slug"],
                "label": s["name"],
                "tone1_hz": s["tone1_hz"],
                "tone2_hz": s["tone2_hz"],
            }
            for s in seqs
        ],
    })


@socketio.on("disconnect")
def on_disconnect():
    logger.info("Dashboard client disconnected: %s", request.sid)


@socketio.on("request_weather")
def on_request_weather():
    """Dashboard requests fresh weather data via socket."""
    try:
        cfg    = sa_config.load()
        entity = cfg.get("weather_entity", "weather.home")
        state  = ha._get(f"/states/{entity}")
        if state:
            attrs = state.get("attributes", {})
            emit("weather", {
                "available":        True,
                "condition":        state.get("state", "unknown"),
                "temperature":      _round(attrs.get("temperature")),
                "temperature_unit": attrs.get("temperature_unit", "°F"),
                "humidity":         _round(attrs.get("humidity")),
                "wind_speed":       _round(attrs.get("wind_speed")),
                "wind_bearing":     attrs.get("wind_bearing"),
                "wind_speed_unit":  attrs.get("wind_speed_unit", "mph"),
                "apparent_temperature": _round(attrs.get("apparent_temperature")),
                "forecast":         [
                    {
                        "label":       "NOW" if i == 0 else f"+{i}HR",
                        "condition":   f.get("condition", state.get("state")),
                        "temperature": _round(f.get("temperature")),
                    }
                    for i, f in enumerate(attrs.get("forecast", [])[:4])
                ],
            })
    except Exception as e:
        logger.warning("Socket weather request: %s", e)


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _coerce_players(value) -> list:
    """Normalize any media player value to a clean list of entity ID strings."""
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _round(val):
    if val is None:
        return None
    try:
        return round(float(val), 1)
    except (TypeError, ValueError):
        return None


def _fmt_uptime(seconds: float) -> str:
    if not seconds:
        return "0s"
    h, rem = divmod(int(seconds), 3600)
    m, s   = divmod(rem, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


# ══════════════════════════════════════════════════════════════════════════════
# STARTUP
# ══════════════════════════════════════════════════════════════════════════════

def _normalize_sound_file(path: Path) -> bool:
    """Re-encode a single sound file to 44.1kHz/mono/128k if needed.

    Returns True if the file was re-encoded, False if already normalized.
    """
    if path.name.startswith("_"):
        return False
    sr = ha._get_mp3_sample_rate(path)
    if sr == 44100:
        return False
    tmp = path.with_suffix(".tmp.mp3")
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-y", "-i", str(path),
                "-ar", "44100", "-ac", "1", "-b:a", "128k",
                str(tmp),
            ],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            tmp.replace(path)
            return True
        tmp.unlink(missing_ok=True)
    except Exception as e:
        logger.warning("Normalize sound %s failed: %s", path.name, e)
        tmp.unlink(missing_ok=True)
    return False


def _normalize_all_sounds():
    """Normalize all sound files across bundled and media directories.

    Scans both the bundled sounds dir and /media/station_assistant/ for
    files that aren't 44.1kHz MP3 and re-encodes them. Also converts
    WAV files to normalized MP3. This handles:
    - Bundled sounds on first install (including WAV files)
    - Files uploaded via HA Media Browser (not through our UI)
    """
    count = 0
    for search_dir in [BASE_DIR / "sounds", Path("/media/station_assistant")]:
        if not search_dir.exists():
            continue
        # Convert WAV files to MP3
        for wav in list(search_dir.glob("*.wav")):
            if wav.name.startswith("_"):
                continue
            mp3_path = wav.with_suffix(".mp3")
            try:
                result = subprocess.run(
                    [
                        "ffmpeg", "-y", "-i", str(wav),
                        "-ar", "44100", "-ac", "1", "-b:a", "128k",
                        str(mp3_path),
                    ],
                    capture_output=True, text=True, timeout=30,
                )
                if result.returncode == 0:
                    wav.unlink()  # remove original WAV
                    count += 1
                    logger.info("Converted WAV to MP3: %s", mp3_path.name)
            except Exception as e:
                logger.warning("WAV conversion failed for %s: %s", wav.name, e)
        # Normalize existing MP3 files
        for mp3 in list(search_dir.glob("*.mp3")):
            if _normalize_sound_file(mp3):
                count += 1

    if count > 0:
        logger.info("Normalized %d sound file(s) to 44.1kHz/mono/128k", count)
        # Mirror bundled sounds to /media/ and /config/www/
        sounds_dir = BASE_DIR / "sounds"
        for dst_dir in [
            Path("/config/www/station_assistant/sounds"),
            Path("/media/station_assistant"),
        ]:
            try:
                dst_dir.mkdir(parents=True, exist_ok=True)
                for mp3 in sounds_dir.glob("*.mp3"):
                    if not mp3.name.startswith("_"):
                        shutil.copy2(mp3, dst_dir / mp3.name)
            except Exception as e:
                logger.warning("Mirror sounds to %s failed: %s", dst_dir, e)


def startup():
    logger.info("═══════════════════════════════════════")
    logger.info("  Station Assistant  v%s", APP_VERSION)
    logger.info("  Home Assistant Add-on")
    logger.info("  Two-Tone Decoder: goertzel.py (NumPy)")
    logger.info("═══════════════════════════════════════")

    # Init SQLite detection log
    dl.init_db()
    logger.info("Detection log database ready")

    # Normalize all sound files to 44.1kHz/mono/128k.
    # This ensures stream-copy concatenation always works at alert time.
    # Also catches files uploaded via HA Media Browser.
    _normalize_all_sounds()

    # Start decoder if already configured
    if SAConfig.is_setup_complete():
        cfg = sa_config.load()
        logger.info("Resuming decoder for station: %s",
                    cfg.get("station_name", "Unknown"))
        decoder.start()
        logger.info("Decoder started")
    else:
        logger.info("First run — setup wizard will be served at /setup")

    # Log HA connectivity
    ok, msg = ha.check_ha_connection()
    if ok:
        logger.info("HA connection: %s", msg)
        # Push initial sensor states so they exist in HA immediately on install
        ha.push_watchdog_sensor(app_version=APP_VERSION)
        ha.push_decoder_sensor(
            "running" if SAConfig.is_setup_complete() else "stopped",
            error=""
        )
    else:
        logger.warning("HA connection failed: %s — events will retry", msg)


if __name__ == "__main__":
    startup()
    socketio.run(
        app,
        host="0.0.0.0",
        port=8099,
        debug=False,
        use_reloader=False,
        log_output=False,
        allow_unsafe_werkzeug=True,
    )
else:
    # Running under gunicorn — call startup() at import time
    startup()
