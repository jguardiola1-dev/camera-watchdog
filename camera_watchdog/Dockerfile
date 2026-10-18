FROM ghcr.io/home-assistant/base:3.23

ARG BUILD_VERSION=3.2.0
ARG BUILD_ARCH
LABEL org.opencontainers.image.source="https://github.com/jguardiola1-dev/camera-watchdog"
LABEL io.hass.version="${BUILD_VERSION}" \
      io.hass.type="app" \
      io.hass.arch="${BUILD_ARCH}"

RUN apk add --no-cache \
    python3 \
    ffmpeg \
    py3-flask \
    py3-pillow \
    py3-opencv \
    py3-numpy

COPY run.sh /run.sh
COPY app.py /app.py
COPY web_ui.py /web_ui.py
COPY alarm_policy.py /alarm_policy.py
COPY reference_catalog.py /reference_catalog.py
COPY static /static
COPY orb_detector.py /orb_detector.py
COPY observation_store.py /observation_store.py
COPY census_detector.py /census_detector.py

RUN chmod a+x /run.sh

CMD ["/run.sh"]
