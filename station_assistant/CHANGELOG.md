# Changelog

## 2.0.0

### New Features
- **Live PA (Line In Audio Relay)** — After alert sounds finish, relays live Line In audio to media players via real-time MP3 stream. Configurable duration and Stream Base URL in Settings.
- **Gapless audio playback** — All alert sound files are concatenated into a single MP3 via ffmpeg before playback, eliminating buffering delays between files on network media players.
- **Dual VU meters** — Audio Monitor now shows both pre-gain (true hardware input) and post-gain (what the decoder analyzes) levels.
- **WAV file support** — Upload and play WAV files alongside MP3 for alert sounds.
- **Live PA settings card** — Dedicated settings section for Line In Audio Relay duration and Stream Base URL.

### Improvements
- **Single-unit timer fix** — Alert Board Timeout starts immediately for single-unit dispatches instead of waiting for the full stack window to expire.
- **Synchronized countdown** — Return timer bar, indicator fill, and countdown text all animate in sync on the Alert Dashboard.
- **Input gain defaults** — Default gain set to 5 (1.0x unity) across all config locations. UI slider default matches backend.
- **Sound concatenation via ffmpeg** — Always re-encodes to 44.1kHz mono to prevent sample rate mismatch issues (chipmunk effect) on LinkPlay devices.
- **Pre-warmed transcoder** — ffmpeg starts during alert sound playback so the Line In stream is ready when media players connect.
- **Per-client stream queues** — Each media player gets its own copy of the MP3 stream to prevent garbled audio when multiple devices connect.
- **Alert border animation** — Removed expensive box-shadow for GPU-composited border-only animation. Smooth on low-power devices (RPi kiosks).
- **Play button visibility** — Sequence action buttons now have explicit text color for visibility on dark themes.
- **Weather card sizing** — Dynamically sizes to fit content instead of fixed 90% width.

### Bug Fixes
- **Detection counter** — Fixed counter always showing 0 (monkey-patched callback wasn't incrementing).
- **Version strings** — Synchronized version across s6 startup script, Dockerfile label, config.yaml, and APP_VERSION.
- **Stream Base URL** — Auto-detection now checks HA internal_url config; falls back gracefully. User-configurable override in Settings.

### Technical
- Added `ffmpeg` to the Docker container for audio concatenation and real-time transcoding.
- MP3/WAV duration reader for accurate playback wait timing (no external dependencies).
- Sound file search across bundled, media, and www directories.

---

## 1.3.2

- Initial public release
- Two-tone Goertzel decoder with configurable sequences
- Alert Dashboard with multi-unit stacking
- Weather integration
- Detection log with SQLite storage
- Home Assistant automation management
- Setup wizard
