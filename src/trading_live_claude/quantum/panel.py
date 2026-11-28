"""Concurrent walk-forward over a panel of assets, and its week-clustered scoring.

**Population.**
- One row per decision week, one column per asset, all evaluated together.
- More assets add samples only to the extent their outcomes are independent. Equity indices move
  together, so the design effect ``1 + (n - 1) * rho`` shrinks their effective count.
- So the scoring bootstrap resamples *weeks* (circular blocks) and keeps every asset of a resampled
  week. The interval then widens with cross-asset correlation instead of pretending each asset-week
  is independent.

**Point-in-time.**
- A refit at week ``b`` sees only weeks ``< b``, and their labels matured by the decision day of
  week ``b``.
- That covers atom normalisation, class cuts, every model, the network refits and the composite
  weights. ``Panel.truncate`` plus a test pins it.

**Concurrency.**
- The per-asset baselines and the network kinds fit in a process pool.
- Every fit is deterministic, and results are gathered in a fixed order, so a concurrent run equals a
  serial one bit for bit (pinned by a test).
"""
from __future__ import annotations

import math
import multiprocessing
from collections.abc import Sequence
from concurrent.futures import Executor, ProcessPoolExecutor
from dataclasses import dataclass, field
from typing import Literal

import numpy as np
import numpy.typing as npt
from pydantic import BaseModel, ConfigDict, Field
from scipy import optimize

from . import composite, fingerprint
from .network import NetworkFit, NetworkSpec, fit_network, predict_network
from .uncertainty import (
    christoffersen_diebold_up,
    complementarity_form,
    constrain_marginals,
    phi_from_correlation,
)

FloatArray = npt.NDArray[np.float64]
IntArray = npt.NDArray[np.int64]
Model = Literal["B", "uncoupled", "field", "quantum", "composite"]
NETWORK_KINDS: tuple[Model, ...] = ("uncoupled", "field", "quantum")


@dataclass(frozen=True)
class Panel:
    """Weekly panel. ``X`` holds raw atoms at each decision day. The labels are next week's, and NaN while unknown."""

    assets: tuple[str, ...]
    atoms: tuple[str, ...]
    dates: tuple[str, ...]
    X: FloatArray          # (weeks, assets, atoms)
    rv_next: FloatArray    # (weeks, assets) next-week realised volatility, annualised
    ret_next: FloatArray   # (weeks, assets) next-week log return
    baseline: tuple[str, str] = ("gjr_vol", "vol_premium")

    def __post_init__(self) -> None:
        W, N, K = (len(self.dates), len(self.assets), len(self.atoms))
        if self.X.shape != (W, N, K) or self.rv_next.shape != (W, N) or self.ret_next.shape != (W, N):
            raise ValueError(f"panel arrays disagree with {W} weeks x {N} assets x {K} atoms")
        missing = [b for b in self.baseline if b not in self.atoms]
        if missing:
            raise ValueError(f"baseline atoms {missing} not in the panel")

    def truncate(self, week: int) -> Panel:
        """Everything known at the decision day of ``week``: its own labels are not."""
        rv, ret = self.rv_next[:week + 1].copy(), self.ret_next[:week + 1].copy()
        rv[week], ret[week] = np.nan, np.nan
        return Panel(self.assets, self.atoms, self.dates[:week + 1], self.X[:week + 1].copy(), rv, ret,
                     self.baseline)

    def col(self, name: str) -> int:
        return self.atoms.index(name)

    def fingerprint(self) -> str:
        return fingerprint.combine(fingerprint.of_json([self.assets, self.atoms, self.dates, self.baseline]),
                                   fingerprint.of_array(self.X), fingerprint.of_array(self.rv_next),
                                   fingerprint.of_array(self.ret_next))


class WalkForwardSpec(BaseModel):
    """Every constant of a run. Its fingerprint is what a pre-registration freezes."""

    model_config = ConfigDict(frozen=True)

    first_eval: int = Field(104, ge=10)
    refit_every: int = Field(13, ge=1)
    network_refit_every: int = Field(26, ge=1)
    models: tuple[Model, ...] = ("B", "uncoupled", "field", "quantum", "composite")
    baseline_l2: float = 1.0
    network_l2: float = 1.0
    max_norm: float = 0.95
    quantum_bound: float = 1.0
    gamma: float = 0.4
    t: float = 1.0
    network_maxiter: int = 500
    quantum_maxiter: int = 200
    composite_min_weeks: int = 26
    min_train_labels: int = 30
    boot_block: int = 8
    boot_reps: int = 5000
    boot_seed: int = 20261128

    def network_spec(self, kind: Model) -> NetworkSpec:
        return NetworkSpec(kind=kind, l2=self.network_l2, max_norm=self.max_norm,  # type: ignore[arg-type]
                           quantum_bound=self.quantum_bound, gamma=self.gamma, t=self.t,
                           maxiter=self.quantum_maxiter if kind == "quantum" else self.network_maxiter)


