#!/usr/bin/with-contenv bashio
# shellcheck shell=bash
bashio::log.info "Starting The Pass on :8787 (ingress + host port)"
if [ -e /dev/usb/lp0 ]; then
  bashio::log.info "Label printer device present: $(ls -l /dev/usb/lp0)"
else
  bashio::log.warning "No /dev/usb/lp0 yet (label printer unplugged?). Cards still render to PNG."
fi
cd /opt/pass
exec python3 -m pass_app.addon
