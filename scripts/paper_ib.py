"""Start an Interactive Brokers paper-trading session.

Same shape as ``paper_kraken.py`` — ``PaperBroker`` simulates every fill against IB's live
quotes so the real IB paper account is untouched. IB's ``enable_live_orders`` gate stays OFF;
even if it were on, PaperBroker's short-circuit fires first.

Two transports for the IB feed:

* ``--transport web`` (default): REST via the Client Portal Gateway (``localhost:5000/v1/api``).
  No TWS/Gateway binary needed — you run the CP Gateway Java daemon (~50 MB) and log in via
  browser once, then it holds the session cookie. Wired through ``IBWebBroker``. The CP
  Gateway times out on idle, so this script also fires a keep-alive tickle every ~90 s.
* ``--transport socket``: legacy TWS/IB Gateway socket API via ``ib_insync``. Requires TWS or
  IB Gateway running with the API enabled. Wired through ``IBBroker``. Only reason to prefer
  this today is L2 depth-of-book or algo orders (VWAP/TWAP/etc), which the Web API doesn't
  expose in the same shape.

Optional intel layers (opt-in via ``--intel-overlay``):
  * OSINT overlay class scalar trims entry conviction (equity class for IB names).
  * Interpret theses further trim entries that implicate any symbol in the monitored list.

What IB adds over the QT/Kraken paper sleeves the framework already runs:
  * Bonds (corporates, treasuries, munis) — QT retail has no continuous book here
  * Native futures contracts (ES, NQ, CL, GC, ZN, …) — not ETF proxies
  * Global equities + LSE / ASX / HKEX / TSE venues
  * Options with a full Greeks surface
  * L2 depth-of-book via ``IBBroker.request_l2_book(contract)`` (socket transport only)
  * HFT-adjacent algo orders (VWAP, TWAP, MIDPRICE, PEG, ICEBERG) via
    ``IBBroker.place_algo_order`` (socket transport only)

The L2 + algo-order surfaces are available on the underlying feed object for research scripts
to use directly — the monitor loop here uses only the ``Broker`` protocol surface.
"""
from __future__ import annotations

import argparse
import sys
import threading

# Windows console defaults to cp1252, which crashes on Unicode arrows / bullets in log lines
# (structlog prints an exception body that contains them). Force stdout/stderr to UTF-8 so a
# single bad log line doesn't kill the whole monitor process. errors="replace" is intentional
# — surviving with a '?' is strictly better than a UnicodeEncodeError crash mid-poll.
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")    # type: ignore[union-attr]
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")    # type: ignore[union-attr]
    except Exception:                                                  # pragma: no cover
        pass

from trading_live_claude.brokers.base import Broker
from trading_live_claude.config import get_settings
from trading_live_claude.monitor.session import (
    SessionError,
    add_session_args,
    options_from_args,
    start_session,
)


class _NewsSidecarThread(threading.Thread):
    """Daemon thread that drains IBBroker's news buffer at a fixed cadence, converts each
    record into intel-graph edges (``event_records_to_edges``) and streams them to the
    graph journal (``append_edges``). Independent of the price-poll cadence so a big
    interval doesn't back-pressure news latency.

    Failures are logged and swallowed — the trading loop must not die because a news
    projection blew up.
    """

    def __init__(self, feed: Broker, cadence_s: float) -> None:
        super().__init__(daemon=True, name="ib-news-sidecar")
        self.feed = feed
        self.cadence_s = max(1.0, float(cadence_s))
        self._stop = threading.Event()
        self._drained_total = 0

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        from datetime import UTC, datetime

        from trading_live_claude.intel.graph import (
            append_edges,
            event_records_to_edges,
        )
        while not self._stop.wait(self.cadence_s):
            try:
                recs = self.feed.drain_news()               # type: ignore[attr-defined]
            except Exception as e:                          # pragma: no cover — defensive
                print(f"[ib-paper] news drain failed: {e}", flush=True)
                continue
            if not recs:
                continue
            now = datetime.now(UTC).isoformat()
            poll_id = f"ib-news:{now}"
            try:
                edges = event_records_to_edges(recs, domain="ib_news",
                                                poll_id=poll_id, as_of=now)
                append_edges(edges)
            except Exception as e:                          # pragma: no cover — defensive
                print(f"[ib-paper] news → graph failed: {e}", flush=True)
                continue
            self._drained_total += len(recs)
            print(f"[ib-paper] news drained {len(recs)} record(s) → {len(edges)} edge(s) "
                  f"(session total {self._drained_total})", flush=True)


