# Engine wiring: paper and money, layer by layer

A paper run is only worth practising if it rehearses the session that will actually trade. Every runner now
goes through one builder, `monitor/session.py`, and differs from its counterpart only where it has to.

| | Paper | Money |
|---|---|---|
| Questrade | `trading paper`, `trading signal --paper` | `trading live` |
| Kraken | `trading paper --venue kraken`, `scripts/paper_kraken.py` | `trading live --venue kraken` |
| IB | `trading paper --venue ib`, `scripts/paper_ib.py` | `trading live --venue ib` |
| Daemon | `AUTONOMOUS_ACCOUNT=paper` | `AUTONOMOUS_ACCOUNT=live` |

## How to check it

```bash
trading doctor
```

`trading doctor` shows what each layer has and lacks, reporting a secret as set or missing, never its value.

```bash
trading audit query session-plans
```

`session-plans` lists each session's settings fingerprint. A money session should match the paper sessions
behind it. The fingerprint leaves out only what has to differ.

```bash
trading audit query fill-quality
```

`fill-quality` measures what fills cost per stream, in basis points against the price the gate decided on. Set
`paper_costs` in `config/trading.yaml` to what it measures.

`tests/test_session.py` compares paper and money plans for every venue and moves every knob, so a setting that
reaches one and not the other fails there.

## Layer by layer

| Layer | Same in both | What differs, and why |
|---|---|---|
| Settings | Every knob in `config/trading.yaml`, resolved into one `SessionPlan`. A flag overrides one for a run, with the same flags on `trading paper` and `trading live`. | Nothing. |
| Data | `MarketData` over the venue's own adapter, same cache, same pre-flight check. | Nothing. |
| Strategy | Same book: `--strategy-map` or `strategy_map`, and Kraken's sleeve with its parameters. | Nothing. |
| Sizing | `PositionSizer`, `risk_pct_per_trade`, `risk_model`, `heat_aggregation`. | Nothing in the code. Results depend on account size, so set `paper_starting_equity` to what you plan to trade. |
| Intel | Overlay on whenever the key is set, refreshed off the poll path. Same thesis trims, allocator per venue and persistence gate. | Nothing. |
| Risk gates | `Router.limits_from(settings)`, kill-switch, reducing-order exemption, heat, size caps, basket, spectral VaR, card. | Daily budget belongs to the daemon, and its rehearsal runs it too. Card cannot wrap the daemon: no human is in that loop. |
| Execution | One Router, market orders, the in-flight guard. | Necessary: a real broker, or `PaperBroker` simulating fills against the same quotes. See the known gaps below. |
| Equity and halts | Same journal columns, same `KillSwitch.evaluate`, a manual stop keeps its reason, a human clear restarts the measure. | Necessary: a paper account starts fresh each session, a real one reads its peak back from `state/live_equity.csv`. |
| Audit | Same ledger events, `SESSION_PLAN`, recon, and every factor behind an order's size. | Necessary: money sessions have their own stream, and a failed ledger write stops the order. Paper's only logs it. |
| Alerts | One emitter, labelled with the session, so a phone alert shows whether real money moved. | Nothing. |
| Before real money | | Necessary: `EXECUTION_MODE=live`, the typed phrase, a token whose environment matches, venue keys, and on Kraken `--allow-screened` for the paper-only sleeve. |

## Paper costs

Paper fills now cross the spread: a buy pays the ask and a sell the bid, plus slippage, plus the venue's fees. The
defaults in `DEFAULT_PAPER_COSTS` are assumptions until `fill-quality` has real fills to compare with:

| Venue | Commission | Slippage |
|---|---|---|
| Questrade | $4.95 per order | 5 bps |
| Kraken | 40 bps | 5 bps |
| IB | $1.00 per order | 5 bps |

## Known gaps a rehearsal does not cover

- **Order types.** Everything is a market order, in both modes. Scoped in `NEXT_SESSION.md` section 14.
- **Fill mechanics.** Paper fills instantly and in full. Real orders can part-fill, lag, or be refused for buying
  power, or on Kraken for minimum order size.
- **Currency.** `PaperBroker` is single-currency. Questrade equity is in the account's currency while a US name is
  priced in USD, so the gates compare unlike amounts. A session warns when its symbols do this. Needs a decision.
- **Whole units.** Orders are whole shares in both modes, so on a smaller account a high-priced crypto pair sizes
  to zero. It behaves the same in both, but the size of the account decides what can trade.
- **The kill-switch is one file.** A drawdown in a paper session halts a money session sharing `state/`. Needs a
  decision.
- **Questrade practice.** `practicelogin.questrade.com/oauth2/token` returned 404 on 2026-11-01, so a practice login
  cannot be routed to. The token decides the account, and a money session refuses to start on a mismatch.
- **`trading live`'s last lines.** The rule against setting a live execution mode in tests means no test enters the
  command itself. It is now a thin call into the code paper runs through, but do a supervised first run.

## Environment keys

| Key | Layer | Needed for |
|---|---|---|
| `QUESTRADE_REFRESH_TOKEN`, `TOKEN_ENCRYPTION_KEY` | Data | Questrade, paper and money |
| `KRAKEN_API_KEY`, `KRAKEN_API_SECRET` | Execution | Kraken money. Paper needs none. |
| IB | Data | No key: log in to the gateway in a browser |
| `WORLDMONITOR_API_KEY` | Intel | Overlay and thesis trims, every runner |
| `ANTHROPIC_API_KEY` | Intel | Held agent layer only, not trading |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Alerts | Phone alerts |
| `SMTP_HOST`, `SMTP_USER`, `SMTP_PASS`, `ALERT_EMAIL_TO` | Alerts | Email alerts, optional |
