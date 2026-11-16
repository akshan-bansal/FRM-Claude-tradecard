"""Typer-based CLI: `trading <subcommand>`.

Every command keeps live-mode behind explicit flags. The default mode
across the board is ``paper``.
"""
from __future__ import annotations

import os
import sys
from dataclasses import fields
from pathlib import Path

import pandas as pd
import typer
from rich.console import Console
from rich.table import Table

from .analysis import build_signal_matrix, render_matrix_markdown
from .audit.ledger import Ledger
from .backtest import BacktestEngine
from .brokers import Broker, PaperBroker
from .brokers.fresh import guard_feed
from .config import get_settings
from .daemon import AutonomousDaemon
from .data import CandleCache, MarketData
from .execution import AUTONOMOUS_ENV_VAR, Router
from .execution.asset_router import ASSET_CLASSES, AssetRouter
from .execution.daily_budget import DailyBudget
from .integrations import (
    QuantConnectClient,
    QuantConnectError,
    analyze_library,
    list_library,
    pull_algorithm,
)
from .integrations.lean_algorithm import (
    DEFAULT_LEAN_ALGORITHM,
    LEAN_CANDLESTICK_MAP,
    comprehensive_lean_algorithms,
    gap_family_algorithms,
    render_candlestick_lean_algorithm,
    render_lean_algorithm,
)
from .logging_setup import configure_logging, get_logger
from .monitor import Alerter
from .monitor.alerter import AlertConfig
from .monitor.session import SessionError, SessionOptions, build_alerter, start_session, venue_feed
from .optimize import PARAM_GRIDS, optimize_parameters
from .risk import KillSwitch
from .scoring.objective import DEFAULT_METRIC_WEIGHTS, OBJECTIVES, make_dot_product
from .scoring.qc_bridge import rank_qc_library
from .scoring.routing import (
    route_symbols_to_strategies,
    strategy_asset_plan,
    to_strategy_map_string,
)
from .scoring.selection import (
    combine_scores,
    family_coverage,
    rank_cells,
    render_combined_scoreboard,
    score_strategies,
)
from .strategies import STRATEGIES
from .tune import (
    DEFAULT_LABEL_HORIZON,
    DEFAULT_TUNE_STRATEGIES,
    DEFAULT_TUNE_UNIVERSE,
    CandidateEvents,
    TuneResult,
    apply_tune,
    audit_candidates,
    lens_cell,
    run_tune,
)


def _session_ledger(settings: object, stream: str, *, mode: str, session_id: str | None = None,
                    strict: bool = False, shared: bool = False, enabled: bool = True) -> Ledger | None:
    """Open this process's audit-ledger stream under ``state/ledger`` and record the session start.

    One stream per process (the chain is single-writer); ``shared`` is for one-shot commands that
    can overlap. ``strict`` makes a failed ledger write stop the order instead of letting it
    trade unrecorded: on for live and autonomous, off for paper.
    """
    if not enabled:
        return None
    import atexit

    from .audit.versioning import session_provenance
    led = Ledger(Path(settings.state_dir) / "ledger", stream=stream, mode=mode,  # type: ignore[attr-defined]
                 session_id=session_id, strict=strict, shared=shared)
    led.append("SESSION_STARTED", session_provenance(argv=sys.argv))
    atexit.register(lambda: led.append("SESSION_STOPPED", {"argv0": sys.argv[0]}))
    return led

app = typer.Typer(help="Claude Code algorithmic trading CLI (paper-first; live behind explicit flag).")
console = Console()
log = get_logger(__name__)

_DEFAULT_REPORTS_DIR = Path("reports")

_O_VENUE = typer.Option("ib", help="ib (equities and derivatives) or kraken (crypto).")
_O_SYMBOLS = typer.Option("", help="Comma-separated symbols. Default: the venue's own list (Kraken: the sleeve).")
_O_STRATEGY = typer.Option("", help="Fallback strategy key. Default: the venue's (Kraken: the sleeve's own).")
_O_STRATEGY_MAP = typer.Option(
    "", help="Per-symbol strategy overrides, e.g. 'XIC.TO=bollinger,BNS.TO=momentum_breakout'"
)
_O_INTERVAL = typer.Option(None, help="Poll interval, seconds. Default: poll_interval_seconds.")
_O_ITERATIONS = typer.Option(0, help="Run N iterations and stop. 0 = forever.")
_O_LEVEL = typer.Option(
    None, "--level/--edge", help="Alert on every poll a name is AT an entry/exit level, not only when it flips.",
)
_O_HEDGE = typer.Option(None, "--hedge/--no-hedge", help="The dollar-hedge overlay (UUP). Off by default.")
_O_INTEL = typer.Option(
    None, "--intel-overlay/--no-intel-overlay",
    help="The live risk overlay and thesis trims. On whenever WORLDMONITOR_API_KEY is set. If the feed fails, "
         "each class falls back to a 0.8 cap, which can loosen a tighter trim.",
)
_O_INTEL_REFRESH = typer.Option(None, help="Seconds between overlay refreshes.")
_O_CARD = typer.Option(
    None, "--require-card/--no-require-card", help="A TradeCard must ACCEPT every order.",
)
_O_CARD_PORT = typer.Option(None, help="Port for the approval shim under --require-card.")
_O_CARD_TTL = typer.Option(None, help="Seconds a card prompt stays open.")
_O_BASKET = typer.Option(
    None, "--require-basket/--no-require-basket", help="Entries need a card-signed basket for this venue.",
)
_O_SPECTRAL_CAP = typer.Option(None, help="Spectral VaR gate: cap an entry's tail loss at this fraction of equity.")
_O_SPECTRAL_LEVEL = typer.Option(None, help="Spectral VaR confidence level.")
_O_SPECTRAL_HORIZON = typer.Option(None, help="Spectral VaR horizon in days.")
_O_FUTURES = typer.Option("", help="IB only: futures roots to resolve as FUT front-month, e.g. 'ES,NQ,GC'.")
_O_TRANSPORT = typer.Option(
    "auto", help="IB only: auto (each region on the transport ib_access in trading.yaml names), "
                 "web (Client Portal), socket (TWS/IB Gateway) or connector (read-only).")


_O_CARD_HOST = typer.Option(
    None, help="Interface the approval shim binds. Loopback by default; set a LAN address deliberately to reach the "
               "card or the desk panel from a device.")
_O_CARD_ATTACH = typer.Option(
    None, "--card-attach/--card-own-shim",
    help="Wrap the router for card approval but do NOT bind a port: attach to a shim already running on "
         "state/approval.db (scripts/approval_shim.py --db ...). This is how QT, Kraken and IB share one port and token.")
_O_FLATTEN = typer.Option(
    None, "--flatten-on-exit/--no-flatten-on-exit",
    help="PAPER ONLY. Close every open position through the Router when the session stops, so its last journal row "
         "is a flat book. On by default. A hard kill skips it. A money session never closes itself.")
_O_WARMUP_INTERVAL = typer.Option(
    None, help="Poll every N seconds for the first --warmup-minutes after launch, then fall back to --interval in the "
               "same process. 0 = off. It re-checks the forming bar and live quotes; it makes no new daily signals.")
_O_WARMUP_MINUTES = typer.Option(None, help="Length of the warm-up window in minutes.")
_O_PROFIT_LOCK = typer.Option(
    None, "--profit-lock/--no-profit-lock",
    help="Once a long is up 1 ATR, exit when price falls back from its best polled price by an allowed giveback that "
         "shrinks from 3 ATR to 1 ATR as the gain grows, never below entry. See signals/profit_lock.py.")
_O_PROFIT_LOCK_EXEMPT = typer.Option(
    None, help="Comma-separated strategies the profit lock never applies to. Default exempts ts_momentum: the "
               "2026-09-18 backtest showed the lock cutting its trend winners. Empty locks every strategy.")
_O_CANDLE_EXIT = typer.Option(
    None, "--candle-exit/--no-candle-exit",
    help="Sell a long up >= 1 ATR (and >= 2 round-trip costs) when a bearish reversal candle completed on the previous "
         "daily bar. See signals/candle_exit.py.")
_O_CANDLE_EXIT_EXEMPT = typer.Option(None, help="Comma-separated strategies the candle exit never applies to.")
_O_OVERBOUGHT_EXIT = typer.Option(
    None, "--overbought-exit/--no-overbought-exit",
    help="Sell a long up >= 1 ATR (and >= 2 round-trip costs) when the last completed daily bar closed above the upper "
         "Bollinger band (20, 2 sd) or RSI(14) >= 70. See signals/overbought_exit.py.")
_O_OVERBOUGHT_EXIT_EXEMPT = typer.Option(None, help="Comma-separated strategies the overbought exit never applies to.")
_O_OVERSOLD_ENTRY = typer.Option(
    None, "--oversold-entry/--no-oversold-entry",
    help="Not walk-forward calibrated. Add a tranche to a HELD trend position when the last completed bar closed below "
         "the lower Bollinger band or RSI(14) <= 30 while its trend signal is on. See signals/oversold_entry.py.")
_O_OVERSOLD_ENTRY_ONLY = typer.Option(None, help="Comma-separated strategies the oversold entry applies to.")
_O_PARALLEL = typer.Option(
    None, "--parallel-sizing/--sequential-sizing",
    help="Allocate each poll's entries jointly (ranked slots, pro-rata share of the leverage headroom, per-symbol cap) "
         "before routing any of them. On by default. The Router still gates every intent.")
_O_MUTE = typer.Option(None, help="Comma-separated symbols whose alerts are muted. They are still traded and journaled.")
_O_COST = typer.Option(
    None, "--max-cost-ratio",
    help="Reject an ENTRY whose round-trip cost exceeds this fraction of its notional, priced per venue "
         "(execution/venue_costs.py). 0 = off. Exits are never gated on cost.")
_O_MAX_POSITIONS = typer.Option(None, "--max-positions", help="Open-position cap for this run, over max_open_positions.")
_O_PARAMS = typer.Option(None, "--params", help="Where each symbol's strategy parameters come from: default, wf or calibrated.")
_O_SIZING_V2 = typer.Option(
    None, "--sizing-v2/--no-sizing-v2", help="Kraken's own lot rules (fractional coins). Refuses to start without them.")
_O_RESUME = typer.Option(
    "", "--resume-session",
    help="PAPER ONLY. Continue an earlier session's book from state/ instead of starting flat, at the same equity. "
         "Refuses if the journals disagree. Pair with --no-flatten-on-exit on the session you stop for a restart.")
_O_CLOSE = typer.Option(
    "", "--close-symbols", help="PAPER ONLY, one-shot. Close these positions in full at startup, through the Router.")
_O_TRIM = typer.Option(
    False, "--trim-to-slots/--no-trim-to-slots",
    help="PAPER ONLY, one-shot. Trim every holding by the same factor to make room for free slots, through the Router.")
_SESSION_FIELDS = {f.name for f in fields(SessionOptions)}


def _options_from(given: dict[str, object], **extra: object) -> SessionOptions:
    """``SessionOptions`` from a command's own parameters, so a flag on the command cannot miss the session."""
    return SessionOptions(**{k: v for k, v in given.items() if k in _SESSION_FIELDS}, **extra)  # type: ignore[arg-type]


def _emit(message: str) -> None:
    console.print(message, markup=False, highlight=False)


def _run_session(settings, *, iterations: int = 0, **kwargs) -> None:
    """Every monitoring runner: build from monitor/session.py, then run. Paper and money share all of it."""
    try:
        session = start_session(settings, emit=_emit, **kwargs)
    except SessionError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=2) from e
    session.run(iterations)


def _roots(text: str) -> list[str]:
    return [s.strip().upper() for s in text.split(",") if s.strip()]



# ----- helpers ---------------------------------------------------------------


