"""Risk-lens audit and backtest: do news / overlay / graph features predict vol-normalised 5-day drawdown?

Revisions 2-5 and 8 of the lens redefinition (NEXT_SESSION.md, 2026-10-12), with the user's rules:

* The fusion layer takes NO price or volatility inputs. Lens features come only from the feature
  store's ``gkg_*`` / ``live_*`` / ``ov_*`` / ``graph_*`` families. Volatility appears only inside the
  target (``risk_labels``), as the yardstick, never as a predictor.
* NO multiple-testing (FDR) correction (user, 2026-10-30). A verdict passes on its own raw p-value. With
  several (lens, rule) pairs tested at once, roughly ``ALPHA`` of the null ones will pass by chance; the
  report states how many verdicts were tested so that is visible.

Method:

* Read-out: the attention read from ``lens_audit`` (similarity-weighted neighbours), with both
  neighbour rules: reliability (other symbols, outside one label window) and future (own matured past).
* Null: date ROTATION. The outcome panel is shifted along the date axis by a random offset of at least one
  label horizon, wrapping round, for every symbol together. Serial and cross-sectional structure survive
  intact; only the alignment between features and outcomes is broken.
* Frozen parameters: ``tau`` and ``recency`` are the lens_audit defaults, fixed before any of this data was
  seen; the veto quantile is fixed here. Nothing is tuned on the holdout.
* Fusion (gated average): only verdicts that pass on the TRAIN dates vote; their predictions are
  rank-normalised and averaged, then judged once on the HOLDOUT dates.
* Backtest: on the holdout, veto the top ``1 - VETO_QUANTILE`` of fused scores and trim the rest linearly,
  and compare realised drawdown of what was vetoed against what was kept.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date

import numpy as np
from numpy.typing import NDArray

from trading_live_claude.analysis.lens_audit import (
    DEFAULT_RECENCY,
    DEFAULT_TAU,
    _attention,
    _future_mask,
    _rank_gap,
    _reliability_mask,
    _spearman,
)

FloatArray = NDArray[np.float64]

HORIZON_DAYS = 5
VETO_QUANTILE = 0.8  # frozen: veto the riskiest fifth
ALPHA = 0.05
TRAIN_SHARE = 0.6

# Lens = a feature view. Counts are log1p'd; a feature that is unknown (null) on a row drops the row from
# that lens rather than being imputed as zero. No price or volatility feature may appear here.
LENSES: dict[str, tuple[str, ...]] = {
    "news_gkg": ("gkg_title_24h", "gkg_tagged_24h", "gkg_sources_24h"),
    "news_gkg_tone": ("gkg_title_24h", "gkg_tone_24h", "gkg_novelty_24h"),
    "news_live": ("live_title_24h", "live_tagged_24h", "live_sources_24h"),
    "overlay": ("ov_scalar", "ov_strategic_risk", "ov_accel_max", "ov_global_alerts"),
    "graph": ("graph_events_24h",),
}
_COUNTS = {"gkg_title_24h", "gkg_tagged_24h", "gkg_sources_24h", "live_title_24h", "live_tagged_24h",
           "live_sources_24h", "graph_events_24h", "ov_global_alerts"}
_FORBIDDEN = ("vol", "ret_", "price", "close", "dd_")


@dataclass(frozen=True)
class Panel:
    """Aligned events for one lens: features known at the cutoff, outcome measured after it."""

    lens: str
    symbols: NDArray[np.str_]
    day: FloatArray  # day number
    dates: list[date]
    x: FloatArray
    y: FloatArray  # vol-normalised drawdown target
    dd: FloatArray  # raw drawdown, for the backtest's economics


@dataclass(frozen=True)
class Verdict:
    lens: str
    rule: str
    n: int
    rho: float
    p: float

    @property
    def passed(self) -> bool:
        """Raw p-value gate: no multiple-testing correction, by the user's decision."""
        return bool(np.isfinite(self.rho) and self.rho > 0 and self.p < ALPHA)


def assert_no_price_inputs(lenses: Mapping[str, Sequence[str]]) -> None:
    """The user's rule (2026-10-12): the fusion layer reads no price or volatility input."""
    bad = [f for feats in lenses.values() for f in feats if any(tok in f for tok in _FORBIDDEN)]
    if bad:
        raise ValueError(f"price/volatility inputs are not allowed in the fusion layer: {bad}")


def build_panel(lens: str, store: Mapping[tuple[str, str], Mapping], labels: Mapping[tuple[str, str], Mapping],
                features: Sequence[str] | None = None, *, min_events: int = 30) -> Panel | None:
    feats = tuple(features or LENSES[lens])
    assert_no_price_inputs({lens: feats})
    syms, days, dts, xs, ys, dds = [], [], [], [], [], []
    for (d, sym), row in sorted(store.items()):
        lab = labels.get((d, sym))
        if lab is None or lab.get("target") is None:
            continue
        vals = [row["features"].get(f) for f in feats]
        if any(v is None or isinstance(v, (dict, bool)) for v in vals):
            continue
        dd = date.fromisoformat(d)
        syms.append(sym)
        days.append(float(dd.toordinal()))
        dts.append(dd)
        xs.append([float(np.log1p(v)) if f in _COUNTS else float(v) for f, v in zip(feats, vals, strict=True)])
        ys.append(float(lab["target"]))
        dds.append(float(lab["dd_5d"]))
    if len(ys) < min_events:
        return None
    return Panel(lens, np.array(syms), np.array(days), dts, np.array(xs), np.array(ys), np.array(dds))


