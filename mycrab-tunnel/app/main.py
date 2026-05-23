import asyncio
import json
import os
import random
import re
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
    ps = t.get("provision_status")
    if ps and ps != "live":
        return ps  # "pending" or "failed"
    proc = _procs.get(t["id"])
    if proc and proc.poll() is None:
        return "running"
    return "stopped"


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


def _write_paid_config(tunnel_id: str, subdomain: str, local_port: int, cf_token: str):
    cfg = _cf_config_path(tunnel_id)
    creds_path = DATA_DIR / f"{tunnel_id}-creds.json"
    cfg.write_text(
        f"tunnel: {subdomain}\n"
        f"credentials-file: {creds_path}\n"
        f"ingress:\n"
        f"  - hostname: {subdomain}.mycrab.space\n"
        f"    service: http://localhost:{local_port}\n"
        f"  - service: http_status:404\n"
    )
    creds_path.write_text(json.dumps({
        "AccountTag": "", "TunnelID": subdomain, "TunnelSecret": cf_token
    }))


def _write_free_config(tunnel_id: str, tunnel_uuid: str, local_port: int):
    cfg = _cf_config_path(tunnel_id)
    creds_path = DATA_DIR / f"{tunnel_id}-creds.json"
    cfg.write_text(
        f"tunnel: {tunnel_uuid}\n"
        f"credentials-file: {creds_path}\n"
        f"ingress:\n"
        f"  - hostname: {tunnel_id}.mycrab.space\n"
        f"    service: http://localhost:{local_port}\n"
        f"  - service: http_status:404\n"
    )


# ── mycrab provisioning ───────────────────────────────────────────────

