# TrueBox 云真机

[![CI](https://github.com/jaxzhu-del/truebox/actions/workflows/ci.yml/badge.svg)](https://github.com/jaxzhu-del/truebox/actions)
[![Release](https://img.shields.io/github/v/release/jaxzhu-del/truebox)](https://github.com/jaxzhu-del/truebox/releases)
[![License](https://img.shields.io/github/license/jaxzhu-del/truebox)](LICENSE)
![Platform](https://img.shields.io/badge/platform-Windows%20%C2%B7%20macOS%20%C2%B7%20Linux-blue)

**Every AI agent deserves its own real computer.**
**每个 AI agent 都值得拥有一台真正的电脑。**

TrueBox turns any device — your laptop, a home server, even a RISC-V board —
into cloud workstations for AI agents. Each workspace is a lightweight,
resource-capped container with a **headed Chromium**, terminal, filesystem and
dev tools: a real machine an agent can *see and touch*, not just an API.
And because every workspace has hard resource limits, your device keeps
working fine for everything else.

TrueBox 把任何设备变成 agent 的云真机：每个工作区是一个轻量、资源受限的容器，
标配**带界面的 Chromium**、终端、文件系统和开发工具 —— agent 能真正动手、
亲眼看到页面的真机，而不是只能调 API。

## Which TrueBox do I need? 我该用哪种方式？

| | 桌面版 Desktop app | 命令行 CLI | HTTP API（给 agent 用） |
|---|---|---|---|
| 适合谁 | 开箱即用 | 开发者 | agent / 自动化接入 |
| 怎么装 | [Releases](https://github.com/jaxzhu-del/truebox/releases) 下载对应系统版本 | `git clone` 本仓库 | `pip install -r requirements.txt` |
| 怎么用 | `truebox serve --open` 打开管理面板 | `python3 truebox.py up/exec/...` | 见下方 API 示例 |

## Quickstart 快速开始

Needs Docker (Docker Desktop on Win/Mac, `docker.io` on Linux).

```bash
# 1. build the workspace image (first time, a few minutes)
python3 truebox.py build            # or: ./truebox-linux build

# 2. spin up a workspace (2 GB RAM, 2 CPUs cap — host stays snappy)
python3 truebox.py up --name demo

# 3. let the agent drive it over HTTP
pip install -r requirements.txt
python3 truebox.py serve --open     # dashboard at http://127.0.0.1:18790/
```

```bash
# agent: create a workspace
curl -X POST localhost:18790/workspaces -H 'Content-Type: application/json' \
  -d '{"name":"agent-1","memory":"2g","cpus":"2"}'
# agent: run a command inside it
curl -X POST localhost:18790/workspaces/agent-1/exec \
  -H 'Content-Type: application/json' -d '{"cmd":["chromium","--version"]}'
# agent: see the screen
curl localhost:18790/workspaces/agent-1/screenshot -o shot.png
```

Full CLI tour:

```bash
python3 truebox.py exec demo -- python3 --version   # run commands inside
python3 truebox.py screenshot demo -o shot.png      # see what the agent sees
python3 truebox.py update demo --memory 4g          # live-update limits, no restart
python3 truebox.py up --name gpu --gpus all         # GPU passthrough (opt-in)
python3 truebox.py list                             # shows memory / CPU / disk per workspace
python3 truebox.py down demo                        # tear down
```

## Features 特性

- **Headed Chromium, standard** — Xvfb virtual display, CDP remote debugging,
  noVNC live view. Agents see pages, click and verify — like a human would.
  带界面浏览器开箱即用。
- **Agent-first API** — `serve` exposes everything over HTTP: create, exec,
  screenshot, live-update limits, destroy.  dashboard included in the same command.
- **Hard resource caps** — `--memory` / `--cpus` / `--pids-limit` per workspace,
  swap pinned to memory size, live `update` without restart. 宿主机永远流畅。
- **Multi-arch** — Debian base runs on **amd64, arm64, riscv64**; Chromium from
  distro repos (no vendor x86-only binaries). Win / macOS / Linux / RISC-V Linux.
- **Single-file desktop builds** — PyInstaller executables for Windows, macOS,
  Linux on every [Release](https://github.com/jaxzhu-del/truebox/releases).

## Architecture 架构

```
 you / agent (CLI, HTTP API, soon MCP)
        │
        ▼
 ┌──────────────── truebox.py ────────────────┐
 │  up / update / exec / screenshot / down   │
 │  workspaces state (~/.truebox/)           │
 └───────────────────┬───────────────────────┘
                     │ docker
                     ▼
 ┌───────────────────────────────────────────┐
 │  workspace container (one per agent)      │
 │  ┌─────────────────────────────────────┐  │
 │  │ Xvfb :99 (virtual display)          │  │
 │  │ Chromium --headed                   │  │
 │  │   CDP 127.0.0.1:9222 --socat-->     │  │
 │  │   0.0.0.0:9223 (published)          │  │
 │  │ x11vnc + noVNC (:6080)              │  │
 │  │ python3 / node / git / tmux         │  │
 │  └─────────────────────────────────────┘  │
 │  --memory 2g --memory-swap 2g --cpus 2    │
 │  --pids-limit 512 [--gpus]                │
 └───────────────────────────────────────────┘
```

### Resource limits 资源配额

| 维度 | 创建时 | 运行中调整 | 说明 |
|---|---|---|---|
| 内存 / CPU | `--memory` `--cpus` | `update`（无需重启） | cgroup 硬限制 |
| Swap | — | — | 默认与内存等值，工作区吃不到宿主机 swap |
| 进程数 | — | — | `--pids-limit 512`，防 fork 炸弹 |
| GPU | `--gpus all`（可选） | — | 需宿主机装 NVIDIA Container Toolkit |
| 磁盘 | — | — | `list` 显示用量；Docker 无可移植的硬磁盘配额，不做假限制 |

## Why not a cloud sandbox? 为什么不用云沙箱

| | TrueBox | E2B / Modal / Daytona 等 |
|---|---|---|
| 费用 | 免费，用自己的设备 | 按量计费，跑多了肉疼 |
| 数据 | 留在自己机器上 | 上传到别人云端 |
| 浏览器 | headed Chromium 开箱即用 | 多为 headless，或要自己配 |
| 锁定 | 无，MIT 开源，想跑哪跑哪 | 绑厂商 API |

Cloud sandboxes are great for burst workloads. TrueBox is for the machines you
already own: your laptop, a home server, a drawer full of ARM/RISC-V boards —
**your devices are the cloud.**

## Roadmap 路线图

- **v0.3** — MCP server mode (`truebox mcp`): workspaces as MCP tools, so
  Claude Code / OpenClaw and friends drive them natively.
- **v0.4** — multi-node registry: register several devices, schedule workspaces
  across them. 多设备组网，你的设备就是一张 agent 云。
- **Later** — workspace snapshots/templates, GPU profiles.

## FAQ 常见问题

**Windows / macOS 能用吗？**
能。装 Docker Desktop，下载对应系统的桌面版（Releases），`truebox serve --open`
即开即用。容器里跑的是 Linux 环境。

**跟直接给 agent 一台云服务器有什么区别？**
云服务器是一台机器一个人（agent）折腾；TrueBox 是一台机器开 N 个隔离工作区，
每个有独立浏览器、独立配额、互不干扰，还能随时开、随时删。

**安全吗？agent 会不会搞坏我电脑？**
容器有内存/CPU/进程数硬限制，搞不死宿主机。但容器**不是**防恶意攻击的安全
边界 —— 把工作区当半可信的 dev container 用，别在里面放裸奔的密钥。

**RISC-V 真机测过吗？**
Dockerfile 和 Chromium 选型都是按 riscv64 做的（用的发行版 Chromium 包，
厂商闭源 Chrome 没有 riscv64 构建），但还没有 RISC-V 实机测试报告。
有板子的欢迎提测试报告，Good first issue。

**MCP 支持呢？**
v0.3 路线图。现在先用 HTTP API，接口是稳定的（`/api` 自描述）。

## Contributing 投稿

PRs welcome — 中文也行。Good first issues: MCP server mode, ARM/RISC-V test
reports, smaller workspace image, demo GIF for this README.

## License

MIT — see [LICENSE](LICENSE).
