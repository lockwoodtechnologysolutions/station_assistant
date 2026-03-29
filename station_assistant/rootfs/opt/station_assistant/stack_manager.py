"""
stack_manager.py
Stacked dispatch logic for Station Assistant.

Sits between the DecoderService callback and the dashboard/HA event layer.
When multiple tone sequences are decoded in quick succession (e.g. Engine 1
then Medic 1 on the same incident), this manager accumulates them into a
single incident stack and fires a unified station_assistant_alert event.

Architecture:
  DecoderService._on_detection()
        ↓ calls
  StackManager.on_tone_detected(seq, confidence)
        ↓ fires (via callbacks)
  → HA event: station_assistant_alert  (Station Assistant unified event)
  → HA event: two_tone_decoded         (existing event — preserved for compatibility)
  → SocketIO broadcast to dashboard
"""

import time
import threading
import logging
from datetime import datetime, timezone
from typing import Callable, Optional

import ha_client as ha

logger = logging.getLogger(__name__)


class StackManager:
    """
    Accumulates decoded tone sequences into incident stacks.

    Config values (read from sa_config):
        stack_window    — seconds after first tone to accept additional units
        return_timeout  — seconds after window closes before returning to idle

    Callbacks registered via set_alert_callback() and set_idle_callback()
    are invoked on the decoder thread, so they must be non-blocking.
    """

    def __init__(self, sa_config):
        self.sa_config = sa_config
        self._alert_cb: Optional[Callable] = None
        self._idle_cb:  Optional[Callable] = None

        # Stack state
        self._stack: list = []
        self._stack_open:  bool = False
        self._window_timer: Optional[threading.Timer] = None
        self._return_timer: Optional[threading.Timer] = None
        self._gap_timer:    Optional[threading.Timer] = None
        self._incident_start: Optional[float] = None
        self._last_detection: dict = {}    # seq_id → timestamp of last confirmed detection

    # ── Public Interface ───────────────────────────────────────────────────

    def set_alert_callback(self, fn: Callable) -> None:
        """Register function called with alert payload on each stack change."""
        self._alert_cb = fn

    def set_idle_callback(self, fn: Callable) -> None:
        """Register function called when the board returns to idle."""
        self._idle_cb = fn

    def on_tone_detected(self, seq: dict, confidence: float) -> None:
        """
        Called by the decoder when a tone sequence is confirmed.
        seq is a full sequence dict from sequences.json.
        """
        now = time.time()
        ts  = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        # Global duplicate cooldown — suppress if this exact sequence was recently decoded
        _dupe_cd = float(self.sa_config.load().get("dupe_cooldown", 120.0))
        if _dupe_cd > 0:
            _last = self._last_detection.get(seq["id"], 0.0)
            if now - _last < _dupe_cd:
                logger.info(
                    "Duplicate suppressed: %s (%.0fs ago, cooldown=%.0fs)",
                    seq["name"], now - _last, _dupe_cd,
                )
                return
        self._last_detection[seq["id"]] = now

        unit = {
            "seq_id":              seq["id"],
            "tone_id":             seq["slug"],
            "label":               seq["name"],
            "slug":                seq["slug"],
            "tone1_hz":            seq["tone1_hz"],
            "tone2_hz":            seq["tone2_hz"],
            "confidence":          round(confidence, 3),
            "time":                ts,
            "seq":                 len(self._stack) + 1,
            # Audio fields needed by _play_audio
            "sound_1":         seq.get("sound_1", ""),
            "sound_2":         seq.get("sound_2", ""),
            "sound_3":         seq.get("sound_3", ""),
            # media_players is the authoritative list; fall back to legacy field
            "media_players":   seq.get("media_players") or (
                [seq["media_player_entity"]] if seq.get("media_player_entity") else []
            ),
            # Kiosk display color
            "alert_color":         seq.get("alert_color", "#8b1a1a"),
            # Kiosk display icon (emoji character, empty = auto-detect from label)
            "icon":                seq.get("icon", ""),
        }

        cfg = self.sa_config.load()
        stack_window   = float(cfg.get("stack_window",      60))
        return_timeout = float(cfg.get("return_timeout",    45))
        page_gap       = float(cfg.get("page_sequence_gap", 3.0))

        if not self._stack:
            # ── New incident ───────────────────────────────────────────────
            self._stack          = [unit]
            self._stack_open     = True
            self._incident_start = now
            self._start_window_timer(stack_window)
            logger.info("New incident: %s (confidence=%.2f)", seq["name"], confidence)
            self._fire_dashboard(return_timeout)
            self._reset_gap_timer(page_gap)

        elif self._stack_open:
            # ── Add to existing stack ──────────────────────────────────────
            already = any(u["seq_id"] == seq["id"] for u in self._stack)
            if not already:
                self._stack.append(unit)
                logger.info("Stacked: %s (total %d units)", seq["name"], len(self._stack))
                self._fire_dashboard(return_timeout)
            else:
                logger.debug("Duplicate ignored: %s", seq["name"])
            # Reset timers — more tones may follow
            self._reset_window_timer(stack_window)
            self._reset_gap_timer(page_gap)

        else:
            # ── Stack window closed — new incident ─────────────────────────
            self._cancel_timers()
            self._stack          = [unit]
            self._stack_open     = True
            self._incident_start = now
            self._start_window_timer(stack_window)
            logger.info("New incident (after closed window): %s", seq["name"])
            self._fire_dashboard(return_timeout)
            self._reset_gap_timer(page_gap)

    def force_idle(self) -> None:
        """Manually return to idle (e.g. disarm button)."""
        self._go_idle()

    # ── Internal ───────────────────────────────────────────────────────────

    def _fire_dashboard(self, return_timeout: float) -> None:
        """Update the dashboard immediately on every tone decode. HA event fires after gap."""
        if not self._stack:
            return

        cfg              = self.sa_config.load()
        multi_unit_color = (cfg.get("multi_unit_color", "#1a4a8b") or "#1a4a8b").strip()
        is_multi         = len(self._stack) > 1
        alert_color      = multi_unit_color if is_multi else (self._stack[0].get("alert_color", "#8b1a1a") or "#8b1a1a")
        stack_window     = float(cfg.get("stack_window", 60))

        payload = {
            "event":            "alert",
            "tone_id":          self._stack[0]["tone_id"],
            "slug":             self._stack[0]["slug"],
            "unit_label":       self._stack[0]["label"],
            "stack":            list(self._stack),
            "unit_count":       len(self._stack),
            "timestamp":        self._stack[0]["time"],
            "stack_open":       self._stack_open,
            "stack_window":     stack_window,
            "return_timeout":   return_timeout,
            "alert_color":      alert_color,
            "multi_unit_color": multi_unit_color,
        }

        if self._alert_cb:
            try:
                self._alert_cb(payload)
            except Exception as e:
                logger.error("Alert callback error: %s", e)

    def _on_gap_expired(self) -> None:
        """
        Called when the Page Sequence Gap timer fires.
        All tones in this page sequence have now been received.
        Fires station_assistant_alert with full multi-unit context,
        then starts the audio playback thread.
        """
        if not self._stack:
            return

        cfg              = self.sa_config.load()
        return_timeout   = float(cfg.get("return_timeout",   45))
        multi_unit_sound = (cfg.get("multi_unit_sound", "") or "").strip()
        stack_snapshot   = list(self._stack)
        is_multi         = len(stack_snapshot) > 1

        payload = {
            "event":          "alert",
            "tone_id":        stack_snapshot[0]["tone_id"],
            "slug":           stack_snapshot[0]["slug"],
            "unit_label":     stack_snapshot[0]["label"],
            "stack":          stack_snapshot,
            "unit_count":     len(stack_snapshot),
            "timestamp":      stack_snapshot[0]["time"],
            "stack_open":     self._stack_open,
            "return_timeout": return_timeout,
            "is_multi_unit":  is_multi,
        }

        try:
            ha._post("/events/station_assistant_alert", payload)
            logger.info(
                "station_assistant_alert fired (gap expired): %s (%d unit%s)",
                payload["unit_label"], len(stack_snapshot),
                "s" if is_multi else "",
            )
        except Exception as e:
            logger.error("Failed to fire station_assistant_alert: %s", e)

        # Spawn audio thread — non-blocking
        threading.Thread(
            target=self._play_audio,
            args=(stack_snapshot, is_multi, multi_unit_sound),
            daemon=True,
            name="sa-audio",
        ).start()

    def _play_and_wait(self, players: list, sound: str) -> None:
        """Play a sound file and wait for it to finish."""
        duration = ha.get_sound_duration(sound)
        if duration:
            logger.debug("File %s duration: %.2fs", sound, duration)
        ha.play_sound(players, sound)
        ha.wait_until_idle(players[0], known_duration=duration)

    def _collect_sounds(self, slots: list[str], unit: dict) -> list[str]:
        """Return list of non-empty sound filenames from a unit dict."""
        sounds = []
        for slot in slots:
            sound = (unit.get(slot) or "").strip()
            if sound:
                sounds.append(sound)
        return sounds

    def _play_audio(self, stack: list, is_multi: bool, multi_unit_sound: str) -> None:
        """
        Background thread — plays alert audio via direct HA REST API calls.

        Concatenates all sound files into a single MP3 before playing,
        so the media player only buffers once.  Falls back to sequential
        playback if concatenation fails.
        """
        try:
            if is_multi:
                entities = []
                for unit in stack:
                    for e in unit.get("media_players", []):
                        e = e.strip()
                        if e and e not in entities:
                            entities.append(e)

                if not entities:
                    logger.debug("No media players configured — skipping audio")
                    return

                all_sounds = []
                if multi_unit_sound:
                    all_sounds.append(multi_unit_sound)
                for unit in stack:
                    all_sounds.extend(self._collect_sounds(
                        ["sound_2", "sound_3"], unit,
                    ))

                if self._play_combined(entities, all_sounds):
                    return

                # Fallback: play files individually
                if multi_unit_sound:
                    self._play_and_wait(entities, multi_unit_sound)
                    logger.info("Multi-unit ramp-up complete: %s", multi_unit_sound)
                for unit in stack:
                    players = unit.get("media_players", [])
                    if not players:
                        continue
                    for sound in self._collect_sounds(["sound_2", "sound_3"], unit):
                        self._play_and_wait(players, sound)
                        logger.info("Played apparatus tone: %s → %s", sound, players)

            else:
                unit    = stack[0]
                players = unit.get("media_players", [])
                if not players:
                    logger.debug("No media player configured — skipping audio")
                    return

                all_sounds = self._collect_sounds(
                    ["sound_1", "sound_2", "sound_3"], unit,
                )

                if self._play_combined(players, all_sounds):
                    return

                # Fallback: play files individually
                for sound in all_sounds:
                    self._play_and_wait(players, sound)
                    logger.info("Played: %s → %s", sound, players)

        except Exception as e:
            logger.error("Audio playback thread error: %s", e)
        finally:
            ha.cleanup_combined_sound()

    def _play_combined(self, players: list, sounds: list[str]) -> bool:
        """Concatenate sounds into one file, play it, and wait."""
        if len(sounds) < 2:
            return False

        combined = ha.concatenate_sounds(sounds)
        if not combined:
            return False

        duration = ha.get_sound_duration(combined)
        logger.info(
            "Playing combined alert (%d files, %.1fs): %s → %s",
            len(sounds), duration or 0, sounds, players,
        )
        ha.play_sound(players, combined)
        ha.wait_until_idle(players[0] if isinstance(players, list) else players,
                           known_duration=duration)
        return True

    def _go_idle(self) -> None:
        """Clear stack, cancel timers, invoke idle callback."""
        self._cancel_timers()
        self._stack          = []
        self._stack_open     = False
        self._incident_start = None
        logger.info("Returning to idle")

        # Fire HA idle event
        try:
            ha._post("/events/station_assistant_alert", {"event": "idle"})
        except Exception as e:
            logger.error("Failed to fire idle event: %s", e)

        if self._idle_cb:
            try:
                self._idle_cb({"event": "idle", "stack": []})
            except Exception as e:
                logger.error("Idle callback error: %s", e)

    def _reset_gap_timer(self, gap: float) -> None:
        """Reset the Page Sequence Gap timer. Fires _on_gap_expired when it expires."""
        if self._gap_timer:
            self._gap_timer.cancel()
        self._gap_timer = threading.Timer(gap, self._on_gap_expired)
        self._gap_timer.daemon = True
        self._gap_timer.start()
        logger.debug("Page sequence gap timer reset (%.1fs)", gap)

    def _start_window_timer(self, window: float) -> None:
        if self._window_timer:
            self._window_timer.cancel()
        self._window_timer = threading.Timer(window, self._window_closed)
        self._window_timer.daemon = True
        self._window_timer.start()
        logger.debug("Stack window opened (%.0fs)", window)

    def _reset_window_timer(self, window: float) -> None:
        self._start_window_timer(window)

    def _window_closed(self) -> None:
        self._stack_open = False
        logger.info("Stack window closed. Units: %s",
                    [u["label"] for u in self._stack])
        cfg = self.sa_config.load()
        timeout = float(cfg.get("return_timeout", 45))
        if self._return_timer:
            self._return_timer.cancel()
        self._return_timer = threading.Timer(timeout, self._go_idle)
        self._return_timer.daemon = True
        self._return_timer.start()
        logger.debug("Return timer started (%.0fs)", timeout)

    def _cancel_timers(self) -> None:
        if self._gap_timer:
            self._gap_timer.cancel()
            self._gap_timer = None
        if self._window_timer:
            self._window_timer.cancel()
            self._window_timer = None
        if self._return_timer:
            self._return_timer.cancel()
            self._return_timer = None
