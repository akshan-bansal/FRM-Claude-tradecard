"""Build the lens risk targets (state/lens_labels.jsonl): 5-day drawdown over trailing vol, per symbol per day.

    python scripts/lens_labels.py            # fetch daily bars, rewrite the label file
    python scripts/lens_labels.py --status

Prices: Kraken public OHLC for crypto (its daily candle closes at 00:00 UTC, the crypto cutoff); Yahoo's
chart endpoint for everything else. IB micro roots share their underlying's series (MGC -> GC,
SIL -> SI). Every row names its source. Labels are a pure function of the bars, so the file is
rewritten whole on each run.
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import httpx
import yaml

from trading_live_claude.analysis.feature_store import universe
from trading_live_claude.analysis.risk_labels import Bar, labels
from trading_live_claude.data.kraken_ohlc import kraken_ohlc

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "state" / "lens_labels.jsonl"
SHARED_UNDERLYING = {"MGC": "GC", "SIL": "SI"}
UA = "Mozilla/5.0 (compatible; FRM-Claude-labels/0.1)"


def kraken_bars(symbol: str, client: httpx.Client) -> list[Bar]:
    base, _, quote = symbol.partition("/")
    pair = ("XBT" if base == "BTC" else base) + quote
    df = kraken_ohlc(pair, client=client)
    # A candle stamped D opens at D 00:00 UTC and closes at D+1 00:00: it is day D. The last candle is
    # still forming, so it is dropped.
    return [Bar(t.date(), float(lo), float(c)) for t, lo, c in zip(df["time"], df["low"], df["close"], strict=True)][:-1]


def yahoo_bars(ticker: str, client: httpx.Client) -> list[Bar]:
    r = client.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
                   params={"range": "2y", "interval": "1d"})
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    tz = ZoneInfo(res["meta"].get("exchangeTimezoneName") or "America/New_York")
    q = res["indicators"]["quote"][0]
    out: list[Bar] = []
    today = datetime.now(tz).date()
    for ts, lo, c in zip(res.get("timestamp") or [], q["low"], q["close"], strict=True):
        d = datetime.fromtimestamp(ts, tz).date()
        if lo is None or c is None or d >= today:  # today's bar is not closed yet
            continue
        out.append(Bar(d, float(lo), float(c)))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--status", action="store_true")
    a = ap.parse_args()

    if a.status:
        rows = [json.loads(ln) for ln in OUT.read_text(encoding="utf-8").splitlines()] if OUT.exists() else []
        if not rows:
            print("no labels yet")
            return 1
        by = collections.Counter(r["symbol"] for r in rows)
        tgt = sorted(r["target"] for r in rows if r["target"] is not None)
        print(f"{len(rows)} labels, {len(by)} symbols, {min(r['date'] for r in rows)}..{max(r['date'] for r in rows)}")
        print(f"target median {tgt[len(tgt) // 2]:.2f}, 90th pct {tgt[int(len(tgt) * 0.9)]:.2f}")
        return 0

    news_cfg = yaml.safe_load((ROOT / "config" / "news_sources.yaml").read_text(encoding="utf-8"))
    tickers = {s: (spec or {}).get("yahoo") for s, spec in news_cfg["symbols"].items()}
    rows: list[dict[str, object]] = []
    problems: list[str] = []
    with httpx.Client(timeout=30, headers={"User-Agent": UA}, follow_redirects=True) as client:
        for inst in universe(ROOT / "config" / "basket_seed.json"):
            src_sym = SHARED_UNDERLYING.get(inst.symbol, inst.symbol)
            try:
                if inst.venue == "kraken":
                    bars, source = kraken_bars(inst.symbol, client), "kraken_ohlc"
                elif tickers.get(src_sym):
                    bars, source = yahoo_bars(str(tickers[src_sym]), client), f"yahoo:{tickers[src_sym]}"
                else:
                    problems.append(f"{inst.symbol}: no price source")
                    continue
            except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
                problems.append(f"{inst.symbol}: {type(exc).__name__}: {exc}"[:120])
                continue
            n0 = len(rows)
            rows += [{"symbol": inst.symbol, "asset_class": inst.asset_class, "price_source": source, **lab}
                     for lab in labels(bars)]
            if len(rows) == n0:
                problems.append(f"{inst.symbol}: {len(bars)} bars, too few for a label")
            time.sleep(1.1)  # Kraken's public tier is ~1 req/s; Yahoo gets the same courtesy
    OUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    tmp.replace(OUT)
    print(f"[lens-labels] {len(rows)} labels for {len({r['symbol'] for r in rows})} symbols "
          f"-> {OUT} ({datetime.now(UTC):%Y-%m-%d %H:%M} UTC)")
    for p in problems:
        print(f"  ! {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
