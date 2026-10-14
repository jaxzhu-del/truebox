#!/bin/bash
# TrueBox workspace entrypoint: virtual display + headed Chromium + VNC.
set -e

export DISPLAY=:99

# 1. virtual display (the "head" for the headed browser)
Xvfb :99 -screen 0 1280x800x24 >/tmp/xvfb.log 2>&1 &
sleep 1

# 2. VNC + browser-based viewer for humans (fail-soft)
(x11vnc -display :99 -nopw -listen localhost -forever -shared >/tmp/x11vnc.log 2>&1 &) || true
(websockify --web /usr/share/novnc/ 6080 localhost:5900 >/tmp/novnc.log 2>&1 &) || true

# 3. headed Chromium with remote debugging for agents (CDP on :9222)
exec chromium \
  --display=:99 \
  --no-sandbox --disable-dev-shm-usage \
  --remote-debugging-port=9222 --remote-debugging-address=0.0.0.0 \
  --user-data-dir=/home/agent/.chromium \
  --no-first-run --no-default-browser-check \
  --disable-features=Translate \
  about:blank