# ------------------------------------------------------------------ building blocks
def ecdf_transform(train: FloatArray, values: FloatArray) -> FloatArray:
    """``x = 2 F(a) - 1``: F is the mid-rank CDF of the finite training values, clipped to [1/2N, 1-1/2N]. NaN stays NaN."""
    srt = np.sort(train[np.isfinite(train)])
    n = len(srt)
    out = np.full(values.shape, np.nan)
    ok = np.isfinite(values)
    if n == 0:
        return out
    less = np.searchsorted(srt, values[ok], side="left")
    leq = np.searchsorted(srt, values[ok], side="right")
    F = np.clip((less + 0.5 * (leq - less)) / n, 0.5 / n, 1 - 0.5 / n)
    out[ok] = 2 * F - 1
    return out


def _normalise(X_tr: FloatArray, X_ev: FloatArray) -> tuple[FloatArray, FloatArray]:
    a, b = np.empty_like(X_tr), np.empty_like(X_ev)
    for i in range(X_tr.shape[1]):
        for k in range(X_tr.shape[2]):
            a[:, i, k] = ecdf_transform(X_tr[:, i, k], X_tr[:, i, k])
            b[:, i, k] = ecdf_transform(X_tr[:, i, k], X_ev[:, i, k])
    return a, b


def _classes(y_tr: FloatArray, y_ev: FloatArray) -> tuple[IntArray, IntArray]:
    """Per-asset tercile cuts of the training labels. Unknown labels are -1."""
    c_tr = np.full(y_tr.shape, -1, dtype=np.int64)
    c_ev = np.full(y_ev.shape, -1, dtype=np.int64)
    for i in range(y_tr.shape[1]):
        ok = np.isfinite(y_tr[:, i])
        if ok.sum() < 3:
            continue
        cuts = np.quantile(y_tr[ok, i], [1 / 3, 2 / 3])
        c_tr[ok, i] = np.digitize(y_tr[ok, i], cuts)
        oke = np.isfinite(y_ev[:, i])
        c_ev[oke, i] = np.digitize(y_ev[oke, i], cuts)
    return c_tr, c_ev


def fit_softmax(X: FloatArray, c: IntArray, K: int, lam: float) -> tuple[FloatArray, FloatArray]:
    """Multinomial logistic regression, intercepts free, L2 ``lam`` on the weights."""
    d = X.shape[1]
    Y = np.eye(K)[c]

    def f(theta: FloatArray) -> tuple[float, FloatArray]:
        W, b = theta[:d * K].reshape(d, K), theta[d * K:]
        Z = X @ W + b
        Z = Z - Z.max(axis=1, keepdims=True)
        logP = Z - np.log(np.exp(Z).sum(axis=1, keepdims=True))
        G = np.exp(logP) - Y
        return (float(-np.sum(Y * logP) + 0.5 * lam * np.sum(W * W)),
                np.concatenate([(X.T @ G + lam * W).ravel(), G.sum(axis=0)]))

    res = optimize.minimize(f, np.zeros(d * K + K), jac=True, method="L-BFGS-B",
                            options={"maxiter": 2000, "gtol": 1e-9})
    th = np.asarray(res.x, dtype=np.float64)
    return th[:d * K].reshape(d, K), th[d * K:]


def predict_softmax(X: FloatArray, W: FloatArray, b: FloatArray) -> FloatArray:
    Z = X @ W + b
    Z = Z - Z.max(axis=1, keepdims=True)
    P = np.exp(Z)
    out: FloatArray = P / P.sum(axis=1, keepdims=True)
    return out


