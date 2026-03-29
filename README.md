# Station Assistant

<a href="https://www.buymeacoffee.com/lockwoodtechnology" target="_blank"><img src="https://cdn.buymeacoffee.com/buttons/v2/default-yellow.png" alt="Buy Me A Coffee" style="height: 60px !important;width: 217px !important;" ></a>

**A Home Assistant addon for fire station two-tone paging decoder and alert display.**

Fire Station Pager and Radio Alerting & Management Platform for Home Assistant

Station Assistant is a free, open-source Home Assistant addon that brings commercial-grade fire station pager and radio alerting capabilities to your department. Decode two-tone pages, monitor radio traffic, and automate station responses—all with a local-first architecture that operates completely offline after installation. No cloud dependencies, no recurring costs, and full data sovereignty.

Every department deserves professional station alerting—not just those with big budgets. Station Assistant brings enterprise-level capabilities to volunteer and small career departments without the enterprise price tag. Build a complete station automation system using affordable, off-the-shelf home automation hardware that just works.

![Version](https://img.shields.io/badge/version-1.3.2-blue) ![HA Addon](https://img.shields.io/badge/Home%20Assistant-Addon-41BDF5) ![Architectures](https://img.shields.io/badge/arch-amd64%20%7C%20aarch64%20%7C%20armv7%20%7C%20armhf-lightgrey)

---

## Features

### 📟 Two-Tone Decoder
- Real-time Goertzel algorithm decodes A/B sequential tone pairs from a USB audio device (radio, pager, scanner audio source)
- Configurable frequency tolerance, detection threshold, and input gain (0–20×)
- Duplicate cooldown prevents repeated triggers from the same page
- Adjustable page sequence gap — waits for tone completion before triggering audio

<img width="1788" height="1032" alt="image" src="https://github.com/user-attachments/assets/526c0c1e-1581-4d59-9cc6-b5158dbc38ef" />


### 🚨 Alert Dashboard
- Full-screen kiosk-style display, accessible without Home Assistant login (direct on port 8099)
- Available via any browser on same network at http://x.x.x.x:8099/dashboard
- Perfect for large video displays in watch office, day room, apparatus bay, etc
- Shows dispatched unit name, icon, and custom alert color
- Multi-unit stacking — multiple tones in a configurable window stack on one screen
- Elapsed timer and countdown return-to-idle bar
- Dark / light mode toggle (preference saved in browser)

Standby Dashboard

Single Unit Page
<img width="1432" height="1232" alt="image" src="https://github.com/user-attachments/assets/c6209b11-93c3-4a46-866f-70d4e310f66b" />

Stacked Pages (Multi Apparatus Call)
<img width="1435" height="1232" alt="image" src="https://github.com/user-attachments/assets/d41a42e3-03f1-4ac2-a029-a1f4ba326f2c" />

Visual Indication When Offline
<img width="1428" height="532" alt="image" src="https://github.com/user-attachments/assets/6f534336-e961-458c-9fbf-c2e34b44bee9" />

### 🌤️ Weather Integration
- Pulls live conditions from any Home Assistant weather entity
- Displays temperature, condition, humidity, wind, hi/lo, and 4-hour forecast
- Can be enabled/disabled in settings

### 🔔 Paging Sequences
- Supports 5 Paging Sequences, each with:
  - Tone A + Tone B frequency pair
  - Up to 3 audio files (ramp-up, apparatus tone 1, apparatus tone 2)
  - HA media players to play audio on
  - Custom alert color and display icon
  - Individual detection threshold and auto-reset timer
- "All-Call" support — single continuous long tone

<img width="2117" height="795" alt="image" src="https://github.com/user-attachments/assets/0e70487a-e7cd-4091-a510-11b5a7cd40b2" />

### 🏠 Home Assistant Integration
- Fires `two_tone_decoded` events on detection for use in automations
- Auto-creates and manages HA automations for each sequence
- Pushes two live HA sensor entities:
  - `sensor.station_assistant_decoder` — decoder status (listening / error / stopped)
  - `sensor.station_assistant_watchdog` — 60-second heartbeat with app version
- Direct link to each sequence's HA automation from the Sequences table

<img width="2116" height="647" alt="image" src="https://github.com/user-attachments/assets/f8d5f5e2-ae34-499f-8c4e-397bc4826c73" />

**Benefits of Home Assistant Integration:**

- Massive device ecosystem - <a href="https://www.home-assistant.io/integrations/?brands=featured">Over 2,000+ integrations available</a> out of the box; if a device has an API or speaks a standard protocol (Z-Wave, Zigbee, MQTT, WiFi), Home Assistant can control it 
- No vendor lock-in - Mix and match devices from different manufacturers; not forced to buy everything from one brand or pay recurring subscriptions to multiple vendors
- Local control by design - All automation logic runs on your local network; devices work even when internet is down, no cloud outages can take down your station alerting
- Use hardware you already own - Already have smart switches, thermostats, or cameras? Integrate them immediately instead of ripping out working equipment
- Consumer pricing, commercial capability - Buy devices from Home Depot, Amazon, or Best Buy at consumer prices instead of paying enterprise markup for "fire station certified" equipment
- Future-proof your investment - When new devices come out, add them to your existing system; not stuck waiting for your vendor to support them (or paying upgrade fees)
- Powerful automation without programming - Visual automation editor and YAML configuration let you build complex logic without hiring developers
- Active open-source community - Thousands of developers constantly improving integrations, fixing bugs, and adding features at no cost to you
- Integration bridges included - Connect to cloud services when you want them (weather, notifications) but never required for core functionality
- Test before you commit - Try devices in a test environment before deploying station-wide; return products that don't work instead of being locked into contracts

### 📡 Audio Monitor
- Live real-time audio level meter (dBFS)
- Goertzel frequency cards showing per-frequency signal strength
- Visual card flash on tone detection
- Start/stop monitoring without leaving the browser

<img width="1795" height="706" alt="image" src="https://github.com/user-attachments/assets/a69ec79a-bd70-4958-b612-e23e117da72f" />

### 📋 Detection Log
- SQLite-backed history of all decoded pages
- Configurable retention (default 30 days)

<img width="1772" height="1160" alt="image" src="https://github.com/user-attachments/assets/42ea7dbe-608f-4ff5-a6f3-3e66ee2f4da7" />

## 📦 Installation

1. In Home Assistant, go to **Settings → Add-ons → Add-on Store**
2. Click the menu (⋮) → **Repositories** → add this repo URL:
   ```
   https://github.com/lockwoodtechnologysolutions/station_assistant
   ```
3. Find **Station Assistant** in the store and click **Install**
4. Go to the addon's **Configuration** tab and set your audio device index (see below)
5. Start the addon and open the UI via **Ingress** or navigate to `http://your-ha-ip:8099`

---

## ⚙️ Configuration

These options are set in the addon's **Configuration** tab in Home Assistant:

| Option | Default | Description |
|--------|---------|-------------|
| `audio_device_index` | `-1` | Index of the USB audio input device. Use `-1` to auto-detect, or check the Audio Monitor tab for device list. |
| `sample_rate` | `44100` | Audio sample rate in Hz. |
| `chunk_size` | `2048` | Audio buffer chunk size. |
| `input_gain` | `50` | Input gain (0–100 maps to 0×–20× amplification). Increase if tones are weak. |
| `log_retention_days` | `30` | Days to keep detection history. |

Station-level settings (department name, weather entity, sound files, etc.) are configured through the addon's **Settings** tab in the web UI.

---

## 🖥️ Hardware Requirements

- A USB audio input device (generic USB sound card, RTL-SDR with audio pipe, etc.)
- Your station's scanner or radio receiver connected to the audio input
- Home Assistant OS or Supervised installation

---

## 🧙 Setup Wizard

On first launch, Station Assistant walks you through:
1. Department and station name
2. Weather entity selection
3. Audio device selection
4. Initial tone sequence configuration

---

## 🌐 Ports

| Port | Description |
|------|-------------|
| `8099` | Alert Dashboard — accessible directly without HA authentication. Suitable for dedicated kiosk/display devices. |  http://x.x.x.x:8099/dashboard

The management UI (Sequences, Audio Monitor, Detection Log, Settings) is accessed via Home Assistant Ingress and requires HA authentication.

---

## 🔗 Architecture

```
goertzel.py         NumPy vectorized Goertzel frequency detector
decoder.py          PyAudio callback loop, SequenceMachine state machine,
                    auto-restart watchdog, confidence scoring
config_manager.py   Sequence CRUD — /data/sequences.json
detection_log.py    SQLite detection history — /data/detections.db
ha_client.py        HA REST API — events, automations, sensors
sa_config.py        Station config — /data/sa_config.json
stack_manager.py    Multi-unit stacking, audio playback queue, SSE emitter
sse.py              Server-Sent Events bus
main.py             Flask application, all HTTP routes
templates/
  dashboard.html    Full-screen alert dashboard (kiosk)
  manage.html       Management UI (sequences, monitor, log, settings)
  setup.html        First-run setup wizard
```

---

## 🛠️ Tech Stack

- **Runtime:** Python 3.11 on Alpine Linux
- **Web:** Flask + Flask-SocketIO + Gunicorn + Eventlet
- **Audio:** PyAudio + NumPy (Goertzel)
- **Storage:** SQLite (detections), JSON flat files (config/sequences)
- **Process supervisor:** s6-overlay

## 🚀 Commercial Options
Station Assistant is the free, open-source foundation of our alerting platform. For departments requiring:

- Multi-station deployments with centralized management
- Advanced response profile tracking
- Device management and provisioning automation
- Commercial support and training
- Enterprise reporting and compliance features

Learn more about CoreAlert Pro at https://www.lockwood.tech or email info@lockwood.tech

## 🙏 Acknowledgments

Built by firefighters, for firefighters. Station Assistant was created to provide fire departments with commercial-grade alerting capabilities without vendor lock-in or recurring costs.

Special thanks to:

- The Home Assistant community for providing an exceptional platform
- Fire departments who provided real-world feedback during development
- Open-source contributors who make projects like this possible
