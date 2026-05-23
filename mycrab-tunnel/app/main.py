import asyncio
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

from aiohttp import web, ClientSession

CONFIG_PATH = os.environ.get("CONFIG_PATH", "/data/options.json")
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data/tunnels"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

TUNNELS_FILE = DATA_DIR / "tunnels.json"
API_BASE = "https://api.mycrab.space"
FREE_TTL = 60 * 60  # 60 minutes

_procs: dict[str, subprocess.Popen] = {}


# ── Persistence ──────────────────────────────────────────────────────

def load_tunnels() -> list:
    if TUNNELS_FILE.exists():
        return json.loads(TUNNELS_FILE.read_text())
    return []


def save_tunnels(tunnels: list):
    TUNNELS_FILE.write_text(json.dumps(tunnels, indent=2))


def load_ha_config() -> dict:
    try:
        return json.loads(Path(CONFIG_PATH).read_text())
    except Exception:
        return {}


# ── Tunnel state ─────────────────────────────────────────────────────

def tunnel_status(t: dict) -> str:
    if _is_expired(t):
        return "expired"
    proc = _procs.get(t["id"])
    if proc and proc.poll() is None:
        return "running"
    return "stopped"


def _is_expired(t: dict) -> bool:
    expires = t.get("expires_at")
    return bool(expires and time.time() > expires)


def _cf_config_path(tunnel_id: str) -> Path:
    return DATA_DIR / f"{tunnel_id}.yml"


