#!/usr/bin/env python3
"""
TrueBox 云真机 — turn any device into an agent's cloud workstation.

A tiny control plane (CLI + HTTP API) that spins up lightweight,
resource-capped containers for AI agents. Every workspace ships with a
headed Chromium (virtual display + remote debugging), terminal, filesystem
and dev tools — a real machine an agent can actually use.

Requires: Docker (Docker Desktop on Win/Mac, docker.io on Linux incl. RISC-V).

    python truebox.py build
    python truebox.py up --name demo --memory 2g --cpus 2
    python truebox.py list
    python truebox.py exec demo -- python3 --version
    python truebox.py screenshot demo -o shot.png
    python truebox.py serve --port 18790    # HTTP API for agents
    python truebox.py down demo
"""

import argparse
import base64
import json
import os
import socket
import struct
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

VERSION = "0.1.0"
IMAGE = "truebox/workspace:latest"
PREFIX = "truebox-"
STATE_DIR = Path.home() / ".truebox"
STATE_FILE = STATE_DIR / "workspaces.json"
CDP_PORT = 9222


# ---------------------------------------------------------------- helpers

class TrueBoxError(RuntimeError):
    pass


def docker(*args):
    """Run `docker ...`, return stripped stdout. Raise TrueBoxError on failure."""
    try:
        p = subprocess.run(["docker", *args], capture_output=True, text=True)
    except FileNotFoundError:
        raise TrueBoxError(
            "docker not found. Install Docker Desktop (Win/Mac) or docker.io (Linux).")
    if p.returncode != 0:
        raise TrueBoxError(f"docker {' '.join(args)} failed:\n{p.stderr.strip()}")
    return p.stdout.strip()


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_state(state):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False),
                          encoding="utf-8")


def cname(name):
    return PREFIX + name


def host_port(container, container_port):
    """Mapped host port for a container port, e.g. '49153'."""
    out = docker("port", container, str(container_port))
    # format: "127.0.0.1:49153"
    return out.rsplit(":", 1)[-1].strip()


def wait_cdp(port, timeout=90):
    url = f"http://127.0.0.1:{port}/json/version"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(1)
    return False


# ------------------------------------------------- minimal CDP client

def _ws_send_text(sock, text):
    payload = text.encode("utf-8")
    mask = os.urandom(4)
    n = len(payload)
    if n < 126:
        header = struct.pack("!BB", 0x81, 0x80 | n)
    elif n < 65536:
        header = struct.pack("!BBH", 0x81, 0x80 | 126, n)
    else:
        header = struct.pack("!BBQ", 0x81, 0x80 | 127, n)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    sock.sendall(header + mask + masked)


def _ws_recv_text(sock, timeout=30):
    """Read frames until one complete text message is assembled."""
    sock.settimeout(timeout)
    buf = b""
    message = b""

    def need(n):
        nonlocal buf
        while len(buf) < n:
            chunk = sock.recv(65536)
            if not chunk:
                raise TrueBoxError("websocket closed by peer")
            buf += chunk

    while True:
        need(2)
        b1, b2 = buf[0], buf[1]
        fin = (b1 & 0x80) != 0
        opcode = b1 & 0x0F
        masked = (b2 & 0x80) != 0
        length = b2 & 0x7F
        idx = 2
        if length == 126:
            need(4)
            length = struct.unpack("!H", buf[2:4])[0]
            idx = 4
        elif length == 127:
            need(10)
            length = struct.unpack("!Q", buf[2:10])[0]
            idx = 10
        if masked:
            need(idx + 4)
            mkey = buf[idx:idx + 4]
            idx += 4
        need(idx + length)
        payload = buf[idx:idx + length]
        buf = buf[idx + length:]
        if masked:
            payload = bytes(b ^ mkey[i % 4] for i, b in enumerate(payload))
        if opcode == 0x8:
            raise TrueBoxError("websocket closed by peer")
        if opcode == 0x9:  # ping -> answer pong (masked, we are the client)
            pmask = os.urandom(4)
            pong = struct.pack("!BB", 0x8A, 0x80 | len(payload)) + pmask
            pong += bytes(b ^ pmask[i % 4] for i, b in enumerate(payload))
            sock.sendall(pong)
            continue
        if opcode in (0x1, 0x0):  # text / continuation
            message += payload
            if fin:
                return message
        # other opcodes ignored


