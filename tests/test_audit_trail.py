"""The audit trail's gap closures (docs/compliance/AUDIT_SCOPE.md section 4), one by one.

G1 one identity, G2 replayable gate decisions, G3 broker-reported executions, G4 broker errors,
G5 cancels and the kill-switch, G6 tamper evidence plus anchors, G8 retention, the semantic layer
(CRA ACB, superficial loss) and the spectral VaR gate. Every test writes under ``tmp_path``.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tests.test_router import _intent, _StubBroker
from trading_live_claude.audit.anchor import (
    day_anchor,
    merkle_root,
    tag_anchor,
    verify_day,
    write_tag,
)
from trading_live_claude.audit.ledger import Ledger, verify_chain
from trading_live_claude.audit.queries import approved_vs_executed, export_year
from trading_live_claude.audit.recon import reconcile
from trading_live_claude.audit.replay import replay
from trading_live_claude.audit.retention import archive
from trading_live_claude.audit.semantics import (
    BankOfCanadaFX,
    classify,
    conduct_flags,
    property_key,
)
from trading_live_claude.brokers.base import BrokerError, TokenExpired
from trading_live_claude.brokers.models import Order, OrderAction, OrderType
from trading_live_claude.brokers.paper import PaperBroker
from trading_live_claude.execution.approval import (
    ApprovalRouter,
    CardRegistry,
    InMemoryApprovalStore,
    verify_audit_record,
)
from trading_live_claude.execution.router import OrderIntent, Router
from trading_live_claude.risk.kill_switch import KillSwitch
from trading_live_claude.risk.spectral_var import SpectralVarGate

ROOT = Path(__file__).resolve().parent.parent


def _led(tmp_path: Path, stream: str = "t", **kw) -> Ledger:
    return Ledger(tmp_path / "ledger", stream=stream, session_id=kw.pop("session_id", "s1"), **kw)


def _events(led: Ledger) -> list[str]:
    return [r["event"] for r in led.rows()]


def _paper(tmp_path: Path) -> PaperBroker:
    return PaperBroker(feed=_StubBroker(), starting_equity=100_000.0, journal_dir=tmp_path,
                       venue="kraken")


# --------------------------------------------------------------------------- G4 broker errors

class _ErrBroker(_StubBroker):
    def __init__(self, exc: Exception) -> None:
        super().__init__()
        self.exc = exc

    def place_order(self, order):
        raise self.exc


@pytest.mark.parametrize("exc", [TokenExpired("token gone"), BrokerError("504 gateway")])
def test_a_broker_error_is_recorded_as_unknown_and_still_raised(tmp_path: Path, exc) -> None:
    led = _led(tmp_path)
    router = Router.build_default(mode="paper", broker=_ErrBroker(exc), state_dir=tmp_path, ledger=led)
    with pytest.raises(type(exc)):
        router.submit(_intent(), equity=100_000.0, existing_risk=0.0, open_positions=0)
    err = [r for r in led.rows() if r["event"] == "BROKER_ERROR"]
    assert len(err) == 1 and err[0]["payload"]["order_state"] == "unknown"
    assert err[0]["payload"]["error_type"] == type(exc).__name__
    rejected = json.loads((tmp_path / "rejected.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert "order state unknown" in rejected["reasons"][0]
    assert rejected["intent_id"] == err[0]["intent_id"]


# --------------------------------------------------------------------------- G5 cancels, kill-switch

class _CancelBroker(_StubBroker):
    def __init__(self, fail: bool) -> None:
        super().__init__()
        self.fail = fail

    def cancel_order(self, account_number, order_id):
        if self.fail:
            raise BrokerError("already filled")


@pytest.mark.parametrize("fail", [False, True])
def test_a_cancel_goes_through_the_router_and_is_recorded(tmp_path: Path, fail: bool) -> None:
    led = _led(tmp_path)
    router = Router.build_default(mode="paper", broker=_CancelBroker(fail), state_dir=tmp_path,
                                  ledger=led)
    ok = router.cancel("PAPER-001", 7, reason="kill-switch sweep", intent_id="i-7")
    assert ok is (not fail)
    assert _events(led) == ["CANCEL_REQUESTED", "CANCEL_FAILED" if fail else "CANCELLED"]
    assert all(r["broker_order_id"] == 7 and r["intent_id"] == "i-7" for r in led.rows())
    row = json.loads((tmp_path / "cancels.jsonl").read_text(encoding="utf-8"))
    assert row["ok"] is (not fail) and row["reason"] == "kill-switch sweep"


def test_the_kill_switch_records_transitions_not_every_poll(tmp_path: Path) -> None:
    ks = KillSwitch(tmp_path, max_drawdown_pct=0.10)
    for _ in range(3):                                   # evaluate() re-trips every poll
        ks.evaluate(equity=80_000.0, peak_equity=100_000.0, day_open_equity=80_000.0)
    ks.clear("I HAVE INVESTIGATED")
    control = Ledger(tmp_path / "ledger", stream="control")
    assert _events(control) == ["KILL_SWITCH_TRIPPED", "KILL_SWITCH_CLEARED"]
    assert "max-drawdown" in control.rows()[0]["payload"]["reason"]
    assert control.verify() == (True, "ok")


def test_two_writers_on_a_shared_stream_keep_one_chain(tmp_path: Path) -> None:
    """The kill-switch can be tripped by the book and by `trading kill` at once."""
    a = Ledger(tmp_path / "ledger", stream="control", shared=True)
    b = Ledger(tmp_path / "ledger", stream="control", shared=True)
    for i in range(5):
        a.append("KILL_SWITCH_TRIPPED", {"by": "a", "i": i})
        b.append("KILL_SWITCH_TRIPPED", {"by": "b", "i": i})
    rows = a.rows()
    assert [r["seq"] for r in rows] == list(range(10))
    assert verify_chain(rows) == (True, "ok")


# --------------------------------------------------------------------------- G1 / G2 gate record + replay

def test_the_gate_record_carries_every_input_and_replays_exactly(tmp_path: Path) -> None:
    led = _led(tmp_path)
    router = Router.build_default(mode="paper", broker=_StubBroker(), state_dir=tmp_path,
                                  ledger=led, max_position_notional_pct=0.02)
    intent = _intent(shares=100, entry=100.0)
    router.submit(intent, equity=100_000.0, existing_risk=300.0, open_positions=1,
                  current_open_notional=5_000.0)
    check = next(r for r in led.rows() if r["event"] == "RISK_CHECK")
    p = check["payload"]
    assert p["requested_shares"] == 100 and p["shares"] == intent.shares < 100     # trimmed
    assert p["inputs"] == {"equity": 100_000.0, "existing_risk": 300.0, "open_positions": 1,
                           "current_open_notional": 5_000.0}
    assert p["observed"]["kill_switch_halted"] is False and "heat_pct" in p["observed"]
    assert [r["payload"]["cap"] for r in led.rows() if r["event"] == "RISK_TRIMMED"] == ["symbol_notional"]
    assert {r["intent_id"] for r in led.rows()} == {intent.intent_id}
    assert [r["status"] for r in replay(led.rows())] == ["match"]


def test_a_tampered_gate_input_no_longer_replays(tmp_path: Path) -> None:
    led = _led(tmp_path)
    router = Router.build_default(mode="paper", broker=_StubBroker(), state_dir=tmp_path,
                                  ledger=led, min_ticket_usd=50_000.0)
    router.submit(_intent(), equity=100_000.0, existing_risk=0.0, open_positions=0)
    rows = led.rows()
    rejected = next(r for r in rows if r["event"] == "RISK_REJECTED")
    rejected["payload"]["observed"]["kill_switch_halted"] = True     # a claim the gate never saw
    out = replay([rejected])
    assert out[0]["status"] == "differs" and "rejected_reasons" in out[0]["diffs"]


def test_a_row_from_other_gate_code_is_reported_not_replayed(tmp_path: Path) -> None:
    led = _led(tmp_path)
    Router.build_default(mode="paper", broker=_StubBroker(), state_dir=tmp_path, ledger=led).submit(
        _intent(), equity=100_000.0, existing_risk=0.0, open_positions=0)
    row = next(r for r in led.rows() if r["event"] == "RISK_CHECK")
    row["risk_check_version"] = "0" * 16
    assert replay([row])[0]["status"] == "code changed"


# --------------------------------------------------------------------------- G3 executions

def test_a_paper_fill_is_reported_as_an_execution_with_price_and_fee(tmp_path: Path) -> None:
    broker = _paper(tmp_path)
    led = _led(tmp_path, stream="kraken", session_id=broker.session_id)
    router = Router.build_default(mode="paper", broker=broker, state_dir=tmp_path, ledger=led)
    intent = _intent(shares=10)
    router.submit(intent, equity=100_000.0, existing_risk=0.0, open_positions=0)
    ex = [r for r in led.rows() if r["event"] == "EXECUTION_REPORTED"]
    assert len(ex) == 1 and ex[0]["intent_id"] == intent.intent_id
    p = ex[0]["payload"]
    assert p["matched"] and p["quantity"] == 10 and p["price"] > 100.0 and p["fee"] == pytest.approx(10 * p["price"] * 0.0026)
    assert p["currency"] == "USD" and p["currency_source"].startswith("listing")
    again = reconcile(led, broker, "PAPER-001", grace_s=0.0)       # nothing is written twice
    assert again.reported == 0 and again.breaks == []


def test_an_execution_no_intent_accounts_for_is_a_break(tmp_path: Path) -> None:
    broker = _paper(tmp_path)
    led = _led(tmp_path, session_id=broker.session_id)
    # A trade placed straight on the broker, not through the Router: nothing in the ledger.
    broker.place_order(Order(symbol="AAPL", action=OrderAction.BUY, orderType=OrderType.MARKET,
                             totalQuantity=3))
    s = reconcile(led, broker, "PAPER-001")
    assert s.reported == 1 and [b["kind"] for b in s.breaks] == ["execution_without_intent"]
    assert reconcile(led, broker, "PAPER-001").breaks == []          # once per finding


def test_a_silent_order_becomes_a_break_after_the_grace_period(tmp_path: Path) -> None:
    led = _led(tmp_path)
    router = Router.build_default(mode="paper", broker=_StubBroker(), state_dir=tmp_path, ledger=led)
    router.submit(_intent(), equity=100_000.0, existing_risk=0.0, open_positions=0)

    class _Silent(_StubBroker):
        name = "stub"

        def executions(self, account, *, since=None):
            return []

    s = reconcile(led, _Silent(), "PAPER-001", grace_s=0.0,
                  now=datetime.now(UTC) + timedelta(seconds=1))
    assert [b["kind"] for b in s.breaks] == ["order_without_execution"]


def test_kraken_order_ids_are_the_same_in_every_process() -> None:
    """``hash()`` was randomised per process; the journals' id could never be joined again."""
    code = ("import sys; sys.path.insert(0, 'src'); "
            "from trading_live_claude.brokers.kraken import _txid_to_int; "
            "print(_txid_to_int('OZAA6H-FQI3S-DK6GHJ'))")
    outs = {subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT,
                           env={**os.environ, "PYTHONHASHSEED": seed}).stdout.strip()
            for seed in ("1", "2", "3")}
    assert len(outs) == 1 and outs.pop().isdigit()