def _start_proc(t: dict) -> bool:
    cfg = _cf_config_path(t["id"])
    if not cfg.exists():
        return False
    proc = subprocess.Popen(
        ["cloudflared", "tunnel", "--config", str(cfg), "--no-autoupdate", "run"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _procs[t["id"]] = proc
    return True


def _stop_proc(tunnel_id: str):
    proc = _procs.pop(tunnel_id, None)
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def _write_cf_config(tunnel_id: str, subdomain: str, local_port: int, cf_token: str):
    cfg = _cf_config_path(tunnel_id)
    cfg.write_text(
        f"tunnel: {subdomain}\n"
        f"credentials-file: /data/tunnels/{tunnel_id}-creds.json\n"
        f"ingress:\n"
        f"  - hostname: {subdomain}.mycrab.space\n"
        f"    service: http://localhost:{local_port}\n"
        f"  - service: http_status:404\n"
    )
    # Write credentials stub with token
    creds = DATA_DIR / f"{tunnel_id}-creds.json"
    creds.write_text(json.dumps({"AccountTag": "", "TunnelID": subdomain, "TunnelSecret": cf_token}))


# ── Provisioning ─────────────────────────────────────────────────────

async def provision_free(name: str, local_port: int) -> dict:
    """Run agent-setup-auto.sh in bot mode and parse out subdomain + CF token."""
    tunnel_id = f"free-{int(time.time())}"
    env = os.environ.copy()
    env["MODE"] = "bot"
    env["HOME"] = str(DATA_DIR)

    # Download and run setup script
    script_url = "https://mycrab.space/agent-setup-auto.sh"
    proc = await asyncio.create_subprocess_exec(
        "bash", "-c", f"curl -fsSL {script_url} | MODE=bot bash",
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
    output = stdout.decode() + stderr.decode()

    # Parse subdomain from output
    subdomain = None
    cf_token = None
    for line in output.splitlines():
        if "agent-" in line and ".mycrab.space" in line:
            import re
            m = re.search(r'(agent-\w+)', line)
            if m:
                subdomain = m.group(1)
        if "tunnel_token" in line.lower() or "TUNNEL_TOKEN" in line:
            import re
            m = re.search(r'[A-Za-z0-9+/=]{40,}', line)
            if m:
                cf_token = m.group(0)

    if not subdomain:
        # Try reading the generated yml from ~/.cloudflared
        cf_dir = DATA_DIR / ".cloudflared"
        ymls = list(cf_dir.glob("agent-*.yml")) if cf_dir.exists() else []
        if ymls:
            newest = max(ymls, key=lambda p: p.stat().st_mtime)
            subdomain = newest.stem

    if not subdomain:
        raise RuntimeError(f"Could not parse subdomain from setup script output.\n{output[-500:]}")

    # Copy generated cloudflared config if present
    src_yml = DATA_DIR / ".cloudflared" / f"{subdomain}.yml"
    if src_yml.exists():
        _cf_config_path(tunnel_id).write_text(src_yml.read_text())
    elif cf_token:
        _write_cf_config(tunnel_id, subdomain, local_port, cf_token)
    else:
        raise RuntimeError("No cloudflared config generated. Setup script may have failed.")

    return {
        "id": tunnel_id,
        "name": name or "Home Assistant",
        "subdomain": subdomain,
        "url": f"https://{subdomain}.mycrab.space",
        "local_port": local_port,
        "tier": "free",
        "expires_at": int(time.time()) + FREE_TTL,
        "created_at": int(time.time()),
    }


async def provision_paid(token: str, name: str, local_port: int) -> dict:
    """Verify token via mycrab API, get subdomain, configure cloudflared."""
    async with ClientSession() as session:
        async with session.post(f"{API_BASE}/verify-token",
                                json={"token": token},
                                timeout=10) as r:
            data = await r.json()

    if not data.get("valid"):
        raise ValueError(f"Invalid token: {data.get('error','unknown error')}")

    subdomain = data.get("subdomain", "").strip()
    cf_token = data.get("tunnel_token") or data.get("cf_token") or token

    if not subdomain:
        raise ValueError("Token valid but no subdomain returned")

    tunnel_id = f"paid-{subdomain}"
    _write_cf_config(tunnel_id, subdomain, local_port, cf_token)

    return {
        "id": tunnel_id,
        "name": name or subdomain,
        "subdomain": subdomain,
        "url": f"https://{subdomain}.mycrab.space",
        "local_port": local_port,
        "tier": "paid",
        "expires_at": None,
        "created_at": int(time.time()),
    }


# ── Background: auto-expire free tunnels ─────────────────────────────

async def _expiry_watcher():
    while True:
        await asyncio.sleep(60)
        tunnels = load_tunnels()
        changed = False
        for t in tunnels:
            if t.get("tier") == "free" and _is_expired(t):
                _stop_proc(t["id"])
                changed = True
        if changed:
            save_tunnels(tunnels)


# ── Routes ───────────────────────────────────────────────────────────

async def ui(request):
    html = (Path(__file__).parent / "templates" / "index.html").read_text()
    return web.Response(text=html, content_type="text/html")


async def api_tunnels(request):
    tunnels = load_tunnels()
    for t in tunnels:
        t["status"] = tunnel_status(t)
        if t.get("expires_at"):
            t["expires_in"] = max(0, int(t["expires_at"] - time.time()))
    return web.json_response(tunnels)


async def api_create(request):
    body = await request.json()
    token = (body.get("token") or "").strip()
    name = body.get("name", "").strip()
    local_port = int(body.get("local_port") or 8123)

    try:
        if token:
            tunnel = await provision_paid(token, name, local_port)
        else:
            tunnel = await provision_free(name, local_port)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=400)

    tunnels = load_tunnels()
    # Replace if same paid subdomain already exists
    tunnels = [t for t in tunnels if t["id"] != tunnel["id"]]
    tunnels.append(tunnel)
    save_tunnels(tunnels)

    if tunnel.get("tier") == "free":
        _start_proc(tunnel)

    return web.json_response(tunnel)


async def api_update_port(request):
    tid = request.match_info["id"]
    body = await request.json()
    new_port = int(body.get("local_port", 8123))
    tunnels = load_tunnels()
    for t in tunnels:
        if t["id"] == tid:
            t["local_port"] = new_port
            _stop_proc(tid)
            _write_cf_config(tid, t["subdomain"], new_port, "")
            break
    save_tunnels(tunnels)
    return web.json_response({"ok": True})


async def api_start(request):
    tid = request.match_info["id"]
    tunnels = load_tunnels()
    t = next((x for x in tunnels if x["id"] == tid), None)
    if not t:
        return web.json_response({"error": "not found"}, status=404)
    if _is_expired(t):
        return web.json_response({"error": "Tunnel expired. Create a new one."}, status=400)
    ok = _start_proc(t)
    return web.json_response({"ok": ok})


async def api_stop(request):
    tid = request.match_info["id"]
    _stop_proc(tid)
    return web.json_response({"ok": True})


async def api_delete(request):
    tid = request.match_info["id"]
    _stop_proc(tid)
    tunnels = [t for t in load_tunnels() if t["id"] != tid]
    save_tunnels(tunnels)
    _cf_config_path(tid).unlink(missing_ok=True)
    (DATA_DIR / f"{tid}-creds.json").unlink(missing_ok=True)
    return web.json_response({"ok": True})


async def api_purge_expired(request):
    tunnels = load_tunnels()
    before = len(tunnels)
    tunnels = [t for t in tunnels if not _is_expired(t)]
    save_tunnels(tunnels)
    return web.json_response({"purged": before - len(tunnels)})


async def api_status(request):
    tunnels = load_tunnels()
    statuses = [tunnel_status(t) for t in tunnels]
    return web.json_response({
        "total": len(tunnels),
        "running": statuses.count("running"),
        "stopped": statuses.count("stopped"),
        "expired": statuses.count("expired"),
    })


async def api_config(request):
    cfg = load_ha_config()
    return web.json_response({"default_token": cfg.get("token", "").strip()})


# ── App ──────────────────────────────────────────────────────────────

async def on_startup(app):
    asyncio.create_task(_expiry_watcher())
    # Auto-start paid tunnels that were running before restart
    for t in load_tunnels():
        if t.get("tier") == "paid":
            _start_proc(t)


app = web.Application()
app.on_startup.append(on_startup)

app.router.add_get("/", lambda r: web.HTTPFound("/ui"))
app.router.add_get("/ui", ui)
app.router.add_get("/api/tunnels", api_tunnels)
app.router.add_post("/api/tunnels", api_create)
app.router.add_post("/api/tunnels/{id}/start", api_start)
app.router.add_post("/api/tunnels/{id}/stop", api_stop)
app.router.add_patch("/api/tunnels/{id}/port", api_update_port)
app.router.add_delete("/api/tunnels/{id}", api_delete)
app.router.add_post("/api/tunnels/purge-expired", api_purge_expired)
app.router.add_get("/api/status", api_status)
app.router.add_get("/api/config", api_config)
app.router.add_static("/static", Path(__file__).parent / "static")

if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=8099)
