# Porting `FRM-Claude` into this repo: the comparison

Your runbook's flags live on `FRM-Claude`, branch `feat/multi-scoring-attention-map-wq7f42` (tip `69b4339`, 2026-10-04). This
repo was rebuilt on 2026-10-30 from `b7a4962` (2026-09-09), the tip of `feat/tradecard-approval`, so the 53 commits after that
never came across. This records what each side has, and what happens where they overlap.

Read from code and commit messages on 2026-11-02. A row marked "by name" has only been read at that level so far.

Landed as a merge commit whose second parent is `69b4339`, so their history comes with it. `tests/test_runbook.py` pins the
desk's commands. Exercised for real on Kraken paper: one poll with the runbook's flags, and the card path through the simulated
card to a fill, with the desk routes and a graceful stop. The Questrade command is checked as far as parsing into a plan: running
it needs the token. No money session has been run.

## The shape of it

| | |
|---|---|
| Common point | `b7a4962`, an ancestor of their tip, so a true three-way merge is possible |
| Commits to bring | 53, kept in history as a merge commit |
| Files they add or change since that point | 285 added, 83 changed, 0 deleted |
| Trial merge against `db71392` | 54 files merge cleanly, 40 conflict, nothing is deleted from either side |
| Largest conflicts | `pwa/desk.template.html` 49 hunks, `scripts/build_desk_dash.py` 25, `monitor/live_loop.py` 14, `execution/router.py` 12, `execution/approval.py` 12 |

## Only on their side

| Layer | What | Where |
|---|---|---|
| Exits | Profit-lock ratchet, candle exit, overbought exit | `signals/profit_lock.py`, `candle_exit.py`, `overbought_exit.py` |
| Entries | Oversold add to a held trend position | `signals/oversold_entry.py` |
| Sizing | Parallel entry allocation: each poll's entries sized jointly | `risk/entry_allocation.py` |
| Sizing | Fractional units with lot step and minimums, from Kraken's own rules | `risk/quantity.py`, `KrakenBroker.lot_rules` |
| Sizing | Volatility-scaled per-name cap | `risk/position_cap.py` |
| Sizing | Sizing v2 correctness fixes | `risk/sizing_policy.py` |
| Strategy | One parameter chain, so paper can run walk-forward parameters (`--params`) | `analysis/params.py` |
| Execution | Per-venue cost model, used by the gate and paper fills | `execution/venue_costs.py` |
| Execution | Cost-aware size floor (`--max-cost-ratio`) | `execution/router.py` |
| Execution | Stale-quote guard on feeds | `brokers/fresh.py` |
| Execution | One CAD numeraire across venues | `brokers/fx.py` |
| Execution | One feed across venues, session-aware queueing, 24-hour book | `brokers/routed.py`, `execution/scheduler.py`, `scripts/paper_global.py` |
| Execution | Commodity futures book, rolls | `futures.py`, `curves.py`, `scripts/discover_futures.py`, `screen_futures.py` |
| Runner | Warm-up cadence, flatten on exit, stop by sentinel, `--resume-session`, `--close-symbols`, `--trim-to-slots`, `--mute-alerts`, `--max-positions` | `monitor/live_loop.py`, `brokers/paper.py`, runners |
| Card and desk | One shim port for three brokers (`--card-attach`, `--card-shim-host`), metrics and stats endpoints, 8-inch panel | `execution/approval_asgi.py`, `approval_metrics.py`, `audit/meters.py`, `docs/DESK_PANELS.md` |
| Policy | Which class trades on which broker, enforced | `desk_policy.py` |
| Intel | Graph-weighted interpretation, thesis intensity | `intel/graph_interpret.py`, `thesis_intensity.py` |
| Research (by name) | Factors, factor edges, RMT, spectral filters, curvature, edge tails, effect size, LSTM-Transformer study, Quantpedia client, sweeps | `analysis/*`, `models/*`, `integrations/*`, `scripts/*` |
| Society and owners (by name) | Society influence on the Router, OASIS graph adapter, membership model, owner-only allocation | `sim/*`, `return_allocation.py`, `scripts/allocate.py` |
| Security | Stops httpx writing the Telegram bot token into logs | `logging_setup.py`, commit `3b00c28` |
| Other | 55 test files, session reports, `reports/` research output, `vm/`, `uv.lock` | |

## Only on this side

All of it stays.

| Layer | What | Where |
|---|---|---|
| Runner | One session builder, a money runner for every venue, `trading doctor`, the paper/money plan comparison | `monitor/session.py`, `doctor.py`, `tests/test_session.py` |
| Risk | Reducing orders pass the halt, ticket, heat and budget gates; route check; spectral VaR gate; live auto-halt | `execution/router.py`, `risk/spectral_var.py`, `risk/equity_watch.py` |
| Execution | Several brokers on one Router; Questrade token environment check; Kraken equity and positions over the whole book; execution reports on every broker | `execution/router.py`, `brokers/*` |
| Audit | Recon, replay, CRA semantics, anchors, retention, `fill-quality`, `session-plans`, sizing recorded on signal rows | `audit/*` |
| Monitor | In-flight guard, per-symbol entry triggers, same-poll book tracking, forced exits wired | `monitor/live_loop.py` |
| Research | News collector, GKG, feature store, risk lens, Atlas, OASIS seed and result boundary | `news/*`, `analysis/*`, `atlas/*`, `sim/*` |
| Symbols | Symbol atlas | `analysis/symbol_atlas.py` |
| Engine | BSM_Spectral, by subtree merge | `BSM_Spectral/` |