def _predict(p: Panel, rule: str, sel: NDArray[np.bool_], y: FloatArray | None = None) -> tuple[FloatArray, NDArray[np.bool_]]:
    """Attention read for the events in ``sel`` from neighbours in ``sel``; returns (pred, has_neighbour)."""
    x, g_raw, t = p.x[sel], p.symbols[sel], p.day[sel]
    yy = (p.y if y is None else y)[sel]
    _, g = np.unique(g_raw, return_inverse=True)
    sd = x.std(axis=0)
    z = (x - x.mean(axis=0)) / np.where(sd < 1e-12, 1.0, sd)
    if rule == "reliability":
        allowed = _reliability_mask(g, t, HORIZON_DAYS)
        bias = None
    else:
        allowed = _future_mask(g, t, HORIZON_DAYS)
        bias = -DEFAULT_RECENCY * np.where(allowed, _rank_gap(g, t), 0.0)
    a = _attention(z, allowed, DEFAULT_TAU, bias)
    return a @ yy, allowed.any(axis=1)


def _rotate(p: Panel, sel: NDArray[np.bool_], k: int) -> FloatArray:
    """Outcomes shifted k dates along the panel's own date axis, every symbol together; NaN where the
    shifted (date, symbol) pair has no outcome."""
    uniq = sorted(set(p.day[sel]))
    pos = {d: i for i, d in enumerate(uniq)}
    look = {(p.day[i], p.symbols[i]): p.y[i] for i in np.flatnonzero(sel)}
    out = np.full(int(sel.sum()), np.nan)
    for j, i in enumerate(np.flatnonzero(sel)):
        src = uniq[(pos[p.day[i]] + k) % len(uniq)]
        out[j] = look.get((src, p.symbols[i]), np.nan)
    return out


def verdict(p: Panel, rule: str, sel: NDArray[np.bool_], *, n_perm: int = 500, seed: int = 7) -> Verdict | None:
    """One-sided Spearman between the read and the outcome, against the date-rotation null."""
    pred, ok = _predict(p, rule, sel)
    if ok.sum() < 20:
        return None
    rho = _spearman(pred[ok], p.y[sel][ok])
    n_dates = len(set(p.day[sel]))
    if n_dates <= 2 * HORIZON_DAYS:
        return None
    rng = np.random.default_rng(seed)
    exceed = 0
    y_full = p.y.copy()
    for _ in range(n_perm):
        k = int(rng.integers(HORIZON_DAYS, n_dates - HORIZON_DAYS + 1))
        yr = _rotate(p, sel, k)
        keep = ok & np.isfinite(yr)
        y_full[sel] = np.where(np.isfinite(yr), yr, 0.0)
        pp, _ = _predict(p, rule, sel, y_full)  # neighbours' outcomes rotated too
        stat = _spearman(pp[keep], yr[keep])
        if np.isfinite(rho) and np.isfinite(stat) and stat >= rho:
            exceed += 1
    pval = 1.0 if not np.isfinite(rho) else (1 + exceed) / (n_perm + 1)
    return Verdict(p.lens, rule, int(ok.sum()), float(rho), float(pval))


def split(p: Panel, share: float = TRAIN_SHARE) -> tuple[NDArray[np.bool_], NDArray[np.bool_], date]:
    """Train = earliest ``share`` of dates; holdout = the rest, starting one label horizon later (embargo)."""
    uniq = sorted(set(p.dates))
    cut = uniq[int(len(uniq) * share)]
    hold_start = uniq[min(len(uniq) - 1, int(len(uniq) * share) + HORIZON_DAYS)]
    d = np.array([x.toordinal() for x in p.dates])
    return d < cut.toordinal(), d >= hold_start.toordinal(), hold_start


def _rank01(a: FloatArray) -> FloatArray:
    r = np.argsort(np.argsort(a))
    return r / max(1, len(a) - 1)


@dataclass(frozen=True)
class Backtest:
    voters: list[str]
    n: int
    rho: float
    dd_vetoed: float
    dd_kept: float
    dd_all: float
    dd_trimmed_exposure: float  # exposure-weighted drawdown under the linear trim
    veto_share: float


def backtest(panels: Mapping[str, Panel], voters: list[tuple[str, str]],
             hold: Mapping[str, NDArray[np.bool_]]) -> Backtest | None:
    """Gated average on the holdout: rank-normalised predictions of the voting (lens, rule) pairs, averaged
    over events every voter can read. Veto the top quintile; trim the rest by (1 - score)."""
    if not voters:
        return None
    reads: list[dict[tuple[float, str], float]] = []
    dd: dict[tuple[float, str], float] = {}
    for lens, rule in voters:
        p = panels[lens]
        pred, ok = _predict(p, rule, hold[lens])
        idx = np.flatnonzero(hold[lens])[ok]
        r = _rank01(pred[ok])
        reads.append({(p.day[i], p.symbols[i]): float(v) for i, v in zip(idx, r, strict=True)})
        dd.update({(p.day[i], p.symbols[i]): float(p.dd[i]) for i in idx})
    common = set.intersection(*(set(r) for r in reads))
    if len(common) < 20:
        return None
    keys = sorted(common)
    score = np.array([np.mean([r[k] for r in reads]) for k in keys])
    realized = np.array([dd[k] for k in keys])
    veto = score >= np.quantile(score, VETO_QUANTILE)
    exposure = np.where(veto, 0.0, 1.0 - score)
    return Backtest(
        voters=[f"{a}/{b}" for a, b in voters], n=len(keys), rho=_spearman(score, realized),
        dd_vetoed=float(realized[veto].mean()), dd_kept=float(realized[~veto].mean()),
        dd_all=float(realized.mean()),
        dd_trimmed_exposure=float((realized * exposure).sum() / max(exposure.sum(), 1e-12)),
        veto_share=float(veto.mean()),
    )
