"""Record sampled L2 snapshots from the live feeds, so microstructure features have a history to read.

The feeds in this package hand every book message to a callback and keep nothing. This writes a sample to
``state/l2/<venue>/<symbol>/<YYYY-MM-DD>.jsonl.gz`` every ``sample_seconds``. Between samples it keeps what a
thinned-out series would otherwise lose: order-flow imbalance is summed, so a sample's ``ofi`` covers the
whole interval, and ``checksum_ok`` is False if any update in the interval failed its check.

``recv_utc`` is the local receive time, which is when the book became known here. :func:`read_samples`
reads strictly before a cutoff, the same rule as ``intel/graph_store.py``.

It reads public market data only. It holds no key, sends no orders and imports nothing from the order path.
"""
from __future__ import annotations

import gzip
import json
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trading_live_claude.logging_setup import get_logger
from trading_live_claude.microstructure.kraken_l2 import BookUpdate

log = get_logger(__name__)


def default_l2_root() -> Path:
    from trading_live_claude.config.settings import get_settings
    return Path(get_settings().state_dir) / "l2"


def _safe_name(symbol: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", symbol)


@dataclass
class _Pending:
    ofi: float = 0.0
    updates: int = 0
    checksum_ok: bool = True
    last_sample: datetime | None = None
    rows: list[dict[str, Any]] = field(default_factory=list)


class L2Recorder:
    """Pass :meth:`on_update` to a feed's ``stream_order_book``. Call :meth:`close` when it stops."""

    def __init__(self, venue: str, *, root: str | Path | None = None, depth: int = 10, sample_seconds: float = 5.0,
                 flush_rows: int = 60, clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        if sample_seconds <= 0:
            raise ValueError("sample_seconds must be positive")
        self.venue = venue
        self.root = Path(root) if root is not None else default_l2_root()
        self.depth = depth
        self.sample_seconds = float(sample_seconds)
        self.flush_rows = int(flush_rows)
        self._clock = clock
        self._pending: dict[str, _Pending] = {}

    def on_update(self, update: BookUpdate) -> None:
        now = self._clock()
        p = self._pending.setdefault(update.symbol, _Pending())
        p.ofi += float(update.ofi)
        p.updates += 1
        p.checksum_ok = p.checksum_ok and bool(update.checksum_ok)
        if p.last_sample is not None and (now - p.last_sample).total_seconds() < self.sample_seconds:
            return
        book = update.book
        p.rows.append({
            "recv_utc": now.isoformat(), "venue": self.venue, "symbol": update.symbol,
            "bids": [[lvl.price, lvl.size] for lvl in book.bids[:self.depth]],
            "asks": [[lvl.price, lvl.size] for lvl in book.asks[:self.depth]],
            "microprice": update.microprice, "imbalance": update.imbalance,
            "ofi": p.ofi, "updates": p.updates, "checksum_ok": p.checksum_ok,
        })
        p.ofi, p.updates, p.checksum_ok, p.last_sample = 0.0, 0, True, now
        if len(p.rows) >= self.flush_rows:
            self._flush(update.symbol, p)

    def _flush(self, symbol: str, pending: _Pending) -> None:
        rows, pending.rows = pending.rows, []
        by_day: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            by_day.setdefault(row["recv_utc"][:10], []).append(row)
        for day, day_rows in by_day.items():
            path = self.root / self.venue / _safe_name(symbol) / f"{day}.jsonl.gz"
            path.parent.mkdir(parents=True, exist_ok=True)
            with gzip.open(path, "at", encoding="utf-8") as fh:
                fh.writelines(json.dumps(r) + "\n" for r in day_rows)

    def flush(self) -> None:
        for symbol, pending in self._pending.items():
            if pending.rows:
                self._flush(symbol, pending)

    def close(self) -> None:
        self.flush()


def read_samples(venue: str, symbol: str, *, before: datetime, root: str | Path | None = None) -> Iterator[dict[str, Any]]:
    """Samples received strictly before ``before``, oldest first. A cut-off last block ends the read early."""
    if before.tzinfo is None or before.utcoffset() is None:
        raise ValueError("before needs a timezone")
    folder = (Path(root) if root is not None else default_l2_root()) / venue / _safe_name(symbol)
    if not folder.exists():
        return
    cutoff = before.astimezone(UTC)
    last_day = cutoff.date().isoformat()
    for path in sorted(folder.glob("*.jsonl.gz")):
        if path.name[:10] > last_day:
            break
        try:
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                for line in fh:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if datetime.fromisoformat(row["recv_utc"]) < cutoff:
                        yield row
        except (EOFError, OSError) as e:           # a crash mid-write leaves a truncated gzip block
            log.warning("l2_recorder.truncated_file", path=str(path), error=str(e))
