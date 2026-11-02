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

from trading_live_claude.config import get_settings
from trading_live_claude.monitor.session import (
    SessionError,
    add_session_args,
    options_from_args,
    start_session,
)


def main() -> None:
    ap = argparse.ArgumentParser(description="Paper-trade the Kraken crypto sleeve.")
    add_session_args(ap)
    args = ap.parse_args()
    try:
        session = start_session(get_settings(), venue="kraken", mode="paper",
                                options=options_from_args(args, level=True), label="[kraken-paper]",
                                emit=lambda s: print(s, flush=True))
    except SessionError as e:
        raise SystemExit(str(e)) from e
    session.run(args.iterations)


if __name__ == "__main__":
    main()
