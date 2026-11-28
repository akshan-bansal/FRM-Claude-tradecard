"""Exact simulator, field equations and uncertainty relations of ``trading_live_claude.quantum``.

Written before the implementation. Every claim the research record relies on is pinned here:
the simulator matches Qiskit, the encoding is exact, energy is conserved, the field equations are
unique only under contraction and track the exact dynamics at weak coupling, and both uncertainty
relations hold where they should.
"""
from __future__ import annotations

import math

import numpy as np
import pytest
from scipy.linalg import expm

from trading_live_claude.quantum import field, statevector, uncertainty
from trading_live_claude.quantum.statevector import Propagator, encode, hamiltonian, readout

rng = np.random.default_rng(3)


def _sym(n: int, scale: float, seed: int) -> np.ndarray:
    r = np.random.default_rng(seed)
    J = r.normal(size=(n, n))
    J = (J + J.T) / 2
    np.fill_diagonal(J, 0.0)
    return J * scale / np.abs(J).max()


# ------------------------------------------------------------------ statevector
@pytest.mark.parametrize("n,seed", [(2, 0), (5, 1), (8, 2)])
def test_simulator_matches_qiskit(n: int, seed: int) -> None:
    pytest.importorskip("qiskit")
    from qiskit import QuantumCircuit
    from qiskit.quantum_info import SparsePauliOp, Statevector

    r = np.random.default_rng(seed)
    x = r.uniform(-0.95, 0.95, n)
    fields = r.uniform(-0.3, 0.3, n)
    J = {(i, j): float(r.uniform(-0.5, 0.5)) for i in range(n) for j in range(i + 1, n) if r.random() < 0.5}
    qc = QuantumCircuit(n)
    for i, v in enumerate(x):
        qc.ry(float(np.arccos(v)), i)
    sv0 = Statevector.from_instruction(qc).data
    terms = ([("ZZ", [i, j], v) for (i, j), v in J.items()] + [("X", [i], 0.4) for i in range(n)]
             + [("Z", [i], float(fields[i])) for i in range(n)])
    Hq = SparsePauliOp.from_sparse_list(terms, num_qubits=n).to_matrix()
    ref = expm(-1j * Hq * 1.3) @ sv0

    assert np.allclose(hamiltonian(n, J, 0.4, fields), Hq, atol=1e-13)
    assert np.allclose(encode(x)[0], sv0, atol=1e-13)
    mine = Propagator.build(n, J, 0.4, fields).evolve(encode(x), 1.3)[0]
    assert abs(np.vdot(ref, mine)) ** 2 >= 1 - 1e-10


def test_qiskit_circuit_bridge_matches_exact_in_the_trotter_limit() -> None:
    pytest.importorskip("qiskit")
    from qiskit.quantum_info import Statevector

    from trading_live_claude.quantum.qiskit_bridge import trotter_circuit

    n = 4
    x = rng.uniform(-0.9, 0.9, n)
    J = {(0, 1): 0.3, (1, 2): -0.2, (2, 3): 0.25}
    exact = Propagator.build(n, J, 0.4).evolve(encode(x), 1.0)[0]
    errs = []
    for steps in (4, 16, 64):
        sv = Statevector.from_instruction(trotter_circuit(x, J, 0.4, 1.0, steps)).data
        errs.append(1 - abs(np.vdot(sv, exact)) ** 2)
    assert errs[0] > errs[1] > errs[2] and errs[2] < 1e-4


def test_encoded_z_equals_atoms_and_energy_is_conserved() -> None:
    x = rng.uniform(-1, 1, (40, 8))
    assert np.max(np.abs(readout(encode(x), []) - x)) < 1e-12
    p = Propagator.build(8, {(i, i + 1): 0.3 for i in range(7)}, 0.4)
    psi0 = encode(rng.uniform(-0.9, 0.9, (10, 8)))
    for t in (0.5, 4.0):
        assert np.max(np.abs(p.energy(psi0) - p.energy(p.evolve(psi0, t)))) < 1e-9


