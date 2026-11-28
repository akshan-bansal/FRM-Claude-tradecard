"""Validity checks on the rough draft's quantum circuit and Hamiltonian, before any build.

Qiskit's exact Statevector is the reference throughout. No measurement sampling: every
distribution below is exact, so a difference is a real difference and not shot noise.
"""
from __future__ import annotations

import itertools
import time

import numpy as np
from qiskit import QuantumCircuit
from qiskit.quantum_info import SparsePauliOp, Statevector
from scipy.linalg import expm

rng = np.random.default_rng(7)
N = 10
x = rng.uniform(-0.9, 0.9, N)  # ten normalised atoms


def encode(xs: np.ndarray) -> QuantumCircuit:
    qc = QuantumCircuit(len(xs))
    for i, v in enumerate(xs):
        qc.ry(float(np.arccos(v)), i)
    return qc


def probs(qc: QuantumCircuit) -> np.ndarray:
    return Statevector.from_instruction(qc).probabilities()


def z_expect(sv: Statevector, n: int) -> np.ndarray:
    return np.array([sv.probabilities([i]) @ np.array([1.0, -1.0]) for i in range(n)])


print("V1  arccos angle encoding: <Z_i> after RY(arccos x_i) should equal x_i")
sv0 = Statevector.from_instruction(encode(x))
print(f"    max |<Z_i> - x_i| = {np.max(np.abs(z_expect(sv0, N) - x)):.2e}")

print("\nV2  the draft circuit: CZ(0,4), CZ(1,5), CZ(2,9) placed just before measurement")
draft = encode(x)
for i in range(N - 1):
    draft.cx(i, i + 1)
with_cz = draft.copy()
with_cz.cz(0, 4)
with_cz.cz(1, 5)
with_cz.cz(2, 9)
d = np.max(np.abs(probs(draft) - probs(with_cz)))
print(f"    max |P(with CZ) - P(without CZ)| over 1024 outcomes = {d:.2e}")

print("\nV3  CX chain on the encoded product state vs a classical coin-flip model")
# Classical model: independent bits b_i ~ Bernoulli((1-x_i)/2), output o_j = b_0 xor ... xor b_j.
p1 = (1 - x) / 2
classical = np.zeros(2**N)
for bits in itertools.product([0, 1], repeat=N):
    p = np.prod([p1[i] if b else 1 - p1[i] for i, b in enumerate(bits)])
    out = np.cumsum(bits) % 2
    idx = sum(int(o) << i for i, o in enumerate(out))  # qiskit is little-endian
    classical[idx] += p
print(f"    max |P(qiskit) - P(10-line XOR coin model)| = {np.max(np.abs(probs(draft) - classical)):.2e}")
sv_draft = Statevector.from_instruction(draft)
zd = z_expect(sv_draft, N)
print("    measured <Z_j> vs the input atom x_j it is labelled as, and vs prod(x_0..x_j):")
names = ["Momentum", "Volatility", "Liquidity", "OrderFlow", "Correlation",
         "Entropy", "Rates", "Credit", "Macro", "Execution"]
for j in range(N):
    print(f"      q{j} {names[j]:<12} x_j={x[j]:+.3f}  <Z_j>={zd[j]:+.5f}  prod={np.prod(x[: j + 1]):+.5f}")
xs_half = np.full(N, 0.5)
dh = encode(xs_half)
for i in range(N - 1):
    dh.cx(i, i + 1)
print(f"    all atoms = +0.5 -> <Z_9 'Execution'> = {z_expect(Statevector.from_instruction(dh), N)[9]:+.5f}")


def pauli_op(h: np.ndarray, J: dict[tuple[int, int], float], gamma: float, n: int) -> SparsePauliOp:
    terms: list[tuple[str, list[int], float]] = []
    terms += [("Z", [i], float(h[i])) for i in range(n) if h[i] != 0]
    terms += [("ZZ", [i, j], float(v)) for (i, j), v in J.items()]
    if gamma:
        terms += [("X", [i], gamma) for i in range(n)]
    return SparsePauliOp.from_sparse_list(terms, num_qubits=n)


