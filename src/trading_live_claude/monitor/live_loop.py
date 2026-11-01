"""Live monitor loop (article skill #5).

Polls Questrade every ``interval`` seconds. For each symbol:
  * fetch recent candles, append latest quote as a synthetic "current bar"
  * run strategy.generate_signals on the rolling window
  * if last bar emits entry==1: size, gate, and dispatch via Router
  * if last bar emits exit==1 and we hold a position: emit close intent

Designed to be safe to restart: state is reloaded from positions/fills journal.

:func:`monitor_policy` turns the ``config/trading.yaml`` knobs into constructor arguments, so every
runner applies the same market-hours guard, entry-trigger choice and forced-exit setting.
"""
from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, cast

import pandas as pd

from ..brokers.base import Broker, BrokerError
from ..brokers.models import OrderAction
from ..data.market import MarketData
from ..execution.router import OrderIntent, Router
from ..intel.interpret import THEME_EXEMPLARS, Thesis
from ..intel.overlay import OverlayDecision
from ..logging_setup import get_logger
from ..models.risk_mitigation import combine
from ..models.strategy_risk import scalar_from_signals
from ..risk.equity_watch import EquityWatch
from ..risk.hedge import HedgePolicy, hedge_shares, hedge_weight, rebalance_delta
from ..risk.risk_model import HeatAggregation, RiskModel, per_trade_risk, portfolio_risk
from ..risk.sizing import PositionSizer
from ..strategies.base import Strategy, StrategyContext

# Interpret-bias floor. Multi-thesis stacking cannot pull conviction below this multiplier — the
# interpret layer is advisory, not a halt, so trimming to zero would violate that contract. The
# overlay layer (with its own floor) and the router's kill-switch handle the actual halt path.
_INTERPRET_BIAS_FLOOR = 0.25
# Per-confidence trim factors. tentative → advisory only (no trim). Multiplicative stacking.
_INTERPRET_BIAS_BY_CONFIDENCE = {"high": 0.5, "moderate": 0.75, "tentative": 1.0}
# How long a placed order blocks a repeat entry while it is not yet visible as a position. A multiple
# of the poll interval, with a floor, because a broker's positions can lag a market order by a while.
_INFLIGHT_POLLS = 3
_INFLIGHT_MIN_SECONDS = 300.0

EntryTrigger = Literal["event", "level", "both"]

log = get_logger(__name__)


def monitor_policy(settings: Any) -> dict[str, Any]:
    """``LiveMonitor`` keyword arguments for the knobs in ``config/trading.yaml``."""
    from ..analysis.venue_calendar import symbol_is_open
    return {"session_open_for": symbol_is_open if settings.respect_market_hours else None,
            "entry_triggers": settings.entry_triggers,
            "forced_exits": settings.forced_exits}


@dataclass
class MonitorEvent:
    timestamp: datetime
    symbol: str
    kind: str  # 'entry' | 'exit' | 'hold'
    price: float
    detail: dict[str, object]
    # Transition metadata, so persistence mode does not throw away edge information. In edge mode
    # every emitted event is by definition a transition; in level mode the same signal is re-emitted
    # each poll, and these say whether it is new and how long it has been standing.
    is_transition: bool = True
    poll_count: int = 1        # consecutive polls this symbol has been in ``kind`` (1 = just entered)


