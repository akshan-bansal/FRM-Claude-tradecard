"""Score journaled headlines with the LLM: state/news.jsonl -> state/news_sentiment.jsonl + `sentiment_on` edges.

    python scripts/news_sentiment.py                # up to 200 unscored headlines, newest first
    python scripts/news_sentiment.py --limit 25     # a small, cheap pass

Needs ANTHROPIC_API_KEY and spends credit. Touches no order path. Nothing schedules it.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from trading_live_claude.intel.news_sentiment import run_pass


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=200)
    a = ap.parse_args()
    r = run_pass(limit=a.limit)
    print(f"[news-sentiment] scored {r.scored}, dropped {r.dropped}, failed batches {r.failed_batches}, "
          f"tokens {r.input_tokens} in / {r.output_tokens} out" + (f"; errors: {sorted(set(r.errors))}" if r.errors else ""))
    return 1 if r.failed_batches and not r.scored else 0


if __name__ == "__main__":
    raise SystemExit(main())
