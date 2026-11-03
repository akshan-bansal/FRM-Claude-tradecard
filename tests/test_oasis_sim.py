"""OASIS layer: ledger, seed, result contract, ingest, society features, and the runner's non-OASIS logic.

No LLM, no OASIS install, no network: results and the OASIS database are synthetic.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sqlite3
import sys
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from trading_live_claude.analysis.feature_store import Instrument, Sources, build_row
from trading_live_claude.analysis.pit import TimeIndex
from trading_live_claude.intel.graph import load_edges
from trading_live_claude.sim import (
    BudgetError,
    ResultError,
    RunBudget,
    build_seed,
    ingest,
    load_seed,
    parse_result,
    write_seed,
)
from trading_live_claude.sim.budget import cancel, committed, remaining, settle

NOW = datetime(2026, 10, 30, 12, tzinfo=UTC)
BUDGET = RunBudget(max_usd=5, max_agents=20, max_steps=5, usd_per_mtok_in=0.5, usd_per_mtok_out=2.0)
ALIASES = {"NVDA": ("Nvidia",), "BTC/USD": ("Bitcoin",), "ENB.TO": ("Enbridge",)}

_spec = importlib.util.spec_from_file_location(
    "oasis_vm_runner", Path(__file__).resolve().parent.parent / "scripts" / "oasis_vm_runner.py")
runner = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
sys.modules["oasis_vm_runner"] = runner  # dataclasses resolve annotations through sys.modules
_spec.loader.exec_module(runner)  # type: ignore[union-attr]


def _news(state: Path, rows: list[dict]) -> None:
    state.mkdir(parents=True, exist_ok=True)
    (state / "news.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def _row(title: str, syms: list[str], hours_ago: float, h: str, source: str = "wire") -> dict:
    return {"arrived_utc": (NOW - timedelta(hours=hours_ago)).isoformat(), "source": source, "title": title,
            "symbols": syms, "content_hash": h}


def _seed(tmp_path: Path, **kw):  # type: ignore[no-untyped-def]
    _news(tmp_path, [_row("Nvidia ships a chip", ["NVDA"], 2, "a"), _row("Bitcoin slides", ["BTC/USD"], 3, "b"),
                     _row("Enbridge pipeline halted", ["ENB.TO"], 1, "c")])
    return build_seed(tmp_path, symbols=list(ALIASES), aliases=ALIASES, budget=kw.pop("budget", BUDGET),
                      model_platform="openai", model="test-model", now=NOW, **kw)


# --------------------------------------------------------------------------- ledger


def test_budget_values_are_validated() -> None:
    with pytest.raises(BudgetError):
        RunBudget(max_usd=0, max_agents=5, max_steps=1, usd_per_mtok_in=1, usd_per_mtok_out=1)
    with pytest.raises(BudgetError):
        RunBudget(max_usd=1, max_agents=5000, max_steps=1, usd_per_mtok_in=1, usd_per_mtok_out=1)
    assert BUDGET.cost(1_000_000, 500_000) == pytest.approx(1.5)


def test_the_ledger_reserves_settles_cancels_and_refuses_overspend(tmp_path: Path) -> None:
    s1 = _seed(tmp_path, budget=replace(BUDGET, max_usd=60))
    write_seed(s1, tmp_path)
    assert remaining(tmp_path) == pytest.approx(40)
    with pytest.raises(BudgetError, match="exceeds"):
        write_seed(_seed(tmp_path, budget=replace(BUDGET, max_usd=41)), tmp_path)
    settle(tmp_path, s1.run_id, 12.5)  # spent less than reserved: the rest comes back
    assert remaining(tmp_path) == pytest.approx(87.5)
    s2 = _seed(tmp_path, budget=replace(BUDGET, max_usd=30), max_items=2)
    write_seed(s2, tmp_path)
    cancel(tmp_path, s2.run_id)
    assert remaining(tmp_path) == pytest.approx(87.5) and committed(tmp_path)["open_runs"] == []
    with pytest.raises(BudgetError, match="no open reservation"):
        cancel(tmp_path, s2.run_id)


# --------------------------------------------------------------------------- seed


def test_briefing_is_title_named_known_before_issue_and_deduplicated(tmp_path: Path) -> None:
    _news(tmp_path, [_row("Nvidia ships a chip", ["NVDA"], 2, "a"),
                     _row("NVIDIA ships a chip!", ["NVDA"], 1, "a2", source="mirror"),  # syndicated copy
                     _row("Markets wobble", [], 1, "b"),  # names no basket symbol
                     _row("Bitcoin later", ["BTC/USD"], -1, "c"),  # arrives after issue
                     _row("Bitcoin old", ["BTC/USD"], 30, "d")])  # outside the 24 h look-back
    s = build_seed(tmp_path, symbols=list(ALIASES), aliases=ALIASES, budget=BUDGET, model_platform="openai",
                   model="m", now=NOW)
    assert [b.title for b in s.briefing] == ["NVIDIA ships a chip!"]
    assert s.briefing[0].item_id == "news:mirror:a2"  # the event node the news collector wrote


def test_briefing_round_robins_across_symbols(tmp_path: Path) -> None:
    rows = [_row(f"Bitcoin story {i}", ["BTC/USD"], i / 10, f"b{i}") for i in range(10)]
    rows += [_row("Nvidia story", ["NVDA"], 5, "n")]
    _news(tmp_path, rows)
    s = build_seed(tmp_path, symbols=list(ALIASES), aliases=ALIASES, budget=BUDGET, model_platform="openai",
                   model="m", now=NOW, max_items=3)
    assert {b.symbols[0] for b in s.briefing} == {"BTC/USD", "NVDA"}


def test_nothing_to_brief_and_unknown_backends_are_refused(tmp_path: Path) -> None:
    _news(tmp_path, [])
    with pytest.raises(ValueError, match="nothing to brief"):
        build_seed(tmp_path, symbols=list(ALIASES), aliases=ALIASES, budget=BUDGET, model_platform="openai",
                   model="m", now=NOW)
    with pytest.raises(ValueError, match="model_platform"):
        build_seed(tmp_path, symbols=list(ALIASES), aliases=ALIASES, budget=BUDGET, model_platform="local",
                   model="m", now=NOW)


def test_an_edited_seed_is_refused_in_the_repo_and_on_the_vm(tmp_path: Path) -> None:
    path = write_seed(_seed(tmp_path), tmp_path)
    assert load_seed(path).run_id and runner.load_seed(path)["seed_hash"]
    d = json.loads(path.read_text(encoding="utf-8"))
    d["budget"]["max_usd"] = 500
    path.write_text(json.dumps(d), encoding="utf-8")
    with pytest.raises(ValueError, match="hash"):
        load_seed(path)
    with pytest.raises(runner.SeedError, match="seed_hash"):
        runner.load_seed(path)


# --------------------------------------------------------------------------- result contract and ingest


def _result(seed, **kw) -> dict:  # type: ignore[no-untyped-def]
    base = {"run_id": seed.run_id, "seed_hash": seed.seed_hash, "finished_utc": NOW.isoformat(),
            "spent_usd": 3.2, "tokens_in": 1000, "tokens_out": 200, "n_agents": 4, "n_steps": 3,
            "stopped": "steps", "stances": {"1": {"NVDA": 0.5, "BTC/USD": -0.6}, "2": {"NVDA": 0.1},
                                            "3": {"BTC/USD": -0.2}},
            "follows": [["1", "2"]], "engaged": [["2", "1", 3]], "attends": [["1", "NVDA", 2]]}
    return base | kw


def test_a_result_must_belong_to_its_seed_and_stay_in_range(tmp_path: Path) -> None:
    seed = _seed(tmp_path)
    assert parse_result(_result(seed), seed).stances["1"]["NVDA"] == 0.5
    for bad, match in ((_result(seed, seed_hash="x"), "different briefing"),
                       (_result(seed, stances={"1": {"NVDA": 1.5}}), r"\[-1, 1\]"),
                       (_result(seed, stances={"1": {"AAPL": 0.1}}), "did not brief"),
                       (_result(seed, n_agents=999), "exceed"),
                       (_result(seed, stopped="bored"), "stopped")):
        with pytest.raises(ResultError, match=match):
            parse_result(bad, seed)
    assert parse_result(_result(seed, spent_usd=9.0), seed).over_budget is True


def test_ingest_writes_the_run_edges_and_settles_the_ledger(tmp_path: Path) -> None:
    seed = _seed(tmp_path)
    write_seed(seed, tmp_path)
    graph = tmp_path / "graph.jsonl"
    out = ingest(parse_result(_result(seed), seed), seed, tmp_path, now=NOW, graph_journal=graph)
    run = json.loads((tmp_path / "oasis_runs.jsonl").read_text(encoding="utf-8"))
    assert run["by_symbol"]["NVDA"] == {"stance": 0.3, "dispersion": pytest.approx(0.282843, abs=1e-6), "n": 2}
    assert run["by_symbol"]["BTC/USD"]["stance"] == pytest.approx(-0.4)
    preds = sorted({e.predicate for e in load_edges(graph)})
    assert preds == ["attends", "engaged", "follows", "holds_stance", "member_of", "seeded"]
    assert all(e.as_of == NOW.isoformat() for e in load_edges(graph))  # stamped at ingest, not the VM clock
    assert remaining(tmp_path) == pytest.approx(100 - 3.2) and out["over_budget"] is False
    with pytest.raises(ValueError, match="already ingested"):
        ingest(parse_result(_result(seed), seed), seed, tmp_path, now=NOW, graph_journal=graph)


def test_society_features_are_known_from_ingest_time_and_expire(tmp_path: Path) -> None:
    run = {"run_id": "r", "by_symbol": {"NVDA": {"stance": 0.3, "dispersion": 0.2, "n": 2}}}
    soc = TimeIndex([(datetime(2026, 6, 10, 19, tzinfo=UTC), run)])
    src = Sources(live={}, gkg={}, live_passes=TimeIndex([]), gkg_files=TimeIndex([]), overlay=TimeIndex([]),
                  graph_events=TimeIndex([]), society=soc)
    inst = Instrument("NVDA", "qt", "equity")
    row = build_row(inst, date(2026, 6, 10), src, now=datetime(2026, 6, 10, 21, tzinfo=UTC), live_days={}, gkg_days={})
    assert row is not None and row["features"]["soc_stance"] == 0.3 and row["coverage"]["society_age_h"] == 1.0
    before = build_row(inst, date(2026, 6, 9), src, now=datetime(2026, 6, 10, 21, tzinfo=UTC), live_days={}, gkg_days={})
    assert before is not None and before["features"]["soc_stance"] is None  # ingested after that cutoff
    late = build_row(inst, date(2026, 6, 20), src, now=datetime(2026, 6, 21, tzinfo=UTC), live_days={}, gkg_days={})
    assert late is not None and late["features"]["soc_stance"] is None  # older than 7 days


# --------------------------------------------------------------------------- runner logic (no OASIS)


def test_the_plan_keeps_the_interview_reserve_inside_the_cap() -> None:
    b = {"max_usd": 1.0, "max_agents": 50, "max_steps": 10, "usd_per_mtok_in": 0.5, "usd_per_mtok_out": 2.0}
    p = runner.plan_run(b)
    assert p.est_usd <= b["max_usd"] * runner.PLAN_SAFETY and p.steps >= 1
    with pytest.raises(runner.SeedError, match="cannot cover"):
        runner.plan_run(b | {"max_usd": 0.0001})


def test_personas_are_deterministic_per_seed() -> None:
    assert runner.personas("abc", 5) == runner.personas("abc", 5) != runner.personas("abd", 5)
    assert [p.agent_id for p in runner.personas("abc", 3)] == [1, 2, 3]


def test_stance_answers_are_parsed_strictly() -> None:
    syms = ["NVDA", "BTC/USD"]
    text = 'Sure. {"nvda": 0.4, "BTC/USD": -2, "AAPL": 0.9, "x": null} thanks'
    assert runner.parse_stance(text, syms) == {"NVDA": 0.4}  # out of range dropped, unbriefed ignored
    assert runner.parse_stance("no json here", syms) == {}


class _Usage:
    def __init__(self, i: int, o: int) -> None:
        self.prompt_tokens, self.completion_tokens = i, o


class _Resp:
    def __init__(self, usage: object) -> None:
        self.usage = usage


def test_the_meter_counts_usage_charges_unmetered_calls_and_stops_at_the_cap() -> None:
    b = {"max_usd": 1.0, "usd_per_mtok_in": 1.0, "usd_per_mtok_out": 1.0}

    class Model:
        def run(self, *a: object) -> _Resp:
            return _Resp(_Usage(400_000, 100_000))

        async def arun(self, *a: object) -> _Resp:
            return _Resp(None)

    m, meter = Model(), runner.Meter(b, limit_usd=1.0)
    runner.attach_meter(m, meter)
    m.run()
    assert meter.spent_usd == pytest.approx(0.5)
    asyncio.run(m.arun())
    assert meter.unmetered_calls == 1 and meter.tokens_in == 400_000 + runner.EST_IN
    m.run()
    with pytest.raises(runner.BudgetExhausted):
        m.run()


def _oasis_db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE user (user_id INTEGER, agent_id INTEGER, user_name TEXT);
        CREATE TABLE post (post_id INTEGER, user_id INTEGER, original_post_id INTEGER, content TEXT);
        CREATE TABLE follow (follower_id INTEGER, followee_id INTEGER);
        CREATE TABLE "like" (user_id INTEGER, post_id INTEGER);
        CREATE TABLE comment (user_id INTEGER, post_id INTEGER, content TEXT);
        CREATE TABLE trace (user_id INTEGER, action TEXT, info TEXT);
        INSERT INTO user VALUES (10, 0, 'newswire'), (11, 1, 'a'), (12, 2, 'b');
        INSERT INTO post VALUES (1, 10, NULL, 'Nvidia ships a chip'), (2, 11, NULL, 'Nvidia looks stretched'),
                                (3, 12, 2, 'agree on NVDA');
        INSERT INTO follow VALUES (11, 12), (12, 10);
        INSERT INTO "like" VALUES (12, 2), (11, 1);
        INSERT INTO comment VALUES (11, 1, 'Bitcoin will feel this');
        INSERT INTO trace VALUES (11, 'interview', '{"response": "{\\"NVDA\\": -0.5}"}'),
                                 (12, 'interview', '{"response": "no view"}'), (11, 'like_post', '{}');
    """)
    con.commit()
    con.close()