async def _poll_mycrab_field(agent_name: str, field: str, timeout: int = 600) -> dict:
    """Poll /agent/response until status=ready and field present in data."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        try:
            async with ClientSession() as s:
                async with s.get(f"{API_BASE}/agent/response",
                                 params={"agent_name": agent_name},
                                 timeout=aiohttp_timeout(10)) as r:
                    data = await r.json()
            if data.get("status") == "ready":
                inner = data.get("data", {})
                if inner.get(field):
                    return inner
        except Exception:
            pass
        await asyncio.sleep(5)
    raise RuntimeError(f"mycrab API did not provide '{field}' within {timeout}s")


def aiohttp_timeout(seconds):
    from aiohttp import ClientTimeout
    return ClientTimeout(total=seconds)


async def _provision_mycrab_task(tunnel_id: str, local_port: int):
    """Mirror the agent-setup-auto.sh provisioning flow inside the addon."""

    def _update(**kwargs):
        tunnels = load_tunnels()
        for t in tunnels:
            if t["id"] == tunnel_id:
                t.update(kwargs)
                break
        save_tunnels(tunnels)

    try:
        # Step 1: announce + request cert.pem
        async with ClientSession() as s:
            await s.post(f"{API_BASE}/agent/message",
                         json={"agent_name": tunnel_id, "message": "Starting autonomous setup"},
                         timeout=aiohttp_timeout(10))
            await asyncio.sleep(1)
            await s.post(f"{API_BASE}/agent/message",
                         json={"agent_name": tunnel_id, "message": "Ready for cert.pem",
                               "status": "awaiting_cert"},
                         timeout=aiohttp_timeout(10))

        # Step 2: wait for cert.pem
        cert_data = await _poll_mycrab_field(tunnel_id, "cert_pem", timeout=600)
        cert_path = DATA_DIR / "cert.pem"
        cert_path.write_text(cert_data["cert_pem"])
        cert_path.chmod(0o600)

        # Step 3: cloudflared tunnel create
        env = {**os.environ, "HOME": "/root"}
        result = subprocess.run(
            ["cloudflared", "--origincert", str(cert_path), "tunnel", "create", tunnel_id],
            capture_output=True, text=True, timeout=30, env=env
        )
        m = re.search(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',
                      result.stdout + result.stderr)
        if not m:
            # Already exists? try tunnel info
            info = subprocess.run(
                ["cloudflared", "--origincert", str(cert_path), "tunnel", "info", tunnel_id],
                capture_output=True, text=True, timeout=30, env=env
            )
            m = re.search(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',
                          info.stdout + info.stderr)
        if not m:
            raise RuntimeError(f"cloudflared create failed: {result.stderr[-300:]}")

        uuid = m.group(0)

        # Move credentials from cloudflared default location to persistent storage
        creds_src = Path(f"/root/.cloudflared/{uuid}.json")
        creds_dst = DATA_DIR / f"{tunnel_id}-creds.json"
        if creds_src.exists():
            creds_dst.write_bytes(creds_src.read_bytes())

        # Step 4: send tunnel_id so operator sets up DNS record
        async with ClientSession() as s:
            await s.post(f"{API_BASE}/agent/message",
                         json={"agent_name": tunnel_id, "message": "Tunnel created successfully",
                               "tunnel_id": uuid, "tunnel_name": tunnel_id},
                         timeout=aiohttp_timeout(10))

        # Step 5: wait for config_yml (signals DNS is live)
        await _poll_mycrab_field(tunnel_id, "config_yml", timeout=600)

        # Step 6: write our own config with correct container paths
        _write_free_config(tunnel_id, uuid, local_port)

        # Step 7: update record, start tunnel
        _update(provision_status="live", url=f"https://{tunnel_id}.mycrab.space",
                subdomain=tunnel_id)

        t = next((t for t in load_tunnels() if t["id"] == tunnel_id), None)
        if t:
            _start_proc(t)

        async with ClientSession() as s:
            await s.post(f"{API_BASE}/agent/message",
                         json={"agent_name": tunnel_id, "message": "Setup completed successfully!",
                               "subdomain": f"{tunnel_id}.mycrab.space", "status": "live"},
                         timeout=aiohttp_timeout(10))

    except Exception as e:
        _update(provision_status="failed", error=str(e))


async def provision_free(name: str, local_port: int) -> dict:
    """Start mycrab free tunnel via API (same flow as agent-setup-auto.sh). Returns immediately."""
    tunnel_id = f"agent-{random.randint(0, 999999):06d}"
    tunnel = {
        "id": tunnel_id,
        "name": name or "Home Assistant",
        "subdomain": tunnel_id,
        "url": f"https://{tunnel_id}.mycrab.space",
        "local_port": local_port,
        "tier": "free",
        "expires_at": None,
        "created_at": int(time.time()),
        "provision_status": "pending",
    }
    tunnels = load_tunnels()
    tunnels = [t for t in tunnels if t["id"] != tunnel_id]
    tunnels.append(tunnel)
    save_tunnels(tunnels)
    asyncio.create_task(_provision_mycrab_task(tunnel_id, local_port))
    return tunnel


async def provision_paid(token: str, name: str, local_port: int) -> dict:
    """Verify token via mycrab API, get subdomain, configure cloudflared."""
    async with ClientSession() as session:
        async with session.post(f"{API_BASE}/verify-token",
                                json={"token": token},
                                timeout=aiohttp_timeout(10)) as r:
            data = await r.json()

    if not data.get("valid"):
        raise ValueError(f"Invalid token: {data.get('error','unknown error')}")

    subdomain = data.get("subdomain", "").strip()
    cf_token = data.get("tunnel_token") or data.get("cf_token") or token

    if not subdomain:
        raise ValueError("Token valid but no subdomain returned")

    tunnel_id = f"paid-{subdomain}"
    _write_paid_config(tunnel_id, subdomain, local_port, cf_token)

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


# ── Routes ───────────────────────────────────────────────────────────

async def ui(request):
    html = (Path(__file__).parent / "templates" / "index.html").read_text()
    return web.Response(text=html, content_type="text/html")


async def api_tunnels(request):
    tunnels = load_tunnels()
    for t in tunnels:
        t["status"] = tunnel_status(t)
    return web.json_response(tunnels)


async def api_create(request):
    body = await request.json()
    token = (body.get("token") or "").strip()
    name = body.get("name", "").strip()
    local_port = int(body.get("local_port") or 8123)

    try:
        if token:
            tunnel = await provision_paid(token, name, local_port)
            tunnels = load_tunnels()
            tunnels = [t for t in tunnels if t["id"] != tunnel["id"]]
            tunnels.append(tunnel)
            save_tunnels(tunnels)
            _start_proc(tunnel)
        else:
            tunnel = await provision_free(name, local_port)
            # already saved + background task started inside provision_free
    except Exception as e:
        return web.json_response({"error": str(e)}, status=400)

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
            _write_paid_config(tid, t["subdomain"], new_port, "")
            break
    save_tunnels(tunnels)
    return web.json_response({"ok": True})


async def api_start(request):
    tid = request.match_info["id"]
    tunnels = load_tunnels()
    t = next((x for x in tunnels if x["id"] == tid), None)
    if not t:
        return web.json_response({"error": "not found"}, status=404)
    if t.get("provision_status") == "pending":
        return web.json_response({"error": "Still provisioning, please wait."}, status=400)
    if t.get("tier") == "free":
        return web.json_response({"error": "Free tunnels cannot be restarted. Delete and create a new one."}, status=400)
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


async def api_status(request):
    tunnels = load_tunnels()
    statuses = [tunnel_status(t) for t in tunnels]
    return web.json_response({
        "total": len(tunnels),
        "running": statuses.count("running"),
        "stopped": statuses.count("stopped"),
        "pending": statuses.count("pending"),
    })


async def api_config(request):
    cfg = load_ha_config()
    return web.json_response({"default_token": cfg.get("token", "").strip()})


# ── App ──────────────────────────────────────────────────────────────

async def on_startup(app):
    # Auto-start paid tunnels that were running before restart
    for t in load_tunnels():
        if t.get("tier") == "paid" and not t.get("provision_status"):
            _start_proc(t)


app = web.Application()
app.on_startup.append(on_startup)

app.router.add_get("/", ui)
app.router.add_get("/ui", ui)
app.router.add_get("/api/tunnels", api_tunnels)
app.router.add_post("/api/tunnels", api_create)
app.router.add_post("/api/tunnels/{id}/start", api_start)
app.router.add_post("/api/tunnels/{id}/stop", api_stop)
app.router.add_patch("/api/tunnels/{id}/port", api_update_port)
app.router.add_delete("/api/tunnels/{id}", api_delete)
app.router.add_post("/api/tunnels/purge-expired", lambda r: web.json_response({"ok": True}))
app.router.add_get("/api/status", api_status)
app.router.add_get("/api/config", api_config)
app.router.add_static("/static", Path(__file__).parent / "static")

if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=8099)
