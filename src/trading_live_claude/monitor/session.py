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
import contextlib
import hashlib
import json
import signal
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

from ..analysis.params import PARAMS_MODES, build_strategy, running_params
from ..analysis.symbol_validation import preflight
from ..analysis.universe import CRYPTO_SLEEVE, validated_for
from ..audit.ledger import Ledger
from ..audit.versioning import session_provenance
from ..brokers.base import Broker, BrokerError
from ..brokers.fresh import guard_feed
from ..brokers.fx import BankOfCanadaRates, CurrencyNormalizingBroker
from ..brokers.paper import PaperBroker
from ..data.cache import CandleCache
from ..data.market import MarketData
from ..desk_policy import VenuePolicyError, assert_ib_futures_only, assert_ib_no_questrade_equities
from ..execution.approval import wire_card_approval
from ..execution.approval_asgi import BookRef
from ..execution.basket import attach_basket_gate
from ..execution.router import (
    LIVE_CONFIRM_PHRASE,
    AutonomousNotEnabled,
    LiveModeNotConfirmed,
    Router,
    RouterMode,
)
from ..execution.venue_costs import VenueCostModel
from ..intel.apply import OverlaidBias
from ..intel.routing import OverlayProvider, PersistenceGate, futures_overrides
from ..intel.vs_engine import MarketContext, VSInvestmentEngine
from ..logging_setup import get_logger
from ..risk.position_cap import position_cap_for
from ..risk.sizing import PositionSizer
from ..risk.sizing_policy import SizingPolicy, calendar_days
from ..risk.spectral_var import attach_spectral_gate
from ..signals.candle_exit import CandleExit
from ..signals.overbought_exit import OverboughtExit
from ..signals.oversold_entry import OversoldEntry
from ..signals.profit_lock import ProfitLock
from ..strategies import STRATEGIES
from ..strategies.base import Strategy
from ..venues import currency_of
from .alerter import AlertConfig, Alerter
from .live_loop import LiveMonitor, MonitorEvent, monitor_policy

log = get_logger(__name__)

Venue = Literal["questrade", "kraken", "ib"]
VENUES: tuple[Venue, ...] = ("questrade", "kraken", "ib")
Emit = Callable[[str], object]

# What may differ between a paper plan and its money counterpart, and why: the mode and the fact of real
# money; a ledger stream of its own, strict because a money order must not go unrecorded; the simulated
# account's balance and costs, which a real account has for itself; closing the book on stop, because a
# money session must never liquidate itself; and resuming, because a real book lives at the broker.
NECESSARY_DIFFERENCES = frozenset({"mode", "money", "stream", "strict_ledger", "paper_equity", "paper_costs",
                                   "flatten_on_exit", "resume_session"})

# Paper's costs per venue: each venue's own fee shape, execution/venue_costs.py. The slippage on top of
# crossing the spread is still an assumption, to be replaced by what `trading audit query fill-quality`
# measures. Override any field with `paper_costs` in trading.yaml.
DEFAULT_PAPER_COSTS: dict[str, dict[str, Any]] = {v: asdict(VenueCostModel.for_venue(v)) for v in VENUES}
_BASKET_VENUES = {"questrade": "qt", "kraken": "kraken", "ib": "ib"}
_ASSET_CLASSES = {"questrade": "equity", "kraken": "crypto", "ib": "multi"}
# The currency a venue's own equity figure is in, where it cannot be asked for the home currency.
_NATIVE_EQUITY = {"kraken": "USD", "ib": "USD"}
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
    card_shim_host: str | None = None
    card_attach: bool | None = None
    card_ttl: float | None = None
    require_basket: bool | None = None
    flatten_on_exit: bool | None = None
    warmup_interval: int | None = None
    warmup_minutes: float | None = None
    profit_lock: bool | None = None
    profit_lock_exempt: str | None = None
    candle_exit: bool | None = None
    candle_exit_exempt: str | None = None
    overbought_exit: bool | None = None
    overbought_exit_exempt: str | None = None
    oversold_entry: bool | None = None
    oversold_entry_only: str | None = None
    parallel_sizing: bool | None = None
    forward_read_shadow: str | None = None
    mute_alerts: str | None = None
    max_cost_ratio: float | None = None
    max_positions: int | None = None
    params: str | None = None
    sizing_v2: bool | None = None
    account_currency: str | None = None
    spectral_var_cap: float | None = None
    spectral_var_level: float | None = None
    spectral_var_horizon: int | None = None
    paper_equity: float | None = None
    audit_ledger: bool = True
    allow_screened: bool = False
    stream: str | None = None
    # One-shots on a simulated book. They run once at startup and leave no setting behind, so they are
    # not part of what is practised.
    resume_session: str = ""
    close_symbols: str = ""
    trim_to_slots: bool = False