def test_a_kraken_cancel_without_its_txid_refuses_instead_of_sending_a_number() -> None:
    from trading_live_claude.brokers.kraken import KrakenBroker
    b = KrakenBroker(api_key="k", api_secret="czJjcjN0", enable_live_orders=True)
    with pytest.raises(BrokerError, match="no Kraken txid"):
        b.cancel_order("acct", 123)


def test_kraken_trades_parse_into_executions(monkeypatch) -> None:
    from trading_live_claude.brokers import kraken
    monkeypatch.setattr(kraken, "private_post", lambda *a, **k: {"trades": {"T1": {
        "ordertxid": "OABC", "pair": "SOLUSD", "time": 1761900000.5, "type": "buy",
        "price": "95.88", "vol": "34", "fee": "5.21"}}})
    b = kraken.KrakenBroker(api_key="k", api_secret="czJjcjN0")
    (e,) = b.executions("acct")
    assert (e.symbol, e.side, e.quantity, e.price, e.fee, e.currency) == ("SOL/USD", "Buy", 34.0, 95.88, 5.21, "USD")
    assert e.order_id == str(kraken._txid_to_int("OABC"))


# --------------------------------------------------------------------------- card evidence

class _SigningStore(InMemoryApprovalStore):
    def __init__(self, registry, key, decision="ACCEPT") -> None:
        super().__init__(registry)
        self.key, self.decision = key, decision

    def wait(self, intent_id, *, timeout=None):
        prompt = next(p for p in self.pending() if p.intent_id == intent_id)
        self.respond(intent_id, decision=self.decision, card_id="card-sim-001",
                     signature=self.key.sign(prompt.canonical.encode("utf-8")))
        return super().wait(intent_id, timeout=timeout)


