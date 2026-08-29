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

## 6. Arm the SMS alerting

On 2026-08-22 the spa dropped off the network at 20:34 in mid-heat and nobody
knew for 46 hours: the UI kept showing the last good reading, `/healthz` kept
answering 200, and the only trace was `network error (attempt 1):` in the logs.

Onsen now texts its owner itself. It does **not** route through Argos: Argos runs
on this same host, so it can't be what notices this host go away — and a
spa-side fault (an E90, a heat cycle that stopped climbing) is something only
Onsen can name in the message.

Fill in `.env` (all of it, or none — see `.env.example`):

```bash
ONSEN_SMS_TO=+336xxxxxxxx
OVH_APPLICATION_KEY=…        # same OVH SMS account Argos uses
OVH_APPLICATION_SECRET=…
OVH_CONSUMER_KEY=…
OVH_SMS_SERVICE=sms-xxxxxxx-1
```

Then `docker compose up -d` and confirm it's armed:

`/api/alerts` sits behind the UI password, so it needs a login cookie — an
unauthenticated `curl` gets a 303 to `/login`, which `curl -f` does **not** treat
as a failure and which would look like a pass:

```bash
JAR=$(mktemp)
curl -s -c "$JAR" -o /dev/null -X POST http://127.0.0.1:8731/login \
  --data-urlencode "password=$(grep '^HERMES_PASSWORD=' .env | cut -d= -f2-)"
curl -s -b "$JAR" http://127.0.0.1:8731/api/alerts; rm -f "$JAR"
# {"enabled":true,"config":{...},"episodes":{}}   <- enabled:true means recipient AND OVH keys are present

docker compose logs onsen | grep -i "half-configured"   # any hit = armed in name only
```

### Prod smoke test

Two levels, and they don't prove the same thing.

**The transport** — credentials, signature, clock, recipient, credits. One SMS,
30 seconds, no socket to the spa (safe while the supervisor holds its one TCP
connection):

```bash
docker compose exec onsen /app/.venv/bin/python sms_probe.py
# recipient: ...8237 / service: sms-xxxxxxx-1 / sender: SXNLABS / sent
docker compose exec onsen /app/.venv/bin/python sms_probe.py --dry-run   # resolve config, spend nothing
```

Exit codes: 0 sent, 1 not configured (it names which half is missing), 2 refused
by OVH. On the LaunchAgent path it's `uv run python sms_probe.py` from the repo.

**`sent` means OVH accepted the job, not that a phone rang.** The API's `ptt`
delivery code stays `0` whatever happens — even for a number that doesn't exist —
so it can't be used as proof either. And an alphanumeric sender like `SXNLABS`
has no entry in the address book, so iOS files it under Filters → Unknown
Senders and never notifies: on 2026-08-25 three test messages arrived, were
read back from the OVH outgoing log, and sat unseen in that tab. The test is
only finished when the message is seen **on the device**, in a thread that
notifies. If it lands in the filtered tab, the alert is not an alert: mark the
thread as not junk and turn off Settings → Apps → Messages → Filter Unknown
Senders.

Fix it on the phone, not in the sender field. `SXNLABS` is the house sender
across every SXN Labs service (Argos alerts land in the same thread), so
swapping this one app to a numeric sender would only hide the problem here and
leave every other alert filtered.

**The rules** — `evaluate()`, the debounce, the persisted episode, the resolution
SMS. None of that is exercised above. To rehearse it end to end without touching
the hardware, point the app at an address that doesn't answer:

```bash
cp .env .env.test-backup
sed -i 's/^INTEX_SPA_HOST=.*/INTEX_SPA_HOST=192.168.20.254/' .env   # dead IP on the IoT VLAN
echo 'ONSEN_ALERT_UNREACHABLE_AFTER=60' >> .env
docker compose up -d
#   ~2 min later: "spa injoignable depuis 1 min…"
mv .env.test-backup .env && docker compose up -d
#   next tick: "spa de nouveau joignable"
```

The spa goes unmanaged for those two minutes, which is harmless. Don't test by
swapping `ONSEN_SMS_TO` for another number — OVH refuses an unknown receiver, so
you'd be exercising the failure path, not the nominal one.

What earns a text, one per episode plus one when it clears:

| Condition | Fires after |
| --- | --- |
| Spa unreachable (spa, tunnel or host) | 1 h of silence — `ONSEN_ALERT_UNREACHABLE_AFTER` |
| The spa reports an error code (E90…) | 5 min |
| Heater on ≥2 h without gaining 1 °C | the window itself — `ONSEN_ALERT_HEATING_STALL_HOURS` |

Faults only — an SMS costs money, so it has to mean something is broken. A water
floor (“water at/below N °C while the setpoint is higher”) is available but
**off unless you set `ONSEN_ALERT_WATER_LOW_C`**: a spa climbing from 29 °C to
its 37 °C setpoint is a normal cold start, and alerting on every one of them
buried the three rules above. Set it to a temperature (`5`, say) if you want a
freeze warning; it fires after 15 min and clears like the others.

**Upgrading from a version that had the floor on?** Unset is what means off, so
an install that never named the variable goes quiet by itself — but one that
*does* still carry `ONSEN_ALERT_WATER_LOW_C=30` (the value the old docs printed
as the default) keeps texting every cold start. Delete that line from `.env`, or
from `state/.sms` on the LaunchAgent path, where `install.sh` may have written it
and where a reinstall leaves the existing file untouched.

Each rule's variable also accepts `off` (or `none`, or `0`) to switch it off —
`ONSEN_ALERT_HEATING_STALL_HOURS=off` retires the stall check. The outage alarm
has no off switch: it's the one this whole module was written for. A value that
isn't a number is logged and ignored rather than raised — an alerting typo must
not be what stops the spa from being driven.

On the **LaunchAgent** path there is no `.env` and launchd carries no environment
of its own, so `install.sh` writes the same settings to `state/.sms` (0600, kept
out of the plist, exactly like `state/.password`):

```bash
ONSEN_SMS_TO=+336xxxxxxxx OVH_APPLICATION_KEY=… OVH_APPLICATION_SECRET=… \
  OVH_CONSUMER_KEY=… OVH_SMS_SERVICE=sms-xxxxxxx-1 \
  INTEX_SPA_HOST=<spa-ip> ./install.sh
```

`state/alerts.json` remembers open episodes, so a redeploy in the middle of an
outage doesn't text you a second time. To test the wiring end-to-end without
waiting an hour, drop `ONSEN_ALERT_UNREACHABLE_AFTER=60` and pull the spa's plug.

**What this still doesn't cover:** if the whole host dies, nothing on it can text
you. `/spa-healthz` (public, 503 as soon as the spa link is stale) is there for an
off-host monitor to poll — put one somewhere other than this machine.

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
