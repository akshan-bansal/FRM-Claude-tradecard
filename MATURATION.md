# Data maturation

`bsm_maturation.py` builds the evidence the paper said it lacked. Each forecast is journaled the
moment it is made, along with the inputs it used. Its label stays unknown until the outcome exists:
the forward horizon has elapsed, or Kalshi has settled the contract. `mature` then records the label
with a `matured_date`, the same convention as `matured_date` in FRM-Claude's `risk_labels.py`.

Hyperparameters were frozen in `journal/config.json` before the first forward forecast, so the
forward record is never tuned on itself.

## Daily cycle

```
python bsm_maturation.py run
```

`run` does four things in order:
1. Refreshes FRED.
2. Backfills newly settled Kalshi days.
3. Matures every label that is now knowable.
4. Journals today's forecasts and prints the scorecard.

Running it twice on the same day adds no duplicate VaR or cross-asset forecasts. It does add a
fresh set of Kalshi quotes each time.

Run it at about **09:00 New York time on weekdays**. Kalshi forecasts use the previous close as
model state, so only pre-open market quotes are comparable with the model. Quotes taken later
still count toward calibrating the model, but not toward the market-versus-model comparison.

## Streams

| Stream | Forecast | Matures | Label |
|---|---|---|---|
| `var` | 1- and 5-day VaR plus a 103-point quantile grid, 3 strikes × 5 models, rolling 21-day S&P digital | 1 or 5 trading days later | Realised marked-to-model loss, breaches, PIT |
| `kalshi` | Every open 4pm-settling `KXINXU` (above/below) and `KXINX` (range) contract: quote, sizes, model p_Q | When Kalshi settles | `yes`/`no` and the settlement value |
| `cross` | 5-day VaR of the cross-asset book (Gaussian, GPD, FHS) | 5 trading days later | Realised book loss |

## Files

All journal files are append-only: nothing is rewritten.

| Path | What it holds |
|---|---|
| `data/*.csv` | FRED series. If FRED revises history, the old file is kept in `data/vintages/`. |
| `data/kalshi/*_settled.csv` | Backfilled settled contracts with their 09:00 ET quotes. Strikes are chosen from the prior close, never from the settlement value. |
| `journal/forecasts_*.jsonl`, `journal/matured_*.jsonl` | The forecast journal and the matured labels. |
| `journal/fetch_log.jsonl` | One row per FRED fetch, with the file's SHA-256. |
| `store/` | Raw Kalshi ladders, order books and candles, as gzip JSON lines. |

## When the evidence matures

`status` prints, for each VaR model, the date at which 250 non-overlapping windows will have
matured. That is roughly when 99% coverage tests start to have power:
- 1-day horizon: about one year;
- 5-day horizon: about five years.

Kalshi labels arrive daily. Each settlement day adds one range ladder and about ten
above/below strikes near the money.
