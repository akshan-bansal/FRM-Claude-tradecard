"""Walk-forward benchmark, exactly as fixed in PREREGISTRATION.md (sha256 d48a44ad...312ed).

Inputs are week-level: A (weeks x 10 atoms, raw units) and y (weeks, next-week realised vol,
NaN where not yet known). Every quantity fitted at a refit uses weeks before the refit only.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np
from scipy import optimize, stats

from sim import Edge, Propagator, encode, readout

# ---- frozen parameters ----
FIRST_EVAL = 104
REFIT_EVERY = 13
L2 = 1.0
EDGE_MIN_RHO = 0.3
KAPPA = 0.5
GAMMA = 0.4
T_EVOLVE = 1.0
BOOT_BLOCK = 8
BOOT_REPS = 5000
BOOT_SEED = 20261128
K = 3
MODELS = ("B", "Q", "C10", "C10x", "Q0")


def ecdf_transform(train: np.ndarray, values: np.ndarray) -> np.ndarray:
    """x = 2 F(a) - 1 with F the mid-rank empirical CDF of `train`, clipped to [1/2N, 1-1/2N]."""
    srt = np.sort(train)
    n = len(srt)
    less = np.searchsorted(srt, values, side="left")
    leq = np.searchsorted(srt, values, side="right")
    F = (less + 0.5 * (leq - less)) / n
    F = np.clip(F, 0.5 / n, 1 - 0.5 / n)
    return 2 * F - 1


def fit_softmax(X: np.ndarray, c: np.ndarray, lam: float = L2) -> tuple[np.ndarray, np.ndarray]:
    m, d = X.shape
    Y = np.eye(K)[c]

    def f(theta: np.ndarray) -> tuple[float, np.ndarray]:
        W, b = theta[: d * K].reshape(d, K), theta[d * K:]
        Z = X @ W + b
        Z -= Z.max(axis=1, keepdims=True)
        logP = Z - np.log(np.exp(Z).sum(axis=1, keepdims=True))
        P = np.exp(logP)
        loss = -np.sum(Y * logP) + 0.5 * lam * np.sum(W * W)
        G = P - Y
        gW = X.T @ G + lam * W
        gb = G.sum(axis=0)
        return float(loss), np.concatenate([gW.ravel(), gb])

    res = optimize.minimize(f, np.zeros(d * K + K), jac=True, method="L-BFGS-B",
                            options={"maxiter": 2000, "gtol": 1e-9})
    if not res.success and res.status != 0:
        raise RuntimeError(f"readout fit failed: {res.message}")
    th = res.x
    return th[: d * K].reshape(d, K), th[d * K:]


def predict_softmax(X: np.ndarray, W: np.ndarray, b: np.ndarray) -> np.ndarray:
    Z = X @ W + b
    Z -= Z.max(axis=1, keepdims=True)
    P = np.exp(Z)
    return P / P.sum(axis=1, keepdims=True)


_PROP0: dict[int, Propagator] = {}


def _uncoupled(n: int) -> Propagator:
    """The J = 0 propagator depends on nothing fitted, so it is built once."""
    if n not in _PROP0:
        _PROP0[n] = Propagator.build(n, {}, GAMMA)
    return _PROP0[n]


def coupling(A_train: np.ndarray) -> tuple[list[Edge], dict[Edge, float]]:
    rho = stats.spearmanr(A_train).statistic
    n = A_train.shape[1]
    edges = [(i, j) for i in range(n) for j in range(i + 1, n) if abs(rho[i, j]) >= EDGE_MIN_RHO]
    return edges, {(i, j): KAPPA * float(rho[i, j]) for i, j in edges}


@dataclass
class Result:
    eval_weeks: np.ndarray
    classes: np.ndarray                      # true class per eval week (-1 = label unknown)
    probs: dict[str, np.ndarray]             # model -> (n_eval, 3)
    edges_per_refit: list[list[Edge]] = field(default_factory=list)
    q_eval_ms: float = 0.0
    refit_s: float = 0.0

    def logloss(self, model: str) -> np.ndarray:
        ok = self.classes >= 0
        P = self.probs[model][ok]
        return -np.log(np.clip(P[np.arange(len(P)), self.classes[ok]], 1e-15, 1.0))

    def brier(self, model: str) -> float:
        ok = self.classes >= 0
        Y = np.eye(K)[self.classes[ok]]
        return float(np.mean(np.sum((self.probs[model][ok] - Y) ** 2, axis=1)))

    def accuracy(self, model: str) -> float:
        ok = self.classes >= 0
        return float(np.mean(self.probs[model][ok].argmax(axis=1) == self.classes[ok]))


def run(A: np.ndarray, y: np.ndarray, baseline_cols: tuple[int, int]) -> Result:
    W_, n = A.shape
    eval_weeks, classes, probs = [], [], {m: [] for m in MODELS}
    edges_log: list[list[Edge]] = []
    q_time, q_count, refit_time = 0.0, 0, 0.0
    for b in range(FIRST_EVAL, W_, REFIT_EVERY):
        tr = np.arange(b)
        tr = tr[np.isfinite(y[tr])]
        ev = np.arange(b, min(b + REFIT_EVERY, W_))
        cuts = np.quantile(y[tr], [1 / 3, 2 / 3])
        c_tr = np.digitize(y[tr], cuts)
        c_ev = np.where(np.isfinite(y[ev]), np.digitize(np.nan_to_num(y[ev]), cuts), -1)

        X_tr = np.column_stack([ecdf_transform(A[tr, i], A[tr, i]) for i in range(n)])
        X_ev = np.column_stack([ecdf_transform(A[tr, i], A[ev, i]) for i in range(n)])

        t0 = time.perf_counter()
        edges, J = coupling(A[tr])
        prop = Propagator.build(n, J, GAMMA)
        prop0 = _uncoupled(n)
        refit_time += time.perf_counter() - t0
        edges_log.append(edges)

        def q_features(X: np.ndarray, p: Propagator) -> np.ndarray:
            return readout(p.evolve(encode(X), T_EVOLVE), edges)

        t0 = time.perf_counter()
        Fq_ev = q_features(X_ev, prop)
        q_time += time.perf_counter() - t0
        q_count += len(ev)
        feats = {
            "B": (X_tr[:, list(baseline_cols)], X_ev[:, list(baseline_cols)]),
            "Q": (q_features(X_tr, prop), Fq_ev),
            "C10": (X_tr, X_ev),
            "C10x": (np.column_stack([X_tr] + [X_tr[:, i] * X_tr[:, j] for i, j in edges]),
                     np.column_stack([X_ev] + [X_ev[:, i] * X_ev[:, j] for i, j in edges])),
            "Q0": (q_features(X_tr, prop0), q_features(X_ev, prop0)),
        }
        for m, (Ftr, Fev) in feats.items():
            Wm, bm = fit_softmax(Ftr, c_tr)
            probs[m].append(predict_softmax(Fev, Wm, bm))
        eval_weeks.append(ev)
        classes.append(c_ev)
    return Result(np.concatenate(eval_weeks), np.concatenate(classes),
                  {m: np.vstack(v) for m, v in probs.items()}, edges_log,
                  1e3 * q_time / max(q_count, 1), refit_time)


def block_bootstrap_ci(d: np.ndarray, block: int = BOOT_BLOCK, reps: int = BOOT_REPS,
                       seed: int = BOOT_SEED) -> tuple[float, float, float]:
    rng = np.random.default_rng(seed)
    n = len(d)
    nb = n // block + 1
    means = np.empty(reps)
    for r in range(reps):
        ii = (rng.integers(0, n, nb)[:, None] + np.arange(block)).ravel()[:n] % n
        means[r] = d[ii].mean()
    lo, hi = np.percentile(means, [2.5, 97.5])
    return float(d.mean()), float(lo), float(hi)


def verdict(res: Result, model: str = "Q", vs: str = "B") -> tuple[float, float, float, bool]:
    d = res.logloss(model) - res.logloss(vs)
    mean, lo, hi = block_bootstrap_ci(d)
    return mean, lo, hi, hi < 0