def test_draft_circuit_findings_stay_true() -> None:
    """Z-only Hamiltonians leave Z-basis probabilities unchanged; that is why the transverse field exists."""
    x = rng.uniform(-0.9, 0.9, (5, 6))
    p = Propagator.build(6, {(0, 3): 0.7, (1, 4): -0.5}, 0.0, [0.2] * 6)
    before = np.abs(encode(x)) ** 2
    after = np.abs(p.evolve(encode(x), 2.0)) ** 2
    assert np.max(np.abs(before - after)) < 1e-12


def test_coupling_sign_aligns_an_uncertain_atom() -> None:
    x = np.array([[0.0, 0.9]])
    z = {J: readout(Propagator.build(2, {(0, 1): J} if J else {}, 0.4).evolve(encode(x), 1.0), [])[0, 0]
         for J in (0.5, 0.0, -0.5)}
    assert z[0.5] > 0.2 and z[-0.5] < -0.2 and abs(z[0.0]) < 1e-12


@pytest.mark.parametrize("bad", [[[0.2, 1.5]], [[0.2, float("nan")]]])
def test_encode_rejects_invalid_atoms(bad: list[list[float]]) -> None:
    with pytest.raises(ValueError):
        encode(np.array(bad))


def test_simulator_refuses_more_qubits_than_it_can_hold() -> None:
    with pytest.raises(ValueError):
        hamiltonian(statevector.MAX_QUBITS + 1, {}, 0.4)


# ------------------------------------------------------------------ field equations
@pytest.mark.parametrize("norm", [0.5, 0.95])
def test_static_field_equations_unique_under_contraction(norm: float) -> None:
    J = _sym(8, 1.0, 7)
    J *= norm / np.linalg.norm(J, 2)
    h = rng.normal(scale=0.3, size=(1, 8))
    sols = {tuple(np.round(field.solve_static(h, J, start=rng.uniform(-1, 1, (1, 8))).m[0], 8)) for _ in range(20)}
    assert len(sols) == 1


def test_static_field_equations_refuse_a_non_contractive_coupling() -> None:
    J = _sym(8, 1.0, 7)
    J *= 1.5 / np.linalg.norm(J, 2)
    with pytest.raises(field.FieldEquationError):
        field.solve_static(np.zeros((1, 8)), J)


def test_box_bound_guarantees_contraction() -> None:
    n, max_norm = 8, 0.95
    bound = field.contraction_box(n, max_norm)
    J = np.sign(_sym(n, 1.0, 9)) * bound
    np.fill_diagonal(J, 0.0)
    assert np.linalg.norm(J, 2) <= max_norm + 1e-12


def test_static_vjp_matches_finite_differences() -> None:
    n, T = 6, 5
    J = _sym(n, 0.12, 11)
    h = rng.normal(scale=0.5, size=(T, n))
    g = rng.normal(size=(T, n))

    def loss(hh: np.ndarray, JJ: np.ndarray) -> float:
        return float(np.sum(g * field.solve_static(hh, JJ).m))

    m = field.solve_static(h, J).m
    dh, dJ = field.static_vjp(m, J, g)
    eps = 1e-6
    for (t, i) in [(0, 0), (3, 4)]:
        hp, hm = h.copy(), h.copy()
        hp[t, i] += eps
        hm[t, i] -= eps
        assert abs((loss(hp, J) - loss(hm, J)) / (2 * eps) - dh[t, i]) < 1e-6
    for (i, j) in [(0, 1), (2, 5)]:
        Jp, Jm = J.copy(), J.copy()
        Jp[i, j] += eps
        Jp[j, i] += eps
        Jm[i, j] -= eps
        Jm[j, i] -= eps
        assert abs((loss(h, Jp) - loss(h, Jm)) / (2 * eps) - dJ[i, j]) < 1e-6


