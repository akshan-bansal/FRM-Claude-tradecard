# Pre-registration, stage 2: learned couplings, field equations and two uncertainty relations

**Status: DRAFT, not frozen.**

To freeze, before any stage-2 data is fetched:
1. Run `python freeze.py`. It checks the spec fingerprint below against `spec.py` and writes
   `PREREGISTRATION.md` and `FROZEN.json`.
   - `FROZEN.json` binds this text, the spec, and the SHA-256 of every code file the run depends on:
     the `trading_live_claude.quantum` package, `BSM_Spectral/bsm_event.py`, `spec.py`, `run_stage2.py`
     and `fetch_stage2.sh`.
2. Only then run `fetch_stage2.sh`, which refuses until the freeze exists.
3. `run_stage2.py` refuses if the text, the spec or any bound code file differs from the latest record.

Amendments are append-only:
- Add a dated `## Amendment N` section at the end of PREREGISTRATION.md, then run `freeze.py --amend "reason"`.
- Every earlier frozen text must remain a byte-exact prefix, and the script checks that.

**Spec fingerprint (`WalkForwardSpec`, `spec.py`):**
`974b5b5424bdca6c65c95f97cfd3f01d136c105bbd554c44292aa3b6fc49ba10`

## Why a stage 2

Stage 1 (`../stage1_2026-11-28/`) failed. Fixed couplings among ten atoms *within* the S&P made the
forecast worse than the two-atom Bansal_Spectre (BSM_Spectral) baseline: +0.156 nats per week
[+0.084, +0.236]. Stage 2 changes three things and tests each one:

| Change | What is tested |
|---|---|
| Couplings are **learned**, not fixed by atom correlations | P1, P2 |
| Couplings join **assets** (volatility spillover). Atoms feed each asset's local field through shared learned weights | P1, P2 |
| The **field equations** `m = tanh(h + J m)` are a model in their own right: the classical, contraction-guaranteed twin of the quantum arm | P1, and the diagnostic quantum - field |
| Two **uncertainty relations** between direction and volatility, as rival arms | S1, S2 |

## Hypotheses

- **H1.** Learned cross-asset couplings, through the field equations, forecast next-week volatility regimes better than the per-asset two-atom baseline.
- **H2.** The same, through exact transverse-field Ising evolution (8 qubits, one per asset).
- **H3.** Constraining direction and vol-up forecasts to one qubit's reachable set, with axes at the BSM spot-vol angle `phi = arccos(rho)`, improves their joint log loss.
- **H4.** Christoffersen-Diebold direction forecasts, `P(up) = 1 - Phi(-mu/sigma)`, beat the base rate.

Predicted from stage-2 validity work on S&P weeks (exploratory, not part of this test):
- H3 will be near zero: the constraint moved only 1% of S&P weeks.
- H4 will fail: it was slightly worse than the base rate.

These predictions are recorded so a surprise is visible.

## Data

- **Underlyings:** FRED, the eight registered in `BSM_Spectral/bsm_event.UNDERLYINGS`: SPX, NDX,
  DJIA, WTI, BTC, ETH, EURUSD, USDJPY.
- **Macro factors:** DGS2, DGS10, DEXUSEU, DEXJPUS.
- **Calendar:** each asset's atoms use its own series aligned with the macro factors. BTC and ETH are
  therefore on business days, with weekend returns folded into Monday.
- **Weekly grid:** every 5th common day, from the first day on which every asset has every atom.
- **Vol stand-in:** BTC, ETH and FX have no implied-vol index. BSM's point-in-time GJR stand-in
  replaces it, so their `vol_premium` is close to noise. This is known in advance.
- **SPX is a node but is not scored in any primary or secondary test**, because its weeks were seen in
  stage 1. The scored set is the other seven.
- **Overlap:** NDX and DJIA move with SPX, so they add little independent evidence. The population
  power test showed that 16 assets at rho = 0.5 count like about 2.
- **Hashes:** the files' SHA-256 are recorded when they are fetched, after the freeze.

## Atoms, labels, walk-forward

- **Atoms:** the ten BSM_Spectral atoms of `trading_live_claude.quantum.atoms`, as validated in stage 1.
- **Regime label:** next-week realised volatility, cut into per-asset terciles of the training labels at each refit: Calm, Elevated, Stress.
- **Direction label:** `up` = next-week log return above 0.
- **Volatility label:** `vol-up` = next-week realised vol (annualised) above this week's `gjr_vol` atom.
- **Walk-forward:** first evaluation at week 104; refit every 13 weeks; network refits every 26 weeks, warm-started.
- **Point-in-time:** every quantity at a refit uses earlier weeks only. This covers normalisation, class cuts, fits and composite weights.