def _venue_of(symbols: list[str]) -> str:
    """Kraken for crypto pairs, IB for everything else. A list that needs both is refused."""
    crypto = ["/" in s and not s.startswith("/") for s in symbols]
    if all(crypto) and crypto:
        return "kraken"
    if not any(crypto):
        return "ib"
    console.print("[red]These symbols span Kraken (crypto) and IB; run each venue separately.[/red]")
    raise typer.Exit(code=2)


def _make_broker(settings, venue: str, *, money: bool = False) -> Broker:
    """The venue's adapter. It places orders only with ``money``; reads go wherever its plan says."""
    try:
        broker, _keepalive = venue_feed(settings, venue, money=money)
    except SessionError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=2) from e
    return broker


def _account(settings, broker: Broker, venue: str) -> str:
    if venue == "ib" and settings.ib_account:
        return settings.ib_account
    accounts = broker.accounts()
    if not accounts:
        console.print(f"[red]No accounts on the {venue} login.[/red]")
        raise typer.Exit(code=1)
    return accounts[0].number


def _make_qc(settings) -> QuantConnectClient:
    if not settings.quantconnect_user_id or not settings.quantconnect_api_token:
        console.print(
            "[red]QUANTCONNECT_USER_ID / QUANTCONNECT_API_TOKEN missing in .env. "
            "Get them at https://www.quantconnect.com/account.[/red]"
        )
        raise typer.Exit(code=2)
    return QuantConnectClient(settings.quantconnect_user_id, settings.quantconnect_api_token)


def _run_qc_flow(
    client: QuantConnectClient, project: str, content: str, name: str, timeout: float
) -> tuple[int, str, dict[str, object]]:
    """Create project → upload → compile → backtest on QC cloud; return ids + result."""
    if not client.authenticate():
        console.print("[red]QuantConnect authentication failed.[/red]")
        raise typer.Exit(code=1)
    proj = client.create_project(project, language="Py")
    projects = proj.get("projects")
    project_id = 0
    if isinstance(projects, list) and projects and isinstance(projects[0], dict):
        project_id = int(str(projects[0].get("projectId", 0)))
    console.print(f"[dim]project {project_id} created[/dim]")

    client.put_file(project_id, "main.py", content)
    compile_id = str(client.compile_project(project_id).get("compileId", ""))
    console.print(f"[dim]compiling ({compile_id})…[/dim]")
    client.wait_for_compile(project_id, compile_id)

    bt = client.create_backtest(project_id, compile_id, name)
    backtest = bt.get("backtest", {})
    backtest_id = str(backtest.get("backtestId", "")) if isinstance(backtest, dict) else ""
    console.print(f"[dim]backtest {backtest_id} running…[/dim]")
    result = client.wait_for_backtest(project_id, backtest_id, timeout_seconds=timeout)
    return project_id, backtest_id, result


def _strategy_or_die(name: str):
    if name not in STRATEGIES:
        console.print(f"[red]Unknown strategy '{name}'. Available: {sorted(STRATEGIES)}[/red]")
        raise typer.Exit(code=2)
    return STRATEGIES[name]()


# ----- commands --------------------------------------------------------------


@app.callback()
def main_callback() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_dir)


@app.command()
def status(venue: str = _O_VENUE) -> None:
    """Read-only sanity check: login, accounts, balances."""
    settings = get_settings()
    broker = _make_broker(settings, venue)
    accounts = broker.accounts()
    table = Table(title=f"{venue} accounts")
    for col in ("number", "type", "status", "primary"):
        table.add_column(col)
    for a in accounts:
        table.add_row(a.number, a.type, a.status, str(a.isPrimary))
    console.print(table)
    if accounts:
        cur = settings.account_currency
        eq = broker.equity(accounts[0].number, currency=cur)
        console.print(f"Equity (primary, {cur}): [green]${eq:,.2f}[/green]")


@app.command()
def backtest(
    strategy: str = typer.Option(..., help="Strategy key (e.g. ema_crossover, rsi_meanrevert, macd, bollinger, momentum_breakout)"),
    symbol: str = typer.Option(..., help="Ticker, e.g. AAPL or SHOP.TO"),
    years: float = typer.Option(3.0, help="Years of history to fetch"),
    interval: str = typer.Option("1d", help="Candle interval: 1d, 1h, 30m, 15m, 5m, 1m"),
    out: Path = typer.Option(_DEFAULT_REPORTS_DIR, help="Output dir for report"),
) -> None:
    """Run a single-symbol backtest and save a markdown report."""
    settings = get_settings()
    broker = _make_broker(settings, _venue_of([symbol.upper()]))
    cache = CandleCache(settings.data_cache_dir)
    market = MarketData(broker, cache=cache)
    df = market.history(symbol=symbol, years=years, interval=interval)
    if df.empty:
        console.print(f"[red]No data returned for {symbol}.[/red]")
        raise typer.Exit(code=1)

    strat = _strategy_or_die(strategy)
    engine = BacktestEngine()
    result = engine.run(strategy=strat, df=df, symbol=symbol, timeframe=interval)
    md = result.summary_markdown()
    out.mkdir(parents=True, exist_ok=True)
    report_path = out / f"{strategy}_{symbol.replace('.', '_')}.md"
    report_path.write_text(md, encoding="utf-8")
    console.print(md)
    console.print(f"\n[green]Report written:[/green] {report_path}")


@app.command()
def signal(
    strategy: str = typer.Option(..., help="Default/fallback strategy key"),
    symbols: str = typer.Option(..., help="Comma-separated symbols"),
    interval: int = _O_INTERVAL,
    iterations: int = _O_ITERATIONS,
    strategy_map: str = _O_STRATEGY_MAP,
    level: bool = _O_LEVEL,
    hedge: bool = _O_HEDGE,
    intel_overlay: bool = _O_INTEL,
    overlay_refresh: float = _O_INTEL_REFRESH,
    venue: str = _O_VENUE,
    paper: bool = typer.Option(
        False,
        "--paper/--dry-run",
        help="Route signals to a SIMULATED paper broker that fills orders and journals them, "
             "instead of the default dry-run router that gates and logs only. Paper never touches "
             "the real account; it builds the fill history live mode requires.",
    ),
    paper_equity: float = typer.Option(None, help="Starting equity. Default: paper_starting_equity."),
    require_card: bool = _O_CARD,
    card_shim_port: int = _O_CARD_PORT,
    card_ttl: float = _O_CARD_TTL,
    require_basket: bool = _O_BASKET,
    spectral_var_cap: float = _O_SPECTRAL_CAP,
    spectral_var_level: float = _O_SPECTRAL_LEVEL,
    spectral_var_horizon: int = _O_SPECTRAL_HORIZON,
    card_shim_host: str = _O_CARD_HOST,
    card_attach: bool = _O_CARD_ATTACH,
    warmup_interval: int = _O_WARMUP_INTERVAL,
    warmup_minutes: float = _O_WARMUP_MINUTES,
    profit_lock: bool = _O_PROFIT_LOCK,
    profit_lock_exempt: str = _O_PROFIT_LOCK_EXEMPT,
    candle_exit: bool = _O_CANDLE_EXIT,
    candle_exit_exempt: str = _O_CANDLE_EXIT_EXEMPT,
    overbought_exit: bool = _O_OVERBOUGHT_EXIT,
    overbought_exit_exempt: str = _O_OVERBOUGHT_EXIT_EXEMPT,
    oversold_entry: bool = _O_OVERSOLD_ENTRY,
    oversold_entry_only: str = _O_OVERSOLD_ENTRY_ONLY,
    parallel_sizing: bool = _O_PARALLEL,
    mute_alerts: str = _O_MUTE,
    max_cost_ratio: float = _O_COST,
    max_positions: int = _O_MAX_POSITIONS,
    params: str = _O_PARAMS,
    sizing_v2: bool = _O_SIZING_V2,
    flatten_on_exit: bool = _O_FLATTEN,
    resume_session: str = _O_RESUME,
    close_symbols: str = _O_CLOSE,
    trim_to_slots: bool = _O_TRIM,
    audit_ledger: bool = typer.Option(
        True, "--audit-ledger/--no-audit-ledger",
        help="Record every signal, gate decision and order to the hash-chained audit ledger "
             "under state/ledger (the venue's stream). On by default.",
    ),
) -> None:
    """Live-signal monitor on the venue's quotes. Never places real orders.

    Article skill #5 ("live-signal-monitor"): output signal only. Use --strategy-map
    to monitor each symbol with its own strategy (e.g. the tuner's best-per-symbol);
    symbols not in the map use --strategy as the fallback.
    """
    given = locals()
    _run_session(
        get_settings(), iterations=iterations, venue=venue, mode="paper" if paper else "dry-run",
        symbols=symbols, strategy=strategy, strategy_map=strategy_map, label="[signal]",
        options=_options_from(given, intel_refresh=overlay_refresh),
    )


@app.command(name="fundamentals-fetch")
def fundamentals_fetch(
    ticker: str = typer.Option(..., help="EDGAR ticker (e.g. AAPL, or RY for a cross-listed CA name)"),
    symbol: str = typer.Option(..., help="Store key = the traded symbol (e.g. AAPL, RY.TO)"),
    user_agent: str = typer.Option("", help="SEC User-Agent with a contact; else the EDGAR_USER_AGENT env var"),
) -> None:
    """Pull quarterly book value per share from SEC EDGAR into data/fundamentals/{symbol}.csv,
    lighting up that symbol's val_* strategies. US filers and SEC-cross-listed CA names only."""
    from .data.fundamentals import FundamentalsStore
    from .data.fundamentals_edgar import fetch_to_store

    store = FundamentalsStore()
    n = fetch_to_store(ticker, symbol, store, user_agent=user_agent or None)
    if n:
        console.print(f"[green]wrote {n} quarterly book-value rows[/green] to {store.path_for(symbol)}")
    else:
        console.print(
            f"[yellow]no EDGAR data for {ticker!r}[/yellow] "
            "(US / SEC-cross-listed only; drop a CSV into data/fundamentals/ by hand otherwise)"
        )


@app.command()
def paper(
    venue: str = _O_VENUE,
    symbols: str = _O_SYMBOLS,
    strategy: str = _O_STRATEGY,
    strategy_map: str = _O_STRATEGY_MAP,
    interval: int = _O_INTERVAL,
    iterations: int = _O_ITERATIONS,
    level: bool = _O_LEVEL,
    hedge: bool = _O_HEDGE,
    intel_overlay: bool = _O_INTEL,
    overlay_refresh: float = _O_INTEL_REFRESH,
    require_card: bool = _O_CARD,
    card_shim_port: int = _O_CARD_PORT,
    card_ttl: float = _O_CARD_TTL,
    require_basket: bool = _O_BASKET,
    spectral_var_cap: float = _O_SPECTRAL_CAP,
    spectral_var_level: float = _O_SPECTRAL_LEVEL,
    spectral_var_horizon: int = _O_SPECTRAL_HORIZON,
    futures: str = _O_FUTURES,
    transport: str = _O_TRANSPORT,
    card_shim_host: str = _O_CARD_HOST,
    card_attach: bool = _O_CARD_ATTACH,
    warmup_interval: int = _O_WARMUP_INTERVAL,
    warmup_minutes: float = _O_WARMUP_MINUTES,
    profit_lock: bool = _O_PROFIT_LOCK,
    profit_lock_exempt: str = _O_PROFIT_LOCK_EXEMPT,
    candle_exit: bool = _O_CANDLE_EXIT,
    candle_exit_exempt: str = _O_CANDLE_EXIT_EXEMPT,
    overbought_exit: bool = _O_OVERBOUGHT_EXIT,
    overbought_exit_exempt: str = _O_OVERBOUGHT_EXIT_EXEMPT,
    oversold_entry: bool = _O_OVERSOLD_ENTRY,
    oversold_entry_only: str = _O_OVERSOLD_ENTRY_ONLY,
    parallel_sizing: bool = _O_PARALLEL,
    mute_alerts: str = _O_MUTE,
    max_cost_ratio: float = _O_COST,
    max_positions: int = _O_MAX_POSITIONS,
    params: str = _O_PARAMS,
    sizing_v2: bool = _O_SIZING_V2,
    flatten_on_exit: bool = _O_FLATTEN,
    resume_session: str = _O_RESUME,
    close_symbols: str = _O_CLOSE,
    trim_to_slots: bool = _O_TRIM,
    starting_equity: float = typer.Option(None, help="Default: paper_starting_equity. Use what you plan to trade."),
) -> None:
    """Rehearse a money session: same build as `trading live`, with fills simulated against the venue's quotes."""
    given = locals()
    _run_session(
        get_settings(), iterations=iterations, venue=venue, mode="paper", symbols=symbols, strategy=strategy,
        strategy_map=strategy_map, futures=_roots(futures), transport=transport, label=f"[{venue}-paper]",
        options=_options_from(given, intel_refresh=overlay_refresh, paper_equity=starting_equity),
    )


