# Regulatory compliance and transaction audit trail: scope

Status: scoped 2026-10-31 against commit `ea6fe14`; implemented the same day (section 0).
Jurisdiction: **Canada** (the operator is a Canadian resident trading through Canadian-regulated
accounts). Not legal or tax advice; every regulatory line below is a question to confirm with the
dealer, the venue or an adviser, not a finding that the rule applies.

## 0. Implementation status

**History.** A hash-chained ledger was built once before, in the FRM-Claude repository on branch
`feat/multi-scoring-attention-map-wq7f42` (2026-09-24, its `AUDIT_LEDGER_SCOPE.md` phases 0–7).
None of it had reached this repository, which was rebuilt from another copy. The writer, envelope,
chain, versioning and projection are ported from there with their tests (59 of them, most of them
tamper attacks); everything below the G-numbers that branch did not cover is new here.

**Where the design differs from section 5, on purpose.** The ported ledger writes one file per
stream per UTC day (`state/ledger/<date>.<stream>.jsonl`) rather than monthly files, and names
events in its own UPPER_CASE vocabulary (`RISK_CHECK`, `BROKER_SUBMITTED`, `EXECUTION_REPORTED`)
rather than the dotted names in 5.2. One stream per process keeps the chain single-writer; the
kill-switch and other one-shot writers use a shared stream with a lock. Semantic facts (5.3) are
*derived* by a pure function from `EXECUTION_REPORTED` rows rather than written into them, because
the superficial-loss test depends on trades up to 30 days after the sale; the export records the
chain heads and FX rates it used, and its own hash goes into the ledger.

| Gap | Closed by | Evidence (tests) |
|---|---|---|
| G1 correlation id | `OrderIntent.intent_id` minted at creation (`execution/router.py`); both approval stores reuse it as the prompt id; every journal row and ledger row carries it | `test_audit_ledger`, `test_an_approval_reverifies_from_the_ledger_alone` |
| G2 replayable gate | `RISK_CHECK` / `RISK_REJECTED` carry inputs, outside observations and thresholds; `trading audit replay` (`audit/replay.py`) re-runs `_gate`, refusing rows written by other gate code | `test_the_gate_record_carries_every_input_and_replays_exactly`, `test_a_tampered_gate_input_no_longer_replays` |
| G3 executions | `executions()` on the paper, Questrade, Kraken and IB brokers; `audit/recon.py` writes `EXECUTION_REPORTED` and `RECON_BREAK`; the Router pulls immediately after each placement, `trading audit recon <venue>` later | `test_a_paper_fill_is_reported_as_an_execution_with_price_and_fee`, `test_kraken_trades_parse_into_executions` |
| G4 broker errors | `BROKER_ERROR` row and a journal line ("order state unknown"), then the exception still propagates | `test_a_broker_error_is_recorded_as_unknown_and_still_raised` |
| G5 cancels, kill-switch | `Router.cancel()` records request and outcome, `cancels.jsonl`; `KillSwitch` writes trips and clears to the shared `control` stream (transitions only) | `test_a_cancel_goes_through_the_router_and_is_recorded`, `test_the_kill_switch_records_transitions_not_every_poll` |
| G6 tamper evidence | the chain (ported) plus `trading audit anchor`: each closed day's Merkle root in a signed git tag pushed to the private remote (`audit/anchor.py`) | `test_an_anchored_day_detects_a_full_rewrite`, `test_a_broken_chain_is_never_anchored` |
| G7 provenance | `SESSION_STARTED`: git commit (+dirty), `trading.yaml` hash, redacted argv; strategy and gate versions on every row (ported) | `test_audit_versioning` |
| G8 retention | `trading audit archive DEST`: closed days, journals, FX cache and an `approval.db` backup, with a re-read SHA-256 manifest (`audit/retention.py`) | `test_the_retention_copy_skips_the_open_day_and_verifies` |
| G9 contamination | every audit test writes under `tmp_path`; ledgers open from the router's own `state_dir` | `test_a_router_given_a_tmp_state_never_writes_the_real_ledger` |
| G10 time | UTC ISO-8601 on every ledger row; FX rows record the rate date used. Clock offset is **not** measured (open) | — |
| R1/R2/R3 | `audit/semantics.py` + `config/audit_vocabulary.yaml`: identical-property pools, ACB in CAD at the Bank of Canada rate, superficial-loss test with affiliated accounts as a CSV input; `trading audit export --year` | `test_a_loss_with_a_buy_back_inside_30_days_is_superficial` and five more |
| R4 | `conduct_flags`: fast cancels, opposite-side orders resting on the same property; `trading audit query fast-cancels` | `test_a_fast_cancel_is_a_conduct_flag` |
| R5/R6/R8 | `trading audit reference --kind terms|registration|adviser --file …`: records a document's hash and date | — (paperwork is yours) |

