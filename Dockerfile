# TrueBox workspace image — multi-arch (amd64 / arm64 / riscv64).
# Debian bookworm ships real Chromium .debs for all three arches
# (Ubuntu's Chromium is a snap and does not work in Docker;
#  Google Chrome has no riscv64 build at all).
FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        chromium \
        xvfb x11vnc novnc websockify \
        python3 nodejs npm \
        git curl wget ca-certificates tmux \
        fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -s /bin/bash agent \
    && mkdir -p /workspace /home/agent/.chromium \
    && chown -R agent:agent /workspace /home/agent

WORKDIR /workspace

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 9222 5900 6080

USER agent
ENTRYPOINT ["/entrypoint.sh"]
