# Next-session backlog

**Pruned 2026-09-16** to active items only. Everything removed was either shipped, run,
superseded, or closed by an explicit user decision; the full pre-prune text is in git history
(last committed version: `4d471f3`) and in the session scratchpad backup
`NEXT_SESSION.backup-2026-09-16.md`.

## Session 2026-09-30 — QuantPort.io basket gate (code-complete, unit-tested; not run live)

Dash renamed QuantPort.io. New **BASKET** tab (first) beside **IB ACCESS**, both ahead of Approvals.
Per venue (kraken / qt / ib) the human types symbols, runs the engines (`analysis/basket_report.py`:
walk-forward record, in-sample screen kept separate, strategy mapping, overlay scalar, interpret
theses, return stats, correlation and allocator weights), and proposes the basket to the card. A
**Router gate** (`execution/basket.py`, `Router.basket_gate`) then rejects ENTRIES outside the
card-signed basket and on venues with none; exits are never gated. Rows in `state/baskets.jsonl`
are re-verified against the card registry on load, so a hand edit approves nothing.

- **Exempt until restart:** running sessions have no gate. `--require-basket` (default: on whenever
  `--require-card` is on) on `paper_kraken.py`, `paper_ib.py`, `cli signal`. After a restart a venue
  takes NO entries until its basket is signed: `python scripts/basket.py sign --venue kraken --from-seed`
  (seed = `config/basket_seed.json`, the asset config settled in the 2026-09-29/30 desktop session).
- **Firmware TODO (not done):** only the SIMULATED card signs baskets (`scripts/basket.py`). The
  firmware parser reads the nine-field trade canonical and refuses `BASKET|1|venue|SYM,SYM|analysis_hash|issued_at|nonce`,
  the safe failure. A physical card needs: a basket prompt kind on the wire (`GET /v1/basket` already
  lists proposals; a signed-row POST does not exist yet), a screen showing venue, symbol count and
  fingerprint, and a `SPEC_VERSION` bump. `firmware/tradecard/main/main.c` was already modified in the tree.
