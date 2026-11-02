"""One way to build a trading session, whatever the venue and whether the money is real.

A paper run is only a rehearsal if the session it rehearses is built the same way, so every runner
comes through here. :func:`plan_session` resolves every setting into a :class:`SessionPlan`, plain data
with no broker in it. :func:`build_session` then assembles the engine one layer at a time: feed and
data, strategy, sizing, intel, risk gates, execution, audit, alerts.

A paper plan and its money counterpart may differ only in :data:`NECESSARY_DIFFERENCES`: the real broker,
the stricter ledger, and the simulated account's own balance and costs. tests/test_session.py compares
the plans, so a new knob that reaches one and not the other fails there. The plan's fingerprint is written
to the ledger as ``SESSION_PLAN``, so a money session can show it ran the settings that were practised.

Knobs come from ``config/trading.yaml``; a flag overrides one for that run.
"""
from __future__ import annotations

import argparse
import asyncio
import atexit
import hashlib
import json
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from ..analysis.symbol_validation import preflight
from ..analysis.universe import CRYPTO_SLEEVE, validated_for
from ..audit.ledger import Ledger
from ..audit.semantics import instrument_currency
from ..audit.versioning import session_provenance
from ..brokers.base import Broker, BrokerError
from ..brokers.paper import PaperBroker
from ..data.cache import CandleCache
from ..data.market import MarketData
from ..execution.approval import wire_card_approval
from ..execution.basket import attach_basket_gate
from ..execution.router import (
    LIVE_CONFIRM_PHRASE,
    AutonomousNotEnabled,
    LiveModeNotConfirmed,
    Router,
    RouterMode,
)
from ..intel.routing import OverlayProvider, PersistenceGate, futures_overrides
from ..intel.vs_engine import MarketContext, VSInvestmentEngine
from ..logging_setup import get_logger
from ..risk.sizing import PositionSizer
from ..risk.spectral_var import attach_spectral_gate
from ..strategies import STRATEGIES
from ..strategies.base import Strategy
from .alerter import AlertConfig, Alerter
from .live_loop import LiveMonitor, MonitorEvent, monitor_policy

log = get_logger(__name__)

Venue = Literal["questrade", "kraken", "ib"]
VENUES: tuple[Venue, ...] = ("questrade", "kraken", "ib")
Emit = Callable[[str], object]

# What may differ between a paper plan and its money counterpart, and why: the mode and the fact of real
# money; a ledger stream of its own, strict because a money order must not go unrecorded; and the
# simulated account's balance and costs, which a real account has for itself.
NECESSARY_DIFFERENCES = frozenset({"mode", "money", "stream", "strict_ledger", "paper_equity", "paper_costs"})

# Paper's costs per venue. ASSUMPTIONS, to be replaced by what `trading audit query fill-quality` measures:
# Questrade keeps the long-standing flat ticket; Kraken is its published base-tier taker rate; IB is its
# minimum per-order commission. Slippage is charged on top of crossing the spread. Override any of it with
# `paper_costs` in trading.yaml.
DEFAULT_PAPER_COSTS: dict[str, dict[str, float]] = {
    "questrade": {"commission_per_trade": 4.95, "commission_bps": 0.0, "slippage_bps": 5.0},
    "kraken": {"commission_per_trade": 0.0, "commission_bps": 40.0, "slippage_bps": 5.0},
    "ib": {"commission_per_trade": 1.0, "commission_bps": 0.0, "slippage_bps": 5.0},
}
_STREAMS: dict[tuple[str, bool], str] = {
    ("questrade", False): "qt-paper", ("questrade", True): "qt-live",
    ("kraken", False): "kraken", ("kraken", True): "kraken-live",
    ("ib", False): "ib", ("ib", True): "ib-live",
}
DEFAULT_IB_SYMBOLS = ("AAPL", "MSFT", "SPY", "QQQ", "IWM")
# Government bonds are hedges only on IB: dormant until the equity overlay says risk-off.
_GOV_BONDS = frozenset({"TLT", "IEF", "SHY", "IEI", "TLH", "GOVT", "BND", "AGG", "MBB",
                        "XBB.TO", "ZAG.TO", "VAB.TO", "ZFL.TO", "ZDB.TO"})
_HEDGE_TRIGGER = 0.5          # equity-class scalar at or below this activates the hedge
_HEDGE_BIAS = 1.0             # neutral sizing when active, not levered


class SessionError(RuntimeError):
    """A session cannot start. The message says why and what to do."""


def _quiet(_message: str) -> None:
    return None


