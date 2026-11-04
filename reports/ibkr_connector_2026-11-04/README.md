# IBKR connector run, 2026-11-04

Backtest, walk-forward, prediction and paper replay on daily bars from the IBKR connector. Nothing was sent to a
broker. The replay used `PaperBroker` and the real `Router`, with state in a scratch directory and no ledger.

## Data

| Series | Bars | Span | Note |
|---|---|---|---|
| SPY, GLD, TLT | 1,252 | 2021-11-08 to 2026-11-03 | The 11-04 bar was dropped; the session was open |
| STW.AX, BHP.AX | 1,263 | 2021-11-07 to 2026-11-03 UTC | IBKR stamps a Sydney session on the prior UTC evening |
| ES front month | 544 | 2024-09-04 to 2026-11-03 | 9 contracts, rolled on volume, ratio-adjusted at same-day closes |
| SPI 200 front month | 106 | 2026-06-11 to 2026-11-03 | IBKR errored on APZ5, APH6 and APM6 every time |

Limits that apply to every figure:

* Prices exclude dividends, so long results and buy-and-hold are understated, most for TLT, STW.AX and BHP.AX.
* Each book is single-currency, 100,000 in local currency. The desk's CAD conversion was not applied.
* These are five symbols and two futures. Treat results as a smoke test, not validation.
* OHLC was widened where IBKR's close sat just outside the regular-session range (largest 0.42%). No price moved.

## Backtest (in-sample, best of 89 configs)

Selected on the same data it reports, so these flatter every strategy. ES has 2.16 years.

| Symbol | Strategy | Return | Sharpe | Max DD | Closed trades | Buy and hold |
|---|---|---|---|---|---|---|
| SPY | `ts_momentum` | 55.2% | 0.89 | -10.4% | 18 | 60.9% |
| GLD | `atr_channel` | 88.6% | 0.98 | -13.9% | 28 | 110.8% |
| TLT | `rsi_meanrevert` | 7.3% | 0.27 | -11.1% | 4 | -45.6% |
| STW.AX | `rsi_meanrevert` | 16.5% | 0.92 | -2.8% | 9 | 13.9% |
| BHP.AX | `rsi_meanrevert` | 64.5% | 1.18 | -8.3% | 24 | 70.5% |
| ES | `ts_momentum` | 20.0% | 0.92 | -9.6% | 0 | 26.5% |

ES's 20% comes from one position that never closed.

## Walk-forward

This run found that `scripts/sweep_universe.py:walk_forward` scored each test fold on its own, so a lookback longer
than the fold could not warm up and read as 0%. That is now fixed: a fold is scored with its training bars in front as
warm-up, measured only on the test bars, and charged an entry cost for any position it starts with. "Before" is the old
scoring and "after" the fix. Windows are 504/126, except ES at a non-protocol 252/63. Returns are the mean per fold.

| Symbol | Folds | Before | After | Buy and hold | Closed OOS trades, after (bar 10) | Win rate, after |
|---|---|---|---|---|---|---|
| SPY | 5 | 0.7% | 5.6% | 11.3% | 10 | 60% |
| GLD | 5 | 1.8% | 14.9% | 18.5% | 1 | 100% |
| TLT | 5 | 1.0% | 2.5% | -1.2% | 14 | 43% |
| STW.AX | 6 | 0.3% | -0.7% | 4.1% | 9 | 44% |
| BHP.AX | 6 | 4.1% | 3.8% | 4.8% | 8 | 100% |
| ES | 4 | 0.0% | 1.1% | 3.5% | 2 | 50% |

TLT alone beats buy-and-hold, and thinly: buy-and-hold here leaves out roughly 2.5 years of distributions. Picks changed
between folds on every symbol. GLD's `oos_score` of 114 is an artefact of one closed trade, since an open position
never counts as a trade. Warm-up reaches back only to the start of the training window, so a crossover before it
stays invisible.

## Prediction

**ARIMA next-day direction, out of sample.** None beat always guessing the majority direction.

