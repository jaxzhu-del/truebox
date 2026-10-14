#!/usr/bin/env python3
"""
TrueBox 云真机 — turn any device into an agent's cloud workstation.

A tiny control plane (CLI + HTTP API + Web UI) that spins up lightweight,
resource-capped containers for AI agents. Every workspace ships with a
headed Chromium (virtual display + remote debugging), terminal, filesystem
and dev tools — a real machine an agent can actually use.

Requires: Docker (Docker Desktop on Win/Mac, docker.io on Linux incl. RISC-V).

    python truebox.py build
    python truebox.py up --name demo --memory 2g --cpus 2
    python truebox.py list
    python truebox.py exec demo -- python3 --version
    python truebox.py screenshot demo -o shot.png
    python truebox.py serve                 # Web UI at http://127.0.0.1:18790/
    python truebox.py serve --open          # ...and open it in your browser
    python truebox.py down demo
"""

import argparse
import base64
import json
import os
import re
import socket
import struct
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

VERSION = "0.2.1"
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
    """Mapped host port for a container port, e.g. '49153'.

    Robust against `docker port` printing several lines
    (IPv4 + IPv6 mappings): prefers the 127.0.0.1 line.
    """
    out = docker("port", container, str(container_port))
    for line in out.splitlines():
        if "127.0.0.1" in line:
            m = re.search(r":(\d+)\s*$", line)
            if m:
                return m.group(1)
    m = re.search(r"(\d+)\s*$", out, re.MULTILINE)
    if m:
        return m.group(1)
    raise TrueBoxError(f"could not parse port mapping: {out!r}")


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
        try:
            logs = docker("logs", "--tail", "30", c)
        except TrueBoxError:
            logs = "(could not read container logs)"
        raise TrueBoxError(
            "Chromium remote debugging did not come up in time.\n"
            f"--- docker logs {c} (tail 30) ---\n{logs}")
    vnc_port = host_port(c, 6080)
    info = {"container": c, "cdp_port": port, "vnc_port": vnc_port,
            "memory": memory, "cpus": str(cpus),
            "created": time.strftime("%Y-%m-%d %H:%M:%S")}
    state[name] = info
    save_state(state)
    return info


def op_list():
    state = load_state()
    for _name, info in state.items():
        try:
            running = docker("ps", "--filter", f"name={info['container']}",
                             "--format", "{{.Names}}")
        except TrueBoxError:
            running = ""
        info["running"] = bool(running)
    return state


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


# ---------------------------------------------------------------- Web UI

UI_HTML = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TrueBox 云真机</title>
<style>
:root { --bg: #0d1117; --panel: #161b22; --border: #30363d; --text: #e6edf3;
        --dim: #8b949e; --accent: #58a6ff; --green: #3fb950; --red: #f85149; }
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
       font-family: -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif; }
header { display: flex; align-items: center; justify-content: space-between;
         padding: 14px 22px; border-bottom: 1px solid var(--border); }
header h1 { font-size: 18px; margin: 0; }
header h1 small { color: var(--dim); font-weight: normal; font-size: 12px; margin-left: 8px; }
header .actions { display: flex; gap: 8px; align-items: center; }
main { max-width: 1080px; margin: 0 auto; padding: 20px 22px 60px; }
.panel { background: var(--panel); border: 1px solid var(--border); border-radius: 10px;
         padding: 16px 18px; margin-bottom: 20px; }
.panel h2 { margin: 0 0 12px; font-size: 14px; color: var(--dim); font-weight: 600; }
.row { display: flex; gap: 10px; flex-wrap: wrap; align-items: end; }
.field { display: flex; flex-direction: column; gap: 6px; }
.field label { font-size: 12px; color: var(--dim); }
input, select { background: var(--bg); color: var(--text); border: 1px solid var(--border);
                border-radius: 6px; padding: 8px 10px; font-size: 14px; }
input:focus, select:focus { outline: none; border-color: var(--accent); }
button { background: var(--accent); color: #fff; border: none; border-radius: 6px;
         padding: 8px 14px; font-size: 14px; cursor: pointer; }
