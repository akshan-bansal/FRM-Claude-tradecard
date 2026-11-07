"""The card's gap-trim line, and the simulator's clickable screen and keys."""
from __future__ import annotations

import importlib.util
import json
import sys
import threading
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from trading_live_claude.execution.approval import THESIS_MAX, gap_clause, with_risk_clause

ROOT = Path(__file__).resolve().parents[1]


def _sim():
    spec = importlib.util.spec_from_file_location("approval_card_sim", ROOT / "scripts" / "approval_card_sim.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["approval_card_sim"] = module
    spec.loader.exec_module(module)
    return module


SIGNED = {"broker": "questrade", "action": "Buy", "symbol": "AAPL", "shares": "100", "entry": "100.00",
          "notional_usd": "10000.00", "account": "X", "intent_id": "abc", "nonce": "n"}


def _prompt(seconds: float = 30.0) -> dict:
    return {"intent_id": "abc", "expires_at": (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat(),
            "thesis": "gap-trim 200>100 for 10.0% gap; bollinger touch"}


def test_a_gap_trim_is_named_and_kept_ahead_of_the_narrative() -> None:
    d = SimpleNamespace(trims=[{"cap": "symbol_notional", "from_shares": 300, "to_shares": 200},
                               {"cap": "gap_risk", "from_shares": 200, "to_shares": 100}],
                        observed={"gap_risk": {"loss_fraction": 0.1}})
    clause = gap_clause(d)
    assert clause == "gap-trim 200>100 for 10.0% gap"
    long = with_risk_clause(clause, "x" * 300)
    assert long.startswith(clause) and len(long) == THESIS_MAX and long.endswith("~") and long.isascii()
    assert gap_clause(SimpleNamespace(trims=[], observed={})) == ""
    assert with_risk_clause("", "plain") == "plain"


def test_screen_rows_come_from_the_signed_bytes_and_cut_like_the_firmware() -> None:
    sim = _sim()
    assert sim.screen_rows(SIGNED) == ["Buy AAPL", "100sh", "@100.00", "$10000.00"]
    assert sim.fit("$123456789012.50") == "$123456789012~" and len(sim.fit("x" * 40)) == sim.LCD_COLS


def _post(url: str, body: dict, token: str | None) -> int:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **({"X-Card-Token": token} if token else {})})
    try:
        return urllib.request.urlopen(req, timeout=5).status
    except urllib.error.HTTPError as e:
        return e.code


@pytest.mark.parametrize(("key", "verdict"), [("R", "ACCEPT"), ("L", "DECLINE")])
def test_a_key_press_decides_the_prompt_on_screen(key: str, verdict: str) -> None:
    sim = _sim()
    ui = sim.CardUI(0)
    try:
        out: list = []
        worker = threading.Thread(target=lambda: out.append(ui.decide(_prompt(), SIGNED, "AB12...CD34")))
        worker.start()
        for _ in range(50):
            state = json.loads(urllib.request.urlopen(urllib.request.Request(
                ui.url + "state", headers={"X-Card-Token": ui.token}), timeout=5).read())
            if state["prompt"]:
                break
        assert state["prompt"]["signed"] == SIGNED and state["prompt"]["fingerprint"] == "AB12...CD34"
        assert _post(ui.url + "press", {"key": key, "intent_id": "other"}, ui.token) == 200   # a stale press
        assert _post(ui.url + "press", {"key": key, "intent_id": "abc"}, None) == 403         # no token
        assert _post(ui.url + "press", {"key": key, "intent_id": "abc"}, ui.token) == 200
        worker.join(timeout=10)
        assert out == [verdict]
    finally:
        ui.server.shutdown()


def test_an_unanswered_prompt_expires_and_the_page_needs_the_token() -> None:
    sim = _sim()
    ui = sim.CardUI(0)
    try:
        assert ui.decide(_prompt(seconds=0.6), SIGNED, "fp") is None
        page = urllib.request.urlopen(ui.url, timeout=5).read().decode()
        assert ui.token in page and "__CARD_TOKEN__" not in page
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(ui.url + "state", timeout=5)
    finally:
        ui.server.shutdown()
