# Greeks as a space-time jet, under data-backed vol dynamics, 2026-11-06

Research only: nothing on the order path imports `BSM_Spectral/bsm_spacetime.py`, and `bsm_event.py` is untouched, so
the live spectral gate's version hash does not move. `scripts/spacetime_greeks.py` reproduces every table here from
`BSM_Spectral/data`.

## What changed in the representation

`bsm_event` takes a price's derivatives in (spot, implied vol) by truncated Taylor jets and handles time by repricing
at a shorter expiry. `bsm_spacetime` puts time-to-expiry in the jet too, so one jet in (S, v, tau) carries every greek
to fourth total order: delta, gamma, speed, vega, vanna, volga, zomma, ultima, and the time greeks theta, charm, veta
and color, all from the same derivative tensors.

Ito's lemma then acts on that jet as an operator. For state dynamics with drift b and instantaneous covariance a, the
generator is

    L = -d/dtau + b_S d/dS + b_v d/dv + 1/2 a_SS d2/dS2 + a_Sv d2/dS dv + 1/2 a_vv d2/dv2

with every coefficient itself a jet, so L P is a jet again. That gives two things:

- **A consistency check.** Under risk-neutral constant-vol dynamics, L P = r P is the Black-Scholes PDE, so
  `ito_residual` is zero for a consistent price formula.
- **A horizon expansion.** E[P(t+h)] = P + h L P + h^2/2 L^2 P, and the variance from the same series on P^2, under
  any of the state dynamics.

Example, a 63-day at-the-money SPX digital on 2026-10-30 (spot 7,580.07, VIX 19.42, price 0.5601), time greeks per
year of calendar time:

| Greek | Value | Greek | Value |
|---|---|---|---|
| delta | 5.21e-4 | theta | -0.1060 |
| gamma | -3.95e-7 | charm | 1.12e-3 |
| vega | -0.2266 | veta | 0.4325 |
| vanna | -2.60e-3 | color | -8.67e-7 |
| volga | 1.285 | ultima | -19.55 |

## 1. The engine's smile is not a constant-vol solution

| Spot (strike 100) | Flat smile: residual | Skew -0.3: residual per year | Per day, % of price |
|---|---|---|---|
| 85 | 0 | 0.324 | 5.05% |
| 95 | 0 | -0.407 | -0.47% |
| 100 | 0 | -0.458 | -0.33% |
| 105 | 0 | -0.239 | -0.13% |
| 115 | 0 | 0.090 | 0.04% |

With a flat smile the residual is zero to machine precision, which checks the jet and the generator. With the engine's
moneyness skew it is not. That need not be an error: a smile that moves vol with log-moneyness implies vol moves with
spot, and only stochastic-vol dynamics under the pricing measure, which need a market price of vol risk the data here
cannot give, could absorb it. The residual measures how much that missing piece would have to carry.

## 2. Vol dynamics on four indices

| Index | Days | Data vol-of-vol exponent | Data 21-day variance ratio | Log-OU half-life | AIC, random walk | AIC, log-OU | AIC, 3/2 |
|---|---|---|---|---|---|---|---|
| VIX (SPX) | 2,511 | 1.69 | 0.54 | 23 days | -12,574 | **-14,323** | -14,087 |
| VXN (NDX) | 6,472 | 1.06 | 0.55 | 53 days | -34,454 | **-37,356** | -36,727 |
| VXD (DJIA) | 2,512 | 1.40 | 0.45 | 13 days | -12,913 | **-13,444** | -8,826 |
| OVX (WTI) | 4,827 | 1.49 | 0.36 | 49 days | -15,644 | **-23,329** | -23,161 |

- **Reproduces section 12.2.** Half-lives of 13 to 53 days, variance ratios of 0.36 to 0.55, vol-of-vol exponents of
  1.06 to 1.69.
- **Log-OU fits best on all four**, including VIX and OVX, where section 12.2 proposed the 3/2 model. Lower AIC is
  better.
- **The 3/2 fit on VXD fails.** Its AIC is worse than the random walk's, with an implausible 5-day half-life. It is
  reported as is, not tuned.
- **One timescale is not enough.** Log-OU implies 21-day variance ratios of 0.62 to 0.88 where the data shows 0.36 to
  0.55, so mean reversion explains only part of it. That fits section 12.2's long-memory item.
- **Spot-vol correlation** is -0.48 to -0.80 for the equity indices and -0.33 for oil; implied vol overstates realised
  spot vol by 6 to 9% on equities and understates it by 13% on oil.

## 3. The Ito expansion against Monte Carlo

The same 63-day at-the-money digital at each index's last state, 200,000 paths with exact repricing.

| Horizon | Standard deviation error | Mean |
|---|---|---|
| 1 day | Under 0.1% | Within 1 standard error |
| 5 days | Under 0.4% | Within about 1.5 standard errors |
| 10 days | Up to 1.6% | Off by up to 6 standard errors under the random walk |

Second order in h is enough to 5 days. At 10 days the mean drifts and a third-order term, or simulation, is needed.

## 4. Walk-forward: which dynamics forecast a digital's 5-day P&L

Refit every 63 days on the past 504, one forecast every 5 days, never overlapping. The P&L is marked to model on the
realised (spot, vol index) path, as in the engine's own backtest, since there is no digital price history. So this
tests which state dynamics forecast the model's P&L, not market P&L. Quantiles use a normal approximation for every
method alike.

| Index | Forecasts | Pinball change, log-OU vs random walk | p | Pinball change, 3/2 vs random walk | Breaches at 1%: RW / log-OU |
|---|---|---|---|---|---|
| SPX | 401 | **-4.5%** | <0.0001 | -5.3% | 0.0% / 0.0% |
| NDX | 1,193 | **-2.0%** | <0.0001 | -2.3% | 0.34% / 0.42% |
| DJIA | 387 | **-2.1%** | <0.0001 | +34.7% (failed fit) | 0.0% / 0.0% |
| WTI | 864 | **-5.2%** | <0.0001 | -5.3% | 0.69% / 0.81% |

Diebold-Mariano on the pinball loss, Holm-adjusted over the four indices for log-OU.

- **Log-OU beats the random walk on every index**, which is the dynamics `var_snapshot` assumes today.
- **3/2 is as good or slightly better** on SPX, NDX and WTI, and much worse on DJIA because its fit there fails. Log-OU
  is the safer choice.
- **Every method is too wide.** Mean squared z-scores are 0.62 to 0.97 against 1, and breaches run at 0 to 0.8%
  against 1%. That is conservative, but it is miscalibration, and it holds for all three dynamics, so the ranking
  stands and the absolute level does not.

## Limits

- **Model P&L, not market P&L.** No digital price history exists.
- **Normal quantiles.** The digital's P&L is skewed; the ranking uses the same approximation for every method.
- **Spot drift is set to zero** in every fit.
- **Euler quasi-likelihood for 3/2.** Log-OU uses its exact transition, so the AIC comparison slightly favours it.
- **One timescale.** The variance ratios say more structure is there.

## What would put this on the trade path

Nothing does yet. The natural route is section 12.2 item 1: replace `var_snapshot`'s random walk in v with log-OU,
which changes `bsm_event.py` and therefore the gate's version hash, and confirm it on held-out windows first, as
section 12.1 does for the tail index.
