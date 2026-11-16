---
description: List current IB and Kraken positions for the primary accounts.
allowed-tools: Bash
---

Run `uv run trading positions --venue ib`, then `uv run trading positions --venue kraken`.
If no positions: say so in one line.
Otherwise: render the table and call out any position whose open PnL is < -5%.
