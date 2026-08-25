# Onsen server — single-process FastAPI runtime.
#
# Mirrors the LaunchAgent contract exactly: ONE uvicorn process in factory mode,
# never --workers (that spawns a process manager around the spa's single TCP
# socket). Python is pinned to 3.12 (see pyproject.toml / .python-version).
#
# The live camera feed shells out to a system `ffmpeg`; there are no Python
# image deps anymore (cover detection was removed), so the image stays slim.

# -- build stage: resolve the venv from the lockfile -------------------------
FROM python:3.12-slim-bookworm AS builder

# uv binary only — we use the image's own CPython 3.12 (UV_PYTHON_DOWNLOADS=never)
# so the venv's interpreter path matches the runtime stage below.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
ENV UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_LINK_MODE=copy

WORKDIR /app
# Only the dependency manifests — keeps this layer cached across code changes.
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev

# -- runtime stage -----------------------------------------------------------
FROM python:3.12-slim-bookworm

# ffmpeg: RTSP snapshot/timelapse. tini: real PID 1 so SIGTERM reaches uvicorn
# and the lifespan teardown closes the spa socket cleanly. tzdata: local-time
# scheduling (the schedule rules are wall-clock, Europe/Paris).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg tini tzdata \
    && rm -rf /var/lib/apt/lists/*

# HOME is needed for httpx/anyio trust-store + temp defaults; PATH puts the venv
# first so `python`/`uvicorn` resolve to the synced environment.
ENV HOME=/app \
    TZ=Europe/Paris \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
# App code is run from the working directory (pyproject has package = false),
# so it lives next to the venv rather than being installed into it.
COPY intex_spa ./intex_spa
COPY web ./web
COPY probe.py sms_probe.py ./
RUN mkdir -p /app/state

EXPOSE 8731

# Liveness: the app's own health endpoint, hit inside this container's netns.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8731/healthz', timeout=4).status == 200 else 1)" || exit 1

ENTRYPOINT ["tini", "--"]
# ONE process. No --workers, no --reload. --factory builds the app once.
CMD ["/app/.venv/bin/python", "-m", "uvicorn", "web.main:make_app", \
     "--factory", "--host", "0.0.0.0", "--port", "8731"]
