"""Start a Kraken paper-trading session over the crypto sleeve.

Runs the same LiveMonitor loop the equity ``signal --paper`` command runs, but with
:class:`KrakenBroker` as the market-data feed and per-symbol strategies from ``CRYPTO_SLEEVE``.
Every fill routes through ``PaperBroker(feed=KrakenBroker(...))`` — Kraken is used purely for live
quotes and candles, orders are simulated locally against those quotes, and nothing touches a real
Kraken account. Public data works without API credentials, so a bare ``.env`` is fine to run this.

Journals share the state directory with the equity paper session — ``paper_fills.jsonl``,
``paper_orders.jsonl``, ``paper_equity.csv`` — but each row is stamped with a distinct
``session_id`` (from PaperBroker), so the two runs stay separable in the record.

Reminder about the sleeve. ``CRYPTO_SLEEVE`` is tier=``screened`` (in-sample only), not
walk-forward-validated. Paper-only is the correct posture until the deep-history WF gate clears —
that is next-session item 2.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import asyncio

# Windows console defaults to cp1252, which crashes on structlog's unicode output when an
# exception message contains non-latin-1 chars — the resulting UnicodeEncodeError propagates
# out of log.exception() and kills the paper session mid-poll. Force UTF-8 on stdout/stderr
# so exception logging can never terminate a long-running monitor.
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")    # type: ignore[union-attr]
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")    # type: ignore[union-attr]
    except Exception:
        pass

from trading_live_claude.analysis.universe import CRYPTO_SLEEVE
from trading_live_claude.brokers.kraken import KrakenBroker
from trading_live_claude.brokers.paper import PaperBroker
from trading_live_claude.config import get_settings
from trading_live_claude.data.cache import CandleCache
from trading_live_claude.data.market import MarketData
from trading_live_claude.execution.approval import wire_card_approval
from trading_live_claude.execution.router import Router
from trading_live_claude.intel.interpret import interpret
from trading_live_claude.intel.overlay import IntelSnapshot
from trading_live_claude.intel.routing import OverlayProvider
from trading_live_claude.intel.vs_engine import MarketContext, VSInvestmentEngine
from trading_live_claude.intel.worldmonitor import WorldMonitorClient
from trading_live_claude.monitor.live_loop import LiveMonitor, MonitorEvent, monitor_policy
from trading_live_claude.portfolio.sleeve_bias import sleeve_conviction_bias
from trading_live_claude.risk.sizing import PositionSizer
from trading_live_claude.strategies import STRATEGIES


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=int, default=300,
                    help="Poll interval, seconds. Kraken is 24/7 so this is real all the time.")
    ap.add_argument("--paper-equity", type=float, default=100_000.0)
    ap.add_argument("--require-card", dest="require_card", action="store_true",
                    help="Route every accepted intent through the ApprovalRouter — a physical "
                         "TradeCard (or scripts/approval_card_sim.py) must ACCEPT before the "
                         "order is dispatched. Boots the approval shim on --card-shim-port.")
    ap.add_argument("--card-shim-port", type=int, default=8787)
    ap.add_argument("--require-basket", dest="require_basket", action="store_true",
                    help="Gate ENTRIES on a card-signed basket for this venue (scripts/basket.py, or the "
                         "QuantPort.io BASKET tab). Off by default; exits are never gated.")
    ap.add_argument("--card-ttl", type=float, default=90.0,
                    help="Seconds a card prompt stays live before it auto-EXPIRES.")
    ap.add_argument("--audit-ledger", dest="audit_ledger", action=argparse.BooleanOptionalAction,
                    default=True,
                    help="Record every signal, gate decision, card verdict and order to the "
                         "hash-chained audit ledger under state/ledger (stream kraken). On by "
                         "default; --no-audit-ledger opts out.")
    ap.add_argument("--spectral-var-cap", dest="spectral_var_cap", type=float, default=None,
                    help="Turn on the spectral VaR gate (BSM_Spectral engine): an ENTRY whose "
                         "VaR exceeds this fraction of equity is trimmed to fit, or rejected. "
                         "e.g. 0.005 = 0.5%%. Off by default; exits are never gated.")
    ap.add_argument("--spectral-var-level", dest="spectral_var_level", type=float, default=0.99)
    ap.add_argument("--spectral-var-horizon", dest="spectral_var_horizon", type=int, default=1,
                    help="VaR horizon in trading days.")
    ap.add_argument("--iterations", type=int, default=0,
                    help="0 = run forever; a positive N runs that many polls and stops.")
    args = ap.parse_args()

    sleeve = CRYPTO_SLEEVE

    settings = get_settings()

    # KrakenBroker with no API creds is fine for paper: public data is unauth. If keys are present
    # in the environment we pass them through (Balance/positions become populated), but live-order
    # placement is still gated OFF here — the PaperBroker wraps this instance for every fill.
    feed = KrakenBroker(
        api_key=settings.kraken_api_key or "",
        api_secret=settings.kraken_api_secret or "",
        enable_live_orders=False,
    )
    exec_broker = PaperBroker(feed=feed, starting_equity=args.paper_equity,
                              journal_dir=Path(settings.state_dir))
    exec_account = exec_broker.accounts()[0].number
    print(f"[kraken-paper] PAPER mode. session_id={exec_broker.session_id} "
          f"starting_equity=${args.paper_equity:,.0f} account={exec_account}", flush=True)
    print("[kraken-paper] Kraken feed is quote/candle ONLY. Real Kraken account untouched.",
          flush=True)

    # Pre-flight symbol validation (2026-09-09). Refuses to start if any sleeve pair
    # would 404 on candles at runtime (the MKR/USD / ARX.TO / RIG.TO failure mode).
    # Uses the Kraken feed directly since PaperBroker.candles delegates to it anyway.
    from trading_live_claude.analysis.symbol_validation import preflight
    preflight(feed, [e.symbol for e in CRYPTO_SLEEVE.values()], emit=lambda s: print(s, flush=True))

    ledger = None
    if args.audit_ledger:
        import atexit

        from trading_live_claude.audit.ledger import Ledger
        from trading_live_claude.audit.versioning import session_provenance
        ledger = Ledger(Path(settings.state_dir) / "ledger", stream="kraken", mode="paper",
                        session_id=exec_broker.session_id)
        ledger.append("SESSION_STARTED", session_provenance(argv=sys.argv))
        atexit.register(lambda: ledger.append("SESSION_STOPPED", {"argv0": sys.argv[0]}))
        print(f"[kraken-paper] audit ledger ON: {ledger.dir} stream kraken", flush=True)
    router = Router.build_default(
        mode="paper",
        broker=exec_broker,
        state_dir=settings.state_dir,
        **Router.limits_from(settings),
        ledger=ledger,
    )

    from trading_live_claude.execution.basket import attach_basket_gate
    if attach_basket_gate(router, "kraken", Path(settings.state_dir), enabled=args.require_basket):
        print("[kraken-paper] basket gate ON: entries need a card-signed kraken basket "
              "(python scripts/basket.py status).", flush=True)

    from trading_live_claude.risk.spectral_var import attach_spectral_gate
    _spectral = attach_spectral_gate(router, feed, cap_pct=args.spectral_var_cap,
                                     level=args.spectral_var_level,
                                     horizon_days=args.spectral_var_horizon)
    if _spectral is not None:
        print(f"[kraken-paper] spectral VaR gate ON: {_spectral.thresholds()}", flush=True)

    if args.require_card:
        _engine = VSInvestmentEngine()
        def _thesis(intent, broker):
            return _engine.explain(intent, broker=broker, market=MarketContext())
        _card_db = Path(settings.state_dir) / "approval.db"
        _wiring = wire_card_approval(
            router,
            shim_host="127.0.0.1",
            shim_port=args.card_shim_port,
            ttl_seconds=args.card_ttl,
            thesis_fn=_thesis,
            db_path=_card_db,
            state_dir=Path(settings.state_dir),
            session_id=exec_broker.session_id,
        )
        router = _wiring.router
        print(f"[kraken-paper] --require-card ON — approval shim at {_wiring.shim_url}.",
              flush=True)
        print(f"[kraken-paper] card auth token: {_wiring.auth_token}", flush=True)
        print("[kraken-paper] Pair the card by passing the token as "
              "Authorization: Bearer <token> on every request.", flush=True)
        print(f"[kraken-paper] desk panel LIVE at {_wiring.desk_url} "
              "(build it first: python scripts/build_desk_dash.py).", flush=True)

    market = MarketData(exec_broker, cache=CandleCache(settings.data_cache_dir))
    sizer = PositionSizer(risk_pct=settings.risk_pct_per_trade)

    # Build the per-symbol strategy map from CRYPTO_SLEEVE. The MAIN strategy is a fallback for any
    # symbol not in the map; here every routed symbol IS in the map, so the fallback should never
    # actually be selected — just picked as the sleeve's leader for clarity.
    smap = {entry.symbol: STRATEGIES[entry.strategy](**dict(entry.params))
            for entry in sleeve.values()}
    fallback_entry = next(iter(sleeve.values()))
    fallback = STRATEGIES[fallback_entry.strategy](**dict(fallback_entry.params))
    sym_list = list(sleeve)
    print(f"[kraken-paper] monitoring {len(sym_list)} pairs: {sym_list}", flush=True)

    # Intel overlay + interpret bias — the same wires the equity `signal --intel-overlay` uses.
    # OverlayProvider computes a per-class de-risk scalar (crypto is the one that matters here)
    # AND writes to state/intel_graph.jsonl on every refresh, so this loop's polling now feeds
    # the graph journal too (closing the gap where Kraken paper contributed nothing to intel).
    # Interpret_for reuses provider.last_snapshot so the reasoning layer sees the exact same
    # snapshot the overlay decided on — no drift.
    overlay_for = None
    interpret_for = None
    if settings.worldmonitor_api_key:
        def _snapshot() -> IntelSnapshot:
            async def _f() -> IntelSnapshot:
                async with WorldMonitorClient(settings.worldmonitor_api_key) as wm:
                    return await wm.snapshot()
            return asyncio.run(_f())
        overlay_provider = OverlayProvider(_snapshot, refresh_seconds=900.0)
        overlay_for = overlay_provider

        def _interpret_current():
            snap = overlay_provider.last_snapshot
            return interpret(snap) if snap is not None else []
        interpret_for = _interpret_current
        print("[kraken-paper] intel overlay ON — crypto class scalar + interpret filter live; "
              "polls contribute to state/intel_graph.jsonl", flush=True)
    else:
        print("[kraken-paper] intel overlay OFF (no WORLDMONITOR_API_KEY) — sleeve runs on "
              "allocator + strategy signal only", flush=True)

    # Correlation-aware allocator: compute per-pair conviction bias from daily OHLC + screen
    # score. Runs once at startup; the correlation matrix is stable enough at daily cadence
    # that a start-of-session compute is fine (weekly refresh cadence at most).
    print("[kraken-paper] computing correlation-aware allocator weights...", flush=True)
    bias_map = sleeve_conviction_bias(sleeve)
    print("[kraken-paper] allocator conviction bias (baseline = 1.0):", flush=True)
    for sym in sorted(bias_map, key=lambda s: -bias_map[s]):
        arrow = "boost" if bias_map[sym] > 1.05 else "trim" if bias_map[sym] < 0.95 else "neutral"
        print(f"    {sym:>10}  x{bias_map[sym]:.2f}  ({arrow})", flush=True)

    def _weight_bias_for(symbol: str) -> float:
        return bias_map.get(symbol, 1.0)

    # Alerter — silent to phone without this. Mirrors the QT CLI wiring. Credentials from settings;
    # empty creds means stdout-only, so the venue works whether or not .env has keys.
    from trading_live_claude.monitor import Alerter
    from trading_live_claude.monitor.alerter import AlertConfig
    from trading_live_claude.intel.notification import (
        format_entry as _fmt_entry,
        format_exit as _fmt_exit,
    )
    alerter = Alerter(AlertConfig(
        telegram_bot_token=settings.telegram_bot_token,
        telegram_chat_id=settings.telegram_chat_id,
        smtp_host=settings.smtp_host,
        smtp_user=settings.smtp_user,
        smtp_pass=settings.smtp_pass,
        email_to=settings.alert_email_to,
    ))
    strategy_name_for = {entry.symbol: entry.strategy for entry in sleeve.values()}

    def _emit(ev: MonitorEvent) -> None:
        state = "NEW" if ev.is_transition else f"persisting ({ev.poll_count})"
        print(f"[kraken-paper] {ev.kind.upper()} {ev.symbol} @ {ev.price:.4f} "
              f"({state}) detail={ev.detail}", flush=True)
        if ev.kind == "entry":
            sname = strategy_name_for.get(ev.symbol, fallback_entry.strategy)
            title, body = _fmt_entry(strategy_name=sname, symbol=ev.symbol, price=ev.price,
                                       detail=ev.detail,
                                       is_transition=getattr(ev, "is_transition", True),
                                       poll_count=getattr(ev, "poll_count", 1))
            alerter.send(title, body)
        elif ev.kind == "exit":
            sname = strategy_name_for.get(ev.symbol, fallback_entry.strategy)
            shares = int(ev.detail.get("shares", 0)) if isinstance(ev.detail, dict) else 0
            title, body = _fmt_exit(strategy_name=sname, symbol=ev.symbol, price=ev.price,
                                      shares=shares)
            alerter.send(title, body)

    monitor = LiveMonitor(
        broker=exec_broker,
        market=market,
        strategy=fallback,
        sizer=sizer,
        router=router,
        account_number=exec_account,
        symbols=sym_list,
        interval_seconds=args.interval,
        on_event=_emit,
        account_currency="USD",             # Kraken quotes are USD; the fiat side is ZUSD
        emit_on_change_only=False,          # persistence mode; edges + poll counts both preserved
        strategy_map=smap,
        weight_bias_for=_weight_bias_for,
        overlay_for=overlay_for,
        interpret_for=interpret_for,
        **monitor_policy(settings),
    )
    monitor.run_forever(max_iterations=args.iterations or None)


if __name__ == "__main__":
    main()
