#!/bin/bash
# TrueBox workspace entrypoint: virtual display + headed Chromium + VNC.
set -e

export DISPLAY=:99

# 1. virtual display (the "head" for the headed browser); wait for its socket
rm -f /tmp/.X11-unix/X99
Xvfb :99 -screen 0 1280x800x24 >/tmp/xvfb.log 2>&1 &
for i in $(seq 1 30); do
  [ -S /tmp/.X11-unix/X99 ] && break
  sleep 1
done
if [ ! -S /tmp/.X11-unix/X99 ]; then
  echo "ERROR: Xvfb failed to start"
  cat /tmp/xvfb.log || true
  exit 1
fi
echo "Xvfb is up on :99"

# 2. VNC + browser-based viewer for humans (fail-soft)
(x11vnc -display :99 -nopw -listen localhost -forever -shared >/tmp/x11vnc.log 2>&1 &) || true
(websockify --web /usr/share/novnc/ 6080 localhost:5900 >/tmp/novnc.log 2>&1 &) || true

# 3. headed Chromium with remote debugging for agents.
#    --disable-gpu: Xvfb has no GPU anyway; avoids GPU-process crashes in containers.
#    IMPORTANT: newer Chromium REMOVED support for --remote-debugging-address=0.0.0.0
#    (it is silently ignored for security), so CDP always binds to the container's
#    loopback 127.0.0.1:9222. Docker's port forwarding (docker-proxy) dials the
#    container's eth0 IP, not its loopback -- so we bridge it with socat:
#    0.0.0.0:9223 -> 127.0.0.1:9222. The published CDP port is 9223.
#    Never remove the socat bridge; without it the published port is unreachable.
#    Chromium's stdout/stderr go to the container log (visible via `docker logs`).
echo "starting socat CDP bridge (0.0.0.0:9223 -> 127.0.0.1:9222)..."
socat TCP-LISTEN:9223,bind=0.0.0.0,fork,reuseaddr TCP:127.0.0.1:9222 \
  >/tmp/socat.log 2>&1 &
sleep 1
(timeout 2 bash -c '</dev/tcp/127.0.0.1/9223') 2>/dev/null \
  && echo "socat bridge is listening on :9223" \
  || echo "WARNING: socat bridge not listening (CDP will be unreachable)"
echo "starting Chromium (headed, CDP on 127.0.0.1:9222)..."
exec chromium \
  --display=:99 \
  --no-sandbox --disable-dev-shm-usage --disable-gpu \
  --remote-debugging-port=9222 \
  --user-data-dir=/home/agent/.chromium \
  --no-first-run --no-default-browser-check \
  --disable-features=Translate \
  about:blank
