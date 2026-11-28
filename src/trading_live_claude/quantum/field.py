"""Field equations: the classical field limit of the coupled network.

Static (mean-field self-consistency, one solve per week)::

    m = tanh(h + J m)

``tanh`` is 1-Lipschitz, so the map is a contraction and the solution unique when ``||J||_2 < 1``.
Above that it can split into several solutions (stage-2 validity F1 found two at ``||J||_2 = 1.5``),
and a forecast would depend on where the solver started. So the solver refuses a non-contractive
``J``, and training keeps every ``|J_ij| <= max_norm / (n - 1)``, which bounds ``||J||_2`` by
``max_norm`` (Gershgorin).

Dynamic (mean-field Bloch equations of the transverse-field Ising Hamiltonian)::

    ds_i/dt = B_i x s_i,   B_i = (2 Gamma, 0, 2 h_i + 2 sum_j J_ij s_j^z)

This is the exact evolution with every two-qubit correlation dropped. It is exact without coupling and
close at weak coupling (F2: error 9e-4 at |J| = 0.1, 0.24 at |J| = 0.6). Only where the two differ
can a quantum model say anything its classical twin cannot.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

FloatArray = npt.NDArray[np.float64]


class FieldEquationError(RuntimeError):
    """The field equations have no unique solution, or the solver did not reach one."""


def spectral_norm(J: FloatArray) -> float:
    return float(np.max(np.abs(np.linalg.eigvalsh((J + J.T) / 2)))) if J.size else 0.0


def contraction_box(n: int, max_norm: float) -> float:
    """Entry bound that keeps ``||J||_2 <= max_norm`` for any symmetric, zero-diagonal ``J``."""
    if n < 2:
        return 0.0
    return max_norm / (n - 1)


def _check_coupling(J: FloatArray) -> None:
    if J.ndim != 2 or J.shape[0] != J.shape[1]:
        raise ValueError("coupling must be square")
    if not np.allclose(J, J.T, atol=1e-12) or np.any(np.diag(J) != 0):
        raise ValueError("coupling must be symmetric with a zero diagonal")


@dataclass(frozen=True)
class FieldSolution:
    m: FloatArray
    iterations: int
    residual: float


def solve_static(h: npt.ArrayLike, J: FloatArray, *, start: npt.ArrayLike | None = None,
                 tol: float = 1e-12, max_iter: int = 200,
                 require_contraction: bool = True) -> FieldSolution:
    """Solve ``m = tanh(h + J m)`` for every row of ``h`` (Newton steps, contraction-checked)."""
    hh = np.atleast_2d(np.asarray(h, dtype=np.float64))
    _check_coupling(J)
    if hh.shape[1] != J.shape[0]:
        raise ValueError("h and J disagree on the number of nodes")
    if require_contraction and spectral_norm(J) >= 1.0:
        raise FieldEquationError(f"||J||_2 = {spectral_norm(J):.3f} >= 1: the solution need not be unique")
    m = np.tanh(hh) if start is None else np.array(np.atleast_2d(np.asarray(start, dtype=np.float64)))
    eye = np.eye(J.shape[0])
    residual = np.inf
    for it in range(1, max_iter + 1):
        f = m - np.tanh(hh + m @ J.T)
        residual = float(np.max(np.abs(f)))
        if residual < tol:
            return FieldSolution(m, it - 1, residual)
        D = 1.0 - np.tanh(hh + m @ J.T) ** 2
        A = eye[None, :, :] - D[:, :, None] * J[None, :, :]          # I - diag(D) J
        step = np.linalg.solve(A, f[:, :, None])[:, :, 0]
        m = np.clip(m - step, -1.0, 1.0)
    raise FieldEquationError(f"no convergence after {max_iter} steps (residual {residual:.2e})")


def static_vjp(m: FloatArray, J: FloatArray, g: FloatArray) -> tuple[FloatArray, FloatArray]:
    """Gradients of ``sum(g * m)`` at a solution ``m``: w.r.t. ``h`` and the symmetric ``J``.

    Implicit differentiation: ``(I - diag(D) J) dm = diag(D)(dh + dJ m)`` with ``D = 1 - m**2``,
    so the adjoint solves ``(I - J diag(D)) lam = g``. ``dJ[i, j]`` is the derivative w.r.t. the one
    parameter that sets both ``J[i, j]`` and ``J[j, i]``.
    """
    D = 1.0 - m**2
    eye = np.eye(J.shape[0])
    At = eye[None, :, :] - J[None, :, :] * D[:, None, :]             # I - J diag(D)
    lam = np.linalg.solve(At, g[:, :, None])[:, :, 0]
    dh: FloatArray = D * lam
    G = dh.T @ m
    dJ: FloatArray = G + G.T
    np.fill_diagonal(dJ, 0.0)
    return dh, dJ


def bloch_dynamics(x: npt.ArrayLike, J: FloatArray, gamma: float, t: float, *,
                   fields: npt.ArrayLike | None = None, steps: int = 1000) -> FloatArray:
    """``<Z_i>`` after time ``t`` under the mean-field Bloch equations, from ``RY(arccos x)|0>``."""
    xa = np.atleast_2d(np.asarray(x, dtype=np.float64))
    _check_coupling(J)
    if np.any(np.abs(xa) > 1.0) or not np.all(np.isfinite(xa)):
        raise ValueError("atoms must be finite and lie in [-1, 1]")
    th = np.arccos(xa)
    s = np.stack([np.sin(th), np.zeros_like(th), np.cos(th)], axis=-1)    # (T, n, 3)
    hz = np.zeros(xa.shape[1]) if fields is None else np.asarray(fields, dtype=np.float64)
    dt = t / steps

    def rhs(s: FloatArray) -> FloatArray:
        bz = 2.0 * (hz[None, :] + s[:, :, 2] @ J.T)
        B = np.stack([np.full_like(bz, 2.0 * gamma), np.zeros_like(bz), bz], axis=-1)
        out: FloatArray = np.cross(B, s)
        return out

    for _ in range(steps):
        k1 = rhs(s)
        k2 = rhs(s + 0.5 * dt * k1)
        k3 = rhs(s + 0.5 * dt * k2)
        k4 = rhs(s + dt * k3)
        s = s + dt / 6.0 * (k1 + 2 * k2 + 2 * k3 + k4)
    out: FloatArray = s[:, :, 2]
    return out
