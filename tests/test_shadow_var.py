"""Shadow VaR: forecasts a runner's book, records before the outcome, acts on nothing."""
from __future__ import annotations

import ast
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import trading_live_claude
from trading_live_claude.analysis import shadow_var as sv
from trading_live_claude.audit.ledger import LEDGER_EVENTS, Ledger

PKG = Path(trading_live_claude.__file__).resolve().parent
SCRIPT = PKG.parents[1] / "scripts" / "var_shadow.py"
FAST = dict(n_starts=2, max_iter=60)
TODAY = datetime.now(UTC).date()


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            out.append("." * node.level + (node.module or ""))
        elif isinstance(node, ast.Import):
            out.extend(a.name for a in node.names)
    return out


def _closes(days: int, end: date, seed: int = 0, start_price: float = 100.0) -> pd.Series:
    rng = np.random.default_rng(seed)
    index = [end - timedelta(days=days - 1 - i) for i in range(days)]
    return pd.Series(start_price * np.exp(np.cumsum(rng.normal(0, 0.01, days))), index=index)


def _write_cache(cache: Path, symbol: str, series: pd.Series, shard: str = "a") -> None:
    folder = cache / "numeraire_CAD"
    folder.mkdir(parents=True, exist_ok=True)
    times = pd.to_datetime(list(series.index)).tz_localize("UTC") + pd.Timedelta(hours=5)
    pd.DataFrame({"time": times, "close": series.to_numpy()}).to_parquet(
        folder / f"{symbol.replace('.', '_').replace('/', '_')}_1d_{shard}.parquet", index=False)


def _execute(state: Path, stream: str, symbol: str, side: str, quantity: float, when: date) -> None:
    Ledger(state / "ledger", stream=stream).append("EXECUTION_REPORTED", {
        "symbol": symbol, "side": side, "quantity": quantity, "price": 100.0, "currency": "CAD",
        "time": datetime.combine(when, datetime.min.time(), tzinfo=UTC).isoformat()})


@pytest.fixture
def desk(tmp_path: Path) -> tuple[Path, Path]:
    state, cache = tmp_path / "state", tmp_path / "cache"
    yesterday = TODAY - timedelta(days=1)
    _write_cache(cache, "QQQ", _closes(400, yesterday, seed=1))
    _write_cache(cache, "VDY.TO", _closes(400, yesterday, seed=2, start_price=40.0))
    _execute(state, "qt", "QQQ", "Buy", 10, yesterday - timedelta(days=30))
    _execute(state, "qt", "VDY.TO", "Buy", 50, yesterday - timedelta(days=20))
    return state, cache


# ---------------------------------------------------------------- acts on nothing


def test_the_event_is_registered() -> None:
    assert sv.EVENT in LEDGER_EVENTS


def test_the_module_and_script_import_nothing_from_the_order_path() -> None:
    for path in (Path(sv.__file__), SCRIPT):
        banned = [m for m in _imports(path) if any(f"{x}" in m for x in ("execution", "brokers", "monitor", "strategies"))]
        assert not banned, (path.name, banned)
    text = Path(sv.__file__).read_text(encoding="utf-8")
    for name in ("place_order", "submit(", "Router", "OrderIntent"):
        assert name not in text, name


def test_nothing_on_the_order_path_reads_the_shadow() -> None:
    for folder in ("execution", "monitor", "risk", "brokers", "strategies"):
        for path in (PKG / folder).rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            assert "shadow_var" not in text and sv.EVENT not in text, path


# ---------------------------------------------------------------- the book


def test_the_book_is_folded_point_in_time(tmp_path: Path) -> None:
    d = date(2026, 1, 10)
    _execute(tmp_path, "qt", "AAA", "Buy", 10, d)
    _execute(tmp_path, "qt", "AAA", "Sell", 4, d + timedelta(days=1))
    _execute(tmp_path, "qt", "BBB", "Buy", 5, d + timedelta(days=3))
    rows = Ledger(tmp_path / "ledger", stream="qt").rows()
    assert sv.book_from_ledger(rows, d)[0] == {"AAA": 10.0}
    assert sv.book_from_ledger(rows, d + timedelta(days=1))[0] == {"AAA": 6.0}
    assert sv.book_from_ledger(rows, d + timedelta(days=3))[0] == {"AAA": 6.0, "BBB": 5.0}


