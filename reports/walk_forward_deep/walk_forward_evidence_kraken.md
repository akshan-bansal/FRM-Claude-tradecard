# Walk-forward validation: kraken

3701 out-of-sample long trades at 45 bps a side on 12 symbols (AAVE/USD, ADA/USD, BTC/USD, ETH/USD, LINK/USD, POL/USD, SOL/USD, UNI/USD, XLM/USD, XMR/USD, XRP/USD, ZEC/USD); 0 short trades not simulated; 3650 with a read. Strategies are those with a parameter grid: bollinger, rsi_meanrevert, ema_crossover, macd, momentum_breakout, atr_channel, ts_momentum, dual_ma, high_52w_breakout.

## Pairing claim

| Test | Reads | Rank IC | p |
|---|---|---|---|
| Pooled | 3650 | +0.197 | 0.000 |
| **Within symbol** (the claim) | 3650 | +0.173 | 0.001 |
| Within symbol, earlier half | 1825 | +0.085 | 0.004 |
| Within symbol, later half | 1825 | +0.274 | 0.001 |
| Within quarter | 3632 | +0.193 | 0.001 |
| Within one strategy on one symbol | 3565 | -0.211 | 1.000 |

**Pairing claim: VALIDATED** (needs p < 0.025, positive overall and in both halves).