def test_extraction_reads_the_oasis_database_into_the_result_contract(tmp_path: Path) -> None:
    db = tmp_path / "oasis.db"
    _oasis_db(db)
    ex = runner.extract(db, symbols=["NVDA", "BTC/USD"], aliases={"NVDA": ["Nvidia"], "BTC/USD": ["Bitcoin"]})
    assert ex.stances == {"1": {"NVDA": -0.5}}
    assert ex.follows == [("1", "2"), ("2", "0")]
    assert ex.engaged == [("1", "0", 2), ("2", "1", 2)]  # like + comment on the wire; repost + like of agent 1
    assert ex.attends == [("1", "BTC/USD", 1), ("1", "NVDA", 1), ("2", "NVDA", 1)]  # the wire's own post excluded
    assert ex.notes == []


def test_a_missing_table_is_noted_not_guessed(tmp_path: Path) -> None:
    db = tmp_path / "bare.db"
    sqlite3.connect(db).close()
    ex = runner.extract(db, symbols=["NVDA"], aliases={})
    assert ex.stances == {} and any("trace" in n for n in ex.notes)


def test_a_runner_result_passes_the_repo_contract(tmp_path: Path) -> None:
    seed = _seed(tmp_path)
    path = write_seed(seed, tmp_path)
    db = tmp_path / "oasis.db"
    _oasis_db(db)
    vm_seed = runner.load_seed(path)
    ex = runner.extract(db, symbols=vm_seed["symbols"], aliases=vm_seed["aliases"])
    raw = {"run_id": vm_seed["run_id"], "seed_hash": vm_seed["seed_hash"], "finished_utc": NOW.isoformat(),
           "spent_usd": 1.0, "tokens_in": 10, "tokens_out": 5, "n_agents": 3, "n_steps": 2, "stopped": "budget",
           "stances": ex.stances, "follows": ex.follows, "engaged": ex.engaged, "attends": ex.attends}
    res = parse_result(json.loads(json.dumps(raw)), seed)  # through JSON, as on the wire
    assert res.stances == {"1": {"NVDA": -0.5}} and res.stopped == "budget"


def test_the_sim_layer_is_outside_the_trading_path() -> None:
    import ast

    forbidden = ("execution", "risk", "brokers", "strategies", "monitor", "daemon", "tune", "cli")
    for f in (Path(__file__).resolve().parent.parent / "src" / "trading_live_claude" / "sim").glob("*.py"):
        mods = {n.module for n in ast.walk(ast.parse(f.read_text(encoding="utf-8")))
                if isinstance(n, ast.ImportFrom) and n.module}
        # The cost model is plain arithmetic: the society's cost check prices with it and places nothing.
        mods -= {"execution.venue_costs", "..execution.venue_costs", "trading_live_claude.execution.venue_costs"}
        assert not {m for m in mods for x in forbidden if f".{x}" in f".{m}"}, f.name