@app.command()
def live(
    confirm: str = typer.Option(..., help='Confirmation phrase. Must equal: I UNDERSTAND THE RISK'),
    venue: str = _O_VENUE,
    symbols: str = _O_SYMBOLS,
    strategy: str = _O_STRATEGY,
    strategy_map: str = _O_STRATEGY_MAP,
    interval: int = _O_INTERVAL,
    iterations: int = _O_ITERATIONS,
    level: bool = _O_LEVEL,
    hedge: bool = _O_HEDGE,
    intel_overlay: bool = _O_INTEL,
    overlay_refresh: float = _O_INTEL_REFRESH,
    require_card: bool = _O_CARD,
    card_shim_port: int = _O_CARD_PORT,
    card_ttl: float = _O_CARD_TTL,
    require_basket: bool = _O_BASKET,
    spectral_var_cap: float = _O_SPECTRAL_CAP,
    spectral_var_level: float = _O_SPECTRAL_LEVEL,
    spectral_var_horizon: int = _O_SPECTRAL_HORIZON,
    futures: str = _O_FUTURES,
    transport: str = _O_TRANSPORT,
    card_shim_host: str = _O_CARD_HOST,
    card_attach: bool = _O_CARD_ATTACH,
    warmup_interval: int = _O_WARMUP_INTERVAL,
    warmup_minutes: float = _O_WARMUP_MINUTES,
    profit_lock: bool = _O_PROFIT_LOCK,
    profit_lock_exempt: str = _O_PROFIT_LOCK_EXEMPT,
    candle_exit: bool = _O_CANDLE_EXIT,
    candle_exit_exempt: str = _O_CANDLE_EXIT_EXEMPT,
    overbought_exit: bool = _O_OVERBOUGHT_EXIT,
    overbought_exit_exempt: str = _O_OVERBOUGHT_EXIT_EXEMPT,
    oversold_entry: bool = _O_OVERSOLD_ENTRY,
    oversold_entry_only: str = _O_OVERSOLD_ENTRY_ONLY,
    parallel_sizing: bool = _O_PARALLEL,
    mute_alerts: str = _O_MUTE,
    max_cost_ratio: float = _O_COST,
    max_positions: int = _O_MAX_POSITIONS,
    params: str = _O_PARAMS,
    sizing_v2: bool = _O_SIZING_V2,
    allow_screened: bool = typer.Option(
        False, "--allow-screened", help="Kraken: trade sleeve symbols that are in-sample only and marked paper-only.",
    ),
) -> None:
    """LIVE trading with real money. Reads `state/HALTED` for kill-switch.

    Built by the same code as `trading paper`, with the same flags and `config/trading.yaml` knobs. What differs
    is what has to: the real broker, the typed confirmation, and a ledger whose failed write stops the order.
    """
    given = locals()
    _run_session(
        get_settings(), iterations=iterations, venue=venue, mode="live", symbols=symbols, strategy=strategy,
        strategy_map=strategy_map, futures=_roots(futures), transport=transport, live_confirmation=confirm,
        label=f"[{venue}-live]",
        options=_options_from(given, intel_refresh=overlay_refresh),
    )


@app.command()
def doctor() -> None:
    """What each engine layer has and lacks, by layer. Shows whether a secret is set, never its value."""
    from .monitor.doctor import missing_for_money, wiring_rows
    settings = get_settings()
    table = Table(title="Engine wiring")
    for col in ("Layer", "Item", "State", "Needed for"):
        table.add_column(col)
    for row in wiring_rows(settings):
        table.add_row(row.layer, row.item, row.state, row.needed_for)
    console.print(table)
    for venue, missing in missing_for_money(settings).items():
        state = "needs " + ", ".join(missing) if missing else ("no keys, log in to the gateway" if venue == "ib" else "keys set")
        console.print(f"{venue} money session: {state}")


@app.command()
def kill(
    reason: str = typer.Option("manual", help="Reason recorded in state/HALTED"),
    from_paper: bool = typer.Option(False, "--from-paper", help="Approve a paper halt stopping money too. Needs --confirm."),
    confirm: str = typer.Option("", help='With --from-paper, must equal: HALT MONEY'),
) -> None:
    """Trip the kill-switch immediately. While tripped the router takes nothing but closing orders."""
    settings = get_settings()
    if from_paper:
        try:
            reason = KillSwitch(settings.state_dir).promote_paper_halt(confirm)
        except (PermissionError, ValueError) as e:
            console.print(f"[red]{e}[/red]")
            raise typer.Exit(code=2) from e
    else:
        KillSwitch(settings.state_dir).trip(reason=reason)
    console.print(f"[red]Kill-switch tripped: {reason}[/red]")


@app.command("clear-kill")
def clear_kill(
    ack: str = typer.Option(..., help='Must equal: I HAVE INVESTIGATED'),
    paper: bool = typer.Option(False, "--paper", help="Clear only the paper halt (state/HALTED_PAPER)."),
) -> None:
    """Clear the kill-switch. Requires explicit ack phrase."""
    settings = get_settings()
    try:
        KillSwitch(settings.state_dir, paper=paper).clear(ack)
        console.print("[yellow]Kill-switch cleared.[/yellow]")
    except PermissionError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=2) from e


@app.command()
def positions(venue: str = _O_VENUE) -> None:
    """Show the venue's open positions for the primary account."""
    settings = get_settings()
    broker = _make_broker(settings, venue)
    acc = _account(settings, broker, venue)
    pos = broker.positions(acc)
    if not pos:
        console.print("[dim]No open positions.[/dim]")
        return
    t = Table(title=f"Positions for {acc}")
    for col in ("symbol", "qty", "avg cost", "current px", "open PnL"):
        t.add_column(col)
    for p in pos:
        t.add_row(
            p.symbol,
            f"{p.openQuantity:g}",
            f"{p.averageEntryPrice:.2f}",
            f"{p.currentPrice:.2f}",
            f"{p.openPnl:+.2f}",
        )
    console.print(t)


@app.command("risk-report")
def risk_report(venue: str = _O_VENUE) -> None:
    """Print current portfolio heat + VaR + kill-switch state for one venue's book."""
    settings = get_settings()
    broker = _make_broker(settings, venue)
    acc = _account(settings, broker, venue)
    eq = broker.equity(acc, currency=settings.account_currency)
    pos = broker.positions(acc)
    notional = sum(abs(p.openQuantity) * p.currentPrice for p in pos)
    heat_pct = notional / eq if eq else 0.0
    ks = KillSwitch(settings.state_dir).state()
    t = Table(title="Risk report")
    for col in ("metric", "value"):
        t.add_column(col)
    t.add_row("equity", f"${eq:,.2f}")
    t.add_row("open notional", f"${notional:,.2f}")
    t.add_row("portfolio heat", f"{heat_pct:.2%}  (cap {settings.portfolio_heat_cap:.0%})")
    t.add_row("kill switch", "HALTED" if ks.halted else "ok")
    if ks.halted:
        t.add_row("reason", ks.reason)
    paper_halt = KillSwitch(settings.state_dir, paper=True)
    if paper_halt.paper_only_halt():
        t.add_row("paper halt", f"{paper_halt.state().reason} (money still running)")
    console.print(t)


# ----- tune ------------------------------------------------------------------


def _print_lens_audit(rows: list[TuneResult], events: list[CandidateEvents], *, n_perm: int) -> None:
    """Advisory table under the scoreboard. Reads the results; changes nothing."""
    verdicts = audit_candidates(events, label_horizon=DEFAULT_LABEL_HORIZON, n_perm=n_perm)
    table = Table(title="Lens audit (advisory only: does not affect ranking or trading.yaml)")
    for col in ("strategy", "symbol", "score", "transfer to other symbols",
                "persistence in own history", "entries"):
        table.add_column(col)
    thinned = 1
    for r in rows:
        v = verdicts.get((r.strategy, r.symbol))
        thinned = max(thinned, v.thinned_by) if v else thinned
        table.add_row(r.strategy, r.symbol, f"{r.score:.2f}",
                      lens_cell(v.reliability if v else None),
                      lens_cell(v.future if v else None),
                      str(v.n_events) if v else "0")
    console.print(table)
    console.print(
        "[dim]transfer: entry-state to forward-return, read from this strategy's entries on other "
        "symbols. persistence: read from this symbol's own already-matured history. 'no' means not "
        "distinguishable from chance, which is a limit on the evidence, not proof there is no edge. "
        f"n/a means too few entries to say.{f' Entries thinned to every {thinned}th.' if thinned > 1 else ''}"
        "[/dim]"
    )


@app.command()
def tune(
    years: float = typer.Option(5.0, help="Years of history per backtest"),
    symbols: str = typer.Option("", help="Comma-separated symbols (empty = curated default universe)"),
    strategies: str = typer.Option("", help="Comma-separated strategy keys (empty = all)"),
    dry_run: bool = typer.Option(False, help="Score + print, but don't write trading.yaml"),
    parallel: int = typer.Option(4, help="Concurrent backtests"),
    lens_audit: bool = typer.Option(
        False, help="Also print an advisory lens audit; never changes ranking or trading.yaml"),
    lens_permutations: int = typer.Option(
        500, help="Permutation draws per lens verdict (more = slower, finer p-values)"),
) -> None:
    """Backtest a strategy x symbol grid; write the winning config to config/trading.yaml."""
    settings = get_settings()
    cache = CandleCache(settings.data_cache_dir)

    sym_list = [s.strip().upper() for s in symbols.split(",") if s.strip()] or list(DEFAULT_TUNE_UNIVERSE)
    broker = _make_broker(settings, _venue_of(sym_list))
    strat_list = [s.strip() for s in strategies.split(",") if s.strip()] or list(DEFAULT_TUNE_STRATEGIES)

    console.print(
        f"[bold]Tuning[/bold] {len(strat_list)} strategies x {len(sym_list)} symbols, "
        f"years={years}, parallel={parallel}"
    )
    audit_sink: list[CandidateEvents] | None = [] if lens_audit else None
    results = run_tune(broker, cache, symbols=sym_list, strategies=strat_list, years=years,
                       parallel=parallel, audit_sink=audit_sink)
    if not results:
        console.print("[red]No backtests succeeded.[/red]")
        raise typer.Exit(code=1)

    table = Table(title="Scoreboard (top 15) — scored by Sortino / |maxDD|")
    for col in ("strategy", "symbol", "Sortino", "Sharpe", "MaxDD", "CAGR", "Win%", "trades", "score"):
        table.add_column(col)
    for r in results[:15]:
        table.add_row(
            r.strategy,
            r.symbol,
            f"{r.sortino:.2f}",
            f"{r.sharpe:.2f}",
            f"{r.max_drawdown:.2%}",
            f"{r.cagr:.2%}",
            f"{r.win_rate:.2%}",
            str(r.num_trades),
            f"{r.score:.2f}",
        )
    console.print(table)

    if audit_sink is not None:
        try:
            _print_lens_audit(results[:15], audit_sink, n_perm=lens_permutations)
        except Exception as exc:  # advisory only: it must never stop the tune from completing
            log.warning("tune.lens_audit.failed", error=str(exc))
            console.print(f"[yellow]Lens audit skipped ({exc}). Ranking and trading.yaml unaffected.[/yellow]")

    update = apply_tune(results, dry_run=dry_run)
    if update is None:
        console.print(
            "[yellow]No combo passed filters (>= 15 trades, max DD >= -20%). trading.yaml unchanged.[/yellow]"
        )
        raise typer.Exit(code=1)
    if dry_run:
        console.print("[yellow]--dry-run: trading.yaml NOT written.[/yellow]")
    else:
        console.print(
            f"[green]trading.yaml updated:[/green] strategy={update['default_strategy']}  "
            f"symbols={update['default_symbols']}"
        )


