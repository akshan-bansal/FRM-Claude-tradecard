"""Validity tests T1-T5 from PREREGISTRATION.md, plus unit checks. All must pass before the real run.

Run: REPO=... FRED=... python -m pytest -q test_qhft.py
"""
from __future__ import annotations

import math
import os
import runpy
import sys
from pathlib import Path

import numpy as np
import pytest
from scipy.linalg import expm

import bench
from sim import Propagator, encode, hamiltonian, readout

HERE = Path(__file__).resolve().parent
rng = np.random.default_rng(3)


# ---------------- T1: simulator == Qiskit ----------------
qiskit = pytest.importorskip("qiskit")
from qiskit import QuantumCircuit  # noqa: E402
from qiskit.quantum_info import SparsePauliOp, Statevector  # noqa: E402


@pytest.mark.parametrize("n,seed", [(2, 0), (5, 1), (10, 2)])
def test_t1_matches_qiskit(n: int, seed: int) -> None:
    r = np.random.default_rng(seed)
    x = r.uniform(-0.95, 0.95, n)
    J = {(i, j): float(r.uniform(-0.5, 0.5)) for i in range(n) for j in range(i + 1, n) if r.random() < 0.4}
    gamma, t = 0.4, 1.0
    qc = QuantumCircuit(n)
    for i, v in enumerate(x):
        qc.ry(float(np.arccos(v)), i)
    sv0 = Statevector.from_instruction(qc).data
    terms = [("ZZ", [i, j], v) for (i, j), v in J.items()] + [("X", [i], gamma) for i in range(n)]
    Hq = SparsePauliOp.from_sparse_list(terms, num_qubits=n).to_matrix()
    ref = expm(-1j * Hq * t) @ sv0

    assert np.allclose(hamiltonian(n, J, gamma), Hq, atol=1e-13)
    assert np.allclose(encode(x)[0], sv0, atol=1e-13)
    mine = Propagator.build(n, J, gamma).evolve(encode(x), t)[0]
    fidelity = abs(np.vdot(ref, mine)) ** 2
    assert fidelity >= 1 - 1e-10


# ---------------- T2: encoding and energy ----------------
def test_t2_encoded_z_equals_atoms() -> None:
    x = rng.uniform(-1, 1, (50, 10))
    z = readout(encode(x), [])
    assert np.max(np.abs(z - x)) < 1e-12


def test_t2_energy_conserved() -> None:
    n = 10
    J = {(i, i + 1): 0.3 * (-1) ** i for i in range(n - 1)}
    p = Propagator.build(n, J, 0.4)
    psi0 = encode(rng.uniform(-0.9, 0.9, (20, n)))
    for t in (0.3, 1.0, 5.0):
        assert np.max(np.abs(p.energy(psi0) - p.energy(p.evolve(psi0, t)))) < 1e-9


def test_encode_rejects_bad_atoms() -> None:
    with pytest.raises(ValueError):
        encode(np.array([[0.2, 1.5]]))
    with pytest.raises(ValueError):
        encode(np.array([[0.2, np.nan]]))


def test_coupling_moves_uncertain_atom_toward_confident_neighbour() -> None:
    """V5b as a regression test: J > 0 aligns, J < 0 anti-aligns, J = 0 does nothing."""
    x = np.array([[0.0, 0.9]])
    z = {J: readout(Propagator.build(2, {(0, 1): J} if J else {}, 0.4).evolve(encode(x), 1.0), [])[0, 0]
         for J in (0.5, 0.0, -0.5)}
    assert z[0.5] > 0.2 and z[-0.5] < -0.2 and abs(z[0.0]) < 1e-12


# ---------------- unit checks on the readout pieces ----------------
def test_ecdf_transform_uses_train_only_and_is_bounded() -> None:
    tr = np.arange(10.0)
    x = bench.ecdf_transform(tr, np.array([-5.0, 0.0, 4.5, 9.0, 100.0]))
    assert np.all(np.abs(x) < 1)
    assert x[0] == x[1] and x[3] == x[4]              # clipped at the training range
    assert abs(x[2]) < 1e-12                            # the training median maps to 0