def _maybe_start_news_sidecar(feed: Broker,
                              args: argparse.Namespace) -> _NewsSidecarThread | None:
    """Subscribe the IB feed to news and start the drain sidecar, iff --news-providers set.

    Returns the running thread so the caller can stop it. When the flag is empty this is a
    no-op that returns None. When --transport web is in use, this refuses loudly rather than
    subscribe against the wrong adapter (IBWebBroker has no news surface today).
    """
    providers_arg = (args.news_providers or "").strip()
    if not providers_arg and not args.news_bulletins:
        return None
    if args.transport != "socket":
        raise SystemExit("--news-providers / --news-bulletins require --transport socket "
                         "(IBWebBroker does not expose the streaming news channel).")
    if not hasattr(feed, "subscribe_news") or not hasattr(feed, "drain_news"):
        raise SystemExit("Feed does not support news subscription — expected IBBroker.")

    providers = [p.strip() for p in providers_arg.split(",") if p.strip()] or None

    # Log which providers this account actually has access to so a wrong code shows up in
    # the boot log rather than being silently dropped by IB. Non-fatal on failure.
    try:
        available = feed.list_news_providers()          # type: ignore[attr-defined]
        codes = ",".join(p["code"] for p in available) or "<none>"
        print(f"[ib-paper] news providers on this account: {codes}", flush=True)
        if providers:
            missing = [p for p in providers if not any(a["code"] == p for a in available)]
            if missing:
                print(f"[ib-paper] WARNING: requested providers not on account: "
                      f"{missing} — they will silently emit no headlines.", flush=True)
    except Exception as e:                              # pragma: no cover
        print(f"[ib-paper] could not list news providers: {e} — continuing.", flush=True)

    sym_arg = [s.strip().upper() for s in (args.symbols or "").split(",") if s.strip()]
    feed.subscribe_news(sym_arg, providers=providers,               # type: ignore[attr-defined]
                        include_bulletins=bool(args.news_bulletins))
    print(f"[ib-paper] news → graph sidecar ON. providers={providers or 'all'} "
          f"bulletins={bool(args.news_bulletins)} cadence={args.news_cadence_s:.0f}s",
          flush=True)
    thread = _NewsSidecarThread(feed, cadence_s=args.news_cadence_s)
    thread.start()
    return thread


def main() -> None:
    ap = argparse.ArgumentParser(description="Paper-trade against Interactive Brokers quotes.")
    ap.add_argument("--symbols", default="", help="Comma-separated symbols. Default is a US equity smoke-test set.")
    ap.add_argument("--strategy", default="", help="Fallback strategy name. Default bollinger.")
    ap.add_argument("--strategy-map", dest="strategy_map", default="",
                    help="Per-symbol strategy overrides, e.g. 'GLD=atr_channel,USO=rsi_meanrevert'. "
                         "Same shape as `trading paper --strategy-map`.")
    ap.add_argument("--futures", default="",
                    help="Comma-separated futures roots resolved as FUT front-month contracts, e.g. "
                         "'ES,NQ,CL,GC,ZN'. Only for --transport web. Without this flag those symbols would "
                         "collide with the STK tickers of the same name (ES=Eversource, CL=Colgate).")
    ap.add_argument("--transport", default="web", choices=("web", "socket"),
                    help="'web' hits the Client Portal Gateway REST API (default). 'socket' uses TWS/IB "
                         "Gateway via ib_insync.")
    ap.add_argument("--account-currency", dest="account_currency", choices=("CAD", "USD"), default=None,
                    help="The book's currency. Default USD on IB: one book per currency, FX is not sourced from IB.")
    ap.add_argument("--news-providers", dest="news_providers", default="",
                    help="Comma-separated IB news providers (socket transport only), e.g. BRFG,BRFUPDN,DJNL. Headlines "
                         "become `mentioned_by` edges in state/intel_graph.jsonl.")
    ap.add_argument("--news-bulletins", dest="news_bulletins", action="store_true",
                    help="Also subscribe to IB exchange bulletins (socket transport only).")
    ap.add_argument("--news-cadence-s", dest="news_cadence_s", type=float, default=30.0,
                    help="Sidecar drain interval in seconds. Independent of --interval.")
    add_session_args(ap)
    args = ap.parse_args()
    try:
        session = start_session(get_settings(), venue="ib", mode="paper", symbols=args.symbols,
                                strategy=args.strategy, strategy_map=args.strategy_map,
                                futures=[s.strip().upper() for s in args.futures.split(",") if s.strip()],
                                transport=args.transport, options=options_from_args(args, level=True),
                                label="[ib-paper]", emit=lambda s: print(s, flush=True))
    except SessionError as e:
        raise SystemExit(str(e)) from e
    news = _maybe_start_news_sidecar(session.feed, args)
    try:
        session.run(args.iterations)
    finally:
        if news is not None:
            news.stop()


if __name__ == "__main__":
    main()
