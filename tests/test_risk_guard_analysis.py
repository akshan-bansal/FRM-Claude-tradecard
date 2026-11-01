"""The risk-guard report: it counts what the journals hold, and refuses to calibrate on too little."""
from __future__ import annotations

import csv
import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest


def _load() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "scripts" / "risk_guard_analysis.py"
    spec = importlib.util.spec_from_file_location("risk_guard_analysis", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    import sys
    sys.modules[spec.name] = mod            # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(mod)
    return mod


rga = _load()


def _write(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def _equity(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["ts", "session_id", "equity", "cash", "positions_value", "drawdown_pct"])
        w.writeheader()
        w.writerows(rows)


def _row(ts: str, session: str, equity: float, cash: float, pos: float, dd: float) -> dict:
    return {"ts": ts, "session_id": session, "equity": equity, "cash": cash, "positions_value": pos,
            "drawdown_pct": dd}


def test_empty_state_says_there_is_no_record(tmp_path: Path) -> None:
    rep = rga.analyse(tmp_path)
    text = rga.render(rep, today="2026-11-01")
    assert not rep.enough_record
    assert "not enough to calibrate" in text and "support no threshold change" in text


def test_counts_each_guard_without_double_counting(tmp_path: Path) -> None:
    _write(tmp_path / "orders.jsonl", [
        {"ts": "2026-10-01T00:00:00+00:00", "intent_id": "a", "action": "Buy", "accepted": True, "rejected_reasons": []},
        {"ts": "2026-10-01T00:00:00+00:00", "intent_id": "b", "action": "Buy", "accepted": False,
         "rejected_reasons": ["kill-switch tripped", "portfolio heat 6.40% > cap 5.00%"]},
        {"ts": "2026-10-02T00:00:00+00:00", "intent_id": "c", "action": "Buy", "accepted": False,
         "rejected_reasons": ["single-name notional 80.0% > cap 50.0%", "portfolio gross leverage 1.20x > cap 1.00x"]},
    ])
    # The same rejection appears in both journals; it is one rejection.
    _write(tmp_path / "rejected.jsonl", [
        {"ts": "2026-10-01T00:00:00+00:00", "intent_id": "b", "reasons": ["kill-switch tripped"]},
    ])
    rep = rga.analyse(tmp_path)
    assert rep.rejections["kill_switch"] == 1 and rep.rejections["heat"] == 1
    assert rep.rejections["symbol_notional"] == 1 and rep.rejections["gross_leverage"] == 1
    assert rep.both_caps == 1 and rep.accepted_entries == 1


def test_equity_journal_gives_drawdown_leverage_and_negative_cash(tmp_path: Path) -> None:
    _equity(tmp_path / "paper_equity.csv", [
        _row("2026-10-01T00:00:00+00:00", "s1", 100_000, 50_000, 50_000, 0.01),
        _row("2026-10-01T01:00:00+00:00", "s1", 95_000, -10_000, 105_000, 0.05),
    ])
    rep = rga.analyse(tmp_path)
    assert rep.drawdown_by_session == {"s1": 0.05}
    assert rep.leverage_by_session["s1"] == pytest.approx(105_000 / 95_000)
    assert rep.negative_cash_rows == 1
    assert "sessions reaching 3%: 1; sessions reaching 8%: 0" in rga.render(rep, today="x")


def test_ledger_trims_count_towards_the_binding_rate(tmp_path: Path) -> None:
    from trading_live_claude.audit.ledger import Ledger
    led = Ledger(tmp_path / "ledger", stream="t", mode="paper", session_id="s")
    led.append("RISK_TRIMMED", {"cap": "symbol_notional"})
    led.append("RISK_TRIMMED", {"cap": "gross_leverage"})
    led.append("FORCED_EXIT", {"symbol": "A"})
    led.append("KILL_SWITCH_TRIPPED", {"reason": "x"})
    rep = rga.analyse(tmp_path)
    assert rep.trims["symbol_notional"] == 1 and rep.trims["gross_leverage"] == 1
    assert rep.forced_exits == 1 and rep.kill_trips == 1


def test_force_exit_sweep_scores_only_a_readable_path(tmp_path: Path) -> None:
    from trading_live_claude.audit.ledger import Ledger
    _write(tmp_path / "paper_fills.jsonl", [
        {"session_id": "s", "symbol": "A", "side": "Buy", "quantity": 10.0, "price": 100.0,
         "fill_time": "2026-10-01T00:00:00Z", "venue": "kraken"},
    ])
    _equity(tmp_path / "paper_equity.csv", [
        _row("2026-10-01T01:00:00+00:00", "s", 99_000, 98_000, 1000.0, 0.0),
        _row("2026-10-01T02:00:00+00:00", "s", 99_000, 98_000, 940.0, 0.0),   # 94/share: 3 ATR down
    ])
    Ledger(tmp_path / "ledger", stream="t", mode="paper", session_id="s").append(
        "STRATEGY_SIGNAL", {"symbol": "A", "atr": 2.0})
    rep = rga.analyse(tmp_path)
    assert {r["mult"]: r["would_fire"] for r in rep.sweep} == {2.0: 1, 2.5: 1, 3.0: 0, 3.5: 0, 4.0: 0}
    assert rep.sweep_skipped == 0
    assert "| 2.0 | 1 | 1 |" in rga.render(rep, today="x")


def test_sweep_skips_what_it_cannot_read(tmp_path: Path) -> None:
    _write(tmp_path / "paper_fills.jsonl", [
        {"session_id": "s", "symbol": "A", "side": "Buy", "quantity": 1.0, "price": 1.0,
         "fill_time": "2026-10-01T00:00:00Z", "venue": "kraken"},
    ])
    rep = rga.analyse(tmp_path)
    assert rep.sweep_skipped == 1 and rep.sweep[0]["positions"] == 0
    assert "no multiple can be scored" in rga.render(rep, today="x").replace("\n", " ")


def test_enough_record_needs_days_and_venues(tmp_path: Path) -> None:
    rep = rga.Report(state_dir="x", days=[f"2026-10-{d:02d}" for d in range(1, 9)], venues=["kraken"])
    assert not rep.enough_record
    rep.venues = ["kraken", "questrade"]
    assert rep.enough_record
    rep.days = rep.days[:3]
    assert not rep.enough_record


def test_wiring_check_sees_the_repo_as_it_is() -> None:
    w = rga.wiring_checks(Path(__file__).resolve().parents[1] / "src" / "trading_live_claude")
    assert w["monitor can call Router.check_forced_exits (needs `forced_exits: true`)"]
    assert w["a tripped kill-switch still lets exits through"]
    assert w["live books auto-trip the kill-switch (EquityWatch)"]
    assert w["monitor passes open notional to the gross-leverage gate"]
    assert w["card path passes open notional through"]
    assert w["place-order passes open notional"]
    assert w["paper broker auto-trips the kill-switch (KillSwitch.evaluate)"]


def test_main_writes_the_report(tmp_path: Path) -> None:
    out = tmp_path / "r.md"
    assert rga.main(["--state-dir", str(tmp_path), "--out", str(out)]) == 0
    assert out.read_text(encoding="utf-8").startswith("# Risk-guard validation")