@dataclass(frozen=True)
class StrategyBook:
    """The strategy each symbol trades, and the fallback for a symbol not named."""

    default: Strategy
    per_symbol: Mapping[str, Strategy] = field(default_factory=dict)

    def strategy_for(self, symbol: str) -> Strategy:
        return self.per_symbol.get(symbol, self.default)

    def name_for(self, symbol: str) -> str:
        return self.strategy_for(symbol).name


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
    params: str
    strategy_params: dict[str, dict[str, Any]]
    account_currency: str
    interval_seconds: int
    allocator_refresh_seconds: float
    allocator_bias_range: tuple[float, float]
    persistence: dict[str, Any]
    warmup_interval_seconds: int
    warmup_minutes: float
    emit_on_change_only: bool
    limits: dict[str, Any]
    position_cap: dict[str, Any]
    stale_quote: dict[str, Any]
    risk_per_trade: float
    risk_model: str
    heat_aggregation: str
    respect_market_hours: bool
    forced_exits: bool
    entry_triggers: dict[str, str]
    profit_lock: bool
    profit_lock_exempt: tuple[str, ...]
    candle_exit: bool
    candle_exit_exempt: tuple[str, ...]
    overbought_exit: bool
    overbought_exit_exempt: tuple[str, ...]
    oversold_entry: bool
    oversold_entry_only: tuple[str, ...]
    parallel_sizing: bool
    forward_read_shadow: str               # forward-read table path, logged in shadow; "" = off
    sizing_v2: bool
    mute_alerts: tuple[str, ...]
    require_card: bool
    card_shim_port: int
    card_shim_host: str
    card_attach: bool
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
    paper_costs: dict[str, Any]
    flatten_on_exit: bool
    resume_session: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def fingerprint(self) -> str:
        """Hash of everything that must match between a rehearsal and the session it rehearses."""
        shared = {k: v for k, v in self.to_dict().items() if k not in NECESSARY_DIFFERENCES}
        return hashlib.sha256(json.dumps(shared, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _names(text: str | None, *, upper: bool = False) -> tuple[str, ...]:
    """A comma-separated list as a sorted tuple, so two plans compare equal however it was typed."""
    parts = {s.strip().upper() if upper else s.strip() for s in (text or "").split(",")}
    return tuple(sorted(p for p in parts if p))


def paper_costs_for(settings: Any, venue: str) -> dict[str, Any]:
    """The simulated account's costs: the venue's own fee shape, then any ``paper_costs`` override."""
    return {**DEFAULT_PAPER_COSTS[venue], **settings.paper_costs.get(venue, {})}


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


def book_with_params(book: StrategyBook, symbols: Sequence[str], mode: str, *, emit: Emit = _quiet) -> StrategyBook:
    """The book with each symbol's parameters taken from ``mode``, printing what actually runs.

    ``default`` leaves it alone: class defaults, and the sleeve's screened parameters on Kraken.
    """
    if mode not in PARAMS_MODES:
        raise SessionError(f"params must be one of {', '.join(PARAMS_MODES)}, not {mode!r}.")
    if mode == "default":
        return book
    resolved: dict[str, Strategy] = {}
    for sym in symbols:
        name = book.name_for(sym)
        built = build_strategy(name, sym, mode)
        resolved[sym] = built.strategy  # type: ignore[assignment]
        note = f"  [{built.note}]" if built.note else ""
        emit(f"params {sym}: {name} <- {built.source} {running_params(built.strategy)}{note}")
    return replace(book, per_symbol=resolved)


def resolve_symbols(settings: Any, venue: str, symbols: str | Sequence[str] | None, book: StrategyBook,
                    futures: Sequence[str] = ()) -> list[str]:
    """The symbols a session watches: those asked for, else the venue's own list, plus map and futures."""
    if isinstance(symbols, str):
        wanted = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    else:
        wanted = [s.strip().upper() for s in (symbols or ())]
    if not wanted and not futures:     # a futures run names its whole book
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
    limits = Router.limits_from(settings)
    if o.max_positions:
        limits["max_open_positions"] = o.max_positions
    if o.max_cost_ratio is not None:
        limits["max_round_trip_cost_ratio"] = o.max_cost_ratio
    require_card = bool(pick(o.require_card, settings.require_card))
    basket = pick(o.require_basket, settings.require_basket)
    return SessionPlan(
        venue=venue, mode=mode, money=money, stream=stream,
        strict_ledger=mode in ("live", "autonomous"),
        audit_ledger=True if mode in ("live", "autonomous") else o.audit_ledger,
        symbols=tuple(symbols), strategy=book.default.name,
        strategy_map={s: v.name for s, v in book.per_symbol.items()},
        params=str(pick(o.params, settings.params)),
        strategy_params={s: running_params(book.strategy_for(s)) for s in symbols},
        account_currency=o.account_currency or settings.account_currency,
        interval_seconds=int(pick(o.interval, interval)),
        allocator_refresh_seconds=float(settings.allocator_refresh_seconds),
        allocator_bias_range=(float(settings.allocator_bias_floor), float(settings.allocator_bias_cap)),
        persistence={"min_polls": settings.persistence_min_polls, "max_age_hours": settings.persistence_max_age_hours},
        warmup_interval_seconds=int(pick(o.warmup_interval, settings.warmup_interval_seconds)),
        warmup_minutes=float(pick(o.warmup_minutes, settings.warmup_minutes)),
        emit_on_change_only=not bool(pick(o.level, False)),
        limits=limits, risk_per_trade=settings.risk_pct_per_trade,
        position_cap={"mode": settings.position_cap_mode, "ref_vol": settings.position_cap_ref_vol,
                      "floor_pct": settings.position_cap_floor_pct, "ceiling_pct": settings.position_cap_ceiling_pct},
        stale_quote={"frozen_s": settings.stale_quote_frozen_s, "max_trade_age_s": settings.stale_quote_max_trade_age_s,
                     "on_stale": settings.on_stale_quote},
        risk_model=policy["risk_model"], heat_aggregation=policy["heat_aggregation"],
        respect_market_hours=settings.respect_market_hours, forced_exits=policy["forced_exits"],
        entry_triggers=dict(policy["entry_triggers"]),
        profit_lock=bool(pick(o.profit_lock, settings.profit_lock)),
        profit_lock_exempt=_names(pick(o.profit_lock_exempt, settings.profit_lock_exempt)),
        candle_exit=bool(pick(o.candle_exit, settings.candle_exit)),
        candle_exit_exempt=_names(pick(o.candle_exit_exempt, settings.candle_exit_exempt)),
        overbought_exit=bool(pick(o.overbought_exit, settings.overbought_exit)),
        overbought_exit_exempt=_names(pick(o.overbought_exit_exempt, settings.overbought_exit_exempt)),
        oversold_entry=bool(pick(o.oversold_entry, settings.oversold_entry)),
        oversold_entry_only=_names(pick(o.oversold_entry_only, settings.oversold_entry_only)),
        parallel_sizing=bool(pick(o.parallel_sizing, settings.parallel_sizing)),
        forward_read_shadow=str(pick(o.forward_read_shadow, settings.forward_read_shadow) or ""),
        sizing_v2=bool(pick(o.sizing_v2, settings.sizing_v2)),
        mute_alerts=_names(pick(o.mute_alerts, settings.mute_alerts), upper=True),
        require_card=require_card,
        card_shim_port=int(pick(o.card_shim_port, settings.card_shim_port)),
        card_shim_host=str(pick(o.card_shim_host, settings.card_shim_host)),
        card_attach=bool(pick(o.card_attach, settings.card_attach)),
        card_ttl_seconds=float(pick(o.card_ttl, settings.card_ttl_seconds)),
        require_basket=require_card if basket is None else bool(basket),
        spectral_var_cap=pick(o.spectral_var_cap, settings.spectral_var_cap),
        spectral_var_level=float(pick(o.spectral_var_level, settings.spectral_var_level)),
        spectral_var_horizon=int(pick(o.spectral_var_horizon, settings.spectral_var_horizon)),
        intel_overlay=bool(pick(o.intel_overlay, settings.intel_overlay)),
        intel_refresh_seconds=float(pick(o.intel_refresh, settings.intel_refresh_seconds)),
        hedge_symbol="UUP" if o.hedge else (settings.hedge_symbol if o.hedge is None else ""),
        daily_budget=((settings.autonomous_daily_max_trades, settings.autonomous_daily_max_notional_usd)
                      if autonomous else None),
        paper_equity=float(pick(o.paper_equity, settings.paper_starting_equity)) if simulated else None,
        paper_costs=paper_costs_for(settings, venue) if simulated else {},
        flatten_on_exit=mode == "paper" and bool(pick(o.flatten_on_exit, settings.flatten_on_exit)),
        resume_session=o.resume_session if simulated else "",
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
    if transport == "connector":
        if money:
            raise SessionError("--transport connector is read-only: it feeds paper sessions and places no orders.")
        from ..brokers.base import TokenExpired
        from ..brokers.ib_connector import IBConnectorBroker
        if not settings.token_encryption_key:
            raise SessionError("TOKEN_ENCRYPTION_KEY must be set in .env.")
        try:
            connector = IBConnectorBroker.from_state(Path(settings.state_dir), settings.token_encryption_key)
        except TokenExpired as e:
            raise SessionError(str(e)) from e
        for root in futures:
            connector.set_sec_type(root.upper(), "FUT")
        return connector, None
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


def allocator_weight_bias(market: Any, symbols: Sequence[str], overlay_for: Any = None, *,
                          emit: Emit = _quiet) -> Any:
    """Correlation-aware per-symbol size bias for the equity pool, or ``None`` when it cannot be computed.

    Computes bias from OOS score + return covariance, so a redundant name (bank ETFs correlated with bank
    ETFs, sector ETFs with their names) gets its slot cut and a low-correlation name gets it lifted. The
    monitor bounds it to [0.1, 3.0]; equal-weight is neutral (1.0). Computed once at startup from the local
    candle cache. Symbols not in WALK_FORWARD_VALIDATED stay neutral. With an overlay, its class scalar is
    applied to the allocation through ``apply_overlay``, and follows each refresh.
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
        if overlay_for is not None:
            emit("live overlay applied to the allocation through apply_overlay.")
            return OverlaidBias(res, eqw, symbols, overlay_for)
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
        from ..portfolio.sleeve_bias import sleeve_allocation
        bias, allocation, equal_weight = sleeve_allocation(CRYPTO_SLEEVE)
        _print_bias(bias, emit)
        if overlay_for is not None and allocation is not None:
            emit("live overlay applied to the allocation through apply_overlay.")
            return OverlaidBias(allocation, equal_weight, bias, overlay_for)
        return lambda sym, _m=bias: _m.get(sym, 1.0)
    if venue == "ib":
        return _ib_weight_bias(market, symbols, overlay_for, emit=emit)
    return allocator_weight_bias(market, symbols, overlay_for, emit=emit)


class RefreshingBias:
    """A venue's allocator bias, recomputed on a cadence so a long session's sizing follows the book.

    ``LiveMonitor`` calls ``maybe_refresh`` between polls. A recompute that fails or finds nothing keeps
    the last good weights, and each new set goes to the ledger, so an order's size can be traced to it.
    """

    def __init__(self, compute: Callable[[], Any], initial: Any, refresh_seconds: float, *, symbols: Sequence[str],
                 ledger: Ledger | None = None, clock: Callable[[], float] = time.monotonic) -> None:
        self._compute, self._current, self._every = compute, initial, float(refresh_seconds)
        self._symbols, self._ledger, self._clock = list(symbols), ledger, clock
        self._at = clock()

    @property
    def applies_overlay(self) -> bool:
        return bool(getattr(self._current, "applies_overlay", False))

    def __call__(self, symbol: str) -> float:
        return float(self._current(symbol)) if self._current is not None else 1.0

    def maybe_refresh(self) -> None:
        if self._clock() - self._at < self._every:
            return
        self._at = self._clock()
        try:
            fresh = self._compute()
        except Exception as e:
            log.warning("session.allocator.refresh_failed", error=str(e))
            return
        if fresh is None:
            log.warning("session.allocator.refresh_empty")
            return
        self._current = fresh
        weights = {s: round(self(s), 4) for s in self._symbols}
        log.info("session.allocator.refreshed", weights=weights)
        if self._ledger is not None:
            self._ledger.append("ALLOCATION_REFRESHED", {"weights": weights})


# ---------------------------------------------------------------- alerts

def build_alerter(settings: Any) -> Alerter:
    return Alerter(AlertConfig(telegram_bot_token=settings.telegram_bot_token,
                               telegram_chat_id=settings.telegram_chat_id, smtp_host=settings.smtp_host,
                               smtp_user=settings.smtp_user, smtp_pass=settings.smtp_pass,
                               email_to=settings.alert_email_to))


def alert_emitter(alerter: Alerter, book: StrategyBook, *, label: str = "", currency: str = "",
                  emit: Emit = _quiet) -> Callable[[MonitorEvent], None]:
    """The ``on_event`` for a monitor: echo each event, then format and send entries, exits and hedges."""
    from ..intel.notification import format_entry, format_exit, format_hedge

    def _on_event(ev: MonitorEvent) -> None:
        state = "NEW" if ev.is_transition else f"persisting ({ev.poll_count})"
        emit(f"{label} {ev.kind.upper()} {ev.symbol} @ {ev.price:.4f} ({state}) detail={ev.detail}".strip())
        message: tuple[str, str] | None = None
        if ev.kind == "halt":
            next_step = ("Paper has stopped taking new risk. Money sessions keep running. To stop them too: "
                         'trading kill --from-paper --confirm "HALT MONEY"' if ev.detail.get("paper_only")
                         else "No new risk until a human clears it. Closing orders still pass.")
            message = ("KILL-SWITCH", f"{ev.detail.get('reason')}\n{next_step}")
        elif ev.kind == "hedge":
            message = format_hedge(symbol=ev.symbol, detail=ev.detail)
        elif ev.kind == "entry":
            # wf_record says whether this symbol cleared walk-forward; without it the alert says so.
            message = format_entry(strategy_name=book.name_for(ev.symbol), symbol=ev.symbol, price=ev.price,
                                   detail=ev.detail, is_transition=ev.is_transition,
                                   poll_count=ev.poll_count, wf_record=validated_for(ev.symbol),
                                   live_params=running_params(book.strategy_for(ev.symbol)))
        elif ev.kind == "exit":
            shares = float(ev.detail.get("shares", 0)) if isinstance(ev.detail, dict) else 0.0
            message = format_exit(strategy_name=book.name_for(ev.symbol), symbol=ev.symbol,
                                  price=ev.price, shares=shares,
                                  detail=ev.detail if isinstance(ev.detail, dict) else None)
        if message is not None:
            # The label says which session this is: a phone alert must show whether real money moved.
            prices = f"Prices in {currency}.\n" if currency and ev.kind != "halt" else ""
            alerter.send(f"{label} {message[0]}".strip(), prices + message[1])
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
    close_symbols: tuple[str, ...] = ()
    trim_to_slots: bool = False
    emit: Emit = _quiet

    def _install_stop_handlers(self) -> None:
        """Ctrl-C and SIGTERM finish the poll and leave through the loop's ``finally``. A hard kill cannot
        be caught, so a force-terminated process leaves the book as it is."""
        def _on_signal(signum: int, _frame: object) -> None:
            self.emit(f"signal {signum}: finishing poll, then "
                      f"{'flattening' if self.monitor.flatten_on_exit else 'exiting'}.")
            self.monitor.request_stop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            # ValueError: not on the main thread. OSError: signal unsupported on this OS.
            with contextlib.suppress(ValueError, OSError):
                signal.signal(sig, _on_signal)

    def _run_one_shots(self) -> None:
        for row in self.monitor.close_symbols(self.close_symbols) if self.close_symbols else ():
            state = "ACCEPTED" if row["accepted"] else f"NOT CLOSED: {row['reason']}"
            self.emit(f"close {row['symbol']}: {row['qty']:g} @ {row.get('price', 0):.4f} {state}")
        for row in self.monitor.trim_to_slots() if self.trim_to_slots else ():
            state = "ACCEPTED" if row["accepted"] else "REJECTED by the Router"
            self.emit(f"slot trim {row['symbol']}: sold {row['sold']:g} of {row['held']:g} @ {row['price']:.4f}, "
                      f"kept {row['kept']:g} (scale x{row['scale']}) {state}")

    def run(self, iterations: int | None = None) -> None:
        self._install_stop_handlers()
        if self.keepalive is not None:
            self.keepalive.start()
        try:
            self._run_one_shots()
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


def _card_thesis(engine: VSInvestmentEngine, overlay_for: Any, weight_bias: Any,
                 monitor_ref: list[LiveMonitor]) -> Callable[[Any, str], tuple[str, str]]:
    """What the card shows for an intent: the engine's thesis over the sizing the session really applied.

    Read at prompt time, so the writeups in state/intel_writeups/ carry the overlay and the sizing chain.
    The market fields beyond the reward-to-risk multiple need columns no strategy emits yet.
    """
    def _thesis(intent: Any, broker: str) -> tuple[str, str]:
        snap = getattr(overlay_for, "last_snapshot", None) if overlay_for else None
        dec = overlay_for(intent.symbol) if overlay_for else None
        r_mult = None
        try:
            risk = abs(intent.entry - intent.stop)
            if risk > 0 and intent.target:
                r_mult = round(abs(intent.target - intent.entry) / risk, 2)
        except (AttributeError, TypeError):
            pass
        chain = [f"Order size: {intent.shares:g} units (notional ~${intent.shares * intent.entry:,.0f})"]
        if dec is not None:
            if dec.scalar < 1.0:
                chain.append(f"OSINT overlay ({dec.asset_class}): x{dec.scalar:.3f} - live intel de-risking this class"
                             + (f" ({'; '.join(dec.reasons)})" if dec.reasons else ""))
            if dec.halt_new_entries:
                chain.append(f"HALT: overlay stood {dec.asset_class} down")
        try:                                   # advisory rows only; never block the prompt
            if monitor_ref:
                ib, theses = monitor_ref[0]._interpret_bias(intent.symbol)
                if ib < 1.0:
                    chain.append(f"Interpret bias: x{ib:.3f} - theses implicating this symbol: "
                                 f"{', '.join(theses) or '(unnamed)'}")
            wb = float(weight_bias(intent.symbol)) if weight_bias else 1.0
            if wb != 1.0:
                chain.append(f"Allocator weight: x{wb:.2f} ({'boost' if wb > 1 else 'trim'})")
        except Exception as e:
            log.warning("session.thesis.chain_failed", symbol=intent.symbol, error=str(e))
        return engine.explain(intent, broker=broker, market=MarketContext(r_multiple=r_mult),
                              overlay_snapshot=snap, overlay_decisions=[dec] if dec else None,
                              sizing_chain=chain)
    return _thesis


def _home_rates(settings: Any, currency: str) -> BankOfCanadaRates:
    """Rates into the home currency, from the Bank of Canada series the audit layer already keeps."""
    return BankOfCanadaRates(Path(settings.state_dir) / "fx", currency)


def _sizing_policy(plan: SessionPlan, feed: Broker, router: Any, state_dir: Path, session_id: str | None,
                   rates: Any, emit: Emit) -> SizingPolicy | None:
    """Kraken sizes in its own lots, and the Router's trim has to round to the same ones.

    Without its real lot rules a pair would be sized on guesses, so the session refuses instead.
    """
    if plan.venue != "kraken" or not plan.sizing_v2:
        return None
    wanted = list(plan.symbols)
    rules = feed.lot_rules(wanted)  # type: ignore[attr-defined]
    missing = sorted(set(wanted) - set(rules))
    if missing:
        raise SessionError(f"No Kraken lot rules for {missing}; not starting. sizing_v2 off falls back to whole units.")
    # Kraken states a pair's minimum value in its quote currency; the sizer works in the home currency.
    rules = {s: replace(r, min_notional=r.min_notional * rates.rate(currency_of(s))) for s, r in rules.items()}
    getattr(router, "inner", router).quantity_rule_for = rules.__getitem__
    emit("sizing v2 ON. Kraken lot rules:")
    for sym, rule in rules.items():
        emit(f"    {sym:>10}  step {rule.step:g}  min {rule.min_qty:g}  min value ${rule.min_notional:g}")
    return SizingPolicy(quantity_rule_for=rules.__getitem__, periods_per_year_for=calendar_days, enforce_stops=True,
                        journal_path=state_dir / "sizing_decisions.jsonl", session_id=session_id)


def _forward_read_shadow(plan: SessionPlan, emit: Emit) -> Any:
    """The phase 4 shadow, or None. A table that will not load stops the session rather than run unlogged."""
    if not plan.forward_read_shadow:
        return None
    from .forward_read_shadow import ForwardReadShadow
    try:
        shadow = ForwardReadShadow.load(plan.forward_read_shadow)
    except (OSError, ValueError) as e:
        raise SessionError(f"forward_read_shadow table {plan.forward_read_shadow!r} did not load: {e}. "
                           "Fix the path or set it to \"\" to turn the shadow off.") from e
    emit(f"forward-read table {shadow.table_version}: {len(shadow.trades)} trades, reference ratio "
         f"{shadow.reference if shadow.reference is None else round(shadow.reference, 4)}.")
    return shadow


def _announce(plan: SessionPlan, session_id: str | None, state_dir: Path, emit: Emit) -> None:
    """Say which optional behaviour this session is running with."""
    emit(f"prices in {plan.account_currency}: every quote, position and gate figure is converted at the Bank of "
         "Canada's daily rate. A fill keeps its own currency.")
    emit(f"allocator weights: {'every ' + format(plan.allocator_refresh_seconds, 'g') + 's' if plan.allocator_refresh_seconds else 'once at startup'}.")
    emit(f"sizing: {'parallel (joint allocation per poll)' if plan.parallel_sizing else 'sequential'}.")
    if plan.forward_read_shadow:
        emit(f"forward-read shadow ON: {plan.forward_read_shadow}. Logs the read and the size it would give "
             "(SIZING_READ); orders keep today's size.")
    if plan.profit_lock:
        emit(f"profit lock ON: arms at +1 ATR, giveback 3 -> 1 ATR by +4 ATR. Exempt: {', '.join(plan.profit_lock_exempt) or 'none'}.")
    if plan.candle_exit:
        emit(f"candle exit ON: bearish reversal closes a long up >= 1 ATR. Exempt: {', '.join(plan.candle_exit_exempt) or 'none'}.")
    if plan.overbought_exit:
        emit(f"overbought exit ON: close > upper BB(20,2) or RSI(14) >= 70 closes a long up >= 1 ATR. "
             f"Exempt: {', '.join(plan.overbought_exit_exempt) or 'none'}.")
    if plan.oversold_entry:
        emit(f"oversold entry ON: adds a tranche to a held trend position. Applies to: "
             f"{', '.join(plan.oversold_entry_only) or 'none'}. Not walk-forward calibrated.")
    if plan.mute_alerts:
        emit(f"alerts muted for: {', '.join(plan.mute_alerts)} (still traded and journaled).")
    if plan.limits.get("max_round_trip_cost_ratio"):
        emit(f"cost floor ON: entries need round-trip cost <= {plan.limits['max_round_trip_cost_ratio']:.2%} of notional.")
    if plan.warmup_interval_seconds:
        emit(f"warm-up: polling every {min(plan.warmup_interval_seconds, plan.interval_seconds)}s for "
             f"{plan.warmup_minutes:g} min, then every {plan.interval_seconds}s.")
    per_session = f" or {state_dir / ('STOP_' + session_id)} (this one)" if session_id and plan.paper_equity is not None else ""
    emit(f"graceful stop: touch {state_dir / 'STOP'} (all sessions){per_session}. Exits within one poll and "
         f"{'flattens' if plan.flatten_on_exit else 'does NOT flatten'}.")


def build_session(settings: Any, plan: SessionPlan, *, feed: Broker, book: StrategyBook,
                  account_number: str | None = None, live_confirmation: str | None = None,
                  expected_env: str | None = None, allow_screened: bool = False,
                  class_overrides: Mapping[str, Any] | None = None, keepalive: Any = None,
                  close_symbols: Sequence[str] = (), trim_to_slots: bool = False,
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
    # Everything downstream is the same object graph, and both read quotes through the stale-quote guard.
    rates = _home_rates(settings, plan.account_currency)
    guarded = CurrencyNormalizingBroker(guard_feed(feed, settings), rates, native_equity=_NATIVE_EQUITY.get(plan.venue))
    exec_broker: Broker = guarded
    cost_model = VenueCostModel(**plan.paper_costs) if plan.paper_costs else None
    if plan.paper_equity is not None:
        paper = PaperBroker(feed=guarded, starting_equity=plan.paper_equity, journal_dir=state_dir,
                            slippage_bps=cost_model.slippage_bps if cost_model else 5.0, cost_model=cost_model,
                            fill_model="touch", session_id=plan.resume_session or None,
                            book_currency=plan.account_currency)
        if plan.resume_session:
            try:
                restored = paper.resume()
            except PaperBroker.RehydrationMismatch as e:
                raise SessionError(f"Cannot resume session {plan.resume_session}: {e}") from e
            emit(f"{label} RESUMED session {plan.resume_session}: {restored['fills_replayed']} fills replayed, positions "
                 f"{restored['positions'] or 'none'}, cash ${restored['cash']:,.2f}, realized ${restored['realized_pnl']:,.2f}.")
        exec_broker = paper
        emit(f"{label} PAPER. session={paper.session_id} equity={plan.paper_equity:,.0f}; "
             "fills are simulated, the real account is untouched.")

    # Data.
    preflight(feed, symbols, emit=emit)
    # Converted bars in a cache of their own: the same path holds native bars from other runners.
    market = MarketData(exec_broker, cache=CandleCache(Path(settings.data_cache_dir) / f"numeraire_{plan.account_currency}"))

    # Audit, before anything can trade.
    ledger = _open_ledger(settings, plan, getattr(exec_broker, "session_id", None))
    session_id = getattr(exec_broker, "session_id", None) or getattr(ledger, "session_id", None)

    # Intel.
    overlay_for, interpret_for = (None, None)
    if plan.intel_overlay:
        overlay_for, interpret_for = intel_overlay_hooks(settings, plan.intel_refresh_seconds, class_overrides,
                                                         emit=emit)
    if overlay_for is None:
        emit("intel overlay OFF " + ("(no WORLDMONITOR_API_KEY)." if plan.intel_overlay else "(turned off)."))
    weight_bias = venue_weight_bias(plan.venue, market, symbols, overlay_for, emit=emit)
    if plan.allocator_refresh_seconds > 0:
        weight_bias = RefreshingBias(lambda: venue_weight_bias(plan.venue, market, symbols, overlay_for), weight_bias,
                                     plan.allocator_refresh_seconds, symbols=symbols, ledger=ledger)

    # Risk gates.
    budget: dict[str, Any] = {}
    if plan.daily_budget is not None:
        budget = {"daily_max_trades": plan.daily_budget[0], "daily_max_notional_usd": plan.daily_budget[1]}
    try:
        router: Any = Router.build_default(mode=plan.mode, broker=exec_broker, state_dir=state_dir,  # type: ignore[arg-type]
                                           **plan.limits, **budget, live_confirmation=live_confirmation,
                                           ledger=ledger, cost_model=cost_model,
                                           position_cap_pct_for=position_cap_for(settings, market))
    except (LiveModeNotConfirmed, AutonomousNotEnabled) as e:
        raise SessionError(str(e)) from e
    if attach_basket_gate(router, _BASKET_VENUES[plan.venue], state_dir, enabled=plan.require_basket):
        emit(f"basket gate ON: entries need a card-signed {_BASKET_VENUES[plan.venue]} basket "
             "(python scripts/basket.py status).")
    spectral = attach_spectral_gate(router, feed, cap_pct=plan.spectral_var_cap, level=plan.spectral_var_level,
                                    horizon_days=plan.spectral_var_horizon)
    if spectral is not None:
        emit(f"spectral VaR gate ON: {spectral.thresholds()}")
    router.price_currency = plan.account_currency
    sizing_policy = _sizing_policy(plan, feed, router, state_dir, session_id, rates, emit)
    monitor_ref: list[LiveMonitor] = []
    if plan.require_card:
        wiring = wire_card_approval(
            router, shim_host=plan.card_shim_host, shim_port=plan.card_shim_port, ttl_seconds=plan.card_ttl_seconds,
            thesis_fn=_card_thesis(VSInvestmentEngine(), overlay_for, weight_bias, monitor_ref),
            db_path=state_dir / "approval.db", state_dir=state_dir, session_id=session_id,
            account_currency=plan.account_currency, start_shim=not plan.card_attach,
            books=None if plan.card_attach else [BookRef(venue=plan.venue, session_id=str(session_id or ""),
                                                         currency=plan.account_currency,
                                                         asset_class=_ASSET_CLASSES[plan.venue])])
        router = wiring.router
        if plan.card_attach:
            emit(f"require-card ON: ATTACHED to the shim at {wiring.shim_url}. It must already serve state/approval.db.")
        else:
            emit(f"require-card ON: approval shim at {wiring.shim_url}. Card auth token: {wiring.auth_token}")
            emit("Pair the card by passing the token as Authorization: Bearer <token> on every request.")
            emit(f"desk panel at {wiring.desk_url} (build it first: python scripts/build_desk_dash.py).")

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
        on_event=alert_emitter(alerter, book, label=label, currency=plan.account_currency, emit=emit),
        account_currency=plan.account_currency, weight_bias_range=plan.allocator_bias_range,
        emit_on_change_only=plan.emit_on_change_only, strategy_map=dict(book.per_symbol) or None,
        hedge_symbol=plan.hedge_symbol or None, overlay_for=overlay_for, interpret_for=interpret_for,
        weight_bias_for=weight_bias,
        persistence_for=PersistenceGate(min_polls=plan.persistence["min_polls"],
                                        max_age_hours=plan.persistence["max_age_hours"],
                                        class_overrides=class_overrides),
        sizing_policy=sizing_policy, flatten_on_exit=plan.flatten_on_exit, stop_sentinel_dir=state_dir,
        warmup_interval_seconds=plan.warmup_interval_seconds or None, warmup_minutes=plan.warmup_minutes,
        profit_lock=ProfitLock() if plan.profit_lock else None, profit_lock_exempt=set(plan.profit_lock_exempt),
        candle_exit=CandleExit() if plan.candle_exit else None, candle_exit_exempt=set(plan.candle_exit_exempt),
        overbought_exit=OverboughtExit() if plan.overbought_exit else None,
        overbought_exit_exempt=set(plan.overbought_exit_exempt),
        oversold_entry=OversoldEntry() if plan.oversold_entry else None,
        oversold_entry_only=set(plan.oversold_entry_only),
        parallel_sizing=plan.parallel_sizing, mute_symbols=set(plan.mute_alerts),
        forward_read_shadow=_forward_read_shadow(plan, emit),
        **monitor_policy(settings),
    )
    monitor_ref.append(monitor)
    emit(f"{label} {plan.mode} on {plan.venue}: {len(symbols)} symbols, plan {plan.fingerprint()}.")
    _announce(plan, session_id, state_dir, emit)
    for sym in symbols:
        emit(f"    {sym:>12} <- {book.name_for(sym)}")
    if ledger is not None:
        emit(f"audit ledger ON: {ledger.dir} stream {plan.stream}")
    return Session(plan=plan, monitor=monitor, router=router, exec_broker=exec_broker, market=market,
                   ledger=ledger, feed=feed, keepalive=keepalive, close_symbols=tuple(close_symbols),
                   trim_to_slots=trim_to_slots, emit=emit)


def start_session(settings: Any, *, venue: str, mode: RouterMode, symbols: str | Sequence[str] | None = None,
                  strategy: str = "", strategy_map: str = "", futures: Sequence[str] = (), transport: str = "web",
                  options: SessionOptions | None = None, account_number: str | None = None,
                  live_confirmation: str | None = None, expected_env: str | None = None,
                  rehearsal: bool = False, label: str = "", emit: Emit = _quiet) -> Session:
    """Everything a runner does, from its arguments to a session ready to run."""
    o = options or SessionOptions()
    book = resolve_book(settings, venue, strategy, strategy_map)
    wanted = resolve_symbols(settings, venue, symbols, book, futures)
    book = book_with_params(book, wanted, o.params or settings.params, emit=emit)
    plan = plan_session(settings, venue=venue, mode=mode, symbols=wanted, book=book, options=o, rehearsal=rehearsal)
    if plan.paper_equity is None and (o.resume_session or o.close_symbols or o.trim_to_slots):
        raise SessionError("--resume-session, --close-symbols and --trim-to-slots act on a simulated book only.")
    if venue == "ib":
        _check_ib_policy(wanted, futures, futures_only=settings.ib_futures_only)
    feed, keepalive = venue_feed(settings, venue, money=plan.money, transport=transport, futures=futures)
    return build_session(settings, plan, feed=feed, book=book, account_number=account_number,
                         live_confirmation=live_confirmation, expected_env=expected_env,
                         allow_screened=o.allow_screened,
                         class_overrides=futures_overrides(list(futures)) if futures else None,
                         keepalive=keepalive, close_symbols=_names(o.close_symbols, upper=True),
                         trim_to_slots=o.trim_to_slots, label=label or f"{venue}-{mode}", emit=emit)


def _check_ib_policy(symbols: Sequence[str], futures: Sequence[str], *, futures_only: bool = False) -> None:
    """The desk's split (desk_policy.py): IB carries derivatives and overseas listings; North American
    equities trade on Questrade. ``ib_futures_only`` narrows it to futures."""
    roots = {r.upper().lstrip("/") for r in futures}
    named = [f"/{s.lstrip('/')}" if s.lstrip("/") in roots else s for s in symbols]
    try:
        (assert_ib_futures_only if futures_only else assert_ib_no_questrade_equities)(named)
    except VenuePolicyError as e:
        raise SessionError(str(e)) from e


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
    ap.add_argument("--card-shim-host", default=None,
                    help="Interface the approval shim binds. Loopback by default; set a LAN address deliberately "
                         "to reach the card or the desk panel from a device.")
    ap.add_argument("--card-attach", dest="card_attach", action="store_true", default=None,
                    help="Wrap the router for card approval but do NOT bind a port: attach to a shim already "
                         "running on state/approval.db. This is how QT, Kraken and IB share one port and token.")
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
    ap.add_argument("--flatten-on-exit", dest="flatten_on_exit", action=bool_flag, default=None,
                    help="PAPER ONLY. Close every open position through the Router when the session stops. To stop "
                         "cleanly, touch state/STOP_<session_id>; a hard kill skips this.")
    ap.add_argument("--warmup-interval", dest="warmup_interval", type=int, default=None,
                    help="Poll every N seconds for the first --warmup-minutes, then fall back to --interval.")
    ap.add_argument("--warmup-minutes", dest="warmup_minutes", type=float, default=None)
    ap.add_argument("--parallel-sizing", dest="parallel_sizing", action=bool_flag, default=None,
                    help="Allocate each poll's entries jointly before routing any of them. The Router still gates each.")
    ap.add_argument("--forward-read-shadow", dest="forward_read_shadow", default=None,
                    help="Forward-read table to log beside each entry (SIZING_READ). Orders keep today's size.")
    ap.add_argument("--mute-alerts", dest="mute_alerts", default=None,
                    help="Comma-separated symbols whose alerts are muted (still traded and journaled).")
    ap.add_argument("--max-cost-ratio", dest="max_cost_ratio", type=float, default=None,
                    help="Reject an ENTRY whose round-trip cost exceeds this fraction of its notional, priced per "
                         "venue. 0 = off. Kraken's fee alone is ~52 bps round trip; a 0.005 ceiling fits at no size.")
    ap.add_argument("--max-positions", dest="max_positions", type=int, default=None,
                    help="Open-position cap for this run, over max_open_positions.")
    ap.add_argument("--params", dest="params", default=None, choices=PARAMS_MODES,
                    help="Where each symbol's strategy parameters come from: default, wf or calibrated.")
    ap.add_argument("--sizing-v2", dest="sizing_v2", action=bool_flag, default=None,
                    help="Kraken's own lot rules (fractional coins). Refuses to start if they cannot be fetched.")
    ap.add_argument("--resume-session", dest="resume_session", default="",
                    help="PAPER ONLY. Continue an earlier session's book from state/ instead of starting flat, at the "
                         "same --paper-equity. Refuses if the journals disagree.")
    ap.add_argument("--close-symbols", dest="close_symbols", default="",
                    help="PAPER ONLY, one-shot. Close these positions in full at startup, through the Router.")
    ap.add_argument("--trim-to-slots", dest="trim_to_slots", action="store_true",
                    help="PAPER ONLY, one-shot. Trim every holding by the same factor to make room for free slots.")


def options_from_args(args: argparse.Namespace, *, level: bool | None = None) -> SessionOptions:
    return SessionOptions(interval=args.interval, level=level, intel_overlay=args.intel_overlay,
                          require_card=args.require_card, card_shim_port=args.card_shim_port,
                          card_shim_host=args.card_shim_host, card_attach=args.card_attach, card_ttl=args.card_ttl,
                          require_basket=args.require_basket, spectral_var_cap=args.spectral_var_cap,
                          spectral_var_level=args.spectral_var_level, spectral_var_horizon=args.spectral_var_horizon,
                          paper_equity=args.paper_equity, audit_ledger=args.audit_ledger,
                          flatten_on_exit=args.flatten_on_exit, warmup_interval=args.warmup_interval,
                          warmup_minutes=args.warmup_minutes, parallel_sizing=args.parallel_sizing,
                          forward_read_shadow=args.forward_read_shadow,
                          mute_alerts=args.mute_alerts, max_cost_ratio=args.max_cost_ratio,
                          max_positions=args.max_positions, params=args.params, sizing_v2=args.sizing_v2,
                          account_currency=getattr(args, "account_currency", None),
                          resume_session=args.resume_session, close_symbols=args.close_symbols,
                          trim_to_slots=args.trim_to_slots)


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
    "book_with_params",
    "build_alerter",
    "build_session",
    "check_money_ready",
    "intel_overlay_hooks",
    "options_from_args",
    "paper_costs_for",
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