@app.command(name="qc-backtest")
def qc_backtest(
    project: str = typer.Option("frm-claude-backtest", help="QuantConnect project name to create"),
    algorithm: str = typer.Option("", help="Path to a LEAN algorithm .py (empty = bundled EMA-cross starter)"),
    name: str = typer.Option("frm-claude run", help="Backtest name"),
    timeout: float = typer.Option(900.0, help="Seconds to wait for the backtest to finish"),
) -> None:
    """Push a LEAN algorithm to QuantConnect, compile, backtest, and print stats.

    Runs on QuantConnect's cloud via the REST API (needs QUANTCONNECT_USER_ID /
    QUANTCONNECT_API_TOKEN in .env). This drives a LEAN algorithm — it does NOT
    translate this repo's pandas strategies. Pass --algorithm to use your own
    LEAN file; otherwise a bundled EMA-cross starter is used.
    """
    settings = get_settings()
    client = _make_qc(settings)

    if algorithm:
        path = Path(algorithm)
        if not path.is_file():
            console.print(f"[red]Algorithm file not found: {path}[/red]")
            raise typer.Exit(code=2)
        content = path.read_text(encoding="utf-8")
    else:
        content = DEFAULT_LEAN_ALGORITHM

    try:
        project_id, backtest_id, result = _run_qc_flow(client, project, content, name, timeout)
    except QuantConnectError as e:
        console.print(f"[red]QuantConnect error: {e}[/red]")
        raise typer.Exit(code=1) from e
    finally:
        client.close()

    final = result.get("backtest", {})
    stats = final.get("statistics", {}) if isinstance(final, dict) else {}
    table = Table(title=f"QuantConnect backtest — {name}")
    table.add_column("statistic")
    table.add_column("value", justify="right")
    if isinstance(stats, dict) and stats:
        for k, v in stats.items():
            table.add_row(str(k), str(v))
    else:
        table.add_row("(no statistics returned)", "-")
    console.print(table)
    console.print(f"[green]Done.[/green] project={project_id} backtest={backtest_id}")


@app.command(name="qc-library")
def qc_library(
    pull: int = typer.Option(0, help="Project id to pull LEAN source for (0 = just list)"),
    analyze: bool = typer.Option(False, help="Read source to detect indicators/asset-class/family"),
) -> None:
    """List the QuantConnect projects in your account (your cloned strategy library).

    Alpha Streams is retired and the global library has no search API, so this reads
    your own account's projects via the base API. --pull <id> prints LEAN source;
    --analyze reads every project's code and detects its signals + family.
    """
    settings = get_settings()
    client = _make_qc(settings)
    try:
        if pull:
            sources = pull_algorithm(client, pull)
            if not sources:
                console.print(f"[yellow]No files in project {pull}.[/yellow]")
                raise typer.Exit(code=1)
            for fname, src in sources.items():
                console.print(f"[bold cyan]--- {fname} ---[/bold cyan]")
                console.print(src)
            return

        if analyze:
            analyses = analyze_library(client)
            if not analyses:
                console.print("[yellow]No projects found.[/yellow]")
                raise typer.Exit(code=1)
            table = Table(title=f"QC library — signal analysis ({len(analyses)} projects)")
            for col in ("projectId", "name", "family", "indicators", "assets", "symbols"):
                table.add_column(col)
            for a in sorted(analyses, key=lambda x: x.family):
                table.add_row(
                    str(a.project_id), a.name[:32], a.family,
                    ", ".join(a.indicators) or "-",
                    ", ".join(a.asset_classes) or "-",
                    ", ".join(a.symbols[:4]) or "-",
                )
            console.print(table)
            return

        strategies = list_library(client)
    finally:
        client.close()

    if not strategies:
        console.print("[yellow]No projects found in your QuantConnect account.[/yellow]")
        raise typer.Exit(code=1)
    table = Table(title=f"QuantConnect library ({len(strategies)} projects)")
    for col in ("projectId", "name", "lang", "category"):
        table.add_column(col)
    for s in sorted(strategies, key=lambda x: x.category):
        table.add_row(str(s.project_id), s.name[:60], s.language, s.category)
    console.print(table)


@app.command()
def optimize(
    strategy: str = typer.Option(..., help="Strategy key (must have a parameter grid)"),
    symbol: str = typer.Option(..., help="Ticker, e.g. ATD.TO"),
    objective: str = typer.Option("sortino_over_dd", help="Objective to optimize (scoring.objective registry)"),
    years: float = typer.Option(5.0, help="Years of history"),
) -> None:
    """Grid-search a strategy's tunable parameters on one symbol; print the best combos.

    Free, native alternative to QC's paid cloud optimizer — sweeps the strategy's knobs
    over the venue's data and ranks by the chosen objective (default Sortino/|maxDD|).
    """
    if strategy not in PARAM_GRIDS:
        console.print(f"[red]No parameter grid for '{strategy}'. Available: {sorted(PARAM_GRIDS)}[/red]")
        raise typer.Exit(code=2)
    if objective not in OBJECTIVES:
        console.print(f"[red]Unknown objective '{objective}'. Options: {sorted(OBJECTIVES)}[/red]")
        raise typer.Exit(code=2)

    settings = get_settings()
    broker = _make_broker(settings, _venue_of([symbol.upper()]))
    cache = CandleCache(settings.data_cache_dir)
    results = optimize_parameters(
        broker, cache, strategy=strategy, symbol=symbol, objective=objective, years=years
    )
    if not results:
        console.print("[red]No results (insufficient data or no combo met the trade minimum).[/red]")
        raise typer.Exit(code=1)

    table = Table(title=f"Parameter optimization — {strategy} on {symbol} ({objective}, {len(results)} combos)")
    for col in ("params", "Sortino", "Sharpe", "MaxDD", "trades", "score"):
        table.add_column(col)
    for r in results[:15]:
        table.add_row(
            str(r.params), f"{r.sortino:.2f}", f"{r.sharpe:.2f}",
            f"{r.max_drawdown:.1%}", str(r.num_trades), f"{r.score:.2f}",
        )
    console.print(table)
    best = results[0]
    console.print(f"[green]Best params:[/green] {best.params}  (score {best.score:.2f}, Sortino {best.sortino:.2f})")


@app.command(name="signal-matrix")
def signal_matrix(
    symbols: str = typer.Option("", help="Comma-separated symbols (empty = curated tune universe)"),
    strategies: str = typer.Option("", help="Comma-separated strategies (empty = all single-symbol)"),
    years: float = typer.Option(5.0, help="Years of history per symbol"),
    horizon: int = typer.Option(10, help="Label horizon in bars (the forward window)"),
    up_threshold: float = typer.Option(0.03, help="Forward-return threshold that defines a real move"),
    rank: str = typer.Option(
        "", help="Rank cells by an objective (e.g. dot_product, roc_auc, precision); empty = sensitivity x specificity"
    ),
    rank_weights: str = typer.Option(
        "", help="Custom dot_product axis weights, e.g. 'precision=0.4,fidelity=0.3,sensitivity=0.1,specificity=0.1,risk=0.1'"
    ),
) -> None:
    """Build the sensitivity x specificity x risk report across strategy x universe.

    Sensitivity (recall) = real +move events caught; specificity = non-moves avoided;
    risk = max drawdown. Writes reports/signal_matrix.md and prints the top cells.
    Use --rank <objective> to order by any scoring objective (5-D dot_product, roc_auc,
    …); --rank-weights overrides the dot_product axis weights for bespoke ranking.
    """
    if rank and rank not in OBJECTIVES:
        console.print(f"[red]Unknown --rank objective '{rank}'. Options: {sorted(OBJECTIVES)}[/red]")
        raise typer.Exit(code=2)

    weights: dict[str, float] | None = None
    if rank_weights:
        if rank and rank != "dot_product":
            console.print(f"[red]--rank-weights only applies to dot_product, not '{rank}'.[/red]")
            raise typer.Exit(code=2)
        known = set(DEFAULT_METRIC_WEIGHTS)
        weights = {}
        for pair in rank_weights.split(","):
            pair = pair.strip()
            if not pair:
                continue
            if "=" not in pair:
                console.print(f"[red]Bad --rank-weights entry '{pair}'. Use axis=weight.[/red]")
                raise typer.Exit(code=2)
            axis, val = pair.split("=", 1)
            axis = axis.strip()
            if axis not in known:
                console.print(f"[red]Unknown axis '{axis}'. Known axes: {sorted(known)}[/red]")
                raise typer.Exit(code=2)
            weights[axis] = float(val)
        rank = "dot_product"  # weights imply the dot-product objective
    settings = get_settings()
    sym_list = [s.strip().upper() for s in symbols.split(",") if s.strip()] or list(DEFAULT_TUNE_UNIVERSE)
    broker = _make_broker(settings, _venue_of(sym_list))
    market = MarketData(broker, cache=CandleCache(settings.data_cache_dir))
    strat_list = [s.strip() for s in strategies.split(",") if s.strip()] or None

    frames: dict[str, pd.DataFrame] = {}
    for sym in sym_list:
        try:
            frames[sym] = market.history(symbol=sym, years=years, interval="1d")
        except Exception as e:
            console.print(f"[yellow]skip {sym}: {e}[/yellow]")

    cells = build_signal_matrix(frames, strategies=strat_list, horizon=horizon, up_threshold=up_threshold)
    if not cells:
        console.print("[red]No cells produced (no symbol had enough history).[/red]")
        raise typer.Exit(code=1)

    out = _DEFAULT_REPORTS_DIR / "signal_matrix.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_matrix_markdown(cells), encoding="utf-8")

    if rank:
        objective_fn = make_dot_product(weights) if weights is not None else rank
        ranked = rank_cells(cells, objective=objective_fn)[:15]
        label = f"dot_product {weights}" if weights is not None else rank
        title = f"Signal matrix — ranked by {label} ({len(cells)} cells)"
        table = Table(title=title)
        for col in ("score", "strategy", "symbol", "sens", "spec", "prec", "auc", "fid", "maxDD"):
            table.add_column(col)
        for c, sc in ranked:
            table.add_row(
                f"{sc:.3f}", c.strategy, c.symbol, f"{c.recall:.1%}", f"{c.specificity:.1%}",
                f"{c.precision:.1%}", f"{c.roc_auc:.2f}", f"{c.fidelity:+.2f}", f"{c.max_drawdown:.1%}",
            )
    else:
        top = sorted(cells, key=lambda c: (c.recall * c.specificity, -abs(c.max_drawdown)), reverse=True)[:15]
        table = Table(title=f"Signal matrix — sensitivity x specificity x risk ({len(cells)} cells)")
        for col in ("strategy", "symbol", "sens", "spec", "prec", "auc", "fid", "maxDD"):
            table.add_column(col)
        for c in top:
            table.add_row(
                c.strategy, c.symbol, f"{c.recall:.1%}", f"{c.specificity:.1%}", f"{c.precision:.1%}",
                f"{c.roc_auc:.2f}", f"{c.fidelity:+.2f}", f"{c.max_drawdown:.1%}",
            )
    console.print(table)
    console.print(f"[green]Report written:[/green] {out}")


