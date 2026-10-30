"""Run the news collector: poll config/news_sources.yaml and journal headlines point-in-time.

    python scripts/news_collect.py --once          # one pass over every feed, then exit
    python scripts/news_collect.py                 # poll until state/STOP_news appears
    python scripts/news_collect.py --status        # per-feed health from the last run

Reads no sentiment and touches no order path. Output: state/news.jsonl (the archive),
`mentions` edges in state/intel_graph.jsonl, per-feed health in state/news_health.json,
a heartbeat per pass in state/news_passes.jsonl.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from trading_live_claude.news.runner import Collector, load_config

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", type=Path, default=ROOT / "config" / "news_sources.yaml")
    ap.add_argument("--once", action="store_true", help="one pass over every feed, then exit")
    ap.add_argument("--status", action="store_true", help="print per-feed health and exit")
    a = ap.parse_args()

    cfg = load_config(a.config, a.config.with_name(a.config.stem + ".local.yaml"))
    if a.status:
        if not cfg.health.exists():
            print("no health file yet; run a pass first")
            return 1
        rows = json.loads(cfg.health.read_text(encoding="utf-8"))
        print(f"as of {rows['as_of']}")
        for r in sorted(rows["feeds"].values(), key=lambda r: (r["source"], r["url"])):
            state = "OK  " if not r["last_error"] else f"FAIL x{r['fails']}"
            tag = r["url"].split("s=")[-1].split("&")[0] if r["source"] == "yahoo" else ""
            print(f"  {state} {r['source']:14} {tag:10} items {r['items_last']:3}  written {r['written_total']:5}"
                  f"  {r['last_error'][:60]}")
        return 0

    print(f"[news] {len(cfg.feeds)} feeds -> {cfg.journal}"
          f"{' + graph ' + str(cfg.graph_journal) if cfg.graph_journal else ''}"
          f"; SEC contact {'set' if cfg.sec_user_agent else 'NOT set (EDGAR skipped)'}", flush=True)
    col = Collector(cfg)
    if a.once:
        written = col.run_pass()
        mapped = sum(1 for h in written if h.symbols or h.feed_symbols)
        print(f"[news] pass done: {len(written)} new headlines, {mapped} mapped to a symbol", flush=True)
        return 0
    stop = cfg.journal.parent / "STOP_news"
    print(f"[news] polling; stop with: touch {stop}", flush=True)
    with contextlib.suppress(KeyboardInterrupt):
        col.run_forever(stop_file=stop)
    return 0


if __name__ == "__main__":
    sys.exit(main())
