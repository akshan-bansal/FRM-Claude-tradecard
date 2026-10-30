"""Backfill the news lens from GDELT GKG 2.1, newest first, under a transfer budget; or follow it forward.

    python scripts/gdelt_gkg_backfill.py --budget-gb 75 --oldest 2023-01-01
    python scripts/gdelt_gkg_backfill.py --follow --span-gb 75
    python scripts/gdelt_gkg_backfill.py --status

Files are filtered in memory and discarded; only rows naming a basket symbol are kept, in
state/gdelt_gkg/YYYY-MM.jsonl (backfill) and live-YYYY-MM.jsonl (follower). Resumable:
progress.jsonl records each file and the bytes it cost, and the budget counts every byte across runs.
Stop early with state/STOP_gkg (backfill) or state/STOP_gkg_follow (follower).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from trading_live_claude.news.gkg import Backfill, latest_stamp, prune_to_span, stamps, window_start
from trading_live_claude.news.runner import load_config

ROOT = Path(__file__).resolve().parent.parent
AVG_FILE_BYTES = 6_000_000  # headroom per in-flight file when checking the budget


def follow(a: argparse.Namespace) -> int:
    """Fetch each new GKG file as GDELT publishes it, into live-* files; prune to the rolling span daily.

    On start it also fills the gap back to the newest file anything has settled, so stopping the
    follower for a while loses nothing. Pruning waits until the backfill has been idle 15 minutes.
    """
    cfg = load_config(ROOT / "config" / "news_sources.yaml")
    bf = Backfill(out_dir=a.out, aliases=cfg.aliases, budget_bytes=0, prefix="live-")
    stop = ROOT / "state" / "STOP_gkg_follow"
    print(f"[gkg-follow] polling GDELT every 5 min; rolling window {a.span_gb:g} GB; stop: touch {stop}", flush=True)
    backfill_progress = a.out / "progress.jsonl"
    last_prune = 0.0
    while not stop.exists():
        latest = latest_stamp()
        if latest:
            settled = bf.settled_everywhere()
            newest = max(settled, default=None)
            floor = datetime.strptime(newest, "%Y%m%d%H%M%S").replace(tzinfo=UTC) if newest \
                else datetime.now(UTC) - timedelta(hours=1)
            top = datetime.strptime(latest, "%Y%m%d%H%M%S").replace(tzinfo=UTC)
            for s in sorted(s for s in stamps(top, floor) if s not in settled):
                row, kept = bf.process(s)
                bf.commit(row, kept)
                print(f"[gkg-follow] {s} {row['status']} kept {row.get('kept', 0)}", flush=True)
        backfill_idle = not backfill_progress.exists() or time.time() - backfill_progress.stat().st_mtime > 900
        if time.time() - last_prune > 86400 and backfill_idle:
            res = prune_to_span(a.out, int(a.span_gb * 1e9))
            print(f"[gkg-follow] window: {res}", flush=True)
            last_prune = time.time()
        for _ in range(60):
            if stop.exists():
                break
            time.sleep(5)
    stop.unlink(missing_ok=True)
    print("[gkg-follow] stopped", flush=True)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--budget-gb", type=float, default=75.0, help="total transfer cap across all runs")
    ap.add_argument("--oldest", default="2023-01-01", help="stop at this date even if budget remains")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--out", type=Path, default=ROOT / "state" / "gdelt_gkg")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--follow", action="store_true",
                    help="poll for new files going forward and keep a rolling --span-gb window")
    ap.add_argument("--span-gb", type=float, default=75.0, help="follow: rolling window, in source bytes")
    a = ap.parse_args()

    cfg = load_config(ROOT / "config" / "news_sources.yaml")
    bf = Backfill(out_dir=a.out, aliases=cfg.aliases, budget_bytes=int(a.budget_gb * 1e9))
    a.out.mkdir(parents=True, exist_ok=True)
    done, spent = bf.done()

    if a.status:
        rows = [json.loads(ln) for ln in bf.progress_path.read_text(encoding="utf-8").splitlines()] \
            if bf.progress_path.exists() else []
        ok = [r for r in rows if r["status"] == "ok"]
        span = f"{min(r['stamp'] for r in ok)} .. {max(r['stamp'] for r in ok)}" if ok else "-"
        print(f"files ok {len(ok)}, missing {sum(r['status'] == 'missing' for r in rows)}, "
              f"errors {sum(r['status'] == 'error' for r in rows)}; span {span}")
        print(f"transferred {spent / 1e9:.2f} GB of {a.budget_gb:.0f} GB; articles seen "
              f"{sum(r.get('rows', 0) for r in ok):,}, kept {sum(r.get('kept', 0) for r in ok):,} "
              f"(title-mapped {sum(r.get('title_kept', 0) for r in ok):,})")
        return 0

    if a.follow:
        return follow(a)

    oldest = datetime.fromisoformat(a.oldest).replace(tzinfo=UTC)
    ws = window_start(a.out)
    if ws:  # never re-fetch what the rolling window pruned
        oldest = max(oldest, datetime.strptime(ws, "%Y%m%d%H%M%S").replace(tzinfo=UTC))
    todo = (s for s in stamps(datetime.now(UTC), oldest) if s not in done)
    stop = ROOT / "state" / "STOP_gkg"
    print(f"[gkg] newest-first from now to {a.oldest}; budget {a.budget_gb:.0f} GB, "
          f"{spent / 1e9:.2f} GB already spent, {len(done)} files settled; stop: touch {stop}", flush=True)

    n = 0
    pending: set[Future[tuple[dict[str, object], list[dict[str, object]]]]] = set()
    exhausted = False
    with ThreadPoolExecutor(max_workers=a.workers) as pool:
        while True:
            while (not exhausted and len(pending) < a.workers and not stop.exists()
                   and spent + (len(pending) + 1) * AVG_FILE_BYTES <= bf.budget_bytes):
                s = next(todo, None)
                if s is None:
                    exhausted = True
                    break
                pending.add(pool.submit(bf.process, s))
            if not pending:
                break
            finished, pending = wait(pending, return_when=FIRST_COMPLETED)
            for fut in finished:
                row, kept = fut.result()
                bf.commit(row, kept)
                spent += int(row.get("bytes", 0))
                n += 1
                if n % 200 == 0:
                    print(f"[gkg] {n} files this run, at {row['stamp']}, {spent / 1e9:.2f} GB spent", flush=True)
    reason = "stop file" if stop.exists() else ("reached --oldest" if exhausted else "budget reached")
    stop.unlink(missing_ok=True)
    print(f"[gkg] done ({reason}): {n} files this run, {spent / 1e9:.2f} GB transferred in total", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