def test_cached_closes_prefer_the_numeraire_copy(tmp_path: Path) -> None:
    d = date(2026, 1, 10)
    _write_cache(tmp_path, "QQQ", pd.Series([500.0], index=[d]))
    pd.DataFrame({"time": [pd.Timestamp(d, tz="UTC")], "close": [360.0]}).to_parquet(tmp_path / "QQQ_1d_x.parquet")
    assert sv.cached_closes(tmp_path, "QQQ").iloc[0] == 500.0


# ---------------------------------------------------------------- the forecast


def test_a_forecast_uses_no_later_bar() -> None:
    end = date(2026, 6, 1)
    a, b = _closes(450, end, seed=3), _closes(450, end, seed=4)
    as_of = a.index[399]
    early = sv.forecast({"A": 5, "B": -3}, {"A": a[:400], "B": b[:400]}, as_of, **FAST)
    late = sv.forecast({"A": 5, "B": -3}, {"A": a, "B": b}, as_of, **FAST)
    assert early["methods"] == late["methods"] and early["methods"]


def test_the_gaussian_figure_is_the_closed_form() -> None:
    et = sv.load_methods()
    end = date(2026, 6, 1)
    closes = {"A": _closes(380, end, seed=5), "B": _closes(380, end, seed=6, start_price=50.0)}
    book = {"A": 4.0, "B": 10.0}
    got = sv.forecast(book, closes, end, **FAST)
    frame = pd.DataFrame(closes)
    R = np.log(frame).diff().dropna().to_numpy()
    Z, sigma = et.devolatilize(R, lam=sv.LAM, warm=sv.WARM)
    sigma_next = np.sqrt(sv.LAM * sigma[-1] ** 2 + (1 - sv.LAM) * R[-1] ** 2)
    a = np.array([4.0, 10.0]) * frame.iloc[-1].to_numpy() * sigma_next
    window = min(sv.MAX_WINDOW, len(R) - sv.WARM)
    expected = -np.sqrt(a @ np.cov(Z[-window:], rowvar=False) @ a) * et.cornish_fisher(sv.ALPHA)
    assert got["methods"]["gauss_sample"] == pytest.approx(expected, rel=1e-10)
    assert got["window"] == window


def test_short_history_records_why_and_no_figures() -> None:
    end = date(2026, 6, 1)
    got = sv.forecast({"A": 1.0}, {"A": _closes(200, end)}, end, **FAST)
    assert got["methods"] == {} and "need" in got["reason"]


def test_the_gate_reference_needs_what_the_gate_needs() -> None:
    end = date(2026, 6, 1)
    short = sv.forecast({"A": 1.0}, {"A": _closes(400, end)}, end, **FAST)
    long = sv.forecast({"A": 1.0}, {"A": _closes(620, end)}, end, **FAST)
    assert short["gate_per_name_sum"] is None
    assert long["gate_per_name_sum"] > 0


def test_books_on_several_clocks_are_flagged() -> None:
    end = date(2026, 6, 1)
    one = sv.forecast({"QQQ": 1.0, "VDY.TO": 1.0}, {"QQQ": _closes(330, end, 1), "VDY.TO": _closes(330, end, 2)}, end, **FAST)
    two = sv.forecast({"QQQ": 1.0, "ETH/USD": 1.0}, {"QQQ": _closes(330, end, 1), "ETH/USD": _closes(330, end, 2)}, end, **FAST)
    assert one["same_date_across_clocks"] is False
    assert two["same_date_across_clocks"] is True


# ---------------------------------------------------------------- record and score


