# Eigenrepresentations against eigentensor representations, 2026-11-04

**Verdict: on this data, tensors add nothing that survives checking. Keep the matrices.** No tensor beat the best matrix
method on VaR, and the fullest one was significantly worse. The one tensor result that beat its null came from markets
closing at different times, not from market structure. This is a small sample, so it rules out large effects, not small
ones.

Research only: nothing on the order path reads this. Run on 1,224 daily bars from the IBKR connector for SPY, GLD, TLT,
STW.AX and BHP.AX, November 2021 to November 2026, price only. `scripts/eigen_vs_tensor.py` reproduces it from the saved
bars.

## Terms

The repo had no definition of the last two, so these are the ones used here.

| Term | Meaning here |
|---|---|
| Eigenrepresentation | Eigen-decomposition of the correlation matrix |
| Eigentensor, coskew | Z-eigenpairs of third moments of whitened returns |
| Eigentensor, cokurt | Z-eigenpairs of whitened fourth moments, less their elliptical part |
| Eigentensor, horizon | HOSVD of correlation stacked over 1, 5 and 21 days |
| Eigenfidelity | Squared overlap of a direction with the same estimate on a later, disjoint window. 1 is the same direction; 0.2 is chance with five assets |
| Eigenpersistence | Eigenfidelity above chance, averaged over 1, 2 and 3 windows apart. 1 never moves; 0 is chance |
| Moment capture | The later window's moment along the earlier direction, as a share of the most it could have been |

Whitening removes what correlation explains, and an elliptical law such as a Gaussian or Student-t then has no coskew or
cokurt left beyond one number, kappa, for how heavy its tails are. Whatever remains is what a matrix cannot carry.

## 1. Is there structure beyond correlation?

Largest tensor eigenvalue against 500 simulations of a Student-t fitted to the same tails.

| Sample | Bars | Coskew | Null | p | Cokurt | Null | p |
|---|---|---|---|---|---|---|---|
| Daily, volatility-adjusted | 1,164 | 0.82 | 0.49 | 0.060 | 2.07 | 2.71 | 0.50 |
| Daily, Sydney a day later | 1,163 | 0.79 | 0.49 | 0.068 | 2.03 | 2.75 | 0.48 |
| Weekly, volatility-adjusted | 218 | 1.15 | 0.80 | 0.088 | 3.81 | 2.99 | 0.20 |
| Daily, unadjusted | 1,224 | 0.83 | 0.81 | 0.30 | 7.35 | 6.59 | 0.23 |

Nothing clears 5%. Coskew is the near miss. Its leading direction loads mainly on SPY against TLT, consistent with equity
crashes where bonds do not fall alongside, but no correction for eight tests would leave it standing.

**Horizon stack, and why it does not count.** Same-date, it looks like the one success. Lining up the closes removes it.

| Sample | Share beyond one matrix | Shuffled | p | US-Sydney correlation: 1 day | 5 days | 21 days |
|---|---|---|---|---|---|---|
| All five, same date | 3.86% | 0.69% | 0.002 | 0.05 | 0.32 | 0.37 |
| All five, Sydney a day later | 0.75% | 0.50% | 0.16 | 0.27 | 0.36 | 0.38 |
| US listings only | 0.77% | 0.41% | 0.11 | | | |
| Sydney listings only | 0.10% | 0.06% | 0.19 | | | |

A Sydney session closes before that date's US session opens, so same-date returns miss most of the link and longer
horizons recover it. Within one market there is no horizon structure either.

**Worth keeping from this:** same-date daily correlation between these markets reads 0.05 where the aligned figure is
0.27, so any risk measure built that way would understate how much they move together. Whether the repo's own multi-venue
figures do so has not been checked.

## 2. Eigenfidelity and eigenpersistence

Windows of 252 bars starting every 63, compared only when disjoint. "Stable population" shuffles dates jointly, keeping
the population and removing time. "No structure" shuffles each asset separately. 100 shuffles each, so 0.0099 is the
smallest possible p.

| Representation | Eigenpersistence | Stable population | No structure | p against no structure | p it moved |
|---|---|---|---|---|---|
| Matrix | 0.82 | 0.79 | 0.00 | 0.010 | 0.59 |
| Horizon | 0.86 | 0.54 | 0.00 | 0.010 | not meaningful |
| Coskew | -0.07 | 0.08 | 0.03 | 0.82 | 0.07 |
| Cokurt | -0.03 | 0.02 | 0.07 | 0.79 | 0.25 |

| Representation | Eigenfidelity, 1 / 2 / 3 windows apart | Moment capture, 1 / 2 / 3 windows apart |
|---|---|---|
| Matrix | 0.88 / 0.83 / 0.87 | 0.97 / 0.96 / 0.96 |
| Horizon | 0.88 / 0.86 / 0.94 | 0.95 / 0.95 / 0.98 |
| Coskew | 0.27 / 0.14 / 0.01 | 0.21 / 0.18 / 0.06 |
| Cokurt | 0.31 / 0.09 / 0.13 | 0.22 / 0.15 / 0.11 |

