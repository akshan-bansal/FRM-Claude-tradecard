"""Record what theses would say right now, in shadow. It places no orders and nothing acts on what it writes.

    python scripts/thesis_shadow.py
    python scripts/thesis_shadow.py --list                    # what the shadow book holds

One pass and out, and nothing schedules it. Signals go to the ledger's ``thesis`` stream. It needs a recent real
snapshot in the overlay journal, and with a stale feed it records nothing and says why.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from trading_live_claude.analysis.universe import CRYPTO_SLEEVE, WALK_FORWARD_VALIDATED  # noqa: E402
from trading_live_claude.intel.graph_store import GraphStore  # noqa: E402
from trading_live_claude.intel.thesis_signal import ThesisBook, run_shadow_pass  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true", help="show what the shadow book holds and stop")
    args = ap.parse_args(argv)

    book = ThesisBook()
    if args.list:
        rows = book.signals(include_replay=True)
        print(f"[thesis-shadow] {len(rows)} signals held; chain {book.verify()}")
        for r in rows[-40:]:
            print(f"  {r['as_of'][:16]}  {r['symbol']:>10}  {r['action']:<8} {r['strength']:.2f}  {r['thesis']}")
        return 0

    universe = set(WALK_FORWARD_VALIDATED) | set(CRYPTO_SLEEVE)
    with GraphStore() as store:
        result = run_shadow_pass(store, book, universe=universe)
    print(f"[thesis-shadow] as of {result.as_of}: {len(result.signals)} signals, {result.recorded} new; "
          f"snapshot {result.snapshot_as_of or 'none'}")
    for note in result.notes:
        print(f"  note: {note}")
    for s in result.signals[:40]:
        print(f"  {s.symbol:>10}  {s.action:<8} {s.strength:.2f}  {s.thesis}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