button:hover { filter: brightness(1.1); }
button.ghost { background: transparent; border: 1px solid var(--border); color: var(--text); }
button.danger { background: transparent; border: 1px solid var(--red); color: var(--red); }
button:disabled { opacity: .5; cursor: default; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 14px; }
.card { background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 16px; }
.card h3 { margin: 0 0 4px; font-size: 16px; display: flex; align-items: center; gap: 8px; }
.pill { font-size: 11px; padding: 2px 8px; border-radius: 20px; }
.pill.on { background: rgba(63,185,80,.15); color: var(--green); border: 1px solid var(--green); }
.pill.off { background: rgba(248,81,73,.12); color: var(--red); border: 1px solid var(--red); }
.meta { color: var(--dim); font-size: 12.5px; line-height: 1.7; margin: 8px 0 12px; }
.meta b { color: var(--text); font-weight: 600; }
.btnrow { display: flex; gap: 8px; flex-wrap: wrap; }
.btnrow button { font-size: 13px; padding: 6px 12px; }
.empty { color: var(--dim); text-align: center; padding: 40px 0; }
#modal { display: none; position: fixed; inset: 0; background: rgba(0,0,0,.7);
         align-items: center; justify-content: center; z-index: 10; padding: 20px; }
#modal.open { display: flex; }
.dialog { background: var(--panel); border: 1px solid var(--border); border-radius: 10px;
          max-width: 860px; width: 100%; max-height: 90vh; overflow: auto; padding: 18px; }
.dialog h3 { margin: 0 0 12px; font-size: 15px; }
.dialog img { width: 100%; border-radius: 6px; border: 1px solid var(--border); }
.dialog pre { background: var(--bg); border: 1px solid var(--border); border-radius: 6px;
              padding: 12px; font-size: 12.5px; white-space: pre-wrap; max-height: 320px; overflow: auto; }
.dialog .close { float: right; }
.execrow { display: flex; gap: 8px; margin-bottom: 10px; }
.execrow input { flex: 1; font-family: monospace; }
#toast { position: fixed; bottom: 24px; left: 50%; transform: translateX(-50%);
         background: var(--panel); border: 1px solid var(--border); border-radius: 8px;
         padding: 10px 18px; font-size: 13px; display: none; z-index: 20; }
</style>
</head>
<body>
<header>
  <h1>TrueBox 云真机 <small>agent cloud workstations</small></h1>
  <div class="actions">
    <button class="ghost" onclick="window.open('/api','_blank')">API</button>
    <button class="ghost" onclick="refresh()">刷新</button>
  </div>
</header>
<main>
  <div class="panel">
    <h2>新建工作区</h2>
    <div class="row">
      <div class="field"><label>名称</label><input id="f-name" placeholder="demo"></div>
      <div class="field"><label>内存</label>
        <select id="f-mem"><option>1g</option><option selected>2g</option><option>4g</option><option>8g</option></select></div>
      <div class="field"><label>CPU</label>
        <select id="f-cpu"><option>1</option><option selected>2</option><option>4</option></select></div>
      <div class="field"><label>&nbsp;</label><button id="btn-create" onclick="createWs()">启动工作区</button></div>
    </div>
  </div>
  <div id="grid" class="grid"></div>
  <div id="empty" class="empty" style="display:none">还没有工作区，在上面建一个吧。</div>
</main>
<div id="modal"><div class="dialog">
  <button class="ghost close" onclick="closeModal()">关闭</button>
  <h3 id="dlg-title"></h3>
  <div id="dlg-body"></div>