| Symbol | n | Hit rate | Always-majority | p | OOS R² |
|---|---|---|---|---|---|
| SPY | 1,000 | 52.0% | 55.2% | 0.045 (worse) | -0.019 |
| GLD | 1,000 | 54.7% | 55.2% | 0.75 | 0.001 |
| TLT | 1,000 | 46.9% | 50.4% | 0.029 (worse) | -0.033 |
| STW.AX | 1,011 | 50.1% | 52.9% | 0.073 | -0.004 |
| BHP.AX | 1,011 | 51.2% | 51.2% | 1.00 | -0.003 |
| ES | 292 | 50.7% | 52.1% | 0.64 | 0.016 |

**GARCH volatility.** Beat a 20-day rolling figure on QLIKE for all six, by small margins.

**Cross-sectional ranker** (five names, 21-day horizon, 252-bar minimum training). 33 rebalances, mean rank IC 0.109,
t = 1.18, positive 55% of the time. Top name 1.65x against equal weight 1.27x. Not significant, and with five names
the top pick is one holding.

**BSM_Spectral 1-day 99% VaR, refit daily with no future data.** Nominal breach rate is 1%.

| Symbol | Tests | Breaches | Rate | Kupiec p |
|---|---|---|---|---|
| SPY | 750 | 12 | 1.60% | 0.13 |
| GLD | 750 | 18 | 2.40% | 0.001 |
| TLT | 750 | 11 | 1.47% | 0.23 |
| STW.AX | 761 | 12 | 1.58% | 0.14 |
| BHP.AX | 761 | 7 | 0.92% | 0.82 |
| ES | 42 | 0 | 0% | too few |
| Pooled | 3,814 | 60 | 1.57% | |

60 breaches against about 38 expected: the gate understates the tail, most clearly on GLD. The rows are not
independent, so read the pooled figure as direction, not a test.

## Paper replay through the Router

Strategy chosen on the opening bars only (504, or 252 for ES), then replayed. Signal at a close, fill at the next
open. `spectral_var_cap` is 0.005, the example value in `config/trading.example.yaml`.

| Book | Strategy | Entries | Gate off: return / DD | Gate on: return / DD | Entries trimmed by gate | Buy and hold |
|---|---|---|---|---|---|---|
| SPY | `rsi_meanrevert` | 10 | 1.4% / -2.1% | 0.6% / -1.0% | 10 | 73.9% |
| GLD | `rsi_meanrevert` | 3 | 0.3% / -2.0% | 0.3% / -0.9% | 3 | 98.0% |
| TLT | `atr_channel` | 22 | -4.3% / -8.5% | -2.5% / -5.2% | 22 | -7.3% |
| STW.AX | `rsi_meanrevert` | 13 | -1.1% / -3.0% | -0.9% / -2.0% | 13 | 25.4% |
| BHP.AX | `bollinger` | 6 | 3.1% / -3.1% | 1.8% / -1.4% | 6 | 21.2% |
| ES x50 | `ema_crossover` | 0 | No entry | No entry | 0 | 11.9% |
| ES at x5 (MES size) | `ema_crossover` | 1 | 1.6% / -1.7% | Rejected | 0 | 11.9% |

The gate trimmed every entry it could measure, so it acts as a size cut. These runs do not show better risk-adjusted
returns. One ES contract rounds to zero at 1% risk on this book, so the order never reached the Router. The x5 book
uses ES prices with a micro multiplier, not MES bars, and the gate rejected it with 405 of the 500 returns it needs.
SPI has no replay: 106 bars leave no training stretch, and the gate would refuse it for the same reason.

## Futures: ES, ZN, GC, CL

Daily regular-session bars from dated contracts, ratio-adjusted at each roll on same-day closes. About two years each,
because the connector's ladders stop at late 2024: ES 544 bars, ZN 492, GC 503, CL 501. ZN and CL are under two years,
so overfitting risk is high. SPI 200 has 106 bars and is excluded: IBKR errors on its expired contracts.

ES and ZN use every quarterly contract. CL and GC do not hold the front month throughout: CL holds H/M/U/Z, rolling 8
business days before last trade, and GC holds G/M/Q/Z, rolling on volume. Their returns are those of those contracts.
Cost is `CostModel.from_price(is_etf=False)`, built for stocks rather than futures.

