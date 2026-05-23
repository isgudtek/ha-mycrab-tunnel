# Testing Checklist — mycrab Tunnel HA Addon

## Status: NOT TESTED — do not submit to HACS default yet

---

## Setup to test

1. Have a Home Assistant OS or Supervised install running
2. Add repo in HA: **Settings → Add-ons → Add-on Store → ⋮ → Repositories**
   ```
   https://github.com/isgudtek/ha-mycrab-tunnel
   ```
3. Install **mycrab Tunnel** → Start

---

## Things to verify

### Dashboard UI
- [ ] Sidebar panel appears with tunnel icon
- [ ] Stats bar shows 0/0/0 on fresh install
- [ ] "New tunnel" button opens modal
- [ ] Quick port chips (8123, 5005, 1880…) fill the port field
- [ ] Cancel / Escape closes modal

### Tunnel creation (free tier)
- [ ] Leave token blank → click Create
- [ ] Progress steps animate (Reserving → Configuring → Starting → Ready)
- [ ] Tunnel card appears with `agent-XXXXXX.mycrab.space` URL
- [ ] URL is reachable from outside the network
- [ ] HA is accessible at that URL

### Tunnel creation (paid token)
- [ ] Paste a valid mycrab token → Create
- [ ] Gets the custom subdomain, not agent-XXXXXX
- [ ] URL resolves correctly

### Tunnel controls
- [ ] Stop button stops the tunnel (cloudflared process dies)
- [ ] Start button restarts it
- [ ] URL stops responding after Stop, comes back after Start
- [ ] Delete removes card and cleans up config file

### HA config tab integration
- [ ] Set a token in the addon **Configuration** tab → Save
- [ ] Open "New tunnel" modal → token field pre-filled

### Persistence
- [ ] Create a tunnel → restart the addon → tunnel card still there
- [ ] Was it running before restart? Does it auto-start? (currently: no — needs manual Start after restart)

### Multi-service
- [ ] Create a second tunnel on a different port (e.g. 1880 for Node-RED)
- [ ] Both tunnels run simultaneously

---

## Known unknowns (blockers if broken)

- `api.mycrab.space/agent/register` — does this endpoint exist? The install.sh uses an interactive agent flow, not a direct REST call. **This is the most likely failure point.**
- cloudflared inside Alpine Docker — does the downloaded binary actually run on armv7/aarch64?

---

## When all boxes checked → reopen HACS PR

```
https://github.com/hacs/default/pull/7913
```
