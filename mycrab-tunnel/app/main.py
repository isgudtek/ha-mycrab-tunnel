import asyncio
import json
import os
import signal
import subprocess
import time
from pathlib import Path

from aiohttp import web, ClientSession

CONFIG_PATH = os.environ.get("CONFIG_PATH", "/data/options.json")
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data/tunnels"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

TUNNELS_FILE = DATA_DIR / "tunnels.json"
API_BASE = "https://api.mycrab.space"

_procs: dict[str, subprocess.Popen] = {}


def load_tunnels() -> list:
    if TUNNELS_FILE.exists():
        return json.loads(TUNNELS_FILE.read_text())
    return []


def save_tunnels(tunnels: list):
    TUNNELS_FILE.write_text(json.dumps(tunnels, indent=2))


def tunnel_status(t: dict) -> str:
    pid = t.get("pid")
    if not pid:
        return "stopped"
    proc = _procs.get(t["id"])
    if proc and proc.poll() is None:
        return "running"
    return "stopped"


def start_tunnel_proc(t: dict):
    subdomain = t["subdomain"]
    local_port = t["local_port"]
    cfg_file = DATA_DIR / f"{t['id']}.yml"
    if not cfg_file.exists():
        return False
    proc = subprocess.Popen(
        ["cloudflared", "tunnel", "--config", str(cfg_file), "run"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _procs[t["id"]] = proc
    return True


def stop_tunnel_proc(tunnel_id: str):
    proc = _procs.pop(tunnel_id, None)
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


async def provision_tunnel(token: str | None, name: str, local_port: int) -> dict:
    tunnel_id = f"mcr-{int(time.time())}"
    async with ClientSession() as session:
        if token:
            async with session.post(f"{API_BASE}/verify-token",
                                    json={"token": token}) as r:
                data = await r.json()
                if not data.get("valid"):
                    raise ValueError("Invalid token")
                subdomain = data["subdomain"]
        else:
            # Free auto-name — call agent registration
            async with session.post(f"{API_BASE}/agent/register",
                                    json={"name": name or tunnel_id}) as r:
                if r.status != 200:
                    # Fallback: use agent-setup flow via script env
                    subdomain = f"agent-{tunnel_id}"
                else:
                    data = await r.json()
                    subdomain = data.get("subdomain", tunnel_id)

        # Get cloudflared config/credentials
        async with session.post(f"{API_BASE}/agent/message",
                                json={
                                    "agent_name": subdomain,
                                    "message": "setup",
                                    "port": local_port,
                                }) as r:
            pass

        # Poll for response (tunnel credentials)
        for _ in range(30):
            await asyncio.sleep(2)
            async with session.get(f"{API_BASE}/agent/response",
                                   params={"agent_name": subdomain}) as r:
                if r.status == 200:
                    resp = await r.json()
                    cf_token = resp.get("tunnel_token") or resp.get("token")
                    if cf_token:
                        break
        else:
            cf_token = None

    cfg_file = DATA_DIR / f"{tunnel_id}.yml"
    if cf_token:
        cfg_file.write_text(
            f"tunnel: {subdomain}\n"
            f"credentials-file: /data/tunnels/{tunnel_id}.json\n"
            f"ingress:\n"
            f"  - hostname: {subdomain}.mycrab.space\n"
            f"    service: http://localhost:{local_port}\n"
            f"  - service: http_status:404\n"
        )

    return {
        "id": tunnel_id,
        "name": name or subdomain,
        "subdomain": subdomain,
        "local_port": local_port,
        "url": f"https://{subdomain}.mycrab.space",
        "pid": None,
        "cf_token": cf_token,
        "created_at": int(time.time()),
    }


# ── Routes ──────────────────────────────────────────────────────────

async def index(request):
    tunnels = load_tunnels()
    for t in tunnels:
        t["status"] = tunnel_status(t)
    raise web.HTTPFound("/ui")


async def ui(request):
    html_path = Path(__file__).parent / "templates" / "index.html"
    return web.Response(text=html_path.read_text(), content_type="text/html")


async def api_tunnels(request):
    tunnels = load_tunnels()
    for t in tunnels:
        t["status"] = tunnel_status(t)
    return web.json_response(tunnels)


async def api_create(request):
    body = await request.json()
    token = body.get("token", "").strip() or None
    name = body.get("name", "").strip()
    local_port = int(body.get("local_port", 8123))

    try:
        tunnel = await provision_tunnel(token, name, local_port)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=400)

    tunnels = load_tunnels()
    tunnels.append(tunnel)
    save_tunnels(tunnels)
    return web.json_response(tunnel)


async def api_start(request):
    tid = request.match_info["id"]
    tunnels = load_tunnels()
    t = next((x for x in tunnels if x["id"] == tid), None)
    if not t:
        return web.json_response({"error": "not found"}, status=404)
    ok = start_tunnel_proc(t)
    return web.json_response({"ok": ok, "status": "running" if ok else "error"})


async def api_stop(request):
    tid = request.match_info["id"]
    stop_tunnel_proc(tid)
    return web.json_response({"ok": True, "status": "stopped"})


async def api_delete(request):
    tid = request.match_info["id"]
    stop_tunnel_proc(tid)
    tunnels = [t for t in load_tunnels() if t["id"] != tid]
    save_tunnels(tunnels)
    cfg = DATA_DIR / f"{tid}.yml"
    cfg.unlink(missing_ok=True)
    return web.json_response({"ok": True})


async def api_status(request):
    tunnels = load_tunnels()
    running = sum(1 for t in tunnels if tunnel_status(t) == "running")
    return web.json_response({
        "total": len(tunnels),
        "running": running,
        "stopped": len(tunnels) - running,
    })


app = web.Application()
app.router.add_get("/", index)
app.router.add_get("/ui", ui)
app.router.add_get("/api/tunnels", api_tunnels)
app.router.add_post("/api/tunnels", api_create)
app.router.add_post("/api/tunnels/{id}/start", api_start)
app.router.add_post("/api/tunnels/{id}/stop", api_stop)
app.router.add_delete("/api/tunnels/{id}", api_delete)
app.router.add_get("/api/status", api_status)
app.router.add_static("/static", Path(__file__).parent / "static")

if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=8099)
