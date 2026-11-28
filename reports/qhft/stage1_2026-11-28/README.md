# Quantum state network, stage 1: validity and a pre-registered benchmark (2026-11-28)

**Result: FAIL.** On S&P 500 data, a 10-qubit transverse-field Ising network over ten BSM_Spectral
atoms forecast next week's volatility regime *worse* than a two-atom classical model.

| | |
|---|---|
| Out of sample | 233 weeks, 2022-03-17 to 2026-11-13 |
| Network minus baseline | +0.156 nats per week, 95% interval [+0.084, +0.236] |

Nothing here touches `src/`, the Router or any broker. It is a research record.

## 1. The draft circuit (`validity/v_circuit.py`, log `results/v_circuit.txt`)

Exact Qiskit statevectors, so there is no shot noise in any comparison.

| Claim in the design discussion | Finding |
|---|---|
| `RY(arccos x)` encodes an atom | Holds: <Z_i> = x_i to 6e-16 |
| `cz(0,4)`, `cz(1,5)`, `cz(2,9)` add cross-factor relations | No effect. Identical outcome probabilities, because a CZ before a Z measurement only changes phase |
| `cx(i,i+1)` chain entangles the network | Its Z-basis output is a classical XOR of coin flips (matches to 1e-17). <Z_j> is the product of atoms 0..j, so the "Execution" qubit reads 0.001 when every atom is 0.5 |
| Schrödinger evolution under H = Σh Z + ΣJ ZZ | No measurable effect. A Z-only Hamiltonian only changes phase |
| Adding a transverse field Γ ΣX | Works. Coupling moves atoms (up to 0.15), energy is conserved to 9e-16, Trotter drift halves per doubling of steps |
| Coupling passes information between atoms | Holds. An uncertain atom moves ±0.30 toward (J > 0) or away from (J < 0) a confident neighbour |
| Speed | 1.75 ms per exact 10-qubit evaluation on a CPU. Hardware queues rule out any HFT path |

## 2. Which BSM_Spectral outputs are atoms (`validity/v_bsm_atoms.py`, `v_partial.py`)

Data: FRED daily series aligned on 2,482 days (2016-11-28 to 2026-11-20). Atoms start on
2020-02-12, giving 337 weekly samples. Fetch with `fetch_fred.sh`; the data is not committed.

- **Lookahead:** all 11 candidates are unchanged when the last 300 days are deleted.
- **Redundant:** `var99` is `gjr_vol` relabelled (Spearman +0.99). The digital's crossing flag never fired.
  The 11 candidates span about 4.5 independent dimensions.
- **Direction:** none of the candidates forecasts next-week direction. `gjr_vol` shows +0.14, but shuffled
  noise reached 0.17 across the 22 tests.
- **Volatility:** `gjr_vol` +0.58, which is the positive control. Beyond what `gjr_vol` already says, only
  `vol_premium` (VIX against GJR) adds anything: +0.28 [+0.18, +0.37].

## 3. The benchmark (`qhft/`, log `results/run_real.txt`)

The rules were fixed in `qhft/PREREGISTRATION.md` before any model ran.

| Version | SHA-256 | Recorded |
|---|---|---|
| Original | `d48a44ad…` | 07:39:40Z |
| With Amendment 1 | `1cc0afbd…` | 07:46:11Z, before any model was fitted on real data |

Amendment 1 corrects the start day. The validity test T3 found three atoms computed from windows
that reached back before the first GJR fit.

- **Validity tests, all passing (`results/tests.txt`):**
  - T1: the simulator matches Qiskit.
  - T2: encoding is exact and energy is conserved.
  - T3: no lookahead, checked on the real data.
  - T4: a planted interaction is detected. Q against B: −0.060 [−0.095, −0.026].
  - T5: on 20 null seeds there were 0 false passes.

| Model | Log loss | Accuracy | Against B |
|---|---|---|---|
| Uniform | 1.099 | — | |
| **B**: `gjr_vol` + `vol_premium` | **0.861** | **65.7%** | |
| C10: ten atoms | 0.907 | 56.2% | +0.045 [+0.004, +0.089] |
| C10x: ten atoms + products | 0.976 | 53.6% | +0.115 [+0.032, +0.216] |
| Q0: circuit, coupling off | 0.948 | 52.4% | +0.087 [+0.035, +0.143] |
| **Q**: 10-qubit network | **1.018** | **47.6%** | **+0.156 [+0.084, +0.236] FAIL** |

Coupling makes the forecast worse: Q against Q0 is +0.069 [+0.037, +0.103].

- **Why it fails:** the two informative atoms are mixed into eight uninformative ones.
  - The couplings are fixed by how atoms correlate with *each other*, which says nothing about next week.
  - A fixed unitary cannot add information, and the readout cannot undo the mixing from 100 to 330 training weeks.
- **Not tested:** learned couplings, other underlyings, real hardware noise. The tuned constants κ, Γ and t
  must not be tuned on these weeks after the fact; any retest needs fresh data.

## Reproduce

```bash
./fetch_fred.sh /tmp/fred        # later downloads differ in the last rows; compare hashes
P=.venv/bin/python               # from the repo root, after `uv sync --extra dev --extra quant` and `uv pip install qiskit`
$P -I validity/v_circuit.py
$P -I validity/v_bsm_atoms.py "$PWD/../../.." /tmp/fred
(cd qhft && REPO="$PWD/../../../.." FRED=/tmp/fred $P -m pytest -q test_qhft.py)
(cd qhft && $P run_real.py "$PWD/../../../.." /tmp/fred)
```
