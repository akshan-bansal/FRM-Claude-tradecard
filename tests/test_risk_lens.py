"""Risk lens: no price inputs, raw-p gate (no FDR), date-rotation null, embargoed split, fused backtest."""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pytest

from trading_live_claude.analysis.risk_lens import (
    Panel,
    Verdict,
    assert_no_price_inputs,
    backtest,
    build_panel,
    split,
    verdict,
)

D0 = date(2026, 1, 1)
SYMS = [f"S{i}" for i in range(6)]


def _store_and_labels(n_days: int = 60, signal: bool = True, seed: int = 1) -> tuple[dict, dict]:
    rng = np.random.default_rng(seed)
    store, labels = {}, {}
    for k in range(n_days):
        d = (D0 + timedelta(days=k)).isoformat()
        for s in SYMS:
            news = int(rng.poisson(3))
            noise = float(rng.normal())
            target = (0.4 * news + 0.3 * noise) if signal else float(rng.normal() + 2)
            store[(d, s)] = {"features": {"graph_events_24h": news}}
            labels[(d, s)] = {"target": max(target, 0.0), "dd_5d": max(target, 0.0) / 50}
    return store, labels


def test_price_and_volatility_inputs_are_refused() -> None:
    with pytest.raises(ValueError, match="not allowed"):
        assert_no_price_inputs({"bad": ("gkg_title_24h", "vol_20d")})
    with pytest.raises(ValueError, match="not allowed"):
        build_panel("x", {}, {}, features=("ret_5",))


def test_the_gate_is_the_raw_p_value_with_no_correction() -> None:
    assert Verdict("l", "r", 100, 0.2, 0.049).passed
    assert not Verdict("l", "r", 100, 0.2, 0.051).passed
    assert not Verdict("l", "r", 100, -0.2, 0.001).passed  # only a positive read can trim risk


def test_unknown_features_drop_the_row_rather_than_count_as_zero() -> None:
    store, labels = _store_and_labels(n_days=10)
    store[("2026-01-01", "S0")] = {"features": {"graph_events_24h": None}}
    p = build_panel("graph", store, labels, min_events=1)
    assert p is not None and len(p.y) == 10 * len(SYMS) - 1


def test_split_embargoes_one_horizon_between_train_and_holdout() -> None:
    store, labels = _store_and_labels(n_days=40)
    p = build_panel("graph", store, labels)
    assert p is not None
    tr, ho, hold_start = split(p)
    last_train = max(d for d, m in zip(p.dates, tr, strict=True) if m)
    assert (hold_start - last_train).days > 5 and not (tr & ho).any()


def test_a_real_signal_passes_and_noise_does_not() -> None:
    store, labels = _store_and_labels(signal=True)
    p = build_panel("graph", store, labels)
    assert p is not None
    v = verdict(p, "reliability", np.ones(len(p.y), dtype=bool), n_perm=99)
    assert v is not None and v.passed
    store, labels = _store_and_labels(signal=False, seed=3)
    q = build_panel("graph", store, labels)
    assert q is not None
    w = verdict(q, "reliability", np.ones(len(q.y), dtype=bool), n_perm=99)
    assert w is not None and not w.passed


def test_backtest_vetoes_the_riskiest_fifth_and_no_voters_means_no_trade() -> None:
    store, labels = _store_and_labels()
    p = build_panel("graph", store, labels)
    assert isinstance(p, Panel)
    _, ho, _ = split(p)
    assert backtest({"graph": p}, [], {"graph": ho}) is None
    bt = backtest({"graph": p}, [("graph", "reliability")], {"graph": ho})
    assert bt is not None and 0.15 <= bt.veto_share <= 0.3
    assert bt.dd_vetoed > bt.dd_kept  # the vetoed events were the riskier ones