@app.command(name="route")
def route(
    symbols: str = typer.Option("", help="Comma-separated symbols (empty = curated tune universe)"),
    strategies: str = typer.Option("", help="Comma-separated strategies (empty = all single-symbol)"),
    objective: str = typer.Option("dot_product", help="Objective to route by (dot_product, roc_auc, precision, …)"),
    years: float = typer.Option(5.0, help="Years of history per symbol"),
    top_n: int = typer.Option(3, help="Assets to list per strategy"),
    min_score: float = typer.Option(0.0, help="Drop symbols whose best score is below this floor"),
) -> None:
    """Strategy-conditioned asset selection + per-symbol routing.

    Prints (1) each strategy's best assets and (2) each symbol routed to its single
    best strategy, plus a ready-to-paste --strategy-map string for `trading signal`.
    """
    if objective not in OBJECTIVES:
        console.print(f"[red]Unknown objective '{objective}'. Options: {sorted(OBJECTIVES)}[/red]")
        raise typer.Exit(code=2)

    settings = get_settings()
    sym_list = [s.strip().upper() for s in symbols.split(",") if s.strip()] or list(DEFAULT_TUNE_UNIVERSE)
    broker = _make_broker(settings, _venue_of(sym_list))
    market = MarketData(broker, cache=CandleCache(settings.data_cache_dir))
    strat_list = [s.strip() for s in strategies.split(",") if s.strip()] or None

    frames: dict[str, pd.DataFrame] = {}
    for sym in sym_list:
        try:
            frames[sym] = market.history(symbol=sym, years=years, interval="1d")
        except Exception as e:
            console.print(f"[yellow]skip {sym}: {e}[/yellow]")

    cells = build_signal_matrix(frames, strategies=strat_list)
    if not cells:
        console.print("[red]No cells produced.[/red]")
        raise typer.Exit(code=1)

    floor = min_score if min_score != 0.0 else None
    plan = strategy_asset_plan(cells, objective=objective, top_n=top_n, min_score=floor)
    routing = route_symbols_to_strategies(cells, objective=objective, min_score=floor)

    sel = Table(title=f"Asset selection by strategy — {objective}")
    for col in ("strategy", "family", "best assets (score)"):
        sel.add_column(col)
    for strat, entries in plan.items():
        if not entries:
            continue
        assets = "  ".join(f"{e.symbol}({e.score:.2f})" for e in entries)
        sel.add_row(strat, entries[0].family, assets)
    console.print(sel)

    rte = Table(title=f"Routing — symbol → best strategy ({len(routing)} routed)")
    for col in ("symbol", "strategy", "family", "score"):
        rte.add_column(col)
    for e in sorted(routing.values(), key=lambda e: e.score, reverse=True):
        rte.add_row(e.symbol, e.strategy, e.family, f"{e.score:.3f}")
    console.print(rte)

    map_str = to_strategy_map_string(routing)
    console.print("\n[bold]Deploy to the monitor:[/bold]")
    console.print(f"[dim]trading signal --strategy rsi_meanrevert --symbols \"{','.join(routing)}\" --strategy-map \"{map_str}\"[/dim]")


@app.command(name="qc-seed")
def qc_seed(
    dry_run: bool = typer.Option(False, help="Show the plan without creating projects"),
    limit: int = typer.Option(0, help="Max projects to create (0 = all)"),
) -> None:
    """Seed the QC account with a comprehensive, tunable strategy set across all domains.

    Creates one detectable LEAN project per template spanning momentum, mean-reversion,
    volatility, seasonality, and every QC-mappable candlestick pattern. All knobs are
    exposed via self.GetParameter(...) so you can run QC parameter optimization + your
    own universe selection over the pool later.
    """
    from collections import Counter

    algos = list(comprehensive_lean_algorithms().items())
    if limit:
        algos = algos[:limit]
    by_family = Counter(fam for _, (fam, _) in algos)
    console.print(f"[bold]Seeding {len(algos)} tunable strategies[/bold] by family: {dict(by_family)}")

    settings = get_settings()
    client = _make_qc(settings)
    try:
        existing = {str(p.get("name", "")) for p in client.list_projects()}
        for proj_name, (fam, source) in algos:
            if proj_name in existing:
                console.print(f"[dim]{fam}: '{proj_name}' already exists — skip[/dim]")
                continue
            if dry_run:
                console.print(f"[dim]{fam}: would create '{proj_name}'[/dim]")
                continue
            resp = client.create_project(proj_name, language="Py")
            projects = resp.get("projects")
            pid = 0
            if isinstance(projects, list) and projects and isinstance(projects[0], dict):
                pid = int(str(projects[0].get("projectId", 0)))
            client.put_file(pid, "main.py", source)
            console.print(f"[green]{fam:14}[/green] {proj_name} → project {pid}")
    except QuantConnectError as e:
        console.print(f"[red]QuantConnect error: {e}[/red]")
        raise typer.Exit(code=1) from e
    finally:
        client.close()

    if not dry_run:
        console.print("[dim]Re-run `trading qc-ingest` to see the full pool + coverage.[/dim]")


@app.command(name="qc-fill-gaps")
def qc_fill_gaps(
    dry_run: bool = typer.Option(False, help="Show what would be created without creating"),
) -> None:
    """Create gap-filling strategies in your QC account, one per missing family.

    QC's base API has no clone-from-library call, so this generates a detectable LEAN
    strategy for each family with no QC coverage (mean-reversion / volatility /
    seasonality / candlestick) and creates it as a project. Re-run `qc-ingest` after.
    """
    settings = get_settings()
    client = _make_qc(settings)
    try:
        analyses = analyze_library(client)
        coverage = family_coverage(STRATEGIES.keys(), [a.family for a in analyses])
        gaps = {fc.family for fc in coverage if fc.gap and fc.family != "other"}
        templates = gap_family_algorithms()
        to_create = {fam: nv for fam, nv in templates.items() if fam in gaps}

        if not to_create:
            console.print("[green]No gap families with an available template — nothing to create.[/green]")
            return

        console.print(f"[bold]Filling {len(to_create)} gap families:[/bold] {sorted(to_create)}")
        for fam, (proj_name, source) in to_create.items():
            if dry_run:
                console.print(f"[dim]{fam}: would create '{proj_name}'[/dim]")
                continue
            resp = client.create_project(proj_name, language="Py")
            projects = resp.get("projects")
            pid = 0
            if isinstance(projects, list) and projects and isinstance(projects[0], dict):
                pid = int(str(projects[0].get("projectId", 0)))
            client.put_file(pid, "main.py", source)
            console.print(f"[green]{fam}[/green] → created '{proj_name}' (project {pid})")
    except QuantConnectError as e:
        console.print(f"[red]QuantConnect error: {e}[/red]")
        raise typer.Exit(code=1) from e
    finally:
        client.close()

    if not dry_run:
        console.print("[dim]Re-run `trading qc-ingest` to see the gaps filled.[/dim]")


@app.command(name="qc-ingest")
def qc_ingest(
    symbols: str = typer.Option("", help="Universe for the native pool (empty = curated tune universe)"),
    with_native: bool = typer.Option(True, help="Also build the native scoreboard and merge into one ranked pool"),
    objective: str = typer.Option("dot_product", help="Objective for native scoring / combined ranking"),
) -> None:
    """Ingest cloned QC strategies, categorize + rank them, and report diversity gaps.

    Reads every project in your QC account, detects its family from the code, ranks it
    by its QC backtest, and (with --with-native) merges it with the native 36 into one
    ranked pool. The family-coverage table flags which families have no QC coverage —
    the ones to clone more of before going live.
    """
    settings = get_settings()
    client = _make_qc(settings)
    try:
        analyses = analyze_library(client)
        qc_scores = rank_qc_library(client, objective="sortino_over_dd")
    except QuantConnectError as e:
        console.print(f"[red]QuantConnect error: {e}[/red]")
        raise typer.Exit(code=1) from e
    finally:
        client.close()

    if not analyses:
        console.print("[yellow]No QC projects found to ingest.[/yellow]")
        raise typer.Exit(code=1)

    ingest = Table(title=f"Ingested QC strategies ({len(analyses)} projects)")
    for col in ("projectId", "name", "family", "indicators"):
        ingest.add_column(col)
    for a in sorted(analyses, key=lambda x: x.family):
        ingest.add_row(str(a.project_id), a.name[:30], a.family, ", ".join(a.indicators) or "-")
    console.print(ingest)

    # Family-coverage gap report: native vs ingested QC.
    coverage = family_coverage(STRATEGIES.keys(), [a.family for a in analyses])
    cov = Table(title="Family coverage — native vs QC (gap = no QC strategy in that family)")
    for col in ("family", "native", "qc", "gap"):
        cov.add_column(col)
    for fc in coverage:
        cov.add_row(fc.family, str(fc.native), str(fc.qc), "[red]GAP[/red]" if fc.gap else "ok")
    console.print(cov)
    gaps = [fc.family for fc in coverage if fc.gap and fc.family != "other"]
    if gaps:
        console.print(f"[yellow]Clone QC strategies for these thin families before live:[/yellow] {gaps}")

    if with_native:
        sym_list = [s.strip().upper() for s in symbols.split(",") if s.strip()] or list(DEFAULT_TUNE_UNIVERSE)
        broker = _make_broker(settings, _venue_of(sym_list))
        market = MarketData(broker, cache=CandleCache(settings.data_cache_dir))
        frames: dict[str, pd.DataFrame] = {}
        for sym in sym_list:
            try:
                frames[sym] = market.history(symbol=sym, years=5.0, interval="1d")
            except Exception as e:
                console.print(f"[yellow]skip {sym}: {e}[/yellow]")
        native = score_strategies(build_signal_matrix(frames), objective=objective)
        combined = combine_scores(native, qc_scores)
        out = _DEFAULT_REPORTS_DIR / "combined_pool.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(render_combined_scoreboard(combined), encoding="utf-8")
        pool = Table(title=f"Combined pool — native + QC (top 15 of {len(combined)})")
        for col in ("source", "name", "family", "objective", "value"):
            pool.add_column(col)
        for c in combined[:15]:
            pool.add_row(c.source, c.name[:24], c.family, c.objective, f"{c.objective_value:.3f}")
        console.print(pool)
        console.print(f"[green]Combined pool written:[/green] {out}")


def _make_quantpedia(settings):
    from .integrations.quantpedia import QuantpediaClient, QuantpediaError
    try:
        return QuantpediaClient(settings.quantpedia_username, settings.quantpedia_api_key)
    except QuantpediaError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=2) from e


@app.command(name="quantpedia-search")
def quantpedia_search(
    terms: str = typer.Argument("", help="Space-separated terms, e.g. 'commodity futures momentum'"),
    deep: bool = typer.Option(False, help="Match against full metadata (one request per strategy) instead of names"),
    limit: int = typer.Option(40, help="Max rows to show"),
) -> None:
    """Search the Quantpedia strategies visible to your account (read-only)."""
    from .integrations.quantpedia import QuantpediaError, matches

    words = [t for t in terms.split() if t]
    with _make_quantpedia(get_settings()) as qp:
        try:
            listed = qp.list_strategies()
            rows: list[dict] = []
            for s in listed:
                if not deep:
                    if all(w.lower() in str(s.get("name", "")).lower() for w in words):
                        rows.append(s)
                else:
                    detail = qp.strategy(str(s["id"]))
                    if matches(detail, words):
                        rows.append(detail)
                if len(rows) >= limit:
                    break
        except QuantpediaError as e:
            console.print(f"[red]Quantpedia: {e}[/red]")
            raise typer.Exit(code=1) from e
    table = Table(title=f"Quantpedia — {len(rows)} of {len(listed)} strategies match {words or 'all'}")
    for col in ("id", "name", "sharpe (paper)", "instruments", "rebalancing"):
        table.add_column(col)
    for r in rows:
        pm = r.get("paperMetrics") or {}
        table.add_row(str(r.get("id")), str(r.get("name", ""))[:70], str(pm.get("sharpeRatio", "-")),
                      ", ".join(map(str, r.get("instruments") or []))[:30] or "-",
                      str(r.get("rebalancingPeriod", "-")))
    console.print(table)