@dataclass(frozen=True)
class SessionOptions:
    """Per-run overrides. ``None`` means take the ``config/trading.yaml`` value."""

    interval: int | None = None
    level: bool | None = None
    hedge: bool | None = None
    intel_overlay: bool | None = None
    intel_refresh: float | None = None
    require_card: bool | None = None
    card_shim_port: int | None = None
    card_ttl: float | None = None
    require_basket: bool | None = None
    spectral_var_cap: float | None = None
    spectral_var_level: float | None = None
    spectral_var_horizon: int | None = None
    paper_equity: float | None = None
    audit_ledger: bool = True
    allow_screened: bool = False
    stream: str | None = None


@dataclass(frozen=True)
class StrategyBook:
    """The strategy each symbol trades, and the fallback for a symbol not named."""

    default: Strategy
    per_symbol: Mapping[str, Strategy] = field(default_factory=dict)

    def name_for(self, symbol: str) -> str:
        return self.per_symbol.get(symbol, self.default).name


@dataclass(frozen=True)
class SessionPlan:
    """Every setting a session runs on, resolved. Plain data, so two plans can be compared."""

    venue: str
    mode: str
    money: bool
    stream: str
    strict_ledger: bool
    audit_ledger: bool
    symbols: tuple[str, ...]
    strategy: str
    strategy_map: dict[str, str]
    account_currency: str
    interval_seconds: int
    emit_on_change_only: bool
    limits: dict[str, Any]
    risk_per_trade: float
    risk_model: str
    heat_aggregation: str
    respect_market_hours: bool
    forced_exits: bool
    entry_triggers: dict[str, str]
    require_card: bool
    card_shim_port: int
    card_ttl_seconds: float
    require_basket: bool
    spectral_var_cap: float | None
    spectral_var_level: float
    spectral_var_horizon: int
    intel_overlay: bool
    intel_refresh_seconds: float
    hedge_symbol: str
    daily_budget: tuple[int, float] | None
    paper_equity: float | None
    paper_costs: dict[str, float]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def fingerprint(self) -> str:
        """Hash of everything that must match between a rehearsal and the session it rehearses."""
        shared = {k: v for k, v in self.to_dict().items() if k not in NECESSARY_DIFFERENCES}
        return hashlib.sha256(json.dumps(shared, sort_keys=True, default=str).encode()).hexdigest()[:16]


def plan_differences(a: SessionPlan, b: SessionPlan) -> dict[str, tuple[Any, Any]]:
    """Every field on which two plans differ."""
    left, right = a.to_dict(), b.to_dict()
    return {k: (left[k], right[k]) for k in left if left[k] != right[k]}


def strategy_or_error(name: str, **params: Any) -> Strategy:
    if name not in STRATEGIES:
        raise SessionError(f"Unknown strategy {name!r}. Known: {sorted(STRATEGIES)}")
    return STRATEGIES[name](**params)


def strategy_map_or_error(text: str) -> dict[str, Strategy]:
    """Parse ``SYM=strategy,SYM=strategy`` into per-symbol strategy instances."""
    out: dict[str, Strategy] = {}
    for pair in (text or "").split(","):
        pair = pair.strip()
        if not pair:
            continue
        if "=" not in pair:
            raise SessionError(f"Bad strategy-map entry {pair!r}. Use SYMBOL=strategy.")
        sym, name = pair.split("=", 1)
        out[sym.strip().upper()] = strategy_or_error(name.strip())
    return out


def resolve_book(settings: Any, venue: str, strategy: str = "", strategy_map: str = "") -> StrategyBook:
    """The strategies a venue trades. Kraken's sleeve carries its own screened parameters."""
    text = strategy_map or settings.strategy_map
    if venue == "kraken" and not strategy and not text:
        smap = {e.symbol: strategy_or_error(e.strategy, **dict(e.params)) for e in CRYPTO_SLEEVE.values()}
        return StrategyBook(default=next(iter(smap.values())), per_symbol=smap)
    default = strategy or ("bollinger" if venue == "ib" else settings.default_strategy)
    return StrategyBook(default=strategy_or_error(default), per_symbol=strategy_map_or_error(text))


def resolve_symbols(settings: Any, venue: str, symbols: str | Sequence[str] | None, book: StrategyBook,
                    futures: Sequence[str] = ()) -> list[str]:
    """The symbols a session watches: those asked for, else the venue's own list, plus map and futures."""
    if isinstance(symbols, str):
        wanted = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    else:
        wanted = [s.strip().upper() for s in (symbols or ())]
    if not wanted:
        wanted = {"kraken": list(CRYPTO_SLEEVE), "ib": list(DEFAULT_IB_SYMBOLS)}.get(venue, settings.symbols_list)
    return list(dict.fromkeys([*wanted, *book.per_symbol, *(s.upper() for s in futures)]))


