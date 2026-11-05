# Forward-read position sizing: scope

**Status:** scope only, drafted 2026-11-04. Nothing here is built, and nothing here changes a live size.

**First evidence, 2026-11-04 (Kraken):** no skill. Across 1,206 reads, on 12 pairs and 10 strategies, rank IC was +0.003
(rotation p = 0.53), +0.003 in the later half, and no strategy cleared the null. By the rule in section 6, phases 3 to 6
do not start on this read. See `scripts/forward_read_evidence.py`.

**IBKR evidence, 2026-11-04:** 10 saved series (1,119 reads). Pooled rank IC was +0.193 (p < 0.001), but that does not
survive inside one strategy on one symbol (-0.187, p = 1.0). The read separates better strategy-and-symbol pairings from
worse ones; it does not pick the better trade within a pairing. Pairing-level weights are the only use still open, and
they need the walk-forward gate, since the pinned parameters were chosen on overlapping history.

**Calibration across inputs, 2026-11-04** (`scripts/strategy_calibration.py`): composite agreement and the BSM digital
probability were scored per strategy. Kraken: 2 of 16 pairs beat the prior win rate, none credibly. IBKR: 5 of 18, the
largest +0.059 on 43 trades. The digital ranked net returns on IBKR mean-reversion strategies (IC +0.28 to +0.45), but
two of those three made worse probabilities than the prior win rate. Graph input: no trades scored, for lack of history.
No input is calibrated well enough to size on.

**Entry and exit tests, 2026-11-05** (`scripts/entry_exit_evidence.py`): composite agreement and the BSM digital
probability, as an entry filter and as an exit, thresholds chosen from closed trades only. 0 of 30 Kraken tests and 0 of
34 IBKR tests passed the pre-set bar. Rules that cut drawdown mostly cut total return with it. The nearest misses were an
IBKR `zscore_ou` composite filter (p = 0.001, but a lower total and 36 trades) and an IBKR `momentum_breakout` Greek exit
(+0.80% a trade, 8 of 9 symbols, p = 0.11, 34 trades). Neither is a basis for a live rule.

**The idea:** size a position by what the algorithm's own record says this signal is likely to do next, net of
costs and over its usual holding period. Today a size comes from how volatile the name has been, and when the
budget runs short every position is cut by the same fraction of its notional.

**The main risk:** a sizer that follows a forecast only helps if the forecast has real skill, and today's paper
record is far too small to show that. So the plan measures first, sizes in shadow next, and acts only after the
walk-forward gate and the user's approval.

---

## 1. How a size is set today

| Step | Code | What it reads | Looks forward? |
|---|---|---|---|
| Base notional | `PositionSizer.size`: `equity x target_vol / annual_vol` | Last 63 bars of realized volatility | No |
| Signal grade | `signal_strength`, in `LiveMonitor` | How strong the indicator is on this bar | No: it is not a return forecast |
| Overlays | `conviction *= mitigation x interpret x weight_bias` | Intel state and allocator bias | Only as risk trims |
| Slots | `risk/entry_allocation.py`, step 1 | Ranks new positions by conviction | No |
| Shared budget | `risk/entry_allocation.py`, step 3 | Same pro-rata scale on every candidate | No: this is the notional-proportional step |
| Gates | `Router._gate`, spectral VaR if on | Caps and tail loss | The spectral gate reads a forward tail, but can only trim or reject |

Two signals with very different histories get the same size if their volatility and grade match. Nothing asks
whether this strategy has made money, on this symbol, from a signal like this one.

## 2. The forward read

A `ForwardRead` is a new, frozen record produced for each entry candidate. It answers only from what was known
before the bar it is made on.

| Field | Meaning | Source to reuse |
|---|---|---|
| `horizon_bars` | Typical holding time for this strategy and symbol | Walk-forward trades |
| `edge` | Expected return over the horizon, net of cost, shrunk | Walk-forward trades, `backtest/costs.py`, `execution/venue_costs.py` |
| `edge_lcb` | A conservative lower bound on `edge` | Same, with its uncertainty |
| `p_win` | Share of similar trades that made money | Same |
| `risk` | Forecast return volatility over the horizon | `bsm_event.fit_gjr` or `models/timeseries.py:rolling_garch_vol` |
| `tail_loss` | Forecast loss at the VaR level over the horizon | `bsm_event.horizon_quantiles`, as the spectral gate uses it |
| `n_effective`, `as_of`, `model_version` | How much evidence stands behind it, and what produced it | New |

**How `edge` is estimated.** Take out-of-sample walk-forward trades only, keyed by strategy, symbol and signal
state (signal-strength bucket and volatility regime). Most cells will hold few trades, so each is shrunk towards
its wider pools: strategy and asset class, then strategy, then zero. A thin cell therefore reads close to no edge,
which is the point of the shrinkage.

**Time.** A trade can inform a read only if it had closed before the read's `as_of`, with a purge gap of one
horizon. `analysis/labeling.py` stays evaluation-only, and its guard test stays as it is. A read is a sizing input
and never a strategy feature, so `entry` and backtest signals do not change.

## 3. From read to size

```
base    = target_vol / forecast_annual_risk         # today's volatility target, on a forward risk
tilt    = clip(read_ratio / reference_ratio, 0, 2)  # read_ratio = edge_lcb / risk
tail    = tail_budget / tail_loss                   # most exposure the loss budget allows
weight  = min(base x tilt, tail, max_leverage)      # fraction of equity, before gates
```

* **No Kelly here.** By the user's rule, fractional Kelly is only for all-or-nothing bets (racing, gamble or not).
  These positions have a continuous range of outcomes, so they use a risk budget tilted by the read.
