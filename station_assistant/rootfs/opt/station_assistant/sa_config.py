"""
sa_config.py
Station Assistant configuration manager.
Stores station-level settings that are separate from tone sequences
(which live in /data/sequences.json managed by config_manager.py).

This file manages:
  - Station name
  - Weather entity
  - Stack window / return timeout
  - Display preferences
  - Setup completion flag

Stored at /data/sa_config.json
"""

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

SA_CONFIG_PATH = Path("/data/sa_config.json")
SETUP_FLAG     = Path("/data/sa_setup_complete")

DEFAULTS = {
    "dept_name":         "",
    "station_name":      "Station 1",
    "weather_entity":    "weather.home",
    "stack_window":      60,
    "return_timeout":    45,
    "page_sequence_gap": 3.0,   # seconds to wait after last tone before playing audio
    "dupe_cooldown":     120,   # seconds before the same sequence is accepted again (0 = off)
    "multi_unit_sound":  "",    # filename for multi-unit ramp-up tone (global, station-wide)
    "multi_unit_color":  "#1a4a8b",  # kiosk border/card color for multi-unit dispatches
    "show_weather":      True,   # show weather card on idle dashboard
    "line_in_duration":  120,    # seconds to relay Line In audio to media players after alert sounds (0 = disabled)
    # Tone 1 (primary — Engine/Primary unit)
    "tone_1_label":    "ENGINE 1",
    "tone_1_freq_a":   688.8,
    "tone_1_freq_b":   440.0,
    "tone_1_tolerance": 20,
    "tone_1_sound":    "engine.mp3",
    "tone_1_timeout":  60,
    # Tone 2 (secondary — Medic/Support unit)
    "tone_2_label":    "MEDIC 1",
    "tone_2_freq_a":   712.0,
    "tone_2_freq_b":   523.0,
    "tone_2_tolerance": 20,
    "tone_2_sound":    "medic.mp3",
    "tone_2_timeout":  60,
}


class SAConfig:

    def load(self) -> dict:
        """Load SA config, filling gaps with defaults."""
        if not SA_CONFIG_PATH.exists():
            return dict(DEFAULTS)
        try:
            with open(SA_CONFIG_PATH) as f:
                stored = json.load(f)
            cfg = dict(DEFAULTS)
            cfg.update(stored)
            return cfg
        except Exception as e:
            logger.error("Failed to load sa_config.json: %s", e)
            return dict(DEFAULTS)

    def save(self, data: dict) -> bool:
        """Save SA config."""
        try:
            SA_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            cfg = dict(DEFAULTS)
            cfg.update(data)
            with open(SA_CONFIG_PATH, "w") as f:
                json.dump(cfg, f, indent=2)
            return True
        except Exception as e:
            logger.error("Failed to save sa_config.json: %s", e)
            return False

    def get(self, key: str, default=None):
        return self.load().get(key, default)

    @staticmethod
    def is_setup_complete() -> bool:
        return SETUP_FLAG.exists()

    @staticmethod
    def mark_setup_complete():
        SETUP_FLAG.touch()

    @staticmethod
    def clear_setup():
        if SETUP_FLAG.exists():
            SETUP_FLAG.unlink()
