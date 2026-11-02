"""LiveMonitor against the paper router: the guards that have to actually fire.

Market-hours skip, per-symbol entry channels, the level-path re-entry guards, the open notional the
gross-leverage gate needs, and forced exits. test_entry_triggers.py covers the columns; these run the
real ``step()``.
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from trading_live_claude.audit.ledger import Ledger, read_stream
from trading_live_claude.brokers.base import BrokerError
from trading_live_claude.brokers.models import Position, Quote
from trading_live_claude.brokers.paper import PaperBroker
from trading_live_claude.execution.router import Router
from trading_live_claude.monitor.live_loop import LiveMonitor, monitor_policy
from trading_live_claude.risk.sizing import PositionSizer
from trading_live_claude.strategies.base import Strategy, StrategyContext
from trading_live_claude.strategies.examples.bollinger import BollingerMeanRevert


class _Script(Strategy):
    """Emits whatever the test scripts for the last bar."""

    name = "scripted"
    supports_level_trigger = True

    def __init__(self, entry: int = 0, level: int = 0, exit_: int = 0, atr: float = 2.0) -> None:
        super().__init__()
        self.entry, self.level, self.exit_, self.atr = entry, level, exit_, atr

    def required_history_bars(self) -> int:
        return 30

    def generate_signals(self, df: pd.DataFrame, ctx: StrategyContext) -> pd.DataFrame:
        out = df.copy()
        for col, v in (("entry", self.entry), ("entry_level", self.level), ("exit", self.exit_)):
            out[col] = 0
            out.loc[out.index[-1], col] = v
        out["atr"] = self.atr
        return out


class _Feed:
    name = venue = "kraken"

    def __init__(self, price: float = 100.0) -> None:
        self.price = price

    def quote(self, symbol: str) -> Quote:
        return Quote(symbol=symbol, symbolId=1, bidPrice=self.price, askPrice=self.price,
                     lastTradePrice=self.price, time=datetime.now(UTC))

    def quotes(self, symbols: list[str]) -> list[Quote]:
        return [self.quote(s) for s in symbols]


class _Market:
    """A frame with a settable last-bar stamp, so a test can move to the next bar."""

    bar = 0

    def recent(self, _symbol: str, bars: int = 60, interval: str = "1d") -> pd.DataFrame:
        n = max(bars, 60)
        close = 100.0 + np.sin(np.arange(n) / 5.0)
        return pd.DataFrame({"time": pd.date_range("2026-01-01", periods=n, tz="UTC") + pd.Timedelta(days=self.bar),
                             "open": close, "high": close, "low": close, "close": close,
                             "volume": np.full(n, 1000)})


def _monitor(tmp: Path, strat: Strategy, symbols: list[str] | None = None, *,
             ledger: Ledger | None = None, price: float = 100.0, router: object = None,
             **kw: object) -> tuple[LiveMonitor, PaperBroker, _Feed]:
    feed = _Feed(price)
    pb = PaperBroker(feed=feed, journal_dir=tmp)  # type: ignore[arg-type]
    r = router or Router.build_default(mode="paper", broker=pb, state_dir=tmp, ledger=ledger)
    m = LiveMonitor(broker=pb, market=_Market(), strategy=strat,  # type: ignore[arg-type]
                    sizer=PositionSizer(risk_pct=0.01), router=r,  # type: ignore[arg-type]
                    account_number="PAPER-001", symbols=symbols or ["BTC/USD"],
                    account_currency="USD", risk_model="atr", heat_aggregation="sum", **kw)  # type: ignore[arg-type]
    return m, pb, feed


def _qty(pb: PaperBroker, symbol: str = "BTC/USD") -> float:
    return next((p.openQuantity for p in pb.positions("PAPER-001") if p.symbol == symbol), 0.0)


class _Recorder:
    """Router stand-in for what the monitor hands over. Places nothing."""

    def __init__(self, result: object = None, raises: Exception | None = None) -> None:
        self.calls: list[dict[str, object]] = []
        self.result, self.raises = result, raises
        self.strategy_version_for = None

    def submit(self, intent: object, **kw: object) -> object:
        self.calls.append({"intent": intent, **kw})
        if self.raises:
            raise self.raises
        return self.result

    def check_forced_exits(self, *_a: object) -> list[dict[str, object]]:
        return []


# ---------------------------------------------------------------- market-hours guard

def test_closed_venue_is_skipped_entries_and_exits_alike(tmp_path: Path) -> None:
    m, pb, _ = _monitor(tmp_path, _Script(entry=1), market_open_for=lambda _s: False)
    assert m.step() == []
    assert _qty(pb) == 0.0


def test_open_venue_trades_as_before(tmp_path: Path) -> None:
    m, pb, _ = _monitor(tmp_path, _Script(entry=1), market_open_for=lambda _s: True)
    assert [e.kind for e in m.step()] == ["entry"]
    assert _qty(pb) > 0


def test_guard_error_reads_as_open(tmp_path: Path) -> None:
    def boom(_s: str) -> bool:
        raise RuntimeError("calendar broke")

    m, pb, _ = _monitor(tmp_path, _Script(entry=1), market_open_for=boom)
    m.step()
    assert _qty(pb) > 0


def test_only_the_closed_symbol_is_skipped(tmp_path: Path) -> None:
    m, _, _ = _monitor(tmp_path, _Script(), ["BTC/USD", "VOO"],
                       market_open_for=lambda s: s == "BTC/USD")
    assert [e.symbol for e in m.step()] == ["BTC/USD"]


def test_policy_reads_the_yaml_knobs() -> None:
    from trading_live_claude.analysis.venue_calendar import symbol_is_open
    risk = {"risk_model": "atr", "heat_aggregation": "sum"}
    on = monitor_policy(SimpleNamespace(respect_market_hours=True, skip_closed_venues=True, entry_triggers={"A": "both"},
                                        forced_exits=True, **risk))
    assert on == {"market_open_for": symbol_is_open, "entry_triggers": {"A": "both"}, "forced_exits": True, **risk}
    off = monitor_policy(SimpleNamespace(respect_market_hours=False, skip_closed_venues=True, entry_triggers={},
                                         forced_exits=False, **risk))
    assert off["market_open_for"] is None and off["forced_exits"] is False


# ---------------------------------------------------------------- entry channels

def test_level_is_ignored_by_default_for_a_strategy_that_only_emits_it(tmp_path: Path) -> None:
    m, pb, _ = _monitor(tmp_path, _Script(level=1))
    assert [e.kind for e in m.step()] == ["hold"]
    assert _qty(pb) == 0.0


def test_symbol_can_opt_in_to_level(tmp_path: Path) -> None:
    m, pb, _ = _monitor(tmp_path, _Script(level=1), entry_triggers={"BTC/USD": "both"})
    ev = m.step()
    assert ev[0].kind == "entry" and ev[0].detail["trigger"] == "level"
    assert _qty(pb) > 0


def test_level_only_symbol_ignores_the_event(tmp_path: Path) -> None:
    m, pb, _ = _monitor(tmp_path, _Script(entry=1), entry_triggers={"BTC/USD": "level"})
    assert [e.kind for e in m.step()] == ["hold"]
    assert _qty(pb) == 0.0


def test_opted_in_family_acts_on_level_and_a_symbol_can_opt_out(tmp_path: Path) -> None:
    assert BollingerMeanRevert.level_trigger_default is True

    class _Family(_Script):
        level_trigger_default = True

    m, _, _ = _monitor(tmp_path, _Family(level=1))
    assert m.step()[0].detail["trigger"] == "level"
    m2, _, _ = _monitor(tmp_path / "b", _Family(level=1), entry_triggers={"BTC/USD": "event"})
    assert [e.kind for e in m2.step()] == ["hold"]


def test_setting_level_on_a_strategy_that_cannot_emit_it_is_refused(tmp_path: Path) -> None:
    class _Plain(_Script):
        supports_level_trigger = False

    with pytest.raises(ValueError, match="emits no entry_level"):
        _monitor(tmp_path, _Plain(), entry_triggers={"BTC/USD": "level"})
    with pytest.raises(ValueError, match="unknown trigger"):
        _monitor(tmp_path / "b", _Script(), entry_triggers={"BTC/USD": "sometimes"})


# ---------------------------------------------------------------- re-entry guards

def test_rejected_level_entry_retries_on_the_next_bar_not_the_next_poll(tmp_path: Path) -> None:
    rec = _Recorder(result=None)
    m, _, _ = _monitor(tmp_path, _Script(level=1), router=rec, entry_triggers={"BTC/USD": "both"})
    m.step()
    m.step()
    m.step()
    assert len(rec.calls) == 1
    m.market.bar = 1  # type: ignore[attr-defined]
    m.step()
    assert len(rec.calls) == 2


def test_event_entry_is_not_limited_to_one_try_per_bar(tmp_path: Path) -> None:
    rec = _Recorder(result=None)
    m, _, _ = _monitor(tmp_path, _Script(entry=1), router=rec)
    m.step()
    m.step()
    assert len(rec.calls) == 2


def test_placed_order_not_yet_a_position_blocks_a_second_entry(tmp_path: Path) -> None:
    rec = _Recorder(result=object())                 # placed, but positions stay empty
    m, _, _ = _monitor(tmp_path, _Script(entry=1), router=rec)
    m.step()
    m.step()
    assert len(rec.calls) == 1


def test_inflight_window_ends(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _Recorder(result=object())
    m, _, _ = _monitor(tmp_path, _Script(entry=1), router=rec)
    m.step()
    m._inflight["BTC/USD"] -= 10_000.0
    m.step()
    assert len(rec.calls) == 2


def test_broker_error_leaves_the_order_in_flight(tmp_path: Path) -> None:
    rec = _Recorder(raises=BrokerError("timeout"))
    m, _, _ = _monitor(tmp_path, _Script(entry=1), router=rec)
    with pytest.raises(BrokerError):
        m.step()
    rec.raises = None
    m.step()                                         # state unknown: no second order goes out
    assert len(rec.calls) == 1


# ---------------------------------------------------------------- gross-leverage input

def test_monitor_passes_the_books_notional_and_the_gate_uses_it(tmp_path: Path) -> None:
    """The leverage cap used to see each intent alone, because nothing passed the open notional."""
    m, pb, _ = _monitor(tmp_path, _Script(entry=1), ["AAA", "BBB", "CCC"])
    m.router.max_position_notional_pct = 0.60       # three 60% entries would be 1.8x
    m.router.heat.cap_pct = 1.0                     # the cap under test here is leverage
    m.step()
    held = sum(p.openQuantity * 100.0 for p in pb.positions("PAPER-001"))
    assert held <= pb.equity("PAPER-001") * m.router.max_gross_leverage * 1.001
    assert _qty(pb, "AAA") > 0 and _qty(pb, "BBB") > 0     # the second trimmed to fit, not refused
    assert _qty(pb, "CCC") == 0.0                          # no room left at all


def test_open_positions_cap_counts_entries_made_earlier_in_the_same_poll(tmp_path: Path) -> None:
    m, pb, _ = _monitor(tmp_path, _Script(entry=1), ["AAA", "BBB", "CCC"])
    m.router.max_open_positions = 2
    m.router.max_position_notional_pct = 0.10
    m.router.heat.cap_pct = 1.0
    m.step()
    assert len([p for p in pb.positions("PAPER-001") if p.openQuantity]) == 2


def test_heat_counts_risk_taken_earlier_in_the_same_poll(tmp_path: Path) -> None:
    """Each entry asks for 4% risk; AAA is trimmed to 2.4%, so BBB's 4% would make 6.4% against 5%."""
    m, pb, _ = _monitor(tmp_path, _Script(entry=1), ["AAA", "BBB"])
    m.router.max_position_notional_pct = 0.60
    m.step()
    assert _qty(pb, "AAA") > 0 and _qty(pb, "BBB") == 0.0


