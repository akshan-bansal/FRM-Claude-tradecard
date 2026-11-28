"""Exact statevector simulation of the encoded transverse-field Ising network.

Qubit order is Qiskit's (little-endian): basis index k holds qubit i in bit (k >> i) & 1, and
spin s_i = +1 for bit 0, -1 for bit 1, so <Z_i> = sum_k p_k s_i(k).

The circuit is
    |psi0> = prod_i RY(arccos x_i)|0>         (so <Z_i> = x_i exactly)
    |psi_t> = exp(-i H t)|psi0>,   H = sum_E J_ij Z_i Z_j + Gamma sum_i X_i
H is real symmetric, so one eigendecomposition per (J, Gamma) gives the exact propagator for
every week that shares it. No Trotter error.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

Edge = tuple[int, int]


def basis_spins(n: int) -> np.ndarray:
    """(2^n, n) array of +/-1 spins."""
    k = np.arange(2**n)
    return 1 - 2 * ((k[:, None] >> np.arange(n)) & 1)


def encode(x: np.ndarray) -> np.ndarray:
    """Rows of atoms in [-1, 1] -> rows of product-state amplitudes (real)."""
    x = np.atleast_2d(np.asarray(x, dtype=float))
    if not np.all(np.isfinite(x)):
        raise ValueError("non-finite atom")
    if np.any(np.abs(x) > 1.0):
        raise ValueError("atoms must lie in [-1, 1]")
    n = x.shape[1]
    th = np.arccos(x)
    c, s = np.cos(th / 2), np.sin(th / 2)
    bits = ((np.arange(2**n)[:, None] >> np.arange(n)) & 1).astype(bool)   # (2^n, n)
    amp = np.where(bits[None, :, :], s[:, None, :], c[:, None, :])           # (m, 2^n, n)
    return amp.prod(axis=2)


def hamiltonian(n: int, J: dict[Edge, float], gamma: float) -> np.ndarray:
    """Dense real H = sum J_ij Z_i Z_j + gamma sum X_i."""
    spins = basis_spins(n)
    diag = np.zeros(2**n)
    for (i, j), v in J.items():
        diag += v * spins[:, i] * spins[:, j]
    H = np.diag(diag)
    k = np.arange(2**n)
    for i in range(n):
        H[k, k ^ (1 << i)] += gamma
    return H


@dataclass(frozen=True)
class Propagator:
    n: int
    eigval: np.ndarray
    eigvec: np.ndarray
    H: np.ndarray

    @classmethod
    def build(cls, n: int, J: dict[Edge, float], gamma: float) -> Propagator:
        H = hamiltonian(n, J, gamma)
        lam, V = np.linalg.eigh(H)
        return cls(n, lam, V, H)

    def evolve(self, psi0: np.ndarray, t: float) -> np.ndarray:
        """Rows of states -> rows of exp(-iHt) applied to each."""
        coeff = psi0 @ self.eigvec                       # (m, 2^n): <v_k|psi0> (V real)
        return (coeff * np.exp(-1j * self.eigval * t)) @ self.eigvec.T

    def energy(self, psi: np.ndarray) -> np.ndarray:
        psi = np.atleast_2d(psi)
        return np.real(np.einsum("mi,ij,mj->m", psi.conj(), self.H, psi))


def readout(psi: np.ndarray, edges: list[Edge]) -> np.ndarray:
    """Rows of states -> features [<Z_i> ..., <Z_i Z_j> for edges]."""
    psi = np.atleast_2d(psi)
    n = int(np.log2(psi.shape[1]))
    p = np.abs(psi) ** 2
    if not np.allclose(p.sum(axis=1), 1.0, atol=1e-9):
        raise RuntimeError("state lost its norm")
    spins = basis_spins(n)
    z = p @ spins
    zz = [p @ (spins[:, i] * spins[:, j]) for i, j in edges]
    return np.column_stack([z, *zz]) if zz else z
