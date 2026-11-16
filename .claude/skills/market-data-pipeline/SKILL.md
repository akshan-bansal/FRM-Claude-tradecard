---
name: market-data-pipeline
description: Use when the user needs market data (EOD candles, intraday bars, quotes, fundamentals). Standardizes ingestion from Interactive Brokers (equities, futures) and Kraken (crypto) into a canonical pandas DataFrame (columns time/open/high/low/close/volume, UTC timestamps). Cached on disk so repeated backtests don't hammer the APIs.
---

# market-data-pipeline

You are the data pipeline for this repo. Every market-data request from a strategy, backtest, monitor, or notebook goes through `trading_live_claude.data.MarketData`.

## Recipe

1. **Identify the data type:** EOD candles, intraday bars, quotes, or account balances. Fundamentals are not this pipeline's: use `data/fundamentals*` (EDGAR) or the provider's own source.
   **Identify the venue:** crypto pairs (`BTC/USD`) come from Kraken; every equity listing and future comes from IB. Build the feed with `monitor.session.venue_feed(settings, venue, money=False)`; on IB it is `IBAccess`, which tries each region's data transports in order (`ib_access` in trading.yaml).
2. **Select the endpoint:**
   - `MarketData.history(symbol, years, interval)` → candles, parquet-cached
   - `MarketData.recent(symbol, bars, interval)` → live monitor warm-up window
   - `broker.quote(symbol)` / `broker.quotes(symbols)` → real-time if the IB account has the market-data subscription, else delayed; Kraken's public ticker is live
3. **Build the request with proper params.**
   - Symbol format: the desk's own spelling everywhere — `.TO`/`.V` (TSX), `.L`, `.AX`, `.T`, `.HK`, bare for US, `/ROOT` for futures, `BASE/QUOTE` for crypto. `venues.venue_for` maps each to its exchange and currency.
   - Interval map: `1d`, `1h`, `30m`, `15m`, `5m`, `1m`. Anything else is a typo; reject.
   - Date range: timezone-aware (UTC) datetimes. Each adapter converts to its own API's format.
4. **Normalize the response.** Always returns a DataFrame with columns `[time, open, high, low, close, volume]`, `time` is `pd.Timestamp` in UTC, sorted ascending, dedup'd. If the broker returns `VWAP`, include it.
5. **Corporate-action adjustments.** IB `TRADES` bars are split-adjusted but not dividend-adjusted; warn the user before computing total returns on long windows.
6. **Cache.** Repeated calls with the same `(symbol, interval, start, end)` hit the parquet cache under `data/cache/`. Cache key is a SHA256 of the tuple. Stale cache is fine if the user is backtesting; for live monitors, use `recent()` which always fetches fresh.
7. **Return the DataFrame**, ready for indicator computation in `signals/indicators.py`.

## Always

- Set `interval="1d"` by default. IB paces historical-data requests (pacing violations above ~60 requests per 10 minutes), and Kraken's public OHLC returns at most 720 bars.
- For multi-symbol fetches, keep IB requests sequential or lightly parallel (`max_workers=2`) so the pacing limit is not hit.
- When the user wants long intraday history, warn first: IB limits how far back small bars go, and Kraken's OHLC endpoint stops at 720 bars.

## Never

- Recommend `yfinance`. The whole point of this repo is to use the same data source for backtest and live.
- Bypass the cache for backtest workflows.
- Silently drop rows from the response — every reshape must preserve row count or log the diff.

## Trigger phrases

- "Pull 3 years of daily SHOP data"
- "What's the latest quote for AAPL?"
- "Get me the candles I need to compute a 50-day EMA on XIC.TO"

## Libraries

`httpx`, `hishel`, `pandas`, `pyarrow` (via `pandas.to_parquet`). IB through `trading_live_claude.brokers.ib_access` (over `ib`, `ib_web`, `ib_connector`); Kraken through `trading_live_claude.brokers.kraken` and `data.kraken_ohlc`.

## Article alignment

Skill #2 in [Top 5 Claude Code Skills for Algorithmic Trading](https://medium.datadriveninvestor.com/top-5-claude-code-skills-for-algorithmic-trading-49620fa2b02c) (upstream: `JoelLewis/finance_skills`/trading-operations). Provider swapped from EODHD to Questrade, then to IB and Kraken (2026-11-16).
