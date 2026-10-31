# BSM spectral code base — reconstruction notes

Rebuilt from the draft paper *Spectral Value-at-Risk for Binary Event Contracts: An Analysis
of the BSM Code Base* (`../Black-Scholes-Merton-Spectral.pdf`). The paper's four modules
weren't in this folder, so they were rewritten from its text. This folder's existing files
(`bsm_parallel_surface.py`, `financial_spectral_filters.py`, `sp100_mp_portfolio_risk.py`)
were left unchanged and used as references.

Dependencies: Python 3.11+, NumPy, SciPy. Nothing else; no network access except the two
Kalshi collectors.

| Module | Paper | Self-test | Other entry points |
|---|---|---|---|
| `bsm_event.py` | §3–7, core engine | `--selftest` | `--demo`, `--backtest --synthetic \| --data DIR`, `--events FILE --data DIR`, `--events-synthetic` |
| `bsm_spectral.py` | §2, predecessor (vanillas, straddles) | `--selftest` | `--demo`, `--backtest --synthetic \| --data DIR` |
| `bsm_microstructure.py` | §8.1 | `--selftest` | `collect`, `analyze`, `signals` subcommands |
| `bsm_cross_asset.py` | §8.2 | `--selftest` | `--demo`, `--data DIR [--balance]` |

Data files are `date,value` CSVs in a directory you pass with `--data`:
- `SP500.csv` and `VIX.csv` for the S&P and VIX.
- `DGS2.csv`, `DGS10.csv`, `DEXUSEU.csv` and `DEXJPUS.csv` for the cross-asset module.

FRED/Yahoo layouts work, and `.` or blank cells are treated as missing. No real data was
supplied, so every backtest has so far run only on synthetic data. That exercises the code
but says nothing about market performance.

## What the self-tests verify (all passing, NumPy 2.4.6 / SciPy 1.17.1)

| Paper claim | Reconstructed result |
|---|---|
| Jet vs finite differences ≈ 2e-6 … 2e-8 | 7.5e-15 … 1.7e-14 against a 60-digit `decimal` finite-difference reference |
| Taylor-4 residual exponent 5.05 (5 expected), degree-6 exponent 7.03 (7) | 5.010 and 6.990, from a two-term fit that removes next-order bias |
| Skewed digital = −∂C/∂K to ≈1e-11 | 1.3e-13 |
| Latent price martingale, mean change ≈1e-17 | 3.1e-17 in double precision (the rounding floor); 8.1e-59 at 60 digits |
| E[(½z′Mz)²] = ¼[(tr M)² + 2 tr M²] vs quadrature, 3e-16 | 3.9e-16 in double precision (2 ulp); 3.6e-60 with 60-digit Gauss–Hermite |
| Diffusion kurtosis ≈3 from 10 s to 1,800 s | 3.01, 2.94, 2.77, 2.62 |
| Jump kurtosis decays like 1/dt (slope −1.09) | 46.5 at 10 s → 2.7 at 1,800 s, slope −1.021 (precision-weighted level fit; −0.95 to −1.06 across six seeds) |
| TSRV within ~11%, raw RV off ≈65× | 1.1% with the plug-in optimal K (16.1% at K = n^(2/3)); raw off 65× |
| OFI regression positive, R² 0.47 | positive, R² 0.65 |
| Almgren–Chriss numerical vs closed form within 9e-4 | 2.4e-15 (exact Newton solve) |
| Kurtosis penalty speeds liquidation (0.118 → 0.033) | 0.161 → 0.134 |

Extra checks, not claimed in the paper:
- Jets agree with the hand-derived Greeks in `bsm_parallel_surface.py` to 1e-13.
- Aitchison powering equals Fisher-z scaling.
- GJR-GARCH QML recovers known parameters.
- The GPD and skew-t laws restandardise to mean 0 and variance 1.
- Kupiec, Christoffersen and Berkowitz tests separate good forecasts from bad ones.
- The PID clamp and anti-windup behave as described.
- Hedge algebra, the logit-power MLE, power de-vig, the store round trip, the LOBSTER parser, MP on pure noise, and the 6-factor JetN derivatives all check out.

Demo results that reproduce the paper's qualitative findings:
- **S&P digital energies by order.** 5.9e-2, 7.4e-4, 3.1e-3, 6.7e-4. The linear term dominates, and order 3 exceeds order 2, so the series is not contracting.
- **S&P digital trust gates.** Long and short positions both fail the ratio gate and are repriced. The latent long position passes it.
- **Latent Greek matrix.** It has rank 2 with condition number ≈8e16. Vega is proportional to gamma, because ∂p/∂s = sτ ∂²p/∂x².
- **Hedging.** Variance-minimising and tail criteria can disagree. A truncated-SVD threshold of 0.1 badly degrades the S&P hedge, with RMS rising from 0.0005 to 0.015.