def test_record_writes_one_row_a_day_on_a_valid_chain(desk: tuple[Path, Path], tmp_path: Path) -> None:
    state, cache = desk
    out = tmp_path / "shadow"
    first = sv.record("qt", state_dir=state, cache_dir=cache, ledger_dir=out, **FAST)
    assert first is not None and first["as_of"] == (TODAY - timedelta(days=1)).isoformat()
    assert first["methods"] and first["reason"] is None and first["written"] is True
    assert sv.record("qt", state_dir=state, cache_dir=cache, ledger_dir=out, **FAST) is None
    assert len(sv.recorded(out)) == 1
    assert Ledger(out, stream=sv.STREAM).verify()[0] is True
    assert not (state / "ledger" / f"{TODAY:%Y-%m-%d}.{sv.STREAM}.jsonl").exists()


def test_a_day_without_a_forecast_is_reported_not_recorded(desk: tuple[Path, Path], tmp_path: Path) -> None:
    state, cache = desk
    _execute(state, "qt", "NEW", "Buy", 1, TODAY - timedelta(days=1))
    _write_cache(cache, "NEW", _closes(100, TODAY - timedelta(days=1)))
    got = sv.record("qt", state_dir=state, cache_dir=cache, ledger_dir=tmp_path / "s", **FAST)
    assert got["written"] is False and "need" in got["reason"]
    assert sv.recorded(tmp_path / "s") == []


def test_a_bar_still_forming_is_never_used(desk: tuple[Path, Path], tmp_path: Path) -> None:
    state, cache = desk
    _write_cache(cache, "QQQ", pd.Series([999.0], index=[TODAY]), shard="today")
    _write_cache(cache, "VDY.TO", pd.Series([99.0], index=[TODAY]), shard="today")
    got = sv.record("qt", state_dir=state, cache_dir=cache, ledger_dir=tmp_path / "s", **FAST)
    assert got["as_of"] == (TODAY - timedelta(days=1)).isoformat()


def test_a_name_sold_since_still_counts_at_as_of(desk: tuple[Path, Path], tmp_path: Path) -> None:
    state, cache = desk
    _execute(state, "qt", "VDY.TO", "Sell", 50, TODAY)
    got = sv.record("qt", state_dir=state, cache_dir=cache, ledger_dir=tmp_path / "s", **FAST)
    assert got["book"] == {"QQQ": 10.0, "VDY.TO": 50.0} and got["methods"]


def test_score_counts_a_row_only_if_written_before_its_outcome(desk: tuple[Path, Path], tmp_path: Path) -> None:
    state, cache = desk
    out = tmp_path / "shadow"
    row = sv.record("qt", state_dir=state, cache_dir=cache, ledger_dir=out, **FAST)
    as_of = date.fromisoformat(row["as_of"])
    q, v = sv.cached_closes(cache, "QQQ")[as_of], sv.cached_closes(cache, "VDY.TO")[as_of]

    _write_cache(cache, "QQQ", pd.Series([q * 0.9], index=[TODAY]), shard="t")
    _write_cache(cache, "VDY.TO", pd.Series([v * 1.0], index=[TODAY]), shard="t")
    same_day = sv.score(out, cache)
    assert not same_day.forward.any()                 # outcome dated the day it was written: not a forecast
    assert same_day.loss.iloc[0] == pytest.approx(-10 * (q * 0.9 - q))

    for s in ("QQQ", "VDY.TO"):
        (cache / "numeraire_CAD" / f"{s.replace('.', '_')}_1d_t.parquet").unlink()
    _write_cache(cache, "QQQ", pd.Series([q * 0.9], index=[TODAY + timedelta(days=1)]), shard="t")
    _write_cache(cache, "VDY.TO", pd.Series([v], index=[TODAY + timedelta(days=1)]), shard="t")
    ahead = sv.score(out, cache)
    assert ahead.forward.all()
    assert ahead.set_index("method").breach["gauss_sample"] == bool(
        ahead.loss.iloc[0] > row["methods"]["gauss_sample"])
    assert set(sv.summary(ahead).method) == set(row["methods"])
