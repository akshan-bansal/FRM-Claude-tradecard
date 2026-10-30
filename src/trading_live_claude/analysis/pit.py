"""Point-in-time helpers shared by the lens feature builders (feature store, graph adapter).

Every window is half-open ``[start, end)``: a record stamped exactly at a cutoff was not known before it.
"""
from __future__ import annotations

import json
from bisect import bisect_left
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


def parse_ts(raw: object) -> datetime | None:
    """A timezone-aware datetime from an ISO string (naive means UTC), or None if unparseable."""
    try:
        t = datetime.fromisoformat(str(raw).replace(" ", "T").replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def read_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    """Rows of a JSONL file; a missing file is empty and a corrupt line is skipped."""
    if not path.exists():
        return
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                yield row


class TimeIndex:
    """Sorted times with a payload, for half-open window queries ``[start, end)``."""

    def __init__(self, items: Iterable[tuple[datetime, Any]]) -> None:
        pairs = sorted(items, key=lambda p: p[0])
        self.times = [p[0] for p in pairs]
        self.items = [p[1] for p in pairs]

    def window(self, start: datetime, end: datetime) -> list[Any]:
        return self.items[bisect_left(self.times, start):bisect_left(self.times, end)]

    def times_in(self, start: datetime, end: datetime) -> list[datetime]:
        return self.times[bisect_left(self.times, start):bisect_left(self.times, end)]

    def last_before(self, end: datetime) -> tuple[datetime, Any] | None:
        i = bisect_left(self.times, end)  # strictly before ``end``
        return (self.times[i - 1], self.items[i - 1]) if i else None


def coverage_hours(index: TimeIndex, start: datetime, end: datetime) -> float:
    """Share of the window's hours with at least one record (heartbeat, fetched file) in them."""
    hours = max(1, round((end - start) / timedelta(hours=1)))
    seen = {int((t - start) / timedelta(hours=1)) for t in index.times_in(start, end)}
    return round(len(seen) / hours, 4)