def _fit_asset(x_tr: FloatArray, c_tr: IntArray, x_ev: FloatArray, K: int, lam: float,
               min_labels: int) -> FloatArray:
    """One asset's logistic forecast; class frequencies when it has too little history or data."""
    ok = (c_tr >= 0) & np.all(np.isfinite(x_tr), axis=1)
    freq = np.bincount(c_tr[c_tr >= 0], minlength=K).astype(float) + 1.0
    fallback = np.tile(freq / freq.sum(), (len(x_ev), 1))
    if ok.sum() < min_labels:
        return fallback
    W, b = fit_softmax(x_tr[ok], c_tr[ok], K, lam)
    seen = np.all(np.isfinite(x_ev), axis=1)
    out = fallback.copy()
    if seen.any():
        out[seen] = predict_softmax(x_ev[seen], W, b)
    return out


def _fit_net(X_tr: FloatArray, C_tr: IntArray, spec: NetworkSpec, init: NetworkFit | None) -> NetworkFit:
    return fit_network(X_tr, C_tr, spec, init=init)


def design_effect(mean_corr: float, n_assets: int) -> float:
    """Variance inflation of a mean over ``n`` equally correlated assets: ``1 + (n - 1) rho``."""
    return 1.0 + (n_assets - 1) * mean_corr


def week_cluster_bootstrap(d: FloatArray, *, block: int, reps: int, seed: int,
                           level: float = 0.95) -> tuple[float, float, float]:
    """Mean of ``d`` over known asset-weeks, with a ``level`` interval from circular blocks of weeks."""
    if not 0.0 < level < 1.0:
        raise ValueError("level must lie in (0, 1)")
    S = np.nansum(d, axis=1)
    n = np.sum(np.isfinite(d), axis=1).astype(float)
    W = len(S)
    if n.sum() == 0:
        raise ValueError("nothing to score")
    rng = np.random.default_rng(seed)
    nb = W // block + 1
    means = np.empty(reps)
    for r in range(reps):
        ii = (rng.integers(0, W, nb)[:, None] + np.arange(block)).ravel()[:W] % W
        means[r] = S[ii].sum() / max(n[ii].sum(), 1.0)
    tail = 50.0 * (1.0 - level)
    lo, hi = np.percentile(means, [tail, 100.0 - tail])
    return float(S.sum() / n.sum()), float(lo), float(hi)


def _pick(P: FloatArray, C: IntArray) -> FloatArray:
    """Per-row log loss, NaN where the class is unknown."""
    picked = np.take_along_axis(P, np.where(C >= 0, C, 0)[..., None], axis=-1)[..., 0]
    out: FloatArray = np.where(C >= 0, -np.log(np.clip(picked, 1e-15, 1.0)), np.nan)
    return out


# ------------------------------------------------------------------ regime walk-forward
@dataclass
class PanelResult:
    spec: WalkForwardSpec
    eval_weeks: IntArray
    classes: IntArray                                   # (E, N), -1 unknown
    probs: dict[str, FloatArray]                        # model -> (E, N, 3)
    input_fingerprint: str
    spec_fingerprint: str
    network_fits: dict[str, list[NetworkFit]] = field(default_factory=dict)
    pool_weights: list[FloatArray] = field(default_factory=list)

    def logloss(self, model: str) -> FloatArray:
        return _pick(self.probs[model], self.classes)

    def verdict(self, model: str, vs: str, *, level: float = 0.95,
                assets: Sequence[int] | None = None) -> tuple[float, float, float, bool]:
        """Mean log-loss difference over ``assets`` (all by default), its week-clustered interval, and PASS (interval below 0)."""
        d = self.logloss(model) - self.logloss(vs)
        if assets is not None:
            d = d[:, list(assets)]
        mean, lo, hi = week_cluster_bootstrap(d, block=self.spec.boot_block, reps=self.spec.boot_reps,
                                              seed=self.spec.boot_seed, level=level)
        return mean, lo, hi, hi < 0


