"""Composite decision: a log-linear pool of the models' regime forecasts.

``P(c) ∝ prod_m p_m(c) ** w_m``, with ``w`` on the simplex. The weights are fitted only on
out-of-sample forecasts whose labels have already matured. A model that carries nothing gets a weight
near zero, so the pool cannot be much worse than its best member (do-no-harm, pinned by a test).
"""
from __future__ import annotations

from collections.abc import Sequence

import numpy as np
import numpy.typing as npt
from scipy import optimize
from scipy.special import logsumexp, softmax

FloatArray = npt.NDArray[np.float64]
IntArray = npt.NDArray[np.int64]


def log_pool(probs: Sequence[FloatArray], w: FloatArray) -> FloatArray:
    """Pool ``(n, K)`` forecasts with simplex weights ``w``."""
    if len(probs) != len(w):
        raise ValueError("one weight per model")
    L = sum(wi * np.log(np.clip(p, 1e-15, 1.0)) for wi, p in zip(w, probs, strict=True))
    L = np.asarray(L, dtype=np.float64)
    out: FloatArray = np.exp(L - logsumexp(L, axis=-1, keepdims=True))
    return out


def fit_pool_weights(probs: Sequence[FloatArray], classes: IntArray) -> FloatArray:
    """Simplex weights minimising the pooled log loss on rows with a known class."""
    ok = classes >= 0
    if not np.any(ok):
        raise ValueError("no labelled rows to fit pool weights on")
    logs = np.stack([np.log(np.clip(p[ok], 1e-15, 1.0)) for p in probs])       # (M, n, K)
    c = classes[ok]

    def nll(v: FloatArray) -> float:
        L = np.tensordot(softmax(v), logs, axes=1)
        L = L - logsumexp(L, axis=1, keepdims=True)
        return float(-np.mean(L[np.arange(len(c)), c]))

    res = optimize.minimize(nll, np.zeros(len(probs)), method="BFGS")
    out: FloatArray = softmax(res.x)
    return out