def plan_session(settings: Any, *, venue: str, mode: RouterMode, symbols: Sequence[str], book: StrategyBook,
                 options: SessionOptions | None = None, rehearsal: bool = False) -> SessionPlan:
    """Resolve every setting for a session. ``rehearsal`` is a money mode on a simulated broker."""
    if venue not in VENUES:
        raise SessionError(f"Unknown venue {venue!r}. One of {VENUES}.")
    o = options or SessionOptions()

    def pick(flag: Any, knob: Any) -> Any:
        return knob if flag is None else flag

    money = mode in ("live", "autonomous") and not rehearsal
    simulated = mode == "paper" or rehearsal
    autonomous = mode == "autonomous"
    policy = monitor_policy(settings)
    interval = settings.autonomous_interval_seconds if autonomous else settings.poll_interval_seconds
    stream = o.stream or ("autonomous" if autonomous else _STREAMS[(venue, money)])
    return SessionPlan(
        venue=venue, mode=mode, money=money, stream=stream,
        strict_ledger=mode in ("live", "autonomous"),
        audit_ledger=True if mode in ("live", "autonomous") else o.audit_ledger,
        symbols=tuple(symbols), strategy=book.default.name,
        strategy_map={s: v.name for s, v in book.per_symbol.items()},
        account_currency="USD" if venue in ("kraken", "ib") else settings.account_currency,
        interval_seconds=int(pick(o.interval, interval)),
        emit_on_change_only=not bool(pick(o.level, False)),
        limits=Router.limits_from(settings), risk_per_trade=settings.risk_pct_per_trade,
        risk_model=policy["risk_model"], heat_aggregation=policy["heat_aggregation"],
        respect_market_hours=settings.respect_market_hours, forced_exits=policy["forced_exits"],
        entry_triggers=dict(policy["entry_triggers"]),
        require_card=bool(pick(o.require_card, settings.require_card)),
        card_shim_port=int(pick(o.card_shim_port, settings.card_shim_port)),
        card_ttl_seconds=float(pick(o.card_ttl, settings.card_ttl_seconds)),
        require_basket=bool(pick(o.require_basket, settings.require_basket)),
        spectral_var_cap=pick(o.spectral_var_cap, settings.spectral_var_cap),
        spectral_var_level=float(pick(o.spectral_var_level, settings.spectral_var_level)),
        spectral_var_horizon=int(pick(o.spectral_var_horizon, settings.spectral_var_horizon)),
        intel_overlay=bool(pick(o.intel_overlay, settings.intel_overlay)),
        intel_refresh_seconds=float(pick(o.intel_refresh, settings.intel_refresh_seconds)),
        hedge_symbol="UUP" if o.hedge else (settings.hedge_symbol if o.hedge is None else ""),
        daily_budget=((settings.autonomous_daily_max_trades, settings.autonomous_daily_max_notional_usd)
                      if autonomous else None),
        paper_equity=float(pick(o.paper_equity, settings.paper_starting_equity)) if simulated else None,
        paper_costs=({**DEFAULT_PAPER_COSTS[venue], **settings.paper_costs.get(venue, {})} if simulated else {}),
    )


# ---------------------------------------------------------------- feed and data

def venue_feed(settings: Any, venue: str, *, money: bool, transport: str = "web",
               futures: Sequence[str] = ()) -> tuple[Broker, Any]:
    """The venue's real adapter, and any keep-alive it needs. It places orders only when ``money``."""
    if venue == "questrade":
        from ..brokers import QuestradeBroker
        if not settings.questrade_refresh_token or not settings.token_encryption_key:
            raise SessionError("QUESTRADE_REFRESH_TOKEN and TOKEN_ENCRYPTION_KEY must be set in .env.")
        return QuestradeBroker.from_settings(refresh_token=settings.questrade_refresh_token,
                                             encryption_key=settings.token_encryption_key,
                                             state_dir=settings.state_dir), None
    if venue == "kraken":
        from ..brokers.kraken import KrakenBroker
        if money and not (settings.kraken_api_key and settings.kraken_api_secret):
            raise SessionError("A money session on Kraken needs KRAKEN_API_KEY and KRAKEN_API_SECRET in .env.")
        return KrakenBroker(api_key=settings.kraken_api_key or "", api_secret=settings.kraken_api_secret or "",
                            enable_live_orders=money), None
    if transport == "web":
        from ..brokers.ib_keepalive import TickleThread
        from ..brokers.ib_web import CPGatewayAuth, IBWebBroker
        auth = CPGatewayAuth(host=settings.ib_web_host, port=settings.ib_web_port,
                             verify_ssl=settings.ib_web_verify_ssl)
        web = IBWebBroker(auth=auth, enable_live_orders=money,
                          auto_confirm_prompts=settings.ib_auto_confirm_prompts)
        for root in futures:
            web.set_sec_type(root.upper(), "FUT")
        return web, TickleThread(web, interval_s=90.0)
    if transport == "socket":
        if futures:
            raise SessionError("--futures is wired only for --transport web (IBWebBroker).")
        from ..brokers.ib import IBBroker
        port = settings.ib_paper_port if settings.ib_use_paper else settings.ib_live_port
        return IBBroker(host=settings.ib_host, port=port, client_id=settings.ib_client_id,
                        account=settings.ib_account or "", enable_live_orders=money,
                        readonly_market_data=not money), None
    raise SessionError(f"Unknown transport {transport!r}.")


