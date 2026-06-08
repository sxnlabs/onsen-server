# Onsen

Onsen is the local spa control project, split across three locations:

- **this repo** (`~/Workspace/SXN-Labs/onsen`) — the FastAPI server that owns the
  single TCP connection to the spa.
- **`~/Workspace/Apps/Onsen`** — the native iOS/watchOS app (separate repo).
- **`~/Specs/Onsen`** — product specs, setup notes, screenshots, and branding assets.

The server is still the only component allowed to talk to the spa module on TCP
port 8990. Native apps should communicate with the server HTTP API only.

## Server

Run server commands from the repo root:

```bash
uv sync --extra dev --extra camera
INTEX_SPA_HOST=<spa-ip> uv run uvicorn web.main:make_app --factory --reload
uv run pytest -q
```

The detailed server spec is in `~/Specs/Onsen/server.md`. First-machine setup is
in `~/Specs/Onsen/setup.md`.

## iOS/watchOS

The app lives in `~/Workspace/Apps/Onsen`. The current native app plan is in
`~/Specs/Onsen/ios-watchos-app-plan.md`. Branding assets for the Onsen icon live
in `~/Specs/Onsen/branding/`.
