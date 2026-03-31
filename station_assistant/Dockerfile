ARG BUILD_FROM
FROM ${BUILD_FROM}

# ── System packages ────────────────────────────────────────────────────────────
RUN apk add --no-cache \
    python3 \
    py3-pip \
    py3-numpy \
    portaudio-dev \
    alsa-utils \
    alsa-lib \
    alsa-plugins-pulse \
    gcc \
    musl-dev \
    python3-dev \
    curl \
    ffmpeg

# ── Python dependencies ────────────────────────────────────────────────────────
COPY requirements.txt /tmp/
RUN pip3 install --no-cache-dir -r /tmp/requirements.txt

# ── Application files ──────────────────────────────────────────────────────────
# CACHEBUST: update the date below to force a clean re-copy on every rebuild
RUN echo "build-2026-03-30y" > /build_info
COPY rootfs/ /

# ── Permissions ────────────────────────────────────────────────────────────────
RUN chmod a+x /etc/s6-overlay/s6-rc.d/station_assistant/run

# ── Labels ────────────────────────────────────────────────────────────────────
LABEL \
    io.hass.name="Station Assistant" \
    io.hass.description="Fire station alerting with two-tone decoder and alert dashboard" \
    io.hass.arch="${BUILD_ARCH}" \
    io.hass.type="addon" \
    io.hass.version="2.0.0"
