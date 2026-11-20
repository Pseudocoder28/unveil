"""Run per-page work across CPU cores.

Pages of a PDF are independent, so long documents are split into chunks and each chunk
is handled by a separate process that opens the file itself (PyMuPDF objects cannot be
shared between processes). Short documents stay in-process: starting workers costs
about half a second, which only pays off past a dozen or so pages.
"""

from __future__ import annotations

import os
from concurrent.futures import ProcessPoolExecutor
from multiprocessing import get_context
from typing import Callable, Iterable

MIN_PAGES_FOR_PARALLEL = int(os.environ.get("UNVEIL_PARALLEL_MIN_PAGES", "12"))
PAGES_PER_WORKER_MIN = 4


def max_workers() -> int:
    env = os.environ.get("UNVEIL_WORKERS")
    if env:
        return max(1, int(env))
    return max(1, min(8, (os.cpu_count() or 2) - 1))


def page_chunks(n_pages: int) -> list[list[int]]:
    """Split pages into contiguous chunks: one chunk if small, else ~2 chunks per worker."""
    workers = max_workers()
    if n_pages < MIN_PAGES_FOR_PARALLEL or workers == 1:
        return [list(range(n_pages))]
    n_chunks = min(workers * 2, max(1, n_pages // PAGES_PER_WORKER_MIN))
    size = -(-n_pages // n_chunks)
    return [list(range(i, min(n_pages, i + size))) for i in range(0, n_pages, size)]


def _can_spawn() -> bool:
    """Worker processes re-import the caller's main script. That is only safe when the
    script guards its work with `if __name__ == "__main__":` (unveil's own CLI and web app
    do). For an unguarded script, or inside a worker, stay single-process instead of crashing."""
    import multiprocessing
    import sys

    if multiprocessing.parent_process() is not None:
        return False
    main = sys.modules.get("__main__")
    path = getattr(main, "__file__", None)
    if not path:
        return True  # REPL, python -c, notebooks: nothing to re-import
    try:
        src = open(path, encoding="utf-8", errors="ignore").read()
    except OSError:
        return False
    return "__name__" in src and "__main__" in src


def run_chunks(fn: Callable, path: str, chunks: list[list[int]], *args) -> Iterable:
    """Yield fn(path, chunk, *args) for each chunk, in order."""
    if len(chunks) > 1 and not _can_spawn():
        chunks = [[p for c in chunks for p in c]]
    if len(chunks) <= 1:
        for c in chunks:
            yield fn(path, c, *args)
        return
    workers = min(max_workers(), len(chunks))
    with ProcessPoolExecutor(max_workers=workers, mp_context=get_context("spawn")) as ex:
        futures = [ex.submit(fn, path, c, *args) for c in chunks]
        for fut in futures:
            yield fut.result()
