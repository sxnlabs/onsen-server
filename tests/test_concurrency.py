import asyncio
import threading

from intex_spa.concurrency import configure_io_threads, reset_io_threads, run_blocking
from web.main import _configured_io_threads


async def test_configured_io_pool_runs_blocking_work_concurrently():
    configure_io_threads(2)
    release = threading.Event()
    both_started = threading.Event()
    lock = threading.Lock()
    active = 0
    names: set[str] = set()

    def blocking(value: int) -> int:
        nonlocal active
        with lock:
            active += 1
            names.add(threading.current_thread().name)
            if active == 2:
                both_started.set()
        if not release.wait(timeout=2):
            raise TimeoutError("test did not release worker")
        return value

    tasks = [asyncio.create_task(run_blocking(blocking, i)) for i in range(2)]
    try:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 1.0
        while not both_started.is_set():
            if loop.time() >= deadline:
                break
            await asyncio.sleep(0.01)

        assert both_started.is_set()
        assert len(names) == 2
        assert all(name.startswith("onsen-io") for name in names)

        release.set()
        assert await asyncio.gather(*tasks) == [0, 1]
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        reset_io_threads()


def test_configured_io_threads_from_env(monkeypatch):
    monkeypatch.setenv("HERMES_IO_THREADS", "3")
    assert _configured_io_threads() == 3

    monkeypatch.setenv("HERMES_IO_THREADS", "bad")
    assert _configured_io_threads() == 8

    monkeypatch.setenv("HERMES_IO_THREADS", "0")
    assert _configured_io_threads() == 1