## Beyond the paper (queue, completed 2026-10-30)

1. **Kalshi model vs market.** Most of the gap is information, not modelling.
   - With the market-implied level, the model's AUC (0.945) nearly matches the market's (0.951). The 09:00 quote knows the overnight futures move; the model only has the prior close.
   - The market prices 1-day S&P volatility at about **0.48 × VIX**.
   - The scorecard now also reports:
     - the prior-close model with GJR 1-day vol;
     - VIX scaled by a ratio learned walk-forward from earlier ladders (Brier 0.133 → 0.127 on above/below contracts);
     - a model curve fitted to each 09:00 ladder.
   - On range ladders, the fitted curve **beats the market's own mids** on log loss: −0.0069, with a 95% interval of [−0.0090, −0.0039] (cluster bootstrap). On above/below contracts it ties.
   - All of these are computed at scoring time from point-in-time data; the frozen forward journal is unchanged.
2. **Cross-asset book VaR.** Every 99% breach is a dollar-up, risk-off move in which all four legs lose together. GJR horizon variance also ran low for FX and rates and high for the mean-reverting S&P and VIX.
   - Fixes:
     - a walk-forward horizon-variance calibration (realised-to-forecast ratio from past windows only);
     - Student-t and GPD-with-t-copula models, with degrees of freedom fitted walk-forward;
     - the paper's PID loop with its default gains.
   - With these, FHS and the walk-forward choice pass Kupiec and Christoffersen at both levels: 5.52% at 95%, and 2.03% at 99% (p 0.091 / 0.072).
   - Spectral sharpening with γ = 1.5 does better at 99% (1.74%), but γ was chosen after seeing the results, so it is not adopted.
3. **Correlation scoring.** A multivariate Student-t, with ν = 5.9 fitted on the tuning windows, beats the Gaussian by 0.35 nats per window. It puts 0.48% of shocks outside its 99% ellipsoid, against 4.35% for the Gaussian. Estimator rankings are unchanged under either law.
4. **Live order book** (454 snapshots over 40 minutes, `KXINXU` 4pm at-the-money contract):
   - fat tails (kurtosis 4–5) without jump scaling (slope −0.19);
   - variance per second falls with the interval, the bid-ask-bounce signature;
   - 1-cent tick discreteness dominates (44 distinct mids);
   - order-flow imbalance R² is only 0.04 over the full sample. The 0.93 seen early came from a few large moves.
5. **Module split: declined.** `bsm_event.py` stays one coherent file, organised by its numbered sections.

## All-purpose extensions (2026-10-30)