def test_an_approval_reverifies_from_the_ledger_alone(tmp_path: Path) -> None:
    key = Ed25519PrivateKey.generate()
    reg = CardRegistry()
    reg.register("card-sim-001", key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    broker = _paper(tmp_path)
    led = _led(tmp_path, stream="kraken", session_id=broker.session_id)
    inner = Router.build_default(mode="paper", broker=broker, state_dir=tmp_path, ledger=led)
    intent = _intent(shares=10)
    ApprovalRouter(inner, store=_SigningStore(reg, key), ttl_seconds=30).submit(
        intent, equity=100_000.0, existing_risk=0.0, open_positions=0)
    ev = _events(led)
    assert ev[:4] == ["INTENT_CREATED", "RISK_CHECK", "INTENT_SENT", "APPROVED"]
    assert ev[4] == "SIGNED" and "FILLED" in ev and "EXECUTION_REPORTED" in ev
    signed = next(r for r in led.rows() if r["event"] == "SIGNED")
    assert signed["payload"]["prompt_id"] == intent.intent_id        # one identity end to end
    ok, why = verify_audit_record({"canonical": signed["payload"]["canonical"],
                                   "signature": signed["signature"],
                                   "pubkey_pem": signed["payload"]["pubkey_pem"]})
    assert ok, why
    forged = signed["payload"]["canonical"].replace("|10|", "|99|")
    assert verify_audit_record({"canonical": forged, "signature": signed["signature"],
                                "pubkey_pem": signed["payload"]["pubkey_pem"]})[0] is False
    (row,) = approved_vs_executed(led.rows())
    assert row["status"] == "match", row["problems"]


def test_an_old_approval_db_gains_signature_columns_without_losing_rows(tmp_path: Path) -> None:
    import sqlite3

    from trading_live_claude.execution.approval_sqlite import (
        SqliteApprovalStore,
        SqliteCardRegistry,
    )
    db = tmp_path / "approval.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE intents (intent_id TEXT PRIMARY KEY, issued_at TEXT NOT NULL, "
                "expires_at TEXT NOT NULL, resolved_at TEXT, verdict TEXT, consumed INTEGER NOT NULL "
                "DEFAULT 0, broker TEXT NOT NULL, symbol TEXT NOT NULL, action TEXT NOT NULL, shares "
                "INTEGER NOT NULL, entry REAL NOT NULL, stop REAL NOT NULL, target REAL, notional_usd "
                "REAL NOT NULL, risk_dollars REAL NOT NULL, strategy TEXT NOT NULL, account TEXT NOT "
                "NULL, mode TEXT NOT NULL, thesis TEXT NOT NULL DEFAULT '', intel_ref TEXT NOT NULL "
                "DEFAULT '', nonce TEXT NOT NULL, canonical TEXT NOT NULL, signer_card_id TEXT)")
    con.execute("INSERT INTO intents VALUES ('old','t','t',NULL,'EXPIRED',0,'kraken','SOL/USD',"
                "'Buy',1,1,1,NULL,1,1,'s','a','paper','','','n','c',NULL)")
    con.commit()
    con.close()
    reg = SqliteCardRegistry(db)
    store = SqliteApprovalStore(reg, db)
    rec = store.audit_record("old")
    assert rec is not None and rec["signature"] is None and rec["verdict"] == "EXPIRED"
    reg.close()


