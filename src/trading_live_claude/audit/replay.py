"""Re-run recorded gate decisions from the ledger and compare (AUDIT_SCOPE.md acceptance 2).

A ``RISK_CHECK`` / ``RISK_REJECTED`` row records the intent as it reached the gate, every input
``Router._gate`` read (equity, existing risk, open positions, open notional), every observation it
took from outside the intent (kill-switch, basket, daily budget, spectral VaR), and the thresholds.
Replay builds a Router with those thresholds, stubs the outside observations with the recorded
values, runs ``_gate`` and compares the outcome: accepted, reasons, and the size it left.

A row is replayed only when the gate code is the one that wrote it: the recorded
``risk_check_version`` must equal the hash of today's ``Router._gate`` source over the recorded
thresholds. Otherwise the row is reported as ``code changed``, which is itself a finding (the
gate's meaning moved), rather than replayed against code it never ran on.
"""
from __future__ import annotations

import inspect
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from ..brokers.base import BrokerError
from ..brokers.models import OrderAction
from ..execution.daily_budget import DailyBudgetState
from ..execution.router import OrderIntent, Router
from ..risk.heat import PortfolioHeat
from ..risk.kill_switch import KillState
from ..risk.spectral_var import (
    SpectralCheck,
    short_reason,
    summary_text,
    unavailable_reason,
)
from .versioning import content_hash


class _NullJournal:
    def __getattr__(self, _name: str) -> Any:
        return lambda *_a, **_k: None


class _NullBroker:
    name = "replay"

    def __init__(self, symbol: str = "", held: float | None = None) -> None:
        self._symbol, self._held = symbol, held

    def positions(self, _account: str) -> list[Any]:
        """The one read the gate makes of a broker, answered from the record."""
        if self._held is None:
            raise BrokerError("the holding could not be read when this row was written")
        return [SimpleNamespace(symbol=self._symbol, openQuantity=self._held)]

    def __getattr__(self, name: str) -> Any:
        raise RuntimeError(f"replay never reaches a broker (called {name})")


class _KS:
    def __init__(self, halted: bool) -> None:
        self.halted = halted
        self.max_drawdown_pct = None
        self.daily_loss_limit_pct = None

    def state(self) -> KillState:
        return KillState(halted=self.halted, reason="replayed", activated_at=None)


class _Basket:
    def __init__(self, venue: str, reason: str | None) -> None:
        self.venue, self.reason = venue, reason

    def check_entry(self, _symbol: str) -> str | None:
        return self.reason


class _Budget:
    def __init__(self, observed: Mapping[str, Any], thr: Mapping[str, Any]) -> None:
        self.max_trades = thr.get("daily_max_trades")
        self.max_notional = thr.get("daily_max_notional_usd")
        self._state = DailyBudgetState(trades_today=int(observed["trades_today"]),
                                       notional_today_usd=float(observed["notional_today_usd"]),
                                       max_trades=int(self.max_trades or 0),
                                       max_notional_usd=float(self.max_notional or 0.0))

    def snapshot(self) -> DailyBudgetState:
        return self._state


class _Spectral:
    def __init__(self, observed: Mapping[str, Any], thr: Mapping[str, Any], symbol: str) -> None:
        self.observed, self.thr, self.symbol = dict(observed), dict(thr), symbol

    def thresholds(self) -> dict[str, Any]:
        return self.thr

    def check(self, symbol: str, *, shares: int, entry: float, equity: float) -> SpectralCheck:
        o, t = self.observed, self.thr
        if "error" in o:
            return SpectralCheck(unavailable_reason(symbol, str(o["error"])), None, "", o)
        if "max_shares" not in o:
            return SpectralCheck(short_reason(int(o.get("n_returns", 0)), symbol,
                                              int(t["min_returns"])), None, "", o)
        summary = summary_text(float(t["level"]), int(t["horizon_days"]), float(o["var_usd"]),
                               float(o["loss_fraction"]), float(o["cap_usd"]),
                               float(t["cap_pct"]))
        return SpectralCheck(None, int(o["max_shares"]), summary, o)


