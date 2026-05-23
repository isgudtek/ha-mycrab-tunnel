# mycrab Tunnel — Home Assistant Add-on

Expose Home Assistant and any local service to the internet in 60 seconds — no port forwarding, no Cloudflare account, no CLI.

[![HACS](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)

---

## What it does

- Runs a secure [Cloudflare tunnel](https://www.cloudflare.com/products/tunnel/) behind the scenes
- Gives you a free `agent-XXXXXX.mycrab.space` URL instantly
- Dashboard to manage multiple tunnels (HA, Frigate, Node-RED, MQTT, Grafana…)
- Optional: bring your own memorable subdomain for $14.99/yr via [mycrab.space](https://mycrab.space)

**No Cloudflare account. No port forwarding. No YAML editing.**

---

## Install via HACS

1. In Home Assistant → HACS → Integrations → ⋮ → Custom repositories
2. Add `https://github.com/isgudtek/ha-mycrab-tunnel` → category **Add-on**
3. Install **mycrab Tunnel**
4. Restart Home Assistant

Or add the add-on repository directly:

**Settings → Add-ons → Add-on Store → ⋮ → Repositories**
```
https://github.com/isgudtek/ha-mycrab-tunnel
```

---

## Usage

1. Open the **mycrab Tunnel** panel in your HA sidebar
2. Click **New tunnel**
3. Choose the local port (default 8123 for HA)
4. Hit **Create** — your tunnel URL appears in seconds

### Custom domain (optional)

Get a token at [mycrab.space/domain-select.html](https://mycrab.space/domain-select.html) and paste it in the token field when creating a tunnel.

---

## Supported architectures

`amd64` · `aarch64` · `armv7` · `i386`

---

## vs Nabu Casa

| | mycrab Tunnel | Nabu Casa |
|---|---|---|
| Price | Free (custom domain $14.99/yr) | $6.50/month |
| Setup | 60 seconds | Minutes |
| Multiple services | Yes | HA only |
| Self-hosted | Yes | No |
| Account required | No | Yes |

---

## License

MIT
