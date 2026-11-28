"""Panel walk-forward, scoring, composite pooling, fingerprints and BSM atoms. All offline.

Written before the implementation. Lookahead, serial-vs-concurrent identity, power, null and
bootstrap coverage are all pinned. BSM atoms are tested on BSM_Spectral's own synthetic market,
so no market data is needed.
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest

from trading_live_claude.quantum import composite, fingerprint
from trading_live_claude.quantum.panel import (
    Panel,
    WalkForwardSpec,
    design_effect,
    ecdf_transform,
    walk_forward,
    walk_forward_uncertainty,
    week_cluster_bootstrap,
)

ATOMS = ("gjr_vol", "vol_premium", "spot_vol_corr")


def synthetic_panel(seed: int, coupling: float, W: int = 260, N: int = 4) -> Panel:
    """Next-week vol of each asset = own evidence (atom 0) + coupling-propagated stress of asset 0."""
    r = np.random.default_rng(seed)
    X = np.empty((W, N, 3))
    X[:, :, 0] = np.exp(r.normal(-1.8, 0.4, (W, N)))          # gjr_vol, annualised
    X[:, :, 1] = r.normal(0.1, 0.2, (W, N))                    # vol_premium
    X[:, :, 2] = r.uniform(-0.9, -0.5, (W, N))                 # spot-vol correlation
    z = (np.log(X[:, :, 0]) + 1.8) / 0.4
    shared = np.zeros((W, N))
    shared[:, 1:] = coupling * z[:, [0]]                       # asset 0's stress spills to the rest
    rv = np.exp(-1.8 + 0.4 * (0.7 * z + shared + 0.5 * r.normal(size=(W, N))))
    ret = r.normal(0.001, rv / math.sqrt(252 / 5))
    rv[-1], ret[-1] = np.nan, np.nan
    return Panel(assets=tuple(f"A{i}" for i in range(N)), atoms=ATOMS,
                 dates=tuple(str(np.datetime64("2020-01-03") + 7 * w) for w in range(W)),
                 X=X, rv_next=rv, ret_next=ret)


FAST = WalkForwardSpec(first_eval=104, refit_every=26, network_refit_every=52,
                       models=("B", "uncoupled", "field", "composite"), boot_reps=500)


# ------------------------------------------------------------------ building blocks
def test_ecdf_transform_is_bounded_and_ignores_missing() -> None:
    tr = np.array([0.0, 1, 2, 3, np.nan, 5, 6, 7, 8, 9])
    x = ecdf_transform(tr, np.array([-5.0, 4.5, 100.0, np.nan]))
    assert np.all(np.abs(x[:3]) < 1) and np.isnan(x[3])
    assert x[0] == ecdf_transform(tr, np.array([0.0]))[0]


def test_panel_validates_shapes_and_truncates_without_lookahead() -> None:
    p = synthetic_panel(0, 0.0, W=50)
    t = p.truncate(30)
    assert t.X.shape[0] == 31 and np.all(np.isnan(t.rv_next[30])) and np.all(np.isnan(t.ret_next[30]))
    assert np.array_equal(t.rv_next[:30], p.rv_next[:30])
    with pytest.raises(ValueError):
        Panel(assets=("a",), atoms=ATOMS, dates=("d",), X=np.zeros((1, 2, 3)), rv_next=np.zeros((1, 2)),
              ret_next=np.zeros((1, 2)))


def test_design_effect() -> None:
    assert design_effect(0.0, 8) == 1.0
    assert design_effect(1.0, 8) == 8.0
    assert design_effect(0.5, 3) == pytest.approx(2.0)


def test_week_cluster_bootstrap_covers_zero_and_widens_with_correlated_assets() -> None:
    r = np.random.default_rng(1)
    covered, widths_corr, widths_ind = 0, [], []
    for _ in range(60):
        common = r.normal(size=(200, 1))
        d = 0.9 * common + 0.44 * r.normal(size=(200, 6))      # strongly correlated across assets
        _, lo, hi = week_cluster_bootstrap(d, block=4, reps=400, seed=int(r.integers(1 << 30)))
        covered += lo <= 0 <= hi
        widths_corr.append(hi - lo)
        _, lo2, hi2 = week_cluster_bootstrap(r.normal(size=(200, 6)), block=4, reps=400, seed=1)
        widths_ind.append(hi2 - lo2)
    assert 0.85 <= covered / 60 <= 1.0
    assert np.mean(widths_corr) > 1.8 * np.mean(widths_ind)


def test_composite_pool_weights_are_a_simplex_and_do_no_harm() -> None:
    r = np.random.default_rng(2)
    n = 3000
    truth = r.dirichlet([2, 2, 2], size=n)
    c = np.array([r.choice(3, p=p) for p in truth])
    good = 0.8 * truth + 0.2 / 3
    noise = r.dirichlet([1, 1, 1], size=n)
    w = composite.fit_pool_weights([good[:2000], noise[:2000]], c[:2000])
    assert w.shape == (2,) and np.all(w >= 0) and w.sum() == pytest.approx(1.0)
    assert w[0] > 0.8
    pooled = composite.log_pool([good[2000:], noise[2000:]], w)
    ll = lambda P: -np.mean(np.log(P[np.arange(len(P)), c[2000:]]))  # noqa: E731
    assert ll(pooled) <= ll(good[2000:]) + 0.01
    assert np.allclose(composite.log_pool([good, noise], np.array([1.0, 0.0])), good)


def test_fingerprints_are_deterministic_and_content_sensitive() -> None:
    a = np.arange(6.0).reshape(2, 3)
    assert fingerprint.of_array(a) == fingerprint.of_array(a.copy())
    assert fingerprint.of_array(a) != fingerprint.of_array(a.T.copy())
    assert fingerprint.of_array(a) != fingerprint.of_array(a.astype(np.float32))
    assert fingerprint.of_model(FAST) != fingerprint.of_model(FAST.model_copy(update={"boot_seed": 1}))
    p = synthetic_panel(0, 0.0, W=20)
    assert p.fingerprint() == synthetic_panel(0, 0.0, W=20).fingerprint() != synthetic_panel(1, 0.0, W=20).fingerprint()


# ------------------------------------------------------------------ the walk-forward
def test_walk_forward_has_no_lookahead() -> None:
    p = synthetic_panel(3, 0.5)
    full = walk_forward(p, FAST)
    part = walk_forward(p.truncate(200), FAST)
    k = len(part.eval_weeks)
    assert np.array_equal(part.eval_weeks, full.eval_weeks[:k])
    for m in FAST.models:
        assert np.max(np.abs(part.probs[m] - full.probs[m][:k])) < 1e-9, m


def test_concurrent_run_equals_serial_run() -> None:
    p = synthetic_panel(4, 0.5)
    a = walk_forward(p, FAST, max_workers=1)
    b = walk_forward(p, FAST, max_workers=3)
    for m in FAST.models:
        assert np.array_equal(a.probs[m], b.probs[m]), m
    assert a.input_fingerprint == b.input_fingerprint


def test_power_planted_spillover_is_detected() -> None:
    res = walk_forward(synthetic_panel(5, 1.2), FAST)
    mean, lo, hi, ok = res.verdict("field", "uncoupled")
    assert ok and hi < 0, (mean, lo, hi)


def test_null_no_spillover_is_not_detected() -> None:
    passes = [walk_forward(synthetic_panel(10 + s, 0.0), FAST).verdict("field", "uncoupled")[3] for s in range(5)]
    assert sum(passes) <= 1


def test_quantum_arm_runs_on_a_small_panel() -> None:
    spec = FAST.model_copy(update={"models": ("B", "uncoupled", "quantum"), "quantum_maxiter": 25})
    res = walk_forward(synthetic_panel(6, 0.8, N=3), spec)
    assert np.all(np.isfinite(res.probs["quantum"]))


def test_uncertainty_arms_are_scored() -> None:
    res = walk_forward_uncertainty(synthetic_panel(7, 0.0), FAST)
    for arm in ("dir_base", "dir_logit", "dir_cd", "vol_logit", "joint_free", "joint_complementarity", "joint_cd"):
        assert np.isfinite(np.nanmean(res.losses[arm])), arm
    assert 0.0 <= res.binding_share <= 1.0


# ------------------------------------------------------------------ BSM_Spectral atoms
@pytest.fixture(scope="module")
def synth_csv_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    from trading_live_claude.risk.spectral_var import load_engine

    be = load_engine()
    d = tmp_path_factory.mktemp("fred")
    n = 1500
    for (pid, vid), seed in ((("SP500", "VIXCLS"), 7), (("NASDAQ100", "VXNCLS"), 8)):
        dates, S, V = be.synthetic_market(n, seed)
        (d / f"{pid}.csv").write_text("observation_date,value\n" + "".join(f"{a},{b}\n" for a, b in zip(dates, S, strict=True)))
        (d / f"{vid}.csv").write_text("observation_date,value\n" + "".join(f"{a},{b}\n" for a, b in zip(dates, V, strict=True)))
    r = np.random.default_rng(0)
    for mid, start, step in (("DGS2", 2.0, 0.03), ("DGS10", 3.0, 0.03), ("DEXUSEU", 1.1, 0.004), ("DEXJPUS", 110.0, 0.4)):
        lv = start + np.cumsum(r.normal(0, step, n + 1))
        (d / f"{mid}.csv").write_text("observation_date,value\n" + "".join(f"{a},{b}\n" for a, b in zip(dates, lv, strict=True)))
    return d


def test_bsm_atoms_are_point_in_time(synth_csv_dir: Path) -> None:
    from trading_live_claude.quantum.atoms import ATOM_NAMES, asset_atoms, load_aligned

    al = load_aligned(synth_csv_dir, "SPX")
    full = asset_atoms(al)
    cut = asset_atoms(al.truncate(1200))
    for name in ATOM_NAMES:
        a, b = full[name][:1200], cut[name]
        assert np.array_equal(np.isfinite(a), np.isfinite(b)), name
        ok = np.isfinite(a)
        assert np.array_equal(a[ok], b[ok]), name


def test_load_panel_builds_a_weekly_panel(synth_csv_dir: Path) -> None:
    from trading_live_claude.quantum.atoms import ATOM_NAMES, load_panel

    p = load_panel(synth_csv_dir, ("SPX", "NDX"))
    assert p.assets == ("SPX", "NDX") and p.atoms == ATOM_NAMES
    assert np.all(np.isfinite(p.X)) and np.all(np.isfinite(p.rv_next[:-1])) and np.all(np.isnan(p.rv_next[-1]))
    assert len(p.dates) == p.X.shape[0] >= 100


def test_verdict_takes_a_level_and_an_asset_subset() -> None:
    res = walk_forward(synthetic_panel(8, 0.8), FAST)
    m95, lo95, hi95, _ = res.verdict("field", "uncoupled")
    m975, lo975, hi975, _ = res.verdict("field", "uncoupled", level=0.975)
    assert m95 == m975 and lo975 <= lo95 and hi975 >= hi95
    sub = res.verdict("field", "uncoupled", assets=[1, 2, 3])
    d = res.logloss("field")[:, 1:] - res.logloss("uncoupled")[:, 1:]
    assert sub[0] == pytest.approx(float(np.nanmean(d)))
    with pytest.raises(ValueError):
        week_cluster_bootstrap(d, block=8, reps=10, seed=0, level=1.2)
