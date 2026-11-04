"""The rolling cube reproduces the engine's matrices slice by slice, never sees ahead, and leaves the engine alone."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest

from trading_live_claude.risk.spectral_var import ENGINE_DIR, engine_version, load_engine

be = load_engine()
sys.path.insert(0, str(ENGINE_DIR))
_spec = importlib.util.spec_from_file_location("bsm_tensor", Path(ENGINE_DIR) / "bsm_tensor.py")
bt = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
sys.modules["bsm_tensor"] = bt
_spec.loader.exec_module(bt)  # type: ignore[union-attr]

R = np.random.default_rng(7).standard_normal((120, 5)) * 0.01
NAMES = list("ABCDE")


def test_each_slice_is_what_numpy_gives_for_that_window() -> None:
    cube = bt.build_cube(R, NAMES, window=60, step=7)
    assert cube.corr.shape == (len(cube.ends), 5, 5) and cube.eigenvalues.shape == (len(cube.ends), 5)
    for k, end in enumerate(cube.ends):
        frame = R[end - 60:end]
        assert np.allclose(cube.cov[k], np.cov(frame.T))
        assert np.allclose(cube.corr[k], np.corrcoef(frame.T))
        assert np.allclose(cube.eigenvalues[k], np.linalg.eigvalsh(np.corrcoef(frame.T)))


def test_filters_match_the_engine() -> None:
    cube = bt.build_cube(R, NAMES, window=60, step=30)
    k = 1
    assert np.allclose(cube.mp_filtered()[k], be.mp_filter(cube.corr[k], 60)[0])
    assert np.allclose(cube.spectral(0.75)[k], be.spectral_correlation(cube.corr[k], 0.75, 0.05))
    assert np.all((cube.top_share() > 0) & (cube.top_share() <= 1))


def test_a_slice_cannot_see_its_own_row_or_later() -> None:
    changed = R.copy()
    changed[80:] *= 50
    a, b = bt.build_cube(R, NAMES, window=60), bt.build_cube(changed, NAMES, window=60)
    assert np.array_equal(a.at(80), b.at(80))
    assert not np.allclose(a.at(81), b.at(81))
    with pytest.raises(KeyError):
        a.at(59)


@pytest.mark.parametrize("bad", ["nan", "flat", "short", "names"])
def test_bad_input_is_refused(bad: str) -> None:
    data, names = R.copy(), NAMES
    if bad == "nan":
        data[3, 1] = np.nan
    if bad == "flat":
        data[:, 2] = 0.0
    if bad == "short":
        data = data[:10]
    if bad == "names":
        names = NAMES[:2]
    with pytest.raises(ValueError):
        bt.build_cube(data, names, window=60)


def test_the_gate_still_hashes_only_the_engine_file() -> None:
    assert engine_version() == __import__("hashlib").sha256((Path(ENGINE_DIR) / "bsm_event.py").read_bytes()).hexdigest()[:16]