def test_bloch_field_dynamics_exact_without_coupling_and_close_at_weak_coupling() -> None:
    n = 6
    x = rng.uniform(-0.9, 0.9, (3, n))
    exact0 = readout(Propagator.build(n, {}, 0.4).evolve(encode(x), 1.0), [])
    assert np.max(np.abs(field.bloch_dynamics(x, np.zeros((n, n)), 0.4, 1.0) - exact0)) < 1e-9
    Jm = _sym(n, 0.1, 4)
    Jd = {(i, j): float(Jm[i, j]) for i in range(n) for j in range(i + 1, n)}
    exact = readout(Propagator.build(n, Jd, 0.4).evolve(encode(x), 1.0), [])
    assert np.max(np.abs(field.bloch_dynamics(x, Jm, 0.4, 1.0) - exact)) < 5e-3


# ------------------------------------------------------------------ uncertainty relations
@pytest.mark.parametrize("phi_deg", [90.0, 60.0, 143.0, 20.0])
def test_complementarity_region_is_the_projected_bloch_ball(phi_deg: float) -> None:
    phi = math.radians(phi_deg)
    r = rng.normal(size=(20000, 3))
    r *= (rng.random(20000) ** (1 / 3) / np.linalg.norm(r, axis=1))[:, None]
    bd, bv = r[:, 2], r @ np.array([math.sin(phi), 0.0, math.cos(phi)])
    assert np.all(uncertainty.is_feasible(bd, bv, phi))
    # the boundary is reached: b_v = 1 forces b_d = cos(phi); b_d = 0 caps b_v at sin(phi)
    assert uncertainty.is_feasible(np.array([math.cos(phi)]), np.array([1.0]), phi)[0]
    assert not uncertainty.is_feasible(np.array([0.0]), np.array([math.sin(phi) + 1e-3]), phi)[0]


@pytest.mark.parametrize("phi_deg", [90.0, 60.0, 143.0])
def test_entropic_bound_holds_on_random_states(phi_deg: float) -> None:
    phi = math.radians(phi_deg)
    r = rng.normal(size=(50000, 3))
    r *= (rng.random(50000) ** (1 / 3) / np.linalg.norm(r, axis=1))[:, None]
    pd_ = (1 + r[:, 2]) / 2
    pv = (1 + r @ np.array([math.sin(phi), 0.0, math.cos(phi)])) / 2
    total = uncertainty.binary_entropy_bits(pd_) + uncertainty.binary_entropy_bits(pv)
    assert total.min() >= uncertainty.entropic_bound(phi) - 1e-12


def test_projection_is_feasible_idempotent_and_leaves_feasible_pairs_alone() -> None:
    phi = math.radians(143.0)
    bd, bv = rng.uniform(-1, 1, 500), rng.uniform(-1, 1, 500)
    pd_, pv = uncertainty.project_feasible(bd, bv, phi)
    assert np.all(uncertainty.is_feasible(pd_, pv, phi, tol=1e-9))
    again = uncertainty.project_feasible(pd_, pv, phi)
    assert np.allclose(again[0], pd_) and np.allclose(again[1], pv)
    ok = uncertainty.is_feasible(bd, bv, phi)
    assert np.allclose(pd_[ok], bd[ok]) and np.allclose(pv[ok], bv[ok])


def test_phi_follows_the_spot_vol_correlation() -> None:
    assert uncertainty.phi_from_correlation(0.0) == pytest.approx(math.pi / 2)
    assert uncertainty.phi_from_correlation(-0.8) == pytest.approx(math.acos(-0.8))
    assert uncertainty.entropic_bound(math.pi / 2) == pytest.approx(1.0)
    assert uncertainty.entropic_bound(0.0) == pytest.approx(0.0)


def test_christoffersen_diebold_direction_from_volatility() -> None:
    p = uncertainty.christoffersen_diebold_up(np.array([0.002, 0.002, 0.0]), np.array([0.01, 0.04, 0.02]))
    assert p[0] > p[1] > 0.5 and p[2] == pytest.approx(0.5)
    with pytest.raises(ValueError):
        uncertainty.christoffersen_diebold_up(np.array([0.0]), np.array([0.0]))