# --------------------------------------------------------------------------- semantics (CRA)

def _fx(tmp_path: Path, rate: float = 1.40) -> BankOfCanadaFX:
    d = tmp_path / "fx"
    d.mkdir(exist_ok=True)
    days = {(date(2026, 1, 1) + timedelta(days=i)).isoformat(): rate for i in range(400)
            if (date(2026, 1, 1) + timedelta(days=i)).weekday() < 5}
    (d / "FXUSDCAD.json").write_text(json.dumps(days), encoding="utf-8")
    return BankOfCanadaFX(d)


def _exec(i: int, side: str, qty: float, px: float, when: datetime, *, sym: str = "SOL/USD",
          fee: float = 0.0, ccy: str | None = "USD") -> dict:
    return {"event": "EXECUTION_REPORTED", "stream": "kraken", "seq": i, "intent_id": f"i{i}",
            "payload": {"exec_id": f"e{i}", "symbol": sym, "side": side, "quantity": qty,
                        "price": px, "fee": fee, "fee_currency": ccy, "currency": ccy,
                        "currency_source": "test", "time": when.isoformat(), "venue": "kraken"}}


T0 = datetime(2026, 3, 2, 15, 0, tzinfo=UTC)          # a Monday


def test_acb_is_averaged_in_cad_and_fees_are_outlays(tmp_path: Path) -> None:
    fx = _fx(tmp_path, 1.40)
    rows = [_exec(1, "Buy", 10, 100.0, T0, fee=1.0), _exec(2, "Buy", 10, 120.0, T0 + timedelta(days=1)),
            _exec(3, "Sell", 5, 130.0, T0 + timedelta(days=60), fee=1.0)]
    s = classify(rows, fx, as_of=T0 + timedelta(days=200))
    acb_total = (10 * 100 + 1 + 10 * 120) * 1.40
    assert s[1]["lot_effect"]["after"]["acb_cad"] == pytest.approx(acb_total)
    expected_gain = (5 * 130 - 1) * 1.40 - acb_total / 20 * 5
    assert s[2]["gain_cad"] == pytest.approx(expected_gain)
    assert s[2]["property"] == "crypto:SOL" and s[2]["fx"]["source"].startswith("Bank of Canada")


