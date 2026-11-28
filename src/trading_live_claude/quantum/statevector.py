"""Exact statevector simulation of the encoded transverse-field Ising network.

Qubit order is Qiskit's (little-endian): basis index ``k`` holds qubit ``i`` in bit ``(k >> i) & 1``,
and spin ``s_i = +1`` for bit 0 and ``-1`` for bit 1, so ``<Z_i> = sum_k p_k s_i(k)``.

The circuit:

* ``|psi0> = prod_i RY(arccos x_i)|0>``, so ``<Z_i> = x_i`` exactly (superposition encoding);
* ``|psi_t> = exp(-iHt)|psi0>``, with ``H = sum_E J_ij Z_i Z_j + sum_i h_i Z_i + Gamma sum_i X_i``.

``H`` is real symmetric, so one eigendecomposition gives the exact propagator for every state that
shares it, with no Trotter error. Without the transverse field, ``H`` is diagonal and the evolution
only changes phases: Z-basis probabilities would never move (stage-1 finding V4).
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

FloatArray = npt.NDArray[np.float64]
ComplexArray = npt.NDArray[np.complex128]
IntArray = npt.NDArray[np.int64]
Edge = tuple[int, int]

MAX_QUBITS = 12  # a dense 4096 x 4096 Hamiltonian; beyond this use the field equations


def _check_size(n: int) -> None:
    if not 1 <= n <= MAX_QUBITS:
        raise ValueError(f"exact simulation supports 1..{MAX_QUBITS} qubits, not {n}")


def basis_spins(n: int) -> IntArray:
    """``(2**n, n)`` array of +/-1 spins."""
    _check_size(n)
    k = np.arange(2**n, dtype=np.int64)
    out: IntArray = 1 - 2 * ((k[:, None] >> np.arange(n, dtype=np.int64)) & 1)
    return out


def encode(x: npt.ArrayLike) -> FloatArray:
    """Rows of atoms in [-1, 1] -> rows of product-state amplitudes (real, unit norm)."""
    xa = np.atleast_2d(np.asarray(x, dtype=np.float64))
    if not np.all(np.isfinite(xa)):
        raise ValueError("non-finite atom")
    if np.any(np.abs(xa) > 1.0):
        raise ValueError("atoms must lie in [-1, 1]")
    n = xa.shape[1]
    _check_size(n)
    th = np.arccos(xa)
    c, s = np.cos(th / 2), np.sin(th / 2)
    bits = ((np.arange(2**n)[:, None] >> np.arange(n)) & 1).astype(bool)
    amp = np.where(bits[None, :, :], s[:, None, :], c[:, None, :])
    out: FloatArray = amp.prod(axis=2)
    return out


def hamiltonian(n: int, couplings: Mapping[Edge, float], gamma: float,
                fields: Sequence[float] | FloatArray | None = None) -> FloatArray:
    """Dense real ``H = sum J_ij Z_i Z_j + sum h_i Z_i + gamma sum X_i``."""
    _check_size(n)
    spins = basis_spins(n).astype(np.float64)
    diag = np.zeros(2**n)
    for (i, j), v in couplings.items():
        if not (0 <= i < n and 0 <= j < n and i != j):
            raise ValueError(f"bad edge {(i, j)} for {n} qubits")
        diag += v * spins[:, i] * spins[:, j]
    if fields is not None:
        f = np.asarray(fields, dtype=np.float64)
        if f.shape != (n,):
            raise ValueError("one field per qubit")
        diag += spins @ f
    H: FloatArray = np.diag(diag)
    k = np.arange(2**n)
    for i in range(n):
        H[k, k ^ (1 << i)] += gamma
    return H


@dataclass(frozen=True)
class Propagator:
    """Exact ``exp(-iHt)`` through one eigendecomposition of ``H``."""

    n: int
    eigval: FloatArray
    eigvec: FloatArray
    H: FloatArray

    @classmethod
    def build(cls, n: int, couplings: Mapping[Edge, float], gamma: float,
              fields: Sequence[float] | FloatArray | None = None) -> Propagator:
        H = hamiltonian(n, couplings, gamma, fields)
        lam, V = np.linalg.eigh(H)
        return cls(n, lam, V, H)

    def evolve(self, psi0: npt.ArrayLike, t: float) -> ComplexArray:
        """Rows of states -> rows of ``exp(-iHt)`` applied to each."""
        psi = np.atleast_2d(np.asarray(psi0))
        coeff = psi @ self.eigvec
        out: ComplexArray = (coeff * np.exp(-1j * self.eigval * t)) @ self.eigvec.T
        return out

    def energy(self, psi: npt.ArrayLike) -> FloatArray:
        p = np.atleast_2d(np.asarray(psi))
        out: FloatArray = np.real(np.einsum("mi,ij,mj->m", p.conj(), self.H, p))
        return out


def readout(psi: npt.ArrayLike, edges: Sequence[Edge]) -> FloatArray:
    """Rows of states -> ``[<Z_i> ..., <Z_i Z_j> for each edge]``. Refuses a state that lost its norm."""
    p = np.abs(np.atleast_2d(np.asarray(psi))) ** 2
    n = p.shape[1].bit_length() - 1
    if 2**n != p.shape[1]:
        raise ValueError("state length is not a power of two")
    if not np.allclose(p.sum(axis=1), 1.0, atol=1e-9):
        raise RuntimeError("state lost its norm")
    spins = basis_spins(n).astype(np.float64)
    cols = [p @ spins] + [(p @ (spins[:, i] * spins[:, j]))[:, None] for i, j in edges]
    out: FloatArray = np.hstack(cols)
    return out


def z_after(x: npt.ArrayLike, couplings: Mapping[Edge, float], gamma: float, t: float,
            fields: Sequence[float] | FloatArray | None = None) -> FloatArray:
    """``<Z_i>`` after encoding ``x`` and evolving for ``t``: the network's propagated atoms."""
    xa = np.atleast_2d(np.asarray(x, dtype=np.float64))
    prop = Propagator.build(xa.shape[1], couplings, gamma, fields)
    return readout(prop.evolve(encode(xa), t), [])