def check_money_ready(settings: Any, plan: SessionPlan, feed: Broker, *, expected_env: str | None = None,
                      allow_screened: bool = False, live_confirmation: str | None = None,
                      emit: Emit = _quiet) -> None:
    """What has to be true before a session may place real orders. Fails before anything is opened."""
    if plan.mode == "live":
        if settings.execution_mode != "live":
            raise SessionError("Set EXECUTION_MODE=live in .env before a live session.")
        if (live_confirmation or "").strip() != LIVE_CONFIRM_PHRASE:
            raise SessionError(f'Live mode requires the confirmation phrase exactly: "{LIVE_CONFIRM_PHRASE}"')
    if plan.venue == "questrade":
        # The refresh token alone decides which account is traded, so the setting is checked against it.
        try:
            feed.verify_environment(expected_env or settings.questrade_env)  # type: ignore[attr-defined]
        except BrokerError as e:
            raise SessionError(str(e)) from e
    if plan.venue == "kraken" and not allow_screened:
        screened = [s for s in plan.symbols if s in CRYPTO_SLEEVE and CRYPTO_SLEEVE[s].tier == "screened"]
        if screened:
            raise SessionError(f"{len(screened)} Kraken symbols are tier=screened: in-sample only, not walk-forward "
                               f"validated and marked paper-only ({', '.join(screened)}). Pass --allow-screened "
                               "to trade them with real money anyway.")
    if plan.venue == "ib" and settings.ib_use_paper:
        emit("IB_USE_PAPER is on: a socket session goes to IB's own paper account.")


# ---------------------------------------------------------------- intel

def intel_overlay_hooks(settings: Any, refresh_seconds: float = 900.0,
                        class_overrides: Mapping[str, Any] | None = None, *,
                        background: bool = True, emit: Emit = _quiet) -> tuple[Any, Any]:
    """``(overlay_for, interpret_for)`` over one WorldMonitor provider; ``(None, None)`` without a key.

    Both only de-risk new entries and neither touches exits. The overlay trims; at the default floor
    (0.25) its halt threshold (0.20) cannot be reached, so it does not stand a class down.

    An outage fails open and can loosen the trim. When every vendor call fails the snapshot is
    degraded and its class scalars rest on no readings, so 0.25 under stress becomes the 0.80 cap.
    The last good read is kept only when the fetch itself raises, and there is no overlay before the
    first read.

    With ``background`` the provider reads once now and then refreshes on its own thread, so a slow
    vendor cannot hold up a poll and the exits in it.
    """
    if not settings.worldmonitor_api_key:
        return None, None
    from ..intel.interpret import interpret
    from ..intel.worldmonitor import WorldMonitorClient

    def _snapshot() -> Any:
        async def _f() -> Any:
            async with WorldMonitorClient(settings.worldmonitor_api_key) as wm:
                return await wm.snapshot()
        return asyncio.run(_f())

    provider = OverlayProvider(_snapshot, refresh_seconds=float(refresh_seconds), class_overrides=class_overrides)
    if background:
        provider.start_background()

    # Same snapshot as the overlay decided on, no second fetch. Before the first read this is empty,
    # so the bias is a no-op.
    def _interpret_current() -> list[Any]:
        snap = provider.last_snapshot
        return interpret(snap) if snap is not None else []

    emit(f"intel overlay ON (refresh every {refresh_seconds:.0f}s; trims entry size).")
    emit("interpret entry-filter ON (theses trim conviction; advisory, never blocks or boosts).")
    return provider, _interpret_current


def _print_bias(bias: Mapping[str, float], emit: Emit) -> None:
    emit("allocator conviction bias (baseline = 1.0):")
    for sym in sorted(bias, key=lambda k: -bias[k]):
        label = "boost" if bias[sym] > 1.05 else "trim" if bias[sym] < 0.95 else "neutral"
        emit(f"    {sym:>12}  x{bias[sym]:.2f}  ({label})")