**Found while closing G3, and fixed.** Kraken order ids were folded through Python's `hash()`,
which is randomised per process: an id in the journals could never be matched by another process
or against Kraken's trade history, and `cancel_order` sent that number to Kraken as a txid, so a
live Kraken cancel could never have worked. The fold is now SHA-256, the txid is kept as
`Order.broker_ref`, and a cancel without its txid refuses instead of sending a number.

**Spectral VaR gate (BSM_Spectral).** `BSM_Spectral/` joined this repository by subtree merge,
history kept. `risk/spectral_var.py` turns its engine (GJR-GARCH variance, semi-parametric GPD
tails, h-day horizon variance) into an opt-in Router gate, step 11, after the size caps: entries
only, it can trim or reject and never enlarge, it fails closed when the VaR cannot be computed, and
the engine's source hash is part of the gate version. `--spectral-var-cap` on the paper runners.

**Still open.**

- Anchor signing: no GPG or SSH signing key is configured in git here. `trading audit anchor`
  signs with git's configured key or `--signing-key` (`--ssh` for an SSH key); `--no-sign` makes
  an annotated tag and the row says unsigned.
- Retention destination: `trading audit archive` needs a place outside OneDrive; not chosen.
- Affiliated accounts: the superficial-loss test sees a spouse's or registered account only if you
  supply its executions as a CSV.
- Live executions arrive after the placement; run `trading audit recon <venue>` after a live
  session (or on a schedule) to pick them up. Not scheduled here.
- Clock offset (G10) is not measured.
- `IBWebBroker` has no `executions()` yet; the socket `IBBroker` does.
- Pre-ledger journals (everything before 2026-10-31) are unchained and have no intent ids. They
  are left as they are; they cannot be presented as chained.

## 1. Boundary

**In scope**

- Orders this repo originates for the operator's **own accounts**: Questrade (practice and live),
  Interactive Brokers (paper and live), Kraken (paper and live), and the in-process paper broker.
- Every step from signal to settlement: signal, sizing, risk gates, card approval, basket gate,
  dispatch, broker acknowledgement, fill, cancel, kill-switch, position and equity marks.
- The inputs to each decision: market data, overlay/intel, config, code version.

**Out of scope** (each one changes the regulatory picture, so the doc must be rewritten if it starts)

- Trading for, or sharing signals, theses or the desk with, anyone else.
- Pooled money, a company account, or any client relationship.
- Direct market access. Every order goes through a registered dealer or venue API.

## 2. Regulatory obligations that touch this repo

The dealer (Questrade, IB, Kraken) carries the dealer-side obligations. These are the ones that
fall on the account holder, or that the dealer can ask the account holder to evidence.

