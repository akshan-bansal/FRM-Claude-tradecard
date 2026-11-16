# Project: trading-live-claude

Claude Code project memory. Loaded automatically when working in this repo.

## What this repo is

Algorithmic trading framework. Strategies → signals → risk gates → orders on two brokers: **Interactive Brokers** for
equities (every listing) and derivatives, **Kraken** for crypto. Questrade was retired on 2026-11-16 and its code removed.
Paper-mode default. Live mode behind multi-step confirmation.

## Config layout

This repo splits configuration so you can manage it freely without ever touching secrets:

- **`.env`** — secrets only. Kraken API keys, the token encryption key, optional SMTP/Telegram creds. Gitignored. **Reading `.env` is denied** in `.claude/settings.json`. You cannot see it.
- **`config/trading.yaml`** — every non-secret knob (strategy, symbols, risk caps, autonomous config). Gitignored (per-machine). **You can read and write this freely.**
- **`config/trading.example.yaml`** — committed template; what `trading.yaml` should look like.

Precedence: `trading.yaml` > environment variables > `.env` > dataclass defaults.

Use `uv run trading tune` (or the `/tune` command) to backtest a strategy x symbol grid and atomically rewrite `trading.yaml` with the winning config.

## Non-negotiables

- **Never bypass the risk gate.** All order intents go through `execution.router.Router`. Even tests must use the paper router, never a live IB or Kraken broker.
- **Never set `EXECUTION_MODE=live` in code, tests, or example snippets.** Live mode is a runtime decision by the human only.
- **Indicators are shifted before comparison.** Adding a strategy? Use `signals.generator.no_lookahead(df)` and write a regression test that fails on lookahead.
- **`state/` files are ground truth.** Don't compute equity, positions, or peak-from-scratch — read from journal.
- **Stored tokens have one writer.** The IB connector's `TokenStore` rewrites its encrypted file under `state/` atomically. Don't add a second code path that reads/writes it.

## Default style

- Python 3.13, `uv` for env, `ruff` for lint, `mypy --strict` for typing
- `httpx` + `hishel` for HTTP (NOT `requests` — global rule)
- `pydantic` v2 models at boundaries (broker responses, config)
- `structlog` for logging, no `print` outside CLI surface
- `pytest` with `respx` for HTTP mocking, no live calls in CI

## When the user asks for a new strategy

1. Subclass `strategies.base.Strategy`
2. Implement `generate_signals(self, df: pd.DataFrame) -> pd.DataFrame` returning columns `entry`, `exit`, `size_hint`
3. Add a backtest run command in the README example block
4. Write at least one test: a synthetic-data round-trip plus a lookahead-bias guard
5. Register in `strategies/__init__.py:STRATEGIES`

## When the user asks to backtest