def test_a_loss_with_a_buy_back_inside_30_days_is_superficial(tmp_path: Path) -> None:
    fx = _fx(tmp_path)
    rows = [_exec(1, "Buy", 10, 100.0, T0), _exec(2, "Sell", 10, 80.0, T0 + timedelta(days=40)),
            _exec(3, "Buy", 10, 82.0, T0 + timedelta(days=50))]
    s = classify(rows, fx, as_of=T0 + timedelta(days=200))
    sup = s[1]["superficial"]
    assert sup["status"] == "final" and sup["denied_fraction"] == 1.0
    assert sup["denied_loss_cad"] == pytest.approx(-s[1]["gain_cad"])
    assert "e3" in sup["carried_to"]
    # The denied loss lands in the substitute's ACB.
    assert s[2]["lot_effect"]["after"]["acb_cad"] == pytest.approx(10 * 82 * 1.40 + sup["denied_loss_cad"])


def test_a_loss_with_no_buy_back_is_allowed_and_an_open_window_is_provisional(tmp_path: Path) -> None:
    fx = _fx(tmp_path)
    rows = [_exec(1, "Buy", 10, 100.0, T0), _exec(2, "Sell", 10, 80.0, T0 + timedelta(days=40))]
    final = classify(rows, fx, as_of=T0 + timedelta(days=200))[1]["superficial"]
    assert final["denied_loss_cad"] == 0 and final["status"] == "final"
    open_ = classify(rows, fx, as_of=T0 + timedelta(days=45))[1]["superficial"]
    assert open_["window_open"] and open_["status"].startswith("provisional")


def test_a_buy_back_in_an_affiliated_account_counts_but_stays_out_of_this_acb(tmp_path: Path) -> None:
    fx = _fx(tmp_path)
    rows = [_exec(1, "Buy", 10, 100.0, T0), _exec(2, "Sell", 10, 80.0, T0 + timedelta(days=40))]
    aff = [{"account": "TFSA", "symbol": "SOL/USD", "side": "Buy", "quantity": 10.0,
            "time": (T0 + timedelta(days=45)).isoformat(), "venue": "kraken"}]
    sup = classify(rows, fx, as_of=T0 + timedelta(days=200), affiliated=aff)[1]["superficial"]
    assert sup["denied_fraction"] == 1.0 and sup["carried_to"].startswith("affiliated account")