- **Any underlying.** `bsm_event.UNDERLYINGS` registers the S&P 500, Nasdaq-100, Dow, WTI, Bitcoin, Ether, EUR/USD and USD/JPY. Underlyings without an implied-vol index use a point-in-time GJR forecast.
  - `bsm_event.py` and `bsm_spectral.py` take `--underlying KEY` for real-data runs.
  - The maturation pipeline journals, matures and scores every enabled underlying. See `MATURATION.md`.
  - **First cross-underlying evidence** (20–41 settled days per Kalshi series):
    - The ladder-fitted model curve beats the market's own mids on log loss, with intervals excluding zero, for the S&P range ladder, the Nasdaq range ladder, WTI, Bitcoin (both series) and Ether. It ties on the S&P and Nasdaq above/below contracts and on FX.
    - Markets price 1-day volatility at about half of the underlying's vol index or GJR forecast: 0.47–0.62×, and 0.26–0.33× for FX.
    - The raw WTI model is poor (Brier 0.39 against the market's 0.07). It uses FRED spot while Kalshi settles on futures, so the futures basis enters directly.
- **Multi-modal threat detection, `bsm_detect.py`.** The same false-alarm-control statistics, applied to screening across three sensor modalities:
  - **Spectroscopy:** matched filter with a Marchenko–Pastur-denoised background.
  - **Dual-energy X-ray:** Reed–Xiaoli pixel anomaly detector.
  - **Trace / vapour time series:** CUSUM.

  Scores are calibrated with GPD tails and fused across modalities with correlation-aware weights. Results on synthetic data with placeholder signatures:
  - Tail calibration holds false-alarm rates of 1e-2, 1e-3 and 1e-4 to within 15% using 5,000 clean items.
  - The MP-denoised background quadruples the matched filter's detection rate (0.05 → 0.20) with 200 training spectra for 128 channels.
  - Fisher fusion, which assumes independent sensors, over-alarms about 9× because shared clutter correlates the modalities.
  - Optimal linear fusion (weights R⁻¹δ, from threat simulants) detects 97.6% of threats against 41% for the best single sensor, at a calibrated false-alarm rate of 9e-4 against a 1e-3 target.
  - The PID loop holds the false-alarm rate under baseline drift, and CUSUM flags the drift.

  This is a statistical framework for calibrating and evaluating detectors. It is not a certified screening system.

## Choices made where the paper is silent

- **Skew sign.** The smile is σ_K = v − β ln(F/K), exactly as the paper writes it, so equity skew needs β < 0. The default is −0.3.
- **Tail index.** The Hill estimate at a 10% threshold overstates ξ (0.38 against ~0.2). After restandardisation that squeezes the body: 17.6% breaches at 90%. So k is tuned on the tuning split over {3, 4, 6, 10, Hill}, consistent with the paper listing the tail index among the tuned hyperparameters.
- **Tuning.** The first 40% of evaluation windows are used for tuning, never for scoring. The tail index is tuned first, then the tempering power γ per strike. γ is tied across strikes by a second-difference penalty and solved exactly over the grid.
- **Hedge instruments.**
  - S&P digital: neighbouring strikes at K(1−1%), K(1+1%) and K(1+2%).
  - Latent event: strikes at K−0.25, K+0.25 and K+0.5.
  - Call-put variant: calls and puts at K(1±1%).
  - Hierarchical hedging matches instantaneous delta, gamma and vega.
- **"Spectral" VaR model.** It uses the GPD draws and applies the Taylor-4 polynomial only when the repriced gate trusts it, otherwise exact repricing. The stress radius is the tail-consistent 99% quantile of ‖z‖ rather than the Gaussian 3.035.
- **Almgren–Chriss fourth-moment penalty.** The paper does not give the form. A CARA-consistent fourth-cumulant term is negligible at realistic risk aversion, so the penalty is `kurt_weight × excess_kurtosis × (variance term at full position) × (x/X)⁴`, with `kurt_weight = 0.05`.
- **Daily regime signal.** log(1 + excess kurtosis of 60-second increments), plus a count of liquidity-void alarms.
- **The three missing imports.** `bsm_spectral.py` originally imported `bsm_parallel_surface_4th`, `bsm_signal` and `bsm_direction`, which weren't supplied. They are replaced by the jets, `curvature_signal`, and `direction_track`/`backtest_dipy`. The last two treat the whitened curvature as a diffusion tensor: fractional anisotropy, fibre continuity, crossings.
  - For an ATM straddle that curvature is effectively rank one, so FA ≈ 1 always and is uninformative there.
- **"Alternative level maps."** These are only an affine VIX→ATM-vol map (`BacktestConfig.level_map`). The paper doesn't say what else they covered.

## Engineering issues from the paper's §10, addressed

- Usage lines use the real file names, not `bsm_cross.py` or `bsm_micro.py`.
- There are no drive-letter paths. The Marchenko–Pastur filter is inlined in `bsm_event.mp_filter`, using the same rule as `sp100_mp_portfolio_risk.py`, instead of being loaded from `E:/FRM-Claude-main/...`.
- `bsm_spectral.py` imports nothing that is missing.
- `bsm_event.py` stays one coherent file, about 2,200 lines, matching the paper's four-module layout. It is organised in numbered sections: jets, contracts, spectral risk, GJR, tails, compositional, scoring, control, hedging, calibration, data, backtest, events, self-test.

## Not verified

- Databento-style CSV parsing is untested. LOBSTER parsing is tested on a hand-made three-row file.

## Update 2026-10-30: real data and Kalshi

- **Kalshi collectors.** All three now run against the live public API. Kalshi had moved to `*_dollars` price strings and `*_fp` sizes, so the parsers were fixed; they still accept the old cent format. A `kalshi_markets` source now records a whole strike ladder per request.
- **Real-data runs.** All modules have been run on FRED data (S&P 500 for 10 years, VIX, 2y/10y yields, EUR/USD, USD/JPY).
- **Forward journal.** See `MATURATION.md`.
- **Correlation filters.** The tables now report statistical power, and judge each filter on three things:
  - fit (log-likelihood);
  - realised risk of the minimum-variance portfolio built from it;
  - 99% tail exceedances.
- **Straddle anisotropy across horizons.** `bsm_spectral.anisotropy_by_horizon` tabulates it. Fractional anisotropy now uses curvature magnitudes, so it stays in [0, 1], and saddles are flagged separately.