def walk_forward(panel: Panel, spec: WalkForwardSpec, *, max_workers: int = 1) -> PanelResult:
    W, N, _ = panel.X.shape
    nets = [m for m in spec.models if m in NETWORK_KINDS]
    if "quantum" in nets and N > 12:
        raise ValueError("the quantum arm simulates one qubit per asset: at most 12 assets")
    base_cols = [panel.col(b) for b in panel.baseline]
    pool_models = [m for m in spec.models if m != "composite"]
    probs: dict[str, list[FloatArray]] = {m: [] for m in spec.models}
    weeks: list[IntArray] = []
    classes: list[IntArray] = []
    fits: dict[str, list[NetworkFit]] = {m: [] for m in nets}
    pool_w: list[FloatArray] = []
    current: dict[str, NetworkFit] = {}
    # forkserver, not fork: forking a multi-threaded process (numpy's BLAS threads) can deadlock.
    executor: Executor | None = (ProcessPoolExecutor(max_workers, mp_context=multiprocessing.get_context("forkserver"))
                                 if max_workers > 1 else None)
    try:
        for b in range(spec.first_eval, W, spec.refit_every):
            ev = np.arange(b, min(b + spec.refit_every, W))
            c_tr, c_ev = _classes(panel.rv_next[:b], panel.rv_next[ev])
            x_tr, x_ev = _normalise(panel.X[:b], panel.X[ev])

            asset_args = [(x_tr[:, i][:, base_cols], c_tr[:, i], x_ev[:, i][:, base_cols], 3, spec.baseline_l2,
                           spec.min_train_labels) for i in range(N)]
            net_due = (b - spec.first_eval) % spec.network_refit_every == 0
            net_args = [(x_tr, c_tr, spec.network_spec(k), current.get(k)) for k in nets] if net_due else []
            if executor is None:
                asset_out = [_fit_asset(*a) for a in asset_args]
                net_out = [_fit_net(*a) for a in net_args]
            else:
                fa = [executor.submit(_fit_asset, *a) for a in asset_args]
                fn = [executor.submit(_fit_net, *a) for a in net_args]
                asset_out = [f.result() for f in fa]
                net_out = [f.result() for f in fn]
            for k, fit in zip(nets if net_due else [], net_out, strict=True):
                current[k] = fit
                fits[k].append(fit)

            block: dict[str, FloatArray] = {}
            if "B" in spec.models:
                block["B"] = np.stack(asset_out, axis=1)
            for k in nets:
                block[k] = predict_network(current[k], x_ev)
            if "composite" in spec.models:
                w = _pool_weights(spec, pool_models, probs, classes, weeks, panel, b)
                pool_w.append(w)
                flat = [block[m].reshape(-1, 3) for m in pool_models]
                block["composite"] = composite.log_pool(flat, w).reshape(len(ev), N, 3)
            for m in spec.models:
                probs[m].append(block[m])
            weeks.append(ev)
            classes.append(c_ev)
    finally:
        if executor is not None:
            executor.shutdown()
    return PanelResult(spec, np.concatenate(weeks), np.concatenate(classes),
                       {m: np.concatenate(v) for m, v in probs.items()}, panel.fingerprint(),
                       fingerprint.of_model(spec), fits, pool_w)


def _pool_weights(spec: WalkForwardSpec, pool_models: Sequence[str], probs: dict[str, list[FloatArray]],
                  classes: list[IntArray], weeks: list[IntArray], panel: Panel, b: int) -> FloatArray:
    """Weights from earlier out-of-sample forecasts whose labels matured by week ``b``. Until enough exist, all weight is on B."""
    one_hot = np.array([1.0 if m == "B" else 0.0 for m in pool_models])
    if not weeks or "B" not in pool_models:
        return one_hot if "B" in pool_models else np.full(len(pool_models), 1.0 / len(pool_models))
    past_weeks = np.concatenate(weeks)
    C = np.concatenate(classes)
    known = past_weeks < b                     # all earlier blocks; their labels have matured
    if known.sum() < spec.composite_min_weeks:
        return one_hot
    flat = [np.concatenate(probs[m])[known].reshape(-1, 3) for m in pool_models]
    return composite.fit_pool_weights(flat, C[known].reshape(-1))


# ------------------------------------------------------------------ uncertainty arms
@dataclass
class UncertaintyResult:
    spec: WalkForwardSpec
    eval_weeks: IntArray
    losses: dict[str, FloatArray]       # arm -> (E, N) log loss, NaN where unknown
    binding_share: float                # share of scored asset-weeks the complementarity constraint moved

    def verdict(self, arm: str, vs: str, *, level: float = 0.95,
                assets: Sequence[int] | None = None) -> tuple[float, float, float, bool]:
        d = self.losses[arm] - self.losses[vs]
        if assets is not None:
            d = d[:, list(assets)]
        mean, lo, hi = week_cluster_bootstrap(d, block=self.spec.boot_block, reps=self.spec.boot_reps,
                                              seed=self.spec.boot_seed, level=level)
        return mean, lo, hi, hi < 0