Pairs: 11, 7 and 3 at the three lags, so the third lag is thin.

- **Matrix:** the direction holds up well beyond chance and moves no more than sampling noise explains. It is a broad
  market factor weighted towards the Sydney pair (STW.AX 0.60, BHP.AX 0.61, others 0.22 to 0.33) and carries 33% of the
  variance, so part of its stability is simply that those two names move together.
- **Tensors:** their directions cannot be told apart from noise, with an earlier direction finding only about a fifth of
  the later window's moment.
- **What that does not show:** the stable-population column for coskew and cokurt is also near zero. Even if the true
  direction never moved, 252 bars could not recover it. So this says a one-year window is too short to use these
  directions; it cannot say whether they are stable. Answering that needs much longer history.
- **Horizon's p-moved is not meaningful:** a date shuffle removes the horizon structure the measure depends on.

## 3. Does it improve VaR out of sample?

One-day 99% VaR on 11 long-only books (equal weight and 10 random), 660 days. Volatility is an EWMA forecast for every
method, refit every 21 bars on a 504-bar window, using Cornish-Fisher quantiles. "Elliptical" adds only kappa to a matrix,
so a tensor has to beat fat tails, not just a Gaussian.

| Method | Pinball x10,000 | Breach rate, all books | Breach rate, equal weight |
|---|---|---|---|
| Elliptical, Marchenko-Pastur | 2.548 | 1.06% | 1.06% |
| Elliptical, sample | 2.551 | 1.03% | 1.06% |
| Tensor, rank 1 | 2.553 | 0.90% | 1.06% |
| Elliptical, spectral | 2.557 | 1.13% | 1.21% |
| Gaussian, sample | 2.567 | 1.50% | 1.52% |
| Tensor, rank 2 | 2.575 | 0.79% | 0.91% |
| Gaussian, Marchenko-Pastur | 2.578 | 1.61% | 1.67% |
| Gaussian, spectral | 2.598 | 1.69% | 1.82% |
| Tensor, full | 2.619 | 0.80% | 1.06% |

**The rule, fixed before the run:** a tensor counts as better only if it beats the best matrix method on pinball loss with
a Holm-adjusted Diebold-Mariano p below 0.05. None does.

| Tensor | Against elliptical Marchenko-Pastur | DM | Holm p |
|---|---|---|---|
| Full | 2.8% worse | 3.07 | 0.0065, significantly worse |
| Rank 2 | 1.1% worse | 0.54 | 1.0 |
| Rank 1 | 0.2% worse | 0.14 | 1.0 |

Raw tensors fit noise, and truncating them only brings them level with one number for tail weight. Tensors also run
slightly over-cautious, with average VaR of 2.00% to 2.06% against 1.92%.

**The single tail number:** adding kappa moves breaches from 1.5%-1.7% to 1.0%-1.1%, which is the right direction and the
largest visible effect here. It is not proven: its pinball gain is 0.6% to 1.6%, with p from 0.57 to 0.82. On equal weight,
1.06% is about 7 breaches in 660 days, and the books overlap heavily.

## Where signal is lost

Measured afterwards on the same bars with `signal_loss.py`, beside this file.

| Where | Measured |
|---|---|
| Same-date pairing across time zones | SPY-STW.AX 0.12 same-date, 0.59 aligned; SPY-BHP.AX 0.05 and 0.32; TLT-STW.AX -0.01 and 0.17 |
| Joining five calendars | 1,224 returns from 1,263 bars, 3% lost |
| Rank-1 truncation | Keeps 68% of the coskew norm (noise 62%) and 52% of cokurt (noise 61%) |
| Rank-2 truncation | Keeps 81% of coskew (noise 76%) and 66% of cokurt (noise 71%) |

Same-date pairing is where nearly all the measurable loss is, and sections 2 and 3 ran same-date, so their tensor results
may understate what aligned data would show. Truncation keeps little more than it would of noise, so it is not discarding a
concentrated signal. A collector to accumulate aligned data is queued in `NEXT_SESSION.md` section 18.

## Limits

- **Five names, about 1,200 bars.** Thin for third and fourth moments. Failing to find structure here is not proof that
  none exists.
- **Time zones.** Sections 2 and 3 use same-date returns. Only section 1 and the horizon stack were re-run aligned.
- **Not comparable with the gate.** The gate's 1.57% breach rate came from single-asset GJR fits with GPD tails. These are
  EWMA Cornish-Fisher books, so 1.06% here does not mean this method beats the gate.
- **Spectral not tuned.** Gamma is fixed at 0.75, where `bsm_cross_asset.py` tunes it, so this may undersell that method.
- **Fixed choices.** The rank-2 deflation is a heuristic, skew and kurtosis are clipped to keep Cornish-Fisher monotone, and
  how often that clip applied was not counted. Prices exclude dividends.
- **Simulated nulls are independent draws.** Volatility clustering left over after the EWMA would make real data look more
  structured than the null, so the tests lean towards finding structure, and still found none.
