---
description: Pre-flight checklist for live trading. DOES NOT run live trading itself.
allowed-tools: Bash, Read
argument-hint: <strategy> <symbol[,symbol,...]>
---

You are about to help the user go live. Your job is ONLY to run the pre-flight checklist; you will not invoke `trading live`.

Checklist (in order, halt on first failure):

1. Confirm `state/HALTED` does NOT exist. If it does, refuse to proceed and tell the user to investigate `state/HALTED` first.
2. Run `uv run trading status --venue <ib|kraken>` (IB for equities and futures, Kraken for crypto) and report account number, type, equity. Refuse if no accounts. On IB, a DU account is IB's paper login, not real money.
3. Run `uv run trading risk-report --venue <ib|kraken>`. Confirm kill-switch is clear and heat is <= 5%.
4. Read `.env` indirectly: run `uv run python -c "from trading_live_claude.config import get_settings;s=get_settings();print(s.execution_mode, s.risk_pct_per_trade, s.max_open_positions)"` and verify:
   - `execution_mode == "live"`
   - `risk_pct_per_trade <= 0.02`
   - `max_open_positions <= 10`
5. Run `uv run pytest -q tests/` and confirm tests pass.
6. Verify the strategy `$ARGUMENTS` was paper-traded on the same venue for >= 5 sessions by reading `state/paper_fills.jsonl` history. If not, refuse.

If all 6 pass, print the EXACT command the user should run themselves:

```
EXECUTION_MODE=live uv run trading live \
  --venue <ib|kraken> \
  --strategy <STRATEGY> \
  --symbols <SYMBOLS> \
  --interval 60 \
  --confirm "I UNDERSTAND THE RISK"
```

Do NOT run that command yourself.