def test_facts_that_cannot_be_determined_are_unknown_not_filled_in(tmp_path: Path) -> None:
    fx = _fx(tmp_path)
    rows = [_exec(1, "Buy", 10, 100.0, T0, ccy="NZD"),
            _exec(2, "Sell", 5, 100.0, T0 + timedelta(days=1), sym="ETH/USD")]
    s = classify(rows, fx)
    assert s[0]["lot_effect"] == "unknown" and "NZD" in s[0]["unknown_reasons"][0]
    assert s[1]["gain_cad"] == "unknown" and "short sale" in s[1]["unknown_reasons"][0]


def test_identical_property_ignores_the_venue() -> None:
    assert property_key("SOL/USD", "kraken") == property_key("SOL/CAD", "paper:kraken") == "crypto:SOL"
    assert property_key("XIC.TO", "questrade") == "ca-listed:XIC"
    assert property_key("VOO", "ib") == "us-listed:VOO"


def test_the_vocabulary_file_names_every_value_the_engine_emits(tmp_path: Path) -> None:
    import yaml
    vocab = yaml.safe_load((ROOT / "config" / "audit_vocabulary.yaml").read_text(encoding="utf-8"))
    rows = [_exec(1, "Buy", 10, 100.0, T0), _exec(2, "Sell", 10, 80.0, T0 + timedelta(days=40))]
    for s in classify(rows, _fx(tmp_path), as_of=T0 + timedelta(days=200)):
        assert s["vocab"] == vocab["vocab"]
        assert s["tax_event"] in vocab["facts"]["tax_event"]["values"]
        assert s["property"].split(":")[0] in vocab["facts"]["property"]["classes"]
        assert set(s["rules_engaged"]) <= set(vocab["rules"])


def test_a_fast_cancel_is_a_conduct_flag() -> None:
    t = datetime(2026, 3, 2, tzinfo=UTC)
    rows = [{"event": "BROKER_SUBMITTED", "intent_id": "a", "ts": t.isoformat(), "stream": "x", "seq": 0,
             "payload": {"symbol": "SOL/USD", "action": "Buy", "venue": "kraken"}},
            {"event": "CANCELLED", "intent_id": "a", "ts": (t + timedelta(seconds=1)).isoformat(),
             "stream": "x", "seq": 1, "payload": {"order_id": 1, "reason": "manual"}}]
    (flag,) = conduct_flags(rows)
    assert flag["flag"] == "cancel_before_fill" and flag["fast"] and flag["open_seconds"] == 1.0


def test_the_tax_export_records_its_own_hash_in_the_ledger(tmp_path: Path) -> None:
    led_dir = tmp_path / "ledger"
    w = Ledger(led_dir, stream="kraken")
    _fx(tmp_path)
    for r in (_exec(1, "Buy", 10, 100.0, T0), _exec(2, "Sell", 10, 80.0, T0 + timedelta(days=40))):
        w.append("EXECUTION_REPORTED", r["payload"], intent_id=r["intent_id"])
    res = export_year(led_dir, tmp_path, 2026, tmp_path / "reports")
    assert res["rows"] == 2 and res["unknown"] == 0
    ref = [r for r in Ledger(led_dir, stream="control").rows() if r["event"] == "EXTERNAL_REFERENCE"]
    from hashlib import sha256
    assert ref[0]["payload"]["csv_sha256"] == sha256(Path(res["csv"]).read_bytes()).hexdigest()


# --------------------------------------------------------------------------- G6 anchors, G8 retention

def _git_repo(path: Path) -> Path:
    path.mkdir()
    for args in (["init", "-q"], ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q",
                                  "--allow-empty", "-m", "init"]):
        subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)
    return path


def test_merkle_root_is_order_sensitive_and_deterministic() -> None:
    a, b, c = ("aa" * 32, "bb" * 32, "cc" * 32)
    assert merkle_root([a, b, c]) == merkle_root([a, b, c]) != merkle_root([b, a, c])


