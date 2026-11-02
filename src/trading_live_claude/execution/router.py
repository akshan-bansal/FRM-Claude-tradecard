"""Execution router. The ONLY thing that places orders.

A strategy emits a signal -> ``Router.submit(intent)`` checks every risk gate
and either dispatches to the chosen broker (paper or live) or rejects+logs.

One Router can hold several brokers: ``intent.broker`` names the route, and an empty one takes the
default. The gates are the same on every route and read the figures the caller passes, so a caller
trading more than one route must pass whole-book figures. Autonomous mode stays on one broker.

Modes:
  * ``paper`` / ``dry-run``  — safe defaults; no human confirmation.
  * ``live``                 — real-money orders. Requires the typed phrase
                               ``I UNDERSTAND THE RISK`` passed by a human.
  * ``autonomous``           — Claude-driven loop. NO typed confirmation, BUT
                               the AUTONOMOUS_ENABLED env-var sentinel must be
                               true, AND a daily trade/notional budget is
                               enforced in addition to all other gates.

Constructing ``Router(mode="live"|"autonomous", ...)`` directly raises
``LiveModeNotConfirmed``. Use ``Router.confirm_live`` / ``Router.confirm_autonomous``.
"""
from __future__ import annotations

import os
import secrets
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from ..brokers.base import Broker, BrokerError, OrderRejected
from ..brokers.models import Order, OrderAction, OrderType
from ..logging_setup import get_logger
from ..risk.heat import PortfolioHeat
from ..risk.kill_switch import KillSwitch
from .daily_budget import DailyBudget
from .journal import OrderJournal

if TYPE_CHECKING:
    from ..audit.ledger import Ledger
    from ..risk.spectral_var import SpectralVarGate
    from .basket import BasketGate

RouterMode = Literal["paper", "dry-run", "live", "autonomous"]
LIVE_CONFIRM_PHRASE = "I UNDERSTAND THE RISK"
AUTONOMOUS_ENV_VAR = "AUTONOMOUS_ENABLED"

log = get_logger(__name__)


class LiveModeNotConfirmed(RuntimeError):
    """Raised when live/autonomous mode is requested without the explicit confirmation."""


class AutonomousNotEnabled(RuntimeError):
    """Raised when autonomous mode is requested without AUTONOMOUS_ENABLED env var."""


def route_key(broker: object) -> str:
    """The name a lone broker routes under: the venue it fills against, else its own name."""
    for key in (getattr(broker, "venue", None), getattr(broker, "name", None)):
        if isinstance(key, str) and key:
            return key
    return "default"


def mint_intent_id() -> str:
    """Time-prefixed hex id: sorts by creation, unique per intent.

    The same shape the approval layer has always minted, so card and passbook records read the
    same as before; the approval stores now reuse this id instead of minting their own.
    """
    return f"{int(time.time_ns()):016x}-{secrets.token_hex(6)}"


@dataclass
class OrderIntent:
    """High-level order request emitted by a strategy.

    ``intent_id`` is minted here, when the intent comes into existence, so the gate decision, the
    card prompt, the broker order, the journals and the audit ledger all carry one identity.
    """

    symbol: str
    action: OrderAction
    shares: int
    entry: float
    stop: float
    target: float | None
    strategy: str
    risk_dollars: float
    account_number: str
    symbolId: int | None = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    intent_id: str = field(default_factory=mint_intent_id)
    # Content hash of the strategy instance that produced this intent (audit/versioning.py). Set by
    # the caller holding the instance; otherwise the Router's ``strategy_version_for`` resolver.
    strategy_version: str = ""
    # Which of the Router's brokers takes this order. Empty = the Router's default route.
    broker: str = ""


@dataclass
class GateDecision:
    accepted: bool
    rejected_reasons: list[str]
    # What the gate read from outside the intent (kill-switch, heat, budget, basket, spectral VaR),
    # and every resize it made, so ``trading audit replay`` can re-run the decision from the record.
    observed: dict[str, Any] = field(default_factory=dict)
    trims: list[dict[str, Any]] = field(default_factory=list)