def test_softmax_recovers_planted_rule() -> None:
    r = np.random.default_rng(5)
    X = r.normal(size=(3000, 2))
    W = np.array([[2.0, 0.0, -2.0], [0.0, 1.5, 0.0]])
    logits = X @ W
    P = np.exp(logits) / np.exp(logits).sum(1, keepdims=True)
    c = np.array([r.choice(3, p=p) for p in P])
    Wf, bf = bench.fit_softmax(X, c, lam=1e-6)
    Pf = bench.predict_softmax(X, Wf, bf)
    assert np.mean(np.abs(Pf - P)) < 0.03


# ---------------- synthetic data for T4 / T5 ----------------
def synth(seed: int, beta: float) -> tuple[np.ndarray, np.ndarray]:
    r = np.random.default_rng(seed)
    W_ = 382
    A = r.normal(size=(W_, 10))
    A[:, 5] = 0.6 * A[:, 3] + 0.8 * r.normal(size=W_)        # a correlated pair -> an edge
    y = 0.6 * A[:, 0] + 0.3 * A[:, 1] + beta * A[:, 3] * A[:, 5] + r.normal(size=W_)
    if beta == 0.0:
        y = r.normal(size=W_)                                  # null: label independent of atoms
    return A, y


def test_t4_power_planted_interaction_is_detected() -> None:
    A, y = synth(11, beta=1.0)
    res = bench.run(A, y, baseline_cols=(0, 1))
    assert all((3, 5) in e for e in res.edges_per_refit)
    q = bench.verdict(res, "Q")
    cx = bench.verdict(res, "C10x")
    print(f"\nT4 planted: Q-B {q[0]:+.4f} [{q[1]:+.4f},{q[2]:+.4f}] pass={q[3]}; "
          f"C10x-B {cx[0]:+.4f} [{cx[1]:+.4f},{cx[2]:+.4f}] pass={cx[3]}")
    assert q[3] or cx[3]


def test_t5_null_false_pass_rate() -> None:
    passes = []
    for seed in range(20):
        A, y = synth(100 + seed, beta=0.0)
        passes.append(bench.verdict(bench.run(A, y, baseline_cols=(0, 1)), "Q")[3])
    print(f"\nT5 null: Q passed {sum(passes)} of 20 seeds (nominal 2.5% -> 0.5 expected)")
    assert sum(passes) <= 3


# ---------------- T3: no lookahead, on the real data ----------------
@pytest.fixture(scope="module")
def real():
    repo, fred = os.environ.get("REPO"), os.environ.get("FRED")
    if not repo or not fred:
        pytest.skip("REPO and FRED not set")
    sys.argv = ["core", repo, fred]
    g = runpy.run_path(str(HERE.parent / "validity" / "v_bsm_atoms_core.py"), run_name="core")
    return g


FIRST_DAY = 796  # Amendment 1: first day all ten atoms are finite
ATOMS = ["gjr_vol", "vol_premium", "tail_xi", "jet_e3_over_e2", "jet_curv_ratio",
         "spx_vix_corr", "absorption", "shock_pctile", "curve_slope", "breach_rate"]


def weekly(g, n_days: int):
    A_d = g["atoms"](n_days) if n_days < len(g["S"]) else g["A"]
    S = g["S"][:n_days]
    r = np.diff(np.log(S))
    idx = np.arange(FIRST_DAY, len(g["S"]) - 5, 5)
    idx = idx[idx < n_days]
    y = np.array([math.sqrt(np.sum(r[t:t + 5] ** 2) * 252 / 5) if t + 5 < n_days else np.nan for t in idx])
    A = np.column_stack([A_d[a][idx] for a in ATOMS])
    return A, y


def test_t3_no_lookahead_real(real) -> None:
    A, y = weekly(real, len(real["S"]))
    assert np.all(np.isfinite(A)), "an atom is missing on a decision day"
    full = bench.run(A, y, baseline_cols=(0, 1))
    w_cut = 250
    cut_day = FIRST_DAY + 5 * w_cut + 1                          # data through week 250's decision day
    At, yt = weekly(real, cut_day)
    assert len(At) == w_cut + 1 and np.isnan(yt[-1])
    part = bench.run(At, yt, baseline_cols=(0, 1))
    k = len(part.eval_weeks)
    assert np.array_equal(part.eval_weeks, full.eval_weeks[:k])
    for m in bench.MODELS:
        assert np.max(np.abs(part.probs[m] - full.probs[m][:k])) < 1e-10, m