def _binary_ll(p: FloatArray, y: FloatArray) -> FloatArray:
    q = np.clip(p, 1e-15, 1 - 1e-15)
    out: FloatArray = np.where(np.isfinite(y), -(y * np.log(q) + (1 - y) * np.log(1 - q)), np.nan)
    return out


def walk_forward_uncertainty(panel: Panel, spec: WalkForwardSpec) -> UncertaintyResult:
    """Score the two direction/volatility relations against unconstrained marginals.

    - Labels: direction is ``up = next-week return > 0``. Volatility is ``vol-up = next-week realised
      vol above this week's GJR forecast``.
    - ``joint_*`` arms score the sum of the two marginal log losses, which is all a single qubit
      carries.
    """
    W, N, _ = panel.X.shape
    gi, si = panel.col("gjr_vol"), panel.col("spot_vol_corr")
    base_cols = [panel.col(b) for b in panel.baseline]
    up = np.where(np.isfinite(panel.ret_next), (panel.ret_next > 0).astype(float), np.nan)
    vu = np.where(np.isfinite(panel.rv_next), (panel.rv_next > panel.X[:, :, gi]).astype(float), np.nan)
    arms = ("dir_base", "dir_logit", "dir_cd", "vol_logit", "joint_free", "joint_complementarity", "joint_cd")
    out: dict[str, list[FloatArray]] = {a: [] for a in arms}
    weeks, binding, scored = [], 0, 0
    for b in range(spec.first_eval, W, spec.refit_every):
        ev = np.arange(b, min(b + spec.refit_every, W))
        x_tr, x_ev = _normalise(panel.X[:b], panel.X[ev])
        cols = {a: np.full((len(ev), N), np.nan) for a in arms}
        for i in range(N):
            ytr_u, ytr_v = up[:b, i], vu[:b, i]
            pu_logit = _fit_asset(x_tr[:, i][:, base_cols], np.where(np.isfinite(ytr_u), ytr_u, -1).astype(np.int64),
                                  x_ev[:, i][:, base_cols], 2, spec.baseline_l2, spec.min_train_labels)[:, 1]
            pv_logit = _fit_asset(x_tr[:, i][:, base_cols], np.where(np.isfinite(ytr_v), ytr_v, -1).astype(np.int64),
                                  x_ev[:, i][:, base_cols], 2, spec.baseline_l2, spec.min_train_labels)[:, 1]
            rate = float(np.nanmean(ytr_u)) if np.isfinite(ytr_u).any() else 0.5
            mu = float(np.nanmean(panel.ret_next[:b, i])) if np.isfinite(panel.ret_next[:b, i]).any() else 0.0
            sigma = panel.X[ev, i, gi] * math.sqrt(5 / 252)
            p_cd = np.where(np.isfinite(sigma) & (sigma > 0),
                            christoffersen_diebold_up(np.full(len(ev), mu), np.where(sigma > 0, sigma, 1.0)), rate)
            phi = np.array([phi_from_correlation(r) if np.isfinite(r) else math.pi / 2 for r in panel.X[ev, i, si]])
            pu_c, pv_c = constrain_marginals(pu_logit, pv_logit, phi)
            yu, yv = up[ev, i], vu[ev, i]
            moved = complementarity_form(2 * pu_logit - 1, 2 * pv_logit - 1, phi) > 1.0
            known = np.isfinite(yu) & np.isfinite(yv)
            binding += int(np.sum(moved & known))
            scored += int(np.sum(known))
            cols["dir_base"][:, i] = _binary_ll(np.full(len(ev), np.clip(rate, 0.01, 0.99)), yu)
            cols["dir_logit"][:, i] = _binary_ll(pu_logit, yu)
            cols["dir_cd"][:, i] = _binary_ll(p_cd, yu)
            cols["vol_logit"][:, i] = _binary_ll(pv_logit, yv)
            cols["joint_free"][:, i] = cols["dir_logit"][:, i] + cols["vol_logit"][:, i]
            cols["joint_complementarity"][:, i] = _binary_ll(pu_c, yu) + _binary_ll(pv_c, yv)
            cols["joint_cd"][:, i] = cols["dir_cd"][:, i] + cols["vol_logit"][:, i]
        for a in arms:
            out[a].append(cols[a])
        weeks.append(ev)
    return UncertaintyResult(spec, np.concatenate(weeks), {a: np.concatenate(v) for a, v in out.items()},
                             binding / max(scored, 1))
