"""Build the point-in-time feature store for the risk lenses (state/lens_features.jsonl).

    python scripts/lens_features.py --since 2026-04-20     # rebuild past days from the journals
    python scripts/lens_features.py --loop                 # build each day at its cutoff, poll the overlay
    python scripts/lens_features.py --status

One row per basket symbol per day, frozen at a cutoff (00:00 UTC crypto, 20:00 UTC the rest) from
records known before it. Rows built within two hours of their cutoff are `live`; anything later is
`rebuild`. A rebuild never replaces a live row. Stop the loop with state/STOP_lens_features.

`--loop` also takes an intel overlay snapshot every --overlay-min minutes (WorldMonitor key from
settings), because the overlay journal is otherwise written only while a trading session polls.
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import contextlib
import sys
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from trading_live_claude.analysis.feature_store import Sources, build, load_store, universe

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "state"
STORE = STATE / "lens_features.jsonl"


def _days(since: date, until: date) -> list[date]:
    return [since + timedelta(days=k) for k in range((until - since).days + 1)]


def _overlay_poller():  # type: ignore[no-untyped-def]
    """A journaling overlay provider, or None when no WorldMonitor key is configured."""
    from trading_live_claude.config.settings import Settings
    from trading_live_claude.intel.routing import OverlayProvider
    from trading_live_claude.intel.worldmonitor import WorldMonitorClient

    key = Settings().worldmonitor_api_key
    if not key:
        return None

    def _snapshot():  # type: ignore[no-untyped-def]
        async def _f():  # type: ignore[no-untyped-def]
            async with WorldMonitorClient(key) as wm:
                return await wm.snapshot()
        return asyncio.run(_f())

    return OverlayProvider(_snapshot, refresh_seconds=0.0, journal=True)


def _build(days: list[date], *, force: bool) -> int:
    insts = universe(ROOT / "config" / "basket_seed.json")
    src = Sources.load(STATE, [i.symbol for i in insts])
    rows = build(STORE, src, insts, days, now=datetime.now(UTC), force_rebuild=force)
    modes = collections.Counter(r["mode"] for r in rows)
    print(f"[lens-features] {len(rows)} rows appended ({dict(modes)}) for {days[0]}..{days[-1]}"
          + (f"; {'; '.join(src.notes)}" if src.notes else ""), flush=True)
    return len(rows)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", type=date.fromisoformat, help="first day to build (rebuild)")
    ap.add_argument("--until", type=date.fromisoformat, help="last day to build (default: today)")
    ap.add_argument("--force-rebuild", action="store_true", help="re-append rebuild rows (live rows are kept)")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--every-min", type=float, default=10.0, help="loop: how often to look for due cutoffs")
    ap.add_argument("--overlay-min", type=float, default=30.0, help="loop: overlay snapshot interval; 0 = off")
    ap.add_argument("--status", action="store_true")
    a = ap.parse_args()

    if a.status:
        rows = list(load_store(STORE).values())
        if not rows:
            print("store is empty")
            return 1
        modes = collections.Counter(r["mode"] for r in rows)
        dates = sorted({r["date"] for r in rows})
        print(f"{len(rows)} rows, {len({r['symbol'] for r in rows})} symbols, {dates[0]}..{dates[-1]}, {dict(modes)}")
        for fam in ("live_title_24h", "gkg_title_24h", "ov_scalar", "graph_events_24h"):
            n = sum(1 for r in rows if r["features"].get(fam) is not None)
            print(f"  {fam:18} known on {n:6} rows ({n / len(rows):.0%})")
        return 0

    today = datetime.now(UTC).date()
    if not a.loop:
        since = a.since or today - timedelta(days=1)
        _build(_days(since, a.until or today), force=a.force_rebuild)
        return 0

    stop = STATE / "STOP_lens_features"
    poller = _overlay_poller() if a.overlay_min > 0 else None
    print(f"[lens-features] loop: cutoffs every {a.every_min:g} min; overlay "
          f"{f'every {a.overlay_min:g} min' if poller else 'OFF (no WorldMonitor key or --overlay-min 0)'}; "
          f"stop: touch {stop}", flush=True)
    next_overlay = 0.0
    with contextlib.suppress(KeyboardInterrupt):
        while not stop.exists():
            if poller is not None and time.time() >= next_overlay:
                try:
                    poller.refresh(force=True)
                except Exception as exc:  # intel I/O must not stop the store
                    print(f"[lens-features] overlay snapshot failed: {exc}", flush=True)
                next_overlay = time.time() + a.overlay_min * 60
            now = datetime.now(UTC).date()
            _build(_days(now - timedelta(days=2), now), force=False)
            deadline = time.time() + a.every_min * 60
            while time.time() < deadline and not stop.exists():
                time.sleep(5)
    stop.unlink(missing_ok=True)
    print("[lens-features] stopped", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