Default flow: `uv run trading backtest --strategy NAME --symbol TICKER --years N`. Output is a markdown summary + equity-curve PNG written to `reports/`. Always flag overfitting risk if `years < 2` (per article skill #1).

## When the user asks to "go live"

Three checks before you run anything:
1. Confirm at least one full week of paper trading on the same venue first (IB paper login or a simulated book), and
   that `trading audit query session-plans` shows the money session's fingerprint matching those paper sessions
2. Confirm `state/HALTED` does not exist
3. Confirm the user has typed the live-mode confirmation phrase

If any check fails, stop and report — do not edit the .env to make it pass.

## Useful entry points

- CLI: `src/trading_live_claude/cli.py`
- Router (the bottleneck): `src/trading_live_claude/execution/router.py`
- Risk gates: `src/trading_live_claude/risk/`
- IB access (one broker over TWS, Client Portal and the connector, by region): `src/trading_live_claude/brokers/ib_access.py`
- Kraken client: `src/trading_live_claude/brokers/kraken.py`
- Which broker trades what: `src/trading_live_claude/desk_policy.py`

## Two trading modes — pick the right one

This repo supports **two distinct ways to trade** that share the same Router and risk gates:

### A. LLM-driven trader (you are the trader)

- Triggered by `/trade-check` or `/trade-now`.
- The `claude-trader` skill describes your decision framework. Read it before deciding.
- You gather state via `trading status / positions / risk-report / signal`, weigh the algorithm's signal against macro/cost/fit, then call `trading place-order` (which still routes through Router).
- Use `/loop 10m /trade-check` to put yourself on a polling schedule. Stops when the user closes Claude.
- Default mode for orders is `--mode auto` which honors `execution_mode` in `trading.yaml`. Refuse `--mode live` unless the user has explicitly typed "live" in this session.

### B. Deterministic autonomous daemon (Python decides)

## Autonomous mode

This repo supports a fully-autonomous Claude-driven trading loop. **Read these rules before touching anything related to it.**

- The daemon process is owned by the user's system, not by Claude. Claude can start/stop/inspect it, but the loop runs in `trading_live_claude.cli:autonomous_run` as a detached process with its own pid.
- A daemon spawned in this repo can place real orders on `autonomous_venue` (`ib` or `kraken`, one broker per daemon) if `AUTONOMOUS_ACCOUNT=live`. Default and reset value is `practice`, which must be an IB paper (DU) login and refuses a live one; Kraken has no practice account. `AUTONOMOUS_ACCOUNT=paper` rehearses the daemon: its own gates and budget on a simulated broker.
- Autonomous router mode requires the runtime env var `AUTONOMOUS_ENABLED=true` AND `autonomous_enabled=true` in `.env`. The CLI sets the env var when calling `autonomous start`; the daemon then re-checks on every Router construction.
- The daemon respects ALL the standard gates (kill-switch, heat, per-trade risk, max positions, min ticket) PLUS a daily trade-count and daily notional cap.

### What Claude is allowed to do
- Run `/autonomous status`, `/autonomous tail`, `/autonomous start`, `/autonomous stop`.
- Use the `autonomous-monitor` agent to audit health.
- Recommend a stop if it sees a pattern of rejections, broker errors, or unexpected fills.

### What Claude is NOT allowed to do
- Flip `AUTONOMOUS_ENABLED` in `.env`. The user does that themselves.
- Flip `AUTONOMOUS_ACCOUNT` from `practice` to `live` in `.env`. The user does that themselves.
- Clear the kill-switch, or the paper halt, under any circumstance. Promote a paper halt to money.
- Edit the Router's gate list, the DailyBudget caps, or the strategy code while the daemon is running.

### Failure modes to watch for
1. Token expiry mid-session → broker raises `TokenExpired`. Daemon continues looping; next iteration re-refreshes. If repeated, stop daemon.
2. Symbol not found → broker raises `BrokerError`. Daemon logs and skips that symbol; healthy.
3. Daily-budget exhausted → router rejects with reason "daily trade cap reached" / "daily notional cap". Healthy; daemon should idle until midnight UTC.
4. Pidfile present but process dead → `status` reports `stale`. `start` clears stale pidfile and proceeds.

## TradeCard — physical Ed25519 approval card (opt-in)

An optional third gate sits between the Router's risk checks and broker dispatch: a
physical approval card the user taps ACCEPT / DECLINE on. It's disabled by default and
opt-in per-run via `--require-card` on any runner but the daemon, or `require_card` in `trading.yaml`.

- **Never weakens existing gates.** `ApprovalRouter` runs `Router._gate` FIRST, and only
  publishes a prompt to the card if the router would have accepted the intent anyway.
  A card ACCEPT is a *human* gate, on top of the automated ones, not instead of them.
- **Autonomous mode cannot be wrapped.** `AUTONOMOUS_ENABLED=true` + card approval are
  mutually exclusive; the wrapper raises at construction. Pick one loop.
- **WYSIWYS canonical bytes.** The card signs a `|`-joined string that includes the
  broker venue (`ib` / `ib_web` / `kraken` / `paper` / `global`; `questrade` is still read on old prompts), action, symbol, shares, entry, notional,
  account, intent id, and nonce. A MITM cannot silently swap the broker or resize the
  intent — the signature won't verify.
- **Thesis from the VS engine.** `intel/vs_engine.py` composes a `<=140` char thesis
  (overlay risk clauses have priority over user-supplied notes during truncation) and
  persists a full writeup to `state/intel_writeups/{intel_ref}.json`. Framing is
  research not advice — clauses describe observed signal state, never issue directives.
- **URL versioning.** Every non-public route lives under `/v1/…` (SPEC_VERSION 1.0.0).
  `/healthz` and `/openapi.json` stay unversioned so a fresh client can bootstrap.
  Legacy unversioned paths still work during the deprecation window; the shim logs a
  warning on every hit.

Layout:

- `src/trading_live_claude/execution/approval.py` — router wrapper, store, registry,
  `wire_card_approval()` helper.
- `src/trading_live_claude/execution/approval_server.py` — stdlib HTTP surface (kept out
  of `execution.approval` so the module dependency arrow points inward).
- `scripts/approval_shim.py` — thin CLI wrapper for running the shim as a standalone
  process rather than a daemon thread beside the paper loop.
- `scripts/approval_card_sim.py` — laptop-side fake card that speaks the wire protocol
  end-to-end (register / poll / sign / respond). Real firmware in `firmware/tradecard/`
  is a separate ESP-IDF project; the Python wheel does not ship it.

To exercise without silicon (verified end to end 2026-09-18, Kraken is 24/7):
```
# 1. The paper script boots the shim itself on --card-shim-port and prints the card's bearer token.
python scripts/paper_kraken.py --interval 20 --iterations 2 --require-card --card-shim-port 8787 --card-ttl 120
# 2. In a second shell, pair the simulated card with that token.
python scripts/approval_card_sim.py --shim http://127.0.0.1:8787 --auto accept --poll 1 --auth-token <token>
```
`--require-card` exists on `scripts/paper_kraken.py`, `scripts/paper_ib.py` and `cli signal --paper`
(any venue, with `--card-shim-host/--card-shim-port/--card-ttl`).
`scripts/approval_shim.py` runs the shim standalone, for a loop that doesn't boot its own.
Note: with the card on, the flatten-on-exit sells also need an ACCEPT.

**The desk panel rides on the same shim.** Whatever boots the shim also serves the built panel:

- `GET /desk` — public unlock shell, carries no journal data and never the token.
- `GET /v1/desk/page` — the built page, behind the same auth as every other `/v1` route.

Serving it there is what makes it live: the page's fetches are same-origin, so no CORS hole is
opened and no token travels in a URL (the viewer pastes it into the shell once, per tab). The panel
polls `/v1/intents/pending` and `/v1/stats` and is **read-only by construction** — it displays the
prompt and its fingerprint for comparison against the card's screen; the ACCEPT that decides it is a
signature made on the device. Without a link it shows no queue at all rather than a specimen one.
Rebuild the page with `python scripts/build_desk_dash.py` (writes `pwa/desk.html` and the copy in
`OS-InvestmentIntelligenceDashboard/`).

Gap-closure objectives live in `NEXT_SESSION.md` section 11.

## Audit trail (docs/compliance/AUDIT_SCOPE.md)

- Every runner writes the hash-chained ledger under `state/ledger/<date>.<stream>.jsonl`, one stream
  per process (`kraken`, `kraken-live`, `ib`, `ib-live`, `autonomous`; `qt`, `qt-paper`, `qt-live` are Questrade's history,
  read and never written; `manual`, `control`, `recon`
  and `thesis` are shared and locked). Live and autonomous are strict: a failed ledger write stops the order.
- Never edit or delete a ledger row. A correction is a new `CORRECTION` row. Never "repair" a chain.
- `OrderIntent.intent_id` is the one identity from signal to execution; the approval stores reuse it.
- New order-path behaviour gets a ledger event, added to `LedgerEvent` in `audit/ledger.py` first.
- Gate inputs are recorded so `trading audit replay` can re-run them: a new gate must put whatever it
  reads from outside the intent into `GateDecision.observed`.
- Semantic (CRA) facts are derived, not stored: `audit/semantics.py` over `EXECUTION_REPORTED`, with
  `config/audit_vocabulary.yaml`. Never redefine a vocabulary term; add one and bump `vocab`.
- The spectral VaR gate (`risk/spectral_var.py`, over `BSM_Spectral/`) is opt-in, entries only, and may
  only trim or reject.

## Routing and monitor policy

- One `Router` can hold several brokers: `intent.broker` names the route, empty takes the default, and an
  unknown route is rejected, never sent to the default. Autonomous mode stays on one broker. Gates read
  the figures the caller passes, so a caller trading several routes must pass whole-book figures.
- `LiveMonitor` does not span brokers yet: its equity and positions come from one broker. Summing them
  across brokers needs the numeraire decision (NEXT_SESSION.md, exchange hopping level 3).
- **Money runners mirror paper.** Every runner goes through `start_session` in `monitor/session.py`, so paper
  and money are one build. A new knob goes in `config/trading.yaml` and `SessionPlan`. A paper/money difference
  must be in `NECESSARY_DIFFERENCES` with a reason; `tests/test_session.py` fails otherwise. See
  `docs/ENGINE_WIRING.md`. `trading doctor` shows what each layer has, without showing a secret.
- **No removals without the user's express permission** (symbols, strategies, gates, flags, data). Move or
  extend instead; if a test and a table disagree, ask rather than shrink the table.
- `tests/test_runbook.py` pins the desk's actual commands. `docs/PORT_FRM_CLAUDE.md` records what the merge with
  `FRM-Claude` decided, and what was kept beside rather than merged.
- **A paper drawdown stops paper.** A simulated book trips `state/HALTED_PAPER`; money reads only `state/HALTED`. Stopping money for
  it is the human's express call: `trading kill --from-paper --confirm "HALT MONEY"`. Claude neither promotes a halt nor clears either file.
- **The gates read the home currency.** Every runner prices quotes, positions and equity in `account_currency` (CAD) at the Bank of
  Canada's daily rate (`brokers/fx.py`). A fill keeps its own currency; the audit layer converts it by trade date.
- **Two brokers, split by class** (`desk_policy.py`): Kraken carries crypto only, IB carries every equity listing and
  derivatives, and each refuses the other's. `ib_futures_only` narrows an IB book to futures. `ib_access` in trading.yaml
  picks the IB transport per region; reads fall back across transports, orders never do, and the connector never orders.
- `AUTONOMOUS_ACCOUNT=practice` routes nothing: the login decides the account, and a money session refuses to start
  when it does not match (`check_account_environment`: IB paper logins are DU accounts). Practise on paper.
- `supports_level_trigger` means a strategy emits `entry_level`; `level_trigger_default` means the monitor
  acts on it unasked. Backtests read `entry` alone, so emitting the column must never change `entry`.
- Gates that stop new risk must not trap the way out. An order that only reduces a position the routed
  broker holds (`reduces` in `Router._gate`, read from the broker and recorded) passes the kill-switch, the
  minimum ticket, the heat cap and the daily budget. The LLM-trader commands still refuse while halted.
- Live and autonomous books auto-halt through `risk/equity_watch.py`, journaling to `state/live_equity.csv`.
  Never reset a peak by editing that file; a human `clear-kill` is what restarts the measure.
- `analysis/symbol_atlas.py` is the cross-broker view of a symbol. It reads the adapters' own helpers:
  change a wire form in the adapter, not in the atlas.
- `scripts/risk_guard_analysis.py` is how to judge the guards. Under a week of paper data on two venues it
  recommends nothing, and neither should you.

## Skills available in this repo

`.claude/skills/`:
- `backtest-expert` — strategy spec → vectorized backtest
- `market-data-pipeline` — OHLCV / quotes via IB and Kraken
- `signal-generation` — rules → signals (with lookahead check)
- `risk-management` — sizing, VaR, heat, kill switch
- `live-signal-monitor` — polling loop, alerts only, never orders
- `order-execution` — signal → risk gate → order on IB or Kraken (own skill)
- `claude-trader` — LLM-driven trading decisions (`/trade-check`); algorithms are helpers

`.claude/commands/`:
- `/autonomous status|start|stop|tail` — manage the autonomous daemon
- `/trade-check`, `/trade-now` — LLM-driven trading (Mode A)
- `/backtest`, `/tune`, `/signal-check`, `/paper-trade`, `/positions`, `/risk-report`, `/kill`,
  `/live-trade-confirm`

`.claude/agents/`:
- `autonomous-monitor` — periodic health audit of the daemon
- `risk-gate` — pre-flight audit before going live (autonomous or human-live)
- `strategy-reviewer` — code review for lookahead/overfitting