def allocator_weight_bias(market: Any, symbols: Sequence[str], *, emit: Emit = _quiet) -> Any:
    """Correlation-aware per-symbol size bias for the equity pool, or ``None`` when it cannot be computed.

    Computes bias from OOS score + return covariance, so a redundant name (bank ETFs correlated with bank
    ETFs, sector ETFs with their names) gets its slot cut and a low-correlation name gets it lifted. The
    monitor bounds it to [0.1, 3.0]; equal-weight is neutral (1.0). Computed once at startup from the local
    candle cache. Symbols not in WALK_FORWARD_VALIDATED stay neutral.
    """
    try:
        from ..analysis.universe import WALK_FORWARD_VALIDATED as wfv
        from ..portfolio.allocator import PortfolioAllocator
        scores = {s: max(wfv[s].oos_score, 0.0) for s in symbols if s in wfv}
        returns = {}
        for s in scores:
            try:
                df = market.recent(s, bars=252, interval="1d")
                if not df.empty and "close" in df.columns:
                    returns[s] = df["close"].pct_change().dropna()
            except Exception as e:
                log.warning("session.allocator.fetch_failed", symbol=s, error=str(e))
        if not returns:
            return None
        res = PortfolioAllocator(max_weight=0.20, max_sleeve_weight=1.0, min_score=0.0).allocate(
            returns, scores, regime_scalar=1.0)
        eqw = 1.0 / len(returns)
        bias = {s: res.weights.get(s, 0.0) / eqw for s in returns}
        _print_bias(bias, emit)
        return lambda sym, _m=bias: _m.get(sym, 1.0)
    except Exception as e:
        emit(f"allocator bias OFF: {e}")
        return None


def _ib_weight_bias(market: Any, symbols: Sequence[str], overlay_for: Any, *, emit: Emit = _quiet) -> Any:
    """IB's bias: a Sharpe proxy over two years of daily returns, with government bonds as hedges only."""
    import numpy as np

    from ..portfolio.allocator import PortfolioAllocator
    returns: dict[str, Any] = {}
    scores: dict[str, float] = {}
    for sym in symbols:
        try:
            df = market.history(sym, years=2.0, interval="1d")
            if df is None or len(df) < 60:
                continue
            rets = df["close"].pct_change().dropna()
            returns[sym] = rets
            vol = float(rets.std(ddof=0) * np.sqrt(252.0)) or 1e-9
            scores[sym] = max(0.0, float(rets.mean() * 252.0) / vol)
        except Exception as e:
            emit(f"allocator: {sym} fetch failed ({e}); bias defaults to 1.0")
    bias = {s: 1.0 for s in symbols}
    if returns:
        result = PortfolioAllocator(max_weight=0.30, max_sleeve_weight=1.0, min_score=0.0).allocate(
            returns, scores, regime_scalar=1.0)
        if result.weights:
            bias.update({s: result.weights.get(s, 0.0) * len(returns) for s in symbols})
    _print_bias(bias, emit)

    def _for(symbol: str) -> float:
        if symbol.upper() in _GOV_BONDS:
            if overlay_for is not None:
                try:
                    decision = overlay_for("SPY")            # SPY as the equity-class proxy
                    if decision is not None and decision.scalar <= _HEDGE_TRIGGER:
                        return _HEDGE_BIAS
                except Exception:
                    return 0.0
            return 0.0                                        # dormant otherwise
        return bias.get(symbol, 1.0)
    return _for


def venue_weight_bias(venue: str, market: Any, symbols: Sequence[str], overlay_for: Any, *,
                      emit: Emit = _quiet) -> Any:
    """Each venue's allocator. Same function for paper and money, so a practised bias is the real one."""
    if venue == "kraken":
        from ..portfolio.sleeve_bias import sleeve_conviction_bias
        bias = sleeve_conviction_bias(CRYPTO_SLEEVE)
        _print_bias(bias, emit)
        return lambda sym, _m=bias: _m.get(sym, 1.0)
    if venue == "ib":
        return _ib_weight_bias(market, symbols, overlay_for, emit=emit)
    return allocator_weight_bias(market, symbols, emit=emit)


# ---------------------------------------------------------------- alerts

def build_alerter(settings: Any) -> Alerter:
    return Alerter(AlertConfig(telegram_bot_token=settings.telegram_bot_token,
                               telegram_chat_id=settings.telegram_chat_id, smtp_host=settings.smtp_host,
                               smtp_user=settings.smtp_user, smtp_pass=settings.smtp_pass,
                               email_to=settings.alert_email_to))


