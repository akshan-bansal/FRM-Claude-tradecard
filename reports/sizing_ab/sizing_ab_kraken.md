# Phase 3 sizing A/B: kraken

3701 walk-forward trades (3701 sized, 0 with no volatility estimate), 3650 with a read (1806 with edge <= 0), 45 bps a side, 2014-10-15 to 2026-11-06.

| Metric | A: volatility | B: forward read |
|---|---|---|
| sortino_over_dd | 0.49 | 0.71 |
| sortino | 0.46 | 0.63 |
| total_return | +8.4% | +250.0% |
| cagr | +0.7% | +11.3% |
| max_drawdown | -93.7% | -87.7% |
| time_in_market | 99% | 98% |
| mean_gross | 0.80 | 0.73 |
| turnover_per_year | 39.9x | 36.5x |
| cost_drag_per_year | 17.95% | 16.44% |
| budget_bound_share | 74% | 58% |
| sortino_over_dd, earlier half | 0.93 | 1.38 |
| sortino_over_dd, later half | 0.08 | 0.15 |

Month-block bootstrap: p = 0.098, median gain in sortino_over_dd +0.23.

| Pass check | Result |
|---|---|
| whole period | yes |
| earlier half | yes |
| later half | yes |
| bootstrap p < 0.05 | NO |
| gross exposure <= 1.1x A | yes |
| turnover <= 1.1x A | yes |

**Phase 3: NOT PASSED.** One variant run.
