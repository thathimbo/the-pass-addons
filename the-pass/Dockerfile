# Multi-arch (amd64 + arm64) Home Assistant Python base, pinned.
FROM ghcr.io/home-assistant/base-python:3.13-alpine3.24-2026.08.0

ARG BUILD_VERSION=0.2.0
ARG BUILD_ARCH

# DejaVu fonts for slip/card rendering; cups-client only for the optional CUPS fallback.
RUN apk add --no-cache font-dejavu cups-client

WORKDIR /opt/pass
COPY app/requirements.txt /opt/pass/requirements.txt
RUN pip3 install --no-cache-dir --only-binary=:all: --root-user-action=ignore -r /opt/pass/requirements.txt

COPY app/pass_app /opt/pass/pass_app
COPY run.sh /run.sh
RUN chmod a+x /run.sh

ENV PYTHONUNBUFFERED=1 PYTHONPATH=/opt/pass

LABEL \
  io.hass.name="The Pass" \
  io.hass.description="Scan-only kitchen-island task loop" \
  io.hass.version="${BUILD_VERSION}" \
  io.hass.type="app" \
  io.hass.arch="aarch64|amd64"

CMD [ "/run.sh" ]
