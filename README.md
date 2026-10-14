# TrueBox 云真机

**Turn any device into an agent's cloud workstation.**
**把任何设备变成 agent 的云真机。**

One lightweight container per agent workspace: a **headed Chromium**, terminal,
filesystem and dev tools — a real machine an AI agent can actually use.
Each workspace is resource-capped, so your device keeps working fine for
everything else.

每个 agent 工作区是一个轻量容器：**带界面的 Chromium**、终端、文件系统、开发工具 —
AI 能真正动手的真机。每个容器都有资源上限，不影响设备上其他正常使用。

## Why 为什么

- Agents need **real machines**, not just APIs. A headed browser is the missing piece:
  agents can see pages, click, scroll and verify — like a human would.
- Cloud sandboxes (E2B, Modal, …) are great but **metered and locked-in**.
  Your own devices — laptop, home server, even a RISC-V board — **are the cloud**.
- TrueBox is **agent-first**: CLI for humans, HTTP API for agents (MCP coming).

## Features 特性

- 🖥️ **Headed Chromium standard** — Xvfb virtual display, CDP remote debugging,
  noVNC live view. 开箱即用的带界面浏览器。
- 📦 **Lightweight** — one Docker container per workspace, `--memory` / `--cpus`
  capped. 轻量，不拖慢宿主机。
- 🔌 **Agent API** — `truebox.py serve` exposes workspaces over HTTP:
  create, exec, screenshot, destroy. 给 agent 用的 HTTP 接口。
- 🖥️ **Web UI** — the same `serve` command opens a dashboard in your browser:
  workspace cards, one-click create/delete, command console, screenshots,
  live browser view. 一套 UI 跑遍 Win / Mac / Linux。
- 🌍 **Multi-arch** — `debian:bookworm-slim` base runs on **amd64, arm64, riscv64**;
  Chromium comes from distro repos (no vendor x86-only builds).
  Win (Docker Desktop) / macOS / Linux / RISC-V Linux 全支持。
- 📸 **Screenshots via CDP** — agents can literally see the screen.

## Quickstart 快速开始

Requirements: Docker (Docker Desktop on Win/Mac, `docker.io` on Linux).

```bash
git clone https://github.com/jaxzhu-del/truebox.git
cd truebox

# 1. build the workspace image (first time, a few minutes)
python3 truebox.py build

# 2. spin up a workspace (2 GB RAM, 2 CPUs cap — host stays snappy)
python3 truebox.py up --name demo --memory 2g --cpus 2

# 3. run commands inside
python3 truebox.py exec demo -- python3 --version

# 4. see what the agent sees
python3 truebox.py screenshot demo -o shot.png

# 5. Web UI + HTTP API (one command, works on Win/Mac/Linux)
pip install -r requirements.txt
python3 truebox.py serve --open
# -> dashboard at http://127.0.0.1:18790/
# API info at http://127.0.0.1:18790/api :
# POST /workspaces  {"name":"demo"}  -> create
# POST /workspaces/demo/exec  {"cmd":["ls","/workspace"]}  -> run
# GET  /workspaces/demo/screenshot  -> PNG
# DELETE /workspaces/demo  -> destroy

# 6. tear down
python3 truebox.py down demo
```

The dashboard lets you create / delete workspaces, run commands, take
screenshots and open the live browser view (noVNC) — no CLI needed.

Then point the agent at it:

```bash
curl -X POST localhost:18790/workspaces -H 'Content-Type: application/json' \
  -d '{"name":"agent-1","memory":"2g","cpus":"2"}'
curl -X POST localhost:18790/workspaces/agent-1/exec \
  -H 'Content-Type: application/json' -d '{"cmd":["chromium","--version"]}'
```

## Architecture 架构

```
 you / agent (CLI, HTTP API, soon MCP)
        │
        ▼
 ┌───────────── truebox.py ─────────────┐
 │  up / exec / screenshot / down       │
 │  workspaces state (~/.truebox/)      │
 └─────────────┬────────────────────────┘
               │ docker
               ▼
 ┌─────────────────────────────────────┐
 │  workspace container (per agent)    │
 │  ┌───────────────────────────────┐  │
 │  │ Xvfb :99  (virtual display)   │  │
 │  │ Chromium --headed  (CDP :9222)│  │
 │  │ x11vnc + noVNC (:6080)        │  │
 │  │ python3 / node / git / tmux   │  │
 │  └───────────────────────────────┘  │
 │  --memory 2g --cpus 2 --pids-limit   │
 └─────────────────────────────────────┘
```

## Roadmap 路线图

## Releases 各系统版本

Push a tag like `v0.2.0` and GitHub Actions builds standalone executables
for **Windows / macOS / Linux** (PyInstaller, no Python needed) and attaches
them to the GitHub Release. Every push also runs a full smoke test
(build → up → exec → screenshot → API → down) on a real Docker host.

- **v0.1** — CLI + HTTP API, headed Chromium, screenshot via CDP,
  multi-arch Dockerfile (amd64/arm64/riscv64).
- **v0.2** (this release) — Web UI dashboard, `--open` flag, CI smoke test,
  per-OS executables on Release.
- **v0.3** — MCP server mode (`truebox.py mcp`): workspaces as MCP tools so
  dsh / Claude Code / OpenClaw drive them natively.
- **v0.4** — multi-node registry: register several devices, schedule workspaces
  across them — your devices become one agent cloud. 多设备组网。
- Later — GPU passthrough profiles, workspace snapshots/templates.

## Notes 说明

- Chromium (not Google Chrome) is used deliberately: distro packages exist for
  **riscv64**, vendor binaries don't. 为 RISC-V 选 Chromium 而非 Chrome。
- Containers are **not** a security boundary against determined attackers;
  treat workspaces as semi-trusted, same as any dev container.
- v0.1 is single-host. Multi-device pooling lands in v0.3.

## Contributing 投稿

PRs welcome — 中文也行。Good first issues: MCP server mode, ARM/RISC-V test
reports, smaller workspace image.

## License

MIT — see [LICENSE](LICENSE).
