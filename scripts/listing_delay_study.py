"""Paired-listing delay study over IBKR's read-only connector. Places no orders.

    python scripts/ib_connector_login.py          # once
    python scripts/listing_delay_study.py --days 730

Writes reports/listing_delay_<date>.md and the events beside it. Research, not a signal: see
analysis/listing_delay.py for what is and is not measured.
"""
from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd

from trading_live_claude.analysis import listing_delay as ld
from trading_live_claude.brokers.base import BrokerError
from trading_live_claude.brokers.ib_connector import IBConnectorBroker
from trading_live_claude.config import get_settings


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=730)
    ap.add_argument("--threshold", type=float, default=3.0)
    args = ap.parse_args()
    settings = get_settings()
    feed = IBConnectorBroker.from_state(Path(settings.state_dir), settings.token_encryption_key or "")
    meetings = ld.load_meetings()
    end, start = datetime.now(UTC), datetime.now(UTC) - timedelta(days=args.days)
    frames, skipped, association = [], {}, {}
    for pair in ld.PAIRS:
        try:
            home = ld.bars_frame(feed.candles(pair.home, start, end, "OneHour"))
            us = ld.bars_frame(feed.candles(pair.us, start, end, "OneHour"))
        except BrokerError as e:
            skipped[pair.name] = str(e)
            continue
        frames.append(ld.pair_events(pair, home, us, meetings, threshold=args.threshold))
        association[pair.home] = ld.meeting_vol_association(home, meetings, threshold=args.threshold)
        association[pair.us] = ld.meeting_vol_association(us, meetings, threshold=args.threshold)
    feed.close()
    events = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    result = {"events": len(events), "skipped": skipped, "fx_adjusted": False,
              "model": ld.fit_delay_model(events, meetings) if len(events) else {"status": "insufficient"},
              "correlations": ld.delay_correlations(events, meetings) if len(events) else {},
              "meeting_vs_high_vol": association}
    out = Path("reports")
    out.mkdir(exist_ok=True)
    stem = out / f"listing_delay_{end:%Y-%m-%d}"
    if len(events):
        events.to_csv(stem.with_suffix(".csv"), index=False)
    body = json.dumps(result, indent=2, default=str)
    stem.with_suffix(".md").write_text(
        "# Paired-listing delay study\n\nResearch only. Prices in each listing's own currency; "
        "several tests were run, so a lone small p-value is not evidence.\n\n"
        f"```json\n{body}\n```\n", encoding="utf-8")
    print(body)


if __name__ == "__main__":
    main()