@app.command(name="quantpedia-show")
def quantpedia_show(strategy_id: str = typer.Argument(..., help="Quantpedia strategy id")) -> None:
    """Show one Quantpedia strategy's metadata, paper metrics and calculated metrics."""
    from .integrations.quantpedia import QuantpediaError

    with _make_quantpedia(get_settings()) as qp:
        try:
            d = qp.strategy(strategy_id)
        except QuantpediaError as e:
            console.print(f"[red]Quantpedia: {e}[/red]")
            raise typer.Exit(code=1) from e
    console.print(f"[bold]{d.get('name')}[/bold]  (id {d.get('id')})")
    console.print(str(d.get("description", ""))[:1200])
    for label in ("instruments", "marketFactors", "regions", "keywords"):
        console.print(f"[cyan]{label}[/cyan]: {', '.join(map(str, d.get(label) or [])) or '-'}")
    for label in ("complexity", "confidence", "crisisHedge", "rebalancingPeriod"):
        console.print(f"[cyan]{label}[/cyan]: {d.get(label, '-')}")
    for group in ("paperMetrics", "calculatedMetrics"):
        table = Table(title=group)
        table.add_column("metric")
        table.add_column("value", justify="right")
        for k, v in (d.get(group) or {}).items():
            table.add_row(str(k), str(v))
        console.print(table)
    paper = d.get("sourcePaper") or {}
    if paper:
        console.print(f"[cyan]source paper[/cyan]: {paper.get('name')} (fileId {paper.get('fileId')})")


@app.command(name="quantpedia-pull")
def quantpedia_pull(
    strategy_id: str = typer.Argument(..., help="Quantpedia strategy id"),
    out: str = typer.Option("reports/quantpedia", help="Output directory"),
) -> None:
    """Save a strategy's metadata, performance curve and backtest source files locally."""
    import json as _json

    from .integrations.quantpedia import QuantpediaError

    target = Path(out) / strategy_id
    with _make_quantpedia(get_settings()) as qp:
        try:
            detail = qp.strategy(strategy_id)
            perf = qp.performance(strategy_id)
            files = qp.source_code(strategy_id)
        except QuantpediaError as e:
            console.print(f"[red]Quantpedia: {e}[/red]")
            raise typer.Exit(code=1) from e
    (target / "source").mkdir(parents=True, exist_ok=True)
    (target / "strategy.json").write_text(_json.dumps(detail, indent=2), encoding="utf-8")
    perf.rename("performance").to_csv(target / "performance.csv", index_label="date")
    for f in files:
        name = Path(str(f.get("file_name", "file.txt"))).name
        (target / "source" / name).write_text(str(f.get("content", "")), encoding="utf-8")
    span = f"{perf.index.min().date()} → {perf.index.max().date()}" if len(perf) else "empty"
    console.print(f"[green]Saved[/green] {target}: metadata, {len(perf)} performance points ({span}), "
                  f"{len(files)} source file(s).")


@app.command(name="qc-rank")
def qc_rank(
    objective: str = typer.Option("sortino_over_dd", help="Objective to rank by (scoring.objective registry)"),
    min_orders: int = typer.Option(20, help="Skip backtests with fewer QC 'Total Orders' than this."),
) -> None:
    """Rank your QC-library strategies by their latest backtest, via the objective adapter.

    Reads each project's most recent completed backtest and scores it (Sortino / drawdown by
    default), skipping errored backtests and those with fewer than --min-orders orders.
    Projects without a saved backtest are skipped. Family is detected from source code so
    these rank in the same taxonomy as native strategies.
    """
    settings = get_settings()
    client = _make_qc(settings)
    try:
        scores = rank_qc_library(client, objective=objective, min_orders=min_orders)
    except QuantConnectError as e:
        console.print(f"[red]QuantConnect error: {e}[/red]")
        raise typer.Exit(code=1) from e
    finally:
        client.close()

    if not scores:
        console.print(
            "[yellow]No QC projects with completed backtests to rank.[/yellow] "
            "Run a backtest on a project in QuantConnect (or via qc-deploy), then retry."
        )
        raise typer.Exit(code=1)

    table = Table(title=f"QC library ranking — {objective} ({len(scores)} projects)")
    for col in ("rank", "projectId", "name", "family", "Sharpe", "DD", "objective"):
        table.add_column(col)
    for i, s in enumerate(scores, start=1):
        table.add_row(
            str(i), str(s.project_id), s.name[:32], s.family,
            f"{s.sharpe:.2f}", f"{s.drawdown:.2%}", f"{s.objective_value:.3f}",
        )
    console.print(table)


@app.command(name="qc-deploy")
def qc_deploy(
    asset_class: str = typer.Option("equity", help=f"Asset class: one of {list(ASSET_CLASSES)}"),
    symbols: str = typer.Option("SPY", help="Comma-separated symbols (first is used as primary)"),
    project: str = typer.Option("frm-claude-deploy", help="QuantConnect project name"),
    name: str = typer.Option("frm-claude deploy", help="Backtest name"),
    dry_run: bool = typer.Option(True, help="Dry-run = generate+compile+backtest only (no live)"),
    timeout: float = typer.Option(900.0, help="Seconds to wait for the backtest"),
    pattern: str = typer.Option("", help="Candlestick pattern to trade via QC CandlestickPatterns (e.g. hammer)"),
) -> None:
    """Route an asset class to its brokerage, generate a LEAN algo, and dry-run it.

    The asset-class→brokerage router picks the LEAN venue; the generator emits a
    matching subscription (AddEquity/AddFuture/AddCrypto). --dry-run (default) runs a
    cloud backtest only. LIVE deploy is intentionally gated (see below).
    """
    if asset_class not in ASSET_CLASSES:
        console.print(f"[red]Unknown asset class '{asset_class}'. Choose from {list(ASSET_CLASSES)}.[/red]")
        raise typer.Exit(code=2)

    router = AssetRouter()
    decision = router.route(asset_class)
    console.print(
        f"[bold]Routing[/bold] {asset_class} → brokerage [cyan]{decision.brokerage}[/cyan] "
        f"(LEAN {decision.add_method}{', ' + decision.market if decision.market else ''})"
    )

    if not dry_run:
        console.print(
            "[red]LIVE deploy is gated.[/red] Live LEAN execution needs a QuantConnect "
            "live node + a connected brokerage that supports this asset class "
            f"([cyan]{decision.brokerage}[/cyan]). Set that up in QuantConnect, then deploy "
            "from there. This command only performs cloud dry-run backtests."
        )
        raise typer.Exit(code=2)

    sym_list = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    if not sym_list:
        console.print("[red]No symbols given.[/red]")
        raise typer.Exit(code=2)
    if pattern:
        if pattern not in LEAN_CANDLESTICK_MAP:
            console.print(
                f"[red]Pattern '{pattern}' has no LEAN equivalent. QC-deployable: "
                f"{sorted(LEAN_CANDLESTICK_MAP)}[/red]"
            )
            raise typer.Exit(code=2)
        console.print(f"[dim]using QC CandlestickPatterns.{LEAN_CANDLESTICK_MAP[pattern]} for '{pattern}'[/dim]")
        content = render_candlestick_lean_algorithm(
            pattern=pattern, symbol=sym_list[0], add_method=decision.add_method, market=decision.market
        )
    else:
        content = render_lean_algorithm(
            symbol=sym_list[0], add_method=decision.add_method, market=decision.market
        )

    settings = get_settings()
    client = _make_qc(settings)
    try:
        project_id, backtest_id, result = _run_qc_flow(client, project, content, name, timeout)
    except QuantConnectError as e:
        console.print(f"[red]QuantConnect error: {e}[/red]")
        raise typer.Exit(code=1) from e
    finally:
        client.close()

    final = result.get("backtest", {})
    stats = final.get("statistics", {}) if isinstance(final, dict) else {}
    table = Table(title=f"QC dry-run — {asset_class}:{sym_list[0]} via {decision.brokerage}")
    table.add_column("statistic")
    table.add_column("value", justify="right")
    if isinstance(stats, dict) and stats:
        for k, v in stats.items():
            table.add_row(str(k), str(v))
    else:
        table.add_row("(no statistics returned)", "-")
    console.print(table)
    console.print(f"[green]Dry-run done.[/green] project={project_id} backtest={backtest_id}")


# ----- autonomous subcommands ------------------------------------------------

autonomous_app = typer.Typer(help="Claude-driven autonomous trading loop. Background daemon process.")
app.add_typer(autonomous_app, name="autonomous")

from .audit.cli import audit_app  # noqa: E402  (registered beside the other sub-apps)

app.add_typer(audit_app, name="audit")


def _build_alerter(settings) -> Alerter:
    return build_alerter(settings)


@autonomous_app.command("status")
def autonomous_status() -> None:
    """Show daemon state, today's trade count, today's notional."""
    settings = get_settings()
    daemon = AutonomousDaemon(settings.state_dir, settings.log_dir)
    st = daemon.status()
    budget = DailyBudget(
        settings.state_dir,
        max_trades_per_day=settings.autonomous_daily_max_trades,
        max_notional_per_day_usd=settings.autonomous_daily_max_notional_usd,
    ).snapshot()
    ks = KillSwitch(settings.state_dir).state()
    t = Table(title="Autonomous daemon")
    for col in ("metric", "value"):
        t.add_column(col)
    t.add_row("running", "yes" if st.running else "no")
    t.add_row("pid", str(st.pid) if st.pid else "-")
    t.add_row("pidfile", str(st.pidfile))
    t.add_row("stale pidfile", "yes" if st.stale else "no")
    t.add_row("kill-switch", "HALTED" if ks.halted else "ok")
    if ks.halted:
        t.add_row("kill reason", ks.reason)
    t.add_row("trades today", f"{budget.trades_today}/{budget.max_trades}")
    t.add_row("notional today", f"${budget.notional_today_usd:,.0f}/${budget.max_notional_usd:,.0f}")
    t.add_row("autonomous_enabled (.env)", str(settings.autonomous_enabled))
    t.add_row("autonomous_account (.env)", settings.autonomous_account)
    t.add_row("interval (s)", str(settings.autonomous_interval_seconds))
    t.add_row("strategy", settings.autonomous_strategy)
    t.add_row("symbols", settings.autonomous_symbols)
    console.print(t)