def alert_emitter(alerter: Alerter, book: StrategyBook, *, label: str = "",
                  emit: Emit = _quiet) -> Callable[[MonitorEvent], None]:
    """The ``on_event`` for a monitor: echo each event, then format and send entries, exits and hedges."""
    from ..intel.notification import format_entry, format_exit, format_hedge

    def _on_event(ev: MonitorEvent) -> None:
        state = "NEW" if ev.is_transition else f"persisting ({ev.poll_count})"
        emit(f"{label} {ev.kind.upper()} {ev.symbol} @ {ev.price:.4f} ({state}) detail={ev.detail}".strip())
        message: tuple[str, str] | None = None
        if ev.kind == "hedge":
            message = format_hedge(symbol=ev.symbol, detail=ev.detail)
        elif ev.kind == "entry":
            # wf_record says whether this symbol cleared walk-forward; without it the alert says so.
            message = format_entry(strategy_name=book.name_for(ev.symbol), symbol=ev.symbol, price=ev.price,
                                   detail=ev.detail, is_transition=ev.is_transition,
                                   poll_count=ev.poll_count, wf_record=validated_for(ev.symbol))
        elif ev.kind == "exit":
            shares = int(ev.detail.get("shares", 0)) if isinstance(ev.detail, dict) else 0
            message = format_exit(strategy_name=book.name_for(ev.symbol), symbol=ev.symbol,
                                  price=ev.price, shares=shares)
        if message is not None:
            # The label says which session this is: a phone alert must show whether real money moved.
            alerter.send(f"{label} {message[0]}".strip(), message[1])
    return _on_event


# ---------------------------------------------------------------- the session

@dataclass
class Session:
    """A built session, ready to run."""

    plan: SessionPlan
    monitor: LiveMonitor
    router: Any
    exec_broker: Broker
    market: MarketData
    ledger: Ledger | None
    feed: Broker
    keepalive: Any = None

    def run(self, iterations: int | None = None) -> None:
        if self.keepalive is not None:
            self.keepalive.start()
        try:
            self.monitor.run_forever(max_iterations=iterations or None)
        finally:
            if self.keepalive is not None:
                self.keepalive.stop()
            if hasattr(self.feed, "close"):
                self.feed.close()


def _open_ledger(settings: Any, plan: SessionPlan, session_id: str | None) -> Ledger | None:
    if not plan.audit_ledger:
        return None
    led = Ledger(Path(settings.state_dir) / "ledger", stream=plan.stream, mode=plan.mode, session_id=session_id,
                 strict=plan.strict_ledger)
    led.append("SESSION_STARTED", session_provenance(argv=sys.argv))
    led.append("SESSION_PLAN", {"fingerprint": plan.fingerprint(), "plan": plan.to_dict()})
    atexit.register(lambda: led.append("SESSION_STOPPED", {"argv0": sys.argv[0]}))
    return led