**Walk-forward.** The 504/126 protocol needs 630 bars, so no root gets a protocol fold. These use a non-protocol 252/63.
Returns are the mean per fold.

| Root | Folds | Strategy | Buy and hold | Closed trades (bar 10) | Win rate |
|---|---|---|---|---|---|
| ES | 4 | 1.1% | 3.5% | 2 | 50% |
| ZN | 3 | -1.0% | -1.2% | 4 | 25% |
| GC | 3 | -1.5% | 1.7% | 2 | 50% |
| CL | 3 | 2.7% | 18.6% | 2 | 100% |

No root reaches the trade bar, and ZN's lead over buy-and-hold is 0.2 points on a loss.

**Full-sample backtest, best of 89 configs, so overfit by construction.**

| Root | Strategy | Return | Sharpe | Max DD | Closed trades | Buy and hold |
|---|---|---|---|---|---|---|
| ES | `ts_momentum` | 20.0% | 0.92 | -9.6% | 0 | 26.5% |
| ZN | `rsi_meanrevert` | 1.3% | 0.37 | -2.2% | 4 | -2.5% |
| GC | `atr_channel` | 40.7% | 1.03 | -13.1% | 10 | 30.1% |
| CL | `rsi_meanrevert` | 26.5% | 1.55 | -2.7% | 6 | 72.7% |

**Forecasts.** ARIMA direction beat always-majority on none: ES 50.7% vs 52.1%, ZN 48.3% vs 56.3% (p = 0.016, worse),
GC 49.4% vs 50.6%, CL 55.4% vs 55.4%. GARCH beat a 20-day rolling figure on QLIKE for ES, ZN and GC, not CL.

**BSM_Spectral 1-day 99% VaR, refit daily.** This fits from 250 returns so that it can be measured at all; the gate
itself needs 500, so these are not the gate's own fits.

| Root | Tests | Breaches | Rate (nominal 1%) | Kupiec p | Latest VaR |
|---|---|---|---|---|---|
| ES | 292 | 3 | 1.03% | 0.96 | 2.47% |
| ZN | 240 | 7 | 2.92% | 0.015 | 0.71% |
| GC | 251 | 7 | 2.79% | 0.020 | 4.04% |
| CL | 249 | 7 | 2.81% | 0.019 | 7.36% |

**Router replay, 100,000 book.** Strategy picked on the first 252 bars. Sizing is `PositionSizer` at 1% risk and a 2-ATR
stop, not the futures runner's `vol_scaled` cap from `config/futures_universe.json`, so read this as a sizing check on
these settings rather than a forecast of that runner. "Size wanted" is contracts before rounding. x5, x10 and x100
micro books use the full contract's prices with a micro multiplier, not micro bars.

| Book | Entry signals | Size wanted | Outcome |
|---|---|---|---|
| ES x50 | 1 | 0.10 | Below one contract, no order |
| ES x5 | 1 | 1.02 | 1.6% / -1.7% gate off; gate refused it (405 of 500 returns; at a 250 floor, VaR $626 over the $500 cap) |
| ZN x1000 | 1 | 1.51 | Router rejected it: the 50% position cap trims one contract to zero |
| GC x100 | 8 | 0.03 to 0.08 | Below one contract, no order |
| GC x10 | 8 | 0.34 to 0.85 | Below one contract, no order |
| CL x1000 | 3 | 0.16 to 0.57 | Below one contract, no order |
| CL x100 | 3 | 1.6 to 5.7 | 0.9% / -0.2% gate off; gate refused all 3 at 500; at a 250 floor, 2 accepted, 0.6% / -0.2% |

Only the ES and CL micro sizes ever traded, on one to three entries, so this says nothing about returns.

## Options

Not run. The repo has no options runner: only an IB contract type, and `risk/spectral_var.py` models a long position
in the underlying rather than an option's own risk.

## Reproduce

`scripts/` holds the scripts, which expect the bars and an output folder beside them under the names `ibkr_bars` and
`ibkr_out`. The IBKR price bars are not in the repo, since vendor market data may carry redistribution limits; they stay
in `bars/` on the machine that ran this. `ibkr_extract.py` reads this session's own record, so it cannot be rerun elsewhere.