def test_an_anchored_day_detects_a_full_rewrite(tmp_path: Path) -> None:
    led = _led(tmp_path)
    day = datetime(2026, 10, 30, 12, tzinfo=UTC)
    for i in range(4):
        led.append("RISK_CHECK", {"i": i}, at=day)
    repo = _git_repo(tmp_path / "repo")
    anchor = day_anchor(tmp_path / "ledger", day.date())
    env = {**os.environ, "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    os.environ.update(env)
    write_tag(repo, anchor, sign=False)
    assert tag_anchor(repo, day.date())["root"] == anchor["root"]
    assert verify_day(tmp_path / "ledger", repo, day.date(), check_signature=False) == (True, "ok")

    # An attacker rewrites the day and recomputes every hash: the chain verifies, the anchor does not.
    path = next((tmp_path / "ledger").glob("2026-10-30.*.jsonl"))
    path.unlink()
    forged = _led(tmp_path)
    for i in range(4):
        forged.append("RISK_CHECK", {"i": i * 10}, at=day)
    assert forged.verify() == (True, "ok")
    ok, why = verify_day(tmp_path / "ledger", repo, day.date(), check_signature=False)
    assert not ok and "root differs" in why


def test_a_broken_chain_is_never_anchored(tmp_path: Path) -> None:
    led = _led(tmp_path)
    day = datetime(2026, 10, 30, 12, tzinfo=UTC)
    for i in range(3):
        led.append("RISK_CHECK", {"i": i}, at=day)
    path = next((tmp_path / "ledger").glob("*.jsonl"))
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join([lines[0], lines[2]]) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="does not verify"):
        day_anchor(tmp_path / "ledger", day.date())


def test_the_retention_copy_skips_the_open_day_and_verifies(tmp_path: Path) -> None:
    state = tmp_path / "state"
    led = Ledger(state / "ledger", stream="t")
    now = datetime(2026, 10, 31, 12, tzinfo=UTC)
    led.append("RISK_CHECK", {"i": 0}, at=now - timedelta(days=1))
    led.append("RISK_CHECK", {"i": 1}, at=now)
    (state / "orders.jsonl").write_text("{}\n", encoding="utf-8")
    res = archive(state, tmp_path / "vault", now=now)
    snap = Path(res["snapshot"])
    manifest = json.loads((snap / "manifest.json").read_text(encoding="utf-8"))
    assert "ledger/2026-10-30.t.jsonl" in manifest["files"]
    assert not any(k.startswith("ledger/2026-10-31") for k in manifest["files"])
    assert res["copy_mismatches"] == []
    with pytest.raises(ValueError):
        archive(state, state / "inside")


# --------------------------------------------------------------------------- spectral VaR gate

def _closes(n: int = 700, vol: float = 0.03, seed: int = 7) -> list[float]:
    rng = np.random.default_rng(seed)
    return list(100.0 * np.exp(np.cumsum(rng.standard_t(4, n) * vol / np.sqrt(2))))


def test_the_spectral_gate_trims_an_entry_to_its_var_cap_and_replays(tmp_path: Path) -> None:
    closes = _closes()
    gate = SpectralVarGate(lambda _s: closes, cap_pct=0.002)
    led = _led(tmp_path)
    router = Router.build_default(mode="paper", broker=_StubBroker(), state_dir=tmp_path, ledger=led)
    router.spectral_gate = gate
    intent = _intent(shares=100, entry=100.0)
    router.submit(intent, equity=100_000.0, existing_risk=0.0, open_positions=0)
    trim = next(r for r in led.rows() if r["event"] == "RISK_TRIMMED")
    assert trim["payload"]["cap"] == "spectral_var" and intent.shares < 100
    obs = next(r for r in led.rows() if r["event"] == "RISK_CHECK")["payload"]["observed"]["spectral_var"]
    assert obs["var_usd"] > obs["cap_usd"] and int(obs["max_shares"]) == intent.shares
    assert intent.shares * 100.0 * obs["loss_fraction"] <= 0.002 * 100_000.0
    assert [r["status"] for r in replay(led.rows())] == ["match"]


def test_the_spectral_gate_fails_closed_on_short_history_and_never_gates_an_exit(tmp_path: Path) -> None:
    gate = SpectralVarGate(lambda _s: _closes(100), cap_pct=0.01)
    router = Router.build_default(mode="paper", broker=_StubBroker(), state_dir=tmp_path)
    router.spectral_gate = gate
    d = router._gate(_intent(), equity=100_000.0, existing_risk=0.0, open_positions=0)
    assert not d.accepted and any("daily returns" in r for r in d.rejected_reasons)
    sell = OrderIntent(symbol="AAPL", action=OrderAction.SELL, shares=100, entry=100.0, stop=110.0,
                       target=None, strategy="t", risk_dollars=0.0, account_number="X")
    assert router._gate(sell, equity=100_000.0, existing_risk=0.0, open_positions=1).accepted


