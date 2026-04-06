from __future__ import annotations

import logging
import os
import time
from contextlib import contextmanager
from threading import Lock
from typing import Any, Iterator


def configure_logging() -> None:
    """Configure stdlib logging (no extra deps)."""
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s - %(message)s")


def log_event(logger: logging.Logger, event: str, **fields: Any) -> None:
    # Keep it dead simple: one log line per event.
    if fields:
        logger.info("%s %s", event, fields)
    else:
        logger.info("%s", event)


@contextmanager
def timer(metric_key: str) -> Iterator[None]:
    start = time.perf_counter()
    try:
        yield None
    finally:
        METRICS.observe_ms(metric_key, (time.perf_counter() - start) * 1000)


class Metrics:
    """Very small in-memory metrics registry (counters + timings)."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._counters: dict[str, int] = {}
        self._timings_ms: dict[str, list[float]] = {}

    def inc(self, key: str, value: int = 1) -> None:
        with self._lock:
            self._counters[key] = int(self._counters.get(key, 0)) + int(value)

    def observe_ms(self, key: str, value_ms: float) -> None:
        with self._lock:
            self._timings_ms.setdefault(key, []).append(float(value_ms))

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "counters": dict(self._counters),
                "timings_ms": {k: list(v) for k, v in self._timings_ms.items()},
            }


METRICS = Metrics()