</div></div>
<div id="toast"></div>
<script>
async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) { const t = await r.text(); throw new Error(t || r.status); }
  const ct = r.headers.get('content-type') || '';
  return ct.includes('json') ? r.json() : r.blob();
}
function esc(s) { return String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
function toast(msg) { const t = document.getElementById('toast'); t.textContent = msg;
  t.style.display = 'block'; setTimeout(() => t.style.display = 'none', 2500); }

async function refresh() {
  try {
    const ws = await api('/workspaces');
    const names = Object.keys(ws);
    document.getElementById('empty').style.display = names.length ? 'none' : 'block';
    document.getElementById('grid').innerHTML = names.map(n => card(n, ws[n])).join('');
  } catch (e) { toast('刷新失败: ' + e.message); }
}
function card(n, w) {
  const pill = w.running ? '<span class="pill on">运行中</span>' : '<span class="pill off">已停止</span>';
  return `<div class="card"><h3>${esc(n)} ${pill}</h3>
    <div class="meta">内存 <b>${esc(w.memory || '-')}</b> · CPU <b>${esc(w.cpus || '-')}</b><br>
    CDP 端口 <b>${esc(w.cdp_port || '-')}</b> · 创建于 ${esc(w.created || '-')}</div>
    <div class="btnrow">
      <button class="ghost" onclick="shot('${esc(n)}')">截图</button>
      <button class="ghost" onclick="execDlg('${esc(n)}')">执行命令</button>
      <button class="ghost" onclick="vnc('${esc(n)}','${esc(w.vnc_port || '')}')">浏览器查看</button>
      <button class="danger" onclick="delWs('${esc(n)}')">删除</button>
    </div></div>`;
}
async function createWs() {
  const name = document.getElementById('f-name').value.trim();
  if (!name) { toast('先填个名称'); return; }
  const btn = document.getElementById('btn-create'); btn.disabled = true; btn.textContent = '启动中…';
  try {
    await api('/workspaces', { method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ name, memory: document.getElementById('f-mem').value,
                             cpus: document.getElementById('f-cpu').value }) });
    document.getElementById('f-name').value = '';
    toast('工作区 ' + name + ' 已启动'); refresh();
  } catch (e) { toast('启动失败: ' + e.message); }
  btn.disabled = false; btn.textContent = '启动工作区';
}
async function delWs(n) {
  if (!confirm('删除工作区 ' + n + '？容器和数据都会被清理。')) return;
  try { await api('/workspaces/' + encodeURIComponent(n), { method: 'DELETE' });
    toast('已删除 ' + n); refresh(); } catch (e) { toast('删除失败: ' + e.message); }
}
async function shot(n) {
  openDlg('截图 — ' + n, '<p style="color:var(--dim)">抓取中…</p>');
  try {
    const blob = await api('/workspaces/' + encodeURIComponent(n) + '/screenshot');
    openDlg('截图 — ' + n, `<img src="${URL.createObjectURL(blob)}">`);
  } catch (e) { openDlg('截图 — ' + n, `<p style="color:var(--red)">失败: ${esc(e.message)}</p>`); }
}
function execDlg(n) {
  openDlg('执行命令 — ' + n,
    `<div class="execrow"><input id="exec-in" placeholder="例如：ls /workspace" onkeydown="if(event.key==='Enter')runExec('${esc(n)}')">
     <button onclick="runExec('${esc(n)}')">运行</button></div><pre id="exec-out">等待输入…</pre>`);
  setTimeout(() => document.getElementById('exec-in').focus(), 50);
}
async function runExec(n) {
  const cmd = document.getElementById('exec-in').value.trim();
  if (!cmd) return;
  const out = document.getElementById('exec-out'); out.textContent = '运行中…';
  const parts = cmd.match(/(?:[^\\s"]+|"[^"]*")+/g).map(s => s.replace(/^"|"$/g, ''));
  try {
    const r = await api('/workspaces/' + encodeURIComponent(n) + '/exec',
      { method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({ cmd: parts }) });
    out.textContent = '$ ' + cmd + '\\n' + (r.stdout || '') + (r.stderr ? '\\n[stderr]\\n' + r.stderr : '')
      + '\\n[exit ' + r.returncode + ']';
  } catch (e) { out.textContent = '失败: ' + e.message; }
}
function vnc(n, port) {
  if (!port) { toast('VNC 端口未知'); return; }
  window.open(`http://${location.hostname}:${port}/vnc.html`, '_blank');
}
function openDlg(title, html) {
  document.getElementById('dlg-title').textContent = title;
  document.getElementById('dlg-body').innerHTML = html;
  document.getElementById('modal').classList.add('open');
}
function closeModal() { document.getElementById('modal').classList.remove('open'); }
document.getElementById('modal').addEventListener('click', e => {
  if (e.target.id === 'modal') closeModal(); });
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeModal(); });
refresh(); setInterval(refresh, 8000);
</script>
</body>
</html>
"""


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
        mark = "" if info.get("running") else " (stopped)"
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
        from fastapi.responses import HTMLResponse, JSONResponse, Response
        import uvicorn
    except ImportError:
        raise TrueBoxError("serve mode needs: pip install -r requirements.txt")
    app = FastAPI(title="TrueBox", version=VERSION)

    @app.get("/", response_class=HTMLResponse)
    def ui():
        return UI_HTML

    @app.get("/api")
    def api_info():
        return {"name": "TrueBox 云真机", "version": VERSION,
                "ui": "/", "endpoints": ["GET /workspaces", "POST /workspaces",
                "POST /workspaces/{name}/exec", "GET /workspaces/{name}/screenshot",
                "DELETE /workspaces/{name}"]}

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
        state = load_state()
        if name not in state:
            return JSONResponse({"error": f"no workspace '{name}'"}, status_code=404)
        try:
            p = subprocess.run(["docker", "exec", "-i", state[name]["container"]] + cmd,
                               capture_output=True, text=True, timeout=300)
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

    url = f"http://{args.host}:{args.port}/"
    print(f"TrueBox UI + API at {url}  (Ctrl+C to stop)")
    if args.open:
        import threading
        import webbrowser
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
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

    p = sub.add_parser("serve", help="Web UI + HTTP API for agents")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=18790)
    p.add_argument("--open", action="store_true", help="open the UI in your browser")
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
