"""Bounded worker pool for blocking I/O.

The ASGI server stays single-process so there is exactly one Supervisor and one
TCP client for the spa. Blocking side work (camera ffmpeg, weather fetches,
image sampling) can still run concurrently in this process through this pool.
"""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import ParamSpec, TypeVar

P = ParamSpec("P")
T = TypeVar("T")

_executor: ThreadPoolExecutor | None = None


def configure_io_threads(max_workers: int | None) -> ThreadPoolExecutor | None:
    """Install a process-local executor used by run_blocking().

    Passing None or a value below 1 leaves asyncio's default to_thread executor
    in use. The caller owns shutdown via reset_io_threads().
    """
    global _executor
    reset_io_threads()
    if max_workers is None or max_workers < 1:
        return None
    _executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="onsen-io")
    return _executor


def reset_io_threads() -> None:
    global _executor
    executor, _executor = _executor, None
    if executor is not None:
        executor.shutdown(wait=False, cancel_futures=True)


async def run_blocking(func: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    """Run blocking work without stalling the event loop."""
    if _executor is None:
        return await asyncio.to_thread(func, *args, **kwargs)
    loop = asyncio.get_running_loop()
    call = functools.partial(func, *args, **kwargs)
    return await loop.run_in_executor(_executor, call)