@autonomous_app.command("start")
def autonomous_start(
    account: str = typer.Option("", help="Override autonomous_account: paper|practice|live"),
    venue: str = typer.Option("", help="Override autonomous_venue: ib|kraken. One broker per daemon."),
) -> None:
    """Spawn the autonomous daemon. Background process; survives this shell."""
    settings = get_settings()
    if not settings.autonomous_enabled:
        console.print(
            "[red]Refusing to start: AUTONOMOUS_ENABLED is false in .env. "
            "Read RISK_DISCLOSURE.md, then set AUTONOMOUS_ENABLED=true manually.[/red]"
        )
        raise typer.Exit(code=2)
    if KillSwitch(settings.state_dir).state().halted:
        console.print("[red]Refusing to start: kill-switch tripped. Clear it manually first.[/red]")
        raise typer.Exit(code=2)

    chosen_account = (account or settings.autonomous_account).lower()
    if chosen_account not in {"paper", "practice", "live"}:
        console.print(f"[red]Invalid account '{chosen_account}'. Use paper, practice or live.[/red]")
        raise typer.Exit(code=2)

    chosen_venue = (venue or settings.autonomous_venue).lower()
    if chosen_venue not in {"ib", "kraken"}:
        console.print(f"[red]Invalid venue '{chosen_venue}'. Use ib or kraken.[/red]")
        raise typer.Exit(code=2)
    if chosen_venue == "kraken" and chosen_account == "practice":
        console.print("[red]Kraken has no practice account. Use --account paper to rehearse it.[/red]")
        raise typer.Exit(code=2)

    extra_env = {
        AUTONOMOUS_ENV_VAR: "true",
        "AUTONOMOUS_ACCOUNT_RUNTIME": chosen_account,
        "AUTONOMOUS_VENUE_RUNTIME": chosen_venue,
    }
    daemon = AutonomousDaemon(settings.state_dir, settings.log_dir)
    argv = [sys.executable, "-m", "trading_live_claude.cli", "autonomous", "run"]
    try:
        pid = daemon.start(argv, extra_env=extra_env)
    except RuntimeError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=2) from e
    console.print(f"[green]Autonomous daemon started (pid={pid}, venue={chosen_venue}, account={chosen_account}).[/green]")
    console.print(f"  stdout log: {daemon.stdout_log}")
    console.print(f"  stderr log: {daemon.stderr_log}")


@autonomous_app.command("stop")
def autonomous_stop() -> None:
    """Stop the autonomous daemon (graceful, with hard-kill fallback)."""
    settings = get_settings()
    daemon = AutonomousDaemon(settings.state_dir, settings.log_dir)
    if daemon.stop():
        console.print("[yellow]Autonomous daemon stopped.[/yellow]")
    else:
        console.print("[dim]No autonomous daemon running.[/dim]")


@autonomous_app.command("run")
def autonomous_run() -> None:
    """Foreground autonomous loop. Normally invoked by `autonomous start`, not by humans."""
    settings = get_settings()
    if not settings.autonomous_enabled:
        console.print("[red]AUTONOMOUS_ENABLED is false. Aborting.[/red]")
        raise typer.Exit(code=2)
    chosen_account = os.environ.get("AUTONOMOUS_ACCOUNT_RUNTIME", settings.autonomous_account).lower()
    if KillSwitch(settings.state_dir, paper=chosen_account == "paper").state().halted:
        console.print("[red]Kill-switch tripped. Aborting.[/red]")
        raise typer.Exit(code=2)
    if chosen_account not in {"paper", "practice", "live"}:
        console.print(f"[red]Invalid AUTONOMOUS_ACCOUNT_RUNTIME='{chosen_account}'.[/red]")
        raise typer.Exit(code=2)

    chosen_venue = os.environ.get("AUTONOMOUS_VENUE_RUNTIME", settings.autonomous_venue).lower()
    if chosen_venue not in {"ib", "kraken"}:
        console.print(f"[red]Invalid AUTONOMOUS_VENUE_RUNTIME='{chosen_venue}'.[/red]")
        raise typer.Exit(code=2)

    # paper is a rehearsal: the daemon's own gates and budget on a simulated broker. practice and live are
    # checked against the login (IB's paper accounts are DU; Kraken has no practice account).
    rehearsal = chosen_account == "paper"
    log.info("autonomous.loop.start", venue=chosen_venue, account=chosen_account, strategy=settings.autonomous_strategy,
             symbols=settings.autonomous_symbols_list, interval=settings.autonomous_interval_seconds)
    _run_session(
        settings, venue=chosen_venue, mode="autonomous", symbols=settings.autonomous_symbols_list,
        strategy=settings.autonomous_strategy, expected_env=None if rehearsal else chosen_account,
        rehearsal=rehearsal, label=f"[autonomous/{chosen_account}]",
    )


@autonomous_app.command("tail")
def autonomous_tail(lines: int = typer.Option(30, help="Lines from each log")) -> None:
    """Print last N lines of stdout/stderr logs from the daemon."""
    settings = get_settings()
    daemon = AutonomousDaemon(settings.state_dir, settings.log_dir)
    for label, p in (("stdout", daemon.stdout_log), ("stderr", daemon.stderr_log)):
        console.print(f"[bold]{label} ({p}):[/bold]")
        if not p.exists():
            console.print("[dim](empty)[/dim]")
            continue
        try:
            content = p.read_text(encoding="utf-8", errors="replace").splitlines()
            for line in content[-lines:]:
                console.print(f"  {line}")
        except OSError as e:
            console.print(f"[red]Could not read: {e}[/red]")


# ----- place-order: the LLM-trader's hands ------------------------------------
#
# This is what the Claude Code session calls when it has done its analysis and
# wants to place a single order. The Router enforces every risk gate before the
# broker sees the order; Claude cannot bypass them regardless of what it
# "decides". On rejection the CLI exits non-zero so Claude can read the failure.


@app.command("place-order")
def place_order(
    symbol: str = typer.Option(..., help="Ticker, e.g. XIC.TO or AAPL"),
    side: str = typer.Option(..., help="buy | sell"),
    shares: int = typer.Option(..., help="Whole-share quantity (broker will reject fractions)"),
    stop: float = typer.Option(..., help="Protective stop price (used by Router sanity gate)"),
    target: float = typer.Option(0.0, help="Optional take-profit reference (informational)"),
    reason: str = typer.Option(..., help='Free-text rationale; logged to state/orders.jsonl'),
    strategy: str = typer.Option("llm-claude", help="Strategy/source tag for the journal"),
    mode: str = typer.Option(
        "auto",
        help='Router mode override: auto|paper|dry-run|live|autonomous. "auto" reads execution_mode.',
    ),
    json_output: bool = typer.Option(False, help="Emit machine-readable JSON instead of text"),
) -> None:
    """Place a single order. Claude calls this after analysis. All risk gates apply."""
    import json as _json

    from .brokers.models import OrderAction
    from .execution.router import OrderIntent

    settings = get_settings()
    chosen_mode = (mode if mode != "auto" else settings.execution_mode).lower()
    if chosen_mode not in {"paper", "dry-run", "live", "autonomous"}:
        console.print(f"[red]Invalid mode {chosen_mode!r}.[/red]")
        raise typer.Exit(code=2)
    if chosen_mode == "live":
        console.print(
            "[red]place-order refuses mode=live (typed confirmation phrase required).[/red] "
            "Use mode=autonomous (with AUTONOMOUS_ENABLED=true) or mode=paper instead."
        )
        raise typer.Exit(code=2)

    side_lower = side.lower().strip()
    if side_lower not in {"buy", "sell"}:
        console.print(f"[red]side must be buy or sell; got {side!r}[/red]")
        raise typer.Exit(code=2)
    action = OrderAction.BUY if side_lower == "buy" else OrderAction.SELL

    venue = _venue_of([symbol.upper()])
    feed = _make_broker(settings, venue, money=chosen_mode == "autonomous")
    broker = feed
    if chosen_mode == "paper":
        broker = PaperBroker(feed=guard_feed(feed, settings), starting_equity=100_000.0,
                             journal_dir=settings.state_dir)
    account_number = _account(settings, broker, venue)
    equity = broker.equity(account_number, currency=settings.account_currency)

    # Approximate existing open-risk dollars (2% of notional as a stand-in stop).
    existing_positions = broker.positions(account_number)
    existing_risk = sum(abs(p.openQuantity) * p.currentPrice * 0.02 for p in existing_positions)
    open_positions_count = sum(1 for p in existing_positions if p.openQuantity != 0)
    open_notional = sum(abs(p.openQuantity) * p.currentPrice for p in existing_positions)

    risk_dollars = abs(shares * (stop - 0.0)) if action == OrderAction.SELL else abs(shares * (0.0 - stop))
    # Better: risk = shares * |entry - stop|. We don't have entry; use latest quote.
    try:
        quote_now = broker.quote(symbol)
    except Exception as e:
        console.print(f"[red]Quote fetch failed for {symbol}: {e}[/red]")
        raise typer.Exit(code=1) from e
    entry_ref = quote_now.mid or quote_now.lastTradePrice or 0.0
    if entry_ref <= 0:
        console.print(f"[red]No usable price for {symbol}.[/red]")
        raise typer.Exit(code=1)
    risk_dollars = abs(shares * (entry_ref - stop))

    intent = OrderIntent(
        symbol=symbol,
        action=action,
        shares=int(shares),
        entry=float(entry_ref),
        stop=float(stop),
        target=float(target) if target > 0 else None,
        strategy=strategy,
        risk_dollars=float(risk_dollars),
        account_number=account_number,
    )

    # One-shot and possibly concurrent with another place-order: a shared stream, locked per row.
    ledger = _session_ledger(settings, stream="manual", mode=chosen_mode, shared=True,
                             strict=chosen_mode == "autonomous")
    router = Router.build_default(
        mode="paper" if chosen_mode == "paper" else ("dry-run" if chosen_mode == "dry-run" else "autonomous"),
        broker=broker,
        state_dir=settings.state_dir,
        **Router.limits_from(settings),
        daily_max_trades=settings.autonomous_daily_max_trades,
        daily_max_notional_usd=settings.autonomous_daily_max_notional_usd,
        ledger=ledger,
    ) if chosen_mode == "autonomous" else Router.build_default(
        mode="paper" if chosen_mode == "paper" else "dry-run",
        broker=broker,
        state_dir=settings.state_dir,
        **Router.limits_from(settings),
        ledger=ledger,
    )

    placed = router.submit(
        intent,
        equity=equity,
        existing_risk=existing_risk,
        open_positions=open_positions_count,
        current_open_notional=open_notional,
    )
    payload = {
        "submitted": placed is not None,
        "order_id": placed.id if placed else None,
        "symbol": symbol,
        "side": side_lower,
        "shares": int(shares),
        "entry_ref": entry_ref,
        "stop": stop,
        "risk_dollars": risk_dollars,
        "mode": chosen_mode,
        "reason": reason,
    }
    if json_output:
        print(_json.dumps(payload))
    else:
        if placed:
            console.print(f"[green]PLACED[/green] {symbol} {side_lower} {shares} (order_id={placed.id})")
        else:
            console.print(f"[red]REJECTED[/red] {symbol} — see state/rejected.jsonl")
        console.print(payload)
    raise typer.Exit(code=0 if placed else 3)


@app.command(name="kraken-l2")
def kraken_l2(
    symbol: str = typer.Option("BTC/USD", help="Kraken pair, e.g. BTC/USD or ETH/USD"),
    depth: int = typer.Option(10, help="Order-book depth to subscribe"),
    messages: int = typer.Option(20, help="Stop after N book messages (0 = run forever)"),
) -> None:
    """Stream real Level-2 depth from Kraken and print microprice / imbalance / OFI (read-only).

    Public market data only — no account, no orders. Needs the optional 'l2' extra (websockets).
    This is the live order-book feed the microstructure execution families were built against.
    """
    import asyncio

    from .microstructure.kraken_l2 import BookUpdate, stream_order_book

    def _show(u: BookUpdate) -> None:
        flag = "" if u.checksum_ok else " [red]CRC!*[/red]"
        console.print(
            f"{u.symbol}  bid/ask={u.book.best_bid.price:.4f}/{u.book.best_ask.price:.4f}  "
            f"micro={u.microprice:.4f}  imb={u.imbalance:+.3f}  ofi={u.ofi:+.4f}{flag}"
        )

    console.print(f"[cyan]streaming Kraken L2[/cyan] {symbol} depth={depth} (read-only)…")
    try:
        asyncio.run(stream_order_book(symbol, depth=depth, on_update=_show,
                                      max_messages=messages or None))
    except KeyboardInterrupt:
        pass
    except ModuleNotFoundError:
        console.print("[yellow]install the 'l2' extra: uv sync --extra l2[/yellow]")


