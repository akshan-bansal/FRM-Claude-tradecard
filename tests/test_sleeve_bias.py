"""Kraken sleeve conviction bias: importable from the package, neutral on a failed fetch. No network."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from trading_live_claude.portfolio.sleeve_bias import sleeve_conviction_bias


@dataclass(frozen=True)
class Entry:
    pair: str
    screen_score: float


def _closes(seed: int, base: np.ndarray | None = None) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rets = (base if base is not None else 0) + rng.normal(0, 0.01, 300)
    return pd.DataFrame({"time": pd.date_range("2025-01-01", periods=300, freq="D", tz="UTC"),
                         "close": 100 * np.exp(np.cumsum(rets))})


def test_correlated_pairs_are_trimmed_against_an_independent_one() -> None:
    # Five pairs, so equal weight (20%) sits under the 30% per-name cap and correlation can show.
    common = np.random.default_rng(0).normal(0, 0.02, 300)
    frames = {"AAA": _closes(1, common), "BBB": _closes(2, common), "CCC": _closes(3, common),
              "DDD": _closes(4), "EEE": _closes(5)}
    pairs = {f"{k}/USD": Entry(k, 1.0) for k in frames}
    bias = sleeve_conviction_bias(pairs, fetch=lambda pair, interval: frames[pair])
    assert set(bias) == set(pairs)
    assert min(bias["DDD/USD"], bias["EEE/USD"]) > 1.0 > max(bias[f"{k}/USD"] for k in ("AAA", "BBB", "CCC"))


def test_a_failed_fetch_leaves_that_pair_neutral_not_zeroed() -> None:
    frames = {"AAA": _closes(1), "BBB": _closes(2)}

    def fetch(pair: str, interval: int) -> pd.DataFrame:
        if pair == "DOWN":
            raise ConnectionError("kraken unreachable")
        return frames[pair]

    pairs = {"AAA/USD": Entry("AAA", 1.0), "BBB/USD": Entry("BBB", 1.0), "DOWN/USD": Entry("DOWN", 1.0)}
    bias = sleeve_conviction_bias(pairs, fetch=fetch)
    assert bias["DOWN/USD"] == 1.0
    assert bias["AAA/USD"] > 0 and bias["BBB/USD"] > 0


def test_every_fetch_failing_means_every_pair_neutral() -> None:
    def fetch(pair: str, interval: int) -> pd.DataFrame:
        raise ConnectionError("down")

    pairs = {"AAA/USD": Entry("AAA", 1.0), "BBB/USD": Entry("BBB", 1.0)}
    assert sleeve_conviction_bias(pairs, fetch=fetch) == {"AAA/USD": 1.0, "BBB/USD": 1.0}