def build_session(settings: Any, plan: SessionPlan, *, feed: Broker, book: StrategyBook,
                  account_number: str | None = None, live_confirmation: str | None = None,
                  expected_env: str | None = None, allow_screened: bool = False,
                  class_overrides: Mapping[str, Any] | None = None, keepalive: Any = None,
                  label: str = "", emit: Emit = _quiet) -> Session:
    """Assemble a session from its plan, one engine layer at a time."""
    state_dir = Path(settings.state_dir)
    symbols = list(plan.symbols)
    if plan.require_card and plan.mode == "autonomous":
        raise SessionError("Autonomous mode cannot be wrapped by card approval: no human is in that loop. "
                           "Turn require_card off for the daemon, or run `trading live`.")
    if plan.money:
        check_money_ready(settings, plan, feed, expected_env=expected_env, allow_screened=allow_screened,
                          live_confirmation=live_confirmation, emit=emit)

    # Execution. The one thing that makes a session paper: its broker simulates fills against the feed.
    # Everything downstream is the same object graph.
    exec_broker: Broker = feed
    if plan.paper_equity is not None:
        costs = plan.paper_costs
        exec_broker = PaperBroker(feed=feed, starting_equity=plan.paper_equity, journal_dir=state_dir,
                                  slippage_bps=costs["slippage_bps"],
                                  commission_per_trade=costs["commission_per_trade"],
                                  commission_bps=costs["commission_bps"], cross_spread=True)
        emit(f"{label} PAPER. session={exec_broker.session_id} equity={plan.paper_equity:,.0f}; "  # type: ignore[attr-defined]
             "fills are simulated, the real account is untouched.")

    # Data.
    preflight(feed, symbols, emit=emit)
    mixed = [s for s in symbols if instrument_currency(s) not in (None, plan.account_currency)]
    if mixed:
        emit(f"WARNING: {', '.join(mixed)} not priced in the account's {plan.account_currency}. The gates compare "
             "notional in the listing currency with equity in the account's, unconverted. A paper account is "
             "single-currency and will not show this.")
    market = MarketData(exec_broker, cache=CandleCache(settings.data_cache_dir))

    # Audit, before anything can trade.
    ledger = _open_ledger(settings, plan, getattr(exec_broker, "session_id", None))

    # Risk gates.
    budget: dict[str, Any] = {}
    if plan.daily_budget is not None:
        budget = {"daily_max_trades": plan.daily_budget[0], "daily_max_notional_usd": plan.daily_budget[1]}
    try:
        router: Any = Router.build_default(mode=plan.mode, broker=exec_broker, state_dir=state_dir,  # type: ignore[arg-type]
                                           **plan.limits, **budget, live_confirmation=live_confirmation,
                                           ledger=ledger)
    except (LiveModeNotConfirmed, AutonomousNotEnabled) as e:
        raise SessionError(str(e)) from e
    if attach_basket_gate(router, plan.venue, state_dir, enabled=plan.require_basket):
        emit(f"basket gate ON: entries need a card-signed {plan.venue} basket.")
    spectral = attach_spectral_gate(router, feed, cap_pct=plan.spectral_var_cap, level=plan.spectral_var_level,
                                    horizon_days=plan.spectral_var_horizon)
    if spectral is not None:
        emit(f"spectral VaR gate ON: {spectral.thresholds()}")
    if plan.require_card:
        engine = VSInvestmentEngine()
        wiring = wire_card_approval(
            router, shim_port=plan.card_shim_port, ttl_seconds=plan.card_ttl_seconds,
            thesis_fn=lambda intent, venue: engine.explain(intent, broker=venue, market=MarketContext()),
            db_path=state_dir / "approval.db", state_dir=state_dir,
            session_id=getattr(exec_broker, "session_id", None) or getattr(ledger, "session_id", None))
        router = wiring.router
        emit(f"require-card ON: approval shim at {wiring.shim_url}. Card auth token: {wiring.auth_token}")
        emit("Pair the card by passing the token as Authorization: Bearer <token> on every request.")
        emit(f"desk panel at {wiring.desk_url} (build it first: python scripts/build_desk_dash.py).")

    # Intel.
    overlay_for, interpret_for = (None, None)
    if plan.intel_overlay:
        overlay_for, interpret_for = intel_overlay_hooks(settings, plan.intel_refresh_seconds, class_overrides,
                                                         emit=emit)
    if overlay_for is None:
        emit("intel overlay OFF " + ("(no WORLDMONITOR_API_KEY)." if plan.intel_overlay else "(turned off)."))
    weight_bias = venue_weight_bias(plan.venue, market, symbols, overlay_for, emit=emit)

    # Alerts.
    alerter = build_alerter(settings)
    if keepalive is not None:
        keepalive.warn_fn = lambda level, msg: alerter.send(f"[{plan.stream}] {level}", msg)

    # The account the session trades.
    if plan.paper_equity is not None:
        account = exec_broker.accounts()[0].number
    else:
        account = account_number or (settings.questrade_account_number if plan.venue == "questrade" else "")
        if not account:
            accounts = feed.accounts()
            if not accounts:
                raise SessionError(f"No accounts on the {plan.venue} login.")
            account = accounts[0].number

    # Strategy, sizing and the loop. The only LiveMonitor a runner ever gets.
    monitor = LiveMonitor(
        broker=exec_broker, market=market, strategy=book.default, sizer=PositionSizer(risk_pct=plan.risk_per_trade),
        router=router, account_number=account, symbols=symbols, interval_seconds=plan.interval_seconds,
        on_event=alert_emitter(alerter, book, label=label, emit=emit), account_currency=plan.account_currency,
        emit_on_change_only=plan.emit_on_change_only, strategy_map=dict(book.per_symbol) or None,
        hedge_symbol=plan.hedge_symbol or None, overlay_for=overlay_for, interpret_for=interpret_for,
        weight_bias_for=weight_bias, persistence_for=PersistenceGate(class_overrides=class_overrides),
        **monitor_policy(settings),
    )
    emit(f"{label} {plan.mode} on {plan.venue}: {len(symbols)} symbols, plan {plan.fingerprint()}.")
    for sym in symbols:
        emit(f"    {sym:>12} <- {book.name_for(sym)}")
    if ledger is not None:
        emit(f"audit ledger ON: {ledger.dir} stream {plan.stream}")
    return Session(plan=plan, monitor=monitor, router=router, exec_broker=exec_broker, market=market,
                   ledger=ledger, feed=feed, keepalive=keepalive)


