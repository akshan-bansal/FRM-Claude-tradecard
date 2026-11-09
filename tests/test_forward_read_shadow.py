"""Phase 4 shadow: the read is logged beside an entry, carries the intent id, and never changes a size."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pandas as pd
import pytest

from trading_live_claude.monitor.forward_read_shadow import ForwardReadShadow
from trading_live_claude.monitor.live_loop import LiveMonitor, MonitorEvent
from trading_live_claude.monitor.session import SessionError, _forward_read_shadow
from trading_live_claude.risk.quantity import QuantityRule
from trading_live_claude.risk.sizing import PositionSizer
from trading_live_claude.risk.sizing_policy import SizingPolicy, calendar_days
from trading_live_claude.strategies.base import Strategy, StrategyContext

NOW = datetime(2026, 11, 1, tzinfo=UTC)


def _table(tmp_path: Path, rows: list[dict[str, Any]]) -> Path:
    path = tmp_path / "table.parquet"
    pd.DataFrame(rows).to_parquet(path, index=False)
    return path


def _row(days_ago: float, ret: float, *, symbol: str = "PAXG/USD", strategy: str = "sig", held: float = 3) -> dict[str, Any]:
    entry = NOW - timedelta(days=days_ago)
    return {"strategy": strategy, "symbol": symbol, "entry": entry, "exit": entry + timedelta(days=held),
            "net_return": ret, "bars": int(held), "vol": 0.5}


def test_a_read_uses_only_trades_closed_a_purge_before_now(tmp_path: Path) -> None:
    rows = [_row(100 + 10 * i, 0.02) for i in range(8)]
    base = ForwardReadShadow.load(_table(tmp_path, rows)).read("sig", "PAXG/USD", as_of=NOW, annual_vol=0.5, raw_shares=1.0)
    late = ForwardReadShadow.load(_table(tmp_path, [*rows, _row(5, -9.0)])).read(
        "sig", "PAXG/USD", as_of=NOW, annual_vol=0.5, raw_shares=1.0)       # closes 2 days ago, inside the purge
    assert base["has_read"] and base["edge"] == late["edge"] and base["acts"] is False
    assert base["raw_shares_shadow"] == pytest.approx(base["tilt"], abs=1e-4)   # the logged tilt is rounded


def test_no_read_gives_todays_size_and_the_table_is_versioned(tmp_path: Path) -> None:
    shadow = ForwardReadShadow.load(_table(tmp_path, [_row(100 + 10 * i, 0.02) for i in range(8)]))
    got = shadow.read("other", "PAXG/USD", as_of=NOW, annual_vol=0.5, raw_shares=2.0)
    assert not got["has_read"] and got["tilt"] == 1.0 and got["raw_shares_shadow"] == 2.0
    assert len(shadow.table_version) == 16


def test_a_table_that_will_not_load_stops_the_session(tmp_path: Path) -> None:
    plan = SimpleNamespace(forward_read_shadow=str(tmp_path / "missing.parquet"))
    with pytest.raises(SessionError):
        _forward_read_shadow(plan, lambda _m: None)  # type: ignore[arg-type]
    assert _forward_read_shadow(SimpleNamespace(forward_read_shadow=""), lambda _m: None) is None  # type: ignore[arg-type]


# ---- the live loop -------------------------------------------------------------------------------


@dataclass
class _Quote:
    mid: float
    lastTradePrice: float

    @property
    def askPrice(self) -> float:
        return self.mid

    @property
    def bidPrice(self) -> float:
        return self.mid


class _Broker:
    def equity(self, account_number: str, currency: str = "CAD") -> float:
        return 100_000.0

    def positions(self, account_number: str) -> list[Any]:
        return []

    def quote(self, symbol: str) -> _Quote:
        return _Quote(4300.0, 4300.0)


class _Market:
    def recent(self, symbol: str, bars: int, interval: str = "1d") -> pd.DataFrame:
        close = 4300.0 * np.cumprod(1 + np.random.default_rng(7).normal(0, 0.01, bars + 30))
        return pd.DataFrame({"close": close, "high": close * 1.01, "low": close * 0.99})


class _Ledger:
    def __init__(self) -> None:
        self.rows: list[tuple[str, dict[str, Any], str | None]] = []

    def append(self, event: str, payload: dict[str, Any], *, intent_id: str | None = None, **_: Any) -> None:
        self.rows.append((event, payload, intent_id))


class _Router:
    def __init__(self) -> None:
        self.intents: list[Any] = []
        self.ledger = _Ledger()

    def submit(self, intent: Any, **kw: Any) -> None:
        self.intents.append(intent)


class _Signal(Strategy):
    name = "sig"

    def required_history_bars(self) -> int:
        return 25

    def generate_signals(self, df: pd.DataFrame, ctx: StrategyContext) -> pd.DataFrame:
        out = df.copy()
        out["entry"], out["exit"], out["atr"], out["signal_strength"] = 1, 0, 40.0, 0.05
        return out


class _Shadow:
    def __init__(self, tilt: float = 2.0, fail: bool = False) -> None:
        self.tilt, self.fail = tilt, fail

    def read(self, strategy: str, symbol: str, *, as_of: datetime, annual_vol: float | None,
             raw_shares: float) -> dict[str, Any]:
        if self.fail:
            raise RuntimeError("table gone")
        return {"strategy": strategy, "symbol": symbol, "tilt": self.tilt, "raw_shares_shadow": raw_shares * self.tilt,
                "acts": False}


def _step(tmp_path: Path, shadow: Any) -> tuple[_Router, list[dict[str, Any]]]:
    router, events = _Router(), list[MonitorEvent]()
    journal = tmp_path / f"sizing_{id(shadow)}.jsonl"
    policy = SizingPolicy(quantity_rule_for=lambda _s: QuantityRule(step=1e-8, min_qty=0.001, min_notional=0.5),
                          periods_per_year_for=calendar_days, journal_path=journal)
    LiveMonitor(broker=_Broker(), market=_Market(), strategy=_Signal(), sizer=PositionSizer(),  # type: ignore[arg-type]
                router=router, account_number="X", symbols=["PAXG/USD"], on_event=events.append,  # type: ignore[arg-type]
                emit_on_change_only=False, risk_model="atr", heat_aggregation="sum", sizing_policy=policy,
                forward_read_shadow=shadow).step()
    return router, [json.loads(x) for x in journal.read_text().splitlines()]


def test_the_shadow_never_changes_the_size_and_ledgers_with_the_intent_id(tmp_path: Path) -> None:
    off, _ = _step(tmp_path, None)
    on, journal = _step(tmp_path, _Shadow(tilt=2.0))
    assert len(off.intents) == len(on.intents) == 1
    assert on.intents[0].shares == off.intents[0].shares
    read = journal[0]["forward_read"]
    assert read["tilt"] == 2.0 and read["shares_today"] == pytest.approx(on.intents[0].shares)
    events = {e: (p, i) for e, p, i in on.ledger.rows}
    assert "SIZING_READ" in events and "SIZING_READ" not in {e for e, _, _ in off.ledger.rows}
    assert events["SIZING_READ"][1] == events["STRATEGY_SIGNAL"][1] == on.intents[0].intent_id


def test_a_failing_read_never_stops_the_entry(tmp_path: Path) -> None:
    router, journal = _step(tmp_path, _Shadow(fail=True))
    assert len(router.intents) == 1 and "forward_read" not in journal[0]
    assert "SIZING_READ" not in {e for e, _, _ in router.ledger.rows}
