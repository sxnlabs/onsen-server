"""One-line, never-empty rendering of an exception.

`str(exc)` is empty for the exceptions this app hits most — `socket.timeout`,
`asyncio.TimeoutError`, a bare `ConnectionResetError` — which is how the spa
outage of 2026-08-22 spent 46 hours logging `network error (attempt 1): ` and
showing `status failed after 3 attempts: ` in the UI banner. Always carry the
class name; append the message only when there is one.
"""

from __future__ import annotations


def describe(exc: BaseException) -> str:
    text = str(exc).strip()
    name = type(exc).__name__
    return f"{name}: {text}" if text else name