| # | Obligation | Who it binds | Evidence the trail must produce | Today |
|---|---|---|---|---|
| R1 | CRA books and records: keep records supporting every disposition, adjusted cost base (ACB, averaged per identical property) and fee, generally 6 years from the end of the tax year they relate to; amounts reported in CAD using the Bank of Canada rate for the day (or another rate CRA accepts, applied consistently) | Account holder | Every fill with price, quantity, fee, currency, **CAD FX rate and its source**, timestamp, venue, broker order/exec id; retained ≥ 6 years, off this laptop | Partial: paper fills carry price and fee; live fills carry neither (see G3) |
| R2 | Superficial-loss rule (CRA): a loss is denied when the same or identical property is acquired in the 30 days before or after the sale and still held 30 days after it, by you or an affiliated person (spouse, RRSP/TFSA you control). The denied loss is added to the replacement lot's ACB | Account holder | Per-symbol lot history across all venues, sessions **and the operator's other accounts** (registered accounts are outside this repo, so the export takes them as an input), with the 61-day window and the ACB adjustment | Not detectable: no lot ledger, no cross-session view |
| R3 | Business income vs capital gain (CRA): frequency, holding period, knowledge and time spent are factors, and an automated system weighs toward business income. A one-time s. 39(4) election does not cover crypto, and is unavailable to a trader | Account holder | Trade count, holding periods, the system's own description | Derivable once R1 holds |
| R4 | No manipulative or deceptive trading (CIRO UMIR 2.2 on Canadian marketplaces; provincial securities acts; the venue's own rules for Kraken and US-listed names reached through IB): wash trades, self-matching, spoofing/layering, marking the close | Account holder (and the dealer, who will ask) | Every order **and every cancel**, with the decision that produced it, showing intent to trade | Cancels not journaled (G5); no self-match guard |
| R5 | Dealer and venue API terms: personal use, automation allowed, rate limits, market-data redistribution | Account holder, by contract | Which terms version was accepted, when; request-rate evidence | Not recorded anywhere |
| R6 | Crypto trading platform registration (CSA; an unregistered platform must have filed a pre-registration undertaking) and IB's Canadian entity (CIRO member) | Venue; the account holder should confirm the entity used | Which Kraken and IB legal entities hold the accounts, and their status on the CSA / CIRO registers, checked on a date | Not recorded |
| R7 | SEC EDGAR fair access (a data-source rule, so it applies from Canada too): descriptive User-Agent, request-rate cap | Requester | Request log with host, rate | Met in code (`data/fundamentals_edgar.py:10`, `news/runner.py:8`); not logged as evidence |
| R8 | Source terms for collected news | Requester | Per-source terms note and fetch log | `config/news_sources.yaml` has no terms field |

R1, R2 and R4 decide the trail's design. R5, R6 and R8 are paperwork that the trail should
reference by hash and date, not reproduce.

## 3. What is recorded today

Read from `state/` and the writers, not from documentation.

| Journal | Writer | Holds | Missing for audit |
|---|---|---|---|
| `orders.jsonl` | `Router.submit` (`execution/router.py:430`) | strategy, symbol, action, shares, entry, stop, target, risk_dollars, accepted, rejected_reasons, ts | intent id, session id, account, venue, **gate inputs** (equity, existing_risk, open_positions, open notional), config/code version |
| `rejected.jsonl` | `Router.submit`, `ApprovalRouter` | symbol, reasons, ts | everything that identifies *which* intent was rejected |
| `fills.jsonl` | `Router.submit` (`router.py:470`) | mode, broker, order_id, symbol, shares, action, ts | **price, fee, currency, exec id**. This row is the broker's placement acknowledgement, not a fill |
| `paper_orders.jsonl` / `paper_fills.jsonl` / `paper_equity.csv` | `brokers/paper.py` | session id, venue, price, commission, fill time, equity marks | link back to the intent; a different schema from `fills.jsonl` (`quantity`/`side` vs `shares`/`action`) |
| `approval.db` `intents` | `SqliteApprovalStore` | intent id, issued/expires/resolved, verdict, consumed, broker, full sizing, nonce, canonical bytes, signer card | the signature itself; link forward to the order and fill |
| `baskets.jsonl` | `execution/basket.py` | signed basket rows, verified on read | adequate for its purpose |
| structlog output | everywhere | broker order ids, errors | not persisted to `state/`; gone when the terminal closes |

Worked example from the session on 2026-10-31 (`3f19bf3f…`): the SOL/USD buy shows up as one
`approval.db` row (ACCEPT, 16:52:42.335), one `orders.jsonl` row (16:52:42.337), one
`paper_orders.jsonl` row, one `paper_fills.jsonl` row and one `fills.jsonl` row. They are joined
today only by symbol and timestamp proximity. The three earlier EXPIRED prompts are stamped
`broker = paper` while the ACCEPT is `kraken`: same intent, two venue labels, because the
card-venue fix landed mid-day. An auditor would see that as an inconsistency unless the trail
records the code version on every row.

## 4. Gaps

Ranked by audit impact.

- **G1. No correlation id.** Nothing carries one id from signal to fill. `OrderIntent`
  (`router.py:53`) has no id; the card's `intent_id` lives only in `approval.db`.
- **G2. Gate decisions cannot be replayed.** `_gate` reads equity, existing risk, open positions
  and open notional, but `orders.jsonl` stores only the outcome. A rejection cannot be re-derived
  and an acceptance cannot be shown to have been correct.
- **G3. Live fills are not fills.** `fills.jsonl` is written from `place_order`'s return, before
  execution, with no price, fee or exec id. Real executions are never pulled back from the
  broker, so R1 cannot be met for live trading.
- **G4. Unjournaled broker failures.** `submit` catches only `OrderRejected` (`router.py:480`).
  A `BrokerError` or `TokenExpired` (its siblings in `brokers/base.py`) raised by `place_order`
  propagates with no journal row. A timeout after the broker accepted would leave a live order
  with no record here.
- **G5. Cancels are invisible.** `cancel_order` on Questrade, IB and Kraken logs to structlog
  only, and the kill-switch's cancel-all writes nothing to `state/`.
- **G6. No tamper evidence.** Every journal is a plain append (`execution/journal.py:22`) with no
  hash chain, sequence number or fsync. An edited or deleted line cannot be detected, and the
  desk's INTEGRITY tab correctly reports "no ledger".
- **G7. No provenance.** Rows don't record git commit, config hash (`trading.yaml`), market-data
  snapshot or overlay row. A decision cannot be tied to the code and data that made it.
- **G8. No retention.** `state/` is gitignored (correctly) and lives on one OneDrive-synced
  laptop. Nothing guarantees 6 years, and OneDrive sync conflicts can silently fork a file.
- **G9. Test and session contamination.** `intel_graph.jsonl` and `intel_overlay.jsonl` already
  hold rows from unknown sessions. The desk filters them, but the trail itself cannot say which
  rows are production.
- **G10. Time.** Timestamps come from the local clock with no recorded source or offset; paper
  fills use `Z`, others `+00:00`.

## 5. Target design

### 5.1 One ledger, hash-chained

`state/ledger/YYYY-MM.jsonl`, append-only, one event per line:

```
{ "seq": 1042, "prev": "<sha256 of line 1041>", "ts": "…Z", "mono_ns": …,
  "event": "gate.decided", "trace_id": "…", "session_id": "…", "venue": "kraken",
  "account": "PAPER-001", "mode": "paper", "code": "ea6fe14+dirty", "config": "<sha256 trading.yaml>",
  "body": { … } }
```

- `prev` chains each line to the previous one; a monthly file opens with the previous file's last
  hash. `seq` is gapless per file.
- Written by one `Ledger` class, with `fsync` after each append. The existing journals stay as
  they are, as derived views, so nothing that reads them breaks.
- Floats stay as written for the views; the ledger records prices and quantities as decimal
  strings, so a reconciliation does not depend on binary rounding.
- A row is never edited. A correction is a new `*.corrected` event that references the `seq` it
  corrects.

### 5.2 Event model (one `trace_id` per intent, minted at signal time)

| Event | Body (beyond the common fields) |
|---|---|
| `session.started` / `session.stopped` | argv, flags (`--require-card`, `--require-basket`), starting equity, host, clock offset if measured |
| `signal.emitted` | strategy, params hash, bar timestamp used, last close, indicator values after the no-lookahead shift |
| `intent.sized` | entry, stop, target, shares, risk dollars, sizer inputs |
| `gate.decided` | **every input `_gate` read** (equity, existing risk, open positions, open notional, heat, kill-switch state, basket id), each gate's verdict, final decision |
| `card.prompted` / `card.resolved` | intent id, canonical bytes hash, fingerprint, verdict, signer, **signature**, latency |
| `order.dispatched` | client order id, broker request summary |
| `order.acknowledged` / `order.failed` | broker order id, or the exception class and message, **including `BrokerError` and `TokenExpired`** |
| `order.cancel_requested` / `order.cancelled` | reason (manual, kill-switch, expiry), broker response |
| `execution.reported` | **pulled from the broker**: exec id, price, quantity, fee, fee currency, venue time |
| `position.marked` | per-symbol qty, cost basis, mark, FX rate, equity (supersedes the `paper_equity.csv` row) |
| `killswitch.tripped` / `killswitch.cleared` | reason, who (cleared is always a human, per CLAUDE.md) |
| `external.reference` | hash and date of an accepted terms version, a registration check, an adviser note (R5, R6, R8) |

### 5.3 Semantic layer: what each event means

The event model (5.2) records what the code did. An auditor asks what it *means*: was this an
acquisition or a disposition, which lot did it touch, what did it do to ACB, which rule does it
engage, and why did it happen. Those answers are written into the ledger at the time, as
**typed facts** with a versioned vocabulary, not re-inferred later from free-text fields.

**Vocabulary.** `config/audit_vocabulary.yaml` is committed and versioned (`vocab: 1`). Every
ledger row carries the `vocab` version it was written under. A term is never redefined: changing
a meaning adds a new term and a new version, so a 2026 row still reads the way it was meant in
2026.

**Semantic facts attached to an event.** Each `execution.reported` and `position.marked` row
carries a `sem` block, derived by one pure function (`audit.semantics.classify`) whose code
version is already on the row:

| Fact | Values | Feeds |
|---|---|---|
| `tax_event` | `acquisition`, `disposition`, `none` | R1 |
| `property` | the *identical-property* key used for ACB averaging (for example `crypto:SOL`, `ca-equity:XIC`, `us-equity:VOO`), never the venue's ticker spelling | R1, R2 |
| `lot_effect` | quantity and ACB before → after, in CAD, with the FX rate and its source | R1 |
| `gain_cad` | proceeds − ACB − outlays on a disposition, signed | R1, R3 |
| `superficial_window` | the 61-day window's start and end, and every `trace_id` that acquired the same `property` inside it | R2 |
| `denied_loss_cad` / `carried_to` | the denied amount and the replacement lot it was added to | R2 |
| `holding_days` | for the closed quantity, first-in basis for reporting only (ACB stays averaged) | R3 |
| `rules_engaged` | the R-numbers this row is evidence for | all |
| `conduct` | `self_match_risk` when an opposite-side order on the same `property` was resting at dispatch; `cancel_before_fill` with the time open; otherwise empty | R4 |

**Why, not just what.** Every row on a `trace_id` can be followed back to the decision that
caused it. `signal.emitted` names the rule that fired, in the strategy's own terms (`close
crossed below the lower band, shifted one bar`), and `card.resolved` records that a human
accepted the exact canonical bytes. Free text is allowed only in fields marked `note`, and
`audit verify` rejects a semantic field that holds a value outside the vocabulary.

**Semantic queries.** The point of typing these facts is that the audit questions become
queries over the ledger, not forensic reconstructions:

- every disposition at a loss in 2026 with a reacquisition inside its window, and where the
  denied loss went;
- every order a human approved, the bytes they approved, and whether what executed matched them
  (symbol, side, quantity, venue);
- every gate rejection whose replayed inputs would now pass (a gate-logic change shows up here);
- every cancel within N seconds of dispatch (R4);
- every row written under code or vocabulary version X.

`trading audit query` ships these as named queries; `audit export` is one of them.

**Guards.** `classify` is pure and deterministic, so `audit replay` re-derives every `sem`
block and diffs it, the same way it re-derives `gate.decided`. A semantic fact that cannot be
determined (for example no FX rate for the day) is written as `unknown` with the reason, never
filled in, matching the repo's rule that every figure comes from execution.

### 5.4 Execution reconciliation

A pull, not a push: after each acknowledgement, and once per session end, fetch executions from
the broker (Questrade `executions`, IB `reqExecutions`, Kraken `TradesHistory`) and write
`execution.reported`. A broker execution with no matching `trace_id`, or a dispatched order with no
execution after its expiry, is written as `recon.break`. That is what turns G3 and G4 from
silent into visible.

### 5.5 Audit tooling

- `trading audit verify`: walks the chain, checks `seq` and `prev`, verifies every card signature
  against `approval.db` cards, reports the first break.
- `trading audit trace <trace_id>`: the full lifecycle of one order, signal to execution.
- `trading audit replay --session ID`: re-runs `_gate` from the recorded inputs and diffs
  against the recorded decisions.
- `trading audit export --year 2026`: a tax-lot CSV (acquisitions, dispositions, ACB, fees, FX)
  in CAD, plus superficial-loss flags with the denied loss carried into the replacement ACB (R2), and
  the chain head hash printed on the report.
- `trading audit anchor`: writes the current head hash to a place this laptop cannot rewrite
  (an email to yourself, or a signed git tag in a separate repo). Periodic anchoring is what
  makes the chain evidence rather than a checksum.

### 5.6 Retention

Monthly ledger files are closed, compressed and copied to a second location after the anchor is
written. Six years at minimum. `state/` stays out of git.

## 6. Acceptance criteria for the audit

1. Every live or paper order on and after cut-over can be traced from `signal.emitted` to
   `execution.reported`, or to a recorded reason it stopped, by `trace_id` alone.
2. `audit replay` reproduces every `gate.decided` outcome, with zero diffs.
3. `audit verify` passes on an untouched ledger. Deleting, editing or reordering any one line
   makes it fail and name the line (a test per tamper class).
4. A `BrokerError` raised after dispatch, and a kill-switch cancel-all, each leave ledger rows
   (regression tests for G4 and G5).
5. A sell at a loss followed by a buy-back of the same symbol within 30 days, across two sessions,
   is flagged by `audit export`, and the denied loss lands in the new lot's ACB.
6. No test writes to the real `state/ledger/` (the G9 regression).
7. Every row on and after cut-over carries `code`, `config` and `vocab`.
8. `audit replay` re-derives every `sem` block with zero diffs, and a value outside the vocabulary
   fails `audit verify`.

## 7. Phasing

| Phase | Work | Closes |
|---|---|---|
| 1 | `Ledger` class, hash chain, `trace_id` on `OrderIntent`, `gate.decided` with inputs, journal `BrokerError`/`TokenExpired`, `audit verify` | G1, G2, G4, G6, G7 |
| 2 | Cancel and kill-switch events; card signature in the ledger; `audit trace` and `audit replay` | G5, R4 |
| 3 | Broker execution pull and `recon.break` for Questrade, IB and Kraken | G3, R1 |
| 4 | Vocabulary v1, `audit.semantics.classify`, ACB lot ledger in CAD (Bank of Canada daily rate), `sem` blocks, `audit query` / `audit export`, superficial-loss flags | R1, R2, R3, R4 |
| 5 | Anchoring, retention copy, `external.reference` entries for terms and registration | G8, R5, R6, R8 |

Phases 1 and 2 touch `Router.submit`. CLAUDE.md forbids editing the router while the autonomous
daemon is running, so they ship only with the daemon stopped. Nothing in this plan changes or
reorders a gate.

## 8. Decisions for the operator

- **Affiliated accounts.** Superficial loss counts buy-backs in a spouse's account or your
  RRSP/TFSA. Are those fed to `audit export` as a CSV, or declared out of scope in writing?
- **Characterisation.** Whether to treat this as business income from the start. It changes what
  the export totals (income and expenses vs capital gains), not what the ledger records.
- **Anchor target.** Where the head hash goes, off this machine.
- **Back-fill.** Whether to convert the existing journals into a "pre-ledger" archive with a
  single anchored hash (honest, not chained per row), or start the ledger clean from cut-over.
  Rows written before cut-over must never be presented as chained.
- **Retention location.** The second copy, outside OneDrive.