def test_turning_the_spectral_gate_on_moves_the_risk_check_version(tmp_path: Path) -> None:
    router = Router.build_default(mode="paper", broker=_StubBroker(), state_dir=tmp_path)
    before = router.risk_check_version()
    router.spectral_gate = SpectralVarGate(lambda _s: _closes(), cap_pct=0.01)
    assert router.risk_check_version() != before


# --------------------------------------------------------------------------- G9

def test_a_router_given_a_tmp_state_never_writes_the_real_ledger(tmp_path: Path) -> None:
    """The kill-switch and recon open ledgers from the router's own state_dir, never ROOT/state."""
    real = ROOT / "state" / "ledger"
    snapshot = {p.name: p.stat().st_mtime_ns for p in real.glob("*")} if real.exists() else {}
    broker = _paper(tmp_path)
    router = Router.build_default(mode="paper", broker=broker, state_dir=tmp_path,
                                  ledger=_led(tmp_path, session_id=broker.session_id))
    router.submit(_intent(shares=5), equity=100_000.0, existing_risk=0.0, open_positions=0)
    router.kill_switch.trip("test")
    after = {p.name: p.stat().st_mtime_ns for p in real.glob("*")} if real.exists() else {}
    assert after == snapshot


# --------------------------------------------------------------------------- the desk's INTEGRITY tab

def test_the_desk_reports_each_chain_as_measured(tmp_path: Path) -> None:
    import importlib.util
    spec = importlib.util.spec_from_file_location("build_desk_dash", ROOT / "scripts" / "build_desk_dash.py")
    dash = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(dash)  # type: ignore[union-attr]
    good, bad = Ledger(tmp_path / "ledger", stream="good"), Ledger(tmp_path / "ledger", stream="bad")
    for i in range(3):
        good.append("RISK_CHECK", {"i": i})
        bad.append("RISK_CHECK", {"i": i})
    path = next((tmp_path / "ledger").glob("*.bad.jsonl"))
    path.write_text(path.read_text(encoding="utf-8").replace('"i": 1', '"i": 9'), encoding="utf-8")
    chains = {c["stream"]: c for c in dash.integrity_model(tmp_path, [])["ledger"]["chains"]}
    assert chains["good"]["ok"] and chains["good"]["rows"] == 3
    assert not chains["bad"]["ok"] and "payload" in chains["bad"]["reason"]


def test_the_sqlite_store_keeps_the_signature_and_the_ledger_gets_it(tmp_path: Path) -> None:
    """Paper sessions use SqliteApprovalStore; its approval must carry the same evidence."""
    from trading_live_claude.execution.approval_sqlite import (
        SqliteApprovalStore,
        SqliteCardRegistry,
    )

    key = Ed25519PrivateKey.generate()
    reg = SqliteCardRegistry(tmp_path / "approval.db")
    reg.register("card-sim-001", key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))

    class _Signing(SqliteApprovalStore):
        def wait(self, intent_id, *, timeout=None):
            prompt = next(p for p in self.pending() if p.intent_id == intent_id)
            self.respond(intent_id, decision="ACCEPT", card_id="card-sim-001",
                         signature=key.sign(prompt.canonical.encode("utf-8")))
            return super().wait(intent_id, timeout=timeout)

    store = _Signing(reg, tmp_path / "approval.db")
    broker = _paper(tmp_path)
    led = _led(tmp_path, stream="kraken", session_id=broker.session_id)
    inner = Router.build_default(mode="paper", broker=broker, state_dir=tmp_path, ledger=led)
    intent = _intent(shares=10)
    ApprovalRouter(inner, store=store, ttl_seconds=30).submit(
        intent, equity=100_000.0, existing_risk=0.0, open_positions=0)
    rec = store.audit_record(intent.intent_id)
    assert rec is not None and rec["signature"] and verify_audit_record(rec) == (True, "ok")
    signed = next(r for r in led.rows() if r["event"] == "SIGNED")
    assert signed["signature"] == rec["signature"]
    reg.close()
