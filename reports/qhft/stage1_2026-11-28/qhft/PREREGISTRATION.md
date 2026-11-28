# Pre-registration: 10-qubit state network vs 2-atom baseline

Frozen 2026-11-28, before any model in this benchmark was fitted or scored. Its SHA-256 is
recorded in the session before the first run. Nothing below may change after that. A change
would make it a new benchmark, reported as such, with this one still reported.

## Question

Can couplings among the ten BSM_Spectral atoms, propagated through a transverse-field Ising
circuit, forecast next week's S&P 500 volatility regime better than a classical model given
only GJR volatility and the VIX premium?

## Data

FRED daily series, downloaded 2026-11-28, aligned on common dates (2,482 days, 2016-11-28 to 2026-11-20):

| File | SHA-256 |
|---|---|
| SP500.csv | c1ba43093ad9c31b9eb50f8cd69a637960cf93eb4f8746b17773764cb603bbdf |
| VIXCLS.csv | 1ea185e0712568d3c83ba416aa5ce705d87c54db42a8ba5e278207db6d4b4da4 |
| DGS2.csv | d025cc4746591063c8e72ff2147891577284f334839aacb72d442bd52b125354 |
| DGS10.csv | 81f894afc4496b080130206fb53b3071f53cebae54169d39a9d6c432593621ac |
| DEXUSEU.csv | 993b5d2500d97ffb4d52701b04e08c6cef27188f1c7fe71c30cc7ac62e3bb49c |
| DEXJPUS.csv | d0f835b0155ad2f4b8ea5745f78221f9ef2eda2eded6766ba5d2f18802ecfb04 |

## Atoms (10, as computed in `validity/v_bsm_atoms.py`, all passed the lookahead test)

`gjr_vol, vol_premium, tail_xi, jet_e3_over_e2, jet_curv_ratio, spx_vix_corr, absorption,
shock_pctile, curve_slope, breach_rate`.
Dropped: `var99` (Spearman +0.99 with `gjr_vol`), the digital's crossing flag (never fired).

## Samples and labels

- Decision days `t = 568, 573, …` (every 5th day from the first day every atom exists), giving 382 non-overlapping weeks.
- Label: next-week realised volatility `sqrt(252/5 * sum r[t..t+4]^2)`. It is known at close `t+5`, which is the next decision day.
- Classes are cut at the 1/3 and 2/3 quantiles of the training labels at each refit: Calm, Elevated, Stress.

## Walk-forward

- First evaluation week: 104, giving two years of training. Refit every 13 weeks.
- A refit at week `b` trains on weeks `0 … b-1`, whose labels have all matured by decision day `t_b`.
- Evaluation covers weeks 104 to 381: 278 weeks.
- Atom normalisation, per refit, from training weeks only: `x = 2*F(a) - 1`, where `F` is the mid-rank empirical CDF, clipped to `[1/(2N), 1 - 1/(2N)]`.

## Models (one readout for all: multinomial logistic, intercepts free, L2 λ = 1.0 on weights)

- **B (baseline).** Features `x_gjr_vol, x_vol_premium`.
- **Q (network).**
  - Encoding: the ten `x_i` encoded as `RY(arccos x_i)|0>`.
  - Hamiltonian: `H = Σ_E J_ij Z_i Z_j + Γ Σ_i X_i`. The edges `E` are atom pairs with training-window `|Spearman ρ| ≥ 0.3`, and `J_ij = κ ρ_ij`, with `κ = 0.5`, `Γ = 0.4`, `t = 1.0`.
  - Evolution: exact, `exp(-iHt)`.
  - Features: `<Z_i>` (10) and `<Z_i Z_j>` for each edge in `E`.

## Primary criterion (decides PASS / FAIL)

- Compute `D = mean over the 278 weeks of (log loss Q - log loss B)`, in nats.
- Its 95% interval comes from a circular block bootstrap: blocks of 8 weeks, 5,000 replicates, seed 20261128.
- **PASS if and only if the upper end of the interval is below 0.**

## Diagnostics (reported; they do not change the verdict)

- **C10:** the readout on the ten `x_i`.
- **C10x:** the ten `x_i` plus `x_i x_j` on the same edges (the classical twin of Q's features).
- **Q0:** Q with `J = 0`.
- **Reference rows:** the uninformed `log 3`, Brier score, accuracy, and evaluation latency.

## Validity tests that must pass before the run (otherwise stop and report)

- **T1.** The NumPy simulator matches Qiskit's Statevector (fidelity ≥ 1 - 1e-10).
- **T2.** The encoded `<Z_i>` equals `x_i`, and energy is conserved under the evolution.
- **T3.** No lookahead: predictions for earlier weeks are unchanged when later data is deleted.
- **T4.** Power: on synthetic data with an interaction planted between two correlated atoms, the machinery detects it (Q or C10x passes against B).
- **T5.** Null: on synthetic data where labels are independent of the atoms, the pass rate over 20 seeds is consistent with a nominal 2.5%.

## Amendment 1 — 2026-11-28, before any model was fitted or scored on real data

**Defect.** Validity test T3 stopped at its precondition (every atom finite on every decision
day). Its benchmark call was never reached, so no real-data model has run.

- For decision days before index 756, `tail_xi`, `var99` and `breach_rate` were computed from
  standardised-residual windows that reached back before the first GJR fit (day 504), where the
  variances are NaN.
- The GPD law then fitted on the finite part only, and `tail_xi` silently returned 0.
- "Day 568" was therefore not "the first day every atom exists", which was the stated intent.

**Change.**
- Those three atoms are computed only when their whole 252-day window is valid.
- Decision days start at the first day all ten atoms are finite: index 796 (2020-02-12),
  stepping by 5. That gives 337 non-overlapping weeks.
- Evaluation covers weeks 104 to 336, which is 233 weeks.
- Everything else above is unchanged: atoms, labels, models, parameters, criterion and diagnostics.

**Consequence noted in advance.** The sample now begins one week before the February 2020 crash,
so the first two training years include it.