## Models (all in `SPEC`)

- **B:** per asset, multinomial logistic on `gjr_vol` and `vol_premium`, L2 = 1.
- **uncoupled:** `m = tanh(w . x + c)`, with atom weights shared across assets, and an ordered-logit readout.
- **field:** the static field equations `m = tanh(h + J m)`.
  - `J` is learned within `|J_ij| <= 0.95/7`, so `||J||_2 <= 0.95` and the solution is unique.
  - The L2 penalty on `w` and `J` is 1. Up to 1,000 L-BFGS iterations.
- **quantum:** `tanh(h)` encoded by `RY(arccos .)`, then exact evolution under `sum J Z Z + 0.4 sum X` for `t = 1`.
  - `<Z>` feeds the same readout.
  - `J` is learned within `|J_ij| <= 1`, which allows the strong coupling where it can differ from the field model.
  - Finite-difference L-BFGS, up to 200 iterations, about 0.7 s each.
- **composite:** a log-linear pool of the other four, with simplex weights fitted on matured out-of-sample forecasts. It is B alone until 26 such weeks exist.

## Uncertainty arms (per asset, refit every 13 weeks from week 104)

- **`dir_base`:** `P(up)` = the training share of up weeks.
- **`dir_logit` / `vol_logit`:** binary logistic regression (L2 = 1) of `up` / `vol-up` on the
  normalised `gjr_vol` and `vol_premium`.
- **`dir_cd`:** `P(up) = 1 - Phi(-mu / sigma)`.
  - `mu` = the asset's mean next-week log return over its training weeks.
  - `sigma = gjr_vol * sqrt(5/252)`.
- **`joint_free`:** the log loss of `dir_logit` plus the log loss of `vol_logit`.
- **`joint_complementarity`:** the same two forecasts after `constrain_marginals` with
  `phi = arccos(rho)`, where `rho` is that week's `spot_vol_corr` atom: the 63-day trailing
  correlation of spot returns and vol-index changes, clipped away from +/-1.
  - Infeasible pairs `(2P(up) - 1, 2P(vol-up) - 1)` shrink radially onto the boundary.
  - The share of asset-weeks moved is reported.
- **`joint_cd`:** the log loss of `dir_cd` plus that of `vol_logit`. Reported as a diagnostic only.

## Primary family (two tests, Bonferroni: each at 97.5%)

Each test is the mean over scored asset-weeks of the log-loss difference against B. Its interval comes
from a circular block bootstrap over **weeks**, with all assets of a week kept together: blocks of 8,
5,000 replicates, seed 20261128.

- **P1.** `field - B` **passes iff** the upper end of the 97.5% interval is below 0.
- **P2.** `quantum - B` **passes iff** the upper end of the 97.5% interval is below 0.

## Secondary family (each at 95%)

- **S1.** `joint_complementarity - joint_free`, the summed direction and vol-up log loss. Passes iff the upper end is below 0.
- **S2.** `dir_cd - dir_base`. Passes iff the upper end is below 0.

## Diagnostics (reported; they change no verdict)

- `field - uncoupled`: does coupling itself help?
- `quantum - field`: is there anything beyond the classical field twin?
- `quantum - uncoupled`, `composite - B`, `uncoupled - B`, `dir_logit - dir_base`.
- Per-asset losses, the count of non-converged refits, the last composite weights, and the
  cross-asset correlation of loss differences (the realised design effect).

## Validity tests that must pass first (otherwise stop and report)

1. `pytest tests/test_quantum_core.py tests/test_quantum_network.py tests/test_quantum_panel.py`:
   - the simulator equals Qiskit;
   - encoding is exact, and energy is conserved;
   - the field equations are unique and their gradients exact;
   - a planted coupling is found, and the null shows none;
   - lookahead, serial vs concurrent, the bootstrap's coverage, and composite do-no-harm.
2. `python power_population.py`: reproduces `results/power_population.txt`.
3. On the fetched data, `run_stage2.py` itself checks two things before printing any verdict. If
   either fails, it stops with no verdict.
   - Every atom is finite on every decision day.
   - `walk_forward(panel.truncate(200))` reproduces the full run's forecasts for every model, to 1e-9.

## Deferred: graph-informed couplings

You asked for couplings informed by the intel graph once realtime run data reaches it
(`state/intel_graph.jsonl`, see `GRAPH_INFLUENCE_SCOPE.md`). That arm is not in this protocol:

- **What it would be:** a prior mask or sign on `J` from symbol-symbol edge weights.
- **Why it waits:** it needs those edges to exist from real sessions first.
- **How it would be tested:** its own pre-registration, with its own fresh evaluation weeks.
