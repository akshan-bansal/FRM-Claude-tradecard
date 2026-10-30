"""Per-pair conviction bias for the Kraken crypto sleeve, from the correlation-aware allocator.

Moved here from ``scripts/paper_kraken.py`` (2026-10-30) so the dashboard can import it: a function in
``scripts/`` is not importable from another script, which is why the dashboard's allocator section
always read "Compute failed: No module named 'scripts'".
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

import pandas as pd

from trading_live_claude.data.kraken_ohlc import kraken_ohlc
from trading_live_claude.logging_setup import get_logger
from trading_live_claude.portfolio.allocator import PortfolioAllocator

log = get_logger(__name__)


def sleeve_conviction_bias(pairs: Mapping[str, Any], *,
                           fetch: Callable[..., pd.DataFrame] = kraken_ohlc) -> dict[str, float]:
    """Run the correlation-aware allocator over the sleeve and return per-symbol conviction bias.

    Bias = allocator_weight / equal_weight_baseline. A pair the allocator concentrates on gets
    bias > 1.0 (boost), a redundant pair in a correlated cluster gets < 1.0 (trim). Equal-weight
    is the neutral case at exactly 1.0.

    ``pairs`` maps the routed symbol to a sleeve entry with ``.pair`` (Kraken pair) and
    ``.screen_score``. Fetches shallow Kraken daily OHLC (~720 bars/pair); a correlation matrix needs
    refreshing weekly at most, so a start-of-session compute is fine. If a pair's fetch fails, its bias
    is 1.0 (neutral) rather than dropping it from the sleeve.
    """
    returns: dict[str, pd.Series] = {}
    scores: dict[str, float] = {}
    for routed, entry in pairs.items():
        try:
            df = fetch(entry.pair, interval=1440)
            returns[routed] = df.set_index("time")["close"].pct_change().dropna()
            scores[routed] = float(entry.screen_score)
        except Exception as e:
            log.warning("allocator.fetch_failed", symbol=routed, error=str(e), bias=1.0)
    if not returns:
        return {r: 1.0 for r in pairs}
    # Cap per-name at 0.30 so no single pair dominates; sleeve-level is one sleeve so it doesn't
    # matter. min_score=0 so every positive-scoring pair gets a slot.
    allocator = PortfolioAllocator(max_weight=0.30, max_sleeve_weight=1.0, min_score=0.0)
    result = allocator.allocate(returns, scores, regime_scalar=1.0)
    if not result.weights:
        return {r: 1.0 for r in pairs}
    equal_weight = 1.0 / len(returns)
    # A pair whose fetch failed has no weight because it was never scored, not because the allocator
    # trimmed it: it stays neutral. (Before 2026-10-30 it got 0.0, which zeroed its sizing.)
    return {r: result.weights.get(r, 0.0) / equal_weight if r in returns else 1.0 for r in pairs}