def start_session(settings: Any, *, venue: str, mode: RouterMode, symbols: str | Sequence[str] | None = None,
                  strategy: str = "", strategy_map: str = "", futures: Sequence[str] = (), transport: str = "web",
                  options: SessionOptions | None = None, account_number: str | None = None,
                  live_confirmation: str | None = None, expected_env: str | None = None,
                  rehearsal: bool = False, label: str = "", emit: Emit = _quiet) -> Session:
    """Everything a runner does, from its arguments to a session ready to run."""
    o = options or SessionOptions()
    book = resolve_book(settings, venue, strategy, strategy_map)
    plan = plan_session(settings, venue=venue, mode=mode, symbols=resolve_symbols(settings, venue, symbols, book, futures),
                        book=book, options=o, rehearsal=rehearsal)
    feed, keepalive = venue_feed(settings, venue, money=plan.money, transport=transport, futures=futures)
    return build_session(settings, plan, feed=feed, book=book, account_number=account_number,
                         live_confirmation=live_confirmation, expected_env=expected_env,
                         allow_screened=o.allow_screened,
                         class_overrides=futures_overrides(list(futures)) if futures else None,
                         keepalive=keepalive, label=label or f"{venue}-{mode}", emit=emit)


def add_session_args(ap: argparse.ArgumentParser) -> None:
    """The flags every script-launched session takes. Each overrides one ``trading.yaml`` knob for the run."""
    bool_flag = argparse.BooleanOptionalAction
    ap.add_argument("--interval", type=int, default=None, help="Poll interval, seconds.")
    ap.add_argument("--paper-equity", type=float, default=None,
                    help="Starting equity. Use the money you plan to trade, or sizes will not translate.")
    ap.add_argument("--iterations", type=int, default=0, help="0 = run forever; N runs that many polls.")
    ap.add_argument("--intel-overlay", dest="intel_overlay", action=bool_flag, default=None,
                    help="The OSINT overlay and thesis trims. On whenever WORLDMONITOR_API_KEY is set.")
    ap.add_argument("--require-card", dest="require_card", action=bool_flag, default=None,
                    help="A TradeCard (or scripts/approval_card_sim.py) must ACCEPT every order.")
    ap.add_argument("--card-shim-port", type=int, default=None)
    ap.add_argument("--card-ttl", type=float, default=None, help="Seconds a card prompt stays live.")
    ap.add_argument("--require-basket", dest="require_basket", action=bool_flag, default=None,
                    help="Entries need a card-signed basket for this venue. Exits are never gated.")
    ap.add_argument("--audit-ledger", dest="audit_ledger", action=bool_flag, default=True,
                    help="Record every signal, gate decision, card verdict and order to state/ledger.")
    ap.add_argument("--spectral-var-cap", dest="spectral_var_cap", type=float, default=None,
                    help="Spectral VaR gate: an ENTRY whose VaR exceeds this fraction of equity is trimmed "
                         "or rejected, e.g. 0.005 = 0.5%%. Exits are never gated.")
    ap.add_argument("--spectral-var-level", dest="spectral_var_level", type=float, default=None)
    ap.add_argument("--spectral-var-horizon", dest="spectral_var_horizon", type=int, default=None,
                    help="VaR horizon in trading days.")


def options_from_args(args: argparse.Namespace, *, level: bool | None = None) -> SessionOptions:
    return SessionOptions(interval=args.interval, level=level, intel_overlay=args.intel_overlay,
                          require_card=args.require_card, card_shim_port=args.card_shim_port, card_ttl=args.card_ttl,
                          require_basket=args.require_basket, spectral_var_cap=args.spectral_var_cap,
                          spectral_var_level=args.spectral_var_level, spectral_var_horizon=args.spectral_var_horizon,
                          paper_equity=args.paper_equity, audit_ledger=args.audit_ledger)


__all__ = [
    "DEFAULT_PAPER_COSTS",
    "NECESSARY_DIFFERENCES",
    "VENUES",
    "Session",
    "SessionError",
    "SessionOptions",
    "SessionPlan",
    "StrategyBook",
    "add_session_args",
    "alert_emitter",
    "allocator_weight_bias",
    "build_alerter",
    "build_session",
    "check_money_ready",
    "intel_overlay_hooks",
    "options_from_args",
    "plan_differences",
    "plan_session",
    "resolve_book",
    "resolve_symbols",
    "start_session",
    "strategy_map_or_error",
    "strategy_or_error",
    "venue_feed",
    "venue_weight_bias",
]