## Where they overlap, and what happens

Rule: neither side loses anything. Where both solved one problem, the better-evidenced version becomes the source and the other
stays as a thin layer over it, or is kept beside it when they do different jobs.

| Problem | Here | There | Decision |
|---|---|---|---|
| What a fill costs | `DEFAULT_PAPER_COSTS`, my assumptions. **My Kraken 40 bps looks wrong**: they carry about 26 bps taker | `VenueCostModel`, built from fee shapes per venue | Theirs is the source. `paper_costs` stays as an override |
| How paper fills | `cross_spread` | `fill_model="touch"` | Same idea. Theirs is the name; every runner sets it |
| Market hours | `analysis/venue_calendar.py`, `session_open_for` | `venues.py`, `market_open_for`, `skip_closed_venues` | The monitor takes their `market_open_for` hook. Mine still answers it, for its Globex and FX weeks; either setting turns the guard off. `venues.py` serves `paper_global.py` and `desk_policy.py` |
| Gross leverage | Caller passes open notional, tracked within a poll | Gate reads the book itself (`_book_notional`) | Theirs in the gate, recorded for replay; my same-poll tracking stays for the heat and position counts |
| Same-poll sizing | Tracks room as entries go | Parallel allocation sizes them jointly | Theirs for sizing; my tracking remains the safety net |
| Whole-unit sizing | Open question 13 | `QuantityRule` | **Answered by theirs** |
| Home currency | Open question 2, warns only | `CurrencyNormalizingBroker` in CAD, used by `paper_global.py` only | Decided 2026-11-03: CAD on every runner, at the Bank of Canada's rate |
| IB on equities | Open question 6 | `desk_policy.assert_ib_futures_only` | Decided 2026-11-03: derivatives and overseas listings, never North American equities. `ib_futures_only: true` narrows it to futures |
| IB prompts | Reject by default, capped | Auto-confirm, capped at a limit | Mine: this path only runs with real money. Their cap constant goes away only as a duplicate |
| Kraken order ids | SHA-256 fold, `broker_ref`, real-txid cancel | SHA-256 fold | Same fix. Mine keeps the txid map |
| Kraken order size | None | `lot_rules` | Theirs added |
| Multi-venue | Several brokers on one Router | One feed routed by symbol | Different jobs. Both stay |
| Strategy parameters | Sleeve only | `analysis/params.py` | Theirs, behind `--params` |
| The ledger | Same origin (2026-09-24), extended here | Same origin | Mine is the base; any later additions of theirs are folded in |
| Society influence | None | Optional multiplier on the Router; can only reduce | Theirs added, off unless a view is supplied |
| Desk panel | Rebuilt 2026-10-31 | 8-inch panel, 2026-09-30 | Mine is the page. Their endpoints are in the shim; their page is kept beside it, not yet folded in (see below) |
| Runner flags | Session builder | Flags on each runner | Their flags become `SessionOptions` and `trading.yaml` knobs, so paper and money both get them |

## Default changes to expect

These arrive with their code, and are the things most likely to move results:

- **Stale quotes:** `on_stale_quote` defaults to `raise`: a frozen quote skips that symbol. There this guarded paper only;
  here it guards every runner.
- **Per-name cap:** `position_cap_mode` defaults to `vol_scaled`. There this was paper only; here it reaches money too, so a
  live name is capped as its rehearsal was. `position_cap_mode: static` restores the flat cap everywhere.
- **Parallel sizing:** on by default on every runner.
- **Basket gate:** on whenever the card is on, as theirs. A card session needs a card-signed basket before it enters.
- **Paper fills:** every runner fills at the touch here. Their runners filled at the mid by default, so paper results here
  will read slightly worse than the same session there.
- **IB:** refuses North American equities, so `DEFAULT_IB_SYMBOLS` is kept but refused. Pass `--symbols` or `--futures`.
- **Flatten on exit:** their `signal` defaults to closing all positions when a paper session stops. Your runbook passes
  `--no-flatten-on-exit`. It is paper-only and stays so; a money session never flattens by itself.

## Not brought in

`pwa/desk.html` is generated from journals and this repo keeps it out of git, so their committed copy stays out.

## Kept beside rather than merged

Nothing was deleted, but these two are not one yet:

- **The desk page.** `pwa/desk.template.html` and `scripts/build_desk_dash.py` are this repo's. Their 8-inch panel is
  `pwa/desk.panel8.template.html` and `scripts/build_desk_panel8.py`, unchanged. `/desk` serves this repo's page.
- **OASIS.** Their overlay-seeded `sim/contract.py` and this repo's VM-seeded `sim/seed.py` both define `RunBudget`, `SimSeed`,
  `SimResult` and `build_seed`. The package exports this repo's; theirs is imported from `sim.contract`, and its command line
  is `scripts/oasis_contract_sim.py`.

## Dropped as duplicates

Each had a twin doing the same job, and no behaviour left with it: `ApprovalRouter._ledger` and `_ledger_verdict`
(`record_gate` and `_record_verdict` write those rows); their inline basket, IB-probe, meters and `/desk` routes (this repo's
are served from `execution/desk_feed.py`, and theirs would have shadowed them); their second `verify_audit_record`.