* **`tilt` may exceed 1.** By the user's decision, a read can size above today's volatility-target size. The cap of 2
  is proposed, and leverage, per-symbol and heat gates still bind.
* **No edge or no read:** see decision 1.
* **The shared budget stops being pro rata.** When candidates exceed the budget, it goes first to the highest
  `edge_lcb / risk`, then falls off, rather than cutting every notional by the same factor. Slot ranking uses the
  same key. The eigen-aware ranking already queued in `NEXT_SESSION.md` should be judged alongside this, since both
  change the slot order.
* **Overlays stay as trims.** Mitigation and thesis trims still multiply `weight`, and may only reduce it.
* **Exits and reductions are not sized by this.** The way out stays as it is.

## 4. What does not change

* Every order still passes through `Router`, with every gate in the same order. A read can never relax a gate.
* The kill-switch, heat, per-trade risk, leverage, per-symbol caps and spectral VaR stay as hard limits.
* The ATR stop stays, and sizing v2's quantity rules still round the result.
* **Runner parity.** The knob goes in `config/trading.yaml` and `SessionPlan`, so paper and money are one build and
  it enters the plan's fingerprint. No entry is needed in `NECESSARY_DIFFERENCES`.
* **The autonomous daemon** stays on current sizing until it is separately approved, because no human reviews it.
* **No removals.** `size_policy: volatility` remains the default and the fallback.

## 5. Audit

* Add a `SIZING_READ` event to `LedgerEvent` before anything emits it, carrying the whole read and the weight it
  implied, keyed by `intent_id`.
* Extend `state/sizing_decisions.jsonl` with the read, so a size can be explained afterwards.
* The read becomes part of what `trading audit replay` needs. It is an input read from outside the intent, so if it
  ever acts inside a gate it must go in `GateDecision.observed`.

## 6. Phases

| Phase | What | Done when | Needs |
|---|---|---|---|
| 0 | Trade table: every walk-forward trade with its signal state, holding time and net return | Reproducible, and purge tested | Existing walk-forward runs |
| 1 | `analysis/forward_read.py`, with a test that fails on lookahead | Future trades cannot change a read | Phase 0 |
| 2 | Calibration report: do higher `edge` cells earn more out of sample? | Date-rotation null from `risk_lens.verdict`, reported per asset class | Phase 1 |
| 3 | Backtest A/B, same sessions, volatility sizing against forward-read sizing | Pre-registered metrics in section 7 | Phase 2 shows skill |
| 4 | Shadow on paper: log the read and the size it would have given, and trade today's size | Enough matured reads (decision 3) | Phase 3 passes |
| 5 | Acting on paper, behind `size_policy: forward_read` | Paper beats shadow on section 7 | User approval |
| 6 | Money | Fingerprint matches the paper sessions | User approval, and the normal go-live checks |

**Stop at phase 2 if the read shows no skill.** That is a useful result: volatility sizing stays, and the cost is a
report rather than a worse sizer.

## 7. How it will be judged

Fixed before any result is seen:

* **Primary:** `sortino_over_dd`, on the same sessions, costs included.
* **Beside it:** Sortino, return, maximum drawdown, time in market, turnover, cost drag, and how often the budget or
  a gate binds. A sizer that wins only by trading more or taking more leverage does not pass.
* **Skill:** rank correlation of `edge` with realized net return, against the date-rotation null, out of sample.
* **Calibration:** realized `tail_loss` breaches near the stated level.
* **Overfitting:** results over under 2 years of data are flagged, and every variant tried is counted and reported.

## 8. Risks

| Risk | What guards against it |
|---|---|
| **Too little data.** The real journal holds 11 paper fills and 4 sizing decisions | Walk-forward trades are the base, and shadow runs before acting. Paper fills alone cannot support this |
| **Selection bias.** The walk-forward record comes from strategies already chosen for doing well | Shrink to zero, use `edge_lcb`, and count the variants tried |
| **Concentration.** Forecast sizing piles into the same bet, and the crypto book's mean correlation is about 0.67 | Existing caps and heat, and a phase 3 test for concentration |
| **Regime change.** Past edge stops working | Volatility regime in the key, decay on old trades, and a rule that retires a stale read |
| **Churn.** Reads move and sizes follow | Sized at entry only. Adding on a read is out of scope |
| **Backtest and live differ.** Live fills cost more | Net of venue costs, compared with `fill-quality` |

## 9. Decisions

**Decided by the user, 2026-11-04:**

* Fractional Kelly is for all-or-nothing bets only, and is not used for position sizing.
* Build the evidence first (phases 0 to 2 in `analysis/forward_read.py` and `scripts/forward_read_evidence.py`).
* A read may size above today's volatility-target size.
* Kraken is the base run, without disturbing the crypto sessions: evidence reads public data only.
* IBKR is the venue for North American equities, plus Australia, Hong Kong and Japan options, derivatives and futures.

**Still open:**

1. **What happens to a candidate with no edge or no read?** *Proposed:* no read keeps today's size. A read with
   `edge_lcb <= 0` takes a reduced size, and stand-aside only after its own test.
2. **How much shadow evidence before phase 5?** *Proposed:* at least 100 matured reads, across Kraken and IBKR.
3. **The IBKR line.** Does "options, derivatives and futures" cover North American too, or only the three Asia-Pacific
   markets? This also changes a recorded decision: IB's scope is futures today, and the desk refuses equities there.

## 10. Not in scope

Short positions, pair legs, options, intraday re-sizing, sizing from headline sentiment, and summing books across
brokers. Graph sentiment could later become a key in section 2, but only after it shows skill by itself.
