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

from trading_live_claude.config import get_settings
from trading_live_claude.monitor.session import (
    SessionError,
    add_session_args,
    options_from_args,
    start_session,
)


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
    session.run(args.iterations)


if __name__ == "__main__":
    main()