h = rng.uniform(-0.5, 0.5, N)
J = {(i, j): float(rng.uniform(-0.5, 0.5)) for i in range(N) for j in range(i + 1, N) if rng.random() < 0.3}

print("\nV4  draft Hamiltonian H_f = sum h_i Z_i + sum J_ij Z_i Z_j (Z terms only)")
Hz = pauli_op(h, J, 0.0, N).to_matrix()
for t in (0.5, 2.0):
    psi_t = Statevector(expm(-1j * Hz * t) @ sv0.data)
    print(f"    t={t}: max |P(t) - P(0)| = {np.max(np.abs(psi_t.probabilities() - sv0.probabilities())):.2e}")

print("\nV5  add a transverse field Gamma * sum X_i  (transverse-field Ising)")
gamma = 0.4
H = pauli_op(h, J, gamma, N).to_matrix()
H0 = pauli_op(h, {}, gamma, N).to_matrix()  # same field, no coupling
t = 1.0
psi = Statevector(expm(-1j * H * t) @ sv0.data)
psi_nc = Statevector(expm(-1j * H0 * t) @ sv0.data)
print(f"    max |P(t) - P(0)|                      = {np.max(np.abs(psi.probabilities() - sv0.probabilities())):.2e}")
print(f"    max |<Z>(coupled) - <Z>(uncoupled)|     = {np.max(np.abs(z_expect(psi, N) - z_expect(psi_nc, N))):.3f}")
e0 = float(np.real(np.vdot(sv0.data, H @ sv0.data)))
e1 = float(np.real(np.vdot(psi.data, H @ psi.data)))
print(f"    energy <H>: before {e0:+.12f}, after exact evolution {e1:+.12f}, |drift| {abs(e1 - e0):.1e}")

print("\n    first-order Trotter circuit (RZ, RZZ, RX gates) vs exact evolution:")
for steps in (1, 2, 4, 8, 16):
    qc = encode(x)
    dt = t / steps
    for _ in range(steps):
        for i in range(N):
            qc.rz(2 * h[i] * dt, i)
        for (i, j), v in J.items():
            qc.rzz(2 * v * dt, i, j)
        for i in range(N):
            qc.rx(2 * gamma * dt, i)
    tr = Statevector.from_instruction(qc)
    fid = abs(np.vdot(tr.data, psi.data)) ** 2
    et = float(np.real(np.vdot(tr.data, H @ tr.data)))
    print(f"      steps={steps:>2}  fidelity={fid:.6f}  |energy drift|={abs(et - e0):.2e}")

print("\nV5b two atoms: does coupling move an uncertain atom toward a confident neighbour?")
for J01 in (+0.5, -0.5):
    for x1 in (+0.9, -0.9):
        xs2 = np.array([0.0, x1])
        s = Statevector.from_instruction(encode(xs2)).data
        Hc = pauli_op(np.zeros(2), {(0, 1): J01}, 0.4, 2).to_matrix()
        Hn = pauli_op(np.zeros(2), {}, 0.4, 2).to_matrix()
        zc = z_expect(Statevector(expm(-1j * Hc * 1.0) @ s), 2)
        zn = z_expect(Statevector(expm(-1j * Hn * 1.0) @ s), 2)
        print(f"    J={J01:+.1f} x1={x1:+.1f}:  x0 0.000 -> {zc[0]:+.4f} (no coupling: {zn[0]:+.4f})")

print("\nV7  latency of one 10-qubit evaluation on this machine (exact simulation, no hardware)")
qc = encode(x)
for (i, j), v in J.items():
    qc.rzz(2 * v * 0.5, i, j)
for i in range(N):
    qc.rx(2 * gamma * 0.5, i)
reps = 200
t0 = time.perf_counter()
for _ in range(reps):
    Statevector.from_instruction(qc).probabilities()
print(f"    qiskit Statevector: {1e3 * (time.perf_counter() - t0) / reps:.2f} ms per evaluation, {len(J)} couplings")