- **Not wired:** QT and IB return statistics (only Kraken pairs in the crypto sleeve fetch closes, via
  Kraken's public OHLC); the panel could not be driven against a live shim from the browser pane, so the
  live render path was checked with stubbed data only.

## Session 2026-09-24 / 25 — defect pass + capital-integrity page

Full suite after the work: **1388 passed, 1 skipped, 0 failed** (4m06s; the skip is the POSIX-only
signal test). Ruff clean on every file touched. Nothing staged or committed by Claude.

### Fixed and verified

- ✅ **`degraded` reported healthy on a stale feed — FIXED 2026-09-24.** `WorldMonitorClient.snapshot`
  set `degraded` only when a tool *raised*. A tool that succeeds but returns a payload it cached days
  earlier left the flag `false`, so a **135-hour-old energy source** was scaling live de-risk at full
  confidence (equity ×0.4669 at the time). Staleness is now measured from each payload's own
  `cached_at` against `MAX_SOURCE_AGE_H = 48.0`, deliberately tied to the existing
  `confluence.STALENESS_HALF_LIFE_H = 24.0` rather than being a second independent policy. Fail-safe:
  tripping it caps every class at `degraded_cap`. 3 tests in `test_intel_overlay.py`.
  **Runtime-exercised** — fired in a live session at `energy: 114.9h`, `degraded: True`.
- ✅ **Intel writeups were structurally empty — FIXED 2026-09-24.** All 19 writeups on disk carried
  `overlay_snapshot`, `market_context` and `warnings` in the schema and empty on disk. The engine was
  never at fault; `scripts/paper_kraken.py` called `explain()` with a bare `MarketContext()` and no
  overlay, while the same script already held a live `OverlayProvider`. Now passes the snapshot, the
  per-symbol decision, and an `r_multiple` derived from the intent. **Runtime-verified** against a
  real prompt: all three fields populated, and the card thesis gained a `geo elevated` clause.
- ✅ **`dashboard.py` fabricated a session — FIXED 2026-09-24.** `.fillna("legacy")` invented a
  session id for the 4 fills that carry none (all 2026-08-31, before `PaperBroker` stamped the field)
  and rendered it as a row beside real ones with no equity curve behind it. Now excluded, with the
  scope stated in the output rather than narrowed silently. Also cleared 4 pre-existing ruff findings
  in that file (verified identical at `HEAD`, none introduced here).

### Considered and deliberately NOT changed

- **Realized P&L excludes commission.** No emitter change. `paper_fills.jsonl` already carries
  `commission` per fill with `session_id`, so net is derivable today. Adding a column would corrupt
  alignment — the equity CSV writes its header only on file creation, so appending wider rows to the
  existing file puts 10 fields under a 9-field header. This is a consumer obligation.
- **Sizing rationale on the Questrade path** (33 of 275 fills). Out of scope by standing rule: sizing
  v2 stays Kraken-only. The coverage gap is that decision visible in the data, not a bug.
- **The 4 fills with no `session_id`** are historical; the live emitter has written it since. Under
  the "unfit data" rule the answer is exclusion, not backfill.

### Open — carried forward

1. ✅ **`paper_ib.py` writeup gap — FIXED 2026-09-25.** Same fix as `paper_kraken.py`: passes the
   live overlay snapshot, the per-symbol decision, and an `r_multiple` derived from the intent.
   **Code-complete, NOT runtime-exercised** — no IB gateway available in this session (the
   interactive-brokers MCP server is also failing to connect), so unlike the Kraken path this one
   has not produced a real populated writeup. Mirrors a fix that was runtime-verified on Kraken.

2. ✅ **Audit ledger — VERIFIED WORKING 2026-09-25. Earlier "writes nothing" was my error.**
   `state/ledger/` looked empty only because no session had yet run with the flag; it defaults ON and
   had simply not been exercised. A card-gated Kraken session then wrote
   `state/ledger/2026-09-25.kraken.jsonl` (10 rows), and `scripts/verify_ledger.py --stream kraken`
   reports **OK** over the hash chain. Full lifecycle present for both intents:
   `STRATEGY_SIGNAL → RISK_CHECK → INTENT_SENT → REJECTED → SIGNED`, each row carrying
   `prev_hash` / `row_hash` / `payload_hash`, plus `signature` and `signing_key_id` on the SIGNED
   rows. `Router.build_default(ledger=...)` does thread through to the emit sites.
   **Residual design note, much weaker than first stated:** `Ledger.append()` is still non-strict by
   default, so a *future* write failure would log rather than raise and would not be obvious from the
   outside. Worth a periodic `verify_ledger` in the runbook rather than a code change; a real
   regulated deployment would construct with `strict=True`.

3. ✅ **Leverage cap now reserves the commission — FIXED 2026-09-25.** `Router._gate` computed
   gross leverage on notional alone, so an order sized to exactly fill the headroom still debited
   the fee on top and overdrew cash. `Router._cost_reserve()` reads `commission_per_trade` off the
   broker (fee structure is broker-specific; the gate must not hardcode one) and the reserve is
   subtracted from the headroom and added to the leverage test. **Only ever tightens the gate.**
   Also exposed `PaperBroker.commission_per_trade` as a property — it was only `self._commission`,
   so both this and `monitor.live_loop`'s existing `getattr(broker, "commission_per_trade")` were
   silently reading nothing and reserving zero.
   **A second bug fell out of the first full-suite run:** the reserve must accept only a REAL
   number, not anything merely `float()`-able. `float(MagicMock())` is `1.0`, so a broker wrapper
   delegating through `__getattr__` — or any test double — fabricated a $1.00 reserve and tightened
   a risk gate on invented input (it broke
   `test_router_risk_gates.py::test_trim_mode_gross_leverage_cap_trims_to_headroom`, trimming to 99
   instead of 100). `_cost_reserve` now rejects non-`int`/`float` values and `bool`, reserving zero.
   4 tests, incl. an order at exactly 100% of equity trimming 1000 -> 999 shares and a
   delegating-broker regression. Unit-tested; not yet seen in a live session.

4. ✅ **OASIS debate output now persisted — DONE 2026-09-25.** `debate()` appends one row per run
   to `state/intel_agents.jsonl`, carrying the `Claim` / `Critique` structure the caller used to
   discard: domain, thesis, direction, self-reported confidence, adversary verdict and reason,
   cited evidence. **Records the attempt, not just survivors** — per-domain `outcome` is one of
   `no_evidence` / `no_claim` / `below_threshold` / `no_critique` / `falsified` / `fired`, plus
   `api_key_present`, so a missing key is distinguishable from a quiet world (the same silent
   no-op the `degraded` flag had). Falsified claims are dropped from the output but KEPT in the
   record, which is what makes "did upheld claims precede better outcomes than falsified ones"
   answerable at all. `journal=False` / `journal_path=` for tests; all 5 pre-existing `debate()`
   calls in `test_intel_agents.py` were switched to `journal=False` so the suite cannot write
   ground truth — verified `state/intel_agents.jsonl` is absent after a run. 4 new tests.
   **Still open:** no claim -> trade -> realized-P&L join yet. `as_of` and domain+thesis are the
   keys to join on; nothing downstream consumes them, so agent accuracy remains unmeasured.

### Analysis corrections — read before reusing any earlier number

- **The "$2,983,231 stranded" figure is retracted.** It summed `positions_value` across 48
  independent $100k accounts, frozen at 11 different as-of dates, over only 13 distinct symbols
  (QQQ in 24 books, EQB.TO in 24), and ~70% of it sat in 15 books whose cash was negative — money the
  accounts never had. It is not exposure, loss, or portfolio value. **Never restate it.**
- **The unfinished-books problem is historical.** All 48 last wrote on or before 2026-09-16; all
  sessions since closed flat.
- **Leverage explains the loss tail.** Peak gross leverage by period: 2.258× (09-01→07, 28 sessions),
  1.111× (09-08→14, 12), 1.000× (09-15→24, 40). The gate at `Router._gate` (`max_gross_leverage=1.0`)
  landed ~09-08. Marking all 48 unfinished books to 2026-09-23 cached closes: **the ten worst are all
  leveraged**; 13 of 48 leveraged overall. Split by leverage, the 35 non-leveraged books total
  **+$13,824** and the 13 leveraged total **−$23,211** — i.e. leverage, not strategy, is why the
  aggregate is negative. Caveat: only 13 symbols across 48 books, so effective sample ≪ 48; the
  direction is robust, the magnitude is not.
- **Closed-book scope** (the only fit scope for rates): 32 sessions, 154 fills, 79 FIFO round trips,
  zero unmatched lots. Winners **28% gross → 10% net**. Commission is flat $4.95/fill.

### Published

- Capital-integrity page: <https://claude.ai/artifact/9thPpNAWXmaHToWUZ6iyj4>
- Partner / diligence brief (TradeCard): <https://claude.ai/artifact/EH9ZFqiFm9oP31MVgjqHTV>

## Status (2026-09-21)

- Branch `feat/multi-scoring-attention-map`, **6 commits ahead of `origin` and NOT pushed**
  (`4d471f3` was the last pushed commit):
  `3b00c28` fix(logging) token leak · `2123b09` fix(intel) threshold calibration ·
  `70a2f8e` fix(strategies) level-trigger strength · `0a13789` fix(schema) venue + AssetClass ·
  `e3f06bd` feat(paper) flatten-on-exit + stop sentinel + warm-up + sizing-v2 default ·
  `92cab0d` docs backlog prune + session report.
  Suite at the last full run (2026-09-21) **1238 passed / 1 skipped / 0 failed** (the skip is the
  POSIX-only signal test). Ruff clean on every file touched; remaining findings are pre-existing
  (`cli.py`, `tests/test_monitor.py`, `tests/test_intel_interpret.py`,
  `tests/test_paper_broker_journal.py`).
- **Uncommitted, Claude's (2026-09-18):** this file, `SESSION_REPORT_2026-09-18.md`, README /
  CLAUDE.md doc fixes, the exit variants (`signals/{profit_lock,candle_exit,overbought_exit}.py`,
  `curves.py`), thesis intensity + heartbeat, the params resolver (`analysis/params.py`), the
  backlog fixes marked "FIXED 2026-09-18" below, and their tests. Nothing staged or committed.
  Futures harness files deleted (unstaged deletions).
- **Uncommitted from 2026-09-14** (user's WIP, not Claude's): desk-policy venue split —
  `src/trading_live_claude/desk_policy.py`, `tests/test_desk_policy.py`, `scripts/paper_ib.py`,
  `scripts/paper_global.py`. This is the **interim** posture:
  IB carries futures/commodities, equities trade on Questrade, no IB FX, one book per currency.
  **IB is staying**, not being removed. Foreign-venue equities and IB FX are held, not dropped
  (see "Held for IB market-data subscriptions").
- **Weekend additions (2026-09-19, not Claude's; kept as given):** crypto random-matrix /
  LSTM-Transformer research, which is the deferred "LSTM" line picked up on crypto:
  `analysis/rmt.py` (Marchenko-Pastur denoised correlation, eigen-embedding),
  `models/lstm_transformer.py`, `scripts/crypto_rmt_lstm.py`, tests `test_rmt.py` /
  `test_lstm_transformer.py`, report `reports/crypto_rmt_lstm_2026-09-19/`, and a `deep` extra
  (`torch>=2.6`, installed 2.14 CPU) in `pyproject.toml`. Research only (no orders). Result on 19
  coins, 216 out-of-sample days: R² vs a zero forecast −0.02, daily rank IC 0.0004 (t = 0.02), so
  no predictive signal yet (a raw-returns ablation ran too). Tests pass in the full suite (1219).
- **Stopping sessions:** `touch state/STOP_<session_id>` (path printed at boot). The session
  exits within one poll and flattens. Never `TaskStop` — it hard-kills and skips the flatten.
  The global `state/STOP` now stops **every** running session (fixed 2026-09-18); a stale one is
  ignored by later launches.
- **Sizing v2 is the Kraken default** (`--no-sizing-v2` to opt out).
- **Warm-up adds nothing (measured 2026-09-18):** 0 of 16 sessions bought after the first poll,
  even with 60 s polling for an hour (daily-bar strategies). Recommendation: drop
  `--warmup-interval` from launches. Open option: a warm-up that ends at the first quiet poll,
  if faster exit checks for the profit lock are wanted.
- **Warm-up cadence (new 2026-09-17):** `--warmup-interval 60 --warmup-minutes 60` on both paper
  launchers polls faster for the first hour, then falls back to `--interval` in the same process
  (no restart, so no flatten). Only ever speeds polling up; 5 s floor; off unless set. Caveat:
  strategies run on daily bars, so warm-up re-checks the forming bar and live quotes — it
  re-establishes positions quickly after a flatten, it does not create new daily signals.
- **Telegram delivery works** (read-only `getMe` / `getChat` OK, 2026-09-18). Thesis alerts had
  gone silent because no reading cleared the 09-16 gates. **Gates lowered 2026-09-18 by user
  decision** to `strategic_risk >= 67` / `conflict_events_active >= 4` (base rates on real reads
  ~81% / ~70%; see the `interpret.py` header). The same constants drive the live-loop trim, so
  CGL.TO / PAXG/USD entries get ×0.75 on most reads. The graph journal now sends a **heartbeat**
  (first poll after launch, then every `--heartbeat-hours`, default 24). Code-complete and
  unit-tested; not yet runtime-exercised, since the graph journal was stopped before a relaunch.

## Next session — ordered work list (set by the user 2026-09-18)

Work these in order. Each links to its detail further down.

1. **Flatten-on-exit gaps.** ✅ **Journal rehydration DONE 2026-09-21:** `PaperBroker.resume()`
   replays a session's fills through the same accounting as live fills, cross-checks cash and
   realized P&L against its last equity row (refuses on mismatch), restores peak equity, and
   continues order ids. `--resume-session <id>` on `cli signal` and `paper_kraken.py` (not yet on
   `paper_ib.py` / `paper_global.py`, which are the desk-policy WIP). Restart recipe: stop with
   `--no-flatten-on-exit`, relaunch with `--resume-session <id>` and the same `--paper-equity`.
   5 unit tests. Runtime check, read-only against the real journals: session `ec72c96f` replays to
   the journal's cash/realized exactly, and **all 48 orphaned books replay cleanly**.
   **Your call:** resume them to close them (that books today's prices into old sessions) or leave
   them marked unfinished. Still open under #1: Also: the card-gated flatten
   (`--require-card` makes exits need an ACCEPT), the unified `_book_risk`, and a policy for
   rejected flattens. Detail: "Flatten-on-exit: remaining gaps".
   **Why it's first (measured 2026-09-18):** flatten-on-stop plus relaunch re-bought a name just
   sold 25 times on 09-17/18, often 1–16 minutes later, at $9.90 per round trip plus ~0.1% in price.
   Commissions on those sessions were $297; QT's +$53.61 gross on 09-18 became −$65.20 net.
   Rehydration lets a restart keep the book instead of selling and re-buying it. Until then, avoid
   mid-day restarts (`--no-flatten-on-exit` without rehydration orphans the book).
2. ✅ **Token-store salt migration — DONE 2026-09-21** (code-complete, unit-tested, checked on a
   copy). New file format `TLC2$` + 16-byte random salt + Fernet token; the legacy fixed-salt file
   is still read. No separate migration step: the next ordinary token refresh rewrites the file in
   the new format, and the `.bak` keeps the legacy copy, which stays readable. Checked on a copy of
   the real `tokens.json.enc`: legacy readable, rewritten, reads back identical, `.bak` fallback
   works; the real files are unchanged (sha256 before/after). 4 new tests. **Not yet
   runtime-exercised:** the first real refresh with this code is the live test.
3. ✅ **`apply_overlay` WIRED 2026-09-21** (your call; code-complete, unit-tested, not yet
   runtime-exercised). `intel.apply.OverlaidBias` wraps the boot-time allocation and, on every
   call, runs `apply_overlay` with the overlay's *current* class decisions, so the allocator bias
   carries the OSINT class scalar and follows the 15-min refresh. It is marked `applies_overlay`,
   and `LiveMonitor` then multiplies in only the strategy-risk part of `combine()`; the halt and
   its reasons still come from the full combination, so the overlay is applied **once**. Wired in
   `cli signal` (QT) and `paper_kraken.py`, only when the overlay is on and the allocator produced
   weights; otherwise the old in-loop path runs unchanged. **Not wired in `paper_ib.py` /
   `paper_global.py`** (desk-policy WIP; `paper_ib`'s bias also carries the bond-hedge rule).
   Known difference: the loop's [0.1, 3.0] bias clamp now applies to bias x overlay rather than
   to the bias alone, which only matters when their product falls below 0.1. 3 new tests
   (conviction identical to the old in-loop path, halt kept, bias follows a mid-session refresh).
4. **Dependency caps in `pyproject.toml`** (no upper bounds; `ib_insync` unmaintained). Cap at
   the next major version from the lockfile, and note `ib_insync` -> `ib_async`.
5. **Signal-system plan phases: V4 (trend pullbacks), then calibration.** "Plan — robust
   entry/exit signal system", Phases 2 and 3. Phase 0.1 (params resolver) is built; its default
   flip (`--params wf`) is still your call.

Open decisions carried alongside (not ordered): the `QUESTRADE_ENV` / autonomous account guard
(🔴), `--params` default, CI scope, `reports/` tracking policy, the QT commission reserve.

## Guardrails — standing rules and closed decisions (do not reopen unprompted)

- **Data-first (2026-09-08).** Accrue before tuning, promoting, or wiring gates. A single WF pass
  validates the *protocol*, never promotes a tier. Don't wire microstructure gates until ≥90 days
  of density. No new autonomy or scheduled tasks without asking.
- **Microstructure controls beyond top-of-book: HELD for L2 market data (2026-09-16).** This
  replaces the 2026-09-09 "omitted" decision; see "Held for market data" below. Don't build the
  accumulator, `LiquidityGate` or depth-aware controls before L2 data is available.
- **Alerter: no changes (2026-09-09, reaffirmed 2026-09-17).** Don't propose Telegram alerter
  dedup or other edits. On 09-17 the user also declined adding an HTTP-status check to
  `_telegram`, so rejected sends stay silent by choice. Verify delivery out-of-band instead
  (read-only `getMe` / `getChat`, or the user's phone).
- **FX pair-trading: not tradeable here (2026-09-04).** Cost drag is 25–75% of the daily FX
  excursion. Only revisit with a sub-pip cost model or `KalmanPairs`.
- **FX single-name sleeve: dropped (2026-09-05).** 0 fills in 14 polls. Only revisit with a
  sub-minute FX vendor or materially looser FX thresholds. FX deep parquets stay on disk.
- **`LQD`, `MUB`, `DBA` excluded from selection (2026-09-04).** Don't re-propose.
- **No sizing v2 on QT (2026-09-16).** Sizing v2 stays Kraken-only (`scripts/paper_kraken.py`).
  Don't add it to `cli.py signal` or propose porting it to the QT path.
- **Client Portal Gateway** (`Downloads/clientportal.gw`): never leave it authenticated to the
  LIVE account. `conf.yaml` has `origin.allowed: "*"` and there's no read-only mode.

## Open decisions — 🔴 user call

1. **Rotate the IBKR OAuth token.** Its value was committed (`638f8d3`) and pushed to
   `origin/feat/multi-scoring-attention-map` and `origin/feat/tradecard-approval`. Now redacted
   here, but still in git history.
2. **The kill-switch is effectively 8%.** `config/trading.yaml` sets
   `max_drawdown_kill_switch: 0.08`, overriding the 0.03 default landed 09-08.
3. **The real-money paths lack the paper wiring.** `trading live` and `autonomous_run` build
   `LiveMonitor` without the overlay / interpret / allocator / persistence hooks, the stale-quote
   guard, session routing, flatten-on-exit or the stop sentinel.
4. **`Router.check_forced_exits` has no caller** outside tests, so the "cheap" intraday
   loss-exit never runs.
5. **The QT paper book mixes CAD and USD names** without conversion (QQQ, VALE, DBC beside `.TO`).
6. **TradeCard signs neither the thesis nor the stop**, and the stop isn't displayed.
7. **Graph-weighted interpret default** — unanswered. The proposal is to keep it behind a flag,
   off, until weeks of graph polls can test whether persistence predicts anything.
8. **Should thesis intensity drive anything?** `intel/thesis_intensity.py` (2026-09-18) grades
   each firing thesis 0–1: bounded `log10(1 + 9u)` curves over each input's value (neutral floor
   → stress ceiling, per-input bounds in `MAGNITUDE_BOUNDS`) and over hours firing continuously
   (0–72h, a fresh fire weighted 0.5). OR theses take their strongest input, AND theses their
   weakest. It is **display-only**: shown in thesis alerts and the heartbeat, but it doesn't
   change firing, confidence bands or the sizing trim. Code-complete and unit-tested; not
   runtime-exercised (the graph journal is stopped). Corpus replay: intensity p50 ≈ 0.29–0.39,
   max 0.50, because firing streaks never exceeded 7h in the record, so the time factor has
   never passed ~0.6. Options: map intensity to the confidence band, make the ×0.75 / ×0.5 trim
   continuous, or require a minimum intensity before alerting.

## Plan — robust entry/exit signal system for the QT book, by strategy family (drafted 2026-09-18)

**Scope (user decisions 2026-09-18): the entry/exit V-layer (V1–V4) applies to the QT equity-venue
book, all 14 names.** That includes DBC (`commodity`) and CGL.TO (`precious_metals`), which trade on
Questrade even though `classify_symbol` doesn't put them in the equity class. Crypto (Kraken) keeps
its own strategy entries and exits and gets no V-layer.

**Goal.** One signal policy that decides, per strategy family, which entry and exit legs run on an
equity name and with which parameters, every choice backed by pinned-parameter walk-forward
evidence net of realistic costs. It replaces today's hand-set CLI flags (`--profit-lock-exempt
ts_momentum` ...). The standing QT config (V1 + V2 + V3 with `ts_momentum` exempt) keeps running on
paper meanwhile as the interim policy.

**What the 2026-09-18 evidence already says** (in-sample, one config; hypotheses, not settled):
profit-taking exits (V1 lock, V2 bearish candle, V3 overbought) help mean-reversion and channel
names (XIU.TO, VALE, XIC.TO, RSI.TO) and destroy trend names (QQQ +97% -> -1%). Candles help
mean-reversion *entries*, not exits. Every "win" so far came partly from less time in market, so
return and exposure must be scored alongside sortino/DD.

### Ground rules (every phase)
- **Scoring:** OOS `sortino_over_dd` is primary, always shown with OOS Sortino, OOS return and time
  in market (a leg that just sits out can "win" the ratio; V1 on all 14 names did).
- **Costs:** realistic per-name costs (`CostModel.from_price` + $4.95/fill) **and a 2x cost stress
  gate**. Nothing is adopted that loses to the baseline at 2x.
- **Pinned-parameter walk-forward** (as in `scripts/calibration_sweep.py`, not the re-optimizing
  `sweep_universe.walk_forward`), using the equity `WF_PROTOCOLS` (2y train / 6mo test / 10 OOS
  trades), so every OOS number is attributable to a specific parameter value.
- **Small a-priori grids**, a count of configs tried, and a noise band: a winner has to beat the
  baseline by more than the fold-to-fold spread, not just on the median.
- **Fold policy** (09-16 precedent): adopt only with WFE >= 1.0 and enough OOS trades. Otherwise
  the cell stays "strategy exit only".
- **Data-first guardrail:** one WF pass validates the protocol; promotion also needs the paper A/B
  (Phase 5).

### Phase 0 — Foundations (blocking; do first)
1. **Fix the QT params path (existing red item "live paper path runs STOCK CLASS DEFAULTS").** Add a
   `resolve_params(strategy, symbol, mode)` chain: CLI -> WF registry -> `calibrated_kwargs` ->
   defaults. 13 of the 14 QT names have registry params, but QT runs defaults. Calibrating exits on
   top of the wrong entry params would tune the wrong system. Re-run the 09-18 exit backtests on
   registry params afterwards.
2. **Exit-reason attribution in the backtest engine.** Tag each exit (strategy / stop / lock /
   candle / overbought) and each V4 entry, so each leg's contribution is measured directly instead
   of inferred from A/B deltas.
3. **One `SignalPolicy` object** (strategy family -> legs + params) resolved per symbol for the QT
   book (all 14 names, DBC and CGL.TO included), replacing the per-leg exempt flags. Kraken names
   resolve to "no V-layer". Journal the resolved policy per intent in `paper_orders.jsonl`.
4. **Data:** refresh the equity cache (`scripts/warm_cache.py --held --seed equity --years 5`).
   Consider widening the equity basket beyond the 12 book names with the other WF-registry
   equities (32 registry names total), so each family cell has more than 2–5 names. Finish the
   graph-journal test leak (spawned session) so no corpus is polluted.

### Phase 1 — Cells to calibrate (QT book, 14 names)
- **Families present in the book:**
    * trend: ts_momentum (EQB.TO, QQQ, VDY.TO);
    * channel: atr_channel (ZEB.TO, CGL.TO);
    * mean reversion: bollinger (VALE, DBC, SLF.TO, RSI.TO), rsi_meanrevert (XIC.TO, CRT.UN.TO),
      confirm_* (ENB.TO, SRU.UN.TO);
    * composite (XIU.TO), evaluated as its own cell.
  DBC and CGL.TO calibrate within their strategy family. Report their per-name result separately,
  since they're the only commodity and gold names.
  Widen each family with registry equities (Phase 0.4) so no decision rests on 1–2 names.
- **Starting hypotheses:**
    * trend: strategy exit only (or a wide Chandelier trail), no V1–V3; V4 pullback buys inside an
      up-trend;
    * mean reversion: V1–V3 candidates, candle confirmation on entries, no V4;
    * channel and composite: test both ways.

### Phase 2 — Build V4 "buy on oversold" (user idea 2026-09-18: win back the return V1–V3 give up)
- **Trigger (mirror of V3):** on the last completed bar, close below the lower Bollinger band
  (20, 2 sd) or RSI(14) <= 30. Optional bullish-reversal candle confirmation, the mirror of V2 and
  the one place candles measurably helped.
- **Role (user decision 2026-09-18): trend pullbacks only.** For trend-family names, buy oversold
  dips only while the trend filter is up (the strategy's own signal is on, e.g. 126-day ROC > 0), so
  it never catches a falling knife in a downtrend. It adds exposure in names that keep their
  strategy exit, offsetting the return the V1–V3 exits give up elsewhere. **No re-entry role:** the
  re-entry lockout after V1–V3 exits stays as is.
- **Guards:** the ATR stop is mandatory on every V4 entry; at most one V4 entry per symbol per N
  bars; normal sizing and the Router; trade count and cost drag reported for every arm.
- **Tests:** no lookahead, the trend filter (no buys while the trend is off), the cooldown, live
  completed-bar reading, and the entry tag in alerts.

### Phase 3 — Calibrate on data (pinned-param WF per family cell)
- **Grids (a priori, small):**
    * V1: `arm_atr` {0.75, 1, 1.5} x `giveback_min_atr` {0.75, 1, 1.5} x `full_atr` {3, 4, 6};
    * V2: pattern set {8 mirrors, strong 3-bar only} x `min_gain_atr` {0.5, 1, 2};
    * V3: `rsi_level` {70, 75, 80} x `bb_std` {2, 2.5};
    * V4 (trend family only): `rsi_level` {25, 30, 35} x `bb_std` {2, 2.5} x candle confirm {on,
      off}. The trend filter is always on.
  Strategy core params come from the WF registry (Phase 0.1) and are **not** re-tuned here, to keep
  the search small.
- **Per family cell:** arm "strategy only" versus each leg, pairs of legs, and V1–V3 (+V4). Take the
  median across the family basket of OOS sortino/DD, with Sortino, return, time in market and
  trades beside it, at realistic and 2x costs.
- **Adoption rule (tolerances confirmed by the user 2026-09-18):** beat "strategy only" on OOS
  sortino/DD by more than the noise band; OOS Sortino no worse than -0.05; return give-up <= 3 pp
  per year; WFE >= 1.0; enough trades; still ahead at 2x costs.
- **Output:** `reports/signal_policy_calibration_<date>.{csv,md}` plus the `SIGNAL_POLICY` table in
  code, with provenance (the report and the numbers) in comments.

### Phase 4 — Integrate (QT book)
The resolver picks each QT symbol's policy from its strategy family, and Kraken names get none. CLI flags become overrides only. Alerts name the leg that fired. The boot banner prints
the resolved policy per symbol.

### Phase 5 — Validate on paper (promotion gate)
- **Concurrent A/B:** two QT paper books on the same equity names at the same time, one on the
  calibrated policy and one on strategy exits only. Running them together removes the regime
  confound that sequential sessions have.
- **Length:** at least 4 weeks. Compare per book on sortino/DD, Sortino, return, time in market and
  trade count. Add a book tag to the journals.
- **Promotion:** only if the policy book holds up.

### Decisions (2026-09-18) and what is still open
- **Resolved:** DBC and CGL.TO are in the V-layer; V4 is trend pullbacks only; the adoption
  tolerances (-0.05 Sortino, <= 3 pp/yr return) stand.
- **Still open:** should the Phase 5 concurrent A/B run two QT paper books (doubling alert volume)?

## Runbook — resume from cold

```
# 1. Tests
.venv/Scripts/python.exe -m pytest tests/ -q --no-cov --ignore=tests/test_quantconnect.py

# 2. Refresh cache if stale
.venv/Scripts/python.exe scripts/warm_cache.py --held --seed equity --years 5

# 3. Pre-flight: kill-switch clear and no stale stop files (a stale STOP kills new sessions)
ls state/HALTED state/STOP* 2>/dev/null

# 4. Paper monitors (flatten-on-exit is the default on both; sizing v2 is the Kraken default)
.venv/Scripts/python.exe -m trading_live_claude.cli signal --strategy bollinger \
    --symbols "XIC.TO,ZEB.TO,CGL.TO,VALE,DBC,SRU.UN.TO,CRT.UN.TO,ENB.TO,XIU.TO,VDY.TO,SLF.TO,RSI.TO,SONY,EWJ,FLJP,JPXN,NVDA,AVGO,AMAT,MU,INTC,QCOM,TXN,ADI,NXPI,ON,SMH,SOXX" \
    --strategy-map "XIC.TO=rsi_meanrevert,ZEB.TO=atr_channel,CGL.TO=atr_channel,VALE=bollinger,DBC=bollinger,SRU.UN.TO=confirm_rsi_meanrevert,CRT.UN.TO=rsi_meanrevert,ENB.TO=confirm_bollinger,XIU.TO=composite,VDY.TO=ts_momentum,SLF.TO=bollinger,RSI.TO=bollinger,SONY=rsi_meanrevert,EWJ=bollinger,FLJP=bollinger,JPXN=rsi_meanrevert,NVDA=bollinger,AVGO=bollinger,AMAT=bollinger,MU=rsi_meanrevert,INTC=bollinger,QCOM=bollinger,TXN=bollinger,ADI=bollinger,NXPI=bollinger,ON=bollinger,SMH=bollinger,SOXX=bollinger" \
    --interval 300 --paper --paper-equity 100000 --level --intel-overlay
.venv/Scripts/python.exe scripts/paper_kraken.py --interval 300 --paper-equity 100000

#    Optional warm-up (2026-09-17): poll faster for the first hour to re-establish positions after a
#    flatten, then fall back to --interval in the same process. Add to either command:
#      --warmup-interval 60 --warmup-minutes 60
#    QT's current map runs ENB.TO=confirm_bollinger, SRU.UN.TO=confirm_rsi_meanrevert, XIU.TO=composite.
#
# 4c. ADDED 2026-09-25 (user): Japan names + chipmakers. 16 new names, QT watchlist is now 28.
#     Strategy per name chosen by pinned-param WF over 5 a-priori strategies x 10y of bars
#     (reports/chip_japan_wf_2026-09-25.md; 63 of 120 cells cleared the gate, 20 of 24 names at
#     best-of-5). Japan: SONY=rsi_meanrevert, JPXN=rsi_meanrevert, EWJ/BBJP/FLJP=bollinger.
#     Chips: ASML and MU = rsi_meanrevert, the other 13 = bollinger.
#     HELD BACK, too few OOS trades: TSM (7), KLAC (8), ARM (5 over 2 folds), BBJP (8) — BBJP is the
#     5th Japan name asked for, held at 8 OOS trades against a 10 minimum.
#     EXCLUDED on OOS return despite clearing the score/WFE/trades rule: MRVL (-4.87% on a 27% win
#     rate), AMD (-0.49%), LRCX (-1.83%). The tier rule never asks whether the name made money.
#     EXCLUDED on degradation: ASML (WFE 0.059 on live params).
#     *** The selection sweep ran DEFAULT params, but `--params wf` resolves every one of these names
#     to CALIBRATED params (bollinger 30/1.5, rsi_meanrevert 15/35) — a different configuration from
#     the one scored. Re-scored on the live params
#     (reports/chip_japan_live_params_2026-09-25.md): 16 of 20 survive, and AMD + ASML flipped from
#     robust to failing. Never promote on a sweep whose params are not the ones the book will run. ***
#     CAVEATS: (a) best-of-5 selection is biased upward — treat the ranking as a shortlist, not
#     evidence; (b) `bollinger` won 15 of 20, which is a suspiciously uniform answer; (c) several
#     WFEs are 5-32x, which means the IN-SAMPLE score was tiny, not that OOS was excellent;
#     (d) single-stock semis carry 20-60% OOS max drawdowns (ON -61.7%, NXPI -46.8%, AVGO -41.2%)
#     against an ETF book that ran -5 to -10% — the vol-scaled cap will size them smaller and the
#     50% clamp binds, but this changes the book's risk character; (e) 32 names compete for 6 slots,
#     so this adds candidate competition and alert volume, NOT exposure.
#
# 4a. OMITTED 2026-09-25 (user): QQQ, EQB.TO, LMT, RTX are out of the QT book. All four are
#     ts_momentum and all four scored 0 with ZERO out-of-sample trades across 6-13 folds in the
#     pinned-param walk-forward (reports/full_sim_wf_2026-09-25.md) — a 126-bar test window is
#     shorter than the strategy's own 126-189 bar lookback, so no trade ever completes. Their WF
#     numbers are evidence of nothing, in either direction. Note what this leaves: VDY.TO is the
#     only ts_momentum name still in the book (OOS 41.3, WFE 9.9, but 6 OOS trades = 'watch'), and
#     it was the biggest loser on both live sessions this week. ITA/NOC/GD stay for now on
#     defaults with 388 bars, which is too little for a single fold.

# 4b. Exit variants — STANDING config (user decision 2026-09-18): add to the QT command above.
#       --profit-lock --profit-lock-exempt ts_momentum --candle-exit --candle-exit-exempt ts_momentum --overbought-exit --overbought-exit-exempt ts_momentum
#     V1 + V2 + V3 on every strategy except ts_momentum. Backtest: "Exit variants" below.
#     2026-09-22 (user): bundle V4 with them — add --oversold-entry --oversold-entry-only ts_momentum
#     (code-complete, unit-tested, NOT walk-forward calibrated; adds a tranche to a HELD trend name).
#     Also new 2026-09-22: --parallel-sizing (default ON, QT + Kraken), --mute-alerts RSI.TO (for now),
#     defense names ITA,LMT,RTX,NOC,GD on the QT watchlist as ts_momentum (no WF evidence: LMT/RTX
#     "watch" tier in reports/wf_symbols_defense_2026-09-22.md, ITA/NOC/GD uncached), and
#     --no-flatten-on-exit on every launch (user rule 2026-09-22: never flatten without an ask).
#     QT cap raised to 5 (user 2026-09-22): add --max-positions 5 to the QT command (Kraken stays on
#     trading.yaml's 3). --trim-to-slots is ONE-SHOT (ran 16:15 UTC on fba831e3: every holding x0.80,
#     pro rata); keep it off the standing command or every restart trims again.
#     (Partial-sell realized_pnl bug: FIXED 2026-09-24, see "Queue — correctness bugs".)

# 4c. Restart WITHOUT closing the book (2026-09-21): stop the running session with its flatten off
#     (launch it with --no-flatten-on-exit), then relaunch the same command plus
#       --resume-session <session_id>        (same --paper-equity as the original)
#     The launch refuses if the journals disagree; nothing is re-bought.

# 5. Graph journal
.venv/Scripts/python.exe scripts/graph_journal.py --iterations 96 --sleep 900 --wash-min-hours 72 --persistence-threshold 5

# 6. Stop a session cleanly (per session — see the global-STOP bug)
touch state/STOP_<session_id>
```

## What's queued and unfixed

Note: IB-paper feedback-loop gaps 1-6 all closed 2026-09-03 → 2026-09-04. Cross-path wiring
Tiers 1-3 closed. Remaining Cross-path work (Tiers 4-5) blocks on data accrual — see the
data-first sequencing section above.

- **Cross-path Tiers 4 + 5** — Realized P&L → thesis calibration; prediction evaluation.
  Both need weeks of accrued paper fills + thesis history. 7 days accrued / ~30 days
  minimum for a diagnostic-scale run. **Data-blocked, do not build now.**
- **`enrich_with_agents`** — built + tested + never called. Held pending Anthropic Console key.
- **IB OAuth 1.0a for CP Gateway auth-skip — queued 2026-09-04.** User confirmed OAuth1 flavor
  (the Third-Party API path — RSA-SHA256 signed requests + Diffie-Hellman key exchange for a
  Live Session Token, then LST-signed HMAC-SHA1 for `/v1/api/*` calls). Real build, ~4-6 hrs.
  **BLOCKED on user-side setup** — needs the following artifacts before code can start (all
  from IBKR Client Portal → Settings → OAuth Access → Configure Third-Party API):
    1. `IBKR_OAUTH_CONSUMER_KEY` — assigned when the app is registered
    2. `IBKR_OAUTH_TOKEN` — one pasted this session as `e0d75b4c5c1d2c0f2af7` **must be rotated
       first** (it appeared in the session transcript at
       `.claude/projects/C--Users-PC-Downloads-FRM-Claude/bac3334b-*.jsonl`, session-local but
       persistent). Rotate → new token → put in `.env` (never chat).
    3. `IBKR_OAUTH_TOKEN_SECRET` — paired with the token
    4. `IBKR_OAUTH_SIGNING_KEY_PATH` — path to a local RSA-2048 private key `.pem` whose public
       half is uploaded to IBKR (`openssl genrsa -out ibkr_signing_key.pem 2048`)
    5. Confirmed DH prime + generator from IBKR's OAuth docs (public constants, baked in code)
  Build shape:
    * New `OAuth1Auth` class in `src/trading_live_claude/brokers/ib_web.py` implementing
      `IBWebAuth` interface
    * Live Session Token exchange + HMAC-SHA1 request signing
    * New `--auth oauth1` flag on `scripts/paper_ib.py` (default stays `browser` for backward
      compat)
    * 4 new secret fields in `settings.py` (all default empty)
    * respx-mocked tests for the DH + signed-request flow
    * Update `.env.example` with the field names
  Semantics: this REPLACES the manual browser-login step on CP Gateway. CP Gateway (the Java
  daemon) still needs to be running — OAuth just handles authentication automatically instead
  of requiring the click-through login page.
- **Liquidity-gated entry/exit — queued behind the accumulator.** 2026-09-05 forward decision:
  once the rolling `state/liquidity_hourly.parquet` accumulator has deep-enough coverage
  (~20 obs/cell target, ~140 days), turn the heatmap into a live gate. Design:
  * New `LiquidityGate` primitive mirroring `PersistenceGate` shape — refreshes daily from
    the accumulator, callable `(symbol, ts) → (mult, reason)` returning a size multiplier
    in `[0.1, 1.0]` per that symbol's (hour, weekday) z-score.
  * Wired into `LiveMonitor.step()` alongside `overlay_for` / `interpret_for` /
    `weight_bias_for` — applied last so the chain is
    `conviction *= overlay × interpret × weight_bias × liquidity_mult`.
  * Two prototypes: (a) pure trim (cold cells only cut, hot cells no boost — matches the
    "de-risk for period inactivity" framing, safer to launch); (b) full amplification (hot
    cells up to 1.5× within the calibration ceiling of 3.0 — needs WF evidence).
  * Validation prerequisite: compare cell-hot vs cell-cold realized slippage on accumulated
    fills. Only ship the gate if cold-cell slippage is measurably worse (e.g., 2×+ typical).
    Without this, gate risks cutting sizing on a cost proxy that doesn't actually cost.
  * Fail-open on every path: missing accumulator, refresh exception, unknown symbol all
    resolve to mult=1.0. Same discipline as PersistenceGate.
  * Revisit earliest at ~2026-Dec (accumulator reaches ~90 days at 30-obs equivalent for
    fewer cells).

- **Microstructure accumulator — OMITTED 2026-09-09 per user decision.** Build spec
  below kept for historical context; do NOT propose implementing this. Downstream
  LiquidityGate wire-up is stranded as a result — do not propose that either.

- **Microstructure accumulator ** Enables the
  three-stage liquidity chain (accumulator → deeper heatmaps → `LiquidityGate` wiring)
  by starting the durable data-collection layer. All three downstream items block on
  the accumulator existing; today they run on one-shot 30-90d snapshots.

  **Script:** `scripts/microstructure_accumulator.py` — a slim per-hour cron/scheduled
  task that appends one row per (symbol, timestamp) to
  `state/microstructure_hourly.parquet`. Not a paper monitor, not a graph writer — a
  narrow single-purpose data-collection loop.

  **Storage schema** — one parquet file, appended row-by-row, one row per (symbol, ts):
  ```
  ts:            datetime64[ns, UTC]      # hour boundary (floor of collection time)
  symbol:        string                   # routed form ("BTC/USD", "SPY", "XIC.TO")
  broker:        string                   # "kraken" | "questrade" | "ib_web"
  hour_volume:   uint64                   # trades in the hour
  hour_open:     float                    # first trade / OHLC open
  hour_high:     float
  hour_low:
  hour_close:    float
  bid:           float | null             # if available at collection time
  ask:           float | null
  spread_bps:    float | null             # (ask - bid) / mid * 10000
  bid_size:      uint32 | null
  ask_size:      uint32 | null
  ```

  **Sources per broker:**
  * Kraken: `/public/OHLC` at `interval=60` for the sleeve pairs (no auth). One call
    per pair per hour. Sleeve size 13 → 13 calls/hr.
  * Questrade: `markets/candles` at `interval=OneHour` for the equity 14-pool + one
    `markets/quotes` for bid/ask/sizes. Auth already handled via refresh token flow.
  * IB Web: `/iserver/marketdata/history` at `bar=1h period=1d` for STK proxies
    (bonds/metals/commodity ETFs). Needs CP Gateway auth alive.

  **Cadence:** hourly cron (Windows scheduled task or manual keep-alive), fires at
  :05 of each hour so it captures the just-closed prior hour. Deduplication via
  (symbol, ts) unique constraint; re-runs are idempotent.

  **Failure modes** (each recorded in a companion `microstructure_accumulator.log`):
  * Broker down → skip that broker's symbols this hour, retry next.
  * Symbol resolution error (like ARX.TO / RIG.TO) → log once per symbol per day,
    write empty row so gaps in coverage are visible without polluting the parquet.
  * CP Gateway auth expired → warn once, downgrade to Kraken + QT only that hour.
  * Never raises to the shell — the accumulator must survive its own failures.

  **Integration:** `scripts/liquidity_heatmap.py` grows an `--accumulator` flag that
  reads from the parquet instead of doing fresh fetches. `LiquidityGate` (queued in
  the wire-up section above) reads from the same parquet — same data source, one
  refresh path.

  **Rollup:** every night a companion `_rollup.py` computes per-symbol
  `(hour, weekday) → mean/median/std volume` matrices from the accumulator, writes
  `state/microstructure_rollup.parquet`. Consumers (heatmap, gate) query the rollup,
  not the raw log — faster and lets the raw log stay append-only.

  **Estimate:** ~3 hr build (accumulator + rollup + tests + scheduled-task doc).
  Zero data-window blocker; starts collecting on first run.

  **Revisit thresholds** for consumers:
  * Heatmap regeneration: comfortable at ~20 obs/cell → ~90 days rolling
  * LiquidityGate wire-up: ~30 obs/cell → ~140 days rolling
  * Both self-service once accumulator runs.

  **Standing user rule:** the accumulator will need a scheduled task to run hourly
  without a live claude session. Per the `no-unattended-automation-without-consent`
  standing rule, ASK BEFORE registering the scheduled task; do not create it as part
  of the build.

- **Liquidity heat map — rolling accumulation not yet wired.** 2026-09-05 preliminary run
  produced heatmaps on the sparse windows available (crypto ~30d hourly via Kraken cap;
  IB STK/equity/fixed_income/precious_metals/commodity ~90d hourly via IB Web). Per-cell
  observation density on that first pass is 4-5 (crypto) to 13 (IB STK) — enough for a
  visual guide, marginal for stable per-cell statistics. To get comfortable 20-30 obs/cell
  (~140 days everywhere), the honest path is a rolling accumulator:
  * Cron/scheduled hourly append of the latest hour's volume-per-symbol into
    `state/liquidity_hourly.parquet` — one row per (ts, symbol, volume).
  * Kraken side is trivial (no auth, one call/pair/hour); IB side needs the CP Gateway
    24h re-auth loop already known + the tickle machinery (`_TickleThread` in
    `scripts/paper_ib.py`) — probably better run daily as a batched 24-hour fetch than
    hourly to reduce auth pressure.
  * Regenerate heatmap on demand from the accumulator, no fresh fetches needed.
  * Revisit at ~2026-Dec (~90 days) or ~2027-Q1 (~140 days) for the density threshold.
  * FUT class stays blocked on Alt B (TWS socket) regardless of accumulator progress.
  Full heatmap re-generation is bounded (~10s) once the accumulator exists.

- **Liquidity heat map (Option B) — queued 2026-09-04.** Script built as
  `scripts/liquidity_heatmap.py`; syntax-verified but NOT yet executed to avoid competing with
  the running FX + crypto Kraken deep-fetch chains for bandwidth. Produces one PNG per asset
  class (rows=hour UTC, cols=weekday, per-asset normalization) plus a combined markdown at
  `reports/liquidity_heatmap_<tag>.md` with top-3 hot/cold buckets per asset. Data sources:
  IB Web `/iserver/marketdata/history` (bar=1h, period=90d) for equity + fixed_income +
  precious_metals + commodity + futures (FUT front-month); Kraken `/public/OHLC` (interval=60)
  for crypto + FX. Run after the deep-fetch chains complete: `python scripts/liquidity_heatmap.py`.
  Needs CP Gateway auth'd for the IB classes.
- **FX single-name sleeve: DROPPED (2026-09-05, explicit user decision).** After the FX
  pair-trading rejection (below), a small FX single-name sleeve (4 EUR-cross legs on
  bollinger/confirm_bollinger/rsi_meanrevert/candle_hammer) was stood up on paper_kraken to
  collect empirical tuning data. After ~70 min of live polling the sleeve produced 0 fills
  across 14 polls — Kraken FX quotes essentially static at 5-min cadence and every calibrated
  threshold too tight vs the FX daily excursion. `FXSleeveEntry`, `FX_SLEEVE`, the
  `--pool crypto|fx` selector on paper_kraken, and the `fx` option on graph_journal's
  `--pools` were all removed. Do NOT re-propose an FX single-name sleeve unless (a) an FX
  vendor with sub-minute quote resolution is wired in place of Kraken's 5-min OHLC, or (b)
  the calibration signal-statistics honing (see follow-up section above) produces asset-
  class-specific FX thresholds materially looser than the current calibrator defaults.
  Deep-fetched FX parquets stay on disk for reuse (`data/cache/EUR{GBP,CAD,CHF,JPY}_{daily,
  trades}.parquet`). Calibration's `fx` asset-class defaults remain in place — they're
  generic, not sleeve-specific.

- **FX pair-trading: NOT tradeable in this framework (2026-09-04, explicit user decision).**
  Deep parquets built for the 4 EUR-cross legs (2100-2400 bars each, ~5.9-6.5y daily); pairs
  strategy WF'd against three grid iterations — original (window 20-60, entry_z 1.5-2.5), first
  widening (up to window 180, entry_z 1.2), and FX-oriented sub-1σ (entry_z 0.1-0.75). Widest
  grid produced 2-4 OOS trades vs 0-1 on the tighter grids, but every additional trade LOST
  money. Empirical conclusion: cost-drag on retail Kraken-fill assumptions (5-15 bp per round-
  trip) is 25-75% of typical FX daily-cross excursion, so the small mean-reversion edges don't
  survive. Hourly bars would likely make it worse (excursions shrink by √t, cost unchanged).
  **Do not re-propose FX pair-trading in the WF-validated pool** unless (a) a professional-grade
  cost model is wired (spread <1 pip) or (b) `KalmanPairs` (adaptive hedge) is tried in place
  of the current fixed-hedge crossing-trigger strategy. Full reports in
  `reports/fx_pairs_wf_2026-09-04-*.md`. FX deep parquets stay on disk for later reuse.
- **Excluded from selection (2026-09-04, explicit user decision).** The 2026-09-04 overnight WF
  produced 3 robust survivors from the ETF-proxy set — `LQD` (IG corporate bonds), `MUB` (US
  munis), `DBA` (agricultural commodities). The user explicitly rejected adopting these into
  `WALK_FORWARD_VALIDATED` and asked they be omitted from selection. Do NOT re-propose them in
  a future session unless the user brings them back. Full WF results still live at
  `reports/wf_symbols_proxies_2026-09-04.{csv,md}` for reference.
- **Futures continuous-contract pipeline via ib_insync socket API (Alt B).** Queued 2026-09-04
  after Phase 1 probe against CP Gateway proved IB Web does NOT expose expired-contract data at
  all (every `/iserver/secdef/*` path returns "No Contracts retrieved" for historical months;
  `/trsrv/futures` returns forward contracts only). IB's socket API (TWS or IB Gateway binary
  + `ib_insync.IB.reqHistoricalData` with an expired Contract) does support historical bars —
  the Web API is a REST subset that omits it. Path: install TWS/IB Gateway, enable API, rewrite
  `data/ib_futures_history.py` to use `ib_insync` for the per-contract fetch. Phases 2 (calendar)
  and 4 (continuous-series builder) already built and data-source-agnostic — they can be reused
  as-is. Files landed this session: `src/trading_live_claude/data/futures_calendar.py` and
  `src/trading_live_claude/data/futures_continuous.py`. Estimate: 3 hrs (install + rewrite Phase
  3 + smoke test end-to-end on ES).
- **IB sweep — execute the run.** `scripts/sweep_ib.py` is built and CLI-tested. Needs CP
  Gateway auth'd (`https://localhost:5000` browser login) to actually fetch. One command:
  `python scripts/sweep_ib.py`. Writes `reports/ib_sweep_YYYY-MM-DD.{csv,md}`. Bounded work,
  under 10 minutes wall-time for the default universe (~30 ETFs + 5 futures).
- **Item 0 — full resweep** — needs ~50 min of compute after cache warm. Runbook already in item 0
  below. Nothing to code; just run when the block window allows.
- **Item 8 execution — walk_forward_pairs.py** built (2026-09-04, uncommitted). Reads the latest
  `fx_pairs_scan_*.csv`, filters tradeable rows, fetches both legs via `kraken_ohlc`, walks
  forward the PairsZScore grid under the FX protocol (train=504 / test=126 / step=126) with a
  36-combo per-fold search, tiers by the same equity bar (WFE >= 0.5, OOS > 0, >= 10 trades).
  Needs `fx_pairs_scan.py` run first to produce the shortlist. Bounded work (~15s per pair).

**Held (built but off):** the LLM agent layer (`intel/agents.py`, `enrich_with_agents`) stays
inert without `ANTHROPIC_API_KEY` in `.env` and no caller runs it. See item 6.

**Closed in-session (2026-09-04):**
- **IB-paper feedback-loop gap #1 — Alerter wiring on paper_ib.py + paper_kraken.py.** Both
  scripts now build an `Alerter` (same shape as the QT CLI's `_build_alerter`), format entries /
  exits via `intel.notification`, and push them to Telegram + email + stdout on every fill.
  Empty creds fall back to stdout-only so the venue works with or without `.env` keys. IB's
  tickle-thread 20h/23h escalation is now routed through the same `Alerter`, so the queued
  wire from gap #6 is closed too.
- **IB-paper feedback-loop gap #2 — PortfolioAllocator on paper_ib.py.** Mirrors the paper_kraken
  wiring: builds a returns matrix from ~2y daily bars via MarketData (IB → cache), scores each
  name by past-year Sharpe as a screen-score proxy, runs `PortfolioAllocator(max_weight=0.30)`,
  and passes the resulting bias map as `weight_bias_for` on the LiveMonitor.
- **IB sweep script built** — `scripts/sweep_ib.py`. Executes CP-Gateway-side (needs auth); one
  pass over a bond / commodity / precious-metals ETF + FUT front-month universe, computing base
  stats + overlay scalar + interpret matches per symbol. Writes CSV + compact markdown to
  `reports/ib_sweep_<tag>.{csv,md}`. Queued execution moved to the "unfixed" list above.
- **Item 7 — Crypto WF shallow fallback.** `scripts/walk_forward_crypto.py` now falls back to
  `kraken_ohlc(pair, interval=1440)` when the deep-history parquet is absent. Shallow-derived
  rows report tier=`screened+` (better than pure in-sample, thinner than deep-history WF; never
  `robust`). `--no-shallow-fallback` disables to keep the old strict behavior.
- **Item 8 — FX pair-trading discovery MVP.** `scripts/fx_pairs_scan.py` fetches shallow daily
  OHLC for Kraken's most-liquid fiat FX pairs, enumerates `C(n, 2)` combinations, runs Engle-
  Granger cointegration via the existing `analysis/pairs.py::enumerate_pairs`, and reports the
  tradeable shortlist (cointegrated + finite half-life). FX-tuned defaults (`max_half_life=60d`
  vs. 252d for equity) reflect faster mean-reversion. Discovery only — walk-forward wrapper for
  the shortlist is still queued.

**Closed in-session (2026-09-03, later pass):**
- **IB-paper feedback-loop gap #4 — fixed_income + precious_metals classes.** `OverlayClass`
  Literal + `OVERLAY_CLASSES` tuple extended in `intel/overlay.py`; `_compose` grows two new
  branches with class-appropriate risk-off character (bonds get lightly-blended global + economy +
  conflict gates because flight-to-quality usually rallies duration; metals get the full dxy gate
  plus lightly-blended conflict/disaster because they're safe-haven with mild systemic squeeze
  risk). `intel/routing.py` grows `_FIXED_INCOME_SYMBOLS` (TLT/IEF/SHY/BND/AGG/LQD/HYG + XBB.TO/
  ZAG.TO/VAB.TO) and `_PRECIOUS_METALS_SYMBOLS` (GLD/SLV/PSLV/CGL.TO/…), and `classify_symbol`
  now checks them BEFORE the broad-commodity list. Bond ETFs on IB paper no longer inherit the
  equity scalar; metals no longer bucket with oil.
- **IB-paper feedback-loop gap #6 — CP Gateway 24h re-auth warning.** `_TickleThread` in
  `scripts/paper_ib.py` now tracks wall-clock session age and escalates once each at 20h (WARN)
  and 23h (CRITICAL). Any tickle failure whose response looks like an auth expiry (401/403/
  unauthorized) also fires the CRITICAL once. Warnings are plumbed through a `warn_fn` callable
  so when the queued Alerter wiring (gap #1) lands, threading through to Telegram is one line.
- **IB-native futures wire-up.** `IBWebBroker.set_sec_type(symbol, sec_type)` registers a
  per-symbol override; `resolve_conid` routes FUT-type resolutions through `/trsrv/futures`
  (which returns per-expiration conids, unlike `/iserver/secdef/search` which only returns
  roots), picking the front-month = earliest expirationDate strictly after today with a
  fallback to earliest-of-stale if every listed contract already expired. `scripts/paper_ib.py`
  grows `--futures ES,NQ,CL,GC,ZN` which registers each root as FUT before the monitor's first
  quote call. 5 new respx-mocked tests covering front-month pick, stale fallback, unknown root,
  cache invalidation on override, and default STK behavior — all green.
- **Cross-path tier 3 — Graph persistence → entry gate.** `intel/routing.py` grows
  `_CLASS_TO_DOMAINS` (a mapping from overlay class to the graph domains that class is exposed
  to) and `PersistenceGate` (callable, refreshes on cadence, returns `(halt, reason)`).
  `LiveMonitor` grows a `persistence_for` hook mirroring `overlay_for` — an entry in a symbol
  whose class touches a persistently-elevated domain (≥ `min_polls` consecutive polls, default 5)
  is halted (router skipped) with the halt reason landing on the alert. Fail-open on every
  failure path: missing graph, parse errors, refresh exceptions all resolve to "no halt" so a
  broken intel path never causes an unexpected trading halt. 11 focused tests in
  `test_intel_routing.py`.

**Closed in-session (2026-09-03, earlier pass):**
- IB-paper feedback-loop gap #3 — `venue` tag on every PaperBroker journal row. `.venue`
  class-attr on every real broker (`questrade`, `kraken`, `ib`, `ib_web`); `PaperBroker.__init__`
  inherits from the feed and accepts an explicit `venue=` override. `paper_fills.jsonl` and
  `paper_orders.jsonl` now carry the venue on every row so the shared journal is groupable by
  dashboard. Equity CSV schema deliberately unchanged (join by `session_id` to derive venue,
  avoiding a schema break in `state/paper_equity.csv`). Tests in `tests/test_paper_broker_journal.py`.
- IB-paper feedback-loop gap #5 — Fills → intel graph event nodes. Extended `intel.graph` with
  `venue`/`symbol` node types and a `traded` predicate; new `fill_edge(...)` helper composes one
  edge per fill (weight = signed notional; meta carries action/qty/price/session_id/order_id).
  `PaperBroker._journal_fill` appends the edge via `append_edges(...)` in the same call site as
  `paper_fills.jsonl`, in a try/except so a graph-write failure never crashes a trade path.
  Closes what was cross-path tier 2 in the earlier audit — `edge_persistence` can now see fills.

---

## Data-first sequencing — 2026-09-08 standing rule

Explicit ordering for the currencies + equities sleeves: **accrue data before you tune,
promote, or wire new gates**. Applies to every calibration, promotion, or microstructure-
based rule proposed today or later. Concrete corollaries:

* Walk-forward runs (like today's 13-pair crypto WF) serve as PROTOCOL VALIDATION —
  confirming the pipeline scores across the sleeve — NOT as tier-promotion triggers.
  Do not promote a pair to `tier=robust` on a single WF pass; require deep-history
  evidence + multi-fold OOS stability.
* Do not calibrate signal-statistics (Bollinger n_std, ZScore entry_z, RSI oversold)
  until deep-history is available across the sleeve — thresholds tuned on shallow
  720-bar windows over-fit to the current regime.
* Do not wire microstructure-based gates (LiquidityGate, liquidity-heat trims) until
  the accumulator has ≥90 days of density.
* No new autonomy or scheduled tasks in the interim beyond what's already running;
  keep the loop human-in-the-loop while the data corpus deepens.

## Crypto WF protocol — 2026-09-08 validated, promotion held  🟢 PIPELINE OK

Today's `scripts/walk_forward_crypto.py` run scored all 13 currencies-sleeve pairs
cleanly (2 deep / 11 shallow) — pipeline works, tier fields NOT updated per the
data-first rule above. Purpose was protocol validation; outputs recorded to
`reports/walk_forward_crypto.csv` for later cross-reference once deep history lands.

**Diagnostic-only takeaways** (not action items):
* 11 of 13 pairs currently on shallow (~720 bar) Kraken /public/OHLC — WF gives them
  ~3.9 folds and 0-3 OOS trades most sleeves. Insufficient sample for tiering.
* Deep-history-backed pairs (PAXG, BTC) had enough sample to produce meaningful OOS
  numbers but tier assignment held pending broader corroboration.
* Zero-trade rows (SOL/POL/ADA/ZEC/XMR) indicate strategy thresholds probably
  mistuned for those pairs' vol distributions — DO NOT hone thresholds now; wait for
  deep data to confirm the pattern.

## Deep-history fetch (currencies sleeve) — 2026-09-08  🟡 QUEUED

Prerequisite for the crypto WF protocol to produce actionable tier decisions. Current
state: only PAXG + BTC have deep parquets under `data/cache/`. Other 11 pairs run on
shallow 720-bar Kraken fetches.

**Priority order** (deepen the diversifiers + cluster cores first, then the rest):
1. **ETH/USD** — cluster core, missing from deep cache. `python scripts/fetch_crypto_history.py --pair ETHUSD --since 2020 --max-pages 15000`
2. **XMR/USD** — partial diversifier (avg |ρ| 0.39). Same command form, --pair XMRUSD.
3. **ZEC/USD** — genuine diversifier per correlation study (avg |ρ| 0.41). --pair ZECUSD.
4. **LINK/USD** — cluster; the atr_channel strategy shows some signal on shallow. --pair LINKUSD.
5. Remaining cluster: **XRP, XLM, SOL, ADA, POL, UNI, AAVE, MKR** — batch after 1-4.

Each fetch is multi-hour (Kraken /public/Trades pagination at ~1 req/s, ~1000
trades/page). Total wall time for all 13 pairs likely 12-24 hours if run sequentially.
Parallelization risky (Kraken rate limits per API key, not per pair).

Prior 2026-09-04 orchestrator (`scripts/targeted_orchestrator.py`) can be adapted;
the CRYPTO_SLEEVE-driven loop already iterates over `sleeve.values()`.

**Blocker for auto-run:** wall-time + Kraken rate discipline. Recommend a scheduled
overnight run (~03:00 local start) rather than a foreground session. Per the standing
`no-unattended-automation-without-consent` rule, ASK before registering the scheduled
task.

## OSINT × commodity-proxy correlation study — DESIGNED, PARKED  🟡 WAITING ON DATA DEPTH

Designed 2026-09-05, parked same-session on window mismatch. The regression spec is ready
to run; the data corpus is not deep enough for it to produce non-noise results yet.

**Research question:** does OSINT domain elevation (from the 15-min graph poll) systematically
precede forward returns in commodity/futures ETF proxies at 1/5/21-day horizons?

**Regression spec (frozen for later):**
* Response: `log(close_{i, t+h} / close_{i, t})` for h ∈ {1, 5, 21}
* Features per domain d ∈ {energy_stress, conflict, natural_disasters, dxy, safe_haven,
  economy, financial_stress}: raw scalar `s_{d,t}`, 90-day-trailing z-score
  `elev_{d,t}`, consecutive-poll persistence count `pers_{d,t}` (via existing
  `edge_persistence`).
* Controls per proxy i: 21-day momentum `mom_{i,t}`, 21-day realized vol `rv_{i,t}`.
* OLS + Newey-West SE (h-lag overlap induces serial correlation).
* Benjamini-Hochberg FDR at α=0.10 across the 14 × 3 × 7 = 294 individual γ tests.
* Cadence alignment: EOD OSINT aggregate (last poll before 16:00 ET), daily ETF close.

**Symbol list (14 ETF proxies):** USO/UNG (energy), GLD/SLV/PPLT/CPER (metals),
WEAT/CORN/SOYB (grains), TLT/IEF/HYG (rates/credit), UUP (dollar), VXX (vol).

**Blocker:** intel journal too shallow.
* `state/intel_overlay.jsonl` — 143 rows, 2026-08-29 → 2026-09-05 (7 days).
* `state/intel_graph.jsonl` — 8974 edges, 2026-09-01 → 2026-09-05 (4 days).
* No backfill path (WorldMonitorClient is live-only; can't retroactively query historical
  DXY_chg or category_alert_counts).
* 5-day and 21-day horizons: essentially zero observations.
* 1-day horizon: ~7 obs per proxy (98 pooled) against 9-10 parameters — over-parameterized.

**Revisit criteria:** intel journal spans ≥ 3 months (~60 trading days per proxy → ~10:1
obs:param at the 1-day horizon; supports pooled cross-sectional OLS). ≥ 6 months for the
5-day horizon to be defensible; ≥ 12 months for 21-day. So earliest honest revisit is
~2026-Dec (3 mo from now), full-scope revisit is ~2027-Mar.

**Available today without waiting:** ETF proxy historical bars are already cachable via
IB Web STK path — the fetch layer is not the blocker. Only the OSINT feature side is.
Anyone who wants to work on this before the intel corpus deepens should build ONLY the
proxy-side data pipeline + the regression harness (so ~2026-Dec re-run is one command),
NOT run the regression itself with the shallow corpus dressed as findings.

**Related standing decision:** same window-depth logic applies to any other cross-signal
study on the intel graph — calibration signal-statistics honing (section above), thesis-
calibration recurrent-learning loop (item 9 tier 4), prediction evaluation (item 9 tier 5).
All four studies wait on the same underlying corpus.

## Symbol-mapping architecture — 2026-09-08  🟢 ALL THREE GAPS LANDED; sleeve migration deferred

**Status 2026-11-01.** Gap 1: `analysis/symbol_validation.py`, wired into `signal` and `paper_kraken.py`,
and now `paper_ib.py`. Gap 3: `brokers/ib.py::_infer_stock_venue`, landed 2026-09-09. Gap 2:
`analysis/symbol_atlas.py` (canonical ids, per-broker wire forms, `desk_route`, `validate_atlas`). It
reads the adapters and sleeve tables rather than copying them. Not done: moving sleeve entries onto
`canonical_id` and absorbing the `asset_spec` registries, which can wait for a real cross-venue trade.
Note `_infer_stock_venue` still falls back to SMART/USD for an unknown suffix (`.T`, `.HK`, `.DE`);
fix that before exchange hopping level 2. Original scope follows.

Surfaced from a mid-session audit of how the same asset is represented across brokerages.
Each broker adapter normalizes independently (Questrade `_symbol_id()`, Kraken
`to_kraken_pair()`, IB Web `_resolve_stk_conid()`, IB socket `Stock(sym, "SMART", "USD")`)
and the internal codebase uses `analysis.asset_spec.spec_for(symbol)` as canonical
identity — functional for single-broker-per-sleeve, but three gaps have real cost.

### Gap 1: Pre-flight symbol validation (small, high-value)
Both landmines from this week's paper sessions surfaced only at runtime — ARX.TO 404'ing
on Questrade candles across 5+ polls, MKR/USD raising `EQuery:Invalid asset pair` mid-
poll and killing the whole Kraken session via structlog cascade. There is no `validate_
sleeve(broker) -> list[SymbolValidation]` helper that walks every sleeve entry at startup,
attempts a minimal fetch (symbol/search + one bar of candles), and returns per-symbol
status BEFORE the monitor's first poll. ~30 lines to build; would have caught both this
week's failures pre-launch instead of mid-run. Wire into `scripts/paper_kraken.py` and
`cli.py signal` startup banners; refuse to start the loop if any HARD failure (candle
404, invalid pair). WARN on soft failures (e.g., short cached history).

### Gap 2: Cross-broker symbol atlas
No shared table maps the same canonical asset across brokerages. If a Kraken-spot vs
IB-futures basis trade is proposed (or any Kraken spot vs IBIT ETF cross-venue), each
broker needs its own sleeve entry and nothing relates them. `CryptoSleeveEntry` today
carries `.symbol` (routed) + `.pair` (Kraken REST), which is a two-form pattern for
one broker; extending to N brokers grows quadratically without a first-class atlas.

Proposed module `analysis/symbol_atlas.py`:

```
@dataclass(frozen=True, kw_only=True)
class BrokerSymbol:
    canonical_id: str            # "BTC-USD-SPOT" — venue-agnostic identity
    broker: Literal["questrade","kraken","ib_web","ib_socket"]
    wire_form: str               # what the broker's API expects on the wire
    sec_type: str = "STK"        # STK / FUT / CASH / CRYPTO
    meta: dict = field(default_factory=dict)

_ATLAS: dict[str, list[BrokerSymbol]] = { ... }

def resolve(canonical_id: str, broker: str) -> BrokerSymbol | None
def validate_atlas(broker: Broker) -> list[SymbolValidation]
```

Migration path: sleeve entries stop carrying dual-form fields; they carry a
`canonical_id` and the atlas resolves per-broker. Backwards-compatible via a shim on
`CryptoSleeveEntry`. Would also absorb the current `_BOND_REGISTRY` / `_METALS_REGISTRY`
/ `_FUTURES_REGISTRY` scaffolding in `analysis/asset_spec.py` — those are effectively
per-class atlases already, just single-broker.

### Gap 3: IB socket path silent mis-routing
`brokers/ib.py:354` hardcodes `Stock(order.symbol, "SMART", "USD")` for equities.
Canadian ETFs (`XIC.TO`, `VDY.TO`) submitted via socket would be silently mis-routed
(SMART routes US-listed venues only; USD currency wrong for TSX). IB Web adapter fixed
this via `set_sec_type()` override + `.TO` suffix strip; socket path never got parity.

Fix (short): port `_resolve_stk_conid()`'s exchange + currency inference into a helper
called by `brokers/ib.py::place_order`. Preserves the socket adapter's operational
independence but removes the mis-routing hazard. ~40 lines + tests.

### Sequencing
1 (validation preflight — ~30 min) → 3 (IB socket exchange fix — ~1 hr) → 2 (full atlas
+ migration — ~4-6 hr, only if cross-venue trades get proposed). Gap 1 alone would have
prevented both this week's mid-session failures; do it first, atlas can wait.

## Risk-guard validation analysis — 2026-09-09  🟢 SCRIPT BUILT; 🟡 DATA-BLOCKED (needs ≥ 1 week post-gate data)

**Status 2026-11-01.** `scripts/risk_guard_analysis.py` writes the report and refuses to recommend on
too little record. Today's state holds one order and one fill from one four-hour session on 2026-10-31,
because the journals did not survive the repo reconstruction, so no threshold can be judged. Rerun
after a week of paper sessions on two venues. Reject strings alone undercount the size caps since trim
mode: the script also counts ledger `RISK_TRIMMED` rows.

Reading the wiring found what no journal could show:
* **Gross-leverage cap was inert.** No runner passed `current_open_notional`, so the gate compared each
  intent to equity alone. FIXED: the monitor, the card path and `place-order` pass it, and it counts
  entries made earlier in the same poll, as do the heat and open-positions gates.
* **`check_forced_exits` had no caller.** The "landed" item 3 could never fire. WIRED, opt-in:
  `forced_exits: true`, off by default. Writes a `FORCED_EXIT` ledger row.
* **A tripped kill-switch rejected exits too.** DECIDED and FIXED 2026-11-01: an order that only reduces
  a position the routed broker holds now passes. A SELL with no position behind it stays refused, since
  it could open a short, and so does anything when the holding cannot be read.
* **No auto-halt on live brokers.** DECIDED and FIXED 2026-11-01: `risk/equity_watch.py` gives live and
  autonomous what `PaperBroker._journal_equity` gives paper, journaling to `state/live_equity.csv`. The
  peak comes back from the journal and restarts at a human clear. A withdrawal reads as a loss.
* **`trading.yaml` risk knobs were ignored.** No runner passed `max_gross_leverage`,
  `max_position_notional_pct`, `force_exit_atr_mult` or `on_size_cap_breach` to the Router, so only their
  defaults ever applied. FIXED: every runner passes `Router.limits_from(settings)`.
* **Other gates refused exits too.** DECIDED and FIXED 2026-11-01: the heat cap refused an exit from a
  book already over it, the min-ticket gate refused closing a small remainder, and the autonomous
  daily budget refused exits once spent. A reducing order now passes all three, by the same test the
  kill-switch uses. Still applied to an exit: `equity <= 0` and the stop-side check. A plain BUY that
  covers a short also still meets the entry-only gates (basket, open-positions, size caps, spectral).

`trading live` now mirrors the paper runners (`tests/test_live_parity.py`): pre-flight, alerts,
`--strategy-map`, `--require-card`, `--require-basket`, the spectral gate, and the same `trading.yaml`
limits. It differs where it has to: real broker, typed confirmation, strict ledger. Not carried over:
`--hedge`, which only `signal` has and which would place real UUP orders. The intel hooks are being
wired by another session.

Original scope follows.

Empirical audit of items 1/2/3/5 from the 2026-09-08 risk-architecture landing (commit
`629594c`). Purpose: verify each new gate is actually firing as designed, calibrate
thresholds against the observed fill/reject distribution, and surface false-positive vs
false-negative behavior. Item 4 (strategy-level opt-in stops) intentionally excluded —
it's data-blocked pending WF and has no code to audit.

**Data sources (all already journalled):**
* `state/paper_orders.jsonl` — accept/reject decisions with `rejected_reasons` list.
  Query for the 4 new reason strings: `"single-name notional"`, `"gross leverage"`,
  `"kill-switch tripped"`, and forced-exit reasons (in fill journal, not orders).
* `state/paper_fills.jsonl` — records `forced-exit` reasons on the intent side when
  Router.check_forced_exits emits them.
* `state/paper_equity.csv` — post-fill drawdown_pct series feeds the kill-switch
  eval; a KillSwitch trip corresponds to `state/HALTED` appearing in a given session's
  state_dir.

**Per-gate probes:**

1. **Kill-switch tighten + auto-halt (item 1)** — count HALTED sentinels per session,
   correlate with the equity trajectory that triggered them. Compare pre-2026-09-08
   sessions (pre-tightening) vs post — did the 3% threshold trip on sessions the 8%
   threshold would have let ride? Cross-reference against the subsequent MTM: would the
   halted position have recovered? Rough false-positive metric.
2. **Per-symbol notional cap 50% (item 2)** — count `"single-name notional"` rejections
   per session. Retrospect: which name/strategy triggered the cap? Would the intent's
   size have been the trade that hit the leverage cap (the VDY.TO failure mode)? Cap
   binding rate = rejections / total-accepted-entries; want this in the 1-5% range
   (frequent enough to be doing work, rare enough not to strangle the sleeve).
3. **Intra-day forced exit (item 3)** — count forced-exit fills per session, correlate
   with entry-to-exit unrealized loss trajectory. Empirical calibration of
   `force_exit_atr_mult`: at 3.0, is the gate firing on noise (position would have
   recovered) or on real breakdowns (position kept falling)? Sweep candidate values
   {2.0, 2.5, 3.0, 3.5, 4.0} on the accrued fill history.
5. **Portfolio gross-leverage cap 1.0× (item 5)** — count `"gross leverage"` rejections;
   verify the sleeve's max observed gross leverage stays ≤ 1.0. Distinguish from the
   per-symbol cap — leverage cap binds when MULTIPLE positions cumulatively hit the
   ceiling, per-symbol binds on a SINGLE dominant position.

**Cross-gate interactions to watch for:**
* Kill-switch trip triggered by a position the per-symbol cap should have prevented
  from opening → indicates the size cap default is too loose.
* Forced-exit gate firing on positions that were sized within all pre-entry caps →
  indicates the entry gates missed something the exit gate caught (good — defense in
  depth working).
* Same position rejected by BOTH per-symbol AND gross-leverage → indicates the two
  gates are collinear at current defaults; may want to relax one.

**Deliverable:** `reports/risk_guard_analysis_YYYY-MM-DD.md` with per-gate firing rates,
threshold-sensitivity tables (esp. force_exit_atr_mult sweep), and any calibration
recommendations. Uses `state/paper_orders.jsonl` + `paper_fills.jsonl` + `paper_equity.csv`
— no fresh fetches needed.

**Data-window requirement:** at least **1 week of post-2026-09-08 sessions** running both
QT + Kraken paper. Today (2026-09-09) is day 1; earliest honest run is **~2026-09-16**.
Longer window (2-4 weeks) improves the false-positive/false-negative separation. Same
data-first discipline as the OSINT and thesis-calibration studies.

**Not blocked by:** intel corpus depth (the analysis uses paper-broker journals, not intel
graph). Independent of the microstructure accumulator + LiquidityGate build path.

## Risk-architecture follow-ups — 2026-09-08  🟢 4 OF 5 LANDED (item 4 data-blocked)

Prompted by a QT paper session where VDY.TO at ts_momentum × 2.33× allocator boost hit the
vol-target max_leverage=1.0 cap and took 100% of paper equity ($100,044 notional on
$99,995 equity). Loss reached −$416 (~0.42%) with no gate firing — well below the 8.0%
kill-switch and 3.0% daily-loss limit, and no strategy-level stop configured on
ts_momentum. Four separate follow-ups:

### 1. Tighten max-drawdown kill-switch: 8.0% → 3.0%  ✅ LANDED 2026-09-08
`config/trading.yaml::max_drawdown_kill_switch = 0.03`. The current 8.0% threshold is
loose for a paper-validation context — by the time it trips the account has already lost
$8k. 3.0% gives the same margin against real-world overnight-gap noise (~2σ on a broad
equity book) but halts before catastrophic runaway. Router already reads this from
settings so no code change; single config edit + relaunch.

### 2. Dynamically-weighted max_position_notional_pct gate per symbol  ✅ LANDED 2026-09-08 (fixed 0.50 default; vol-weighted variant deferred)
Add a router gate that caps single-symbol notional as a percent of equity, weighted by
the position's own risk contribution (not just size). Shape:

* Per-symbol cap defaults to `1 / max_open_positions` × 1.5 (~50% for the current
  max_open_positions=3), acting as a hard ceiling.
* Weighted by (annual_vol_i / mean_annual_vol_sleeve) so a low-vol name (VDY, PAXG)
  can hold a larger absolute notional than a high-vol name (VALE, SOL) — the point is
  equal risk contribution, not equal weight.
* Runs as a Router gate alongside heat_cap / max_open_positions / daily_loss_limit.
  Rejects the intent (or trims the size to the cap) when a fill would push notional
  above the weighted ceiling.
* Cross-checks against `PortfolioAllocator.max_weight` (0.30 sleeve-level cap) — the
  new gate is per-name inside a sleeve, catches the "one boosted name saturates the
  leverage cap and takes 100% of equity" failure mode the sleeve-level cap misses.

### 3. Move exit checks to intra-day bar cadence  ✅ CHEAP PATH LANDED 2026-09-08 (Router.check_forced_exits); real path (intra-day bar strategy hook) deferred
ts_momentum (and other daily-bar strategies) currently check `generate_signals` on the
daily close only, so a −0.4% intra-day drawdown is invisible until the next EOD bar.
Two paths to fix:

* **Cheap:** router adds a "loss-based exit" gate — if any open position's unrealized
  loss exceeds N × ATR since entry, force-close on the next intent. Runs on the
  monitor's poll cadence (5-min), independent of the strategy's bar.
* **Real:** strategies opt into an intra-day recheck flag; the monitor calls a slim
  `should_exit_intraday(bar, position) -> bool` on every poll, backed by a 1h or 30m
  bar instead of daily. Bigger change; needs the interval-standardization work (already
  landed 2026-09-05) plus a broker-side intra-day fetch pipeline.

Recommend starting with the cheap path (router-level unrealized-loss exit) since it
protects every strategy uniformly and doesn't require per-strategy retrofits.

### 4. Configure strategy-level risk-stops on the exit-less strategies  🟡 DATA-BLOCKED (needs WF)
`strategies.base.Strategy` supports opt-in `stop_atr_mult` / `trail_atr_mult` /
`time_stop_bars` but every strategy in the sleeve except `bollinger` (time_stop_bars=15)
and `candlestick` (stop_atr_mult=3.0) leaves them at `None`. Concrete assignments to add
after walk-forward evidence supports them:

* `ts_momentum` — add `trail_atr_mult=4.0` (Chandelier trailing stop, standard for
  momentum). Chosen over a fixed stop because momentum needs room for pullbacks; ATR
  scales that room by the name's own volatility.
* `rsi_meanrevert` / `bb_rsi_combo` / `zscore_ou` — add `time_stop_bars=20`. A dip
  that hasn't reverted in 20 bars is a stale thesis; force-close and free the capital.
* `atr_channel` / `macd` / `ema_crossover` — add `stop_atr_mult=3.0`. Trend-following
  wants a hard floor; the trailing-stop version (`trail_atr_mult`) is only appropriate
  once WF evidence confirms the strategy captures durable trends.

Each assignment must clear a walk-forward run before landing — an added stop that hurts
the OOS score is a bad trade for peace of mind. Bounded work: rerun tune per strategy
family, keep the assignment only if sortino_over_dd improves.

### 5. Portfolio cash-balance / gross-leverage gate on the router  ✅ LANDED 2026-09-08

### 6. Trim-instead-of-reject on the size-cap gates  🟡 CODE-COMPLETE 2026-09-09, NOT EMPIRICALLY VALIDATED
Behavior change: both size caps (per-symbol + gross-leverage) resize `intent.shares` to
fit the tighter cap and accept, controlled by `on_size_cap_breach: 'trim' (default) |
'reject'` on Router + settings. Falls through to reject only when no headroom remains
OR when trimmed size falls below `min_ticket_usd`.

**What actually got validated today:** the OLD reject-mode gate fired 39x on VDY.TO in
a single QT paper session at 100%-of-equity notional (correcting a false claim earlier
in the day that gates were idle). That validated the ORIGINAL 2026-09-08 gate was
binding correctly.

**What did NOT get validated today:** the new trim mode's runtime behavior. Written +
unit-tested (5 tests, all passing) but not exercised against a live paper session
until the next launch. Do NOT claim "trim prevents the 39-attempt loop" as empirical
until at least one paper session shows the actual trim → accept → position-open
sequence in `paper_fills.jsonl`.

Old reject-mode tests preserved via explicit `on_size_cap_breach="reject"`.

### 7. Alerter dedup — OMITTED 2026-09-09 per user decision.
Do NOT propose changes to the Telegram alerter. The Alerter stays as it currently is.

### 9. Exchange hopping — trading beyond user's geographical timezone  🟢 L1 LANDED 2026-11-01; L2-4 need decisions

**Status 2026-11-01.** Level 1: `analysis/venue_calendar.py`, on in every runner through
`monitor_policy` (`respect_market_hours`). Hours only, no holiday calendar: a holiday reads as open.
A held position on a closed venue is still quoted once per poll for the heat figures. Level 2 needs a
market-data spend decision and level 3 a numeraire choice; both are yours.

**Levels approved but not built** (queued 2026-09-09). Applies the propagation-notes
discipline: each level's cross-module impact is enumerated so we don't slip an
architectural change in as a small feature.

**Level 1 — Session-hours guard (~2 hr, next step).**
* New `analysis/venue_calendar.py` with market-hours-per-venue + `is_open(venue, ts)`.
* LiveMonitor consults the guard before polling each symbol; symbols whose venue is
  currently closed get skipped, preventing wasteful broker calls + stale-quote signal
  errors during off-hours.
* No new trading enabled — foundation only. QT + Kraken behavior unchanged during
  their live sessions.
* **Propagation:** LiveMonitor.step() adds one gate. Broker adapters get a
  `.venue_calendar` attribute (or free function). No state schema change. Kill-switch
  + heat gates unaffected. Alerter behavior unchanged.
* **Cadence effect:** polls for closed-venue symbols drop from every 5min to zero
  during their off-hours; net API-call reduction on the current pool.

**Level 2 — Multi-venue equity via IB (~6-8 hr + WF).**
* Add Tokyo / London / Sydney / etc. equities to `WALK_FORWARD_VALIDATED`. IB socket
  path's `_infer_stock_venue` already handles suffix routing (landed 2026-09-09).
* Requires IB Gateway + global-market-data subscriptions (real account cost decision).
* WF runs per new symbol before promotion.
* **Propagation:** no code deltas beyond L1 for the routing itself; the additions
  are data (WF pool). Intel overlay's US-centric OSINT feed does NOT surface Asia
  events well — a Nikkei trade uses `equity` class scalar without any Asia-specific
  intel signal. Document that gap when adding.

**Level 3 — Multi-currency accounting (~8-12 hr).**
* PaperBroker records position currency. Equity CSV adds `numeraire_equity` column
  with FX-adjusted totals. FX rates from IB or Kraken's fiat pairs.
* All risk gates switch to numeraire-based math (KillSwitch, PortfolioHeat, allocator
  correlation matrix).
* **Propagation — critical.** FX rate moves become a NEW risk vector: a position
  flat in native currency can trip the drawdown gate through FX alone. The KillSwitch
  daily-loss baseline needs an explicit numeraire choice (USD is conventional but
  CAD may be the user's home currency). Correlation math is only sound in a common
  numeraire — mixed-currency return series bias the covariance matrix by the FX
  volatility of each pair.
* Feedback loop: FX-adjusted equity → KillSwitch.evaluate → potential HALT even when
  book is fine in native currency. Must be tested with an FX-shock scenario before
  live.

**Level 4 — 24-hr scheduler + non-overlapping correlation (~15-20 hr, speculative).**
* True global book, session-aware intent scheduling, correlation smoothing across
  venues with different close times (Kalman filter or overlapping-window rebase).
* Only worth designing after L2 + L3 have live evidence — this level is a
  hypothesis, not a build spec.

**Sequencing gate:** Levels 2-4 depend on real IB account market-data subscriptions
(spend decision) AND on the L3 numeraire choice (architecture decision). L1 is
bounded and independent — worth doing regardless of the higher levels.

### 8. Event + level entry triggers (both, decoupled)  ✅ LANDED 2026-11-01

**Status 2026-11-01.** Columns: `entry_level` on ema_crossover, macd, rsi_meanrevert, zscore_ou, pairs,
kalman_pairs, arima_garch, turn_of_month and month_of_year, with a lookahead test; a strategy whose
`entry` already is a state has nothing to add. Default stays event; bollinger is the one family that
acts on level unasked; `entry_triggers` in `trading.yaml` sets it per symbol. Tight guard: an order not
yet visible as a position blocks a repeat entry, and a level-only entry routes once per bar, so a
rejected or declined one retries next bar rather than every poll. No level setting is in use yet, and
none has been walk-forward tested: the level path has no backtest. Original scope follows.
User standing rule: we need BOTH event-based (fresh cross) AND level-based (state
currently satisfied) entry triggers — neither one alone is reliable enough.

Failure modes each covers for the other:
* **Event-only fails when:** the monitor starts after the fresh cross has already
  happened; OR the first-fired intent is gate-rejected (e.g., size-cap trim now
  handles this on-line, but there are other reject reasons where the position never
  opens); OR the fresh-cross bar is skipped for any polling reason. Result: no entry
  until the NEXT cross, which may never come for many bars.
* **Level-only fails when:** the current level is persistent for many polls in a row
  (would re-fire indefinitely, requiring open-position checks to prevent re-entry);
  OR the level is a one-bar spike that closed level-eligible even though the intent
  was "trigger at the crossing".

Contract shape:
* Each strategy emits BOTH `entry_event` (0/1 on fresh cross) AND `entry_level`
  (0/1 while state currently satisfies condition). Strategy authors pick which apply
  to their setup — momentum crossings are event; band-touch dips are level.
* LiveMonitor consumes both columns; per-symbol config picks which to act on
  (default: event; opt-in to level for specific families).
* Router's open-position check prevents re-entry on level-triggered re-fires
  (already handled by the monitor's holding-book, but the check needs to be tight
  in the level path).

Touches every strategy class (adds one column) + LiveMonitor + optionally per-strategy
config. Estimate 3-4 hr + tests. **Explicitly does NOT touch the Alerter** — the
alerter's ergonomics are separate and out of scope per the standing rule above.
Surfaced from the same VDY-concentration session: 3 fills totaled $110,109 notional
against $99,995 starting equity — paper broker allowed cash to go **negative (−$10,124)**
because the sizer's `max_leverage=1.0` is per-position (vol-scale-ceiling), not
per-portfolio. Portfolio gross leverage on that session was 1.10× with zero real
guardrail; a real account would either reject or margin-borrow. Distinct from the
per-name notional cap in item 2 above — that one prevents any single name from
dominating; this one prevents the sleeve as a whole from exceeding available cash.

Router gate shape:

* `max_gross_leverage` config (default 1.0 for paper — accepts no leverage; higher for
  margin accounts once live).
* Pre-fill check: `(sum_open_notional + intent.notional) / equity` must not exceed the
  cap. If breach, either trim the intent size to the remaining headroom OR reject
  outright — config flag `on_leverage_breach = 'trim' | 'reject'`.
* Runs BEFORE the vol-target sizer's leverage-cap-of-1.0 fires, so a name never gets
  sized past what the sleeve can fund. Ordering matters: this gate sees intents in
  submission order, so first-fill wins the remaining headroom.
* Cross-check: `PaperBroker._journal_equity` already computes cash + positions_value
  per row — reuses the same accounting, no double-tracking.

Estimate: ~1.5 hr including test coverage. Would have prevented the −$10k cash breach
outright.

**Dependency ordering:** 1 (config edit — 5 min) → 3-cheap (router unrealized-loss exit
— ~1 hr) → 5 (portfolio leverage gate — ~1.5 hr, catches the more common failure than 2)
→ 2 (per-name notional gate — ~2 hr) → 4 (WF-validated strategy stops — ~4 hr per
strategy family, needs cache warm). None require the intel corpus to deepen.

## Asset-class calibration — signal-statistics follow-up  🟡 QUEUED

Landed 2026-09-05: `src/trading_live_claude/analysis/calibration.py` translates a symbol into
per-class strategy kwargs (window, n_std, oversold, entry_z, exit_ma, atr_window), wired
through `tune.py` and the `confirm_<base>`/`candle_<pattern>` factories. Matrix output on
2026-09-05 confirmed the **time-scale side is sound** — windows track half-life, ema/donchian
scale with `bar_scale`, exit_ma tracks the 0.7·HL rule.

**What still needs honing (the microstructure-signal side):**
- `n_std` on Bollinger — currently a simple `vol/0.20` multiplier around a regime-based
  base (2.0/2.5/1.8). Doesn't yet reflect that crypto's fat-tail distribution needs a
  higher-percentile band than the same annualized-vol gaussian would predict; FX majors'
  tight-spread microstructure means the 1.44 band probably over-triggers on cost.
- `entry_z` on ZScoreOU — currently `1.5 + spread/(bar_bps)`, which lands 1.5-1.7 across
  every asset class. That's a suspiciously flat surface: crypto with 8-20bps spreads
  should demand a wider entry_z than FX at 1bp, and the current formula compresses them.
  Recompute against realized round-trip cost drag, not just spread nominal.
- `oversold` on RSI — the three-way 25/30/35 discretization is coarse. Fixed_income
  gets 35 across the board even though a short-duration bond and a long-duration bond
  have very different reversion timescales; either add a duration-scaled oversold or
  accept the coarseness and document.
- Confirmation-pattern filter — currently drops only `piercing_line` on 24/7 markets.
  Should probably also drop `bullish_engulfing` on crypto (its "gap-below-prior-close"
  criterion is a rounding artefact on continuous bars), and consider adding session-
  specific patterns for FX (Asian-session hammers behave differently from NY-session
  hammers). Requires per-symbol backtest evidence, not a-priori reasoning.

**Path forward:** run a sweep of Bollinger n_std ∈ {1.5, 2.0, 2.5, 3.0, 3.5} × asset-class
representative symbols with the walk-forward harness, scored on sortino_over_dd, and let
the surface pick the class-specific band width empirically. Same for ZScoreOU entry_z
∈ {0.5, 1.0, 1.5, 2.0, 2.5, 3.0}. Then fold the winners back into the calibration table
as class-specific constants replacing the current heuristic formulas. Bounded work
(~30 min per strategy × 4 asset classes) but needs the deep-history parquets already
built for crypto/FX + the equity cache.

## Audit gaps — 2026-09-04

Explore-agent audit sweep across the whole repository, ranked by severity, excluding items
already elsewhere in this document. Format: `severity file:line — one-liner`. Do NOT redesign
solutions; call out and prioritize. Full agent report lives in the session transcript.

### 🔴 Critical (act soon — live-path risk or silent correctness bugs)

- ✅ FIXED 2026-11-01. `trading live` AND `autonomous_run` built `LiveMonitor` without
  `overlay_for`, `interpret_for`, `weight_bias_for`, `persistence_for`, so the two CLI entry points
  that can touch real money bypassed every intel/overlay/allocator/persistence gate the paper path
  uses. See the Top-5 entry below for the decisions taken.
- 🔴 `data/market.py:51` — `end = end or datetime.now(UTC)` is then part of the parquet cache
  key. `datetime.now()` changes every call, so `MarketData.history()`/`.recent()` NEVER hit
  the cache. Every `LiveMonitor.step()` symbol is a fresh live-broker fetch. Cache module
  exists but the primary caller can't reach it.
- 🔴 `data/market.py:22` vs `brokers/{kraken,ib,ib_web}.py` — interval-name mismatch. `MarketData`
  uses Questrade lexicon (`"HalfHour"`); Kraken uses `_INTERVAL_MINUTES` (numeric); IB uses
  `_INTERVAL_TO_BAR` (`"ThirtyMinutes"`). Non-QT 30-minute requests silently fall through to
  `OneDay`.
- ✅ FIXED 2026-11-01. `monitor/live_loop.py:245` — `float(last.get("atr", price * 0.02)) or price * 0.02` returns
  `NaN` when `last["atr"]` is `NaN` (NaN is truthy). Strategy with NaN ATR silently sizes off
  NaN, corrupting stop distance and downstream risk math.
- 🔴 `monitor/live_loop.py:283-290` — `conviction *= weight_bias` is passed to `PositionSizer.size`
  which clips conviction to `[0, 1]`. All `weight_bias > 1.0` boost is discarded. The
  correlation-aware allocator's up-side amplification is silently no-op; only trims work.
- 🔴 `risk/kill_switch.py:67-80` — `KillSwitch.evaluate()` is defined + would auto-trip on
  drawdown/daily-loss, but NO CALLER anywhere invokes it. The drawdown-based auto-halt shown
  in the README architecture diagram (§#4) is manual-only.
- 🔴 `brokers/ib_web.py:509-511` — `place_order` auto-confirms IB warning prompts in a while
  loop with NO CAP. In live mode this can accept margin/exchange warnings a human should
  approve; also potentially loops unbounded on a misbehaving API.
- ✅ FIXED 2026-10-31. `README.md:141` — Quick-start block includes `EXECUTION_MODE=live uv run trading live …`.
  `CLAUDE.md:24` explicitly says "Never set `EXECUTION_MODE=live` in code, tests, or example
  snippets." README example directly contradicts the CLAUDE.md non-negotiable.
- 🔴 `cli.py:1381-1383` — `os.environ["QUESTRADE_ENV"] = chosen_account` is written AFTER
  `get_settings()` has been called and after `_make_questrade(settings)` used the loaded
  settings. Runtime override is a no-op — users flipping `AUTONOMOUS_ACCOUNT=live` still hit
  whatever the frozen settings resolved.
- 🔴 `config/settings.py:210` — `@lru_cache(maxsize=1)` on `get_settings()`. Any test/process
  that mutates env/yaml AFTER first call gets stale settings. Compounds with the autonomous_run
  env-mutation bug above.

### 🟡 Medium (correctness / hygiene)

- 🟡 `brokers/kraken.py:382-387` — `_txid_to_int` uses `hash(txid)` which is randomized per
  Python process (PYTHONHASHSEED). Docstring calls it "stable" — it is not. Kraken fill row
  `order_id` changes across restarts; joining fills back to orders breaks post-restart.
- 🟡 `execution/daily_budget.py:54-88` — `snapshot()` re-parses the entire `state/orders.jsonl`
  from disk on every gate check. Scales linearly with journal size; runs synchronously inside
  `Router._gate` on every intent.
- 🟡 `data/cache.py:40-45` — `put()` writes parquet directly with no atomic rename. Crash
  mid-write leaves a corrupt file; subsequent `get()` returns `None` but the corrupted file
  stays forever (never overwritten because of the time-varying key bug).
- 🟡 `brokers/ib.py:354` — `Stock(order.symbol, "SMART", "USD")` hardcoded. Non-US equities
  (e.g. `XIC.TO`) become the wrong contract; futures/bonds registered via `IBContract` elsewhere
  are not consulted by `place_order`. Silent mis-routing on the socket path.
- 🟡 `monitor/live_loop.py:398` — `heat = existing_risk / equity`, but `existing_risk` is DOLLAR
  CVaR/ATR risk while `hedge_weight` expects a fraction. Units mismatch when the hedge
  overlay is enabled.
- 🟡 `brokers/models.py:161` — `Fill.venue: Literal["paper", "questrade-practice",
  "questrade-live"]`. Kraken/IB/IB-Web fills MUST be constructed with `venue="paper"` (see
  `paper.py:165`); any adapter that writes `Fill` directly with `venue="kraken"` fails Pydantic
  validation. Schema anchored to pre-multi-venue world.
- 🟡 `execution/asset_router.py:23-37` — `DEFAULT_ASSET_BROKERAGE`/`ASSET_LEAN_SPEC` still say
  `InteractiveBrokersBrokerage`, but the runtime brokers implemented are `IBBroker`,
  `IBWebBroker`, `KrakenBroker`, `QuestradeBroker`. AssetRouter is LEAN-oriented and doesn't
  route to any real Broker adapters.
- 🟡 `execution/asset_router.py:16` — `AssetClass = Literal["equity","future","commodity",
  "crypto"]`. But `intel/overlay.py::OverlayClass` now carries `fixed_income`, `precious_metals`
  (2026-09-03 close of gap #4). Two class enums have drifted apart.
- 🟡 `intel/apply.py::apply_overlay` — Grep shows only the `intel/__init__` export and its test
  import it; no runtime code path. Public API surface that's effectively dead.
- 🟡 `brokers/paper.py:186-220` — `mark_to_market` swallows quote failures per symbol. If the
  feed is throttled/down, equity CSV keeps writing STALE unrealized P&L with no telemetry that
  the marks are stale — the max-drawdown kill-switch invariant would read a lying number.
- 🟡 `brokers/base.py:22-48` — `Broker` `Protocol` does not declare `venue: str`, but every
  concrete broker declares it and `PaperBroker.__init__` depends on it. A new adapter that
  omits `venue` type-checks fine but silently falls back to `.name` at `paper.py:64`.
- 🟡 `cli.py:496-497` — `trading live` command WARNs but does NOT abort when
  `QUESTRADE_ENV != "live"`. User typing the phrase can run "live" mode against the practice
  environment thinking it's live (or vice versa).
- 🟡 `data/cache.py:22-28` — Path uses first 16 chars of SHA-256 hex, no schema version in the
  filename. Future OHLCV column change can't invalidate existing parquets.
- 🟡 `data/market.py` — no gap-filling / incremental append. Every `history()` call re-fetches
  entire window rather than fetching only missing tail.
- 🟡 `brokers/ib_web.py:107` and `config/settings.py:73` — `verify_ssl=False` default (CP Gateway
  self-signed cert). If a caller mis-points `ib_web_host` off `localhost`, TLS validation is
  silently skipped. No guard that the target IS localhost.
- 🟡 `monitor/alerter.py:43-60` — `_telegram` no rate-limit (30-msg/sec Telegram cap will 429),
  no response-status check, no scrub for anything containing secrets (fine today but no
  guardrails).
- 🟡 Test coverage — no test files for `data/market.py`, `data/cache.py`, `execution/journal.py`,
  new futures pipeline (`data/futures_calendar.py`, `data/futures_continuous.py`,
  `data/ib_futures_history.py`), `microstructure/{bitstamp_l2,coinbase_l2,simulator,arbitrage}.py`,
  `intel/{apply,chart,worldmonitor}.py`, `scripts/liquidity_heatmap.py`,
  `scripts/walk_forward_pairs.py`, `scripts/sweep_ib.py`.

### 🟢 Minor (docs / cleanup / future risk)

- 🟢 `README.md:186-206` — "What ships in the box" table lists 6 strategies but
  `strategies/examples/` actually holds 11 files (also arima_garch, mean_reversion, momentum,
  seasonality, volatility). Stale.
- 🟢 `README.md:232-234` vs `CLAUDE.md:118-120` — Two docs disagree on the agent set. README
  lists 2, CLAUDE.md lists 3 (adds `autonomous-monitor`).
- 🟢 `CLAUDE.md:60-64` — "Entry points" section is stale relative to actual layout — doesn't
  mention `intel/`, `portfolio/`, `microstructure/`, `models/` or the four broker adapters.
- 🟢 `brokers/paper.py:47` — `_order_counter: Iterator[int] = itertools.count(1)` is a CLASS
  variable. Two PaperBroker instances in the same process share it. Fine today; will bite
  two-broker research scripts.
- 🟢 `brokers/token_store.py:39` — Fixed salt `b"trading-live-claude/v1/tokens"`. If two users
  share the same `TOKEN_ENCRYPTION_KEY`, they get the same Fernet key. Per-install random salt
  would be safer.
- 🟢 `pyproject.toml:9-26` — every runtime dep is `>=` with NO upper cap. Breaking major
  (pydantic v3, pandas v3) would install and break silently. No `uv.lock` review at commit time.
- 🟢 `pyproject.toml:31-33` — `ib_insync>=0.9.86` is optional but effectively unmaintained
  (last release 2023). `brokers/ib.py` depends on it. Long-term move to `ib-async` (community
  fork) is likely required.
- 🟢 `pyproject.toml` — `PyJWT` used in `ib_web.py::_mint_client_assertion` but NOT declared
  in any extras. Lazy import + `BrokerError` on ImportError catches it, but users of
  `OAuth2JWTAuth` get no install hint until first token exchange.
- 🟢 `brokers/questrade.py:16` — `LOGIN_HOST` hardcoded — no override for QT's practice sandbox.
  `questrade_env=practice` has no effect on this constant (the auth flow returns the api_server
  anyway).
- 🟢 `state/paper_fills.jsonl`, `paper_orders.jsonl`, `paper_equity.csv` — `session_id` added
  post-hoc; older rows don't have it. No migration script; analytics joining by session_id must
  tolerate NULL.
- 🟢 `execution/router.py:229-247` — router journals order intent BEFORE the kill-switch check;
  intent is written even for HALTED, then written again as rejected. Harmless duplication but
  grows journal on halted state.
- 🟢 `intel/graph.py:397-406` — `append_edges` catches all exceptions and logs; a full-disk
  condition silently drops graph write with nothing surfacing to the operator.
- 🟢 `monitor/alerter.py:71` — hardcodes SMTP port 465; no `AlertConfig.smtp_port` field.
  Users on 587/STARTTLS can't configure.
- 🟢 `brokers/paper.py:52` — `starting_equity=100_000.0` hardcoded across CLI callers; not
  read from `trading.yaml`.
- 🟢 `monitor/live_loop.py:38-40` — `_INTERPRET_BIAS_FLOOR = 0.25` and confidence factor map
  hardcoded; not configurable.

### Top-5 recommended fixes (agent's ranking) — status as of 2026-09-08

1. ✅ **Wire intel/overlay/allocator/persistence hooks into `trading live` and `autonomous_run`**
   LANDED 2026-11-01, with these decisions from the user. `tests/test_intel_hooks_wiring.py` pins the
   wiring on every runner.
   * Overlay: on when `WORLDMONITOR_API_KEY` is set; without one the runner starts and says it is
     off. `live` has `--no-intel-overlay`. Fails open. **Corrected 2026-11-01:** the user chose this on
     the description "an outage keeps the last good read", which is wrong for a normal outage. See
     section 13, item 2.
   * Allocator: the full 0.1×–3× range, same as paper, so one real order can be 3× larger with only
     the Router's caps and the daemon's daily budget above it. The boost side has no validation.
   * Persistence gate: every runner. It cannot fire until a poller writes `elevated_in` edges.
2. ✅ **`MarketData` cache fix** — LANDED commit `638f8d3` (floor `end` to interval boundary).
3. ✅ **Interval-name mismatch across brokers** — LANDED commit `638f8d3` (IB/Kraken lexicon
   canonical; Questrade translates on its own side).
4. ✅ **Conviction-clip / weight-bias contradiction** — LANDED commit `638f8d3` (raised to
   [0, 3.0] matching `weight_bias` cap in live_loop).
5. ✅ **Wire `KillSwitch.evaluate` into `PaperBroker._journal_equity`** — LANDED 2026-09-08.
   PaperBroker._journal_equity now calls evaluate() with current equity + peak + day-open,
   trips the file sentinel on breach; day-open equity resets on UTC date change. Combined
   with the tightened 3.0% max_drawdown_kill_switch default, auto-halt in the README
   architecture is now real.

---


## 0. Full resweep — reproducible calibration on the expanded universe  🟢 SCRIPTED, run pending

Universe expansion + reproducible-calibration plumbing landed this session (commit `6d20e50`).
`scripts/sweep_universe.py` now takes `--min 0 --max 1_000_000 --wf-top 30 --tag <label>
--carry-held` (default ON) and writes to `reports/sweep_{tag}_{panel,walkforward}.csv`. Held
names (CGL.TO, ZUT.TO, SDE.TO) always reach the walk-forward stage regardless of screen filters,
flagged in the output — so a resweep can never silently drop coverage of what we actually own.

**Runbook to complete next session** — 3 steps, foreground shell (Windows stdout buffers in
background; ~476 cached names → ~439 pass screen → panel ~40 min, WF ~10 min more):

```
# 1. Pre-warm the cache for held names + the expanded SEED (adds anything Questrade will fetch).
#    Held names had NO cached history on the partial resweep run — this is the exact "silent
#    drop" the carry-in was designed to catch, and warm_cache.py is the fix.
python scripts/warm_cache.py --held --seed equity --years 5

# 2. Full resweep. Reports land at reports/sweep_resweep_full_{panel,walkforward}.csv.
python scripts/sweep_universe.py --tag resweep_full --min 0 --max 1000000 \
    --wf-top 30 --min-bars 900

# 3. Inspect the WF output; edit analysis/universe.py::WALK_FORWARD_VALIDATED with survivors.
#    Held names appearing BELOW top-N in that CSV are research prompts, not auto-sells — the
#    current holding is not in the sweep's best cohort.
```

Also useful during the vertex/edge iteration:

```
# Grow the intel graph off-cadence (default 15-min vendor cadence; --sleep tunable).
python scripts/graph_journal.py --iterations 20

# Read the current shape any time (writes reports/graph_profile.md).
python scripts/graph_profile.py
```

Partial resweep observations from this session (killed at panel 50/439):
- Cached universe: 476 names with ≥900 bars; 439 pass ADV≥$1M and price>$0
- Held assets (CGL.TO, ZUT.TO, SDE.TO) had NO cached history — warm_cache is the prereq
- Panel-stage throughput: ~10 names/min ⇒ ~40 min for the full 439, then ~30 WF folds ~10 min more

## 1. Cross-interface arbitrage — interlisted equities (TSX ⇄ NYSE)  ✅ BUILT
`microstructure/interlisted.py::InterlistedArb` — FX-adjusted TSX/NYSE dislocation detector, tested,
committed. Live scan of 25 pairs confirmed the honest verdict: 0/25 clear at retail FX (~180 bps),
~1/25 marginally at institutional (~3 bps). **Follow-up if revisited:** run it during **market
hours** (the scan ran at ~2am on wide/stale closing quotes), add a **stale-quote sanity filter**
(reject implied-FX deviations too large to be real — the MFC +500 bps artifact, the interlisted
VELO), and stream live quotes rather than one-shot.

## 2. Deeper crypto history → walk-forward the crypto sleeve  ✅ CODE + PIPELINE
`data/kraken_ohlc.py` now has `kraken_trades_paginated` / `aggregate_trades_to_daily` /
`kraken_ohlc_deep` (see commit `cee64b3`), and there are two scripts:
- `scripts/fetch_crypto_history.py` — pulls multi-year history for every sleeve pair via the
  paginated `/0/public/Trades` endpoint and caches parquet under `data/cache/`. Slow (Kraken
  public tier is ~1 req/s and pages hand back ~1000 trades); resumable via `--since`.
- `scripts/walk_forward_crypto.py` — reads the cached parquets and runs the same walk-forward
  helper the equity sweep uses (2y train / 6mo test, per-fold re-opt, WFE ≥ 0.5 ∧ OOS>0
  ∧ ≥10 trades). Reports to `reports/walk_forward_crypto.csv` and prints promotion candidates.

**Still to do:** actually run `fetch_crypto_history.py` (multi-hour on the network) and then
`walk_forward_crypto.py`, and edit `CRYPTO_SLEEVE` tiers based on the report. The script deliberately
does NOT auto-flip the tiers — that belongs on a human diff.

## 3. KrakenBroker — let the Router fill crypto orders  ✅ BUILT (commit dff46ff)
`brokers/kraken.py` implements the `Broker` protocol against Kraken's public + private REST APIs,
committed and registered in `brokers/__init__.py`, with respx-mocked tests. Live placement is
gated behind `enable_live_orders=True` at construction — the switch is per-instance and no code
in the repo flips it on its own. Used today by `scripts/paper_kraken.py` as the market-data feed
wrapped in `PaperBroker`.

**Still pending:** thread through `AssetRouter` for live crypto order routing when the go-live
decision is made. Not urgent — paper path is complete.

- Fractional sizing: `Order.totalQuantity` is `float` already; the router will need to skip its
  integer round on `.crypto` symbols. Small change in `execution/router.py`.
- Secrets (Kraken API key/secret) in `.env` only, same as Questrade; never commit them.

## 4. Richer interpret.py catalog — three new thesis motifs  ✅ BUILT (commit 1278d69)
Dollar strength divergence (primary + mirror), Disaster / insurance underpricing, Commodity
carry-inversion proxy (moderate-cap, awaits real futures-curve feed). New `insurance` and
`emerging_markets` theme keys, 7 focused tests. **Follow-up:** ingest a live futures-curve
feed so the carry-inversion thesis fires on the real signal rather than the stress+flow proxy.
_(Original spec pruned 2026-09-08 — recover from commit `1278d69` if needed.)_

## 5. Paper-trading journal upgrade  ✅ BUILT (commit 2173d35)
`PaperBroker` now emits `state/paper_orders.jsonl`, `state/paper_equity.csv`, and stamps a
per-instance `session_id` on every row across all three journals. Realized/unrealized P&L, peak
equity, and drawdown_pct feed the max-drawdown kill-switch invariant. 7 focused tests.
**Follow-up (queued):** tick-aware fill prices instead of full-precision floats — do this when
tick-aware sizing enters the router.
_(Original spec pruned 2026-09-08 — recover from commit `2173d35` if needed.)_

## 6. GraphRAG / multi-agent overlay on the intel wing  🟡 SHIPPED + HELD

**Validation 2026-11-01.** The logic works against a mocked API, end to end from graph to monitor bias
(`tests/test_intel_hooks_wiring.py`). **No real call has ever been checked:** the real debate is written
but `ANTHROPIC_API_KEY` is not in the app's settings, so nothing was sent. It remains unwired, and the
journal holds no events for it to read. Fixed on the way:
* The evidence had no headlines (ids, sources, categories and regions only). `meta.title` now carries them.
* Direction was dropped, so a constructive read trimmed its own exemplars. Only adverse reads trim now.
* `_domain_slice` matched hashed ids ("fed", "ecb"), and its no-text branch could never fire.
* `max_tokens=700` had to cover thinking too, which is on by default on `claude-sonnet-5`. Now 4000.
* The adversary prompt sent its JSON template with doubled braces, because it is never `.format`ted.

**Current posture (decided this session): keep the pipeline informative on the rule + graph
layers alone; iterate vertices/edges from journaled inputs as the record accrues; hold the
specialist/adversary agent sim layer.** The agent layer is built and tested, but wiring it into
the live poll cadence needs an Anthropic Console API key (separate from Claude Desktop, ~cents
per debate run at Sonnet 5), and the value only compounds once the graph journal has meaningful
depth — running debate on a thin corpus is expensive noise. Revisit when the graph has weeks of
history and there is a specific hypothesis worth the LLM round-trip to sharpen.

**What's built and running today:**
- `intel/graph.py` — append-only edge journal at `state/intel_graph.jsonl`. Snapshot decomposes
  into typed `(subject, predicate, object, weight, ts)` rows. Node types: poll, domain, region,
  source, market, event. Predicates: observed, elevated_in, co_occurs, stressed_by, mentioned_by,
  about_domain, affects_region.
- **Per-event decomposition** (commit `75206bf`) — `WorldMonitorClient.snapshot` writes per-event
  edges from news cross-source signals, advisories, and conflict strategic-risk sample. Vendor id
  preferred; deterministic hash of title+timestamp fallback. Corroboration is now a graph query.
- `recent_events_from_graph()` projects edges back into evidence records for downstream consumers.
- `edge_persistence()` query — "this predicate→object edge has held for N consecutive polls".
- Inverse-weighted source freshness (commit `75206bf`) — market-driven gates (fear, VIX, DXY,
  crypto_vol) now discounted by the market payload age; the freshness formula weights each
  source by its own freshness so a stale source drags the blend down less than a naive mean.

**What's built but held (not wired into the live path):**
- `intel/agents.py` — SpecialistReader (per domain), Adversary, `debate()`. Real Anthropic
  Messages API calls (no injectable stub); respx-mocked in tests. Commits `c925916` + `f523349`.
- `interpret.py::enrich_with_agents()` — merges FiredThesis rows into the rule reads with
  vocabulary-aligned confidence bands. Deliberately explicit-opt-in; not called from anywhere on
  the deterministic hot path. Kept available so a future runner (e.g. `scripts/paper_intel_debate.py`)
  can call it on-demand once a Console key is in place and the graph journal is deep enough to
  reward the LLM cost.

**Iterative next steps that keep this posture:**
- Extend `snapshot_to_edges` and the vendor-payload decomposition as new evidence shapes surface
  (e.g. per-event severity edges, per-actor mentions, per-corridor edges for shipping/energy).
- Grow the `edge_persistence` query family: co-persistence across two edges, thickening rate,
  first-seen recency. These are the features `intel/history.py` cannot express on the flat frame.
- SQLite backend once the JSONL grows past a few MB — the query surface stays the same.

_(Original speculative spec — MiroFish/OASIS/GraphRAG design notes — pruned 2026-09-08. Full
text recoverable from git history if the agent-layer path is revisited.)_

## 7. Crypto WF — shallow fallback  ✅ CODE + PIPELINE + PROTOCOL VALIDATED (2026-09-08)
`scripts/walk_forward_crypto.py` shallow-fallback closed 2026-09-04 (uses
`kraken_ohlc(pair, interval=1440)` when deep-history parquet absent; tier=`screened+`
for shallow-derived rows, never `robust`). Protocol validated 2026-09-08 across all 13
CRYPTO_SLEEVE pairs (2 deep / 11 shallow). Tier promotions DEFERRED per data-first rule
— see the "Deep-history fetch (currencies sleeve)" queued item above. Report at
`reports/walk_forward_crypto.csv`.

## 8. Pair-trading strategy oriented to FX pairs

`strategies/examples/pairs.py` is cointegration-based and asset-agnostic — feed it two
co-integrated symbols and it emits entry/exit against the spread. Currently calibrated on
equity pairs (`RY.TO/BNS.TO` etc). To use it on FX:

**Blockers:**
1. **No FX price feed wired.** Questrade returns FX only inside its Cdn ADR / interlisted arb
   plumbing, not as standalone pair quotes. Kraken quotes `EUR/USD`, `USD/CAD`, etc. natively
   for the pairs it lists, and via `KrakenBroker.quotes` that's already accessible — enough for
   a small MVP.
2. **No FX-side cointegration research.** Equity pairs cointegrate on shared factors (bank
   fundamentals, sector cyclicality); FX pairs cointegrate on rate differentials, real-vs-
   nominal moves, and carry — different mean-reversion horizons and different appropriate
   half-lives.
3. **`WF_PROTOCOLS["fx"]` is registered** with the right shape (504/126/260-annualization) but
   `data_source="pair-price feed (not yet wired)"`. Same honest gap as futures.

**Realistic path:**
- **MVP:** enumerate the FX pairs Kraken lists; use `KrakenBroker.quotes` + `kraken_ohlc` to
  build daily histories; sweep `pairs.py` across every FX-pair combination that has cointegrated
  history (Engle-Granger + Johansen, pick the pair pool). Uses the fx protocol.
- **Later:** add `data/fx.py` alongside `data/kraken_ohlc.py` / `data/market.py` — an FX-vendor
  adapter (OANDA / Alpha Vantage / Polygon FX) with the canonical OHLCV shape. Then the FX pair
  universe widens beyond Kraken's fiat list.
- Register FX-specific parameter grids (shorter mean-reversion half-lives than equity pairs).

## 9. Cross-path wiring — intel ↔ trading ↔ alerter  🟡 TIERS 1-3 DONE

Status as of 2026-09-08:
- ✅ **Tier 1:** OSINT scalar + interpret filter + allocator bias + Alerter (both QT + Kraken)
- ✅ **Tier 2:** Fills → intel graph via `traded` predicate + `fill_edge()` helper
- ✅ **Tier 3:** Graph persistence → entry gate via `PersistenceGate`. **Corrected 2026-11-01:** marked
  done here on 2026-09-08, but no trading runner passed it (only a report column in `sweep_ib.py`).
  Now wired into all six `LiveMonitor` calls. Also fixed: one fill reset every run to 0, because a
  `traded` edge counted as a poll with no elevation; and a run with no age limit could halt entries
  on a months-old reading, so a run older than `max_age_hours` (6h) now counts as none.
- ⏳ **Tier 4:** Realized P&L → thesis calibration — **DATA-BLOCKED**, needs weeks of paper
  fills matched against active theses, ~30 days minimum. The accrual clock restarted at zero: the
  journals did not survive the repo reconstruction (as of 2026-11-01, `state/intel_graph.jsonl` is 27
  `traded` edges, 26 of them dated 2026-10-30 and mostly test-fixture leftovers, with no `elevated_in`).
- ⏳ **Tier 5:** Prediction evaluation — **DATA-BLOCKED**, needs 21-day forward-return windows
  post-thesis fire. Same restart: zero thesis history today.
- **Not validated on real data:** tiers 1–3 are verified on stubs only. Nothing says the overlay, the
  allocator or the persistence gate improves outcomes. `elevated_in` edges accrue only while an overlay
  provider or `graph_journal.py` polls.

_(Original spec pruned 2026-09-08 — the "missing wires" narrative was subsumed by the tier
list above. Recover from git history if needed.)_

## 10. Correlation-aware allocator on the crypto + equity sleeves  ✅ BUILT (commits 365452a + f9acdcc)

Landed on both venues:
- **Crypto:** `paper_kraken.py` computes bias from 720-day daily OHLC + screen_score, biases
  per-pair conviction via `LiveMonitor.weight_bias_for`. Current 2026-09-08 bias distribution:
  BTC/PAXG at 3.90× cap, ZEC 0.58× (real diversifier tier), cluster names 0.24-0.51×.
- **Equity:** `cli.py signal` computes bias from WF-validated OOS scores + 252-bar return
  histories from local cache. Symbols not in `WALK_FORWARD_VALIDATED` default to neutral 1.0×
  (as seen with RSI.TO / RIG.TO in the 2026-09-08 pool).

**Follow-up:** recompute correlation matrix on cadence (weekly?) and write snapshots to
`state/{crypto,equity}_corr.jsonl` for auditability. One-off correlation study for the 13-pair
sleeve landed as `reports/crypto_corr_2026-09-05.{csv,png}` — not yet on cron.
_(Original spec + correlation cluster table pruned 2026-09-08 — recover from git history.)_

---

## 11. TradeCard — six-axis gap closure  🟢 INTEGRATED, GAP CLOSURE IN PROGRESS

**Status (2026-09-08).** Landed on `feat/tradecard-approval` at
<https://github.com/akshan-bansal/FRM-Claude/pull/new/feat/tradecard-approval>
as 10 coherent commits (through the thesis-prose framing patch).
**44 tests pass.**

### Architectural decision — zero vendor infrastructure

Approved 2026-09-08 after weighing security auditors, backend cost, and
user updateability against each other. The full rationale is in the
memory file `tradecard-zero-vendor-infra-architecture.md`; the summary:

- **The vendor operates no recurring services.** Firmware ships via
  **GitHub Releases** (signed with sigstore/cosign in CI); PWA / docs /
  OpenAPI ship via **GitHub Pages**; push notifications ride a
  **user-chosen relay** (ntfy.sh topic or native APNs/FCM through a
  PWA-registered VAPID key on the user's own shim). No user accounts,
  no telemetry, no push tokens stored server-side.
- **The shim is single-owner-per-box.** Multi-tenant / per-owner bearer
  tokens are a coordinator concept and are dropped from the near-term
  plan. Each box is one tenant.
- **Recurring vendor cost stays $0/month** whether there are 10 users
  or 10,000 — the card is a one-time purchase and the software is
  open-source; nothing recurs.
- **Supply-chain concentration on GitHub** is mitigated by sigstore
  signatures + a public transparency log, matching the pattern npm /
  PyPI / Docker already ship.
- **Non-technical customers** get a bundled RPi-class box pre-flashed
  with the shim (manufacturing allies produce the hardware). The vendor
  still doesn't operate services — the box runs the same open-source
  shim, offline-capable.

The revised first-week migration plan (below) reflects this: no
`owners` table, no coordinator client, no vendor push service.

### Sub-obj 4 queue

- ✅ **URL versioning to `/v1/…`** — landed. Every non-public route now lives
  under `/v1/…`; legacy unversioned paths still resolve during the
  deprecation window but the shim logs a warning per hit and the OpenAPI
  spec advertises `/v1/…` only. `SPEC_VERSION` bumped to `1.0.0`.
- ✅ **Multi-broker routing inside a single Router** — LANDED 2026-11-01, Router level
  (`tests/test_multi_broker_router.py`). `Router(brokers=..., default_broker=...)`; an unknown route
  is rejected, not defaulted; the card signs the routed venue; autonomous stays single-broker. Still
  single-broker: `LiveMonitor` (equity and positions from one broker, needs the numeraire decision)
  and the basket gate (one venue; other routes take no new entries). Original note follows.
  (moved 2026-09-08). Today's `Router` holds one `Broker` instance and the
  card displays whatever `router.broker.name` is set to. Turn
  `intent.broker` into a real routing key:
  `Router(brokers: dict[str, Broker], default: str)`; dispatch inside
  `submit()` selects by `intent.broker` (falls back to `default`); every
  existing risk gate keeps running unchanged. `wire_card_approval` passes
  the dispatched broker name into the prompt so the WYSIWYS canonical
  remains the true destination. Reason for the delay: land the FastAPI
  port first (see below) so this refactor happens in the target framework
  once, not twice.

Nothing here changes `Router._gate` or the autonomous-daemon gate list.

### Shim runtime migration — first-week plan (revised for zero-vendor-infra)

Runs on `feat/tradecard-approval` in order (revised 2026-09-08 to
front-load the framework port before the multi-broker refactor, so the
refactor lands in the final shape once instead of being re-done on the
FastAPI side):

1. ✅ **URL versioning to `/v1/…`** — landed (commit `509d287`).
2. ✅ **SQLite persistence for the store** — landed (commit `c8db37a`).
   Prompts + pubkeys + passbook rows survive shim restart; single-owner
   schema.
3. ✅ **FastAPI port** — landed (commit `f13b45d`). Pydantic models are
   the single source of truth; hand-authored `approval_schemas.py`
   deleted. Legacy paths still resolve via middleware during the
   deprecation window.
4. ✅ (landed 2026-11-01, see Sub-obj 4) **Multi-broker routing inside a single Router** — QUEUED FOR NEXT
   SESSION. `Router(brokers: dict[str, Broker], default: str)`; dispatch
   inside `submit()` selects by `intent.broker` (falls back to `default`).
   Watch for merge interaction with `629594c` (kill-switch / gross-
   leverage / per-symbol cap / intra-day forced exit) which landed on
   `feat/multi-scoring-attention-map` — the risk-architecture work
   touches `Router._gate` internals; ApprovalRouter only wraps `submit`,
   but the rebase will need care.
5. ✅ **Packaging** — landed (commit `d95764c`). `tradecard-shim` console
   entry-point, hardened `deploy/Dockerfile` (unprivileged uid 10001,
   `/data` volume, healthcheck), hardened `deploy/tradecard-shim.service`
   (NoNewPrivileges, MemoryDenyWriteExecute, private tmp, empty
   capability bounding set).
6. ✅ **Sigstore-signed release workflow** — landed (commit `5b5dbe8`).
   `.github/workflows/release.yml` fires on `v*` tags, builds wheel +
   sdist, attests SLSA v1 provenance, signs with cosign keyless via OIDC
   to Rekor, attaches `.sigstore.json` bundles to a GitHub Release. Also
   `.github/workflows/test.yml` runs the 58-test approval slice on push
   / PR. `deploy/VERIFY.md` documents the customer-facing cosign
   invocation. Firmware `.bin` signing plugs in the same way once the
   ESP-IDF build lands in CI.
7. ✅ **PWA scaffold** — landed (commit `dede629`). `pwa/` is a static
   companion viewer (index.html + manifest + sw.js — no build step). No
   third-party JavaScript, no fonts / icons off-domain, no telemetry.
   Bearer token in sessionStorage only. Renders pending prompts + the
   passbook against the user's own shim over LAN.
   `.github/workflows/pages.yml` publishes on push to `main`.

DROPPED (kept for the record): per-owner bearer tokens / owners table —
the zero-vendor-infra decision makes the shim single-owner-per-box and a
bearer token scopes to the box, not to a user record.

### Follow-up PRs after `feat/tradecard-approval` merges

The current branch closes out the shim runtime migration plan except for
item 4. Two objectives are queued as their own follow-up PRs so the
current 18-commit review doesn't grow further:

- **`feat/tradecard-multi-broker`** — item 4 of the migration plan.
  Refactor `Router` to carry `brokers: dict[str, Broker]` + `default: str`
  instead of a single `broker`; dispatch inside `submit()` selects by
  `intent.broker` (falls back to `default`); ApprovalRouter's WYSIWYS
  canonical continues to bind the dispatched broker name. Rebase target
  is whatever's on `main` at that point — the risk-architecture work
  from `629594c` (kill-switch / gross-leverage / per-symbol cap /
  intra-day forced exit) touches `Router._gate` internals, and this
  refactor is small enough (~1 file, ~1 test file, existing 58 tests
  stay green) that the rebase surface is limited.
- **`feat/tradecard-settings`** — on-device SETTINGS menu + a new
  signed-intent kind for settings changes. Server side: a
  `POST /v1/settings` endpoint that accepts a signed
  `SettingsChangeIntent`, mutates `config/trading.yaml` on ACCEPT, and
  journals the change to the passbook with an `S` verdict alongside the
  `A` / `D` / `X` trade verdicts. Firmware side: new state in the poll
  loop, D-pad menu rendering, LEFT/RIGHT to step values, CENTER to
  publish. Tier-A card-enforced rules (per-tap cap, broker allowlist,
  trading hours) go in NVS on the card; Tier-B rules (watchlist, heat
  cap, R filter) round-trip through the shim.

Not on the near-term list: coordinator service, cloud-hosted anything,
plugin sandbox, Postgres, phone-as-shim. Those come only when there are
real users forcing the decisions.

**Sub-objective 0: complete integration into the GitHub repo — DO THIS FIRST.**
Everything else in this section presumes the code is landed on `main` behind a feature flag,
not sitting as an uncommitted diff on `feat/multi-scoring-attention-map`. Concrete steps:

1. **Branch off cleanly.** Create `feat/tradecard-approval` from the current branch's HEAD
   (or from `main` if the multi-scoring work has already merged). Rebase down to a series of
   coherent commits: (a) `approval.py` + tests; (b) `vs_engine.py` + tests; (c) shim +
   simulator + shim tests; (d) firmware skeleton; (e) paper-script `--require-card` wiring.
2. **Split `firmware/` from Python packaging.** Right now it sits at the repo root outside
   `src/`; add `firmware/` to `.gitignore` for the Python wheel and note in `pyproject.toml`
   that the wheel only ships Python. The ESP-IDF project is standalone.
3. **Fix the cross-package import in `wire_card_approval`.** It currently reaches into
   `scripts.approval_shim` via an injected `shim_starter`. Move the HTTP server into
   `src/trading_live_claude/execution/approval_server.py` so the module dependency arrow
   points the right way, and turn `scripts/approval_shim.py` into a thin CLI wrapper.
4. **Docs**: extend `CLAUDE.md` with a "TradeCard" section (feature flag, security posture,
   which scripts honor it, how to run the shim + simulator end-to-end without silicon).
5. **CI**: add `tests/test_approval_shim.py` to the default run; keep it out of the
   coverage-gate baseline if it flakes on socket-bind races on Windows CI.
6. **Changelog / release notes** entry — user-facing description of `--require-card` and the
   published brief link.
7. **Open a PR** with the published-brief artifact URL in the description so reviewers can
   see the shape without pulling the branch. Keep `execution_mode` and `AUTONOMOUS_ENABLED`
   untouched; card approval is orthogonal to live/autonomous.

Do NOT commit any of this until the user says so — the standing rule in
`memory/no-commits-without-explicit-ask.md` still applies.

### Sub-objective 1: hardware

- No schematic, no BOM, no PCB. `firmware/tradecard/main/main.c` documents a pinout but
  there is no board that wires it up.
- No secure element. Private Ed25519 key sits in NVS flash — trivially readable over UART
  with `esptool.py read_flash`. Migration target: ATECC608A (I²C) or the ESP32-S3 DS
  peripheral so the sk never leaves silicon.
- No power path. LiPo cell + charging IC (MCP73831 class) + fuel gauge (MAX17048) + boost
  converter not selected. Card cannot run untethered.
- No enclosure. Credit-card form factor is aspirational — the ESP32-S3-DevKitC-1 is roughly
  10× the volume. Realistic target for v0.3: business-card-thick 3D-printed shell with an
  ESP32-S3-MINI-1 module and an FPC-attached PCD8544.
- No display sourcing decision. Nokia 5110 modules on the aftermarket are aging; SSD1306
  128×64 OLED and Waveshare 2.9" e-paper haven't been evaluated as alternates.
- No physical five-key input array. Dome-switch vs tactile vs capacitive not chosen.
- No RF-certification path (FCC / IC / CE) for the Wi-Fi radio.
- No antenna decision (PCB antenna on ESP32-S3-WROOM-1 vs external chip antenna with a
  U.FL pigtail).

### Sub-objective 2: firmware

- `lcd_puts` is a UART mirror. Real 5×7 font + framebuffer painter (u8g2 or Adafruit-GFX
  port) not linked. Nothing appears on the physical LCD yet.
- No NTP sync. `handle_prompt` uses a hardcoded 60-second TTL rather than parsing
  `expires_at` against a synced clock. First real-world prompt with the wrong TTL will
  expire early or run past its actual deadline.
- No TLS. `esp_http_client` uses plain HTTP; the mbedTLS bundle is configured in
  `sdkconfig.defaults` but the client never asks for it. Card ↔ shim is in the clear.
- No deep sleep. Wi-Fi stays on between polls; battery budget for a card-form-factor cell
  measures in minutes, not hours.
- No CENTER-button detail view. `GET /intel/{ref}` shipped on the shim side; the firmware
  does not call it, so the thesis writeup can't be pulled up on the card.
- Trust-on-first-use pairing. Anyone with physical access can flash a new key and
  re-register; the shim has no way to distinguish a real ATECC608A-attested key from a
  spoofed one.
- No firmware OTA. Updates require USB re-flash.
- No factory-reset gesture (e.g. hold CENTER 10s to wipe NVS keys) — a compromised card
  cannot be rekeyed by the user.
- No fault UI. Wi-Fi drop, shim unreachable, signature-rejected responses aren't surfaced
  on the LCD.
- Passbook has no filter/search — only linear scroll.
- No long-press detection for mode switching (accept-vs-detail vs passbook-scroll gestures
  will collide once the CENTER view lands).

### Sub-objective 3: VS investment engine software

- Deterministic-rules only. No LLM path for a richer prose narrator when the caller wants
  one (opt-in via `thesis_fn` swap is easy; not built).
- Thesis carries no confidence or attribution. "geo-risk 78" is a scalar; the writeup
  doesn't cite which WorldMonitor edges / events fired to move it.
- No historical-comparison clause. "Last week the same thesis on XIU.TO hit stop" would
  meaningfully change the reader's calibration; the passbook has the data, the engine
  doesn't use it.
- No feedback loop. Accepted vs declined vs expired verdicts aren't fed back into future
  thesis phrasing or priority (a decline pattern for a given clause could down-weight it
  next time).
- ~~Broker → asset-class mapping is 1:1 in `ASSET_CLASS_HINT`.~~ DONE 2026-10-30: the thesis
  resolves broker + symbol through `routing.classify_instrument` (CGL.TO on Questrade is precious
  metals, an IB futures root is a future), the same resolver the feature store and the IB scripts
  use. `tests/test_asset_class_integrity.py` locks every per-class table and every basket symbol.
- No writeup pruning. `state/intel_writeups/` grows unbounded.
- No multi-lingual output — a Canadian user might want FR/EN toggle.
- Existing strategies still don't emit the `score`/`rank`/`r_multiple` columns the engine
  is prepared to lift via `MarketContext.from_signal_row()`. Contract is documented in
  `strategies/base.py`; adoption is per-strategy work.

### Sub-objective 4: API / plugin endpoints

- **Shim has no auth.** Anyone on the LAN can POST an intent (which prompts the card) or
  POST a fresh `/card/register` (adding a signer). Minimum acceptable: shared bearer +
  loopback-only; production: mTLS with a shim-issued client cert per card.
- No `DELETE /card/{card_id}` — a compromised card cannot be revoked without editing
  in-process state or restarting the shim.
- No `GET /passbook` — the shim knows every verdict but doesn't expose them for a
  companion app or web dashboard.
- No SSE / WebSocket push. Card and any web client both long-poll.
- No admin endpoint for listing active cards, pending intents, or writeup counts.
- No rate limiting on any endpoint.
- No CORS controls — a rogue web page loaded in the user's browser could POST to
  `localhost:8787` if that origin is ever reachable.
- No OpenAPI spec. Third-party integrations reverse-engineer from `approval_shim.py`.
- No URL / Accept-header versioning. First protocol change breaks every deployed card.
- Router still holds one broker at a time. Multi-broker routing (`intent.broker` picks
  the destination brokerage inside a single Router) is not implemented — the card just
  displays whatever `router.broker.name` is set to for this process.

### Sub-objective 5: user interaction

- No first-boot onboarding on the card. LCD shows nothing meaningful until the first
  prompt arrives.
- No queue-preview screen — user can't see "3 prompts pending" while browsing the passbook.
- No secondary "why" screen. The CENTER button is unimplemented, so the reader cannot pull
  up the full VS-engine thesis before deciding.
- No haptic feedback. User must look at the card to know a prompt arrived.
- No LED / bezel indicator for a pending prompt.
- No per-user profile or PIN before signing — card is single-tenant.
- No timeout-warning UI. TTL just runs down silently.
- Passbook has no "jump to today" or symbol filter.
- No language selection.
- Font size fixed; no accessibility affordance for low-vision users.

### Sub-objective 6: connectivity

- Wi-Fi only. No BLE (which was in the original blueprint for phone-tethered operation)
  and no cellular (NB-IoT). If home Wi-Fi drops, the card is inert.
- No connection-status indicator on-device. A dead shim looks identical to "quiet market".
- No offline queue. A signed response with no shim reachable is lost — user's ACCEPT tap
  never lands.
- No on-device network configuration. SSID/PSK are baked at build time via menuconfig; a
  new Wi-Fi means a re-flash.
- No mDNS / auto-discovery for the shim URL.
- No captive-portal handling (hotel Wi-Fi).
- No handoff. Moving the shim to a new machine means re-flashing the card.
- No health telemetry from card → shim (battery, RSSI, last-seen), so the shim can't say
  "your card is offline" in a UI or an alert.
- Shim binds to loopback only. Documented options for exposing it beyond the same host
  (Tailscale, Cloudflare Tunnel) exist as prose but no scripted path.

### Sequencing (what unblocks what)

1. **Sub-objective 0** — landing the code on `main` behind a flag — is a hard prerequisite
   for everything else. Nothing else should be built on an uncommitted skeleton.
2. **Firmware font + NTP** (sub-obj 2) turns the LCD from a UART mirror into a real card
   surface — this is what makes hardware bring-up worth doing.
3. **Shim auth + `/card/{id}` revoke** (sub-obj 4) is the smallest thing that lets a card
   run outside a fully-trusted LAN.
4. **Hardware SE integration** (sub-obj 1) is only worth it once the key handling in
   firmware is written to feed off an I²C signer rather than an in-memory buffer.
5. **Multi-broker routing + strategy MarketContext adoption** (sub-obj 3, 4) is a good
   fit for the same PR since both hinge on the strategy signal-row contract.
6. Everything under sub-obj 5 / 6 is polish that lands after the card is a real physical
   object; UX for a virtual card is close to write-only.

**Published brief for this build (link stays live across sessions):**
<https://claude.ai/code/artifact/9567c2ac-b2bd-4797-adf5-2edbce2a4d90>

## 12. Spectral VaR gate — tail index, then the post-GBM dynamics queue  🔴 NEXT (2026-10-31)

**Context.** `risk/spectral_var.py` (opt-in via `--spectral-var-cap` on `paper_ib.py` /
`paper_kraken.py`) caps an entry's tail loss with `BSM_Spectral/bsm_event.py`: walk-forward
GJR-GARCH plus a semi-parametric GPD law on the standardised residuals. Scope item 1 (multi-day
horizons) landed 2026-10-31 on `ci/pytest-cov`. The engine part is commit `ec50436`. The gate's
h > 1 path branch, `tests/test_spectral_var.py`, the `-path` journal challengers, the
`journal/config.json` amendments and the docs are in the commit that adds this section. Evidence
is in `BSM_Spectral/RECONSTRUCTION.md` under "Multi-day horizons: GJR paths".

### 12.1 Fix the gate's tail index  🔴 do first

**Problem.** `estimate_tail` builds `SemiParamGPD(z, tail_frac=0.10)` with no `k`, so the tail
index is a Hill estimate. Hill puts xi_lo at 0.36–0.45, hitting the 0.45 cap on BTC and ETH.
Restandardising by the inflated sd then squeezes the body. BTC's 1-day 1% quantile is -2.04,
less extreme than the Gaussian's -2.33. The engine's own reconstruction notes already flag Hill
overstating xi.

**Walk-forward breach rates at 1 day** (the gate's default horizon). Nominal is
5.00% / 2.50% / 1.00% at 95% / 97.5% / 99%:

| Underlying | Hill (current) | k = 10 |
|---|---|---|
| SPX | 10.51 / 6.08 / 1.94 | 6.37 / 2.44 / 1.00 |
| BTC | 10.79 / 5.93 / 2.47 | 4.86 / 1.97 / 0.81 |
| USDJPY | 11.25 / 6.88 / 2.80 | 5.52 / 2.71 / 1.00 |
| ETH | 8.67 / 4.94 / 1.73 | 4.32 / 1.90 / 0.75 |
| DJIA | 10.31 / 5.63 / 2.04 | 6.08 / 2.94 / 1.05 |

All eight underlyings move to near nominal with k = 10.

**Do:**
1. Add `tail_k` to `SpectralVarGate` / `estimate_tail` / `attach_spectral_gate` and to
   `thresholds()`, so it moves `risk_check_version`. Add a `--spectral-var-tail-k` flag on both
   paper scripts.
2. Choose the default by walk-forward on a tuning split only, over {3, 4, 6, 10, Hill}, the
   engine's own grid. Then confirm on the held-out windows with
   `bsm_event.run_horizon_backtest(..., tail_k=k)`, which already takes `tail_k`, using Kupiec,
   Christoffersen and `paired_power` on the pinball difference. Do not pick k on the full sample.
3. The fix must not weaken the gate. Check that the new default's VaR is at least the Hill VaR
   at 99% on every underlying, or say plainly where it is not.
4. Rerun the h = 5 and h = 10 horizon backtests with the chosen k. With the right tail law,
   paths moved closer to nominal for SPX, DJIA, WTI, ETH and EURUSD, and further from it for
   USDJPY and BTC; re-judge the h > 1 path branch on that basis.

**Done when:** the gate's 1-day 99% breach rate is within its Kupiec band on held-out windows
for the gate's markets (equities, BTC, ETH), the tests cover the new parameter, the full suite
is green, and `RECONSTRUCTION.md` records the chosen k and the held-out evidence.

### 12.2 Remaining post-GBM dynamics queue  🟡 QUEUED

Scoped 2026-10-31 by testing each GBM assumption three ways: what GBM predicts, the current
model's own simulated band (FHS-GJR), and the data, across all eight underlyings. Ranked:

1. **Vol-index dynamics.** `var_snapshot` moves v as an arithmetic random walk with a floor.
   The data disagree:
   - Vol indices mean-revert, with half-lives of 13–53 days.
   - The 21-day variance ratio of dV is 0.36–0.55, so v shocks at 21 days are overstated
     about 2×.
   - Vol-of-vol scales as level^1.06–1.97 (Heston gives 0, lognormal 1, the 3/2 model 2).

   Candidates: log-OU for VXN and VXD, a 3/2-type model for VIX and OVX. Positivity comes from
   the dynamics, so the floor goes away.

   **Tested 2026-11-06, research only** (`BSM_Spectral/bsm_spacetime.py`,
   `reports/spacetime_greeks_2026-11-06/`). Log-OU has the lowest AIC on all four indices,
   VIX and OVX included, and the 3/2 fit fails on VXD. Walk-forward on a 63-day digital's 5-day
   model P&L, log-OU beats the random walk on pinball loss by 2.0–5.2% on every index, each at
   Holm-adjusted p < 0.0001. All dynamics are too wide (breaches 0–0.8% against 1%), so the
   ranking holds but the calibration does not. Next: swap the random walk in `var_snapshot`
   for log-OU, which moves the gate's version hash, and confirm on held-out windows as 12.1
   does. One timescale explains only part of the variance-ratio drop (model 0.62–0.88 against
   data 0.36–0.55), which points at item 3.
2. **Clock.**
   - Crypto weekend days carry 0.54 (BTC, CI 0.44–0.63) and 0.66 (ETH) of a weekday's variance,
     but `year_frac` counts calendar days equally.
   - Kalshi's 09:00 quotes imply 0.47–0.62× of the vol index for the same day.

   Replace the learned vol scale with a measured operational-time clock.
3. **Long memory in volatility.**
   - GJR persistence is 0.97–0.998.
   - The |r| autocorrelation at lag 126 sits above the model's median for 7 of 8 underlyings
     (outside the band for BTC).
   - Vol-index Hurst is 0.30–0.41, against 0.42–0.48 from mean reversion alone.

   Candidates: a multi-timescale OU superposition, a multifractal random walk, rough vol. Daily
   data cannot separate roughness from quote noise.
4. **WTI.** The price went negative (-37.63), kurtosis is 85 and intermittency 0.28 (both
   outside the band), and Kalshi settles on futures while the model uses spot. Use a two-factor
   Schwartz–Smith model in arithmetic coordinates. **Data-blocked:** it needs daily front-month
   futures, which FRED doesn't carry.

Not supported by the data: momentum or inertia (price variance ratios sit inside the model
bands), and daily jumps beyond FHS except WTI. Microstructure is deferred by user decision.

## 13. Open items after the intel-hook work  🔴 START HERE (2026-11-01)

Recorded after `5b80db6` on `feat/backlog-routing-triggers`. In priority order; the spectral tail index
is in 12.1 and the post-GBM queue in 12.2.

### Fix first: the intel feed is down

0. 🔴 **WorldMonitor has moved, so the overlay and persistence gate have no feed.** Checked 2026-11-01:
   `initialize` at `DEFAULT_URL` (`intel/worldmonitor.py`) returns `410 Gone` with "Use
   https://worldmonitor.app/mcp". `__aenter__` raises, so every refresh fails and the provider has no read:
   every runner behaves as if the overlay were off, while its startup line says it is on. No new
   `elevated_in` edges get written either. The key is set. The new host answered a keyless `initialize` and
   `tools/list`; the key has not been sent there, pending the user's OK.

### Fix before any live or autonomous run

1. 🔴 **`QUESTRADE_ENV` selects nothing.** Worse than the audit's "runtime override is a no-op". From
   reading the code, not tested against Questrade:
   * `brokers/questrade.py:23` hard-codes `LOGIN_HOST`, and `from_settings` takes no environment. The
     refresh token alone decides which account is traded.
   * `autonomous_run` sets `os.environ["QUESTRADE_ENV"]`, and nothing reads it afterwards.
   * `AUTONOMOUS_ACCOUNT=practice` is therefore only a label, and `live`'s "routing through practice
     account" message is not a fact the code checks.
   * The "confirm `QUESTRADE_ENV=practice`" go-live check in `CLAUDE.md` proves nothing until this is fixed.

   Do: route to Questrade's practice login, or refuse to start when the account type can't be
   confirmed. Check Questrade's docs for the host. This matters more now that real-money runners can
   size an order up to 3×.
2. 🔴 **The overlay loosens in an outage, and its halt cannot fire. Needs the user's decision.** Simulated
   on 2026-11-01, on stub snapshots:
   * Heaviest stress tried: scalar 0.25, `halt_new_entries` False. `OverlayConfig` floors the scalar at
     0.25 and halts at 0.20 or below, so the halt is unreachable. "Halt gate" wording in `paper_ib.py`
     and `live_loop.py` overstates it.
   * Every vendor call failing: scalar 0.80. `snapshot()` catches each failure and returns a degraded
     snapshot with no readings, and its decisions replace the last good ones. The last read is kept
     only when the fetch itself raises.

   The user chose "fail open" on the description "keeps the last good read", so ask again. Options: hold
   the prior decisions while degraded, or take the tighter of the prior scalar and the cap; separately,
   lower the floor below `halt_below` if the halt should be live. The wrong wording also remains in the
   message of commit `5b80db6`, which is merged and stays as it is; the docstring and help text are fixed.
3. 🔴 **The ledger can't explain a sized or suppressed order.** It records the final conviction and a
   halt reason, not the overlay scalar, the allocator bias, the thesis trims or the persistence reason,
   so a 3× boost has no recorded cause. `audit/versioning.py`, `audit/ledger.py` and `audit/replay.py`
   never mention the hooks, so the risk-check version doesn't move when they change. This falls short of
   the `CLAUDE.md` audit rules, on the real-money paths.
4. 🔴 **CI skips the risk code.** `.github/workflows/test.yml` runs four TradeCard test files. A green PR
   says nothing about the Router, the gates, `tests/test_live_parity.py` or
   `tests/test_intel_hooks_wiring.py`, and the BSM self-tests never run in CI although the spectral gate
   imports that engine. The workflow says it is deliberately narrow because the full run touches
   network-facing paths, so widen it to the offline risk tests rather than to everything.
5. 🔴 **`brokers/ib_web.py:509` auto-confirms IB warnings in an uncapped loop.** Still open. In live mode
   it can accept margin or exchange warnings a human should see, and it can spin on a misbehaving API.
6. 🟡 **A slow feed can hold up a real-money poll.** From reading the code, not measured. The overlay
   refreshes inside `LiveMonitor.step`, on an entry evaluation, with about 8 vendor calls in sequence at
   a 40-second timeout each. That could block the rest of the poll, exits included, for around 5 minutes.
   Refresh on a background thread or between polls.
7. 🟡 **`config/settings.py:258` caches `get_settings()`.** Still open: a change to the environment or
   `trading.yaml` after the first call is not seen. `autonomous_run` writes to the environment after that call.

The other items on the 2026-09-04 critical list were rechecked in code on 2026-11-01 or landed earlier:
NaN ATR, `KillSwitch.evaluate` callers (paper and `risk/equity_watch.py`), the README live snippet, the
cache key, the interval lexicon, the conviction clip, and the intel hooks.

### Finish the intel-hook work

8. **Stale-run warning.** `PersistenceGate` ignores a run older than `max_age_hours` silently. Log it, so
   a dead poller is visible rather than looking like a calm market.
9. **Persistence domains: decided 2026-11-01.** The desk does not seek to profit on disasters.
   * `disaster` is out of `_CLASS_TO_DOMAINS` and stays unwritten. Keeping it unwritten also keeps the
     "Disaster / insurance underpricing" thesis in `interpret.py` dormant, since it needs disaster
     acceleration. That thesis calls cat-exposed insurers "a specific bet" and the agent layer's
     `disaster` domain maps to the same themes: **ask the user whether to remove both.** The overlay's
     `_disaster_gate` stays, because it only trims.
   * `economy` is **half done and cannot fire on live data yet.** `intel/graph.py` writes its
     `elevated_in` edge at `ECONOMY_ALERT_THRESHOLD` (3 alerts, uncalibrated) from `category_alert_counts`,
     and the gate reads it for `fx` and `fixed_income`. But `worldmonitor._build_snapshot` never fills
     `category_alert_counts`, so that edge, and the overlay's existing `_econ_gate`, stay silent. Adding
     `economy` to `INTEL_DOMAINS` is not the fix: `get_intel_timeline` allows only conflict, military and
     energy, and a rejected call marks every snapshot degraded. Next: with item 0 fixed, read the real
     news payload and fill the counts, or build the intel hook on `get_economic_data` or
     `get_macro_history`. Don't guess the payload's shape.
10. **Allocator weights go stale.** `allocator_weight_bias` computes once at startup and never refreshes,
    so a long-running daemon keeps old weights.
11. **Hook settings aren't in `trading.yaml`.** `min_polls`, the 6-hour stale limit and the allocator range
    are hard-coded, and the daemon's only overlay opt-out is removing the key.
12. **Thin test coverage on the real-money paths.** `intel_overlay_hooks` has no test with a key set, and
    `live` and `autonomous_run` are checked by reading their source, because tests may not use live mode.
13. **One real agent call.** Run `debate()` on two or three made-up headlines with `domains=("energy",)`,
    at most two API calls, and record `stop_reason` and usage. This is the first check of the model id and
    the reply parsing against the real API. The layer stays unwired. Tried 2026-11-03: the key is in `.env`,
    and the first request returned 400 "credit balance is too low", so model and parsing remain unverified.
    Rerun once the Anthropic account has credits. Note `debate()` returns an empty list on that error, which
    reads like "no thesis" unless you see the `agents.api_error` log line.
14. **Accrue the data.** Run an overlay provider or `scripts/graph_journal.py` so `elevated_in` edges exist.
    Until then the persistence gate cannot fire, and nothing validates the overlay, the allocator or the
    gate on real data. Tiers 4 and 5 in section 9 need about 30 days and 21-day windows from that start.
15. **Thesis trims cover little of the book.** `THEME_EXEMPLARS` overlaps 8 of the 32 validated symbols and
    1 crypto pair (PAXG/USD), so most names, BTC and ETH among them, can never be trimmed by a thesis.

### Housekeeping

16. **Failing test.** `tests/test_crypto_sleeve.py::test_crypto_sleeve_is_well_formed` expects 7 symbols, and
    the sleeve holds more (AAVE/USD among them; section "Live processes" says 13). Decide which is right.
17. **Type check.** `mypy` is a dev dependency but is not in `.venv`, so `mypy --strict` has not run on
    this work or on `risk/spectral_var.py`.
18. **Reach `main`.** `5b80db6` merged into `feat/backlog-routing-triggers` through PR #4 as `e1072bf`, and
    gets to `main` through PR #3, which was still open on 2026-11-01. Anything new needs its own PR: a push
    to that branch lands directly in #3.
19. **Check the first maturation run.** The scheduled task now runs from `BSM_Spectral/` inside this repo.
    Its first run there is 2026-11-02 09:00, and it journals the new `-path` challengers for the first
    time. Read `BSM_Spectral/journal/run.log` afterwards.
20. **`bsm_detect.py` permissions.** Its commands are not in `BSM_Spectral/.claude/settings.json`. The user
    adds these; Claude is blocked from editing its own permissions.
21. **Unanswered question.** "Unilateral contract", said during the Kalshi work, was never explained.

## 14. Order types: minimise spread and slippage  🟡 SCOPED, NOT BUILT (2026-11-01)

**Why.** Every order is a market order, so every entry and exit pays the spread and takes whatever slippage comes.
That cost is now visible rather than hidden: paper crosses the spread as of `monitor/session.py`, and
`trading audit query fill-quality` measures slippage and fees per stream against the decision price.

**What is there today, from reading the code:**
* `execution/router.py` hard-codes `OrderType.MARKET` in `Router.submit`. `OrderIntent` cannot ask for anything else.
* `Order` already carries `limitPrice`, `stopPrice` and `timeInForce`, and `TimeInForce` is Day, GTC or GTD.
* Questrade: `to_questrade_payload` sends limit price and time in force. Kraken: `_order_type_wire` maps limit and
  stop. IB socket: `IBAlgoOrderType` lists `LMT`, `MIDPRICE`, `PEG MID`, `VWAP`, `TWAP` and more. IB web sends a
  limit price. None of this has run for real.
* `PaperBroker` never reads `orderType`: every order fills at once and in full.
* Nothing manages a working order. `Router.cancel` exists, but nothing watches an order that has not filled, and
  the monitor's in-flight guard only stops a repeat entry for a few polls.

**Proposed order styles**, set per venue and per asset class in `trading.yaml`, so paper and money read the same knob:

| Style | Order | Cost | Risk |
|---|---|---|---|
| `market` | As today | Full spread plus slippage | None missed |
| `marketable_limit` | Limit at the far touch plus a cap, IOC | Spread, slippage capped | Part-fill or miss if price runs |
| `passive_limit` | Join the near touch, post-only, reprice up to a limit, then cross or give up | Can earn the spread and maker fees | Misses, and adverse selection: it fills when price moves against it |
| `venue_algo` | IB `MIDPRICE` or `PEG MID`, where the venue offers it | Near the mid | Venue-specific, harder to simulate |

Exits stay urgent: forced exits and anything sent under a tripped kill-switch stay marketable whatever the style.

**Decisions to make first:**
1. How far may an entry chase before it gives up? A missed trade has a cost too, and the backtests assume a fill.
2. How long does a passive order rest, and what does it do then: cross, or drop the signal?
3. Which style per venue and asset class? Start from what `fill-quality` measures, not from a guess.
4. Does the card sign a limit? The signed canonical carries `entry`; a repriced order must not exceed what was
   approved, or it must go back to the card.

**Engine layers it touches:**
* **Intent and Router:** an `order_style` on `OrderIntent`, a limit price derived from a fresh quote, and a gate
  check that the limit still fits the size caps.
* **Execution:** a working-order manager that polls, reprices, cancels and always leaves nothing resting when a
  session stops. Time-in-force per venue. Check each venue's post-only and IOC support in its docs first.
* **Paper:** a limit fills only if the market trades through it, with a haircut for queue position. Without this
  paper would flatter passive orders and stop translating, which is the point of this work.
* **Audit:** new ledger events (`ORDER_WORKING`, `ORDER_REPRICED`, `ORDER_UNFILLED`), added to `LedgerEvent` first;
  recon for part-fills; `fill-quality` split by style.
* **Monitor:** the in-flight guard and the once-per-bar level rule both have to understand a working order.
* **Backtest:** `CostModel` by style, so research costs match.

**Sequence.** (a) Accrue a baseline: at least a week of paper on two venues, then `fill-quality`, per the data-first
rule. (b) `marketable_limit` first: it caps slippage and is the smallest change. (c) The working-order manager and
`passive_limit`. (d) Venue algos.

**Done when:** all-in cost per stream, measured by `fill-quality`, falls against the market-order baseline; the miss
rate is reported beside it, since a cost saved by not trading is not a saving; paper and money agree within a stated
band; no order is left working after a session stops; and every order re-verifies and replays.

## 15. Engine wiring: what changed and what is left  🟢 BUILT (2026-11-01)

`docs/ENGINE_WIRING.md` goes through it layer by layer. Every runner now goes through `monitor/session.py`. Closes
section 13 items 1 (the token's environment is checked, since the practice login's token endpoint returned 404), 3
(ledger `sizing` and `SESSION_PLAN`), 4 (CI runs the risk tests and the spectral self-test), 5 (IB prompts reject by
default, are bounded, and an error or a missing order id no longer reads as placed) and 6 (the overlay refreshes on
its own thread).

Also new: money runners for Kraken and IB; `trading paper --venue`; `AUTONOMOUS_ACCOUNT=paper`; paper fills crossing
the spread with per-venue costs (assumptions until measured); Kraken equity and positions covering the whole book;
the daily budget counting only its own mode; `trading doctor`; and `fill-quality` and `session-plans` queries.

**Decisions still open, yours:**
1. A drawdown in a paper session trips the one `state/HALTED` file, which halts a money session sharing `state/`.
   Recommended: a separate paper sentinel, with `trading kill` still stopping everything.
2. Home currency. Questrade equity is in the account's currency and a US name is priced in USD, unconverted. A session
   warns; the gates do not convert. This is exchange hopping level 3.
3. Overlay outage behaviour: section 13 item 2.
4. Paper's cost defaults are assumptions. Replace them from `fill-quality` once real fills exist.

**Standing rule from the user, 2026-11-01:** no removals from this build without their express permission.

## 16. Questions for the user  🔴 ANSWER FIRST (2026-11-01)

Each has a recommendation. Answers go here or in chat.

**Answered 2026-11-03, and acted on:**
* **1. Paper may halt money only after express approval.** A simulated book trips `state/HALTED_PAPER`, which stops paper. Money
  reads only `state/HALTED`. An alert asks; a human approves with `trading kill --from-paper --confirm "HALT MONEY"`.
* **2. Home currency CAD.** Every runner prices quotes, positions and equity in CAD at the Bank of Canada's daily rate. A fill
  keeps its own currency. See the gaps in `docs/ENGINE_WIRING.md`.
* **3. 100,000 notional.** `paper_starting_equity` stays at 100,000, read as equity: with leverage capped at 1.0x that is also the
  most notional the book can hold.
* **6. IB trades derivatives and international markets.** North American equities are refused there; overseas listings and futures
  pass. `ib_futures_only: true` narrows it back. No runner trades options yet, so that part is permitted but has no path.
* **22. TFSA only, confirmed.** TFSA trades will be supplied as a CSV; spouse and RRSP accounts are declared out of scope, so the
  superficial-loss test cannot see their buy-backs. `trading audit export --affiliated <file>` takes
  `account,symbol,side,quantity,time[,venue]`. **The file itself is still needed.**
* **24. Keep** `Components\BSM_Spectral.pre-subtree.git`.

Section 13, same day: **7** fixed (`get_settings` notices a change to the yaml, `.env` or the environment). **8** fixed (a stale run
is logged). **10** fixed (`allocator_refresh_seconds`, hourly by default: weights recomputed between polls, last good kept on a failure,
each new set in the ledger as `ALLOCATION_REFRESHED`). **11** fixed (`persistence_min_polls`, `persistence_max_age_hours`,
`allocator_bias_floor`, `allocator_bias_cap`; `intel_overlay: false` already turns the overlay off for the daemon). **12** see
`tests/test_money_paths.py`; the user decided Questrade's HTTP layer stays untested, keeping the `CLAUDE.md` rule against building a
Questrade broker in tests. **15** accepted as it is. **19** see below. **20** done: `bsm_detect.py` is in
`BSM_Spectral/.claude/settings.json`, and the maturation script's allowed path is corrected.

**Section 13 item 19, the maturation run.** The scheduled runs on 2026-11-02 and 11-03 both logged a header and `^C`, and the task's
last result is `0xC000013A` (console closed or Ctrl-C), so neither did any work. Run by hand on 2026-11-03 at 12:18 ET the same script
finished with "all stages ok", so the script is fine and the scheduled start is what dies. That run's Kalshi quotes are past the
09:00 pre-open window, so they calibrate the model but do not count in the market-versus-model comparison. What kills the 09:00
console is not known; the task is set to "Interactive only". Running it hidden needs the user's password, so it is theirs to change.

The port of `FRM-Claude` (`docs/PORT_FRM_CLAUDE.md`, 2026-11-02) settles two of these and changes a third: **6** IB is
futures-only, enforced (`ib_futures_only`); **13** Kraken trades fractional units in its own lots; **14** fees are now the venue
cost model (Kraken 26 bps, not 40), and only the 5 bps slippage is still unmeasured. **2** is not settled: the CAD normaliser
arrived but only `paper_global.py` uses it.

Found while testing the port, 2026-11-02: `api.worldmonitor.app` answers 410 and the new host `worldmonitor.app/mcp` answers
403 on three tools with the key in this environment, so the overlay reads as degraded and runs on what is left. That is
question 4's case happening now, not a hypothetical. Check the key when the others are fixed.

### Before any money session

1. **Should a paper drawdown be able to halt money?** Paper and money share one `state/HALTED` file, so today it can.
   *Recommended:* a separate paper sentinel, with `trading kill` still stopping everything.
2. **What is the home currency for the gates?** Questrade equity is in the account's currency and a US name is priced in
   USD, unconverted, so the gates compare unlike amounts. A session warns but does not convert.
   *Recommended:* CAD, the setting's default, converted at the Bank of Canada rate the audit layer already fetches.
3. **How much do you plan to trade?** Set `paper_starting_equity` to it. Rounding, the minimum ticket and fees all scale
   with account size, so results at the $100,000 default will not translate to a smaller account.
4. **What should the overlay do in an outage?** A full vendor outage can loosen a 0.25 trim to the 0.80 cap, and at the
   default floor its halt cannot fire (section 13, item 2).
   *Recommended:* hold the last good read while the feed is degraded. Separately: should the halt be reachable?
5. **Is `--allow-screened` enough for Kraken money?** The sleeve is in-sample only and marked paper-only, and that flag is
   the only thing in the way once keys are set.
   *Recommended:* stay on paper until the deep-history walk-forward clears it.
6. **What should IB money be allowed to do?** The desk split says IB carries futures, but only the basket gate enforces it,
   so `trading live --venue ib` would trade an equity. Warning prompts reject orders by default
   (`ib_auto_confirm_prompts`).
   *Recommended:* keep prompts off until you have seen which ones IB sends, and refuse equities on an IB money session.
7. **Should forced exits be on?** `forced_exits` is off everywhere. Being one knob, turning it on for paper also turns it
   on for money unless the file is changed between runs.
   *Recommended:* on for paper now, to gather evidence; decide for money from the sweep in the risk-guard report.
8. **What should the daemon's default account be?** `AUTONOMOUS_ACCOUNT` defaults to `practice`, which now refuses to start
   on a real token, so the default does nothing useful. `CLAUDE.md` calls it the default and reset value.
   *Recommended:* make `paper` the default and reset value.

### Order types (section 14)

9. **How far may an entry chase before it gives up?** A missed trade has a cost too, and the backtests assume a fill.
10. **How long does a passive order rest, and then what:** cross the spread, or drop the signal?
11. **Which style per venue and asset class?** *Recommended:* decide from a week of `fill-quality`, starting with
    `marketable_limit`.
12. **Does the card sign a limit?** A repriced order must not exceed what was approved. *Recommended:* the card signs the
    worst acceptable price, and anything beyond it goes back to the card.

### Sizing and costs

13. **Should crypto trade fractional units?** Orders are whole units in both modes. With the 50% single-name cap, BTC cannot
    open at all unless half the account covers one coin. *Recommended:* yes for crypto. It touches `OrderIntent.shares`
    and the gate's trim, so it is its own piece of work.
14. **Are the paper cost defaults acceptable until measured?** Questrade $4.95 per order, Kraken 40 bps (from memory,
    unchecked), IB $1.00 per order, all plus 5 bps slippage. *Recommended:* check them against your actual fee schedules now,
    then replace them from `fill-quality`.

### The no-removals rule

15. **What is the rule's exact scope?** Your words: "There'll be no removals from this build without my express permission
    for reasons not statistical or those of social-policy or competition...." The sentence trails off, so it is being read
    conservatively: nothing goes without asking, whatever the reason. Is a statistical reason ever enough on its own?
16. **Can `test_crypto_sleeve` be updated to the 13 symbols the sleeve holds?** It expects 7 and has been failing since before
    this work. *Recommended:* update the test and keep every symbol.

### Getting it to `main`

17. **Which keys will you add, and when?** `trading doctor` lists them: Questrade's two, Kraken's two, Telegram's two, and
    `ANTHROPIC_API_KEY` for the one real agent call (section 13, item 13).
18. **In what order do the PRs go?** PR #3 is open with Auto-fix on, and `feat/runner-parity` is committed locally on top of
    it, not pushed. *Recommended:* merge #3, then open a PR for this branch against `main`.
19. **Does a market-data subscription belong in the budget?** Exchange hopping level 2 needs one.

### Left over from the audit work (`docs/compliance/AUDIT_SCOPE.md`)

20. **Which key signs the daily anchors?** Git has none configured here. Pass `--signing-key` (or `--ssh`), or use
    `--no-sign`. No anchor exists yet, because no session has written a ledger day.
21. **Where does the retention archive go?** It should be outside OneDrive.
22. **Will you supply spouse or RRSP/TFSA trades as a CSV?** The superficial-loss test cannot see them otherwise.
23. **Who holds the copyright for a LICENSE file?** `pyproject.toml` names "pouria" as the author, so none was added.
24. **Can `Components\BSM_Spectral.pre-subtree.git` be deleted?** It is the backup from the subtree merge. Under the
    no-removals rule it stays until you say so.

## 17. GraphRAG search and thesis signals  🟢 PHASES 1–4 BUILT (2026-11-04)

**Goal.** Build search into our GraphRAG (not CAMEL-OASIS, whose `AgentGraph` has no traversal and whose
`search_posts` is an unranked SQL `LIKE`), so theses can influence and post entry and exit signals on top of
the composites and microstructure pipelines.

**User decisions, 2026-11-03.**
* A thesis may confirm or veto entries, post exits, and originate entries, with originating gated hardest.
* Promotion from shadow log to acting: the walk-forward gate, then the user's approval. Paper and money stay
  one build, and everything still goes through the Router.
* Search stack: SQLite, networkx, BM25.
* Microstructure feeds the fusion directly, as a new `mx_*` lens family. **This changes two standing rules,
  neither yet changed in code:** the risk-lens "no price or volatility inputs" rule (2026-10-12, enforced by
  `assert_no_price_inputs`) and the 90-day L2 wait in "Data-first sequencing" (2026-09-08). Narrow the assert to
  allow `mx_*` in phase 8 and keep the price-free audit as a baseline. Lens results need redoing then.
* `disaster` posts no signals.

| Phase | What | Done when | Needs |
|---|---|---|---|
| 1 ✅ | `intel/graph_store.py`, `microstructure/l2_recorder.py`, `scripts/record_l2.py` | No future edge can be returned | Nothing |
| 2 ✅ | `intel/graph_search.py`: `neighbors`, `bfs_paths`, `shortest_path`, `temporal_walk`, `personalized_pagerank`, `centrality`, `communities`, `bm25_search`, `co_persistence`, `first_seen`, `thickening_rate`, `trending_nodes`, `reciprocal_rank_fusion`, `mmr_diversify` | Tested on fixed sample graphs, all through a `GraphView` | `graph` extra (`networkx`, `scipy`) |
| 3 ✅ | `intel/retrieval.py`: `retrieve_evidence`, `ViewTools`, `ThesisMemory`, `debate_on_view`; `run_debate` in `intel/agents.py` | A mocked debate cites its evidence paths | Anthropic credit for real calls |
| 4 ✅ | `intel/thesis_signal.py`: `ThesisSignal`, `thesis_to_signals`, `fired_to_signals`, `ThesisBook`, `run_shadow_pass`; `scripts/thesis_shadow.py`; `THESIS_SIGNAL` ledger event | Signals logged, acting on nothing | Live feed for real signals (section 13, item 0) |
| 5 | `promotion_gate`, replay | Walk-forward results per thesis type | Weeks of shadow data |
| 6 | `ThesisOverlay`: confirm, veto, exit | Works identically on paper and money | Gate and user approval |
| 7 | `ThesisStrategy`: originates entries | Strictest gate passed | Gate and user approval |
| 8 | `micro_features`, `mx_*` in fusion | Lens audit redone | Recorded L2 |

**Phase 1 notes.** The store is derived from the journal and not on the trading path yet. Its reads go only
through `GraphView._select`, which applies the cutoff; a new reader must be added to `_every_read` in
`tests/test_graph_store.py`. Removing the guard fails 9 of its 22 tests (checked by hand). The recorder is public
data only and is not running: nothing schedules it, so the L2 clock has not started until someone runs
`python scripts/record_l2.py`.

**Phase 2 notes.** Every search takes a `GraphView`, and BM25 indexes per call so rarity statistics are
point-in-time too. A new search must be added to `SEARCHES` in `tests/test_graph_search.py`. With the cutoff guard
removed, 13 of the 14 future-edge tests fail (checked by hand); `first_seen` rightly gives the same answer.
`SearchScope` leaves out our own trades, simulated societies, analysis outputs and poll nodes; `SIGNAL_SCOPE` also
drops `disaster` and its events. For phase 3:
* **Speed.** Each search rebuilds from the view: about 3 to 6 seconds at 100,000 edges. `retrieve_evidence` runs
  several, so it should build once per view. Measured on a synthetic graph only.
* **News titles.** `mentions` events (`news:<source>:<hash>`) carry no title in the graph, so BM25 finds them by
  symbol only. Join `state/news.jsonl`, by `as_of`, when building evidence.
* **Domain elevation** is not in the evidence graph, because `elevated_in` hangs off a poll node. Read it through
  `view.persistence` and `co_persistence`.
* **`uv.lock`** does not have the `graph` extra: `uv` is not installed here. Run `uv lock` where it is.
* **Evidence is empty today.** The real journal holds 27 edges, all `traded`, so every search on it returns nothing.

### 17.5 Pick up here: review the direction table, then phase 5  🔴 NEXT

**State, 2026-11-04.** Phases 1 to 4 are on `feat/graphrag-search`, at `745c1d6` plus this note, not pushed and not in
`main`. That branch was cut from `main` at `e9b8aa7`; `feat/runner-parity` holds the older copies of the same work and
can be ignored. Full suite on that code: 2,657 passed, 2 skipped, 0 failed. It takes 7 to 11 minutes, so run it in the
background: `.venv/Scripts/python.exe -m pytest -o addopts="" -q`.

**1. The user reviews this table, then bump `DIRECTION_TABLE_VERSION` off `draft-`.** It decides what phase 5 scores, so
settle it before signals accrue: a change later splits the record. Constructive logs `confirm` and `entry`, adverse logs
`veto` and `exit`, unsigned logs only `watch`.

| Thesis | Theme | Direction | From its wording |
|---|---|---|---|
| Complacency divergence | `volatility_convexity` | constructive | "Favour cheap convexity" |
| Complacency divergence | `safe_haven` | constructive | **Weakest call:** only implied, and it asks a wider margin on new risk in these themes |
| Energy event concentration | `energy`, `materials` | unsigned | "in both directions" |
| Conflict escalation watch | `defense_geopolitical`, `safe_haven` | unsigned | "as a hedge on escalation, not as a directional bet" |
| Sentiment stretch — greed | `volatility_convexity` | constructive | Greed "thins the buffer against bad news" |
| Commodity carry-inversion proxy | `energy`, `materials` | constructive | "producers positioned for physical scarcity", but also "Do not size on this alone" |
| Dollar strength divergence | `dollar` | constructive | "the risk asset gives first" |
| Dollar strength divergence | `materials`, `emerging_markets` | adverse | "Prefer trimming into commodity / EM strength" |
| Dollar weakness divergence | `dollar` | unsigned | Treats the dollar's fall as a given, not a call on it |
| Dollar weakness divergence | `materials` | adverse | "the driver is demand destruction" |

**2. The user unblocks the real data.** Nothing below proves anything until these are done:
* Approve sending the WorldMonitor key to `https://worldmonitor.app/mcp` (section 13, item 0). The address came from a
  server response, and the user has said "not yet". The client still points at the old one.
* Add Anthropic credit, then run the first real debate (section 13, item 13). Tool use is unproven against the real API.
* Run `python scripts/thesis_shadow.py` regularly. Nothing schedules it, because of the no-new-scheduled-tasks rule in
  "Data-first sequencing". The user may lift that for this one job. The forward record only grows while it runs.
* Say whether to start `python scripts/record_l2.py`, which the user has so far left off. Phase 8 waits on its history.

**3. Phase 5: `promotion_gate` and replay.** Can be built on stubs now; it cannot judge anything for weeks.
* Score each thesis type and each action separately, on forward returns over the signal's `horizon_days`.
  `analysis/labeling.py` and the `risk_lens.verdict` date-rotation null are the pieces to reuse, not to rewrite.
* Count only rows with `replay` false, recorded in the ledger before their outcome. Check the chain before trusting them.
* `watch` is never promotable. `entry` gets the strictest bar. Passing makes a type eligible, and the user's approval
  promotes it: gate first, then approval, never the gate alone.
* The lens has run with no multiple-testing correction since 2026-10-30, and a verdict passes on its raw p-value. With
  many thesis-action pairs, about 5% of null ones will pass by chance, so report how many were tested.
* Keep `ThesisSignal`'s shadow-only check until phase 6 changes it on purpose.

**Open decisions.** The `disaster` specialist and thesis stay unless the user says otherwise (section 13, item 9).
`enrich_with_agents` still cannot see debate errors. `uv.lock` lacks the `graph` extra, and mypy is not in `.venv`.

### 17.4 Phase 4 as built  🟡 TABLE AWAITS REVIEW

Signals are rows in the ledger's `thesis` stream, so they cannot be edited or added after an outcome without
breaking the chain. A pass for a past moment is marked `replay` and stays out of `ThesisBook.signals()` by default;
phase 5 must never count one as forward evidence. An adverse read logs `veto` and `exit`, a constructive one `confirm`
and `entry`, and an unsigned one `watch`, which must never be promotable. Nothing on the order path reads any of it, and
tests check that by import and by source.

**`THEME_DIRECTIONS` is a draft awaiting the user's review (`DIRECTION_TABLE_VERSION`).** Rule theses state no
direction, so the table supplies one per thesis and theme, from each thesis's own wording. Bump the version on any
change. `Thesis.direction` is not used for rule signals: its `adverse` default only exists to make them trim.

**Not done, and known.**
* No real signal exists yet: the WorldMonitor feed is down, so `run_shadow_pass` records nothing and says why.
* Nothing schedules a pass. The standing rule in "Data-first sequencing" says no new scheduled tasks, so it runs by hand:
  `python scripts/thesis_shadow.py`. The forward record only grows while someone runs it.
* Rule signals land on `THEME_EXEMPLARS`, which covers little of the book (section 13, item 15).
* `strength` is uncalibrated: band weight times `intensity` where modelled, or the agent's self-reported confidence.

### 17.3 Phase 3 brief: evidence retrieval, agent search, memory and trace  ✅ BUILT 2026-11-04

**As built, where it differs from the brief below.**
* The trace extends `state/intel_agents.jsonl`, which the port added, rather than a new `thesis_trace.jsonl`:
  rows now carry `target`, `evidence_hash`, `evidence_ids` and `errors`, and each domain has `calls` with model, prompt
  hash, `stop_reason`, usage and tool calls. `ThesisMemory` reads the same file.
* Sharing one graph is `view.cached()`, which remembers at `_select`, so it cannot go round the cutoff. Hold one for
  one piece of work: it will not see later arrivals.
* `debate()` still returns a list. `run_debate()` returns a `DebateReport` whose `errors` names each domain that
  failed (`no_api_key`, `http_error`, `api_400:invalid_request_error`, `refusal`, `no_text`, `unparseable`,
  `tool_turn_limit`). `intel/interpret.py`'s `enrich_with_agents` still calls `debate()`, so it does not see them.
* Not verified: no real API call has run. Tool use is tested only against a mocked API, so the tool-turn shape,
  thinking blocks and the model's real behaviour are unproven until the Anthropic account has credit.

Next is phase 4, the shadow journal. It needs the live feed. Original brief follows.

**Goal.** Give the debate real evidence: found by search, cited by path, remembered across debates, and fully
traced. It stays unwired, and nothing here posts a signal. Phase 4 starts that, in shadow.

**Build (new `intel/retrieval.py` unless noted; names are proposals):**
1. `retrieve_evidence(view, target, *, budget, scope)`. `target` is a symbol, a domain or free text. Runs
   `personalized_pagerank`, `bm25_search` and `trending_nodes`, merges with `reciprocal_rank_fusion`, diversifies
   with `mmr_diversify`, and returns an `EvidenceBundle`: items with title, sources, the path that found each one,
   and a content hash. Default scope is `SIGNAL_SCOPE`, so `disaster` stays out of anything that can become a signal.
2. **Build once per view.** Let searches share one built graph. Each rebuilds today, at 3 to 6 seconds per 100,000
   edges, and retrieval runs several. Must not weaken the rule that everything reads through a `GraphView`.
3. **News titles.** `mentions` events carry no title. Join `state/news.jsonl`, using only headlines whose
   `arrived_utc` is before the cutoff. That file sits outside the store, so this join needs its own future test.
4. **Domain state.** Add `view.persistence` and `co_persistence` readings to the bundle, because `elevated_in` hangs
   off poll nodes and is not in the evidence graph.
5. **Agent tools (`intel/agents.py`).** Let `SpecialistReader` and `Adversary` call search during a debate: text,
   neighbours and paths, all bound to one view, with a cap on calls per debate. An agent must be unable to widen its
   cutoff or scope. Tool results are untrusted text. `claude-api` is the skill to load first: thinking counts against
   `max_tokens`, and forced tool choice returns a 400 on current models.
6. **Thesis memory.** `ThesisMemory`: earlier claims and verdicts, shown to agents only if made before the cutoff.
7. **Trace.** An append-only `state/thesis_trace.jsonl` (gitignore it): evidence hash, model, prompt hash, `stop_reason`,
   usage, claim and verdict for every debate, so a debate can be audited and rule-based theses replayed in phase 5.
8. **Failure must be visible.** `debate()` returns `[]` on an API error, which reads like "no thesis fired". Return
   or record the difference. Found 2026-11-03, when low credit looked like an empty result.

**Done when:** a mocked debate on a sample graph cites evidence paths that exist in the view; adding future edges or
future `news.jsonl` rows changes nothing; `disaster` events never reach a bundle under `SIGNAL_SCOPE`; a failed call
is told apart from an empty one; and the full suite is green.

**Needs from the user.** None to build it on stubs and a mocked API. For real runs:
* Anthropic credit (section 13, item 13).
* The WorldMonitor switch or a running news collector, because the real graph holds no usable evidence (item 0).
* A call on whether the `disaster` specialist and thesis stay (item 9). Under the no-removals rule, leave both.

**Keep in mind.** `.env.example` looked to hold two real credentials as of 2026-11-03, so stage by path. `uv.lock` lacks
the `graph` extra. `mypy --strict` has not run on phases 1 and 2, because mypy is not in `.venv`.

---

# Brought by the port: FRM-Claude `feat/multi-scoring-attention-map-wq7f42` handover (2026-10-04)

Their notes, unedited. Where a line disagrees with the sections above, the sections above are later.

Notes: `uv` isn't on PATH on this machine; use `.venv/Scripts/python.exe`. QT launched after
16:00 ET opens nothing. ARX.TO and RIG.TO were removed for 404-on-candles; pre-flight symbol
validation now catches that class of failure at launch.

## Queue — correctness bugs

- ✅ **Paper broker didn't book realized P&L on a partial sell — FIXED 2026-09-24.** `_apply_fill`
  now realises `(fill − avg) × closed_qty` on any reducing fill (average entry unchanged), handles a
  fill that crosses zero, and keeps a legacy full-closes-only accumulator so `resume` accepts a
  journal written before the fix instead of refusing to start — `state/` is not rewritten, and the
  corrected figure is used with a warning. Replaying real session `fba831e3` reproduces the measured
  **−$39.92** of unbooked P&L exactly. Tests: partial books P&L, partial+close == one close, resume
  round-trip, legacy journal accepted, garbage journal still refused. **Sessions started before the
  fix keep the old behaviour until restarted.** Original report:
  `brokers/paper.py` `_apply_fill` adds to `_realized_pnl` only in the `new_qty == 0` branch (a full
  close). A partial reduction lowers `openQuantity`, keeps `averageEntryPrice`, and moves the
  proceeds into cash, so **equity is correct**, but the sold shares' P&L never reaches the
  `realized_pnl` column of `paper_equity.csv`. That column then understates realized P&L.
    * **Who does partial sells now:** `LiveMonitor.trim_to_slots` (`--trim-to-slots`, new
      2026-09-22) and the V4 tranche stop (`live_loop.py`, `qty = min(qty, self._v4_tranche[...])`).
      Strategy exits, profit lock, V2/V3 and flatten all sell the full quantity, so they are unaffected.
    * **Measured:** QT session `fba831e3…`, slot trims at 16:15 UTC (orders 5–8). Realized P&L not
      booked: **−$39.92** (EQB.TO +0.62, QQQ −4.46, SRU.UN.TO −4.31, VDY.TO −31.77). Its equity rows
      are right; its `realized_pnl` reads 0.00 where it should read −39.92 (before commissions,
      which the column excludes anyway).
    * **Fix:** in the reducing branch, realize `(fill − avg) × closed_qty` (sign-flipped for a
      short) and leave the average entry unchanged. Also handle a fill that crosses zero (close
      plus reopen); long-only today, so it can't happen yet.
    * **Resume interaction — why it wasn't fixed on the spot.** `resume()` replays fills through the
      same `_apply_fill` and raises `RehydrationMismatch` when replayed cash or realized P&L differs
      from the session's last equity row by more than $0.05. Today both sides use the buggy path, so
      they agree. After the fix, replaying `fba831e3` computes −39.92 against a journal row that says
      0.00, and **`--resume-session fba831e3…` refuses to start**. Options: (a) fix once `fba831e3`
      has closed; (b) make the realized check tolerate rows written before the fix (a journal
      version marker, or check only cash, which the fix doesn't change); (c) fix, then start a
      fresh session. Don't rewrite the journal rows: `state/` is ground truth.
    * **Tests to add:** partial sell books realized P&L; a partial sell then a full close sums to
      the same total as one full close; resume round-trips a session with partial sells.

- ✅ **TradeCard approval axis in paper mode — RESOLVED, verified end to end 2026-09-18.** The fixes
  were already in the uncommitted TradeCard WIP. `approval_asgi.Broker` now includes `paper`,
  `ib_web` and `global`, and prompts carry the broker's `.venue`. `approval_card_sim._get` decodes
  non-JSON error bodies instead of crashing. Re-test (session `c49d975a…`, `paper_kraken.py
  --require-card`, card sim on auto-accept):
    * entry published → card ACCEPT → `paper.order.filled` Buy PAXG/USD 5.986;
    * the flatten Sell published → ACCEPT → filled; book flat (−$35.95).
  Both intents are in `state/approval.db` with `verdict=ACCEPT`, `broker=paper`, `consumed=1`,
  signer `card-sim-001` and canonical `paper|…`. The shim had exited, so this was read from the DB,
  not `/v1/passbook`. No 500s. `CLAUDE.md`'s recipe was rewritten to the verified commands.
  `SPEC_VERSION` is still 1.0.0; bump it if the firmware allowlist needs to learn `paper`.

- 🟡 **UPDATE 2026-09-18 — mechanism built, default unchanged pending your call.** `analysis/params.py` `resolve_params` / `build_strategy` implement the precedence below (steps 1–3):
  `cli.py signal --params {default,wf,calibrated}`, default `default`, so QT runs exactly as before
  until you flip it. The boot banner prints each symbol's source and running params. Entry alerts
  now show "Evidence is for: …" (registry) and "Running: …" (the live instance) and flag a MISMATCH.
  Unit-tested (`tests/test_params_resolver.py`); not runtime-exercised. With `--params wf`, 10 of 14
  QT names get registry params (e.g. QQQ `lookback=189, threshold=0.02` vs default 126 / 0.0); the
  `confirm_*` names get calibrated (their symbol reaches the pattern filter at last); `composite`
  stays on defaults. **Still open:** (a) your decision to flip the default to `wf` (it changes what
  QT trades and breaks comparability with the 09-18 exit-variant backtests, which ran defaults);
  (b) journaling the resolved params per intent in `paper_orders.jsonl` (lives in `brokers/paper.py`,
  which the graph-leak session is editing); (c) `confirm_*` wrappers can't yet take their base
  strategy's registry params; (d) the A/B in step 4.
- 🔴 **The live paper path runs STOCK CLASS DEFAULTS, not WF-validated or calibrated params, and
  the alerts misreport this.** Found 2026-09-16. Verified by inspection:
    * `cli.py` `_strategy_or_die(name)` returns `STRATEGIES[name]()`, zero-arg, and the
      `--strategy-map` build calls it for every symbol.
    * `analysis/calibration.py::calibrate_for` has one caller in the repo: `tune.py`.
    * `intel/notification.py` renders the alert's `Strategy: … with <params>` line from
      `wf_record.params` (the registry), never from the running instance.

  Measured on QT session `b14e4de0…`:

  | symbol | alert claimed (registry) | actually running | calibrated_kwargs |
  |---|---|---|---|
  | `SRU.UN.TO` | `window=7, oversold=25` | `window=14, oversold=30` | `window=15, oversold=35` |
  | `VDY.TO` | `lookback=63, threshold=0.02` | `lookback=126, threshold=0.0` | `{}` |
  | `EQB.TO` | `lookback=126, threshold=0.0` | same (coincidence) | `{}` |

  Only 3 of 14 symbols were sampled. **Consequence:** `state/paper_fills.jsonl` is *not*
  evidence about WF-validated configs, and every paper alert to date may describe a config that
  never traded.

- **An explicit parameter-precedence chain, so the calibration fold can be A/B tested.** Depends on
  the bug above. Taken literally, "WF params pull from `calibrated_kwargs`" would overwrite
  per-symbol walk-forward evidence with class-level medians and recreate the misreporting bug. Plan:
    1. `intel/notification.py` renders the *live instance's* params. Keep the registry OOS/WFE
       numbers, labelled as evidence for the registry params, and show the mismatch when it exists.
    2. One resolver, `resolve_params(strategy_name, symbol, mode)`, with the order: CLI override →
       WF registry → calibrated_kwargs → class defaults.
    3. A `--params {wf,calibrated,default}` flag on `cli.py signal` and the paper scripts. Journal
       the mode and the resolved params per intent in `paper_orders.jsonl`.
    4. A/B: `--params default` vs `--params calibrated` on the same symbols, ideally interleaved
       or run concurrently (regime confound). **Not** promotion evidence; WF stays the gate.

- ✅ **Graph-journal wash cadence resets on every restart — FIXED 2026-09-18.** `intel/graph.py` `last_wash_time` / `record_wash_time` persist it (`<journal>.last_wash`, falling back to the `.bak` mtime); `graph_journal.py` reads it at boot. Unit-tested; the real journal reads the 14:55 wash (4.6h ago), so the next launch won't prune.
- ✅ **`--max-prune-fraction 0.0` pruned everything instead of nothing — FIXED 2026-09-18.** `wash_due()` treats <= 0 as "no wash" (and leaves `.bak` alone); `None` still means uncapped. The boot banner says when pruning is disabled. Unit-tested.
- 🟡 **The test suite was writing into `state/` ground truth.** Traced 2026-09-18:
    * **Overlay journal: FIXED (uncommitted).** Three `OverlayProvider(...)` calls in
      `tests/test_intel_overlay.py` used the default `journal=True`, so every run appended empty
      snapshots (`global_alert_count 12`, `market {"crypto_chg": 9.0}`, `degraded: false`) to
      `state/intel_overlay.jsonl`. They now pass `journal=False`, and a full run leaves the file
      unchanged (341 → 341). About 89 of 341 rows are empty or degraded, most of them from this
      leak; they diluted the 09-16 calibration's base rates. The rows are left in place: cleanup
      needs your approval.
    * **Graph journal: OPEN.** A full run still adds ~33 fake `traded` edges (venues `fake`,
      `static-feed`, `my-custom-venue`, …) to `state/intel_graph.jsonl` through the paper broker's
      `fill_edge` append. There's a spawned task for it ("Stop tests writing fill edges to the real
      graph").

- ✅ **The global `state/STOP` stopped only ONE session — FIXED 2026-09-18.** It is no longer consumed; each session honours it only if its mtime is at or after that session's start (2 s grace for Windows mtime granularity), so every running session stops and later launches ignore it. `STOP_<id>` is still consumed. Tests: both sessions stop, a stale file is ignored, the per-session file is consumed. `HALTED` untouched.

- 🟡 **Flatten-on-exit: remaining gaps.** Built 2026-09-16 (`LiveMonitor.flatten`,
  `request_stop`, `--flatten-on-exit` on `cli.py signal` and `paper_kraken.py`; stop-sentinel
  exit). Verified flat on `f082cdb1…`, `889dde54…`, `b5870348…`, `9e834d13…`. Still open:
    * Not wired into `paper_ib.py` / `paper_global.py` (the user's desk-policy WIP; coordinate).
    * `PaperBroker.__init__` starts flat with **no journal rehydration**, which is in tension with
      "`state/` files are ground truth". A crashed session's book can't be recovered or closed.
    * `_book_risk` duplicates `step()`'s inline risk block. Unify.
    * **With `--require-card`, the flatten-on-exit sells need a card ACCEPT too** (seen in the
      2026-09-18 E2E). If the card is offline or declines at stop time, the flatten prompt expires
      and the book stays open. Decide whether exits bypass the card (they reduce risk) or wait.
    * A flatten can be **rejected** by the kill-switch, the min-ticket floor or the heat cap (they
      apply to SELL). It's reported loudly but not retried. Decide the policy.
    * ✅ `PaperBroker` journal rehydration — built 2026-09-21 (see the ordered list, item 1).
    * Orphaned books in the journals — now **recoverable** via `resume()` (48 of 48 replay cleanly,
      2026-09-21); closing them is your call (it books today's prices into old sessions):
      `4a6ad96a`, `982b7458`, `b14e4de0`, `eeefb9e2`, `ca1252ad`, `dbefdbe2`, `e6c192ef`,
      `1af5bb80`, plus earlier sessions of the same shape.

- ✅ **`classify_symbol` routed `EUR/USD` to crypto — FIXED 2026-09-18.** Slash pairs with two fiat legs are `fx`; crypto slash pairs are unchanged. Regression test covers EUR/USD, USD/CAD, EURUSD, BTC/USD, BTC/EUR, PAXG/USD, BTC-USD, /ES.

- 🟡 **CI runs a hardcoded test list — NEEDS YOUR DECISION (2026-09-18).** The workflow is deliberately TradeCard-only (its header says the full suite touches network-facing IB/Kraken paths). Proposal: a second job on `ubuntu-latest` running `pytest tests/ --ignore=tests/test_quantconnect.py` with the full dev install. Unverified here: the POSIX-only daemon test (skipped on Windows) would run for the first time, and any test doing real network I/O would surface. Needs a push to try, so it's your call.

- 🟢 **QT paper cash goes a few dollars negative (diagnosed 2026-09-18, left as is).** Four first-poll buys fill to ~99.99% of equity because the router's gross-leverage cap (1.0 × equity) doesn't count the $4.95/fill commissions (session `ec72c96f`: $99,985 notional + $19.80 → −$5.24 cash). Realistic for a margin account and cosmetic. The fix would be a commission reserve in the Router's trim headroom, which is your call since it's the gate.

- ✅ **`qc-rank` ranked a tutorial template #1 — FIXED 2026-09-18.** `rank_qc_library` skips runtime-errored backtests and those with fewer than `--min-orders` (default 20) QC "Total Orders", maps Sortino from QC stats, and defaults to `sortino_over_dd` (was Sharpe-based, against the standing ranking rule), as does the `qc-library` merge. Min-*duration* is not filtered: the QC backtest fields for it are unverified. 3 tests.

## Queue — build / run

- **Futures proxies for the chip + Japan equities — MEASURED 2026-09-25/26, verdict: a hedge, not a
  substitute (except for the Japan ETF sleeve).** User asked what futures could proxy the names just
  added. Two structural facts first:
    * **Single-stock futures do not exist** for NVDA/AVGO/MU/etc. — US single-stock futures died with
      OneChicago in 2020. A "proxy future" can only be an index future.
    * **`config/futures_universe.json` has no equity index future at all**: 23 contracts, every one a
      commodity (/CL /GC /HG /NG /SI /ZC /ZS /ZW, ICE softs, OSE platinum + rubber, SGX iron ore +
      rubber, canola). Adding an index future is a config addition, not a code change — the roll,
      lots and `AssetRouter` paths are already wired for futures.

  **Measured fit** (daily returns, cached bars, `reports/futures_proxy_fit_2026-09-25.csv`; beta and
  R² vs the ETF that stands in for each future):

  | name | proxy future | beta | R² | unhedged variance | ann vol | residual vol |
  |---|---|---:|---:|---:|---:|---:|
  | FLJP | Nikkei/TOPIX (via EWJ) | 0.96 | **0.949** | 5% | 18.2% | 4.1% |
  | JPXN | Nikkei/TOPIX | 0.93 | **0.874** | 13% | 17.6% | 6.2% |
  | SMH | /NQ or /MNQ | 1.40 | 0.781 | 22% | 35.7% | 16.7% |
  | SOXX | /NQ or /MNQ | 1.44 | 0.758 | 24% | 37.4% | 18.4% |
  | NVDA | /NQ | 1.74 | 0.612 | 39% | 50.3% | 31.3% |
  | AMAT / ADI / AVGO | /NQ | 1.06-1.50 | 0.51-0.53 | ~47-50% | 33-47% | 23-32% |
  | QCOM / NXPI / TXN / ON / MU | /NQ | 0.94-1.54 | 0.40-0.48 | 52-61% | 33-54% | 25-42% |
  | **SONY** | Nikkei (via EWJ) | 0.98 | **0.359** | 64% | 28.8% | 23.1% |
  | **INTC** | /NQ | 1.21 | **0.280** | 72% | 51.9% | 44.0% |

  **What follows**
    1. **The Japan ETF sleeve is genuinely replaceable by one future.** FLJP R² 0.95 and JPXN 0.87
       against a Nikkei/TOPIX proxy, residual vol 4-6%. One contract would replace three correlated
       ETF positions (EWJ, FLJP, JPXN are the same exposure three times — worth noting they are
       currently three of the 28 watchlist names competing for 6 slots), trade ~23h so it covers the
       after-US-close gap the exchange-hopping item is about, and OSE.JPN is **already a configured
       venue** (the platinum and rubber contracts trade there), so the plumbing exists.
    2. **No future proxies a single chipmaker.** /NQ leaves 39-72% of variance unhedged; INTC at 0.28
       and SONY at 0.36 are barely related to their index. Use /MNQ as a **beta hedge on the semi
       basket** (SMH/SOXX at R² ~0.78 are the only single tickers a future tracks reasonably), never
       as a stand-in for a named position.
    3. **Sizing is the constraint, as the existing config shows** — most of the 23 contracts are
       `enabled: false` precisely because `notional_per_contract` does not fit the position cap on a
       $100k book. Micros are the only viable rung: /MNQ ($2 × NDX), /MES ($5 × SPX), OSE Nikkei 225
       **micro** (¥10 × index) and mini (¥100 × index). Compute each against the vol-scaled cap and
       the 50% clamp before enabling anything.

  **Open work if this is taken up:** (a) probe whether OSE **index** futures return data on the paper
  login — index futures data is a different subscription from the missing TSEJ *equity* permission
  (error 162), and all futures fronts already resolved in the 09-21 probe, so this may be free;
  (b) `execution/venue_costs.py` has no futures entry — futures charge per contract, not per share,
  so the cost floor cannot price them yet; (c) currency: /NKD is USD-denominated while OSE Nikkei is
  JPY, which interacts with the numeraire question in the exchange-hopping item; (d) decide whether a
  future *replaces* the Japan ETFs (fewer, better-correlated positions) or *hedges* the semi basket —
  they are different mandates and the second one needs a short leg, which nothing in the book does today.

- **Japanese tech: searched 2026-09-25, NOTHING ADOPTED.** User asked for Japanese-tech exposure.
  Result: **no candidate clears the walk-forward gate, and pure Japanese tech is not reachable
  through Questrade at all.**
    * **Reachable (validated against the live Questrade symbol API):** SONY, EWJ, DXJ, BBJP, FLJP,
      JPXN, SCJ, HEWJ, DFJ — 9 of 14 candidates. `CAJ` (Canon ADR) is **not carried**, and
      `6758.T`, `8035.T`, `6861.T`, `9984.T` (Sony, Tokyo Electron, Keyence, SoftBank on their home
      exchange) all return "Symbol not found" — Questrade has no Tokyo listings, as expected.
    * **So the only single-name Japanese tech available here is SONY.** Everything else on the list
      is a broad Japan fund where tech is a sector weight, not the exposure. Real Japanese tech
      (Tokyo Electron, Advantest, Keyence, Disco, Renesas, Murata, Fanuc) needs the **.T listings via
      IB**, which are blocked on the missing TSEJ market-data permission — error 162 on daily bars,
      the one venue where even history fails. That makes it a dependency of the IB item, not a
      Questrade one.
    * **Walk-forward (5y cached bars, same harness as the equity pool):
      `reports/wf_symbols_japan_tech_2026-09-25.md` — 0 robust of 9.** Best OOS scores came with
      almost no trades: FLJP 7.73 (3 trades), BBJP 4.90 (2), JPXN 4.66 (3), EWJ 3.14 (2),
      SONY 2.46 (**10 trades**, the only one near the trade minimum) — every WFE between −0.02 and
      0.40, all under the 0.5 gate. DXJ and HEWJ produced 0-1 trades.
    * **Reading:** these names rarely trigger the book's strategies, so the scores rest on 1-3
      trades and mean nothing. Do not add any of them on this evidence.
    * **What would change the answer:** (a) deeper history — the cache warm pulled 5y (1,255 bars),
      enough for only ~4-5 folds, so a 10y pull is the cheap next test; (b) the Tokyo data
      permission, which unlocks the actual tech names rather than proxies; (c) a strategy family
      suited to these names, since the current set is what failed to trigger.

- **SCOPE (user direction 2026-09-25): unlock strategy capacity, walk-forward the currency
  sleeves, and hop exchanges after the US close.** Three separable programmes; A is cheap and
  self-contained, B reopens two closed decisions, C is gated on IB market data. None started.

  ### A. Unlocking strategy capacity — the bottleneck is slots, not strategies
  **Where it stands:** 44 strategies in `STRATEGIES`; the live QT map uses 7 of them and the crypto
  sleeve 5. `WALK_FORWARD_VALIDATED` holds 32 names, **all equity (29) + commodity (3)** — no FX, no
  crypto. Since 2026-09-24 the book runs `--params wf`, so the machinery to run registry configs
  in production exists and is exercised.
  **The trap:** 44 strategies x 32 names is 1,408 cells. Sweeping that and keeping the winners is
  how a repo manufactures overfit. And more validated strategies do **not** buy more exposure: the
  book has 6 position slots, so extra candidates only change *which* name wins a slot. Treat this
  as improving the ranking, not widening the book.
  **Steps:** (1) inventory which of the 44 have ever been walk-forwarded at all — the `candle_*`
  single-pattern family (17 of the 44) almost certainly has not; (2) pinned-parameter WF per
  strategy-family cell (the `scripts/calibration_sweep.py` protocol, not the re-optimising sweep),
  a-priori grids, config count recorded; (3) promotion only on WFE >= 1.0, OOS trades >= the class
  minimum, beating the incumbent by more than the fold-to-fold spread, still ahead at 2x costs;
  (4) then ask whether the winner displaces an incumbent in the strategy map, symbol by symbol.
  **Score on `sortino_over_dd`** with Sortino, return and time in market beside it.

  ### B. Walk-forward validating the currency sleeves — reopens two closed decisions
  **Say this out loud before spending time:** the guardrails record **"FX pair-trading: not
  tradeable here (2026-09-04)"** (cost drag 25-75% of the daily FX excursion; revisit only with a
  sub-pip cost model or `KalmanPairs`) and **"FX single-name sleeve: dropped (2026-09-05)"** (0 fills
  in 14 polls), plus **IB FX off-policy (2026-09-14)**. The user has now asked to revisit, so this is
  prompted — but the evidence that closed it has not changed, and nothing here should be presented as
  new until a cost model contradicts it.
  **What already exists:** `WF_PROTOCOLS["fx"]` (504 train / 126 test / 126 step, annualisation 260,
  `min_wfe` 0.5, `min_oos_trades` 10) with `data_source="pair-price feed (not yet wired)"`. Its own
  note says currency-*hedged ETFs* clear under the **equity** protocol. On disk: `AUDUSD_daily.parquet`
  (2,272 bars, 2020-06 -> 2026-09) and its trades file — one pair, deep; the other majors are not
  cached. `scripts/single_fx_wf.py`, `fx_pairs_scan.py`, `walk_forward_pairs.py` are written.
  **Two paths, and the cheap one is policy-compatible:**
    1. **Currency exposure through ETFs** (FXE / FXB / FXA / UUP / CEW.TO), walk-forwarded under the
       **equity** protocol: Questrade-fetchable, no new vendor, no FX venue, no sub-pip claim, and it
       reuses the whole existing pipeline. The dollar-hedge overlay already trades UUP, so the
       plumbing is proven. **Do this first.**
    2. **Native FX pairs**: needs a pair-price feed AND a cost model with a real spread per pair
       before any result means anything. `execution/venue_costs.py` (new 2026-09-25) is where that
       spread belongs — an FX entry with a sub-pip half-spread, not a flat fee. Until then a native
       FX walk-forward would be scored against a cost model that does not describe FX.
  **Acceptance:** a currency sleeve must clear the same gate as an equity name on `sortino_over_dd`
  net of **2x** costs, and must beat the trivial alternative of simply running the hedge overlay.

  ### C. Exchange shifting after the US close — the clock first
  **Correction to the framing:** London is not an after-4pm venue. Measured from `venues.py` for
  2026-09-25 (America/New_York):

  | venue | local session | NY open -> close |
  |---|---|---|
  | ASX (AUD) | 10:00-16:00 Sydney | **20:00 -> 02:00** |
  | TSEJ (JPY) | 09:00-15:30 Tokyo | **20:00 -> 02:30** |
  | SEHK (HKD) | 09:30-16:00 Hong Kong | **21:30 -> 04:00** |
  | LSE (GBP) | 08:00-16:30 London | **03:00 -> 11:30** |
  | US / TSX | 09:30-16:00 | 09:30 -> 16:00 |

  So the handoff after 16:00 NY is **Asia first (ASX + Tokyo, then Hong Kong), London pre-dawn**, and
  London's afternoon overlaps the US open rather than the US close. Any "follow the sun" scheduler
  should be built from `Venue.sessions` rather than from a hand-written clock, and must handle the
  DST drift between NY, London and Sydney (their offsets change on different dates).
  **Already built:** all five venues with sessions, currency, IB exchange and lot rules;
  `paper_global.py` (IB futures + overseas equities, one numeraire, `--equities`); `SessionRouter`
  queueing closed-venue intents; `priceMagnifier` pence handling and per-name board lots
  (fixed 2026-09-21, checked on real IB bars).
  **Blockers, in the order they bite:**
    1. **Live quotes: error 354 on every venue including SPY**, on both 2026-09-21 probes. Bars work
       everywhere except Tokyo. Re-probe after a *fresh paper login* first — it is free (option 1 of
       the four off-hours options).
    2. **Tokyo has no TSEJ market-data permission** — even daily bars fail (error 162).
    3. **LSE ETFs list on `LSEETF`** but `.L` routes to `LSE`, so ISF.L / VUSA.L do not resolve.
    4. **Numeraire and FX conversion**: a book spanning AUD/JPY/HKD/GBP needs one accounting
       currency and a conversion for equity, heat and cap math. `paper_global.py` takes a
       `--numeraire`; the Router's cap/leverage math is currency-naive and would need to agree.
    5. **Per-venue costs**: `venue_costs.py` models Questrade / Kraken / IB US. London adds **UK
       stamp duty (0.5% on purchases of shares; ETFs exempt)**, which dwarfs commission and must be
       in the model before any LSE result is scored.
  **Steps:** re-probe -> daily-bar dry run of the hop (plumbing evidence only, no P&L claim) ->
  decide subscriptions (US futures bundle before any Asia equity data) -> then **one venue at a
  time, easiest first: ASX, then SEHK (per-stock lots), then LSE (pence + LSEETF + stamp duty),
  Tokyo last (needs a data permission).**
  **Gate:** no overseas book trades until that venue's quotes, lots, currency conversion and cost
  model are each verified, and the whole path has been runtime-exercised on daily bars.

  **Cross-cutting:** none of C is reachable through Questrade, so it shares its fate with the IB
  equity-migration item below — the same feed swap, cost model and contract resolution serve both.

- **Dynamic portfolio holdings, managed every poll instead of only at entry (user direction
  2026-09-25, not started).** Today the book's weights are decided ONCE and then left alone:
  `weight_bias_for` is "computed ONCE at startup from the local candle cache" (`cli.py`), the
  per-name cap is evaluated only on a BUY, and `trim_to_slots` is a one-shot at launch. Between
  polls nothing re-weights: a name that drifts from 20% to 63% of the book keeps its shares until a
  strategy exit fires. That is what produced 2026-09-23's QT book (VDY.TO 63% of equity, −$557 of a
  −$533 gross day) — no gate was breached, the weights simply were never revisited.

  **What "real-time" should mean here.** The loop polls every 60-300s but the strategies read daily
  bars, so the target weights change slowly; what needs to be continuous is the *comparison* of held
  weights against target, plus a decision to act. Target weight per name from the existing pieces
  (OOS score + covariance bias, the vol-scaled cap as a ceiling, the intel overlay as a trim),
  recomputed each poll on live marks rather than at boot.

  **The precedent to copy, not invent:** `risk/hedge.rebalance_delta(current, target, band=0.20)` —
  a no-trade band as a fraction of the target position, so small drifts never trade. The dollar-hedge
  sleeve already rebalances this way inside `step()`; a holdings rebalancer is the same shape applied
  to the whole book, and it must route through `Router.submit` like everything else.

  **Dependency CLEARED 2026-09-24** (the partial-sell fix landed — see the ✅ entry in the
  correctness queue; 5 tests in `tests/test_paper_resume.py`). The argument is kept because it is the
  standing reason rebalancing needs correct accounting: a
  rebalance trims positions, i.e. it is a *partial* sell, and `PaperBroker._apply_fill` books
  realized P&L only on a full close. Rebalancing daily would push most of the book's P&L into the
  gap: equity would stay right, `realized_pnl` would stay 0.00, and per-session attribution would
  become unrepresentable. The emitter is fixed, so this no longer blocks — but verify per-session
  attribution against a live rebalancing session before trusting it.

  **Design constraints**
    * **Cost first.** At $4.95/fill a 5-name book rebalanced daily pays ~$50/day, which on $100k is
      ~12%/yr of drag. The no-trade band and the cost-aware floor (see the cost-aware sizing item)
      are what make this viable or not; pair them, and require each rebalance leg to clear the same
      round-trip-cost ratio a new entry must clear.
    * **Trim-only first.** Ship the reducing half (cut an oversized name) before the adding half:
      trims lower risk and cannot breach a cap, adds re-open every sizing question. An add is also
      an entry and belongs in the parallel allocator's slot/budget pass, not in a separate path.
    * **Never fight an exit.** If V1-V3 or a stop fires on a name, that wins; no rebalance may
      re-buy into a name the lockout is holding out of.
    * **Journal the target.** Write target weight, held weight, band and decision per symbol per
      poll (the `sizing_decisions.jsonl` shape), or the behaviour is unauditable after the fact.
    * **Resume-safe by construction:** rebalance fills land in the session's own journals, so
      `--resume-session` replays them; nothing extra needed.

  **How to judge it:** A/B two paper books on the same strategy map, rebalanced vs not, comparing
  `sortino_over_dd` net of the extra fills, realised max single-name weight, and turnover. The
  hypothesis is that capping concentration is worth more than the commissions it costs — which is
  exactly the thing 09-23 suggests but does not prove.

  *(Filed as the trading mechanism. If what you wanted was a live read-only holdings view instead,
  say so — that is a different, smaller item, and per the unfit-data principle it must show only
  closed-book-derived figures plus current marks.)*

- **Audit ledger + cryptographic continuity — PHASES 0–7 BUILT 2026-09-24, 8 scoped.** Full structure in
  `AUDIT_LEDGER_SCOPE.md`: an append-only event ledger (one row per state transition, hash-chained,
  signature persisted) plus an intent fingerprint shown on dashboard, device and ledger alike. Seven
  gaps found in the current stores; the worst is that **the card's signature is verified and then
  discarded** (`intents` has `signer_card_id`, no signature column), so no approval can be
  re-verified after the fact — **now fixed**, along with the missing correlation key and the
  partial-sell P&L bug (phases 0–2; see the scope doc's phase table for exactly what landed).
  Phase 3 landed too: `audit/ledger.py` (append-only, hash-chained per stream, fsync per row),
  Router dual-write behind an optional `ledger=`, `scripts/verify_ledger.py` (chain check + per-intent
  reconstruction, exit 1 on a break), on by default in `paper_kraken.py` via `--audit-ledger`.
  Phase 4 landed too: `audit/versioning.py` content-hashes the strategy (module source + that
  instance's params) and the risk gate (gate source + live thresholds); both are on every ledger row
  and shown by `verify_ledger.py --intent`. Known limit, documented: strategy versions do not follow
  imports, so an edit to `signals/indicators.py` changes behaviour without changing the version.
  Phase 5 landed too (mostly): STRATEGY_SIGNAL / SIGNAL_SUPPRESSED with reasons, RISK_TRIMMED,
  PARTIAL vs FILLED, BROKER_REJECTED, INTENT_QUEUED / RELEASED, INTENT_SENT, APPROVED / REJECTED /
  EXPIRED, SIGNED **with the signature in the row**, POSITION_FLATTENED. It found two real gaps
  (the card path's accepted gate emitted nothing; the Router compared fill quantity against a
  broker-mutated order) — both fixed. **Still unemitted:** INTENT_DISPLAYED (needs the store to
  record the card's first fetch), SIZED, KILL_SWITCH_TRIPPED, CARD_REGISTERED / CARD_REVOKED,
  FUTURES_ROLLED, CANCELLED.
  Phase 6 landed too, except firmware: the fingerprint is on `Prompt` and `PassbookEntry` (derived
  from the stored canonical bytes), in `PromptOut` / `PassbookEntryOut`, rendered by the PWA (pending
  prompts show HASH / ACCOUNT / STATUS; history shows the hash and signing card), and the card sim
  recomputes it and **refuses to sign on mismatch**. **Firmware is specified but unwritten** —
  `handle_prompt` in `firmware/tradecard/main/main.c` needs an mbedTLS SHA-256 over the same
  `canon_buf` and a display line; the 4-line LCD is full, so which line it replaces is your UX call,
  and it needs a device build I cannot do here.

### Cheap fixes cleared 2026-09-24 (working the backlog lowest-cost first)

* ✅ **Stale `state/STOP_KRAKEN_FETCH` removed.** It was left behind when the interleave driver was
  stopped by hand, and `kraken_interleave.py` exits between steps whenever it exists — so the 16:30
  scheduled fetch would have exited immediately every night without fetching anything. Nothing in the
  driver clears its own sentinel; worth deciding whether it should (a stop file that outlives the run
  it stopped is a foot-gun) — left as-is for now because an auto-clearing stop file can also lose an
  intentional "stay stopped".
* ✅ **`graph_journal.py --help` no longer crashes** on the cp1252 console. `sys.stdout.reconfigure`
  at import, as the paper runners already do, so callers no longer need `PYTHONIOENCODING=utf-8`.
* ✅ **`backtest/costs.py`: 4 invalid `\$` escapes fixed** — 8 SyntaxWarnings gone from every test
  run. No docs build renders those docstrings, so the backslash bought nothing.
* ✅ **`/v1/stats` stopped serving invented numbers as measured ones.** `overlay_scalar` was
  hardcoded `0.47`, `session_id` `"trading-session-1"` and `overlay_risk_zone` `"crypto"`. All three
  are now `null` and named in a new `placeholders` field, and the genuinely computed pending/expired
  counts — previously assigned and thrown away, which is what ruff's F841 was pointing at — are
  returned as `intents_pending` / `intents_expired`. `get_conviction_matrix`'s demo fallback is
  commented as such; **it still returns invented conviction when the allocator has no data**, so a
  client must not draw it unlabelled. Nothing outside the shim consumed these fields.
* ✅ **Ledger wired into `paper_ib.py` (stream `ib`) and `paper_global.py` (stream `global`).** All
  four runners now record: qt, kraken, ib, global — each its own chain, `--audit-ledger` on by
  default. The stream-separation test covers all four. This clears the phase 7 leftover, so
  `reconcile_ledger.py` will stop reporting IB/QT journal rows as missing once those books next run.
* ✅ **The BI endpoints stopped inventing data (2026-09-25).** `/v1/stats` and
  `/v1/conviction-matrix` each had **two** fabricating code paths — one in `ApprovalMetrics`, one
  inline in the route for when the shim has no journal, which is the path the tests actually
  exercised. Removed: a hard-coded 13x5 "conviction" grid **including SPY and BTC/USD, neither of
  which has ever been walk-forward validated**; a `0.5` "neutral conviction" for every unvalidated
  symbol; five per-strategy columns derived from one number by fixed offsets (`+0.15` because
  "momentum tends higher", `-0.20` for a "bearish overlay"); a `[0, 1]` clamp that hid that
  `oos_score` is an unbounded ratio running 19-111, so every real symbol saturated at 1.0; and
  `-0.2` drawdown / `4.2s` response / `0.47` overlay scalar in the no-journal stats branch.
  The matrix now reports only measured `(symbol, strategy)` pairs from `WALK_FORWARD_VALIDATED` —
  32 symbols, 7 strategies, **one value per row and null everywhere else**, because a null means
  "never validated", not "zero conviction". `metric` states the range so nothing renders it as 0-1.
  Three tests changed with it; each had been asserting the fabricated values, including one that
  pinned `starting_equity == 100_000` and one that required SPY to be present.
  **Still unfit for display:** per-strategy conviction is not computed anywhere in this codebase, so
  the heatmap is sparse by construction. If a real per-strategy conviction is wanted, that is a
  modelling task, not a rendering one.
* ✅ **45 unclosed-SQLite ResourceWarnings gone.** `SqliteCardRegistry` and `SqliteApprovalStore`
  held a connection for the life of the instance with no way to release it, so every short-lived one
  was reclaimed by the GC with its connection open — and the warning surfaced against whichever test
  happened to be running at collection time, which is why it looked like an arima/entries problem.
  Both now have `close()` (idempotent), context-manager support and a `__del__` backstop; the
  projection tests register their connections for teardown. Beyond the noise, an open handle on
  Windows keeps the `.db` locked against `tmp_path` cleanup. Suite-wide count is now **0**. One
  unclosed *file* warning remains in `tests/test_order_journal.py:43` (not Claude's file, left alone).
* ✅ **`execution/router.py` is clean under `mypy --strict`** — 18 errors to 0, on the file every
  order passes through. The 15 `arg-type` errors were one root cause: `build_default` assembled the
  gate knobs as a bare `dict` and splatted it into `confirm_live` / `confirm_autonomous` / the
  constructor, so mypy inferred a union value type and could not check a single argument — a
  misspelled key or a float where an int belongs would have reached the risk gate unchallenged. Now a
  `GateKwargs` TypedDict. The bare `list[dict]` annotations became `list[dict[str, object]]`, except
  `check_forced_exits(positions=...)` which is `list[dict[str, Any]]` on purpose: those rows come
  from a broker payload or a journal replay, mix strings with numbers, and the body reads them
  defensively, so claiming a precise shape would be a lie about data it deliberately distrusts.
  **Repo-wide mypy is 116 errors in 21 files**, so this is one file down, not the suite.
* ✅ **`scripts/build_desk_dash.py`** — "repopulate the dash" is now one command. It reads `state/`,
  computes every figure the panel draws (session ranking, curve, return distribution, round trips,
  attribution, activity heatmap, venue mix, gate denials, per-strategy acceptance, approval
  analytics) and injects one JSON blob into `pwa/desk.template.html`. It also prints the thin-sample
  warning: 7 closed trades against a 30-trade floor.
* ✅ **The Meter Atlas is folded into the desk panel** (2026-09-28) — the two pages in
  `OS-InvestmentIntelligenceDashboard/` are now one. The atlas is a fourth tab, ATLAS, restyled to
  the desk's dark tokens and unchanged in substance: 120 defined meters in 12 groups, neutral and
  needleless where nothing has been measured, a snapshot loader that rejects a file whole if any
  reading lacks a source, a scope or a plausible timestamp, search, and a per-meter detail dialog
  with the definition, unit, scale, code support and pinned source. The catalogue is baked in from
  `meter_catalog.csv`, the HISTORICAL snapshot is derived from the first row of
  `reports/calibration_sweep.csv` (all six readings or none), and the provenance line resolves the
  pinned commit through git rather than asserting it. One build writes both
  `pwa/desk.html` and `OS-InvestmentIntelligenceDashboard/TradeCard Desk Panel.html`, byte-identical.
  **Runtime-exercised**, not just built: loaded in a browser, the atlas renders 120 tiles, the
  historical snapshot reproduces the standalone page's six values exactly, a snapshot from
  `export_meters.py` run against the real journals loads and shows STALE/OBSERVED correctly, and a
  file with a future timestamp is rejected whole.
* ✅ **The desk panel is wired to a running session on both QT and Kraken** (2026-09-28) — the card
  shim now serves the panel, which is what makes it live: `GET /desk` is a public unlock shell that
  carries no journal data and never the token, and `GET /v1/desk/page` serves the built page behind
  the same auth as every other `/v1` route. Same-origin, so no CORS hole is opened and no token
  travels in a URL. The page polls `/v1/intents/pending` and `/v1/stats` and is **read-only by
  construction**: it shows the prompt and its fingerprint to compare against the card's screen, and
  the ACCEPT that decides an order is still a signature made on the device.
  * Kraken: `scripts/paper_kraken.py --require-card` prints the desk URL; new `--card-shim-host`.
  * QT: `cli signal --paper --require-card` gained the whole card path (`--card-shim-host`,
    `--card-shim-port`, `--card-ttl`) — it had none before.
  * **Runtime-exercised end to end**: with a shim booted against this repo's Router and one intent
    published, the panel showed `link live · 1 pending`, the prompt took the screen with the
    fingerprint the shim published (`A8AA...DC69`), and the TTL counted down. 11 new tests in
    `tests/test_desk_link.py`; the 119 approval-related tests still pass.
* ⚠️ **Three fabricated figures removed from the panel and its source** (2026-09-28), found while
  wiring the live link. (1) The AWAITING APPROVAL queue was a hard-coded pair of specimen rows
  (PAXG/USD, XIC.TO) with invented fingerprints; it is now the live queue, or an explicit "no link"
  note. (2) The takeover prompt was the same specimen; it is now either the live prompt or a
  **replay** of the newest real row in `approval.db`, ribboned as such, with the verdict that was
  actually recorded in place of the buttons. (3) `ApprovalMetrics.get_avg_ttl_response()` returned
  the literal **4.2** in every case — the loop meant to compute it was a `pass`, and the passbook
  did not carry `issued_at` anyway. `PassbookEntry.issued_at` is now populated by both stores, the
  median is measured over decided prompts only, and `StatsBody.avg_ttl_response` is nullable and
  named in `placeholders` when nothing has been decided. The header chips (kill-switch, card) are
  read from `state/HALTED` and the registered cards instead of being asserted in the markup.
* ⚠️ **Two defects the artifact wrapper had been masking** — found while folding, both in the
  already-committed `pwa/desk.html`. (1) `heat()` wrote to `#heat-n` / `#heat`, which existed in no
  markup, so the inline script threw there and **every statement after it never ran**: tab
  switching, the table toggles and the prompt takeover were all dead in the standalone file. The
  missing WHEN THE BOOK TRADES section is now in the BOOK panel. (2) `.takeover` sets `display:
  grid`, which outranks the UA rule for `[hidden]`, so the prompt overlay could be opened but never
  closed; the page now carries its own `[hidden] { display: none !important }`. Both only showed up
  outside the artifact host, which patches `[hidden]` itself.

### Phase 7 — SQLite projection + reconciliation — ✅ DONE 2026-09-24

Built as specified: `audit/projection.py`, `scripts/rebuild_projection.py`,
`scripts/reconcile_ledger.py`, 20 tests. The ledger stays the write path and the source of truth;
the projection is **derived and disposable**, rebuilt from scratch by replaying
`state/ledger/*.jsonl`. If the two disagree, the ledger wins and the disagreement is the finding.

**Verified on a seeded ledger** (11 events → 3 intents): outcomes folded correctly, a tampered row
reported as `chain_status.ok = 0` instead of being projected as sound, and 2 journal fills plus 1
rejection reconciled exactly against `fills.jsonl` / `rejected.jsonl`. **Against the real
`state/ledger/` it reports 0 events** — no session has yet traded with the ledger on, so there is
nothing to project. That is the honest state, not a failure.

**Remaining from this phase:** wire `ledger=` into the other runners (`cli.py signal`,
`paper_ib.py`, `paper_global.py`) — the same three lines as `paper_kraken.py`. Until then only the
Kraken book records, and `reconcile_ledger.py` will keep reporting QT/IB journal rows as missing on
the ledger side. Retire no journal until reconciliation is clean for a full session.

Original spec kept below for the parts not yet done.

* **Tables** (all `PRIMARY KEY`-ed on ledger identity so a replay is idempotent):
  `ledger_events(stream, seq, ts, event, intent_id, …, row_hash)` — the flat spine;
  `intents(intent_id, first_seen, last_event, symbol, action, shares, strategy_id,
  strategy_version, risk_check_version, verdict, signer_card_id, fingerprint, broker_order_id,
  filled_qty, outcome)` — one row per intent, folded from its events;
  `chain_status(stream, rows, last_seq, last_row_hash, verified_at, ok, reason)` — so a verification
  run is queryable rather than only printed.
* **Queries it must answer** (scope §2.5, currently only answerable by grepping JSONL):
  rejection rates per gate reason per week; time from `INTENT_SENT` to `APPROVED` (the TTL-pressure
  question the card UX needs); which `strategy_version` traded a symbol on a date; every intent with
  a `SIGNED` row whose signature no longer verifies; intents that reached `BROKER_SUBMITTED` but have
  no terminal event (the crashed-writer case).
* **Reconciliation** (`scripts/reconcile_ledger.py`, exit non-zero on mismatch): ledger `FILLED` /
  `PARTIAL` rows against `state/fills.jsonl` and `paper_fills.jsonl` by `(intent_id, order_id)`;
  ledger `RISK_REJECTED` against `rejected.jsonl`; `approval.db` verdicts against ledger
  `APPROVED` / `REJECTED` / `EXPIRED`; replayed positions/cash against the session's last
  `paper_equity.csv` row. **Expect real mismatches on the first run** and treat them as findings, not
  bugs to paper over: sessions started before 2026-09-24 have no ledger at all, and the Kraken book
  is the only runner wired (`--audit-ledger`), so QT and IB sessions will have journal rows with no
  ledger counterpart until they are wired too (small: pass `ledger=` in `cli.py signal` and
  `paper_ib.py` / `paper_global.py`, same three lines as `paper_kraken.py`).
* **Retire nothing yet.** The existing journals stay until reconciliation is clean for a full
  session; only then is dropping a writer a discussion.
* **Watch:** the projection must never be on the trading path — build it in a separate process, not
  inside the poll loop.

### Phase 8 — daily anchor + retention + trusted time (BLOCKED on three decisions)

Mechanics are easy; both open questions are about trust, so they are yours.

* **What it does:** at UTC rollover, for each stream, compute the Merkle root of that day's rows,
  sign the root with a key the trading process cannot use, and append
  `state/ledger/anchors/<date>.<stream>.json` (`{date, stream, first_seq, last_seq, row_count,
  merkle_root, signature, key_id}`). Publish the root somewhere append-only. That bounds tampering
  to the current day: a rewrite of any earlier row changes the root, and the root is already signed
  and copied elsewhere.
* **🔴 DECISION 1 — where the anchoring key lives.** An anchor signed by a key sitting beside the
  ledger, usable by the process that wrote it, proves nothing about that process. Options, cheapest
  first: (a) a second Ed25519 key in `state/` — honest about being weak, only defends against an
  edit that doesn't think to re-sign; (b) **the TradeCard signs the daily root** — the hardware is
  already there, already holds a key the host cannot read, and this is the option that actually fits
  the design; costs a daily tap; (c) an offline key on removable media, signed weekly rather than
  daily; (d) publish the root to a remote append-only place and treat public visibility as the
  anchor. Recommendation: **(b), with (d) as the publication channel** — it matches the
  zero-vendor-infrastructure posture where GitHub is the coordinator, so a commit of the day's root
  to a private repo is both the copy and the timestamp, with no service to run.
* **🔴 DECISION 2 — retention and WORM.** Today nothing stops a process — including the test
  suite, which has done it twice — from writing into `state/`. Needs: a retention period (how long
  ledger files and anchors are kept, and whether anything may ever be deleted); where the off-box
  copy goes and how often; whether to set the Windows read-only attribute on closed day files (cheap,
  defeats an accident, not an attacker); and whether the ledger directory should be moved out of
  `state/` so the "everything under state/ is fair game" convention stops applying to it.
* **🔴 DECISION 3 — trusted time (surveyed 2026-09-24, see scope §2.6).** We have timestamps; none
  of them is evidence. `ts` on every ledger row, and every `issued_at` / `resolved_at` in
  `approval.db`, is the writing host's own clock. `seq` + the chain carry **order** but say nothing
  about wall-clock. The card has no clock at all, by design: `prompt_ttl_seconds` takes the window
  from the server's `issued_at`/`expires_at` so no NTP is needed, and its passbook stores uptime
  since boot — so the card can contradict the host about the bytes, never about the time. Set the
  system clock back and every new row follows it while the chain still verifies.
  **One property worth not losing:** `mint_intent_id()` is `f"{time.time_ns():016x}-…"` and
  `intent_id` is inside the signed canonical bytes, so an approval cannot be re-dated without
  invalidating its signature (`18d84e7c8730f1e0` → `2026-09-24T16:25:48.630815+00:00`). It is still
  only the host's claim, signed by the card — and anyone reformatting the id would delete this
  silently. Options: (a) log `time.monotonic()` beside `ts` so an in-session clock jump is visible —
  cheap, catches accidents only; (b) publish the daily root remotely and let the publication time be
  a timestamp we did not author; (c) an **RFC 3161** timestamp token over the daily Merkle root —
  one HTTP call, a standard token, verifiable by any third party, no service of ours to run.
  Recommendation: **(c) plus (b)** — together they make the anchor mean "this existed by then"
  instead of "we say this existed by then". Note this is a prerequisite for the anchor being worth
  much: a root signed locally records a time the same host chose.
* **Do not claim immutability before this lands.** The chain makes edits *detectable*; the anchor
  makes them detectable *by someone else*; only WORM storage makes them *impossible*. The scope doc
  and `audit/ledger.py`'s docstring both say so — keep it that way in any write-up.
* **Also still open from phase 5–6:** `INTENT_DISPLAYED` (the store must record the card's first
  fetch of a prompt — a small `approval.db` migration plus a touch in `pending()`), and the firmware
  fingerprint line.
* ✅ **`INTENT_CREATED` now has an emitter — FIXED 2026-09-25, taxonomy coverage 25/25.** Neither of
  the two options in the original finding was needed: rather than stamping N construction sites or
  deleting the event, `Router.announce_intent()` writes it the first time a router sees an intent,
  carrying the intent's own `timestamp` plus `pre_gate_ms` — so the gap between "the strategy built
  it" and "the gate ran" is on the record without the Router having to be where it was created.
  Announced once per intent via a bounded `deque`: **the card path gates, waits for a verdict, then
  submits the same intent again**, so a naive emit in `submit()` put the creation event in the middle
  of the chain after SIGNED, reading as a second intent. `ApprovalRouter` calls `announce_intent`
  before its own pre-prompt gate; the inner router's call is then a no-op. 3 tests, and the
  diagnostic harness now demands full coverage rather than tolerating one gap.
* **Display target changed (user, 2026-09-24): ideally an 8" 1280x800 touch panel** for
  firmware/dash-type devops. This obsoletes the phase 6 firmware note that asked which of the 6 SPI
  LCD lines the hash should replace — at 1280x800 there is no line-budget problem, and the
  fingerprint, thesis, device id and status all fit at once. Two consequences worth thinking through
  before writing any firmware: (a) the current `firmware/tradecard/main/main.c` drives a small
  monochrome SPI panel with a 5x7 font (`lcd_puts`, `LCD_COLS`, `font5x7.h`) — a 1280x800 touch panel
  is a different device class (RGB/MIPI + touch controller, likely a different SoC or an SBC), so this
  is a port, not a display tweak; (b) at that size the **PWA is the natural dashboard** — it already
  renders the hash, account and status — so the question becomes whether the card stays a minimal
  tap-to-approve device with the 8" panel as a separate dash, or whether one touch device does both.
  The security property does not change either way: whatever displays the intent must show the
  fingerprint derived from the same record the signer received. The PWA is currently styled for phone
  width, so it would want a wider layout for an 8" panel.

- 🟡 **Cost-aware minimum position size — BUILT 2026-09-25, off by default, not runtime-exercised.**
  `execution/venue_costs.py`: `VenueCostModel.for_venue()` prices one side per venue (Questrade flat
  $4.95; Kraken 26 bps taker; IB $0.005/share, $1 order minimum, capped at 1% of notional; anything
  unrecognised or duck-typed gets the conservative flat fee), plus `round_trip_cost_ratio` and
  `min_notional_for`. Wired into `Router` as gate 11 (`max_round_trip_cost_ratio`, entries only,
  evaluated AFTER the size-cap trim so it judges what would actually be sent; the rejection names the
  ratio, the venue and the minimum ticket) and into `PaperBroker`, whose commission is now
  venue-priced unless a caller pins one. 13 tests in `tests/test_venue_costs.py`; suite 1415 passed,
  1 skipped. Flags: `--max-cost-ratio` on `cli signal` and `paper_kraken.py`, default 0.0 = off.
    * **Finding that shapes the setting:** Kraken's taker fee alone is ~52 bps round trip, so a
      0.5% ceiling fits at NO size there, while the same ceiling implies a ~$2,475 minimum ticket on
      Questrade and ~$500-800 on IB. **The ceiling must be per-venue** (or ~2% on Kraken). A
      percentage-fee venue needs no size floor at all — its ratio is size-independent.
    * **Still open:** pick the ceilings from data (replay every closed round trip: cost-as-%-of-
      notional vs realised return, see what 0.25/0.5/1% would have refused and what those netted);
      decide whether `min_ticket_usd` stays as a floor underneath; add an FX spread entry when the
      currency-sleeve work needs one; the backtest engine's `CostModel` is still separate, so
      backtests and the live gate do not yet share one cost source.

- **(original scope) Cost-aware minimum position size, per venue and per share count (user direction 2026-09-23).** The min-ticket gate is a flat `min_ticket_usd: 100.0` in `trading.yaml`, applied
  identically on every venue. It is blind to what a fill actually costs, so the engine happily opens
  positions that cannot pay for themselves. The rule the user asked for: **a position is only worth
  taking when its size is large relative to the commissions it will pay — and what "large" means
  depends on the share count and the venue.**

  **Evidence (Kraken paper session `bc361280`, 2026-09-23).** One fill all session: LINK/USD 14.55
  units, **$184.97 notional, $4.95 commission = 2.68% of notional on entry, ~5.4% round trip.** It
  cleared the $100 floor with room to spare and would have needed a 5.4% move to break even. At a
  flat $4.95/fill, **anything under roughly $2,000 is structurally unprofitable**, i.e. the floor
  permits positions 20x too small.

  **Two defects, and they compound**
    1. **The floor is not cost-aware.** `Router._gate` compares notional to a constant. It should
       compare the *round-trip cost fraction* to a ceiling: reject when
       `round_trip_cost / notional > max_cost_ratio` (0.5% is a defensible starting point, which at
       $4.95/fill implies a ~$2,000 minimum). `signals/profit_lock.round_trip_cost_frac` already
       computes exactly this number for the exit side, and `backtest/costs.CostModel.per_side_frac`
       does it for backtests — the gate is the only place that doesn't use it.
    2. **The commission model is not venue-aware.** `PaperBroker(commission_per_trade=4.95)` is
       Questrade's equity commission, and `scripts/paper_kraken.py` uses that default unchanged, so
       **the crypto book is charged $4.95 per fill when Kraken actually charges ~0.16-0.26% of
       notional** (~$0.40 on that LINK trade). The flat model overstates small crypto trades and
       would understate large ones. IB is different again: per-share with a per-order minimum and a
       cap as a % of trade value, so **cost per share falls with price and rises with share count** —
       which is precisely the "depends on share-size and venue" the user named.

  **Shape of the fix**
    * One `VenueCostModel` (extend `backtest/costs.CostModel`) resolved per venue: Questrade flat
      per fill, Kraken bps of notional, IB per-share with min/max. One source of truth for
      `PaperBroker`, the Router's min-ticket gate, the profit lock's arming test and the backtest
      engine — today three of those four disagree.
    * Gate becomes `max_round_trip_cost_ratio` (keep `min_ticket_usd` as a floor underneath it).
      **Reject, never size up**: if the risk-sized position is too small to carry its costs, the
      trade is not worth taking — inflating it past the sizer's answer would be the tail wagging
      the dog.
    * The rejection reason must name the number ("round-trip cost 5.4% of notional > 0.5% cap"),
      because a silent skip looks like a missing signal in the journals.
    * Venue tags already exist on every journal row (`venue` on fills/orders), so a replay can
      attribute costs correctly once the model is venue-aware.

  **Measure before choosing the ceiling.** Replay `state/paper_fills.jsonl`: for every round trip
  ever closed, compute cost-as-%-of-notional and the realised return, then ask what a 0.25% / 0.5% /
  1% ceiling would have refused and what those refused trades actually netted. Pick the ceiling
  from that distribution rather than from the $4.95 arithmetic alone — and note the obvious
  confound, that the flat-$4.95 model has mispriced every crypto fill in the journal to date.

- **FIRST STEP of the IB equity move: diff the two feeds' daily bars (2026-09-23, not started).**
  Standalone and runnable now — it is step 2 of the IB item below, pulled out because it blocks
  everything else and needs no decision. Every walk-forward parameter, every backtest and every
  calibration in this repo was fitted on **Questrade** bars. If IB's history differs materially,
  those numbers do not transfer and the migration is not a feed swap but a re-fit.

  **What to build:** `scripts/feed_diff.py --symbols <list> --years N`, writing
  `reports/feed_diff_<date>.{csv,md}`.

  **Data sources, and why this can run while a QT session is live:**
    * **Questrade side: from the local cache only.** `data/cache/` already holds 18,327 shards
      across 590 symbols (`EQB_TO_1d_<hash>.parquet` — dots become underscores, which is why a
      naive `EQB.TO_1d_*` glob finds nothing). No token, no API call, **so no risk to the one-shot
      refresh token of a running QT session.** The 2026-09-22 defense-name gap has since closed
      itself: ITA/NOC/GD now have shards, fetched by the live session.
    * **IB side: `IBBroker.candles(..., whatToShow="TRADES", useRTH=True)`** with TWS on 7497.
      Daily bars worked on every venue except Tokyo in the 09-21 probe, so **this step does not
      need the missing live-quote subscriptions**.

  **What to measure**, per symbol, over the overlapping window:
    1. Coverage: bar counts, first/last date, missing sessions on each side (calendar mismatches
       are as damaging as price differences).
    2. Close-to-close: mean and max absolute difference in bps, and the count of bars over 10 bps.
       Split TSX (`.TO`) from US — currency, listing venue and consolidated-vs-primary tape all
       differ, and TSX is where a surprise is most likely.
    3. Return series: correlation and, more usefully, the distribution of daily return differences.
       A constant price offset is harmless to a strategy; differing returns are not.
    4. OHLC beyond the close: the ATR stop and the candle/overbought exits read high/low/open, so a
       feed that agrees on closes can still move every stop. Check ranges, not just closes.
    5. Adjustment convention: pick a few names with recent dividends or splits (ENB.TO, VDY.TO,
       SLF.TO all pay) and check whether the two feeds adjust the same way — this is the most
       likely source of a systematic gap.

  **Acceptance, decided before looking at the output:** if median close differences are within a
  few bps and the return distributions match, the registry params carry over and the migration
  proceeds. If TSX or the adjustment convention differs materially, the honest conclusion is that
  **the walk-forward has to be re-run on IB bars** before any IB-fed book trades — record that as a
  cost of the migration rather than discovering it later in a paper A/B.

  **Then:** re-run one or two known backtests (a `ts_momentum` name and a `bollinger` name) on IB
  bars and compare `sortino_over_dd`, OOS return and trade count against the QT-fitted result.
  That converts a bar-level diff into the number that actually matters.

- **Move the equity book from Questrade to IB — US/TSX too, not just futures and options (user
  direction 2026-09-23, not started).** Today's split is a desk policy, not a technical limit: IB
  carries futures and overseas listings (`.L`, `.AX`, `.T`, `.HK`) and `scripts/paper_global.py`
  actively *refuses* US/TSX names (`assert_ib_no_questrade_equities`), while the whole QT path —
  `cli.py signal`, `backtest`, `paper`, `tune`, the candle cache and pre-flight symbol validation —
  is wired to `_make_questrade` at 8 call sites. The ask is to make IB the equity broker as well.

  **Why it is worth doing**
    * One broker for every asset class: one calendar, one contract model, one cost model, one
      account. `AssetRouter` already maps asset class -> brokerage; today equity is the odd one out.
    * **Questrade's refresh token is one-shot and process-local.** A second QT process invalidates
      the running session's token, which is why the defense names (ITA/NOC/GD) could not be
      cached on 2026-09-22 while a QT paper session was live. IB has no equivalent constraint.
    * Costs: QT is modelled at a flat $4.95/fill; IB tiered US equity pricing is per-share with a
      small minimum, materially cheaper for the sizes this book trades — and the paper cost model
      would finally match the venue it claims to simulate.
    * Limit orders, extended hours and real-time quotes all live on the IB side of the codebase
      already (`brokers/ib.py`, `brokers/ib_web.py`, both with `quote` / `candles` / `place_order`).

  **Blocking facts — read before starting**
    * **No live quotes yet.** Probe 2026-09-21 (paper login on 7497): error 354 on *every* venue
      including SPY, twice, before and after enabling real-time sharing. Daily bars work everywhere
      except Tokyo. Per "Held for market data", an IB-fed book without quote permissions launches
      and never trades. **However** — the strategies decide on completed DAILY bars; live quotes are
      used for fills, marks and the stop/profit-lock checks. So a daily-bar IB paper book is
      testable before subscriptions exist, which is the cheap first step below.
    * The `interactive-brokers` MCP server failed to connect again on 2026-09-23
      (`CONNECTION_CLOSED`), and TWS must be listening (7497 paper / 7496 live). Any IB path
      inherits that operational dependency; QT needs only a token.
    * `brokers/ib.py` is built on `ib_insync`, which is unmaintained — the `ib_async` migration is
      already queued in the dependency-caps item. Do that first or accept the debt knowingly.

  **Work, in order**
    1. **Feed swap behind a flag**, not a rewrite: `--feed {questrade,ib}` on `cli.py signal`
       (and `backtest`). `MarketData` takes any `Broker`, so the seam is `_make_questrade`. Keep QT
       working the whole time — this must be A/B-able, not a cutover.
    2. **Cache separation.** Do NOT mix QT and IB bars in the same parquet shards: different
       adjustment and timestamp conventions. Key the cache by source, and **diff the two feeds over
       their overlap** (close-to-close deltas per symbol) before trusting IB history. Every WF
       registry param and every backtest in the repo was fitted on QT bars.
    3. **Symbol resolution and pre-flight.** `.TO` -> TSE/SMART with CAD currency, US -> SMART;
       replace the QT-candles-404 validation with IB contract resolution + a one-bar fetch, so a
       bad symbol still refuses the launch. `stock_details()` price-magnifier handling already
       exists; US/TSX shouldn't need it, but assert rather than assume.
    4. **Cost model.** Replace the flat $4.95 in `CostModel` / `PaperBroker(commission_per_trade=)`
       with IB tiered (per-share, per-order minimum, max % of notional). Paper P&L, the profit
       lock's round-trip cost gate and the min-ticket gate all read it.
    5. **Daily-bar paper A/B (the cheap first run).** Same strategy map, same `--paper-equity`, one
       QT-fed book and one IB-fed book side by side, marks from the last completed bar where live
       quotes are unavailable. Compare fills, fill prices, costs and `sortino_over_dd`. Two books
       double the alert volume — mute one.
    6. **Live path parity** only after 1-5: order types, TIF, TSX routing, account base currency
       (CAD vs USD) and the FX leg for a mixed book, plus `AssetRouter` / approval-card venue tags
       (`approval_asgi.Broker` already knows `ib`, `ib_web`, `questrade`).
    7. **Decide QT's fate explicitly.** Keep it as a second opinion on quotes, or retire it? If it
       stays, the one-shot-token constraint stays with it and must be documented where it bites
       (concurrent processes).

  **Do not** flip the desk-policy refusal in `paper_global.py` as the first move — that guard is
  what keeps a half-migrated book from double-trading a name on two venues. It comes down in
  step 6, once one venue owns the equity book.

- **Wire the RMT eigen representation into the live analysis layers (user decision 2026-09-22,
  not started).** Question raised: is the eigenvalue representation built into the top layers of
  analysis? **No — it is research-only.** `analysis/rmt.py` (commit `9b9958f`: Marchenko-Pastur
  band with a Tracy-Widom margin, k-deep embedding, trace-preserving noise shapes) has exactly one
  importer in the repo, `scripts/rmt_lstm_study.py`. Nothing in the live path imports it. Meanwhile
  every correlation the live path does use is a **raw sample estimate**, which on a 5-name book is
  mostly noise:
    * `risk/risk_model.py` `portfolio_risk(method="corr")` — raw rho with Dimson +/-1 lead-lag,
      `sqrt(r' rho r)`, run every poll for the heat gate;
    * `risk/allocation.py` via `cli.py` — CVaR + score softmax, computed once at session start,
      multiplies entry conviction through `weight_bias_for`;
    * `risk/entry_allocation.py` (new 2026-09-22) — ranks slots by conviction only, no correlation;
    * `intel/interpret.py` — no correlation input at all.

  Three entry points, **ranked by cost/benefit — do them in this order**:

    1. **Denoise the heat gate's correlation matrix.** Swap the raw rho in
       `portfolio_risk(method="corr")` for the MP-filtered one. Cheapest: self-contained, no new
       data, one call site, and the study code already exists. Highest immediate value because the
       heat gate's diversification credit is currently computed from noise — a spuriously low
       off-diagonal lets the book carry more risk than intended. **It changes when the gate binds,
       so it changes sizing:** A/B the same sessions, `sortino_over_dd` primary with Sortino,
       return and time in market beside it, plus a count of how often the gate's verdict flips.
    2. **Eigen-aware entry ranking in the parallel allocator.** Today's slot ranking is conviction
       alone, so two names loading on the same dominant eigenvector can take both free slots. Rank
       (or trim) by conviction discounted by the candidate's loading on the top eigenvector of the
       denoised matrix, i.e. prefer the name that adds a new direction. This is the honest version
       of what `weight_bias_for` is reaching for, and it belongs in the allocator now that the
       whole poll is decided at once. Needs the eig computed per poll (cheap at 5-19 names) or
       cached per session like the current bias. Medium cost, medium evidence burden: it decides
       which names enter, so judge it on realised book correlation and `sortino_over_dd`, not on
       how sensible the ranking looks.
    3. **Top-eigenvalue share as an intel thesis input** ("cross-asset correlation spike"), the
       third of the new inputs queued in the thesis-layer item above. Computed locally from cached
       daily returns, no vendor, and it measures the thing that actually hurts a concentrated book:
       everything moving together. Lowest priority of the three because it feeds the layer that
       has never been scored (see step 4 of that item) — do it after the thesis layer earns its
       keep, or alongside, but do not let a new gate start trimming size before then.

  **Caveat for all three.** MP denoising has its own parameters and the commit message records the
  sigma2 estimate already misbehaving once (a fixed-point iteration collapsed 1 -> 10 signal modes
  on crypto). Pin the estimator and the lower cut, record them, and treat a denoised matrix as a
  change to position sizing that needs a before/after — not a drop-in improvement.

- **Intel thesis layer — fix the inputs and the gates before adding theses (user decision
  2026-09-22, not started).** Question raised: only 2 of the 8 theses fire consistently, so should
  we build more on top of them? Answer: not yet, and these four steps first. Evidence is in the
  `intel/interpret.py` module header. Four theses are gated on values the feed has never reached
  (`fear_greed >= 70`/`<= 25` against an observed 54-68, zero fires in 280 reads; disasters `>= 5`
  against a max of 2; `disaster_accel` whose key is absent from every payload; conflict/energy
  accel `>= 2.0` against a max of 1.20), and the two that do fire are near-constants since the
  09-18 gate change (Complacency 81%, Conflict watch 70% of real reads). A conjunction of two
  ~80% gates still fires ~65% of the time, so composing new theses out of the existing eight adds
  words, not information. **Ordered, 1 blocks 2 and 4.**

    1. **Degraded-row detection is wrong.** 89 of 336 rows in `state/intel_overlay.jsonl` carry no
       strategic-risk reading and no VIX yet are flagged `degraded: false`. Every base rate,
       calibration and backtest of this layer reads those rows. Fix where the flag is set
       (`intel/overlay.py` / `intel/worldmonitor.py`), and treat old rows at READ time — a
       `is_real_read(row)` helper used by every replay — rather than rewriting the journal
       (`state/` is ground truth).
    2. **Trailing-percentile gates instead of hard constants.** Already flagged as the queued
       design change in the module header ("a trailing-percentile gate would be regime-robust
       where a hard constant is not"). Replace `strategic_risk >= 67` / `conflict_events_active
       >= 4` (and the accel gates) with "this read is in the top decile of the last N real reads",
       N ~90 days, with a minimum sample before the gate arms and the current constants as the
       cold-start fallback. This is what actually fixes "only 2 fire consistently": every thesis
       then fires at a designed rate in any regime, and a WorldMonitor recentring of the index
       can't silently invalidate the constants. **Sizing impact: the same gates drive the
       live-loop conviction trim** (Complacency at moderate = x0.75 on the safe-haven exemplars),
       so a firing-rate change is a position-size change — measure before adopting.
    3. **New inputs, which is where genuinely new theses can come from.** The existing feed is one
       slow-moving geopolitical index plus event counts; no rewording of it will produce a
       signal it doesn't carry.
         * **VIX via IB** (`Index("VIX", "CBOE")`): coverage 77% -> ~100% and real-time. Note the
           header's caveat — this alone changes no firing rate, both `calm_market` legs were
           already satisfied.
         * **VIX term-structure inversion** (VIX vs VIX3M, or the front two VX futures): a real
           regime marker with actual range, unlike the strategic-risk index. Check the
           "Held for market data" section first — VX futures need an IB subscription; the
           VIX/VIX3M index pair may not.
         * **Credit spreads**: HYG vs LQD vs IEF as a Questrade-fetchable proxy (no new vendor, no
           subscription, already in the cache pipeline). Widening high-yield spreads against a
           calm VIX is the honest version of the complacency thesis the current index only gestures at.
         * **Cross-asset correlation spike**: computed locally from cached daily returns — the top
           eigenvalue's share of the correlation matrix, straight out of the RMT / MP-denoising
           work on this branch (`9b9958f`). No vendor at all, and it measures the thing that
           actually hurts a 5-name book: everything moving together.
       Each new input needs coverage stats and a percentile distribution recorded before any
       thesis is written on it, so step 2's gates have something to sit on.
    4. **Score the layer before expanding it.** Nothing has ever measured whether a thesis trim
       helped. Replay the corpus, tag each thesis ONSET (not each firing read — the de-duplicated
       alert stream is what acts), and compare forward returns of that thesis's `THEME_EXEMPLARS`
       against the no-thesis baseline over 1/5/20 days, net of costs, scored on `sortino_over_dd`
       with Sortino, return and time in market beside it. State the sample honestly: one regime,
       ~4 weeks, so this is a sanity check on sign and magnitude, not validation. If the trims
       show no edge, the answer is fewer theses and a smaller trim, not more theses.

- **Limit orders, so entries stop paying the full spread + slippage (added 2026-09-22, not
  started).** Today's paper cost breakdown: $214.36 of −$595.68 was "spread + slippage", which is
  the paper broker's flat model (mid + 5 bps per fill, on ~$429k traded). Limit orders are the real
  lever against that cost, but nothing in the path supports them yet:
    * `execution/router.py` builds every order as `OrderType.MARKET` (the `Order(...)` in `submit`).
    * `brokers/paper.py` `place_order` fills immediately at mid (or touch) ± `slippage_bps`;
      `order.limitPrice` is only a fallback reference price, and `limit_price` is always null in
      `paper_orders.jsonl`.
    * `brokers/models.py` `Order` already carries `limitPrice` and serializes it for Questrade.

  Scope when picked up:
    1. **Intent → order.** A limit price on `OrderIntent` (entries only at first): e.g. join the bid
       for a buy, or mid, with a time-to-live. Stops, flatten, slot trims and V1–V3 exits stay
       market, since those need to get out.
    2. **Paper simulation.** A limit order rests in a pending book and fills on a LATER poll only
       when the market trades through it (buy: ask ≤ limit), at the limit, with no 5 bps charge.
       Unfilled orders expire at TTL and are journaled as expired, never silently dropped. Model
       adverse selection honestly: a resting buy fills mostly when price is falling.
    3. **Risk gate.** The Router gates at submission; decide whether it re-gates at fill time (the
       book, heat and kill-switch may have changed while the order rested).
    4. **Cash and slots.** The parallel allocator and the position cap must count pending orders,
       or two resting orders can oversubscribe the same headroom.
    5. **Resume.** `--resume-session` must rebuild pending orders from the journal, or expire them
       explicitly on restart. Today resume replays fills only.
    6. **Live path.** Questrade limit order placement + status polling (partial fills, cancels).
       Paper first.

  **How to judge it:** fill cost vs the mid baseline per fill, **and** the cost of missed trades
  (signals that expired unfilled, marked to where the trade would have gone), on the same
  sessions. A limit policy that saves 5 bps but misses the winners is worse. Score with
  `sortino_over_dd` as usual.

- **Runtime exercise of `composite` + `confirm_*` — STARTED 2026-09-17.** The QT strategy map now
  runs `ENB.TO=confirm_bollinger`, `SRU.UN.TO=confirm_rsi_meanrevert`, `XIU.TO=composite`; the
  other 11 names are unchanged. Kraken is unchanged (crypto would need `symbol` passed into the
  confirmation filter so gap patterns are dropped). First session: `da6c798e…`. Run ≥1 week and
  compare fill count / holding period / return per fill against those names' earlier
  base-strategy sessions. The comparison is across different days, so regime is a confound.
  Not promotion evidence.
    * **Evidence so far — one 2.5 h session, far too little to judge anything.** `2c8274d6…`
      (2026-09-17, 85 polls, 60 s warm-up then 300 s): SRU.UN.TO `confirm_rsi_meanrevert` filled
      1,500 @ 26.6983 and closed at 26.7016 on the flatten → **+$4.95 gross, roughly flat**.
      `ENB.TO` (confirm_bollinger) and `XIU.TO` (composite) **never signalled**, so both still have
      zero runtime evidence. Session net +$29.25 after $39.60 of commissions, driven by VDY.TO
      (ts_momentum), not by the new strategies.
    * All 4 fills landed on poll 1 and every later poll was a hold, so intraday polling added no
      entries — consistent with daily-bar strategies.
    * `confirm_*` run their intended pinned params (30/3.0 and 14/35) regardless of the params bug.
      `composite`'s members run class defaults.
    * **Bug found and fixed 2026-09-17 (uncommitted):** `ConfirmOverlay` masked `signal_strength`
      by the event channel only, so every level-triggered confirmed entry sized to **0 shares** on
      the `--level` QT path. Observed on `da6c798e…`: SRU.UN.TO alerted ENTRY with 0 shares.
      Fixed in `strategies/overlay.py`; regression tests in `tests/test_overlay.py` (verified
      failing on the old code). Runtime-confirmed on `2c8274d6…` (2026-09-17 15:11 UTC):
      SRU.UN.TO `confirm_rsi_meanrevert` sized **1,500 shares** and filled, the first `confirm_*`
      fill in the journals. (`da6c798e…` predates the fix and never traded the three names.)
    * The alert's "walk-forward evidence" line shows the *base* strategy's registry params
      (e.g. `rsi_meanrevert window=7, oversold=25`) under a `confirm_rsi_meanrevert` entry — the
      same misreporting bug as above.
    * `CONFIRM_STRATEGIES` zero-arg construction passes `symbol=None`, so there's no asset-aware
      pattern filter on this path. Fine for equities; needed before any crypto use.

- 🟢 **Telegram bot token leak — RESOLVED 2026-09-17.** New bot and token are in `.env`, and the
  user confirmed the old bot is revoked, so the leaked token is dead. Session scratchpad logs are
  redacted. Leftovers (hygiene only, the token is invalid): `logs/trading.log` and
  `reports/logs/paper_qt_*.log` still contain the revoked token (user's files, gitignored).
- 🟢 **Telegram delivery — FIXED 2026-09-17.** Earlier in the session `getChat` returned
  400 "chat not found" and every alert was silently rejected; after the user contacted the new
  bot it returns **ok, private chat**, with `getMe` ok (`@Fintelligence_Notifs_bot`). No alert has
  actually been delivered yet (the fix landed after both sessions were stopped) — the next paper
  session's first fill is the real confirmation. Recheck recipe, read-only and sends nothing:
  `getMe` + `getChat?chat_id=$TELEGRAM_CHAT_ID`; if `getChat` fails, `getUpdates` lists the chats
  that have contacted the bot. Remember `_telegram` never checks the HTTP status (user's choice),
  so this out-of-band check is the only way to know. httpx logs every
  request URL at INFO, and Telegram's URL contains the bot token. It's in `logs/trading.log`,
  `reports/logs/paper_qt_2026-09-09*.log`, `reports/logs/paper_qt_2026-09-10.log` and the session
  scratchpad logs (all gitignored, never committed), and it also appeared in a Claude session
  transcript. Code fix landed (uncommitted): `logging_setup.configure_logging` pins
  `httpx` / `httpcore` to WARNING. **Still needed from the user:** revoke/regenerate the token via
  BotFather and update `.env`. Then purge or redact the old log files. Any QT process started
  before the fix keeps leaking until it's restarted.
- **Runtime-exercise the IB news → graph pipeline** (unit-tested only). With TWS on 7496:
  `IBBroker(port=7496).list_news_providers()`, then
  `IB_PAPER_PORT=7496 .venv/Scripts/python.exe scripts/paper_ib.py --transport socket
  --news-providers BRFG,FLY --news-cadence-s 30`. Check the `news drained N → M edges` lines and
  `mentioned_by` rows in `state/intel_graph.jsonl`. News records carry `meta.symbol=None`, and
  news adds one `reqMktData` line per symbol against the ~100-line cap.
- **First run of `scripts/screen_futures.py`** (never run; needs TWS on 7496, read-only):
  `IB_PAPER_PORT=7496 .venv/Scripts/python.exe scripts/screen_futures.py --years 2`.
  Expect little value while futures market data is missing (see Parked).
- **Asset-class calibration — remaining after the 2026-09-15 sweep.** The fold landed for 4 of 6
  cells (WFE ≥ 1.0). Still open:
    * Crypto `bollinger n_std` (WFE 0.35) and `zscore_ou entry_z` (WFE 6.20, degenerate) were left
      on the heuristic. The basket was n=3; widen it (needs the deep-history fetch below) and re-run
      `scripts/calibration_sweep.py`.
    * RSI `oversold=35` won at the top edge of `{20,25,30,35}` in both classes. Extend the grid to
      `{40,45}`.
    * The FX slice is blocked on the FX classification bug and on FX being dropped.
    * Confirmation filter: consider dropping `bullish_engulfing` on crypto (the gap criterion is a
      rounding artefact on continuous bars). Needs backtest evidence.
### Kraken data → graph → walk-forward → calibration (one chain, in order)

Four steps that only pay off in sequence: the fetch feeds the graph and the walk-forward, and the
walk-forward feeds the calibration. Doing 2–4 before step 1's data lands produces confident-looking
numbers from a 12-day-stale cache. Both fetch scripts hit Kraken's ~1 req/s public tier, so they
**must not run concurrently** — `scripts/kraken_interleave.py` (committed `b1cdabd`) serializes them.

- **1. Fetch — IN PROGRESS 2026-09-23.**
    * *Deep history* (daily bars, `fetch_crypto_history.py`): `_daily.parquet` existed only for
      BTC, ETH, PAXG. XMR started 2026-09-23 (~2.5M trades in ~45 min; 15000-page cap ≈ 4.4h).
      Remaining after it: ZEC, LINK, then XRP, XLM, SOL, ADA, POL, UNI, AAVE — **hours per pair**,
      so ~days of wall-clock. Only runs while a session is alive; an unattended schedule needs the
      user's consent first.
    * *Tick deepening* (`deepen_kraken_trades.py`): one pass ran 2026-09-23 (12 pairs, 14.4 min,
      `--max-pages 60`). That cap does NOT close the gap — the cache had been idle since 09-10, and
      60k trades buys ~14h of BTC. After the pass: POL current, AAVE 2d behind, LINK/XMR ~5d,
      ADA/XLM/UNI 7–8d, BTC/ETH/ZEC/SOL/XRP/PAXG 10–12.6d. So `trades_24h=0` and
      `buy_vol_share=n/a` for 11 of 12 pairs in `reports/kraken_trade_intel.csv`. Each interleaved
      pass moves every pair forward by ≤ `--tick-pages`×1000 trades; closing the gap needs several.
      PAXG is frozen by design (left the traded sleeve 2026-09-23).
    * **Done when:** every sleeve pair has a `_daily.parquet`, and the tick caches are < ~1 day
      behind so a 24h microstructure window is actually populated.

- **2. Wire the fetch output into the intel graph journal — NOT WIRED, design only.** Today neither
  script imports `intel.graph`: deepening writes per-pair parquets + `reports/kraken_trade_intel.csv`,
  the history fetch writes daily parquets, and **nothing reads that CSV** (a produced-but-unconsumed
  artefact). Proposal, unchanged in shape: new predicates `flow_imbalanced`
  (weight = `buy_vol_share − 0.5`) and `vwap_gap`, emitted by the batch job (never the paper loop),
  one edge per pair per run — **no per-tick edges**, or a single pass would dwarf the ~10k-edge
  journal. Prototype behind `--emit-edges` on `deepen_kraken_trades.py`.
    * *Implementation notes gathered 2026-09-23:* `Predicate` in `intel/graph.py` is a `Literal`,
      so both names must be added there **and** to `DEFAULT_POLICIES` — a predicate with no policy
      is kept unchanged by `wash_edges` forever (that is why `traded` / `ranked_by` never prune).
      Give them fast decay (`half_life_h≈24`, `ttl_h=72`) so stale flow can't linger. Subject/object
      should be the existing `symbol` / `venue` node types, so the edges join the fill (`traded`)
      records already in the journal.
    * *Gate before emitting anything:* step 1 must be current. Emitting now would write `n/a`-derived
      or 12-day-old flow as if it were today's.

- **3. Walk-forward on the deepened data.** `scripts/walk_forward_crypto.py` reads the deep parquets
  (2y train / 6mo test, per-fold re-opt; robust = WFE ≥ 0.5 AND positive OOS AND ≥ 10 OOS trades)
  and writes `reports/walk_forward_crypto.csv`. The 09-08 run had 11 of 13 pairs shallow, so its
  tiering is not evidence. Re-run per pair as each `_daily.parquet` lands; the script deliberately
  does **not** flip the `tier` field in `CRYPTO_SLEEVE` — a human reads the report and edits
  `universe.py`, so the sleeve isn't silently bound to the last Kraken pull.
    * Open question worth answering with the fuller data: does the order-flow feature from step 2
      add anything to a WF fold, or is it decoration? Test it as a feature, not as a live gate.

- **4. Calibration / tuning of the crypto cells.** `scripts/calibration_sweep.py` sweeps Bollinger
  `n_std`, ZScoreOU `entry_z` and RSI `oversold` on a class basket with the same WF harness, scored
  on `sortino_over_dd`, writing `reports/calibration_sweep.{csv,md}`. Its crypto basket was limited
  to whichever pairs had a deep cache, so the crypto cells rest on BTC/ETH/PAXG. Re-run once step 1
  widens it. Carry the known follow-up: **RSI `oversold=35` won at the top edge of {20,25,30,35}
  in both classes — extend the grid to {40,45}**, or the "winner" is just the boundary. The script
  does not edit `analysis/calibration.py`; a human folds winners in.
    * Standing rules that apply here: `sortino_over_dd` is primary (Sharpe is reference only);
      data-first — one WF pass validates the protocol, promotion also needs the paper A/B.
- **IB OAuth 1.0a for CP Gateway** (~4–6 hr). **Blocked on user setup**: consumer key, a
  **rotated** token plus its secret (see decision #1; the old value also sits in a session
  transcript under `.claude/projects/…/bac3334b-*.jsonl`), a local RSA-2048 signing key whose
  public half is uploaded to IBKR, and the DH constants. Build: `OAuth1Auth` in `brokers/ib_web.py`,
  `--auth oauth1` on `paper_ib.py`, empty-default secret fields, respx tests, `.env.example`.
- **Symbol atlas** (`analysis/symbol_atlas.py`, ~4–6 hr) — only if cross-venue trades get proposed.
  Gaps 1 (pre-flight validation) and 3 (IB socket routing) are closed.

## Data-blocked — revisit on date

- **Risk-guard validation analysis — due now (≥1 week post-09-08).** Per-gate firing rates and
  threshold sensitivity (especially a `force_exit_atr_mult` sweep over {2.0…4.0}) from
  `paper_orders.jsonl`, `paper_fills.jsonl` and `paper_equity.csv`, written to
  `reports/risk_guard_analysis_<date>.md`. **Caveats found 2026-09-16:**
    * **Size-cap trims aren't journaled.** A trim is an accepted order with fewer shares, so it
      never appears in `rejected_reasons`. The trim rate can't be measured from the journal as-is.
      Observed only via alert vs fill: VDY.TO 1294→165 (`4a6ad96a`), 1293→578 (`b14e4de0`); ENB.TO
      1142→684. Log requested vs filled shares first.
    * The journals mix orphaned books and default-params sessions (see the bugs above).
    * `check_forced_exits` has no caller, so there's no forced-exit data to analyse.
- **Strategy-level risk stops** (`ts_momentum trail_atr_mult=4.0`; mean-reversion
  `time_stop_bars=20`; trend `stop_atr_mult=3.0`). Each must improve `sortino_over_dd` in walk-forward
  before it lands.
- **Cross-path tiers 4 + 5** (realized P&L → thesis calibration; prediction evaluation). Need weeks
  of fills matched to theses, and 21-day forward windows. Revisit ~2026-Oct / Nov.
- **OSINT × commodity-proxy correlation study** (spec frozen 2026-09-05). Response: forward
  log-returns at h∈{1,5,21}. Features per domain: raw scalar, 90d z-score, persistence count.
  Controls: 21d momentum and 21d realized vol. OLS with Newey-West errors; BH-FDR α=0.10 over
  294 tests. Proxies: USO UNG GLD SLV PPLT CPER WEAT CORN SOYB TLT IEF HYG UUP VXX. Needs ≥3 months
  of intel journal (~2026-Dec); full scope ~2027-Mar. Until then only the proxy data pipeline and
  the regression harness may be built — don't run it on the shallow corpus.
- **The `enrich_with_agents` / `intel/agents.py` LLM debate layer** — built and tested, never
  called. Held until there's an `ANTHROPIC_API_KEY`, weeks of graph depth, and a specific hypothesis
  worth the round-trip.
- **Thesis threshold drift.** The 2026-09-16 `interpret.py` cutoffs (`STRATEGIC_RISK_STRESSED=73`,
  `CONFLICT_EVENTS_ELEVATED=6`) are fitted to one 18-day regime. Re-measure base rates as the
  corpus grows; consider a trailing-percentile gate. Four gates have never fired on observed data
  (listed in the `interpret.py` module header).

## Held for market data (IB subscriptions / L2 depth)

The items below wait on market-data access that isn't in place: real IB market-data
subscriptions (US futures bundle, plus the global equity / ICE Europe / SGX / OSE feeds as needed)
and, for microstructure, Level-2 depth-of-book. Without subscriptions, error 354 means an IB-fed
book launches and never trades. Revisit when the data exists.

- **Microstructure controls beyond top-of-book — HELD on L2 market data (decided 2026-09-16).**
  Same treatment as exchange hopping: parked, not abandoned.
    * **Active today (top-of-book and exchange rules only, per the 2026-09-13 decision):**
      `execution/scheduler.py::MicrostructureConfig` — spread ceiling (50 bps equity / 30 bps
      crypto, entries only; exits are never blocked), board lots, open/close auction buffers,
      intent TTL, touch fills.
    * **Built but not wired into any trading path:**
        * `microstructure/` — `kraken_l2`, `coinbase_l2` and `bitstamp_l2` book streams,
          `orderbook`, `simulator`, `arbitrage`, `cross_exchange`, `avellaneda_stoikov`,
          `live_market_maker`, `interlisted`.
        * The `kraken-l2` CLI (read-only microprice / imbalance / OFI).
        * `IBBroker.request_l2_book` (socket only; needs a deep-book subscription per exchange).
        * `scripts/liquidity_heatmap.py` (one-shot, 09-05 run).
        * `scripts/deepen_kraken_trades.py` tick caches.
    * **Held until L2 data is available in the pipeline:**
        * The rolling microstructure accumulator (spec in git history, pre-2026-09-16 revision)
          → deeper heat maps → `LiquidityGate` (a size multiplier by hour×weekday liquidity,
          trim-only first; fail-open everywhere).
        * Depth-aware entry/exit controls: book-based impact/slippage estimates, microprice / OFI
          gating, and depth-scaled sizing in place of the flat spread ceiling.
    * **Before activating:**
        1. L2 source per venue. IB needs a deep-book subscription per exchange. Crypto venues
           publish L2 free, but it isn't wired into the paper loop.
        2. Evidence that cold-liquidity periods actually cost more: realized slippage cold vs hot,
           e.g. ≥2×. Otherwise the gate cuts size on a proxy that doesn't cost.
        3. ≥90 days of accumulated density (data-first rule).
        4. Ask before registering any scheduled collector.
    * Not part of this hold: the order-flow **intel-graph** edges from Kraken public trade ticks
      (build queue). They are research features from trade prints, not depth-based controls.

- **IB paper route: derivatives + overseas exchange hopping — UN-HELD 2026-09-21 (user decision),
  🟡 CODE-COMPLETE, UNIT-TESTED, NOT RUNTIME-EXERCISED** (TWS had no API port open).
  Decisions: **per-currency books** (no IB FX; single-currency guard kept), **live market data shared
  to the paper login**. `desk_policy.assert_ib_no_questrade_equities` lets .L/.AX/.T/.HK through
  and still refuses US/TSX/TSX-V (Questrade's). `scripts/paper_global.py` gained `--equities`
  (overseas only), `--client-id` (one per book on a shared TWS), `--min-ticket` (book currency),
  `--resume-session`, `--flatten-on-exit` (default on) + `STOP_<id>`. Futures rows in another currency
  are skipped per book. Non-CAD/USD books refuse to launch without explicit `--paper-equity` and
  `--min-ticket` (`require_explicit_book_sizing`). Caveat: a book stopped while its venue is closed
  can't flatten (SessionRouter queues the sell) — `monitor.flatten.incomplete` fires; stop it while
  open, or use `--no-flatten-on-exit` + `--resume-session`. **Next:** enable the TWS API on 7497
  (paper login), probe one quote per venue (live vs error 354), pick a basket per venue from the
  probe (lot sizes, LSE pence), then launch, e.g.:
  `paper_global.py --crypto "" --futures config/futures_universe.json --numeraire USD --client-id 51`
  (CME micros + SGX iron ore/rubber, which trade in Asia hours) and a JPY book (OSE futures + .T
  names, `--paper-equity 15000000 --min-ticket 15000 --client-id 52`).
  * **Probe 2026-09-21 (paper login DU***99 on 7497; 7496 not listening):** live quotes error 354 on
    every venue incl. SPY, twice — before and right after the user enabled real-time sharing (IB
    applies it at the next paper login, up to 24h). Daily bars work everywhere except Tokyo (error
    162, no TSEJ permission). All futures fronts resolve. Re-run: scratchpad `ib_probe.py 7497`.
  * **Pence / lots / HK — FIXED 2026-09-21, unit-tested + checked on real IB bars:**
    `IBBroker.stock_details()` (cached contract details) divides stock quotes and bars by
    `priceMagnifier` (BP. 542.3p → £5.423; ISF 100, VUSA 1 — varies per listing, so no venue rule)
    and multiplies live limit prices back. Fails closed (BrokerError) when details are missing.
    `paper_global.py` takes board lots from `sizeIncrement` (1306.T 10, 7203.T 100, 2800.HK 500,
    700.HK 100); trading.yaml `board_lots` still wins.
  * **Still open:** LSE ETFs list on `LSEETF`, but `.L` routes to `LSE`, so ISF.L / VUSA.L don't
    resolve (BP..L does). Tokyo needs a TSEJ market-data permission.

- **Off-hours coverage — the four options (laid out 2026-09-22, UNDECIDED).** No IB book can trade
  until live quotes exist; these are the ways forward, cheapest first. Recommendation: do 1, then
  2 while deciding on 3, and buy the US futures bundle before any Asia equity data — nothing on
  this route has been runtime-exercised yet, so paying for Asia equity data now buys untested
  plumbing.
    1. **Re-test the sharing (free, ~5 min).** IB applies shared market data at the next *paper
       login*; neither 2026-09-21 probe followed a fresh login. Start TWS, log into paper, re-run
       the scratchpad `ib_probe.py 7497`. Could clear everything except Tokyo.
    2. **Run the books on daily bars instead of live quotes (free, small change).** Bars already
       work for US / LSE / ASX / HK / all futures, and the strategies are daily-bar strategies —
       only the *fill* needs a quote. Synthesize the fill from the last bar, marked stale, to
       exercise routing, gates, lots, pence, rolls and session hopping end to end. Gives plumbing
       evidence only: no honest fill price, no spread, so P&L means nothing.
    3. **Buy the subscriptions.** The only path to real quotes. Highest value first: the **US
       futures bundle** (CME/NYMEX/CBOT) — futures run ~23h, rolls are wired, the USD book is
       ready. Tokyo needs one before even its bars work. ASX / LSE / HKEX are separate additions.
       Prices are per-exchange monthly fees in Client Portal → Market Data Subscriptions (some
       waived above a commission threshold); don't quote them from memory.
    4. **Reconsider what actually covers the gap.** The goal is coverage outside Canadian hours.
       Futures (~23h, USD) plus the existing 24/7 Kraken crypto book cover most of it for one
       subscription. Asian/European *equities* add genuinely different exposure but cost the most
       and carry the most unfinished work (Tokyo permissions, LSEETF routing, per-name lots).

- **Options (the instrument) — NOT WIRED, scoped 2026-09-22.** `brokers/ib.py`'s header advertises
  "equity and index options with the full Greeks surface"; what exists is contract *construction*
  only. Concretely:
    * **Exists:** `IBContract(sec_type="option", strike=…, right="C"/"P", expiry=…, multiplier=…)`
      builds an `ib_insync.Option` via `_to_ib_contract`, reachable through `quote_contract()` and
      `place_algo_order()` — both IBContract-level, outside the symbol-string `Broker` protocol.
      `scripts/probe_ib_data.py` names chain resolution (`/iserver/secdef/strikes` + `/info`) on
      the Web API, but no adapter method implements a chain walk.
    * **Missing before an option could ever be a position:** (a) a symbol grammar — `venues.py`
      reads `/ROOT` as a future and `BASE/QUOTE` as crypto, everything else as an equity, so an
      option can't be named in `--symbols`; (b) no chain/expiry/strike selection anywhere;
      (c) `Quote` carries no IV or Greeks fields, so nothing can price or hedge one; (d) the
      contract multiplier reaches the book only through `CurrencyNormalizingBroker.multiplier_for`
      (futures path) — `PaperBroker` otherwise fills shares × price, so a 100-multiplier contract
      would book at 1/100 of its real notional; (e) sizing/risk is ATR-on-the-underlying
      fixed-fractional — premium-at-risk, delta and assignment have no representation, and the
      heat/kill-switch math would read premium as notional.
    * **If it's ever wanted, the honest order is:** decide the use (covered calls / protective puts
      on existing equity exposure vs directional vs vol) → symbol grammar + chain resolution →
      multiplier through the paper fill path → Greeks on the quote model → an options-aware sizer
      → only then a paper book. Needs an options market-data subscription too (OPRA for US).
      Treat as a project, not a flag.
- **Exchange hopping, levels 2–4 — HELD (decided 2026-09-16).** Not abandoned: IB stays in the
  stack, and these levels resume once market-data access exists. *(Superseded 2026-09-21 by the
  entry above for per-currency books; L3 multi-currency and L4 remain held.)*
    * **Already built (2026-09-13, `2dd2043`):** `venues.py` (suffix → IB exchange / currency /
      hours / board lot for US, TSX, TSX-V, LSE, ASX, Tokyo, Hong Kong); the closed-venue skip
      (L1, active); venue-routed IB socket quotes and candles; the CAD numeraire via IB spot FX
      (`brokers/fx.py`); `SessionRouter` queueing of closed-venue intents with release after the
      open buffer through all gates; `scripts/paper_global.py` as one book over IB + Kraken;
      spread ceiling, board lots, auction buffers, touch fills, Dimson ±1 lead-lag correlation.
    * **Currently gated off** by the desk-policy WIP (`desk_policy.py` refuses equities and
      mixed-currency baskets on the IB paths). Re-enabling L2–4 means revisiting that guard then.
      Don't strip it now.
    * **Before trading any new venue:**
        1. Market-data subscriptions for that venue.
        2. Per-symbol walk-forward before any foreign name enters `WALK_FORWARD_VALIDATED`.
        3. Note that the US-centric OSINT overlay gives Asia and Europe names no region-specific
           intel.
    * **L3 (multi-currency accounting) caution:** FX moves become a new risk vector. A book that is
      flat in native currency can trip the drawdown kill-switch through FX alone. Numeraire-based
      math is needed across KillSwitch / heat / the allocator covariance. Test an FX-shock scenario
      before going live.
    * **L4 (24h scheduler + non-overlapping correlation):** a hypothesis, not a spec. Design it
      only after L2 and L3 have live evidence.
    * **Known limits of what's built:**
        * No exchange-holiday calendar (the stale-quote guard covers it).
        * LSE pence quoting unverified.
        * Native-currency returns in the covariance.
        * In-memory intent queue (lost on restart).
        * FX needs `--transport socket`.
- **Futures strategy research — removed 2026-09-18.** See Parked. The market-data subscription
  is also a prerequisite for reviving it.

## Exit variants — parked for comparison

Candidate exit-rule changes kept side by side so they can be compared on the same harness before any
goes live. None is enabled. Compare each against the no-change baseline on `sortino_over_dd`, **and**
on Sortino, return and time in market, because a variant that just sits out can "win" the ratio
through a smaller drawdown alone.

- **Variant #1 — profit-lock ratchet (parked 2026-09-18; folded into paper 2026-09-18 15:27 ET).**
    * **Now running on QT paper** session `ec72c96f…` with `--profit-lock` and the new
      `--profit-lock-exempt` (default `ts_momentum`). At launch the lock covers only SRU.UN.TO of
      the held names; EQB.TO, QQQ and VDY.TO are exempt `ts_momentum`. First paper exposure: judge it
      over weeks of sessions against the earlier no-lock sessions, not on one day.
    * Code in place, **off by default**: `signals/profit_lock.py`; hooked into
      `SignalSet.to_positions(profit_lock=..., round_trip_cost_frac=...)`,
      `BacktestEngine.run(profit_lock=...)` and `LiveMonitor(profit_lock=...)`. The QT flag is
      `cli.py signal --profit-lock` (QT only; not wired into `paper_kraken.py`).
    * Rule: arms at +1 ATR; exits when price falls back from the peak by more than a giveback that
      shrinks on `log10(1 + 9u)` from 3 ATR (at +1 ATR) to 1 ATR (from +4 ATR).
    * Cost cross-check: floors at net breakeven (entry + round-trip cost) and arms only once the gain
      covers 2× the round-trip cost.
    * Re-entry lockout: after a lock exit, no re-buy until the entry signal has switched off once.
      Without it, level entries (`ts_momentum`) churn a round trip per poll live.
    * Evidence: `reports/profit_lock_backtest_2026-09-18.md`. In-sample, one a-priori configuration,
      2–43 trades per name. Equal-weight 14-name QT book at realistic costs, sortino/DD:
        - no lock: 45; Sortino 2.21, DD −4.9%, +39.4%, 29% in market;
        - lock on all 14: 74; Sortino 1.89, DD −2.6%, +21.3%, 14% in market. It cuts trend
          winners: QQQ +97% → +4%;
        - lock except `ts_momentum`: 65; Sortino 2.27, DD −3.5%, +37.8%, 27% in market. The benefit
          holds at 2× costs.
      Gains concentrate in ZEB.TO, XIU.TO and VALE; CGL.TO and SRU.UN.TO are worse.
    * Unit-tested (`tests/test_profit_lock.py`, 19 tests); never runtime-exercised.
    * If revived: add a per-strategy exemption (`ts_momentum` first), walk-forward it rather than
      trusting the in-sample run, then a paper session with the flag on.
    * Kept regardless of the variant: exit alerts now carry the real reason (stop / profit_lock /
      flatten) instead of always "strategy exit signal".

- **Variant #2 — candlestick exit (built + backtested 2026-09-18; NOT recommended, off).**
    * Code in place, **off by default**: `signals/candle_exit.py`; hooked into
      `SignalSet.to_positions(candle_exit=...)`, `BacktestEngine.run(candle_exit=...)` and
      `LiveMonitor(candle_exit=..., candle_exit_exempt=...)`; QT flags `--candle-exit` /
      `--candle-exit-exempt`.
    * Rule: sell a long that is up ≥ 1 ATR and ≥ 2× round-trip cost on the bar after a bearish
      reversal completes (the 8 mirrors of the entry-confirm set). Shares the re-entry lockout;
      live reads the last completed daily bar.
    * Evidence: `reports/exit_variants_backtest_2026-09-18.md` (same harness as #1, in-sample, one
      config). Portfolio at realistic costs, sortino/DD / Sortino / return:
        - no change: 45.2 / 2.21 / +39.4%;
        - V2 on all 14: 38.0 / 1.46 / +17.3% (time in market 29% → 12%);
        - V2 except `ts_momentum`: 41.2 / 1.98 / +33.6%, DD barely moves (−4.9% → −4.8%);
        - V1 + V2: 64.8 / 2.19 / +35.9%, no better than V1 alone (65.0 / 2.27 / +37.8%).
      It gets worse as costs rise (+35 trades), and it never fires on XIC.TO, VALE, CRT.UN.TO,
      ENB.TO or XIU.TO. Consistent with `strategies/overlay.py`'s finding that candles are a
      precision lever only at mean-reversion *entries*.
    * Unit-tested (`tests/test_candle_exit.py`, 10 tests); never runtime-exercised.

- **Variant #3 — overbought (mean-reversion) exit (built + backtested 2026-09-18, off).**
    * `signals/overbought_exit.py`. Sells a long that is up ≥ 1 ATR and ≥ 2× round-trip cost on the
      bar after a close above the upper Bollinger band (20, 2 sd) or RSI(14) ≥ 70. A run with no
      down days is scored RSI 100. It shares `WinnerGate` (arming) with #2, plus the re-entry
      lockout and live completed-bar reading. QT flags `--overbought-exit` /
      `--overbought-exit-exempt`. Unit-tested (`tests/test_overbought_exit.py`, 8 tests);
      never runtime-exercised.
- **STANDING configuration (user decision 2026-09-18, revised the same day): V1 + V2 + V3 with
  `ts_momentum` exempt from all three** on QT:
  `--profit-lock --profit-lock-exempt ts_momentum --candle-exit --candle-exit-exempt ts_momentum
  --overbought-exit --overbought-exit-exempt ts_momentum`. The momentum names (EQB.TO, QQQ,
  VDY.TO) keep only their strategy exit; the other 11 names take profits on the lock, a bearish
  reversal candle, or an overbought reading. Not launched yet: QT was closed when it was set.
  Launch it at the next open (runbook 4b).
    * Backtest (`reports/exit_variants_v123_backtest_2026-09-18.md`, in-sample, one a-priori config,
      2–50 trades per name). Portfolio, sortino/DD / Sortino / max DD / return / time in market:
        - realistic costs: no change 45.2 / 2.21 / −4.9% / +39.4% / 29% → **standing 68.9 / 2.15 /
          −3.1% / +34.1% / 25%**;
        - 2× costs: no change 38.0 / 1.97 / −5.2% / +34.6% → **standing 52.8 / 1.82 / −3.5% / +28.3%**.
      So it trades ~5 pp of return and a little Sortino for a ~1.8 pp smaller drawdown, and that
      holds when costs double. Gains concentrate in XIU.TO, VALE, XIC.TO and RSI.TO; CGL.TO,
      SRU.UN.TO and ZEB.TO give up return.
    * Superseded the same day: the mix that exempted `ts_momentum` from V1 only (V2/V3 still cut the
      momentum names: 64.3 / 1.59 / +16.7%, worse than no change at 2× costs).
    * Judge it on paper over weeks against the no-change sessions, on Sortino, return and time in
      market as well as sortino/DD. Not walk-forward validated.

- **Variant #4 — buy on oversold (planned 2026-09-18, not built).** User idea to win back the return
  V1–V3 give up. Role (user decision): **trend pullbacks only**. Trend-family names buy oversold
  dips (lower Bollinger band or RSI <= 30 on the last completed bar) while their trend signal is up.
  Designed in "Plan — robust entry/exit signal system", Phase 2.
- **Scope of all exit/entry variants: the QT book, all 14 names, DBC and CGL.TO included (user
  decision 2026-09-18).** Crypto stays on its own strategy exits; no Kraken wiring.

## Parked

- **Futures strategy research — removed 2026-09-18.** The QC futures research harness is deleted
  (`integrations/lean_futures.py`, `scripts/qc_futures_empirical.py`, the shelved WF stash and the
  `qc_futures_*` / `qc_trend_zscore_*` / `qc_xsec_momentum_*` reports). Come back to it with a
  refreshed approach and a more thorough baseline. **LSTM-embedded volatility-weighted momentum**
  is deferred to a later horizon. The IB futures paper path (`futures.py`, `data/futures_*`,
  `paper_ib.py` / `paper_global.py`) is unaffected. The QC projects `frm-futures-empirical`,
  `frm-trend-zscore-wf` and `frm-xsec-momentum-wf` are kept on QuantConnect.
- **Interlisted TSX⇄NYSE arb** (`microstructure/interlisted.py`): 0/25 pairs clear at retail FX.
  If revisited, scan during market hours with a stale-quote filter and streaming quotes.
- **Live crypto routing** through `AssetRouter`, only once the go-live decision is made.
- **Tick-aware fill prices** in `PaperBroker`, alongside tick-aware router sizing.
- **Correlation matrix snapshots** to `state/{crypto,equity}_corr.jsonl` on a schedule (needs consent).
- **Carry-inversion thesis** on a real futures-curve feed instead of the stress+flow proxy.

## Open audit gaps (from 2026-09-04, re-verified 2026-09-16)

🔴
- ✅ FIXED 2026-09-18: `_atr_or_default` (NaN / inf / <= 0 / non-numeric -> 2% of price), tested. Was: `monitor/live_loop.py` (~L357): `float(last.get("atr", …)) or price*0.02` passes NaN through,
  because NaN is truthy. Sizing then runs off NaN.
- ✅ FIXED 2026-09-18: capped at `MAX_ORDER_CONFIRMATIONS = 5`, then `OrderRejected`, tested. Was: `brokers/ib_web.py` (~L503): the `place_order` auto-confirm `while` loop has **no cap**. In live
  mode it accepts warnings a human should see, and it could loop forever.
- ✅ FIXED 2026-09-18: the README step 7 now points to the pre-flight checklist instead of a copy-paste live command. Was: `README.md:141` showed `EXECUTION_MODE=live …`.
- 🔴 **`QUESTRADE_ENV` selects nothing (found 2026-09-18, needs your decision).** Deeper than the
  audited "`os.environ` override set after settings load" (`cli.py` autonomous `run`, which is
  indeed a no-op): `QuestradeBroker` never reads `questrade_env`. It always logs in at
  `login.questrade.com` (`LOGIN_HOST` hardcoded) and trades whatever account the **refresh token**
  belongs to. `questrade_env` is only a label (`settings.is_live_capable`, the `trading live` check).
  Paper sessions are unaffected (PaperBroker never sends orders to Questrade). **Risk:** the
  autonomous daemon with its default `AUTONOMOUS_ACCOUNT=practice` and a *live* token in `.env` would
  place real orders on the live account. Options: (a) map practice to Questrade's practice login
  host (verify it still exists) and pass it from `questrade_env`; (b) verify the account type from
  `/v1/accounts` at boot and refuse on mismatch; (c) refuse `autonomous start` unless
  `AUTONOMOUS_ACCOUNT == QUESTRADE_ENV == "live"` is set by the human. Not changed: the live and
  autonomous paths are yours to decide.

🟡
- ✅ FIXED 2026-09-18: SHA-256 fold; a cross-process test pins stability. Was: `brokers/kraken.py::_txid_to_int` uses `hash()`, which is randomized per process, so order ids
  change across restarts.
- ⏸ MEASURED 2026-09-18, deferred: 300 lines / 90 KB, 5.4 ms per snapshot, autonomous-only. Rewriting a risk gate for that is risk without benefit; revisit past ~100k lines. Was: `execution/daily_budget.py::snapshot` re-parses the whole `orders.jsonl` on every gate check.
- ✅ FIXED 2026-09-18: temp file + `os.replace`, first tests for the cache (`tests/test_candle_cache.py`). Was: `data/cache.py::put` writes without an atomic rename. `cache.py` paths carry no schema version,
  and `data/market.py` has no incremental tail fetch.
- ✅ VERIFIED NOT A BUG 2026-09-18: `existing_risk` is dollars (`portfolio_risk` aggregates per-position dollar risk), so `existing_risk / equity` is already a fraction, matching `HedgePolicy.heat_ref = 0.05`; `heat_boost` defaults to 0 anyway. Was: `monitor/live_loop.py` (~L553): `heat = existing_risk / equity` is dollars vs fraction when the hedge is on.
- ~~`brokers/models.py`: the `Fill.venue` Literal predates multi-venue.~~ ✅ FIXED 2026-09-17
  (`0a13789`) — open `str` + `CANONICAL_VENUES` drift test; PaperBroker now stamps the resolved
  feed venue instead of hardcoding "paper".
- ~~`execution/asset_router.py`: `AssetClass` has drifted from `OverlayClass`.~~ ✅ FIXED
  2026-09-17 (`0a13789`) — now an alias of `OverlayClass` with mappings for fixed_income /
  precious_metals / fx and a drift test. Still true and deliberate: the brokerage ids are LEAN
  deployment targets, not this repo's runtime adapters.
- `intel/apply.py::apply_overlay` has no runtime caller.
- 🟡 **`.gitignore` gaps (filed 2026-09-17).** Two, both found while mapping the repo vs the
  working tree:
    * ✅ FIXED 2026-09-18: `state/*` + `!state/.gitkeep`. Was: **`state/` uses a deny-list, not `state/*`.** Each journal is named individually, so any NEW
      state file is tracked by default — `state/sizing_decisions.jsonl` and
      `state/intel_graph.jsonl.bak` both show up as untracked right now, and every future journal
      will too. Fix: `state/*` plus `!state/.gitkeep`.
    * **`reports/*.md` is ignored while PNG/CSV/JSON are tracked** — the polarity is backwards for
      a research repo. 78 binary PNGs are versioned (reports/ is ~11 MB, larger than `src/` plus
      `tests/`) while the markdown write-ups that carry the interpretation are local-only, so a
      committed chart has no committed explanation.
- ✅ FIXED 2026-09-18: `venue: str` declared (all six brokers already set it). Was: `brokers/base.py`: the `Broker` Protocol doesn't declare `venue`.
- ✅ FIXED 2026-09-18: `trading live` now exits (code 2) before building the broker, and the misleading "routing through practice account" message is gone (see the 🔴 `QUESTRADE_ENV` item). CLI test added. Was: `cli.py` `trading live` warns but doesn't abort when `QUESTRADE_ENV != "live"`.
- ✅ FIXED 2026-09-18: `CPGatewayAuth` refuses unverified TLS to a non-loopback host (at construction and in `new_client`), tested. Was: `brokers/ib_web.py`: `verify_ssl=False` with no guard that the host is localhost.
- Missing tests — PARTLY DONE 2026-09-18: `data/cache.py` (`tests/test_candle_cache.py`) and
  `execution/journal.py` + `DailyBudget` counting (`tests/test_order_journal.py`) added;
  `data/market.py` was already covered (`tests/test_market_data.py`). Still untested: the futures
  pipeline, `microstructure/*`, `intel/{apply,chart,worldmonitor}.py`, several scripts.

🟢
- ✅ FIXED 2026-09-18: the README lists all 44 registered strategies by family plus the exit
  layers; the execution row names real modules (no `execution/paper.py` / `live.py`); the skills,
  commands and agents lists match `.claude/`. `CLAUDE.md` gains `claude-trader` and the full
  command list. Its entry points were checked and are current. Was: "The README strategy table is
  stale … README and CLAUDE.md disagree on the agent set, and CLAUDE.md's entry-points list is stale."
- ✅ FIXED 2026-09-21: per-instance counter, continued by `resume()`; tested. Was:
  `PaperBroker._order_counter` is a class variable, shared across instances.
- ✅ FIXED 2026-09-21 (per-file random salt, legacy still read): `brokers/token_store.py` used a fixed PBKDF2 salt.
- `pyproject.toml`: dependencies have no upper caps; `ib_insync` is unmaintained (consider
  `ib-async`); PyJWT isn't declared.
- `brokers/questrade.py`: `LOGIN_HOST` is hardcoded.
- Older journal rows lack `session_id`, so joins must tolerate NULL.
- ✅ CHECKED 2026-09-18, not changed: `Router.submit` runs `_gate` (kill-switch first) *before* journaling. A rejection writes one `orders.jsonl` row (`accepted: false`) plus one `rejected.jsonl` row: two journals, by design, not a duplicate. The real effect is volume, since a halted session re-submits every poll. Was: "The router journals an intent before the kill-switch check, which duplicates rows when halted."
- ✅ FIXED 2026-09-18: still never raises, but logs `error_type` / `error`, tested. Was: `intel/graph.py::append_edges` swallows all errors, so a full disk drops writes silently.
- `starting_equity` and `_INTERPRET_BIAS_FLOOR` are hardcoded rather than configurable.

## TradeCard (§11) — open work

**⚠️ The approval axis does not currently work in paper mode** — the shim 500s on every paper-mode
prompt, so no card approval can complete and no card-gated order can fill. See the 🔴 entry at the
top of "Queue — correctness bugs". Everything below assumes that is fixed first; until it is, the
end-to-end claim in this section is aspirational, not verified.

**Architecture (decided 2026-09-08):** zero vendor infrastructure. Releases go through GitHub
(sigstore-signed), and the PWA and docs through GitHub Pages. Push goes via a user-chosen relay. The
shim is single-owner-per-box, with no accounts or telemetry. See memory
`tradecard-zero-vendor-infra-architecture.md`.

**Branch status — corrected 2026-09-17. There is no TradeCard PR to open.** The earlier note said
`feat/tradecard-approval` was pushed and awaiting a PR. In fact that branch is **fully contained in
`feat/multi-scoring-attention-map`** — 0 unique commits, 25 behind — so a PR into the working branch
would be empty, and a PR into `main` would be 133 commits / 261 files / +38,538 lines of the whole
long-lived line, not TradeCard-specific work (it carries the risk/router, bulk-catchup and
symbol-validation commits too). The user declined a catch-up PR of that size on 2026-09-17 and chose
to keep the feature branch as trunk. The branch tip is preserved as the pushed tag
`archive/tradecard-approval`. **The user chose 2026-09-17 to keep the branch and its worktree at
`C:/Users/PC/Downloads/FRM-Claude-tradecard` in place (clean, 0 changes) — do not propose deleting
either.** It is harmless, just redundant, and this note explains why it exists. If a reviewable TradeCard PR is ever wanted, it needs a fresh branch off `main` with only
the approval / shim / firmware / PWA paths cherry-picked — expect conflicts, those files have moved
a long way since `main`. `gh` 2.101.0 is now installed and authenticated, so tooling is no longer
the blocker.

**Follow-up PRs:**
- `feat/tradecard-multi-broker`: `Router(brokers: dict[str, Broker], default: str)`, dispatch by
  `intent.broker`, with the WYSIWYS canonical bound to the dispatched broker. The rebase touches
  `Router._gate`, so take care.
- `feat/tradecard-settings`: signed `SettingsChangeIntent` → `POST /v1/settings` → mutate
  `trading.yaml`, journaled with verdict `S`. On the card, add a SETTINGS menu and NVS-enforced
  Tier-A rules.

**Hardware:** no schematic, BOM or PCB. No secure element (the key sits in NVS; target ATECC608A or
the S3 DS peripheral). No power path or enclosure. No display or input sourcing decision. No RF
certification or antenna choice.

**Firmware:** the LCD now renders text — `main/font5x7.h` (5×7, ASCII 0x20–0x7E) plus a real
`lcd_puts_ex` painting into `s_fb`, inverse-video rows, a 14-column prompt layout with one
signed field per row, a fixed-deadline countdown, a panning thesis, a passbook empty state and
boot status screens (2026-09-23). **Code-complete, NOT compiled** — no ESP-IDF on the box; the
font and layouts were verified by porting the renderer to a host harness and rendering the real
canonical strings from session `24a7fd7f`. `idf.py build` is still the gate. Remaining: no TLS on card↔shim; no deep
sleep; no CENTER detail view (`GET /intel/{ref}` isn't called); trust-on-first-use pairing; no OTA;
no factory reset; no fault UI; no passbook search; no long-press handling. Firmware changes from
2026-09-13 aren't compiled yet (`idf.py build`).

**VS engine:** rules only (no optional LLM narrator); no confidence or attribution in the thesis;
no historical-comparison clause; no verdict feedback loop; broker→asset-class is 1:1;
`state/intel_writeups/` grows unbounded; no FR/EN; strategies don't emit `score` / `rank` /
`r_multiple`.

**API:** no SSE/WebSocket push, admin listing endpoint, rate limiting or CORS controls; mTLS per
card is still the production target; single-broker Router.

**UX:** no onboarding, queue preview, "why" screen, haptics or LED, PIN, timeout warning, passbook
filter, language setting or accessibility sizing.

**Connectivity:** Wi-Fi only (no BLE or cellular); no status indicator; no offline queue; SSID/PSK
fixed at build time; no mDNS; no captive-portal handling; no shim handoff; no card→shim health
telemetry; the shim is loopback-only with no scripted remote path.

**Sequencing:** firmware font → hardware SE once the firmware signs via I²C → multi-broker together
with strategy `MarketContext` adoption → UX and connectivity polish once the card is a physical object.

Published brief: <https://claude.ai/code/artifact/9567c2ac-b2bd-4797-adf5-2edbce2a4d90>