def gate_version_now(thresholds: Mapping[str, Any]) -> str:
    return content_hash({"gate_source": inspect.getsource(Router._gate),
                         "thresholds": dict(thresholds)})


def replay_row(row: Mapping[str, Any]) -> dict[str, Any]:
    p = row.get("payload") or {}
    base = {"stream": row.get("stream"), "seq": row.get("seq"), "intent_id": row.get("intent_id"),
            "ts": row.get("ts")}
    thr, inputs, observed = p.get("thresholds"), p.get("inputs"), p.get("observed")
    if thr is None or inputs is None or observed is None:
        return base | {"status": "not replayable",
                       "why": "written before gate inputs were recorded"}
    if gate_version_now(thr) != row.get("risk_check_version"):
        return base | {"status": "code changed",
                       "why": "today's Router._gate is not the code that wrote this row"}
    routes = thr.get("brokers") or ["replay"]
    held = observed.get("held_quantity")
    router = Router(mode="dry-run",
                    brokers={r: _NullBroker(str(p["symbol"]), held) for r in routes},  # type: ignore[misc]
                    default_broker=thr.get("default_broker") or routes[0],
                    journal=_NullJournal(),  # type: ignore[arg-type]
                    kill_switch=_KS(bool(observed.get("kill_switch_halted"))),  # type: ignore[arg-type]
                    heat=PortfolioHeat(cap_pct=float(thr["heat_cap_pct"])),
                    max_open_positions=int(thr["max_open_positions"]),
                    min_ticket_usd=float(thr["min_ticket_usd"]),
                    max_gross_leverage=float(thr["max_gross_leverage"]),
                    max_position_notional_pct=float(thr["max_position_notional_pct"]),
                    force_exit_atr_mult=float(thr["force_exit_atr_mult"]),
                    on_size_cap_breach=thr["on_size_cap_breach"])
    if thr.get("basket_gate"):
        router.basket_gate = _Basket(thr["basket_gate"], observed.get("basket_reason"))  # type: ignore[assignment]
    if "daily_budget" in observed:
        router.daily_budget = _Budget(observed["daily_budget"], thr)  # type: ignore[assignment]
    if thr.get("spectral_var") and "spectral_var" in observed:
        router.spectral_gate = _Spectral(observed["spectral_var"], thr["spectral_var"],  # type: ignore[assignment]
                                         str(p["symbol"]))
    intent = OrderIntent(symbol=str(p["symbol"]), action=OrderAction(p["action"]),
                         shares=int(p["requested_shares"]), entry=float(p["entry"]),
                         stop=float(p["stop"]),
                         target=float(p["target"]) if p.get("target") is not None else None,
                         strategy=str(row.get("strategy_id") or ""),
                         risk_dollars=float(p["risk_dollars"]), account_number="replay",
                         timestamp=datetime.now(UTC), broker=str(p.get("broker") or ""))
    decision = router._gate(intent, equity=float(inputs["equity"]),
                            existing_risk=float(inputs["existing_risk"]),
                            open_positions=int(inputs["open_positions"]),
                            current_open_notional=float(inputs["current_open_notional"]))
    diffs = {}
    if decision.accepted != bool(p["accepted"]):
        diffs["accepted"] = {"recorded": p["accepted"], "replayed": decision.accepted}
    if list(decision.rejected_reasons) != list(p.get("rejected_reasons") or []):
        diffs["rejected_reasons"] = {"recorded": p.get("rejected_reasons"),
                                     "replayed": decision.rejected_reasons}
    if intent.shares != p.get("shares"):
        diffs["shares"] = {"recorded": p.get("shares"), "replayed": intent.shares}
    return base | {"status": "match" if not diffs else "differs", "diffs": diffs}


def replay(rows: Iterable[Mapping[str, Any]], *, session_id: str | None = None) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        if r.get("event") not in ("RISK_CHECK", "RISK_REJECTED"):
            continue
        if session_id and r.get("session_id") != session_id:
            continue
        out.append(replay_row(r))
    return out
