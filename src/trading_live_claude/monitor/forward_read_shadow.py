"""Phase 4 of ``POSITION_SIZING_SCOPE.md``: the forward read, logged beside every entry, acting on nothing.

For each entry the live loop sizes, this gives the read for that strategy on that symbol (from walk-forward trades
that closed a purge before now) and the share count forward-read sizing would have asked for: today's raw share
count times the phase 3 tilt. The order keeps today's size; the read goes to ``state/sizing_decisions.jsonl`` and
a ``SIZING_READ`` ledger row, so matured reads can later be scored against what the trades did.

The table is a parquet of walk-forward trades (``strategy, symbol, entry, exit, net_return, bars, vol``), built by
``scripts/build_forward_read_table.py``. Its content hash is recorded with every read, so a read can be traced to
the evidence it came from.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from ..analysis.forward_read import TradeRow, causal_reads, read_at
from ..analysis.sizing_ab import Rules, tilt_for

COLUMNS = ("strategy", "symbol", "entry", "exit", "net_return", "bars", "vol")


@dataclass(frozen=True)
class ForwardReadShadow:
    trades: tuple[TradeRow, ...]
    reference: float | None          # median positive edge-to-vol ratio over the table's own reads
    table_version: str
    rules: Rules = field(default_factory=Rules)

    @classmethod
    def load(cls, path: Path | str) -> ForwardReadShadow:
        p = Path(path)
        raw = p.read_bytes()
        df = pd.read_parquet(p)
        missing = set(COLUMNS) - set(df.columns)
        if missing:
            raise ValueError(f"forward-read table {p} lacks {sorted(missing)}")
        df["entry"] = pd.to_datetime(df["entry"], utc=True)
        df["exit"] = pd.to_datetime(df["exit"], utc=True)
        rows = tuple(TradeRow(str(r.strategy), str(r.symbol), r.entry.to_pydatetime(), r.exit.to_pydatetime(),
                              float(r.net_return), int(r.bars)) for r in df.itertuples(index=False))
        vol = {(str(r.strategy), str(r.symbol), r.entry.to_pydatetime()): float(r.vol)
               for r in df.itertuples(index=False) if pd.notna(r.vol) and r.vol > 0}
        ratios = [rd.edge / vol[k] for rd in causal_reads(rows)
                  if rd.edge > 0 and (k := (rd.trade.strategy, rd.trade.symbol, rd.trade.entry)) in vol]
        return cls(rows, float(np.median(ratios)) if ratios else None, hashlib.sha256(raw).hexdigest()[:16])

    def read(self, strategy: str, symbol: str, *, as_of: datetime, annual_vol: float | None,
             raw_shares: float) -> dict[str, Any]:
        """What the read says, and the raw share count it would have asked for. Never raises on a missing read."""
        got = read_at(self.trades, strategy, symbol, as_of)
        edge, n_cell, n_strategy = got if got is not None else (None, 0, 0)
        tilt = tilt_for(edge, annual_vol, self.reference, self.rules)
        return {"strategy": strategy, "symbol": symbol, "as_of": as_of.isoformat(), "has_read": got is not None,
                "edge": edge, "n_cell": n_cell, "n_strategy": n_strategy, "annual_vol": annual_vol,
                "reference": self.reference, "tilt": round(tilt, 4), "raw_shares_today": raw_shares,
                "raw_shares_shadow": raw_shares * tilt, "table_version": self.table_version, "acts": False}
