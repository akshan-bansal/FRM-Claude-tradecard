from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Sandbox state/log/cache dirs per test so journals don't leak between tests."""
    for var, sub in (("STATE_DIR", "state"), ("LOG_DIR", "logs"), ("DATA_CACHE_DIR", "data/cache")):
        path = tmp_path / sub
        path.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv(var, str(path))
    # Defang accidental live mode in test envs.
    monkeypatch.setenv("EXECUTION_MODE", "paper")
    monkeypatch.setenv("QUESTRADE_ENV", "practice")
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", "test-key-do-not-use-in-production")
    monkeypatch.setenv("QUESTRADE_REFRESH_TOKEN", "test-refresh-token")
    # Redirect trading.yaml to a sandbox path so tests don't pick up the real repo's yaml.
    monkeypatch.setenv("TRADING_YAML_PATH", str(tmp_path / "trading.yaml"))
    # Refresh the cached Settings singleton.
    from trading_live_claude.config.settings import get_settings as gs

    gs.cache_clear()


class LogRecorder:
    """Stands in for a module's ``log``. structlog caches a logger once used, so ``capture_logs`` depends on run order."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self.rows: list[dict] = []

    def __getattr__(self, _level: str):
        def record(event: str, **kw: object) -> None:
            self.events.append(event)
            self.rows.append({"event": event, **kw})
        return record


@pytest.fixture
def record_log(monkeypatch: pytest.MonkeyPatch):
    """``record_log(module)`` swaps that module's ``log`` for a recorder and returns it."""
    def attach(module: object) -> LogRecorder:
        rec = LogRecorder()
        monkeypatch.setattr(module, "log", rec)
        return rec
    return attach


@pytest.fixture(autouse=True)
def _no_fx_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """A session prices in the home currency. No test reaches the Bank of Canada, and 1:1 keeps prices as written."""
    from types import SimpleNamespace

    from trading_live_claude.monitor import session
    monkeypatch.setattr(session, "_home_rates", lambda *_a, **_k: SimpleNamespace(rate=lambda _c: 1.0))


@pytest.fixture()
def random_walk_df() -> pd.DataFrame:
    rng = np.random.default_rng(seed=42)
    n = 500
    rets = rng.normal(loc=0.0005, scale=0.015, size=n)
    close = 100.0 * np.exp(np.cumsum(rets))
    high = close * (1 + np.abs(rng.normal(0, 0.005, n)))
    low = close * (1 - np.abs(rng.normal(0, 0.005, n)))
    open_ = close * (1 + rng.normal(0, 0.002, n))
    vol = rng.integers(100_000, 1_000_000, n)
    times = pd.date_range("2022-01-01", periods=n, freq="B", tz="UTC")
    return pd.DataFrame(
        {"time": times, "open": open_, "high": high, "low": low, "close": close, "volume": vol}
    )


@pytest.fixture()
def trending_df() -> pd.DataFrame:
    """V-shape series: declines first, then rallies. Forces an EMA crossover near the trough."""
    n = 400
    half = n // 2
    down = np.linspace(200, 100, half)
    up = np.linspace(100, 200, n - half)
    base = np.concatenate([down, up])
    noise = np.random.default_rng(0).normal(0, 0.3, n)
    close = base + noise
    high = close + 0.5
    low = close - 0.5
    open_ = close - 0.1
    vol = np.full(n, 500_000)
    times = pd.date_range("2022-01-01", periods=n, freq="B", tz="UTC")
    return pd.DataFrame(
        {"time": times, "open": open_, "high": high, "low": low, "close": close, "volume": vol}
    )
