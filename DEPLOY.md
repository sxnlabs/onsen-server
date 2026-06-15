# Deploying Onsen on a remote server (Docker + WireGuard → UniFi)

Goal: run the server off a Linux host you already keep on, so the Mac at home no
longer has to stay powered just to control the spa. The UniFi gateway is always
on and terminates a WireGuard tunnel; the container dials the spa's LAN IP
through that tunnel.

## Why not Scaleway Serverless Containers

Serverless Containers are the wrong fit here: no persistent volume for `state/`,
scale-to-zero/idle freezing breaks the poll loop that keeps the spa's single TCP
connection alive, and there's no TUN device for WireGuard. Use a plain always-on
host (a VPS you own, or a small Scaleway **Instance**/VM) running Docker.

## Prerequisites

- An always-on Linux host with Docker + Docker Compose.
- A modern UniFi gateway (UDM / UDR / UCG / UXG, Network 8+) with the built-in
  **WireGuard VPN Server**.
- A DHCP reservation (fixed IP) for the spa — and for the IP camera, if you use it.
- The host kernel usually has WireGuard built in. If not, uncomment the
  `/lib/modules` mount in `docker-compose.yml`.

## 1. WireGuard server on UniFi

1. UniFi OS → **Settings → VPN → VPN Server → Create New → WireGuard**.
2. Add a client/peer for the remote host and download/copy its config.
3. If your home WAN IP is dynamic, enable UniFi **Dynamic DNS** and note the
   hostname — you'll use it as the peer `Endpoint`.

## 2. Lock down the spa

The spa firmware has **no authentication**. At the UniFi firewall, restrict the
spa's `:8990` (and the camera) so only the VPN peer / app host can reach them.

## 3. On the remote host

```bash
git clone <repo-url> onsen && cd onsen

cp .env.example .env
#   set INTEX_SPA_HOST (spa LAN IP), HERMES_PASSWORD, WEATHER_LAT/LON

cp wireguard/wg0.conf.example wireguard/wg0.conf
#   paste the UniFi peer config; then in wg0.conf:
#     - AllowedIPs = <your LAN subnet only>   (split tunnel; add the camera subnet if it differs)
#     - Endpoint   = <wan-or-ddns>:51820
#     - PersistentKeepalive = 25

# (don't start the app yet — verify the tunnel first, in step 4)
```

## 4. Verify the tunnel, then start

The spa accepts only **one** TCP client, so confirm reachability *before* the app
owns the connection. `docker compose run` brings up the WireGuard tunnel and runs
a one-off container **without** the supervisor — `probe.py` is read-only but opens
its own socket, so it must never run alongside the live `onsen` server:

```bash
docker compose run --rm onsen python3 probe.py "$INTEX_SPA_HOST"   # tunnel + spa OK
```

Then start the app and check it:

```bash
docker compose up -d --build
docker compose ps
curl -fsS http://127.0.0.1:8731/healthz
```

Open the UI: live water temperature should update (SSE), and a toggle (e.g.
bubbles) should round-trip and append a row to `state/commands.jsonl`. The camera
card shows the live frame and timelapse — there is no cover detection. To run
`probe.py` again later, stop the app first (`docker compose stop onsen`) — never
probe while `onsen` is running.

## 5. Cutover from the Mac (once)

The spa accepts only **one** TCP client, so the Mac LaunchAgent and the container
must never run at the same time.

1. On the Mac, stop the agent:
   `launchctl bootout gui/$(id -u)/com.sxnlabs.spa`
2. (Optional) preserve history/schedule by copying state to the server while the
   container is down — skip the now-defunct `cover_state.json`:
   `rsync -a --exclude cover_state.json state/ user@server:/path/to/onsen/state/`
   then `docker compose restart onsen` on the server.
3. Once the remote instance looks healthy, leave the agent off — the Mac can be
   shut down.

## Notes

- **One process only.** Never `docker compose up --scale onsen=2`, never add a
  uvicorn `--workers` flag. The image CMD runs a single uvicorn in `--factory`
  mode by design.
- **UI exposure.** The port is published on the host's `127.0.0.1:8731`. Front it
  with your existing reverse proxy (TLS) or reach it over the VPN. Always set
  `HERMES_PASSWORD`.
- **Persistence.** `state/` is a bind mount; history, schedule, login secret,
  pause flag, cooldowns and camera frames survive `docker compose restart` and
  host reboots (`restart: unless-stopped`).