class Router:
    def __init__(
        self,
        *,
        mode: RouterMode,
        broker: Broker | None = None,
        brokers: Mapping[str, Broker] | None = None,
        default_broker: str | None = None,
        journal: OrderJournal,
        kill_switch: KillSwitch,
        heat: PortfolioHeat,
        max_open_positions: int = 5,
        min_ticket_usd: float = 100.0,
        daily_budget: DailyBudget | None = None,
        max_gross_leverage: float = 1.0,
        max_position_notional_pct: float = 0.50,
        force_exit_atr_mult: float = 3.0,
        on_size_cap_breach: Literal["trim", "reject"] = "trim",
        ledger: Ledger | None = None,
        _confirmed: bool = False,
    ) -> None:
        if mode in {"live", "autonomous"} and not _confirmed:
            raise LiveModeNotConfirmed(
                f"Construct Router for mode={mode!r} via Router.confirm_live(...) or "
                "Router.confirm_autonomous(...)."
            )
        self.mode: RouterMode = mode
        if (broker is None) == (brokers is None):
            raise ValueError("Pass exactly one of broker= or brokers=.")
        if broker is not None:
            default_broker = route_key(broker)
            brokers = {default_broker: broker}
        assert brokers is not None
        if default_broker is None or default_broker not in brokers:
            raise ValueError(f"default_broker {default_broker!r} is not one of {sorted(brokers)}")
        self.brokers: dict[str, Broker] = dict(brokers)
        self.default_broker: str = default_broker
        # The default route's broker: what every single-broker caller has always read.
        self.broker: Broker = self.brokers[default_broker]
        self.journal = journal
        self.kill_switch = kill_switch
        self.heat = heat
        self.max_open_positions = max_open_positions
        self.min_ticket_usd = min_ticket_usd
        self.daily_budget = daily_budget
        # Landed 2026-09-08 to catch two failure modes seen on live paper sessions:
        # (a) a single boosted position taking 100% of equity, and (b) portfolio gross
        # notional exceeding cash. See docstrings on the gates + Router.check_forced_exits.
        self.max_gross_leverage = max_gross_leverage
        self.max_position_notional_pct = max_position_notional_pct
        self.force_exit_atr_mult = force_exit_atr_mult
        # 'trim' (default, 2026-09-09) = when the per-symbol notional cap or the
        # portfolio gross-leverage cap would be breached, RESIZE the intent's shares
        # down to fit the tighter of the two caps and accept. 'reject' = the pre-
        # 2026-09-09 behavior of rejecting the whole intent. Trim prevents the
        # observed failure mode where a boosted name (VDY.TO ts_momentum × 2.33x
        # allocator) fires ENTRY every poll, gets rejected every poll, floods
        # Telegram, and never opens even a fractional position — while a 50%-cap
        # position would have been perfectly valid.
        self.on_size_cap_breach: Literal["trim", "reject"] = on_size_cap_breach
        # Card-signed basket gate (execution/basket.py). None = not enforced, which is how every
        # session started without --require-basket runs. Set by the launch scripts after
        # construction; when set, an ENTRY outside the signed basket, or on a venue with no valid
        # signed basket, is rejected. Exits are never gated by it.
        self.basket_gate: BasketGate | None = None
        # Spectral VaR gate (risk/spectral_var.py, over the BSM_Spectral engine). None = off, the
        # default. When set it runs after the size caps on ENTRIES only, and can only trim an
        # intent or reject it; it never enlarges one and never touches an exit.
        self.spectral_gate: SpectralVarGate | None = None
        # Audit ledger (audit/ledger.py). Dual-written beside the journals; None = no ledger, and
        # the router behaves exactly as it did before the ledger existed.
        self.ledger = ledger
        # Strategy name -> version, for intents that do not carry their own.
        self.strategy_version_for: Callable[[str], str] | None = None
        self._risk_check_version: str | None = None
        self._rcv_key: tuple[int, int] | None = None
        # Intent ids already announced. The card path gates an intent, waits for the card, then
        # submits the same intent again; without this INTENT_CREATED would be written twice.
        self._announced: deque[str] = deque(maxlen=512)

    # ----- alternate constructors --------------------------------------------

    @classmethod
    def confirm_live(
        cls,
        *,
        confirmation: str,
        broker: Broker | None = None,
        brokers: Mapping[str, Broker] | None = None,
        default_broker: str | None = None,
        journal: OrderJournal,
        kill_switch: KillSwitch,
        heat: PortfolioHeat,
        max_open_positions: int = 5,
        min_ticket_usd: float = 100.0,
        max_gross_leverage: float = 1.0,
        max_position_notional_pct: float = 0.50,
        force_exit_atr_mult: float = 3.0,
        on_size_cap_breach: Literal["trim", "reject"] = "trim",
        ledger: Ledger | None = None,
    ) -> "Router":
        if confirmation.strip() != LIVE_CONFIRM_PHRASE:
            raise LiveModeNotConfirmed(
                f'Live mode requires confirmation phrase exactly: "{LIVE_CONFIRM_PHRASE}"'
            )
        return cls(
            mode="live",
            broker=broker,
            brokers=brokers,
            default_broker=default_broker,
            journal=journal,
            kill_switch=kill_switch,
            heat=heat,
            max_open_positions=max_open_positions,
            min_ticket_usd=min_ticket_usd,
            max_gross_leverage=max_gross_leverage,
            max_position_notional_pct=max_position_notional_pct,
            force_exit_atr_mult=force_exit_atr_mult,
            on_size_cap_breach=on_size_cap_breach,
            ledger=ledger,
            _confirmed=True,
        )

    @classmethod
    def confirm_autonomous(
        cls,
        *,
        broker: Broker,
        journal: OrderJournal,
        kill_switch: KillSwitch,
        heat: PortfolioHeat,
        daily_budget: DailyBudget,
        max_open_positions: int = 5,
        min_ticket_usd: float = 100.0,
        max_gross_leverage: float = 1.0,
        max_position_notional_pct: float = 0.50,
        force_exit_atr_mult: float = 3.0,
        on_size_cap_breach: Literal["trim", "reject"] = "trim",
        ledger: Ledger | None = None,
    ) -> "Router":
        """Build an autonomous-mode router.

        Refuses unless the AUTONOMOUS_ENABLED environment sentinel is set to a
        truthy value AT THE TIME OF CONSTRUCTION. We deliberately do *not*
        cache or shortcut this check; the env-var must be set for every new
        autonomous loop process.
        """
        flag = os.environ.get(AUTONOMOUS_ENV_VAR, "").strip().lower()
        if flag not in {"1", "true", "yes", "on"}:
            raise AutonomousNotEnabled(
                f"Autonomous mode requires environment variable {AUTONOMOUS_ENV_VAR}=true. "
                "Set it explicitly when launching the daemon; do not bake it into shell rc files."
            )
        return cls(
            mode="autonomous",
            broker=broker,
            journal=journal,
            kill_switch=kill_switch,
            heat=heat,
            max_open_positions=max_open_positions,
            min_ticket_usd=min_ticket_usd,
            daily_budget=daily_budget,
            max_gross_leverage=max_gross_leverage,
            max_position_notional_pct=max_position_notional_pct,
            force_exit_atr_mult=force_exit_atr_mult,
            on_size_cap_breach=on_size_cap_breach,
            ledger=ledger,
            _confirmed=True,
        )

    # ----- risk gates ---------------------------------------------------------

    def _gate(
        self,
        intent: OrderIntent,
        *,
        equity: float,
        existing_risk: float,
        open_positions: int,
        current_open_notional: float = 0.0,
    ) -> GateDecision:
        reasons: list[str] = []
        observed: dict[str, Any] = {}
        trims: list[dict[str, Any]] = []

        # 0. route. An intent naming a broker this router does not hold is rejected, never sent to
        # the default, where it would fill on a venue nobody chose.
        route = self.route(intent)
        observed["route"] = route
        if route not in self.brokers:
            reasons.append(f"unknown broker {route!r}: router holds {sorted(self.brokers)}")

        # Whether this order only reduces a position the routed broker holds. The gates that stop new
        # risk must not trap the way out of it, so the kill-switch, the minimum ticket, the heat cap
        # and the daily budget all let such an order through. If the holding cannot be read, nothing
        # shows the order reduces anything, and every gate applies.
        held = self._held_quantity(intent, route)
        reduces = held is not None and intent.shares > 0 and (
            (intent.action == OrderAction.SELL and held >= intent.shares)
            or (intent.action in (OrderAction.BUY, OrderAction.BUY_TO_COVER) and -held >= intent.shares))
        observed["held_quantity"] = held
        observed["reduces_position"] = reduces

        # 1. kill switch
        halted = self.kill_switch.state().halted
        observed["kill_switch_halted"] = halted
        if halted and not reduces:
            reasons.append("kill-switch tripped")

        # 2. equity available
        if equity <= 0:
            reasons.append(f"equity {equity} <= 0")

        # 3. share count > 0
        if intent.shares <= 0:
            reasons.append("intent.shares <= 0")

        # 4. min ticket
        notional = intent.shares * intent.entry
        if notional < self.min_ticket_usd and not reduces:
            reasons.append(f"notional ${notional:,.2f} below min ${self.min_ticket_usd}")

        # 5. open positions cap (only for entries)
        if intent.action == OrderAction.BUY and open_positions >= self.max_open_positions:
            reasons.append(f"open positions {open_positions} >= cap {self.max_open_positions}")

        # 5b. card-signed basket (additional gate; entries only, exits always pass)
        if intent.action == OrderAction.BUY and self.basket_gate is not None:
            # The gate holds one venue's basket, and that venue is the default route's. Any other
            # route has no signed basket to check against, so it takes no new entries.
            basket_reason = (self.basket_gate.check_entry(intent.symbol)
                             if route == self.default_broker
                             else f"no card-signed basket gate on route {route}")
            observed["basket_reason"] = basket_reason
            if basket_reason:
                reasons.append(basket_reason)

        # 6. portfolio heat
        snap = self.heat.snapshot(equity=equity, open_risk_dollars=existing_risk + intent.risk_dollars)
        observed["heat_pct"] = snap.heat_pct
        if snap.breached and not reduces:
            reasons.append(f"portfolio heat {snap.heat_pct:.2%} > cap {self.heat.cap_pct:.2%}")

        # 7. stop side sanity
        if intent.action == OrderAction.BUY and intent.stop >= intent.entry:
            reasons.append(f"long stop {intent.stop} >= entry {intent.entry}")
        if intent.action == OrderAction.SELL and intent.stop <= intent.entry:
            reasons.append(f"short stop {intent.stop} <= entry {intent.entry}")

        # 8. daily budget (autonomous mode only)
        if self.daily_budget is not None:
            snap_b = self.daily_budget.snapshot()
            observed["daily_budget"] = {"trades_today": snap_b.trades_today,
                                        "notional_today_usd": snap_b.notional_today_usd}
            ok, reason = snap_b.admits(additional_notional_usd=notional)
            if not ok and not reduces:
                reasons.append(reason)

        # 9 + 10. Size caps: per-symbol notional cap + portfolio gross-leverage cap
        # (2026-09-08 / trim mode 2026-09-09). Only apply to entries — exits reduce
        # exposure and are always allowed regardless of size.
        #
        # In 'trim' mode (default), when the intent's proposed notional exceeds the
        # tighter of the two caps, we RESIZE intent.shares down to what fits and let
        # the trade proceed. This closes the loop where a boosted name (allocator
        # 2.33x × ts_momentum × vol-target-at-leverage-cap) would size to 100% of
        # equity, get rejected 39x/session by the per-symbol cap, spam Telegram, and
        # never open even a fractional position — while a 50%-cap position would have
        # been perfectly valid research data.
        #
        # In 'reject' mode, the old behavior of appending a rejection reason. Kept as
        # an option for cases where any breach of the cap is a genuine "don't trade"
        # signal (e.g., misconfigured allocator) rather than a "trim to fit" one.
        if intent.action == OrderAction.BUY and equity > 0 and intent.entry > 0:
            symbol_pct = notional / equity
            gross_leverage = (current_open_notional + notional) / equity
            symbol_over = symbol_pct > self.max_position_notional_pct
            leverage_over = gross_leverage > self.max_gross_leverage

            if symbol_over or leverage_over:
                if self.on_size_cap_breach == "trim":
                    # Compute the max notional that fits BOTH caps. Take the tighter.
                    symbol_max_notional = equity * self.max_position_notional_pct
                    leverage_max_notional = max(
                        0.0, equity * self.max_gross_leverage - current_open_notional
                    )
                    fit_notional = min(symbol_max_notional, leverage_max_notional)
                    if fit_notional <= 0:
                        # No room left at all — sleeve is already at leverage cap and
                        # this new symbol can't fit. Fall through to reject rather
                        # than trim to 0 shares (which the min-ticket gate would flag).
                        reasons.append(
                            f"no room after size caps: symbol cap ${symbol_max_notional:,.0f}, "
                            f"leverage headroom ${leverage_max_notional:,.0f}"
                        )
                    else:
                        # Trim shares to fit. Recompute notional for downstream gates
                        # (min-ticket check must see the trimmed size, not the original).
                        trimmed_shares = int(fit_notional // intent.entry)
                        if trimmed_shares <= 0:
                            reasons.append(
                                f"trim would round to 0 shares (fit ${fit_notional:,.0f} / "
                                f"entry ${intent.entry:,.2f})"
                            )
                        else:
                            log.info("router.size_trimmed",
                                     symbol=intent.symbol,
                                     from_shares=intent.shares,
                                     to_shares=trimmed_shares,
                                     original_notional=notional,
                                     trimmed_notional=trimmed_shares * intent.entry,
                                     reason=(
                                         f"symbol_cap={symbol_pct:.1%}>{self.max_position_notional_pct:.1%}"
                                         if symbol_over else
                                         f"leverage={gross_leverage:.2f}x>{self.max_gross_leverage:.2f}x"
                                     ))
                            trims.append({"cap": "symbol_notional" if symbol_over else "gross_leverage",
                                          "from_shares": intent.shares, "to_shares": trimmed_shares,
                                          "fit_notional": fit_notional})
                            intent.shares = trimmed_shares
                            notional = trimmed_shares * intent.entry
                            # Re-check min-ticket on the trimmed size (must not fall through
                            # to a size below the min).
                            if notional < self.min_ticket_usd:
                                reasons.append(
                                    f"after trim: notional ${notional:,.2f} below min "
                                    f"${self.min_ticket_usd}"
                                )
                else:
                    # Reject mode — preserve the pre-2026-09-09 gate behavior.
                    if symbol_over:
                        reasons.append(
                            f"single-name notional {symbol_pct:.1%} > cap {self.max_position_notional_pct:.1%}"
                        )
                    if leverage_over:
                        reasons.append(
                            f"portfolio gross leverage {gross_leverage:.2f}x > cap "
                            f"{self.max_gross_leverage:.2f}x  (open ${current_open_notional:,.0f} + "
                            f"intent ${notional:,.0f} vs equity ${equity:,.0f})"
                        )

        # 11. Spectral VaR (opt-in; entries only). Runs last so it sees the size the caps left, and
        # can only make it smaller: a trim to what the VaR cap admits, or a rejection.
        if (self.spectral_gate is not None and intent.action == OrderAction.BUY and not reasons
                and equity > 0 and intent.shares > 0):
            check = self.spectral_gate.check(intent.symbol, shares=intent.shares,
                                             entry=intent.entry, equity=equity)
            observed["spectral_var"] = check.observed
            if check.reason:
                reasons.append(check.reason)
            elif check.max_shares is not None and check.max_shares < intent.shares:
                if check.max_shares <= 0:
                    reasons.append(f"spectral VaR: {check.summary} admits no whole unit")
                else:
                    trims.append({"cap": "spectral_var", "from_shares": intent.shares,
                                  "to_shares": check.max_shares, "summary": check.summary})
                    intent.shares = check.max_shares
                    notional = intent.shares * intent.entry
                    if notional < self.min_ticket_usd:
                        reasons.append(f"after spectral VaR trim: notional ${notional:,.2f} below "
                                       f"min ${self.min_ticket_usd}")

        return GateDecision(accepted=not reasons, rejected_reasons=reasons,
                            observed=observed, trims=trims)

    def check_forced_exits(
        self,
        positions: list[dict],
        current_prices: dict[str, float],
        atr_at_entry: dict[str, float],
    ) -> list[dict]:
        """Router-level intra-day exit gate (2026-09-08).

        Runs on the monitor's poll cadence, independent of the strategy's bar cadence.
        Detects positions whose unrealized loss since entry exceeds
        ``force_exit_atr_mult × ATR_at_entry`` and returns close-intent dicts for the
        monitor to submit. Closes the gap where a daily-bar strategy (ts_momentum,
        rsi_meanrevert) can't see a mid-day drawdown until the next daily close.

        Inputs are dicts to keep the router broker-agnostic — the monitor already
        maintains its own position + ATR map and knows how to convert an intent dict
        into an OrderIntent for its broker.

        * ``positions`` — list of dicts with keys: symbol, entry_price, quantity, side
          ('long' | 'short'). Only 'long' currently supported; short-close semantics TBD.
        * ``current_prices`` — {symbol: latest mid quote}. Missing symbol = skip
          (fail-open so a quote hiccup can't spuriously force-close a position).
        * ``atr_at_entry`` — {symbol: ATR value at entry bar}. Missing = skip.

        Returns a list of exit-intent dicts. Empty when the mult is 0 (feature disabled)
        or when no position breaches. Never raises — a per-position exception is logged
        and the position is skipped, matching the fail-open discipline of the other
        intel-adjacent gates.
        """
        if self.force_exit_atr_mult <= 0.0:
            return []
        exits: list[dict] = []
        for pos in positions:
            try:
                sym = pos.get("symbol")
                if not sym:
                    continue
                side = pos.get("side", "long")
                if side != "long":
                    continue  # short semantics deferred
                entry = float(pos.get("entry_price", 0.0))
                qty = float(pos.get("quantity", 0.0))
                if entry <= 0 or qty <= 0:
                    continue
                price = current_prices.get(sym)
                atr = atr_at_entry.get(sym)
                if price is None or atr is None or atr <= 0:
                    continue
                loss_per_share = entry - float(price)
                if loss_per_share <= 0:
                    continue  # position is at or above entry — not a loss
                if loss_per_share > self.force_exit_atr_mult * atr:
                    exits.append({
                        "symbol": sym,
                        "action": "SELL",
                        "shares": qty,
                        "reason": (
                            f"forced-exit: loss ${loss_per_share:.2f}/sh > "
                            f"{self.force_exit_atr_mult:.1f}x ATR ${atr:.2f} since entry ${entry:.2f}"
                        ),
                    })
            except Exception as e:  # pragma: no cover — belt-and-suspenders
                log.warning("router.forced_exit.eval_failed", symbol=pos.get("symbol"), error=str(e))
        return exits

    # ----- main entrypoint ----------------------------------------------------

    def submit(
        self,
        intent: OrderIntent,
        *,
        equity: float,
        existing_risk: float,
        open_positions: int,
        current_open_notional: float = 0.0,
    ) -> Order | None:
        self.announce_intent(intent)
        requested_shares = intent.shares
        decision = self._gate(
            intent, equity=equity, existing_risk=existing_risk, open_positions=open_positions,
            current_open_notional=current_open_notional,
        )
        self.record_gate(intent, decision, requested_shares=requested_shares, equity=equity,
                         existing_risk=existing_risk, open_positions=open_positions,
                         current_open_notional=current_open_notional)

        self.journal.order_intent(
            {
                "intent_id": intent.intent_id,
                "mode": self.mode,
                "strategy": intent.strategy,
                "broker": self.route(intent),
                "symbol": intent.symbol,
                "action": intent.action.value,
                "shares": intent.shares,
                "entry": intent.entry,
                "stop": intent.stop,
                "target": intent.target,
                "risk_dollars": intent.risk_dollars,
                "accepted": decision.accepted,
                "rejected_reasons": decision.rejected_reasons,
            }
        )

        if not decision.accepted:
            self.journal.rejected({"intent_id": intent.intent_id, "symbol": intent.symbol,
                                   "reasons": decision.rejected_reasons})
            log.warning("router.rejected", symbol=intent.symbol, reasons=decision.rejected_reasons)
            return None

        if self.mode == "dry-run":
            log.info("router.dry_run.skip_placement", symbol=intent.symbol, shares=intent.shares)
            return None

        # The gate rejected any route this router does not hold, so this lookup cannot miss.
        broker = self.brokers[self.route(intent)]
        self._ledger_event("BROKER_SUBMITTED", {
            "broker": broker.name, "venue": getattr(broker, "venue", None),
            "account": intent.account_number, "symbol": intent.symbol,
            "action": intent.action.value, "shares": intent.shares, "entry": intent.entry,
        }, intent=intent)

        order = Order(
            symbol=intent.symbol,
            symbolId=intent.symbolId,
            accountId=intent.account_number,
            action=intent.action,
            orderType=OrderType.MARKET,
            totalQuantity=intent.shares,
            intended_stop=intent.stop,
            intended_target=intent.target,
            risk_dollars=intent.risk_dollars,
            strategy=intent.strategy,
        )

        # Read before dispatch: a broker may mutate the order it is handed.
        requested_qty = float(order.totalQuantity or 0.0)
        try:
            placed = broker.place_order(order)
        except OrderRejected as e:
            self._ledger_event("BROKER_REJECTED", {
                "broker": broker.name, "symbol": intent.symbol, "error": str(e),
            }, intent=intent)
            self.journal.rejected({"intent_id": intent.intent_id, "symbol": intent.symbol,
                                   "reasons": [f"broker: {e}"]})
            log.error("router.broker_rejected", symbol=intent.symbol, error=str(e))
            return None
        except BrokerError as e:
            # Anything else the broker raised (TokenExpired, a timeout, a 5xx). The order may or
            # may not be at the broker, so it is recorded as unknown, never as rejected, and the
            # exception still propagates: callers handle it exactly as they did before.
            self._ledger_event("BROKER_ERROR", {
                "broker": broker.name, "symbol": intent.symbol,
                "error_type": type(e).__name__, "error": str(e), "order_state": "unknown",
            }, intent=intent)
            self.journal.rejected({"intent_id": intent.intent_id, "symbol": intent.symbol,
                                   "reasons": [f"broker error, order state unknown: "
                                               f"{type(e).__name__}: {e}"]})
            log.error("router.broker_error", symbol=intent.symbol, error=str(e))
            raise
        # PARTIAL when the broker took less than was asked for. This is the placement
        # acknowledgement; the execution itself (price, fee, exec id) is pulled back from the
        # broker by audit/recon.py and recorded as EXECUTION_REPORTED.
        filled_qty = float(placed.totalQuantity or 0.0)
        self._ledger_event("PARTIAL" if 0 < filled_qty < requested_qty else "FILLED", {
            "requested_shares": requested_qty, "symbol": placed.symbol,
            "action": placed.action.value, "shares": placed.totalQuantity,
            "broker_ref": getattr(placed, "broker_ref", None),
        }, intent=intent, broker_order_id=placed.id, execution={
            "source": "place_order", "broker": broker.name, "order_id": placed.id,
            "requested_quantity": requested_qty, "quantity": placed.totalQuantity,
            # Only what the broker's acknowledgement carried; never the intent's reference price.
            "price": getattr(placed, "avgExecPrice", None),
        })
        self.journal.fill(
            {
                "intent_id": intent.intent_id,
                "mode": self.mode,
                "broker": broker.name,
                "order_id": placed.id,
                "symbol": placed.symbol,
                "shares": placed.totalQuantity,
                "action": placed.action.value,
            }
        )
        if self.ledger is not None:
            from ..audit.recon import report_order
            report_order(self.ledger, broker, intent.account_number, placed.id,
                         placed_at=intent.timestamp)
        return placed

    def cancel(self, account_number: str, order_id: int, *, reason: str,
               intent_id: str | None = None, broker: str = "") -> bool:
        """Cancel an open order through the Router, so the request and its outcome are recorded.

        Returns True when the broker accepted the cancel. A failed cancel is recorded and
        returns False; it never raises, because the callers (a kill-switch sweep, an expiry) must
        go on to the next order. ``broker`` is the route the order went out on; empty = default.
        """
        route = broker or self.default_broker
        target = self.brokers.get(route)
        payload: dict[str, Any] = {"broker": target.name if target is not None else route,
                                   "account": account_number,
                                   "order_id": order_id, "reason": reason}
        self._ledger_event("CANCEL_REQUESTED", payload, intent_id=intent_id,
                           broker_order_id=order_id)
        try:
            if target is None:
                raise BrokerError(f"unknown broker {route!r}")
            target.cancel_order(account_number, order_id)
        except BrokerError as e:
            self._ledger_event("CANCEL_FAILED", payload | {"error": str(e)}, intent_id=intent_id,
                               broker_order_id=order_id)
            self.journal.cancel(payload | {"intent_id": intent_id, "ok": False, "error": str(e)})
            log.error("router.cancel_failed", order_id=order_id, error=str(e))
            return False
        self._ledger_event("CANCELLED", payload, intent_id=intent_id, broker_order_id=order_id)
        self.journal.cancel(payload | {"intent_id": intent_id, "ok": True})
        return True

    # ----- routing -------------------------------------------------------------

    def _held_quantity(self, intent: OrderIntent, route: str) -> float | None:
        """What the routed broker holds of the intent's symbol, signed; None if it cannot be read."""
        broker = self.brokers.get(route)
        if broker is None:
            return None
        try:
            positions = broker.positions(intent.account_number)
        except Exception as e:                  # a read failure must not look like a holding
            log.error("router.held_quantity_failed", symbol=intent.symbol, error=str(e))
            return None
        return float(sum(p.openQuantity for p in positions if p.symbol == intent.symbol))

    def route(self, intent: OrderIntent) -> str:
        """The route an intent asks for. It may not be one this router holds; the gate checks."""
        return intent.broker or self.default_broker

    # ----- audit ---------------------------------------------------------------

    def announce_intent(self, intent: OrderIntent) -> None:
        """Record an intent's creation once, however many times it is gated."""
        if intent.intent_id in self._announced:
            return
        self._announced.append(intent.intent_id)
        self._ledger_event("INTENT_CREATED", {
            "symbol": intent.symbol, "action": intent.action.value, "shares": intent.shares,
            "entry": intent.entry, "stop": intent.stop, "target": intent.target,
            "risk_dollars": intent.risk_dollars, "account": intent.account_number,
            "broker": self.route(intent),
            "created_at": intent.timestamp.isoformat(),
            "pre_gate_ms": round((datetime.now(UTC) - intent.timestamp).total_seconds() * 1000, 1),
        }, intent=intent)

    def record_gate(self, intent: OrderIntent, decision: GateDecision, *, requested_shares: int,
                    equity: float, existing_risk: float, open_positions: int,
                    current_open_notional: float) -> None:
        """Write the gate decision with everything it read, so it can be replayed exactly."""
        if self.ledger is None:
            return
        from ..audit.versioning import gate_thresholds
        for trim in decision.trims:
            self._ledger_event("RISK_TRIMMED", trim, intent=intent)
        self._ledger_event("RISK_CHECK" if decision.accepted else "RISK_REJECTED", {
            "accepted": decision.accepted, "rejected_reasons": decision.rejected_reasons,
            "symbol": intent.symbol, "action": intent.action.value, "broker": intent.broker,
            "requested_shares": requested_shares, "shares": intent.shares,
            "entry": intent.entry, "stop": intent.stop, "target": intent.target,
            "risk_dollars": intent.risk_dollars,
            "inputs": {"equity": equity, "existing_risk": existing_risk,
                       "open_positions": open_positions,
                       "current_open_notional": current_open_notional},
            "observed": decision.observed,
            "thresholds": gate_thresholds(self),
        }, intent=intent)

    def risk_check_version(self) -> str:
        """Content hash of this router's gate code and thresholds (audit/versioning.py), cached.

        Recomputed when an opt-in gate is attached after construction, since that changes it.
        """
        from ..audit.versioning import risk_check_version
        key = (id(self.basket_gate), id(self.spectral_gate))
        if self._risk_check_version is None or self._rcv_key != key:
            self._risk_check_version = risk_check_version(self)
            self._rcv_key = key
        return self._risk_check_version

    def _strategy_version(self, intent: OrderIntent) -> str | None:
        if intent.strategy_version:
            return intent.strategy_version
        if self.strategy_version_for is None:
            return None
        try:
            return self.strategy_version_for(intent.strategy) or None
        except Exception as e:                          # a resolver must never break a trade
            log.warning("router.strategy_version_failed", strategy=intent.strategy, error=str(e))
            return None

    def _ledger_event(self, event: str, payload: dict[str, Any], *,
                      intent: OrderIntent | None = None, intent_id: str | None = None,
                      broker_order_id: int | str | None = None,
                      execution: dict[str, Any] | None = None) -> None:
        """Dual-write one audit event. Never raises into the order path unless the ledger is strict."""
        if self.ledger is None:
            return
        self.ledger.append(event, payload,
                           intent_id=intent.intent_id if intent is not None else intent_id,
                           broker_order_id=broker_order_id, execution=execution,
                           strategy_id=intent.strategy if intent is not None else None,
                           strategy_version=self._strategy_version(intent) if intent else None,
                           risk_check_version=self.risk_check_version())

    @staticmethod
    def limits_from(settings: Any) -> dict[str, Any]:
        """``build_default`` keyword arguments for every risk knob in ``config/trading.yaml``.

        Every runner passes this, so a knob means the same thing in paper and in live, and a new one
        only has to be added here.
        """
        return {
            "cap_pct": settings.portfolio_heat_cap,
            "max_drawdown_pct": settings.max_drawdown_kill_switch,
            "daily_loss_limit_pct": settings.daily_loss_limit_pct,
            "max_open_positions": settings.max_open_positions,
            "min_ticket_usd": settings.min_ticket_usd,
            "max_gross_leverage": settings.max_gross_leverage,
            "max_position_notional_pct": settings.max_position_notional_pct,
            "force_exit_atr_mult": settings.force_exit_atr_mult,
            "on_size_cap_breach": settings.on_size_cap_breach,
        }

    @classmethod
    def build_default(
        cls,
        *,
        mode: RouterMode,
        broker: Broker | None = None,
        brokers: Mapping[str, Broker] | None = None,
        default_broker: str | None = None,
        state_dir: Path,
        cap_pct: float = 0.05,
        max_drawdown_pct: float = 0.03,
        daily_loss_limit_pct: float = 0.03,
        max_open_positions: int = 5,
        min_ticket_usd: float = 100.0,
        live_confirmation: str | None = None,
        daily_max_trades: int = 10,
        daily_max_notional_usd: float = 10_000.0,
        max_gross_leverage: float = 1.0,
        max_position_notional_pct: float = 0.50,
        force_exit_atr_mult: float = 3.0,
        on_size_cap_breach: Literal["trim", "reject"] = "trim",
        ledger: Ledger | None = None,
    ) -> "Router":
        journal = OrderJournal(state_dir)
        ks = KillSwitch(state_dir, max_drawdown_pct=max_drawdown_pct, daily_loss_limit_pct=daily_loss_limit_pct)
        heat = PortfolioHeat(cap_pct=cap_pct)
        gate_kwargs = dict(
            max_open_positions=max_open_positions,
            min_ticket_usd=min_ticket_usd,
            max_gross_leverage=max_gross_leverage,
            max_position_notional_pct=max_position_notional_pct,
            force_exit_atr_mult=force_exit_atr_mult,
            on_size_cap_breach=on_size_cap_breach,
            ledger=ledger,
        )
        if mode == "live":
            if not live_confirmation:
                raise LiveModeNotConfirmed("Pass live_confirmation when mode='live'.")
            return cls.confirm_live(
                confirmation=live_confirmation,
                broker=broker,
                brokers=brokers,
                default_broker=default_broker,
                journal=journal,
                kill_switch=ks,
                heat=heat,
                **gate_kwargs,
            )
        if mode == "autonomous":
            if broker is None:
                raise ValueError("Autonomous mode routes to one broker: pass broker=.")
            budget = DailyBudget(
                state_dir,
                max_trades_per_day=daily_max_trades,
                max_notional_per_day_usd=daily_max_notional_usd,
                mode="autonomous",
            )
            return cls.confirm_autonomous(
                broker=broker,
                journal=journal,
                kill_switch=ks,
                heat=heat,
                daily_budget=budget,
                **gate_kwargs,
            )
        return cls(
            mode=mode,
            broker=broker,
            brokers=brokers,
            default_broker=default_broker,
            journal=journal,
            kill_switch=ks,
            heat=heat,
            **gate_kwargs,
        )
