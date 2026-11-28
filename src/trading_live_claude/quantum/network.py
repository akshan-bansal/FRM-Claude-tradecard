"""The two-level network: atoms -> each asset's local field -> learned asset couplings -> regimes.

**Atom level.**
- Each asset-week's local field is ``h = w . x + c``.
- The weights are shared across assets, and ``x`` are that asset's normalised BSM_Spectral atoms.
- An asset-week with any atom missing gets ``h = 0``: an uncertain node, not a guess.

**Asset level.** Each node's state ``m`` is a function of every field and the couplings ``J``.

| ``kind`` | node state |
|---|---|
| ``uncoupled`` | ``m = tanh(h)``. The control, with no coupling at all |
| ``field`` | the static field equations ``m = tanh(h + J m)``. ``|J_ij| <= max_norm / (n - 1)`` keeps them contractive, so the solution is unique |
| ``quantum`` | ``<Z>`` after encoding ``tanh(h)`` and exact transverse-field Ising evolution with ``J``. Here ``|J_ij| <= quantum_bound``, since unitary evolution needs no contraction. That bound leaves room for strong coupling, the regime where it differs from the field model |

**Readout.**
- An ordered logit on ``m``, shared across assets, with Calm < Elevated < Stress:
  ``P(class <= k) = sigmoid(theta_k - beta m)``, with ``beta > 0`` and ``theta_1 < theta_2``.
- Training minimises the summed log loss plus ``l2/2 (|w|^2 + |J|^2)``.
- ``uncoupled`` and ``field`` use exact analytic gradients (implicit differentiation of the field
  equations). ``quantum`` uses finite differences, which is affordable up to about ten assets.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np
import numpy.typing as npt
from pydantic import BaseModel, ConfigDict, Field
from scipy import optimize
from scipy.special import expit, log_expit

from ..logging_setup import get_logger
from . import field
from .statevector import Propagator, encode, readout

FloatArray = npt.NDArray[np.float64]
IntArray = npt.NDArray[np.int64]
Kind = Literal["uncoupled", "field", "quantum"]

log = get_logger(__name__)


class NetworkSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Kind
    l2: float = Field(1.0, ge=0.0)
    max_norm: float = Field(0.95, gt=0.0, lt=1.0)
    quantum_bound: float = Field(1.0, gt=0.0)
    gamma: float = Field(0.4, ge=0.0)
    t: float = Field(1.0, gt=0.0)
    maxiter: int = Field(500, ge=0)


@dataclass(frozen=True)
class NetworkParams:
    w: FloatArray
    c: float
    J: FloatArray
    beta: float
    th1: float
    th2: float


@dataclass(frozen=True)
class NetworkFit(NetworkParams):
    spec: NetworkSpec
    nll: float            # summed data log loss at the fitted parameters
    objective: float      # nll plus the L2 penalty
    converged: bool
    iterations: int


def _n_upper(spec: NetworkSpec, N: int) -> int:
    return 0 if spec.kind == "uncoupled" else N * (N - 1) // 2


def _pack(spec: NetworkSpec, N: int, K: int, rng: np.random.Generator) -> FloatArray:
    """Starting point: small atom weights, no coupling, a neutral readout."""
    w = 0.1 + 0.01 * rng.standard_normal(K)
    return np.concatenate([w, [0.0], np.zeros(_n_upper(spec, N)), [1.0, -0.5, 0.0]])


def _unpack(theta: FloatArray, spec: NetworkSpec, N: int, K: int) -> NetworkParams:
    w = theta[:K]
    c = float(theta[K])
    P = _n_upper(spec, N)
    J = np.zeros((N, N))
    if P:
        iu = np.triu_indices(N, 1)
        J[iu] = theta[K + 1:K + 1 + P]
        J = J + J.T
    beta, th1, delta = theta[K + 1 + P:K + 4 + P]
    return NetworkParams(np.array(w), c, J, float(beta), float(th1), float(th1 + np.exp(delta)))


def _bounds(spec: NetworkSpec, N: int, K: int) -> list[tuple[float | None, float | None]]:
    box = field.contraction_box(N, spec.max_norm) if spec.kind == "field" else spec.quantum_bound
    free: tuple[float | None, float | None] = (None, None)
    coupled: tuple[float | None, float | None] = (-box, box)
    return ([free] * (K + 1) + [coupled] * _n_upper(spec, N)
            + [(1e-3, 50.0), free, (-10.0, 5.0)])


def _fields(X: FloatArray, w: FloatArray, c: float) -> tuple[FloatArray, npt.NDArray[np.bool_]]:
    seen = np.all(np.isfinite(X), axis=2)
    h = np.where(seen, np.nan_to_num(X) @ w + c, 0.0)
    return h, seen


def node_state(spec: NetworkSpec, p: NetworkParams, X: FloatArray) -> FloatArray:
    h, _ = _fields(X, p.w, p.c)
    if spec.kind == "uncoupled":
        return np.tanh(h)
    if spec.kind == "field":
        return field.solve_static(h, p.J).m
    N = X.shape[1]
    couplings = {(i, j): float(p.J[i, j]) for i in range(N) for j in range(i + 1, N) if p.J[i, j] != 0.0}
    prop = Propagator.build(N, couplings, spec.gamma)
    return readout(prop.evolve(encode(np.tanh(h)), spec.t), [])


def _probs(m: FloatArray, beta: float, th1: float, th2: float) -> FloatArray:
    sa, sb = expit(th1 - beta * m), expit(th2 - beta * m)
    out: FloatArray = np.stack([sa, sb - sa, 1.0 - sb], axis=-1)
    return out


def _check(X: FloatArray, C: IntArray) -> None:
    if X.ndim != 3 or C.shape != X.shape[:2]:
        raise ValueError("X must be (weeks, assets, atoms) and C (weeks, assets)")
    if not np.all(np.isin(C, (-1, 0, 1, 2))):
        raise ValueError("classes must be 0, 1, 2, or -1 for unknown")
    if not np.any(C >= 0):
        raise ValueError("no labelled asset-week to train on")


def _objective(theta: FloatArray, X: FloatArray, C: IntArray,
               spec: NetworkSpec) -> tuple[float, FloatArray]:
    """Penalised log loss and, for ``uncoupled`` / ``field``, its exact gradient (zeros otherwise)."""
    _, N, K = X.shape
    p = _unpack(theta, spec, N, K)
    _, seen = _fields(X, p.w, p.c)
    m = node_state(spec, p, X)
    a, b = p.th1 - p.beta * m, p.th2 - p.beta * m
    sa, sb = expit(a), expit(b)
    P1 = np.maximum(sb - sa, 1e-300)
    obs = C >= 0
    ll = np.where(C == 0, log_expit(a), np.where(C == 1, np.log(P1), log_expit(-b)))
    nll = -float(np.sum(ll[obs]))
    P = _n_upper(spec, N)
    J_up = theta[K + 1:K + 1 + P]
    value = nll + 0.5 * spec.l2 * (float(p.w @ p.w) + float(J_up @ J_up))
    grad = np.zeros_like(theta)
    if spec.kind == "quantum":
        return value, grad
    dLa = np.where(C == 0, -(1 - sa), np.where(C == 1, sa * (1 - sa) / P1, 0.0)) * obs
    dLb = np.where(C == 1, -sb * (1 - sb) / P1, np.where(C == 2, sb, 0.0)) * obs
    g_m = -p.beta * (dLa + dLb)
    if spec.kind == "field":
        dh, dJ = field.static_vjp(m, p.J, g_m)
        grad[K + 1:K + 1 + P] = dJ[np.triu_indices(N, 1)] + spec.l2 * J_up
    else:
        dh = g_m * (1.0 - m**2)
    dh = dh * seen
    grad[:K] = np.einsum("ta,tak->k", dh, np.nan_to_num(X)) + spec.l2 * p.w
    grad[K] = float(np.sum(dh))
    grad[K + 1 + P] = float(np.sum(-m * (dLa + dLb)))
    grad[K + 2 + P] = float(np.sum(dLa + dLb))
    grad[K + 3 + P] = float(np.sum(dLb)) * (p.th2 - p.th1)
    return value, grad


def fit_network(X: FloatArray, C: IntArray, spec: NetworkSpec, *, init: NetworkFit | None = None,
                seed: int = 0) -> NetworkFit:
    """Fit by L-BFGS-B within the coupling box. ``init`` warm-starts from an earlier refit."""
    _check(X, C)
    _, N, K = X.shape
    theta0 = (_pack(spec, N, K, np.random.default_rng(seed)) if init is None
              else _theta_of(init, spec, N, K))
    exact = spec.kind != "quantum"
    if spec.maxiter == 0:
        theta, ok, its = theta0, False, 0
    else:
        if exact:
            res = optimize.minimize(_objective, theta0, args=(X, C, spec), jac=True, method="L-BFGS-B",
                                    bounds=_bounds(spec, N, K),
                                    options={"maxiter": spec.maxiter, "gtol": 1e-7})
        else:
            res = optimize.minimize(lambda th: _objective(th, X, C, spec)[0], theta0, method="L-BFGS-B",
                                    bounds=_bounds(spec, N, K),
                                    options={"maxiter": spec.maxiter, "gtol": 1e-6})
        theta = np.asarray(res.x, dtype=np.float64)
        ok, its = bool(res.success), int(res.nit)
        if not ok:
            log.warning("network_fit_not_converged", kind=spec.kind, message=str(res.message), iterations=its)
    objective, _ = _objective(theta, X, C, spec)
    p = _unpack(theta, spec, N, K)
    P = _n_upper(spec, N)
    J_up = theta[K + 1:K + 1 + P]
    nll = objective - 0.5 * spec.l2 * (float(p.w @ p.w) + float(J_up @ J_up))
    return NetworkFit(p.w, p.c, p.J, p.beta, p.th1, p.th2, spec, nll, objective, ok, its)


def _theta_of(fit: NetworkFit, spec: NetworkSpec, N: int, K: int) -> FloatArray:
    P = _n_upper(spec, N)
    J_up = fit.J[np.triu_indices(N, 1)] if P else np.zeros(0)
    box = field.contraction_box(N, spec.max_norm) if spec.kind == "field" else spec.quantum_bound
    return np.concatenate([fit.w, [fit.c], np.clip(J_up, -box, box),
                           [max(fit.beta, 1e-3), fit.th1, np.log(max(fit.th2 - fit.th1, 1e-9))]])


def predict_network(fit: NetworkFit, X: FloatArray) -> FloatArray:
    """``(weeks, assets, 3)`` regime probabilities."""
    return _probs(node_state(fit.spec, fit, X), fit.beta, fit.th1, fit.th2)


def log_loss(P: FloatArray, C: IntArray) -> float:
    """Mean log loss over labelled asset-weeks."""
    obs = C >= 0
    picked = np.take_along_axis(P, np.where(obs, C, 0)[..., None], axis=-1)[..., 0]
    return float(-np.mean(np.log(np.clip(picked[obs], 1e-15, 1.0))))
