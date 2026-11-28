# Quantum state network, stage 2: built and validated, not yet run on data

**Status:** the code is in `src/trading_live_claude/quantum/`, with 49 tests. The protocol is drafted
in `PREREGISTRATION_DRAFT.md`, and **no stage-2 data has been fetched.** The next session freezes the
protocol, fetches the data and runs it once. Nothing here touches the Router or any broker.

## What was built, and the validity test behind each piece

| Layer | Module | Validity evidence |
|---|---|---|
| Exact simulator (Schrödinger dynamics, superposition) | `statevector.py` | Matches Qiskit to fidelity 1 - 1e-10 at 2, 5 and 8 qubits. Encoding is exact. Energy is conserved to 1e-9. A Z-only H leaves probabilities unchanged |
| Qiskit circuit (optional `quantum` extra) | `qiskit_bridge.py` | The Trotter circuit converges to the exact evolution as steps grow |
| Field equations | `field.py` | Unique under `‖J‖₂ < 1`, refused above it (F1 found two solutions at 1.5). The implicit-differentiation gradient matches finite differences. The Bloch field limit is exact at J = 0 and within 5e-3 at \|J\| = 0.1 |
| Uncertainty relations | `uncertainty.py` | The reachable set equals the projected Bloch ball. The Maassen-Uffink bound holds on 50k random states. Christoffersen-Diebold limits hold |
| Two-level network (atoms → asset fields → learned couplings) | `network.py` | Exact gradients. A planted spillover is recovered and helps out of sample; null panels learn none. Learned J stays contractive. The quantum arm reduces to the uncoupled family at J = 0 |
| Concurrent panel walk-forward and scoring | `panel.py` | No lookahead (truncation). Concurrent results equal serial ones bit for bit. The week-clustered bootstrap covers zero and widens with cross-asset correlation |
| Composite decision | `composite.py` | Weights lie on the simplex, and the pool does no harm |
| BSM_Spectral atoms and panel loader | `atoms.py` | Point-in-time on BSM's own synthetic market; builds a weekly panel from FRED-style CSVs |
| Fingerprints ("encode state as cryptography") | `fingerprint.py` | Deterministic and content-sensitive. They bind the spec, the inputs and the frozen protocol |

## Experiments run before building (`results/`)

**Uncertainty relations** (`validity/v_uncertainty_field.py`, `results/v_uncertainty_field.txt`; exploratory, on the stage-1 S&P data):
- The complementarity bound holds. With no direction view it caps volatility confidence at sin φ,
  and full volatility confidence forces a direction lean of cos φ.
- On S&P weeks, φ ≈ 143° (ρ = −0.80). Honest forecasts break the constraint in only 1% of weeks.
- A single qubit cannot hold the observed −0.30 correlation between direction and vol-up outcomes;
  it holds the marginals only.
- Christoffersen–Diebold was slightly worse than the base rate (0.6850 vs 0.6808).

**Your population hypothesis** (`power_population.py`, `results/power_population.txt`): significance
amplifies with more concurrent assets only for a real effect, and only as far as the assets are
independent.

| Case | Pass rate, 1 → 16 assets |
|---|---|
| Real gain, independent assets | 9% → 53% |
| Real gain, assets correlated at ρ = 0.5 | 6% → 14% |
| Real gain, assets correlated at ρ = 0.9 | about 10% |
| No effect | 2–4% at any N |
| Real loss | 0% at any N |

The real machinery detects planted spillover 8/8 at 2, 4 and 8 assets, with 0/8 false passes.

## Next session (data)

```bash
python reports/qhft/stage2/freeze.py                       # 1. freeze before seeing data
reports/qhft/stage2/fetch_stage2.sh /tmp/fred2             # 2. fetch (refuses if not frozen)
.venv/bin/python -m pytest -q tests/test_quantum_*.py      # 3. validity tests
.venv/bin/python reports/qhft/stage2/run_stage2.py /tmp/fred2 --workers 8   # 4. the one run, ~25 min
```

Setup: `uv sync --extra dev --extra quant --extra quantum`. The FRED host must be reachable: this
session's "FRM-Claude market data" environment allows it.
