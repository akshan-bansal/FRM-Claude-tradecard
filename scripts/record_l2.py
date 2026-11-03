"""Record sampled L2 order books to state/l2/. Public market data only: no key, no orders.

    python scripts/record_l2.py --venue kraken --symbols BTC/USD,ETH/USD
    python scripts/record_l2.py --venue kraken --symbols BTC/USD --sample-seconds 5 --max-seconds 60

Needs the ``l2`` extra (websockets). Reconnects with a growing wait when a feed drops, and a dropped period
is simply absent from the record. Ctrl-C stops it and flushes what it holds. It runs until stopped, so it is
started by hand and nothing schedules it.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from trading_live_claude.microstructure import bitstamp_l2, coinbase_l2, kraken_l2  # noqa: E402
from trading_live_claude.microstructure.l2_recorder import L2Recorder  # noqa: E402

FEEDS: dict[str, Callable[..., Awaitable[None]]] = {
    "kraken": kraken_l2.stream_order_book,
    "coinbase": coinbase_l2.stream_order_book,
    "bitstamp": bitstamp_l2.stream_order_book,
}
MAX_WAIT_SECONDS = 60.0


async def _follow(feed: Callable[..., Awaitable[None]], symbol: str, recorder: L2Recorder) -> None:
    wait = 1.0
    while True:
        try:
            await feed(symbol, on_update=recorder.on_update)
            wait = 1.0
        except asyncio.CancelledError:
            raise
        except Exception as e:                     # a dropped feed must not end the record
            print(f"[record-l2] {recorder.venue} {symbol}: {e}; retrying in {wait:.0f}s", flush=True)
            await asyncio.sleep(wait)
            wait = min(wait * 2.0, MAX_WAIT_SECONDS)


async def _run(args: argparse.Namespace, recorder: L2Recorder) -> None:
    feed = FEEDS[args.venue]
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    tasks = [asyncio.create_task(_follow(feed, s, recorder)) for s in symbols]
    print(f"[record-l2] {args.venue}: {symbols}, a sample every {args.sample_seconds}s, to {recorder.root}", flush=True)
    try:
        if args.max_seconds:
            await asyncio.sleep(args.max_seconds)
        else:
            await asyncio.gather(*tasks)
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--venue", choices=sorted(FEEDS), default="kraken")
    ap.add_argument("--symbols", default="BTC/USD,ETH/USD", help="comma-separated, in the venue's own form")
    ap.add_argument("--sample-seconds", type=float, default=5.0)
    ap.add_argument("--root", default=None, help="output folder; default state/l2")
    ap.add_argument("--max-seconds", type=float, default=0.0, help="stop after this long; 0 runs until Ctrl-C")
    args = ap.parse_args(argv)
    recorder = L2Recorder(args.venue, root=args.root, sample_seconds=args.sample_seconds)
    try:
        asyncio.run(_run(args, recorder))
    except KeyboardInterrupt:
        pass
    finally:
        recorder.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