def test_open_notional_reaches_the_router(tmp_path: Path) -> None:
    rec = _Recorder(result=None)
    m, pb, _ = _monitor(tmp_path, _Script(entry=1), ["AAA"], router=rec)
    pb.positions = lambda _a: [Position(symbol="ZZZ", symbolId=2, openQuantity=3.0, currentPrice=100.0)]  # type: ignore[method-assign]
    m.step()
    assert rec.calls[0]["current_open_notional"] == pytest.approx(300.0)


# ---------------------------------------------------------------- forced exits

def _opened(tmp: Path, **kw: object) -> tuple[LiveMonitor, PaperBroker, _Feed, _Script]:
    strat = _Script(entry=1, atr=2.0)
    m, pb, feed = _monitor(tmp, strat, **kw)
    m.router.max_position_notional_pct = 0.05       # small enough that the kill-switch stays quiet
    m.step()
    assert _qty(pb) > 0
    strat.entry = 0
    return m, pb, feed, strat


def test_forced_exit_closes_on_a_loss_past_the_atr_multiple(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger", stream="t", mode="paper", session_id="s")
    m, pb, feed, _ = _opened(tmp_path, forced_exits=True, ledger=ledger)
    feed.price = 90.0                                # > 3 x ATR 2.0 below entry
    ev = m.step()
    assert ev[0].kind == "exit" and "forced-exit" in ev[0].detail["forced_exit"]
    assert _qty(pb) == 0.0
    rows = [r for p in (tmp_path / "ledger").glob("*.jsonl") for r in read_stream(p)]
    forced = next(r for r in rows if r["event"] == "FORCED_EXIT")
    assert forced["payload"]["atr_at_entry"] == 2.0 and forced["intent_id"]


def test_forced_exit_still_closes_after_the_kill_switch_trips(tmp_path: Path) -> None:
    """A loss big enough to trip the kill-switch is exactly when the way out has to work."""
    m, pb, feed, _ = _opened(tmp_path, forced_exits=True)
    m.router.kill_switch.trip(reason="test")
    feed.price = 90.0
    ev = m.step()
    assert ev[0].kind == "exit" and "forced_exit" in ev[0].detail
    assert _qty(pb) == 0.0


def test_strategy_exit_closes_under_a_halt_but_an_entry_does_not_open(tmp_path: Path) -> None:
    m, pb, _, strat = _opened(tmp_path)
    m.router.kill_switch.trip(reason="test")
    strat.exit_ = 1
    m.step()
    assert _qty(pb) == 0.0
    strat.exit_, strat.entry = 0, 1
    m.step()
    assert _qty(pb) == 0.0


def test_the_ledger_explains_why_an_order_is_the_size_it_is(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger", stream="t", mode="paper", session_id="s")
    m, _, _ = _monitor(tmp_path, _Script(entry=1), ledger=ledger, weight_bias_for=lambda _s: 2.0)
    m.step()
    rows = [r for p in (tmp_path / "ledger").glob("*.jsonl") for r in read_stream(p)]
    sizing = next(r for r in rows if r["event"] == "STRATEGY_SIGNAL")["payload"]["sizing"]
    assert sizing["allocator_bias"] == 2.0 and sizing["interpret_bias"] == 1.0 and sizing["signal_strength"] == 1.0
    assert sizing["persistence_halt"] is False and "mitigation_scalar" in sizing


def test_a_suppressed_entry_says_what_suppressed_it(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger", stream="t", mode="paper", session_id="s")
    m, _, _ = _monitor(tmp_path, _Script(entry=1), ledger=ledger, persistence_for=lambda _s: (True, "energy elevated"))
    m.step()
    rows = [r for p in (tmp_path / "ledger").glob("*.jsonl") for r in read_stream(p)]
    row = next(r for r in rows if r["event"] == "SIGNAL_SUPPRESSED")["payload"]
    assert row["reason"] == "persistence halt" and row["sizing"]["persistence_reason"] == "energy elevated"


def test_forced_exit_is_off_unless_asked_for(tmp_path: Path) -> None:
    m, pb, feed, _ = _opened(tmp_path)
    feed.price = 50.0
    assert [e.kind for e in m.step()] == ["hold"]
    assert _qty(pb) > 0


def test_forced_exit_holds_inside_the_multiple(tmp_path: Path) -> None:
    m, pb, feed, _ = _opened(tmp_path, forced_exits=True)
    feed.price = 96.0
    assert [e.kind for e in m.step()] == ["hold"]
    assert _qty(pb) > 0


def test_inherited_position_is_protected_after_a_restart(tmp_path: Path) -> None:
    m, pb, feed, _ = _opened(tmp_path, forced_exits=True)
    m._atr_at_entry.clear()                          # a new process knows nothing about the entry
    feed.price = 90.0
    assert m.step()[0].kind == "exit"
    assert _qty(pb) == 0.0


def test_nan_atr_falls_back_instead_of_disabling_the_exit(tmp_path: Path) -> None:
    strat = _Script(entry=1, atr=float("nan"))
    m, pb, feed = _monitor(tmp_path, strat, forced_exits=True)
    m.router.max_position_notional_pct = 0.05
    m.step()
    assert _qty(pb) > 0 and m._atr_at_entry["BTC/USD"] == pytest.approx(2.0)   # 2% of price
    strat.entry = 0
    feed.price = 90.0
    assert m.step()[0].kind == "exit"


# ---------------------------------------------------------------- settings

def test_settings_validate_the_entry_trigger_values() -> None:
    from pydantic import ValidationError

    from trading_live_claude.config.settings import Settings
    assert Settings(entry_triggers={"A": "both"}).entry_triggers == {"A": "both"}
    with pytest.raises(ValidationError):
        Settings(entry_triggers={"A": "sometimes"})
    defaults = Settings()
    assert defaults.forced_exits is False and defaults.respect_market_hours is True
