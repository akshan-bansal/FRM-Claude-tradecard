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

## Underlyings

Every underlying registered in `bsm_event.UNDERLYINGS` runs through the same streams. To add
one, add a registry entry with:
- a FRED price id;
- an implied-vol index id, or `None` to use the point-in-time GJR forecast instead;
- its Kalshi series, and a regex that picks one daily settlement from their event tickers;
- its rate, dividend (or foreign rate) and skew;
- its day count: 252, or 365 for assets that trade every day.

To narrow the list, set `"enabled": [...]` in `config.json`.

| Key | Price | Vol | Kalshi series | Notes |
|---|---|---|---|---|
| SPX | SP500 | VIXCLS | KXINXU, KXINX | Original frozen config |
| NDX | NASDAQ100 | VXNCLS | KXNASDAQ100U, KXNASDAQ100 | |
| DJIA | DJIA | VXDCLS | — | VaR stream only |
| WTI | DCOILWTICO | OVXCLS | KXWTI (2:30pm daily) | Kalshi settles on futures, the model uses spot: basis risk. The non-positive price on 2020-04-20 is dropped. FRED spot lags about 4 business days, so forward snapshots are often stale. |
| BTC, ETH | CBBTCUSD, CBETHUSD | GJR fallback | KXBTCD, KXBTC / KXETHD | 365-day calendar; 5pm ET settlement |
| EURUSD, USDJPY | DEXUSEU, DEXJPUS | GJR fallback | KXEURUSD / KXUSDJPY | FRED's H.10 rates arrive about a week late, so most forward states are stale and skipped. |

Each new underlying is tuned on its own history and frozen into `config.json` under
`underlyings.<key>` on its first cycle. Existing frozen sections are never rewritten. Gold has
Kalshi contracts but no FRED price series, so it is not registered.

## Streams

| Stream | Forecast | Matures | Label |
|---|---|---|---|
| `var` | Per underlying: 1- and 5-day VaR plus a 103-point quantile grid, 3 strikes × 5 models, rolling 21-day digital. At 5 days, three `-path` challengers run the GJR recursion daily instead of rescaling a 1-day draw (amendment of 2026-10-31). | 1 or 5 trading days later | Realised marked-to-model loss, breaches, PIT |
| `kalshi` | Every open daily contract of every enabled underlying: quote, sizes, model p_Q. Skipped if the model state is more than 3 business days old. | When Kalshi settles | `yes`/`no` and the settlement value |
| `cross` | 5-day VaR of the cross-asset book (Gaussian, GPD, FHS, plus `gpd-path` and `fhs-path` challengers) | 5 trading days later | Realised book loss |

## Files

All journal files are append-only: nothing is rewritten.

| Path | What it holds |
|---|---|
| `data/*.csv` | FRED series. If FRED revises history, the old file is kept in `data/vintages/`. |
| `data/kalshi/*_settled.csv` | Backfilled settled contracts with their 09:00 ET quotes. Strikes are chosen from the prior close, never from the settlement value. |
| `journal/forecasts_*.jsonl`, `journal/matured_*.jsonl` | The forecast journal and the matured labels. |
| `journal/fetch_log.jsonl` | One row per FRED fetch, with the file's SHA-256. |
| `store/` | Raw Kalshi ladders, order books and candles, as gzip JSON lines. |

## Protobuf schemas (`bsm.v1`)

`proto/bsm/v1/` defines every record as a message:
- **`market.proto`:** `BookSnapshot`, `Trade` and a `MarketRecord` envelope.
- **`journal.proto`:**
  - `VarForecast` and `VarMatured`
  - `KalshiForecast` and `KalshiMatured`
  - `CrossForecast` and `CrossMatured`
  - `KalshiSettled`, `FetchLog` and `MaturationConfig`
  - a `JournalEntry` envelope.

Journal field names equal the JSON keys, so existing lines parse unchanged. Generated Python is
checked in under `bsm_pb/`. It needs the runtime `protobuf==5.29.3`, which matches protoc 29.3.

```
python bsm_proto.py validate        # strict parse + exact round trip of everything on disk
python bsm_proto.py export          # varint length-delimited .pb per stream in exports/
python bsm_proto.py compile         # regenerate bsm_pb/ after editing a .proto
```

Parsing is strict, so a field written by the code but missing from the schema fails validation.
The daily `run` validates at the end of every cycle; the result is in `journal/run.log`.

## When the evidence matures

`status` prints, for each VaR model, the date at which 250 non-overlapping windows will have
matured. That is roughly when 99% coverage tests start to have power:
- 1-day horizon: about one year;
- 5-day horizon: about five years.

Kalshi labels arrive daily. Each settlement day adds one range ladder and about ten
above/below strikes near the money.