def cdp_screenshot(cdp_port, timeout=30):
    """Capture the headed Chromium screen via CDP. Returns PNG bytes."""
    with urllib.request.urlopen(f"http://127.0.0.1:{cdp_port}/json/list",
                                timeout=timeout) as r:
        targets = json.load(r)
    pages = [t for t in targets if t.get("type") == "page"]
    if not pages:
        raise TrueBoxError("no page target found in Chromium")
    ws_url = pages[0]["webSocketDebuggerUrl"]  # ws://127.0.0.1:PORT/devtools/page/XXX
    if not ws_url.startswith("ws://"):
        raise TrueBoxError(f"unexpected debugger URL: {ws_url}")
    hostport, path = ws_url[5:].split("/", 1)
    host, port = hostport.rsplit(":", 1)
    path = "/" + path

    s = socket.create_connection((host, int(port)), timeout=timeout)
    try:
        key = base64.b64encode(os.urandom(16)).decode()
        s.sendall((
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = s.recv(4096)
            if not chunk:
                raise TrueBoxError("CDP handshake failed")
            resp += chunk
        if b"101" not in resp.split(b"\r\n", 1)[0]:
            raise TrueBoxError(f"CDP handshake rejected: {resp[:120]!r}")

        _ws_send_text(s, json.dumps({
            "id": 1, "method": "Page.captureScreenshot",
            "params": {"format": "png"},
        }))
        while True:
            raw = _ws_recv_text(s, timeout=timeout)
            try:
                msg = json.loads(raw.decode("utf-8"))
            except Exception:
                continue
            if msg.get("id") == 1:
                data = msg.get("result", {}).get("data")
                if not data:
                    raise TrueBoxError(f"screenshot failed: {msg}")
                return base64.b64decode(data)
    finally:
        s.close()


# ---------------------------------------------------------------- core ops

def op_up(name, memory="2g", cpus="2"):
    state = load_state()
    if name in state:
        raise TrueBoxError(f"workspace '{name}' already exists "
                           f"(truebox.py down {name} first)")
    c = cname(name)
    docker("run", "-d", "--name", c,
           "--memory", memory, "--cpus", cpus,
           "--pids-limit", "512",
           "-p", "127.0.0.1::9222", "-p", "127.0.0.1::6080",
           "-v", f"{PREFIX}{name}-data:/workspace",
           IMAGE)
    try:
        port = host_port(c, CDP_PORT)
    except TrueBoxError:
        raise TrueBoxError("container started but port mapping unreadable; "
                           f"check `docker logs {c}`")
    if not wait_cdp(port):
        raise TrueBoxError("Chromium remote debugging did not come up in time; "
                           f"check `docker logs {c}`")
    vnc_port = host_port(c, 6080)
    info = {"container": c, "cdp_port": port, "vnc_port": vnc_port,
            "memory": memory, "cpus": str(cpus),
            "created": time.strftime("%Y-%m-%d %H:%M:%S")}
    state[name] = info
    save_state(state)
    return info


def op_list():
    return load_state()


def op_exec(name, cmd):
    state = load_state()
    if name not in state:
        raise TrueBoxError(f"no workspace '{name}'")
    p = subprocess.run(["docker", "exec", "-i", state[name]["container"]] + cmd)
    return p.returncode


def op_screenshot(name):
    state = load_state()
    if name not in state:
        raise TrueBoxError(f"no workspace '{name}'")
    port = state[name].get("cdp_port") or host_port(state[name]["container"], CDP_PORT)
    return cdp_screenshot(int(port))


def op_down(name):
    state = load_state()
    if name not in state:
        raise TrueBoxError(f"no workspace '{name}'")
    docker("rm", "-f", state[name]["container"])
    del state[name]
    save_state(state)


# ---------------------------------------------------------------- CLI

def cmd_build(args):
    print(f"building {IMAGE} (multi-arch debian base; first build takes a few minutes)...")
    try:
        subprocess.run(["docker", "build", "-t", IMAGE, "."], check=True)
    except FileNotFoundError:
        raise TrueBoxError("docker not found")
    except subprocess.CalledProcessError:
        raise TrueBoxError("docker build failed")
    print("done.")


def cmd_up(args):
    info = op_up(args.name, args.memory, args.cpus)
    print(f"workspace '{args.name}' is up")
    print(f"  CDP (agent browser control): 127.0.0.1:{info['cdp_port']}")
    print(f"  noVNC (watch in browser)   : http://127.0.0.1:{info['vnc_port']}/vnc.html")
    print(f"  shell: docker exec -it {info['container']} bash")


def cmd_list(args):
    state = op_list()
    if not state:
        print("no workspaces. create one: python truebox.py up --name demo")
        return
    print(f"{'NAME':<16}{'CONTAINER':<24}{'CDP':<8}{'CREATED'}")
    for name, info in state.items():
        running = docker("ps", "--filter", f"name={info['container']}",
                         "--format", "{{.Names}}")
        mark = "" if running else " (stopped)"
        print(f"{name:<16}{info['container']:<24}"
              f"{info.get('cdp_port', '-'):<8}{info.get('created', '-')}{mark}")


def cmd_exec(args):
    if not args.cmd:
        raise TrueBoxError("nothing to run")
    sys.exit(op_exec(args.name, args.cmd))


def cmd_screenshot(args):
    png = op_screenshot(args.name)
    out = args.output or f"{args.name}-screenshot.png"
    Path(out).write_bytes(png)
    print(f"saved to {out} ({len(png)} bytes)")


def cmd_down(args):
    op_down(args.name)
    print(f"workspace '{args.name}' removed")


def cmd_serve(args):
    try:
        from fastapi import FastAPI
        from fastapi.responses import JSONResponse, Response
        import uvicorn
    except ImportError:
        raise TrueBoxError("API mode needs: pip install -r requirements.txt")
    app = FastAPI(title="TrueBox API", version=VERSION)

    @app.get("/")
    def root():
        return {"name": "TrueBox 云真机", "version": VERSION,
                "hint": "POST /workspaces to create an agent workspace"}

    @app.get("/workspaces")
    def api_list():
        return op_list()

    @app.post("/workspaces")
    def api_up(body: dict):
        name = body.get("name") or f"ws-{os.urandom(3).hex()}"
        try:
            info = op_up(name, body.get("memory", "2g"), str(body.get("cpus", "2")))
        except TrueBoxError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return {"name": name, **info}

    @app.post("/workspaces/{name}/exec")
    def api_exec(name: str, body: dict):
        cmd = body.get("cmd")
        if not cmd:
            return JSONResponse({"error": "body.cmd required, e.g. {\"cmd\": [\"ls\", \"/workspace\"]}"},
                                status_code=400)
        try:
            p = subprocess.run(["docker", "exec", "-i", cname(name)] + cmd,
                               capture_output=True, text=True, timeout=300)
        except TrueBoxError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        except subprocess.TimeoutExpired:
            return JSONResponse({"error": "command timed out after 300s"}, status_code=504)
        return {"returncode": p.returncode, "stdout": p.stdout, "stderr": p.stderr}

    @app.get("/workspaces/{name}/screenshot")
    def api_screenshot(name: str):
        try:
            png = op_screenshot(name)
        except TrueBoxError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return Response(content=png, media_type="image/png")

    @app.delete("/workspaces/{name}")
    def api_down(name: str):
        try:
            op_down(name)
        except TrueBoxError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return {"deleted": name}

    print(f"TrueBox API on http://{args.host}:{args.port}  (Ctrl+C to stop)")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


def main():
    ap = argparse.ArgumentParser(
        prog="truebox",
        description="TrueBox 云真机 — turn any device into an agent's cloud workstation.")
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("build", help="build the workspace image")
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("up", help="start a workspace")
    p.add_argument("--name", required=True)
    p.add_argument("--memory", default="2g", help="e.g. 2g (default: 2g)")
    p.add_argument("--cpus", default="2", help="e.g. 2 (default: 2)")
    p.set_defaults(func=cmd_up)

    p = sub.add_parser("list", help="list workspaces")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("exec", help="run a command inside a workspace")
    p.add_argument("name")
    p.add_argument("cmd", nargs=argparse.REMAINDER,
                   help="command, e.g.: exec demo -- ls /workspace")
    p.set_defaults(func=cmd_exec)

    p = sub.add_parser("screenshot", help="capture the workspace screen via CDP")
    p.add_argument("name")
    p.add_argument("-o", "--output", default=None)
    p.set_defaults(func=cmd_screenshot)

    p = sub.add_parser("down", help="destroy a workspace")
    p.add_argument("name")
    p.set_defaults(func=cmd_down)

    p = sub.add_parser("serve", help="HTTP API for agents")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=18790)
    p.set_defaults(func=cmd_serve)

    args = ap.parse_args()
    # `exec demo -- ls` leaves a leading "--" in REMAINDER; drop it
    if getattr(args, "cmd", None) and args.cmd[:1] == ["--"]:
        args.cmd = args.cmd[1:]
    try:
        args.func(args)
    except TrueBoxError as e:
        print(f"truebox: error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