@app.command(name="kraken-mm")
def kraken_mm(
    symbol: str = typer.Option("BTC/USD", help="Kraken pair to make a market in"),
    messages: int = typer.Option(200, help="Stop after N book updates (0 = run forever)"),
    gamma: float = typer.Option(0.1, help="Inventory risk aversion"),
    quote_size: float = typer.Option(0.001, help="Size quoted per side (base units)"),
    inventory_limit: float = typer.Option(0.02, help="Max +/- inventory before standing a side down"),
) -> None:
    """PAPER Avellaneda-Stoikov market maker on the live Kraken book — no orders are placed.

    Quotes are centered on the live microprice, spread by the A-S formula, and skewed by inventory
    and order-flow imbalance. Fills are modeled (we don't post), P&L/inventory are paper. Live
    quoting stays behind the human go-live gate. Needs the optional 'l2' extra.
    """
    import asyncio

    from .microstructure.live_market_maker import MMConfig, MMState, run_paper_market_maker

    cfg = MMConfig(gamma=gamma, quote_size=quote_size, inventory_limit=inventory_limit)

    last: list[MMState] = []

    def _show(s: MMState) -> None:
        last.append(s)
        if s.step % 10 == 0 or s.fills:
            console.print(
                f"[dim]{s.step:4d}[/dim] micro={s.microprice:.2f} vol={s.sigma:.3f} ofi={s.ofi:+.3f}  "
                f"bid/ask={s.bid:.2f}/{s.ask:.2f}  inv={s.inventory:+.4f}  fills={s.fills}  pnl={s.pnl:+.4f}"
            )

    console.print(f"[cyan]PAPER market-making[/cyan] {symbol} (no orders placed)…")
    try:
        mm = asyncio.run(run_paper_market_maker(symbol, on_state=_show, cfg=cfg,
                                                max_messages=messages or None))
        pnl = last[-1].pnl if last else mm.cash
        console.print(f"[green]done[/green] fills={mm.fills} inventory={mm.inventory:+.4f} paper-pnl={pnl:+.4f}")
    except KeyboardInterrupt:
        pass
    except ModuleNotFoundError:
        console.print("[yellow]install the 'l2' extra: uv sync --extra l2[/yellow]")


@app.command(name="xexch-arb")
def xexch_arb(
    venues: str = typer.Option("kraken,coinbase", help="Two of: kraken, coinbase, bitstamp"),
    symbols: str = typer.Option("", help="Optional per-venue overrides, e.g. 'kraken=ETH/USD,coinbase=ETH-USD'"),
    ticks: int = typer.Option(200, help="Stop after N book updates (0 = run forever)"),
    fee_bps: float = typer.Option(10.0, help="Per-leg taker fee in basis points"),
    max_size: float = typer.Option(0.01, help="Max base size per arbitrage"),
) -> None:
    """PAPER cross-exchange arbitrage across two live crypto venues — no orders.

    Streams both order books, and whenever one venue's bid clears the other's ask net of fees,
    books the (paper) spread. Measures the real opportunity; live capture needs funded balances on
    both venues and stays behind the human go-live gate. Needs the optional 'l2' extra.
    """
    import asyncio

    from .microstructure.cross_exchange import XArbConfig, XArbTick, run_cross_exchange_arb

    vs = tuple(v.strip() for v in venues.split(",") if v.strip())
    if len(vs) != 2:
        console.print("[red]--venues needs exactly two, e.g. 'kraken,coinbase'[/red]")
        raise typer.Exit(code=2)
    sym_map: dict[str, str] = {}
    for item in (s for s in symbols.split(",") if s.strip()):
        v, _, sy = item.partition("=")
        sym_map[v.strip()] = sy.strip()

    cfg = XArbConfig(fee_bps=fee_bps, max_size=max_size)
    best = {"edge": -1e9, "n": 0.0}

    def _show(t: XArbTick) -> None:
        if len(t.quotes) < 2:
            return
        best["n"] += 1
        best["edge"] = max(best["edge"], t.edge_bps)
        if t.action != "none" or best["n"] % 25 == 0:
            q = " ".join(f"{v}={b:.6g}/{a:.6g}" for v, (b, a) in t.quotes.items())
            tag = f"  [green]{t.action}[/green] +{t.trade_pnl:.4f}" if t.action != "none" else ""
            console.print(f"edge={t.edge_bps:+.2f}bps  {q}{tag}")

    console.print(f"[cyan]PAPER cross-exchange arb[/cyan] {vs[0]} vs {vs[1]} (no orders)…")
    try:
        eng = asyncio.run(run_cross_exchange_arb(on_tick=_show, venues=(vs[0], vs[1]), symbols=sym_map,
                                                 cfg=cfg, max_ticks=ticks or None))
        console.print(f"[green]done[/green] arb-trades={eng.n_trades} paper-pnl={eng.cum_pnl:+.4f} best-edge={best['edge']:+.2f}bps")
    except KeyboardInterrupt:
        pass
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
    except ModuleNotFoundError:
        console.print("[yellow]install the 'l2' extra: uv sync --extra l2[/yellow]")


@app.command(name="crypto-signal")
def crypto_signal(
    push: bool = typer.Option(True, help="Send to Telegram (needs TELEGRAM_* in .env); --no-push previews to console only"),
    only_signals: bool = typer.Option(True, "--only-signals/--all", help="Only alert ENTRY/EXIT; --all includes HOLD"),
) -> None:
    """Evaluate the crypto sleeve on live Kraken daily data and push live-format alerts to Telegram.

    Same alert format as the equity monitor ('{strategy} {ENTRY|EXIT}: {symbol}' / 'price=… detail=…').
    Read-only / paper — this computes and pushes signals, it never places an order. The detail line
    carries tier='screened' so a reader can see this sleeve isn't walk-forward-validated yet.
    """
    from .analysis.universe import CRYPTO_SLEEVE
    from .data.kraken_ohlc import kraken_ohlc
    from .monitor.alerter import Alerter
    from .strategies import STRATEGIES
    from .strategies.base import StrategyContext

    settings = get_settings()
    alerter = _build_alerter(settings) if push else Alerter(AlertConfig())  # empty config = console only
    if push and not (settings.telegram_bot_token and settings.telegram_chat_id):
        console.print("[yellow]TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set in .env — printing to console only.[/yellow]")

    console.print(f"[cyan]crypto sleeve signals[/cyan] ({len(CRYPTO_SLEEVE)} pairs, live Kraken daily)…")
    sent = 0
    for entry in CRYPTO_SLEEVE.values():
        try:
            df = kraken_ohlc(entry.pair)
        except Exception as exc:
            console.print(f"[yellow]skip {entry.symbol}: {type(exc).__name__}[/yellow]")
            continue
        strat = STRATEGIES[entry.strategy](**dict(entry.params))
        sig = strat.generate_signals(df, StrategyContext(symbol=entry.symbol))
        last = sig.iloc[-1]
        price = float(last["close"])
        kind = "ENTRY" if int(last.get("entry", 0)) == 1 else "EXIT" if int(last.get("exit", 0)) == 1 else "HOLD"
        if only_signals and kind == "HOLD":
            continue
        detail = {"tier": entry.tier, "sleeve": "crypto", "score": entry.screen_score}
        if "signal_strength" in sig:
            detail["strength"] = round(float(last["signal_strength"]), 3)
        alerter.send(f"{entry.strategy} {kind}: {entry.symbol}", f"price={price:.4f} detail={detail}")
        sent += 1
    tgt = "Telegram" if (push and settings.telegram_bot_token and settings.telegram_chat_id) else "console"
    console.print(f"[green]done[/green] {sent} alert(s) → {tgt}")


@app.command(name="intel-overlay")
def intel_overlay(
    demo: bool = typer.Option(False, "--demo", help="Score a synthetic stressed snapshot offline (no network/key)."),
    chart: bool = typer.Option(True, help="Write reports/intel_overlay.png; --no-chart skips it."),
    countries: str = typer.Option("", help="Comma-separated ISO-2 codes to weight news exposure (e.g. US,CA)."),
) -> None:
    """Route live WorldMonitor intelligence into a per-asset-class gross-exposure overlay.

    Read-only and de-risk-only: it computes a risk scalar in [floor, 1] for equity / future /
    commodity / fx / crypto and flags which classes should halt NEW entries. It never places an
    order and never adds exposure. Each read is appended to state/intel_overlay.jsonl so we build our
    own point-in-time history of the overlay over time.
    """
    import asyncio
    import json as _json
    from dataclasses import asdict
    from datetime import UTC, datetime
    from pathlib import Path

    from .intel import IntelSnapshot, RiskOverlay

    if demo:
        # A regional energy/conflict flare: commodities & futures stand down, equity/fx trim,
        # crypto least affected — a legible gradient rather than an everything-floored world.
        snap = IntelSnapshot(global_alert_count=2, global_max_importance=0.72,
                             conflict_events_active=7, natural_disasters_active=1, energy_stress=0.82,
                             market={"equity_vol": 19.0, "crypto_chg": -3.0, "dxy_chg": 0.4})
        console.print("[cyan]intel-overlay[/cyan] (demo synthetic snapshot -- offline)")
    else:
        from .intel.worldmonitor import WorldMonitorClient
        settings = get_settings()
        if not settings.worldmonitor_api_key:
            console.print("[yellow]WORLDMONITOR_API_KEY not set — run with --demo, or add the key.[/yellow]")
            raise typer.Exit(1)
        ccs = tuple(c.strip().upper() for c in countries.split(",") if c.strip())

        async def _fetch() -> IntelSnapshot:
            async with WorldMonitorClient(settings.worldmonitor_api_key) as wm:
                return await wm.snapshot(countries=ccs)

        console.print("[cyan]intel-overlay[/cyan] (live WorldMonitor)…")
        snap = asyncio.run(_fetch())

    decisions = RiskOverlay().evaluate(snap)

    table = Table(title="WorldMonitor risk overlay -- gross scalar by asset class")
    table.add_column("class")
    table.add_column("scalar", justify="right")
    table.add_column("new entries")
    table.add_column("top driver")
    for cls, d in decisions.items():
        colour = "green" if d.scalar >= 0.7 else "yellow" if d.scalar > 0.4 else "red"
        table.add_row(cls, f"[{colour}]{d.scalar:.0%}[/{colour}]",
                      "[red]HALT[/red]" if d.halt_new_entries else "ok",
                      d.reasons[0] if d.reasons else "no elevated risk")
    console.print(table)
    if snap.degraded:
        console.print("[yellow]feed degraded — every class capped conservatively.[/yellow]")

    # journal (build our own point-in-time history)
    rec = {"as_of": datetime.now(UTC).isoformat(), "degraded": snap.degraded,
           "snapshot": {k: v for k, v in asdict(snap).items() if k != "as_of"},
           "decisions": {c: {"scalar": d.scalar, "halt": d.halt_new_entries, "reasons": d.reasons}
                         for c, d in decisions.items()}}
    jl = Path("state/intel_overlay.jsonl")
    jl.parent.mkdir(parents=True, exist_ok=True)
    with jl.open("a", encoding="utf-8") as fh:
        fh.write(_json.dumps(rec) + "\n")
    console.print(f"[dim]journaled -> {jl}[/dim]")

    if chart:
        from .intel.chart import render_overlay_chart
        out = render_overlay_chart(decisions, "reports/intel_overlay.png",
                                   as_of=snap.as_of, degraded=snap.degraded)
        console.print(f"[green]chart[/green] -> {out}")


def _entry() -> None:
    app()


if __name__ == "__main__":
    _entry()
