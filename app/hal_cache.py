"""
Process-level HAL index cache.

Rebuilding the index means reading ~18,000 rows and constructing an object per
record. Doing that on every request costs a second or two of dead time on every
click -- switching tabs, changing a filter, picking a card -- which is exactly
the latency a reviewer working a long queue feels most.

So it's built once and reused. Two things keep it from going stale:

  - a TTL, so a sync in another process (the cron worker) is picked up within a
    few minutes without any coordination;
  - an explicit `invalidate()` the sync calls in-process, so a manual "Sync HAL
    now" is reflected immediately rather than after the TTL.

Thread-safe because uvicorn serves requests from a thread pool, and two
concurrent rebuilds would defeat the point.
"""

from __future__ import annotations

import json
import logging
import threading
import time

from sqlalchemy.orm import Session

from engine.hal import HalIndex, HalRecord
from .models_db import HalRow

log = logging.getLogger(__name__)

# How long a cached index is trusted before it's rebuilt. Short enough that a
# cron-process sync shows up quickly, long enough that a burst of clicks pays
# the build cost once.
TTL_SECONDS = 180

_lock = threading.Lock()
_index: HalIndex | None = None
_built_at: float = 0.0
_row_count: int = 0


def invalidate() -> None:
    """Force the next request to rebuild. Called after an in-process sync."""
    global _index, _built_at
    with _lock:
        _index = None
        _built_at = 0.0
    log.info("HAL index cache invalidated")


def _build(db: Session) -> HalIndex | None:
    rows = db.query(HalRow).all()
    if not rows:
        return None
    return HalIndex([
        HalRecord(
            record_id=r.record_id,
            holder_name=r.holder_name,
            team=r.team,
            statuses=json.loads(r.statuses_json or "{}"),
            plan_type=r.plan_type,
            has_payment_plan=r.has_payment_plan,
        )
        for r in rows
    ])


def get_index(db: Session) -> HalIndex | None:
    """Return the cached index, rebuilding if missing or past its TTL."""
    global _index, _built_at, _row_count

    now = time.monotonic()
    if _index is not None and (now - _built_at) < TTL_SECONDS:
        return _index

    with _lock:
        # Another thread may have rebuilt while this one waited on the lock.
        now = time.monotonic()
        if _index is not None and (now - _built_at) < TTL_SECONDS:
            return _index

        started = time.perf_counter()
        _index = _build(db)
        _built_at = now
        _row_count = len(_index.by_name) if _index else 0
        log.info("HAL index rebuilt: %d distinct names in %.0f ms",
                 _row_count, (time.perf_counter() - started) * 1000)
        return _index


def stats() -> dict:
    age = time.monotonic() - _built_at if _built_at else None
    return {
        "cached": _index is not None,
        "distinct_names": _row_count,
        "age_seconds": round(age) if age is not None else None,
        "ttl_seconds": TTL_SECONDS,
    }
