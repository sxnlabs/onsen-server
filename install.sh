#!/usr/bin/env bash
# Install the spa controller as a user LaunchAgent (com.sxnlabs.spa).
#
#   INTEX_SPA_HOST=<spa-ip> ./install.sh            # localhost only
#   HERMES_HOST=0.0.0.0 INTEX_SPA_HOST=<spa-ip> ./install.sh   # reachable from the iPhone
#
set -euo pipefail
cd "$(dirname "$0")"
WORKDIR="$(pwd)"

SPA_HOST="${INTEX_SPA_HOST:?set INTEX_SPA_HOST, e.g. INTEX_SPA_HOST=<spa-ip>}"
SPA_PORT="${INTEX_SPA_PORT:-8990}"
POLL="${INTEX_SPA_POLL:-10}"
BIND="${HERMES_HOST:-127.0.0.1}"   # 0.0.0.0 to expose on the LAN
PORT="${HERMES_PORT:-8731}"
IO_THREADS="${HERMES_IO_THREADS:-8}"
LABEL="com.sxnlabs.spa"

UV="$(command -v uv || true)"
if [ -z "$UV" ]; then
  echo "uv not found. Install it:  curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
  exit 1
fi

echo "→ provisioning venv (uv sync, incl. dev for the smoke test)"
# .python-version pins CPython 3.12. Don't bump to 3.14 without testing the service
# under launchd for ≥30 min — uvicorn[standard] pulls uvloop/httptools/pydantic-core
# (all native), and 3.14 produced silent ~30s deaths with no traceback on this machine.
uv sync --extra dev

echo "→ smoke test (offline)"
uv run python -m pytest -q

mkdir -p state

# UI password (optional): persisted to state/.password, kept out of the plist
if [ -n "${HERMES_PASSWORD:-}" ]; then
  printf '%s' "$HERMES_PASSWORD" > state/.password
  chmod 600 state/.password
  echo "→ UI password set"
elif [ "$BIND" = "0.0.0.0" ]; then
  if [ "${HERMES_ALLOW_NO_PASSWORD_LAN:-}" != "1" ]; then
    echo "Refusing to expose the UI on the LAN (0.0.0.0) with no password." >&2
    echo "Add one: HERMES_PASSWORD=… HERMES_HOST=0.0.0.0 INTEX_SPA_HOST=$SPA_HOST ./install.sh" >&2
    echo "Override only for a deliberately isolated network: HERMES_ALLOW_NO_PASSWORD_LAN=1" >&2
    exit 1
  fi
  echo "⚠  Exposing the UI on the LAN (0.0.0.0) with NO password by explicit override."
  echo "   Lock the spa's own port at the UDM so only this Mac can reach"
  echo "   $SPA_HOST:$SPA_PORT (the firmware itself has no auth)."
fi

PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PATHV="$(dirname "$UV"):/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

sed -e "s#__UV__#${UV}#g" \
    -e "s#__WORKDIR__#${WORKDIR}#g" \
    -e "s#__HOME__#${HOME}#g" \
    -e "s#__BIND__#${BIND}#g" \
    -e "s#__PORT__#${PORT}#g" \
    -e "s#__SPA_HOST__#${SPA_HOST}#g" \
    -e "s#__SPA_PORT__#${SPA_PORT}#g" \
    -e "s#__POLL__#${POLL}#g" \
    -e "s#__IO_THREADS__#${IO_THREADS}#g" \
    -e "s#__PATH__#${PATHV}#g" \
    com.sxnlabs.spa.plist.tmpl > "$PLIST"

echo "→ (re)loading LaunchAgent"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
sleep 2  # bootout is async — let the old job fully exit or bootstrap hits EIO (err 5)
launchctl enable "gui/$(id -u)/$LABEL"
launchctl bootstrap "gui/$(id -u)" "$PLIST"
# RunAtLoad=true already starts the job — do NOT also kickstart -k here: a second
# start while the first is still binding leaves the spa's single socket contended
# and can wedge the process. One start only.

echo
echo "✓ $LABEL installed"
echo "  UI:    http://${BIND}:${PORT}"
echo "  spa:   ${SPA_HOST}:${SPA_PORT}  (poll ${POLL}s)"
echo "  I/O:   ${IO_THREADS} worker threads"
echo "  logs:  ${WORKDIR}/state/spa.err.log"
echo
echo "  uninstall:  launchctl bootout gui/$(id -u)/$LABEL && rm '$PLIST'"