class LiveMonitor:
    def __init__(
        self,
        *,
        broker: Broker,
        market: MarketData,
        strategy: Strategy,
        sizer: PositionSizer,
        router: Router,
        account_number: str,
        symbols: list[str],
        interval_seconds: int = 60,
        on_event: Callable[[MonitorEvent], None] | None = None,
        account_currency: str = "CAD",
        emit_on_change_only: bool = True,
        strategy_map: dict[str, Strategy] | None = None,
        risk_model: str = "cvar",
        heat_aggregation: str = "corr",
        hedge_symbol: str | None = None,
        hedge_policy: HedgePolicy | None = None,
        overlay_for: Callable[[str], OverlayDecision | None] | None = None,
        interpret_for: Callable[[], list[Thesis]] | None = None,
        weight_bias_for: Callable[[str], float] | None = None,
        persistence_for: Callable[[str], tuple[bool, str]] | None = None,
        strategy_risk: bool = False,
        session_open_for: Callable[[str], bool] | None = None,
        entry_triggers: Mapping[str, EntryTrigger] | None = None,
        forced_exits: bool = False,
    ) -> None:
        self.broker = broker
        self.market = market
        self.strategy = strategy
        # Per-symbol strategy overrides. A symbol not in the map uses ``strategy``
        # as the fallback, so single-strategy monitoring stays backward compatible.
        self.strategy_map = strategy_map or {}
        self.sizer = sizer
        self.router = router
        # Strategy versions for the audit ledger come from the instances running here, so a tuned
        # parameter set versions differently from the default. Installed only when the router has
        # no resolver yet: a caller's explicit one wins.
        self._install_strategy_version_resolver(router)
        # The router's ledger, so one stream carries the chain from signal to fill.
        self.ledger = getattr(getattr(router, "inner", router), "ledger", None)
        self.account_number = account_number
        self.symbols = symbols
        # How the heat gate estimates risk: per-trade model (atr/var/cvar) and aggregation
        # (sum/corr). Default is tail-aware (CVaR) + covariance-aware (corr); set to
        # atr/sum for the original ATR-stop, correlation-blind behaviour.
        self.risk_model: RiskModel = cast(RiskModel, risk_model)
        self.heat_aggregation: HeatAggregation = cast(HeatAggregation, heat_aggregation)
        self.interval_seconds = max(int(interval_seconds), 5)
        self.on_event = on_event or (lambda _: None)
        self.account_currency = account_currency
        # Edge-triggering: when True, on_event fires only when a symbol's signal
        # state (entry/exit/hold) changes from the previous poll, so a persistent
        # signal alerts once instead of every interval. Order routing below is
        # unaffected — only the notification callback is deduplicated.
        self.emit_on_change_only = emit_on_change_only
        self._last_kind: dict[str, str] = {}
        # consecutive polls each symbol has held its current kind (drives poll_count)
        self._kind_runs: dict[str, int] = {}
        # Dynamic dollar-hedge overlay (opt-in): scale a UUP sleeve up as the book draws
        # down. ``_equity_peak`` is tracked in-memory (resets on restart → hedge starts at
        # 0 and re-ramps as fresh drawdown develops).
        self.hedge_symbol = hedge_symbol
        self.hedge_policy = hedge_policy or (HedgePolicy(symbol=hedge_symbol) if hedge_symbol else None)
        self._equity_peak = 0.0
        # Live WorldMonitor risk overlay (opt-in). Given a symbol it returns the current per-asset-
        # class decision, or None. De-risk only: its scalar trims entry conviction (smaller size) and
        # its halt flag blocks NEW entry routing for that class. Exits are never blocked — the overlay
        # can only stand the book down, never trap it in a position.
        self.overlay_for = overlay_for
        # Interpret-thesis bias: called at each entry evaluation to fetch the CURRENT list of
        # fired theses (from intel/interpret.py). If the entry symbol appears in any moderate-
        # or high-confidence thesis's implicated exemplars, conviction is trimmed by the
        # per-confidence factor. NEVER blocks and NEVER boosts — the interpret layer is advisory,
        # so it can only trim, and trimming stacks multiplicatively with a floor of 0.25.
        # Exits are untouched.
        self.interpret_for = interpret_for
        # Cache the last-computed bias per symbol so alerts can surface which theses applied
        # without recomputing at the alert boundary.
        self._interpret_last_applied: dict[str, list[str]] = {}
        # Per-symbol portfolio-allocation weight bias — multiplies conviction at sizing time.
        # Unlike interpret_for (which trims only, floor 0.25), this CAN boost above 1.0 because
        # it represents a diversification-aware rebalance rather than a risk signal: a low-
        # correlation name gets its share of the risk budget lifted, a redundant name gets it
        # cut, and equal-weight is the neutral case (multiplier 1.0). Bounded [0.1, 3.0] so a
        # runaway allocator can't leverage past a sane cap.
        self.weight_bias_for = weight_bias_for
        self._weight_bias_last: dict[str, float] = {}
        # Cross-path tier 3: graph-persistence entry gate. Same shape as ``overlay_for`` (returns
        # a halt decision + reason), but grounded in the intel graph's ``edge_persistence`` query
        # rather than the current-poll overlay scalar. Enforces "the same 6x reading across five
        # polls is a regime, not noise" — an entry in a symbol whose overlay class is exposed to a
        # persistently-elevated domain is blocked, and the reason lands on the rejection alert.
        # Fail-open: if not wired, entries proceed unaffected.
        self.persistence_for = persistence_for
        self._persistence_last_reason: dict[str, str] = {}
        # Strategy-risk gate: the trailing-volatility scalar computed from the strategy's own return
        # stream. Chosen over the gradient-boosted classifier because an honest walk-forward showed
        # the simple rule is the better forward-drawdown predictor (6 of 8 real strategies).
        self.strategy_risk = strategy_risk
        # Market-hours guard (analysis/venue_calendar.py). Given a symbol, whether its venue is open;
        # a closed symbol is skipped for the poll, entries and exits alike. None = no guard.
        self.session_open_for = session_open_for
        self._closed_logged: set[str] = set()
        # Which entry channels a symbol acts on; a symbol not named takes its strategy family's
        # default (see Strategy.level_trigger_default). A symbol set to act on a level its strategy
        # never emits could never enter, so that is refused here rather than failing silently.
        self.entry_triggers = dict(entry_triggers or {})
        for sym, mode in self.entry_triggers.items():
            if mode not in ("event", "level", "both"):
                raise ValueError(f"entry_triggers[{sym!r}]: unknown trigger {mode!r}")
            strat = self._strategy_for(sym)
            if mode != "event" and not getattr(strat, "supports_level_trigger", False):
                raise ValueError(f"entry_triggers[{sym!r}] = {mode!r}, but strategy "
                                 f"{strat.name!r} emits no entry_level column")
        # The bar each symbol last routed a level-only entry on. A level holds for the whole bar, so
        # without this a rejected or declined intent would re-fire on every poll.
        self._level_tried: dict[str, object] = {}
        # symbol -> when an order went out that is not yet visible as a position.
        self._inflight: dict[str, float] = {}
        # Router.check_forced_exits on each poll, off unless asked for. It sends SELL orders with no
        # strategy signal behind them, so turning it on is the operator's decision.
        self.forced_exits = forced_exits
        self._atr_at_entry: dict[str, float] = {}
        self._entry_prices: dict[str, float] = {}
        # A live book's equity journal and drawdown auto-halt: what PaperBroker._journal_equity does
        # for a paper one on every mark. Not in dry-run, where a read-only look at a real account must
        # not be able to halt whatever else is trading on it.
        inner = getattr(router, "inner", router)
        self._book: tuple[float, float, float] = (0.0, 0.0, 0.0)
        self._equity_watch = (
            EquityWatch(inner.kill_switch, account=account_number, mode=inner.mode,
                        currency=account_currency,
                        session_id=getattr(getattr(inner, "ledger", None), "session_id", None))
            if getattr(inner, "mode", None) in ("live", "autonomous") else None)

    def _strategy_for(self, symbol: str) -> Strategy:
        """The per-symbol strategy, falling back to the default ``strategy``."""
        return self.strategy_map.get(symbol, self.strategy)

    def _venue_closed(self, symbol: str) -> bool:
        """True while the guard says the symbol's venue is closed. A guard error reads as open."""
        if self.session_open_for is None:
            return False
        try:
            closed = not self.session_open_for(symbol)
        except Exception as e:                 # never lose a live session to a calendar bug
            log.warning("monitor.session_guard.failed", symbol=symbol, error=str(e))
            return False
        if closed and symbol not in self._closed_logged:
            log.info("monitor.venue_closed", symbol=symbol)
        (self._closed_logged.add if closed else self._closed_logged.discard)(symbol)
        return closed

    def _entry_channels(self, symbol: str, strat: Strategy) -> EntryTrigger:
        """Which entry channels a symbol acts on: its own setting, else its strategy family's."""
        mode = self.entry_triggers.get(symbol)
        if mode is not None:
            return mode
        return "both" if getattr(strat, "level_trigger_default", False) else "event"

    def _order_inflight(self, symbol: str) -> bool:
        """Whether an earlier order for ``symbol`` may still be working at the broker."""
        placed_at = self._inflight.get(symbol)
        if placed_at is None:
            return False
        if time.monotonic() - placed_at > max(self.interval_seconds * _INFLIGHT_POLLS, _INFLIGHT_MIN_SECONDS):
            del self._inflight[symbol]
            return False
        return True

    def _forced_exit_reason(self, symbol: str, qty: float, price: float, atr_value: float) -> str | None:
        """The Router's verdict on one held position. ATR at entry is the value when this monitor
        opened it; after a restart the first ATR it sees stands in, so an inherited position is still
        protected."""
        atr_at_entry = self._atr_at_entry.setdefault(symbol, atr_value)
        inner = getattr(self.router, "inner", self.router)      # unwrap an ApprovalRouter
        exits = inner.check_forced_exits(
            [{"symbol": symbol, "entry_price": self._entry_prices.get(symbol, 0.0), "quantity": qty,
              "side": "long"}],
            {symbol: price}, {symbol: atr_at_entry})
        return str(exits[0]["reason"]) if exits else None

    def _interpret_bias(self, symbol: str) -> tuple[float, list[str]]:
        """Multiplicative conviction bias from the current interpret() theses.

        Returns ``(multiplier, thesis_names_applied)``. When ``interpret_for`` is not wired or
        no thesis implicates this symbol, returns ``(1.0, [])`` — no effect. When theses do
        implicate it, the per-confidence factor is applied multiplicatively per thesis, with
        the product floored at ``_INTERPRET_BIAS_FLOOR``.

        This never boosts and never blocks — it can only trim conviction. The interpret layer
        is advisory-only per its docstring; enforcing that contract at the gate is the point of
        the floor. Exits ignore this method entirely (see step()).
        """
        if self.interpret_for is None:
            return 1.0, []
        try:
            theses = self.interpret_for() or []
        except Exception as e:                 # pragma: no cover — never break the poll on interpret I/O
            log.warning("monitor.interpret_bias.failed", symbol=symbol, error=str(e))
            return 1.0, []
        if not theses:
            return 1.0, []
        bias = 1.0
        applied: list[str] = []
        for t in theses:
            if t.name == "No notable configuration":
                continue                        # quiet-tape null is not evidence
            # Union of exemplar tickers across this thesis's themes.
            exemplars: set[str] = set()
            for theme in t.themes:
                exemplars.update(THEME_EXEMPLARS.get(theme, ()))
            if symbol in exemplars:
                factor = _INTERPRET_BIAS_BY_CONFIDENCE.get(t.confidence, 1.0)
                if factor < 1.0:
                    bias *= factor
                    applied.append(t.name)
        return max(_INTERPRET_BIAS_FLOOR, bias), applied

    def _open_positions(self) -> dict[str, float]:
        positions = self.broker.positions(self.account_number)
        self._entry_prices = {p.symbol: float(getattr(p, "averageEntryPrice", 0.0) or 0.0)
                              for p in positions}
        self._book = (sum(float(getattr(p, "currentMarketValue", 0.0) or 0.0) for p in positions),
                      sum(float(getattr(p, "closedPnl", 0.0) or 0.0) for p in positions),
                      sum(float(getattr(p, "openPnl", 0.0) or 0.0) for p in positions))
        return {p.symbol: p.openQuantity for p in positions if p.openQuantity != 0}

    def step(self) -> list[MonitorEvent]:
        events: list[MonitorEvent] = []
        # If the broker supports live mark-to-market (PaperBroker does; QuestradeBroker gets fresh
        # numbers from the account API directly and doesn't need it), refresh position currentPrice
        # from the feed and journal a fresh equity row BEFORE reading equity. That way the equity
        # + drawdown numbers we compute this poll reflect the current mid-market, not the stale
        # fill-time prices. Duck-typed check so non-paper brokers stay unaffected.
        if hasattr(self.broker, "mark_to_market"):
            try:
                self.broker.mark_to_market()
            except Exception as e:                     # pragma: no cover — never break the poll
                log.warning("monitor.mtm.failed", error=str(e))
        equity = self.broker.equity(self.account_number, currency=self.account_currency)
        open_positions = self._open_positions()
        # Before any quote, so a quote failure on a held name cannot skip the check, and so the gate
        # sees a trip on this same poll.
        if self._equity_watch is not None:
            try:
                self._equity_watch.observe(equity, positions_value=self._book[0],
                                           realized_pnl=self._book[1], unrealized_pnl=self._book[2])
            except Exception as e:                     # pragma: no cover — never break the poll
                log.error("monitor.equity_watch.failed", error=str(e))
        # Per-position risk (ATR-stop proxy by default, or a VaR/CVaR tail estimate of the
        # name's returns), then aggregated for the heat gate — a naive sum by default or a
        # covariance-aware combine that credits diversification.
        pos_risk: dict[str, float] = {}
        pos_rets: dict[str, pd.Series | None] = {}
        # What the book holds at market: the gross-leverage gate adds each new intent to this.
        open_notional = 0.0
        for sym, qty in open_positions.items():
            q = self.broker.quote(sym)
            px = q.mid or q.lastTradePrice or 0.0
            open_notional += abs(qty) * px
            rets: pd.Series | None = None
            if self.risk_model != "atr" or self.heat_aggregation == "corr":
                try:
                    rets = self.market.recent(sym, bars=90, interval="1d")["close"].pct_change()
                except Exception:
                    rets = None
            pos_rets[sym] = rets
            pos_risk[sym] = per_trade_risk(qty, px, stop_distance=px * 0.02, returns=rets, model=self.risk_model)
        existing_risk = portfolio_risk(pos_risk, pos_rets, method=self.heat_aggregation)
        # The book as this poll has left it, so several entries in one poll cannot each see room
        # that an earlier one has already taken.
        open_count = len(open_positions)

        for symbol in self.symbols:
            if self._venue_closed(symbol):
                continue
            strat = self._strategy_for(symbol)
            bars_needed = strat.required_history_bars()
            df = self.market.recent(symbol, bars=bars_needed + 5, interval="1d")
            if len(df) < bars_needed:
                log.warning("monitor.insufficient_history", symbol=symbol, have=len(df), need=bars_needed)
                continue
            ctx = StrategyContext(symbol=symbol, timeframe="1d")
            signals = strat.generate_signals(df, ctx)
            last = signals.iloc[-1]
            quote = self.broker.quote(symbol)
            price = quote.mid or quote.lastTradePrice or float(last["close"])

            # Both entry-trigger channels (2026-09-09). ``entry`` is event-triggered —
            # fires on the fresh cross. ``entry_level`` is state-triggered — fires
            # while the current bar's state satisfies the entry condition. Strategy
            # must set ``supports_level_trigger=True`` for the level column to be
            # consulted; if not, we ignore ``entry_level`` even if present (backward
            # compatible with legacy strategies that don't emit it). Of what the
            # strategy emits, a symbol acts on the channels _entry_channels names.
            channels = self._entry_channels(symbol, strat)
            entry_event = channels != "level" and int(last.get("entry", 0)) == 1
            entry_level = (
                channels != "event"
                and bool(getattr(strat, "supports_level_trigger", False))
                and int(last.get("entry_level", 0)) == 1
            )
            bar = self._bar_stamp(df)
            if entry_level and not entry_event and self._level_tried.get(symbol) == bar:
                entry_level = False  # already routed on this bar; the next try is the next bar
            entry = entry_event or entry_level
            entry_trigger: str | None = None
            if entry_event and entry_level:
                entry_trigger = "event+level"
            elif entry_event:
                entry_trigger = "event"
            elif entry_level:
                entry_trigger = "level"
            exit_ = int(last.get("exit", 0)) == 1
            atr_raw = float(last.get("atr", price * 0.02))
            atr_value = atr_raw if atr_raw > 0 else price * 0.02  # a NaN ATR falls back too

            # ``holds`` guards re-entry from level-triggered re-fires: while a position
            # is open, subsequent level-eligible polls fall through to the HOLD branch.
            # An order still on its way counts the same, until it shows or its window ends.
            holds = open_positions.get(symbol, 0.0) > 0
            if holds:
                self._inflight.pop(symbol, None)
            else:
                self._atr_at_entry.pop(symbol, None)
            forced_reason = (self._forced_exit_reason(symbol, open_positions[symbol], price, atr_value)
                             if self.forced_exits and holds else None)

            if entry and not holds and not self._order_inflight(symbol):
                # Volatility targeting (annualized daily vol) + conviction from the strategy's
                # graded signal_strength; the ATR still defines the protective stop.
                rets = df["close"].pct_change().dropna()
                annual_vol = float(rets.tail(63).std(ddof=0) * (252.0 ** 0.5)) if len(rets) >= 20 else None
                ss = last.get("signal_strength", 1.0)
                conviction = 1.0 if (ss is None or pd.isna(ss)) else float(ss)
                # Live intelligence overlay: trim conviction by the asset-class risk scalar, and note
                # whether new entries in this class are halted (routing is skipped, alert still fires).
                decision = self.overlay_for(symbol) if self.overlay_for else None
                # Strategy risk (backtestable, from the strategy's own returns) composed with the
                # live OSINT class scalar. Both only de-risk; either can halt new entries.
                srisk = 1.0
                if self.strategy_risk:
                    try:
                        srisk = scalar_from_signals(
                            signals, atr_stop_mult=strat.stop_atr_mult,
                            trail_atr_mult=strat.trail_atr_mult, time_stop_bars=strat.time_stop_bars)
                    except Exception as e:  # pragma: no cover - never break the poll on a risk calc
                        log.warning("monitor.strategy_risk.failed", symbol=symbol, error=str(e))
                mitigation = combine(srisk, decision)
                overlay_halt = mitigation.halt
                conviction *= mitigation.scalar
                # Interpret-thesis bias — trim conviction further when a moderate/high thesis
                # implicates this symbol. Applied AFTER the overlay/strategy composition so the
                # floor and the reasons compose cleanly; recorded on the entry event for audit.
                interp_bias, interp_applied = self._interpret_bias(symbol)
                if interp_bias < 1.0:
                    conviction *= interp_bias
                    self._interpret_last_applied[symbol] = interp_applied
                # Portfolio-allocator weight bias — applied last so it multiplies whatever the
                # gates have already left. Bounded so a runaway allocator can't lever past 3x.
                weight_bias = 1.0
                if self.weight_bias_for is not None:
                    try:
                        weight_bias = float(self.weight_bias_for(symbol))
                    except Exception:                         # never break the poll on allocator I/O
                        weight_bias = 1.0
                    weight_bias = max(0.1, min(3.0, weight_bias))
                    if weight_bias != 1.0:
                        conviction *= weight_bias
                        self._weight_bias_last[symbol] = weight_bias
                sized = self.sizer.size(
                    equity=equity, entry=price, atr_value=atr_value, side="long",
                    annual_vol=annual_vol, conviction=conviction,
                )
                # Cross-path tier 3: persistence-driven halt. Queries the intel graph via the
                # injected ``persistence_for`` callable; blocks the router submit (but not the
                # alert) when the symbol's overlay class has been exposed to a persistently-
                # elevated domain. Fail-open: if the callable is not wired or its refresh raises,
                # halt stays False and the entry proceeds as before.
                persistence_halt = False
                persistence_reason = ""
                if self.persistence_for is not None:
                    try:
                        persistence_halt, persistence_reason = self.persistence_for(symbol)
                    except Exception as e:                       # never break the poll on graph I/O
                        log.warning("monitor.persistence.failed", symbol=symbol, error=str(e))
                        persistence_halt, persistence_reason = False, ""
                    if persistence_halt:
                        self._persistence_last_reason[symbol] = persistence_reason
                routable = sized.shares > 0 and not overlay_halt and not persistence_halt
                if not routable:
                    self._ledger_signal(symbol, strat, price, df, sized=sized, intent_id=None,
                                        reason=("overlay halt" if overlay_halt else
                                                "persistence halt" if persistence_halt else
                                                "sized to zero shares"), conviction=conviction,
                                        trigger=entry_trigger, atr=atr_value)
                if routable:
                    entry_rets = df["close"].pct_change() if self.risk_model != "atr" else None
                    risk_dollars = per_trade_risk(
                        sized.shares, price, stop_distance=abs(price - sized.stop),
                        returns=entry_rets, model=self.risk_model,
                    )
                    intent = OrderIntent(
                        symbol=symbol,
                        action=OrderAction.BUY,
                        shares=sized.shares,
                        entry=sized.entry,
                        stop=sized.stop,
                        target=sized.target,
                        strategy=strat.name,
                        risk_dollars=risk_dollars,
                        account_number=self.account_number,
                    )
                    self._ledger_signal(symbol, strat, price, df, sized=sized,
                                        intent_id=intent.intent_id, reason="", conviction=conviction,
                                        trigger=entry_trigger, atr=atr_value)
                    if not entry_event:
                        self._level_tried[symbol] = bar
                    try:
                        placed = self.router.submit(
                            intent,
                            equity=equity,
                            existing_risk=existing_risk,
                            open_positions=open_count,
                            current_open_notional=open_notional,
                        )
                    except BrokerError:
                        # Order state unknown: it may be at the broker, so do not send another.
                        self._inflight[symbol] = time.monotonic()
                        raise
                    if placed is not None:
                        self._inflight[symbol] = time.monotonic()
                        self._atr_at_entry[symbol] = atr_value
                        open_notional += intent.shares * intent.entry  # as the gate left it
                        # risk_dollars was computed on the size asked for; the gate may have trimmed it.
                        existing_risk += intent.risk_dollars * intent.shares / max(sized.shares, 1)
                        open_count += 1
                # Alert on the entry SIGNAL regardless of sizeability — the monitor is an
                # alerter, so a real signal must surface even when the account is too small
                # to size a position (0 shares); only the order routing is gated by shares.
                entry_detail: dict[str, object] = {"sized": sized.shares}
                if entry_trigger is not None:
                    entry_detail["trigger"] = entry_trigger
                if decision is not None or self.strategy_risk:
                    entry_detail["mitigation"] = {
                        "scalar": mitigation.scalar, "strategy": mitigation.strategy_scalar,
                        "osint": mitigation.osint_scalar, "halt": overlay_halt,
                        "class": decision.asset_class if decision else None,
                    }
                    if overlay_halt:
                        entry_detail["halt_reason"] = "; ".join(mitigation.reasons)
                if interp_bias < 1.0:
                    entry_detail["interpret"] = {
                        "bias": round(interp_bias, 4),
                        "theses": interp_applied,
                    }
                if weight_bias != 1.0:
                    entry_detail["allocator"] = {
                        "weight_bias": round(weight_bias, 4),
                    }
                if persistence_halt:
                    entry_detail["persistence"] = {"halt": True, "reason": persistence_reason}
                events.append(MonitorEvent(datetime.now(UTC), symbol, "entry", price, entry_detail))
            elif (exit_ or forced_reason) and holds:
                qty = open_positions[symbol]
                intent = OrderIntent(
                    symbol=symbol,
                    action=OrderAction.SELL,
                    shares=int(abs(qty)),
                    entry=price,
                    stop=price * 1.10,  # protective; exits are market in v1
                    target=None,
                    strategy=strat.name,
                    risk_dollars=0.0,
                    account_number=self.account_number,
                )
                exit_detail: dict[str, object] = {"shares": int(abs(qty))}
                if forced_reason:
                    exit_detail["forced_exit"] = forced_reason
                    self._ledger_forced_exit(symbol, strat, price, qty, intent.intent_id, forced_reason)
                self.router.submit(
                    intent,
                    equity=equity,
                    existing_risk=existing_risk,
                    open_positions=open_count,
                    current_open_notional=open_notional,
                )
                events.append(MonitorEvent(datetime.now(UTC), symbol, "exit", price, exit_detail))
            else:
                events.append(MonitorEvent(datetime.now(UTC), symbol, "hold", price, {}))

        for ev in events:
            same = self._last_kind.get(ev.symbol) == ev.kind
            run = self._kind_runs.get(ev.symbol, 0) + 1 if same else 1
            self._kind_runs[ev.symbol] = run
            ev.is_transition = not same
            ev.poll_count = run
            if self.emit_on_change_only and same:
                continue  # edge mode: same state as last poll — suppress duplicate notification
            self._last_kind[ev.symbol] = ev.kind
            self.on_event(ev)

        # Dynamic dollar-hedge overlay: size the hedge sleeve from the book's drawdown and
        # heat, and surface a rebalance when the target position drifts past the no-trade
        # band. A rebalance is an action (not a persistent state), so it bypasses the
        # edge-dedup and always surfaces when due.
        if self.hedge_symbol and self.hedge_policy:
            self._equity_peak = max(self._equity_peak, equity)
            drawdown = equity / self._equity_peak - 1.0 if self._equity_peak > 0 else 0.0
            heat = existing_risk / equity if equity > 0 else 0.0
            target_w = hedge_weight(drawdown, policy=self.hedge_policy, heat=heat)
            try:
                hq = self.broker.quote(self.hedge_symbol)
                hpx = hq.mid or hq.lastTradePrice or 0.0
            except Exception:  # pragma: no cover - broker hiccup shouldn't break the poll
                hpx = 0.0
            if hpx > 0:
                target = hedge_shares(equity=equity, hedge_price=hpx, target_weight=target_w)
                current = int(open_positions.get(self.hedge_symbol, 0.0))
                delta = rebalance_delta(current, target)
                if delta != 0:
                    action = OrderAction.BUY if delta > 0 else OrderAction.SELL
                    intent = OrderIntent(
                        symbol=self.hedge_symbol, action=action, shares=abs(delta), entry=hpx,
                        stop=hpx * 0.9 if delta > 0 else hpx * 1.1, target=None,
                        strategy="dollar_hedge", risk_dollars=abs(delta) * hpx * 0.02,
                        account_number=self.account_number,
                    )
                    try:
                        self.router.submit(intent, equity=equity, existing_risk=existing_risk,
                                            open_positions=open_count,
                                            current_open_notional=open_notional)
                    except Exception as e:  # pragma: no cover
                        log.warning("monitor.hedge.route_failed", error=str(e))
                    self.on_event(MonitorEvent(datetime.now(UTC), self.hedge_symbol, "hedge", hpx,
                        {"weight": round(target_w, 3), "target": target, "delta": delta,
                         "drawdown": round(drawdown, 3)}))
        return events

    @staticmethod
    def _bar_stamp(df: object) -> object:
        """The last bar's timestamp, or None for a frame without a usable one."""
        try:
            frame = cast("pd.DataFrame", df)
            return frame["time"].iloc[-1] if "time" in frame.columns else frame.index[-1]
        except Exception:
            return None

    def _ledger_forced_exit(self, symbol: str, strat: object, price: float, qty: float,
                            intent_id: str, reason: str) -> None:
        """Why a position is being closed with no strategy signal behind the order."""
        if self.ledger is None:
            return
        try:
            self.ledger.append("FORCED_EXIT",
                               {"symbol": symbol, "price": price, "shares": float(abs(qty)),
                                "entry_price": self._entry_prices.get(symbol),
                                "atr_at_entry": self._atr_at_entry.get(symbol), "reason": reason},
                               intent_id=intent_id, strategy_id=str(getattr(strat, "name", "")),
                               strategy_version=self._strategy_version_of(strat))
        except Exception as e:                    # never let the audit path break a poll
            log.warning("monitor.ledger_forced_exit_failed", symbol=symbol, error=str(e))

    def _ledger_signal(self, symbol: str, strat: object, price: float, df: object, *,
                       sized: object, intent_id: str | None, reason: str,
                       conviction: float, trigger: str | None = None,
                       atr: float | None = None) -> None:
        """STRATEGY_SIGNAL for an entry that goes on to the router, SIGNAL_SUPPRESSED (with the
        reason) for one that does not. Without the second, an empty ledger would look the same as
        a broken one."""
        if self.ledger is None:
            return
        bar_ts = self._bar_stamp(df)
        payload = {"symbol": symbol, "price": price, "trigger": trigger, "atr": atr,
                   "shares": float(getattr(sized, "shares", 0) or 0),
                   "stop": getattr(sized, "stop", None), "target": getattr(sized, "target", None),
                   "conviction": round(float(conviction), 4), "bar_ts": str(bar_ts),
                   "reason": reason}
        try:
            self.ledger.append("SIGNAL_SUPPRESSED" if reason else "STRATEGY_SIGNAL", payload,
                               intent_id=intent_id, strategy_id=str(getattr(strat, "name", "")),
                               strategy_version=self._strategy_version_of(strat))
        except Exception as e:                    # never let the audit path break a poll
            log.warning("monitor.ledger_signal_failed", symbol=symbol, error=str(e))

    def _strategy_version_of(self, strat: object) -> str | None:
        from ..audit.versioning import strategy_version
        cache: dict[int, str] = self.__dict__.setdefault("_strategy_version_cache", {})
        key = id(strat)
        if key not in cache:
            cache[key] = strategy_version(strat)
        return cache[key] or None

    def _install_strategy_version_resolver(self, router: object) -> None:
        """Wire ``Router.strategy_version_for`` from this monitor's strategy instances."""
        inner = getattr(router, "inner", router)          # unwrap an ApprovalRouter
        if getattr(inner, "strategy_version_for", "missing") is not None:
            return
        from ..audit.versioning import strategy_version

        instances: dict[str, object] = {}
        for strat in (self.strategy, *self.strategy_map.values()):
            if strat is not None:
                instances.setdefault(str(getattr(strat, "name", "")), strat)
        cache: dict[str, str] = {}

        def resolve(name: str) -> str:
            if name not in cache:
                inst = instances.get(name)
                # An unknown name (a hedge intent, say): record nothing rather than attribute it to
                # a strategy that did not produce it.
                cache[name] = strategy_version(inst) if inst is not None else ""
            return cache[name]

        inner.strategy_version_for = resolve

    def run_forever(self, max_iterations: int | None = None) -> None:
        i = 0
        while True:
            try:
                self.step()
            except Exception as e:  # pragma: no cover
                log.exception("monitor.step.error", error=str(e))
            i += 1
            if max_iterations is not None and i >= max_iterations:
                return
            time.sleep(self.interval_seconds)
