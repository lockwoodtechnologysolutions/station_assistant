# Station Assistant

**Fire Station Pager and Radio Alerting & Management Platform for Home Assistant**

Station Assistant is a free, open-source Home Assistant addon that brings commercial-grade fire station pager and radio alerting capabilities to your department. Decode two-tone pages, monitor radio traffic, and automate station responses — all with a local-first architecture that operates completely offline after installation. No cloud dependencies, no recurring costs, and full data sovereignty.

Every department deserves professional station alerting — not just those with big budgets. Station Assistant brings enterprise-level capabilities to volunteer and small career departments without the enterprise price tag.

---

## Features

### Two-Tone Decoder
- Real-time Goertzel algorithm decodes A/B sequential tone pairs from a USB audio device
- Configurable frequency tolerance, detection threshold, and input gain (0-20x, default 1.0x unity)
- Dual VU meters: pre-gain (true hardware input level) and post-gain (what the decoder analyzes)
- Duplicate cooldown prevents repeated triggers from the same page
- Adjustable page sequence gap for tone completion before triggering audio

### Alert Dashboard
- Full-screen kiosk-style display, accessible without Home Assistant login (port 8099)
- Shows dispatched unit name, icon, and custom alert color
- Multi-unit stacking — multiple tones in a configurable window stack on one screen
- Single-unit dispatches start the return timer immediately (no waiting for stack window)
- Elapsed timer and synchronized countdown return-to-idle bar
- Dark / light mode toggle

### Gapless Audio Playback
- Sound files (ramp-up + apparatus tones) are concatenated into a single MP3 via ffmpeg before playback
- Eliminates buffering delays between files on network media players (LinkPlay, Arylic, Sonos, etc.)
- Supports both MP3 and WAV sound file uploads
- All files re-encoded to 44.1kHz for universal compatibility

### Live PA (Line In Audio Relay)
- After alert sounds finish, relays live Line In audio to media players via MP3 stream
- Configurable relay duration (30s to 5 minutes, or disabled)
- Pre-warms the ffmpeg transcoder during alert sound playback for minimal delay
- Configurable Stream Base URL for network routing

### Weather Integration
- Pulls live conditions from any Home Assistant weather entity
- Displays temperature, condition, humidity, wind, hi/lo, and 4-hour forecast
- Can be enabled/disabled in settings

### Paging Sequences
- Supports 5 paging sequences, each with:
  - Tone A + Tone B frequency pair
  - Up to 3 audio files (ramp-up, apparatus tone 1, apparatus tone 2)
  - Route audio to any HA media player(s)
  - Custom alert color and display icon
  - Individual detection threshold and auto-reset timer

### Home Assistant Integration
- Fires `two_tone_decoded` and `station_assistant_alert` events for automations
- Auto-creates and manages HA automations for each sequence
- Pushes live HA sensor entities:
  - `sensor.station_assistant_decoder` — decoder status
  - `sensor.station_assistant_watchdog` — 60-second heartbeat
- Direct link to each sequence's HA automation from the Sequences table

### Audio Monitor
- Dual VU meters: Input Level (pre-gain) and Decoder Level (post-gain)
- Live Goertzel frequency cards showing per-frequency signal strength
- Peak frequency detection via FFT
- Visual card flash on tone detection

### Detection Log
- SQLite-backed history of all decoded pages
- Configurable retention (default 30 days)

---

## Installation

1. In Home Assistant, go to **Settings > Add-ons > Add-on Store**
2. Click the menu > **Repositories** > add this repo URL:
   ```
   https://github.com/lockwoodtechnologysolutions/station_assistant
   ```
3. Find **Station Assistant** in the store and click **Install**
4. Start the addon and open the UI via **Ingress**

---

## Configuration

These options are set in the addon's **Configuration** tab:

| Option | Default | Description |
|--------|---------|-------------|
| `audio_device_index` | `-1` | Index of the USB audio input device. `-1` for auto-detect. |
| `sample_rate` | `44100` | Audio sample rate in Hz. |
| `chunk_size` | `2048` | Audio buffer chunk size. |
| `input_gain` | `5` | Input gain slider (0-100). Maps to 0.0x-20.0x gain. Default 5 = 1.0x unity gain. |
| `log_retention_days` | `30` | Days to keep detection history. |

Station-level settings (department name, weather, sounds, Live PA, timing) are configured through the **Settings** tab in the web UI.

---

## Hardware Requirements

- A USB audio input device (generic USB sound card)
- Your station's scanner or radio receiver connected to the audio input
- Home Assistant OS or Supervised installation
- Network media players (Sonos, LinkPlay/Arylic, Google Cast, etc.) for audio playback

---

## Ports

| Port | Description |
|------|-------------|
| `8099` | Alert Dashboard — accessible directly without HA authentication at `http://your-ha-ip:8099/dashboard` |

The management UI (Sequences, Audio Monitor, Detection Log, Settings) is accessed via Home Assistant Ingress and requires HA authentication.

---

## Enterprise Options

Station Assistant is the free, open-source foundation of our alerting platform. For departments requiring multi-station deployments, advanced response tracking, device management, commercial support, or enterprise reporting — learn more at https://www.lockwood.tech or email info@lockwood.tech
