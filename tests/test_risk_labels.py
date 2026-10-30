"""Risk labels: drawdown arithmetic, vol window alignment, maturity, no label without full history."""
from __future__ import annotations

import ast
import math
from datetime import date, timedelta
from pathlib import Path

from trading_live_claude.analysis import risk_labels
from trading_live_claude.analysis.risk_labels import Bar, labels

D0 = date(2026, 1, 1)


def _bars(closes: list[float], lows: list[float] | None = None) -> list[Bar]:
    lows = lows or closes
    return [Bar(D0 + timedelta(days=i), lo, c) for i, (lo, c) in enumerate(zip(lows, closes, strict=True))]


def _zigzag() -> list[float]:
    return [100.0 * (1.01 if i % 2 else 0.99) for i in range(21)]


def test_no_label_without_a_full_vol_window_and_horizon() -> None:
    assert list(labels(_bars([100.0] * 25))) == []  # 20 bars of vol + anchor + 5 ahead = 26 needed
    assert len(list(labels(_bars([100.0] * 26)))) == 1


def test_drawdown_is_the_worst_low_in_the_next_five_bars() -> None:
    closes = _zigzag() + [100.0] * 5
    lows = [*closes[:21], 99.0, 95.0, 98.0, 99.0, 97.0]
    (lab,) = labels(_bars(closes, lows))
    assert lab["date"] == (D0 + timedelta(days=20)).isoformat()
    assert math.isclose(lab["dd_5d"], 1 - 95.0 / closes[20], rel_tol=1e-5)
    assert lab["matured_date"] == (D0 + timedelta(days=25)).isoformat()


def test_a_rally_has_zero_drawdown_not_a_negative_one() -> None:
    (lab,) = labels(_bars(_zigzag() + [120.0] * 5))
    assert lab["dd_5d"] == 0.0 and lab["target"] == 0.0


def test_vol_uses_only_returns_up_to_the_anchor_bar() -> None:
    """Changing a forward close must not change vol_20d: that would leak the outcome into the scale."""
    a = next(labels(_bars(_zigzag() + [100.0] * 5)))
    b = next(labels(_bars(_zigzag() + [50.0] * 5)))
    assert a["vol_20d"] == b["vol_20d"] and a["dd_5d"] != b["dd_5d"]


def test_target_scales_drawdown_by_the_expected_five_day_move() -> None:
    (lab,) = labels(_bars(_zigzag() + [90.0] * 5))
    assert math.isclose(lab["target"], lab["dd_5d"] / (lab["vol_20d"] * math.sqrt(5)), rel_tol=1e-4)


def test_flat_prices_give_no_target_rather_than_infinity() -> None:
    (lab,) = labels(_bars([100.0] * 26))
    assert lab["vol_20d"] == 0.0 and lab["target"] is None


def test_labels_stay_outside_the_trading_path() -> None:
    tree = ast.parse(Path(risk_labels.__file__).read_text(encoding="utf-8"))
    mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert not {m for m in mods for f in ("execution", "risk.", "brokers", "strategies", "monitor") if f in m}
