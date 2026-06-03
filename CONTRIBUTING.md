# Contributing

This project was built for one specific Mac → Intex PureSpa setup. PRs and
issues are welcome but please match the existing constraints.

## Hard rules (don't fight these)

- **One TCP connection** to the spa, ever. The firmware allows a single client
  on :8990; the whole app is built around that. Never instantiate a second
  `IntexSpaClient` or `Supervisor`, never add `--workers` to uvicorn.
- **Toggles are toggles.** Functional commands (power/heater/filter/bubbles)
  invert state on the wire. `IntexSpaClient.set()` reads first and only writes
  on a desired-state mismatch. New commands follow the same pattern.
- **Pure decision engine, async reconciler.** `intex_spa/schedule.py::evaluate()`
  must stay pure; the async loop in `scheduler.py` is the only place that
  touches the spa. This is what keeps the rule logic unit-testable without a
  clock or a spa.
- **Fail-soft, stale-but-useful.** Every external dependency (spa, weather,
  camera) is best-effort. Errors are logged, the last good state is
  kept, the UI shows it with an "offline" badge if relevant.
- **No CDN imports.** All JS/CSS lives under `web/static/vendor/` so the app
  works offline on the LAN. Re-vendor by hand if you bump a version.
- **Secrets stay in `state/`** (gitignored). The `rtsps_url` with its token,
  `state/.password`, `state/.secret` — none of those ever reach git.

## Running tests

```bash
uv sync --extra dev --extra camera
uv run pytest -q
```

The tests are all offline (fake spa + mocked subprocess + Pillow/numpy guarded).
New code should come with new tests. The test suite must stay green
on a fresh checkout with **no `state/camera.json`** — that's the contract for
the master-switch design.

## Style

- Comments explain **why**, not what. Short. No "added in commit X" or
  "called by Y" — those rot.
- No emojis in code or commit messages.
- Long-running blocking work goes in `asyncio.to_thread` — see `weather.py`
  and `camera.py` for the pattern.
- Match the surrounding terseness. Don't add wrappers that don't pull weight.

## launchd gotchas

If you change anything about how the LaunchAgent starts — the plist template,
`install.sh`, the Python pin, the entry point — run it for ≥30 min and watch
`state/spa.err.log` plus `~/Library/Logs/DiagnosticReports/` for fresh
`Python-*.ips` crash reports. The README and `CLAUDE.md` document specific
modes (silent 30 s deaths on Python 3.14, ffmpeg silent hang without
`-f image2`, multiprocess wedge with `--workers 1`) that bit me — pinning
tests are there so they don't bite again.

## Reverse-engineering provenance

The wire protocol decode in `intex_spa/protocol.py` is derived from
[`mathieu-mp/aio-intex-spa`](https://github.com/mathieu-mp/aio-intex-spa) and
cross-checked against captured frames from a real PureSpa Baltik. If you have
captures from other Intex models, please open an issue with the hex dumps —
expanding the decode is the most useful thing this codebase could grow.
