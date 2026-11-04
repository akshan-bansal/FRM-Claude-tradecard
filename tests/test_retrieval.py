"""Phase 3's done criteria: a debate cites paths that exist, sees nothing from the future, and keeps disaster out."""
from __future__ import annotations

import inspect
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

pytest.importorskip("networkx")
pytest.importorskip("scipy")

from trading_live_claude import intel
from trading_live_claude.intel import graph_search as gs
from trading_live_claude.intel import retrieval as rt
from trading_live_claude.intel.agents import debate, run_debate
from trading_live_claude.intel.graph import Edge, event_records_to_edges
from trading_live_claude.intel.graph_store import GraphStore, GraphView

_URL = "https://api.anthropic.com/v1/messages"
T0 = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
AFTER = T0 + timedelta(hours=5)
XLE = ("symbol", "XLE")


def _at(hours: float) -> str:
    return (T0 + timedelta(hours=hours)).isoformat()


def _event(event_id: str, title: str, sources: list[str], category: str, hours: float, *symbols: str) -> list[Edge]:
    stamp = _at(hours)
    rec = {"id": event_id, "title": title, "sources": sources, "categories": [category]}
    return (event_records_to_edges([rec], domain=category, poll_id=stamp, as_of=stamp)
            + [Edge(("event", event_id), "mentions", ("symbol", s), as_of=stamp) for s in symbols])


def _sample() -> list[Edge]:
    return [*_event("e1", "Refinery strike halts pipeline flows", ["ap", "reuters"], "energy", 1, "XLE"),
            *_event("e2", "OPEC holds output quotas", ["reuters"], "energy", 2, "XLE", "USO"),
            *_event("e3", "Border clash escalates", ["bbc"], "conflict", 3, "LMT"),
            *_event("e4", "Hurricane batters the gulf refinery coast", ["ap"], "disaster", 4, "XLE", "RE"),
            Edge(("venue", "kraken"), "traded", XLE, weight=5000.0, as_of=_at(1)),
            Edge(("society", "run1"), "holds_stance", XLE, weight=0.9, as_of=_at(1))]


def _future() -> list[Edge]:
    return _event("leak", "Refinery leak strike XLE", ["ap"], "energy", 100, "XLE")


def _news_row(source: str, digest: str, title: str, hours: float) -> dict[str, str]:
    return {"source": source, "content_hash": digest, "title": title, "arrived_utc": _at(hours)}


@pytest.fixture()
def store(tmp_path: Path) -> Iterator[GraphStore]:
    with GraphStore(tmp_path / "g.sqlite") as s:
        s.ingest(_sample())
        yield s


@pytest.fixture()
def view(store: GraphStore) -> GraphView:
    return store.as_of_view(AFTER)


@pytest.fixture(autouse=True)
def _fake_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from trading_live_claude.config import get_settings
    get_settings.cache_clear()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")


NO_NEWS = "no-news.jsonl"


# ---------------------------------------------------------------- cached views


def test_a_cached_view_reads_once_and_agrees_with_the_plain_view(view: GraphView, monkeypatch: pytest.MonkeyPatch) -> None:
    cached = view.cached()
    seen = []
    original = GraphView._select

    def spy(self: GraphView, where: str = "", params: Any = ()) -> Any:
        seen.append(where)
        return original(self, where, params)
    monkeypatch.setattr(GraphView, "_select", spy)
    first = cached.edges(predicate="mentions")
    assert cached.edges(predicate="mentions") == first
    assert len(seen) == 1
    assert first == view.edges(predicate="mentions")
    assert cached.cached() is cached


def test_searches_share_one_built_graph_on_a_cached_view(view: GraphView) -> None:
    cached = view.cached()
    assert gs.build_graph(cached) is gs.build_graph(cached)
    assert gs.build_graph(view) is not gs.build_graph(view)
    assert gs.personalized_pagerank(cached, [XLE]) == gs.personalized_pagerank(view, [XLE])


def test_a_cached_view_keeps_the_cutoff(store: GraphStore) -> None:
    cached = store.as_of_view(T0 + timedelta(hours=2, minutes=30)).cached()
    assert {e.as_of for e in cached.edges()} <= {_at(1), _at(2)}


# ---------------------------------------------------------------- the news join


def _write_news(path: Path, rows: list[dict[str, str]]) -> Path:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


def test_news_titles_stop_strictly_before_the_cutoff(tmp_path: Path, view: GraphView) -> None:
    journal = _write_news(tmp_path / "news.jsonl", [
        _news_row("ap", "old", "Known headline", 1), _news_row("ap", "edge", "Exactly at the cutoff", 5),
        _news_row("ap", "new", "Future headline", 6),
        {"source": "ap", "content_hash": "undated", "title": "No time", "arrived_utc": ""}])
    assert rt.news_titles(view, journal) == {"news:ap:old": "Known headline"}


def test_a_missing_news_journal_is_empty(tmp_path: Path, view: GraphView) -> None:
    assert rt.news_titles(view, tmp_path / NO_NEWS) == {}


def test_news_titles_fill_mentions_events_for_search_and_evidence(tmp_path: Path, store: GraphStore) -> None:
    store.ingest([Edge(("event", "news:ap:abc"), "mentions", XLE, as_of=_at(4.5))])
    journal = _write_news(tmp_path / "news.jsonl", [_news_row("ap", "abc", "Tanker fire closes the strait", 4.5)])
    v = store.as_of_view(AFTER)
    bundle = rt.retrieve_evidence(v, "tanker strait", news_path=journal)
    assert [i.event_id for i in bundle.items if i.title.startswith("Tanker")] == ["news:ap:abc"]
    assert rt.retrieve_evidence(v, "tanker strait", news_path=tmp_path / NO_NEWS).items == ()


def test_a_later_news_row_cannot_reach_an_earlier_search(tmp_path: Path, store: GraphStore) -> None:
    """The edge is old enough to see, but its headline arrived later: the words must stay unknown."""
    store.ingest([Edge(("event", "news:ap:abc"), "mentions", XLE, as_of=_at(1))])
    journal = _write_news(tmp_path / "news.jsonl", [_news_row("ap", "abc", "Tanker fire", 50)])
    assert rt.retrieve_evidence(store.as_of_view(AFTER), "tanker", news_path=journal).items == ()
    assert rt.retrieve_evidence(store.as_of_view(T0 + timedelta(hours=51)), "tanker", news_path=journal).items


# ---------------------------------------------------------------- retrieval


def test_a_symbol_gets_its_events_with_paths_that_exist(tmp_path: Path, view: GraphView) -> None:
    bundle = rt.retrieve_evidence(view, XLE, news_path=tmp_path / NO_NEWS)
    ids = [i.event_id for i in bundle.items]
    assert {"e1", "e2"} <= set(ids)
    graph = gs.build_graph(view, gs.SIGNAL_SCOPE)
    for item in bundle.items:
        assert item.path is not None and item.path.nodes[0] == XLE and item.path.nodes[-1] == ("event", item.event_id)
        for hop in item.path.hops:
            assert graph.has_edge(hop.source, hop.target)
    assert all(i.title for i in bundle.items if i.event_id in ("e1", "e2"))


def test_records_show_how_an_event_connects(tmp_path: Path, view: GraphView) -> None:
    rec = next(r for r in rt.retrieve_evidence(view, XLE, news_path=tmp_path / NO_NEWS).records() if r["id"] == "e2")
    assert rec["title"] == "OPEC holds output quotas" and rec["symbols"] == ["USO", "XLE"]
    assert rec["path"] == ["e2 -mentions-> XLE"]


def test_free_text_finds_by_words_and_widens_through_the_graph(tmp_path: Path, view: GraphView) -> None:
    bundle = rt.retrieve_evidence(view, "refinery strike", news_path=tmp_path / NO_NEWS)
    assert bundle.items[0].event_id == "e1"
    assert "text" in bundle.items[0].found_by
    assert any(i.event_id == "e2" and i.found_by == ("graph",) for i in bundle.items)


def test_a_target_nothing_connects_to_gets_nothing_not_general_news(tmp_path: Path, view: GraphView) -> None:
    assert rt.retrieve_evidence(view, ("symbol", "NOPE"), news_path=tmp_path / NO_NEWS).items == ()
    assert rt.retrieve_evidence(view, "words nothing uses", news_path=tmp_path / NO_NEWS).items == ()


def test_the_budget_caps_the_bundle_and_diversity_changes_the_pick(tmp_path: Path) -> None:
    with GraphStore(tmp_path / "d.sqlite") as s:
        s.ingest(_event("same1", "Oil strike", ["ap"], "energy", 1, "XLE") + _event("same2", "Oil strike again", ["ap"], "energy", 1, "XLE")
                 + _event("other", "Oil grid fault", ["bbc"], "economy", 1, "XLE"))
        bundle = rt.retrieve_evidence(s.as_of_view(AFTER), XLE, budget=2, news_path=tmp_path / NO_NEWS)
    assert len(bundle.items) == 2
    assert "other" in {i.event_id for i in bundle.items}   # not just the same story twice


def test_the_bundle_hash_is_stable_and_tracks_content(tmp_path: Path, store: GraphStore) -> None:
    news = tmp_path / NO_NEWS
    v = store.as_of_view(AFTER)
    first = rt.retrieve_evidence(v, XLE, news_path=news)
    assert first.content_hash == rt.retrieve_evidence(v, XLE, news_path=news).content_hash
    other = rt.retrieve_evidence(store.as_of_view(T0 + timedelta(hours=1, minutes=30)), XLE, news_path=news)
    assert other.content_hash != first.content_hash


def test_domain_runs_reach_the_context_from_poll_edges(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    def poll(minutes: int, *domains: str) -> list[Edge]:
        stamp = (now - timedelta(minutes=minutes)).isoformat()
        return [Edge(("poll", stamp), "elevated_in", ("domain", d), weight=4.0, as_of=stamp) for d in domains]
    with GraphStore(tmp_path / "r.sqlite") as s:
        s.ingest(poll(90, "conflict") + poll(60, "conflict", "energy") + poll(30, "conflict", "energy"))
        bundle = rt.retrieve_evidence(s.as_of_view(now), "anything", news_path=tmp_path / NO_NEWS)
    assert bundle.domain_runs == {"conflict": 3, "energy": 2}
    assert bundle.co_runs == {"conflict+energy": 2}
    assert bundle.context()["consecutive_polls_elevated_by_domain"]["conflict"] == 3


# ---------------------------------------------------------------- disaster stays out of anything that can become a signal


def test_disaster_events_never_reach_a_bundle(tmp_path: Path, view: GraphView) -> None:
    news = tmp_path / NO_NEWS
    for target in (XLE, ("symbol", "RE"), "hurricane gulf", "refinery"):
        bundle = rt.retrieve_evidence(view, target, news_path=news)
        assert "e4" not in {i.event_id for i in bundle.items}, target
        assert "urricane" not in json.dumps(bundle.records())


def test_tools_cannot_reach_disaster_or_widen_their_scope(view: GraphView) -> None:
    tools = rt.ViewTools(view)
    out, err = tools.call("search_text", {"query": "hurricane", "scope": "EVIDENCE_SCOPE", "as_of": "2030-01-01"})
    assert (json.loads(out), err) == ([], False)
    out, _ = tools.call("neighbors", {"node_type": "symbol", "node_id": "XLE", "hops": 2})
    assert "e4" not in out and "disaster" not in out
    out, _ = tools.call("find_path", {"from_type": "symbol", "from_id": "XLE", "to_type": "event", "to_id": "e4"})
    assert json.loads(out) == {"connected": False}


def test_the_default_debate_skips_the_disaster_specialist(tmp_path: Path, view: GraphView) -> None:
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(_URL).mock(return_value=_reply('{"thesis": "x", "direction": "neutral", "confidence": 0.1}'))
        result = rt.debate_on_view(view, XLE, domains=("energy", "disaster"), journal=False, news_path=tmp_path / NO_NEWS)
    assert set(result.report.outcomes) == {"energy"}
    assert all("Domain: disaster" not in r.request.content.decode() for r in route.calls)


# ---------------------------------------------------------------- nothing sees the future


@pytest.mark.parametrize("target", [XLE, "refinery strike"])
def test_future_edges_change_no_bundle(tmp_path: Path, store: GraphStore, target: Any) -> None:
    news = tmp_path / NO_NEWS
    v = store.as_of_view(AFTER)
    before = rt.retrieve_evidence(v, target, news_path=news)
    store.ingest(_future())
    assert rt.retrieve_evidence(v, target, news_path=news) == before
    after = rt.retrieve_evidence(store.as_of_view(T0 + timedelta(hours=101)), target, news_path=news)
    assert "leak" in {i.event_id for i in after.items}     # the same edges do arrive for a later view


def test_tools_see_only_the_view(store: GraphStore) -> None:
    v = store.as_of_view(AFTER)
    store.ingest(_future())
    tools = rt.ViewTools(v)
    assert "leak" not in tools.call("search_text", {"query": "leak strike"})[0]
    assert "leak" not in tools.call("neighbors", {"node_type": "symbol", "node_id": "XLE"})[0]


def test_the_retrieval_module_reads_only_through_a_view_and_the_news_join() -> None:
    source = inspect.getsource(rt)
    for banned in ("load_edges", "default_graph_journal", "GraphStore", "sync_journal", "._select"):
        assert banned not in source, banned


def test_no_tool_definition_offers_a_time_or_a_scope(view: GraphView) -> None:
    for definition in rt.ViewTools(view).definitions():
        props = set(definition["input_schema"]["properties"])
        assert not props & {"as_of", "cutoff", "since", "until", "scope", "date"}, definition["name"]
        assert definition["input_schema"]["additionalProperties"] is False


def test_nothing_on_the_trading_path_imports_retrieval() -> None:
    root = Path(intel.__file__).resolve().parents[1]
    for folder in ("execution", "monitor", "risk", "brokers", "strategies"):
        for path in (root / folder).rglob("*.py"):
            assert "intel.retrieval" not in path.read_text(encoding="utf-8"), path


# ---------------------------------------------------------------- tools


def test_a_tool_budget_is_enforced_and_a_bad_call_comes_back_as_text(view: GraphView) -> None:
    tools = rt.ViewTools(view, max_calls=2)
    assert tools.call("neighbors", {"node_type": "symbol"})[1] is True      # missing argument, no exception
    assert tools.call("no_such_tool", {})[1] is True
    out, err = tools.call("search_text", {"query": "opec"})
    assert err is True and "budget" in out


def test_hops_are_capped_whatever_the_model_asks(view: GraphView) -> None:
    out = json.loads(rt.ViewTools(view).call("neighbors", {"node_type": "symbol", "node_id": "USO", "hops": 99})[0])
    assert max(n["hops"] for n in out) == 2


# ---------------------------------------------------------------- the debate: citations, tools, memory, trace


def _reply(text: str, *, stop: str = "end_turn", extra: list[dict[str, Any]] | None = None) -> httpx.Response:
    return httpx.Response(200, json={"content": [*(extra or []), {"type": "text", "text": text}], "stop_reason": stop,
                                     "usage": {"input_tokens": 100, "output_tokens": 20}})


def _claim(*evidence: str, direction: str = "adverse", confidence: float = 0.8) -> str:
    return json.dumps({"thesis": "Supply stress", "direction": direction, "confidence": confidence,
                       "inference": "One line.", "evidence": list(evidence)})


UPHELD = json.dumps({"verdict": "UPHELD", "reason": "Corroborated.", "demote_by": 0})


def _script(*replies: httpx.Response) -> Any:
    it = iter(replies)
    return lambda _req: next(it)


@respx.mock
def test_a_debate_cites_paths_that_exist_in_the_view(tmp_path: Path, view: GraphView) -> None:
    respx.post(_URL).mock(side_effect=_script(_reply(_claim("e1", "e2")), _reply(UPHELD)))
    result = rt.debate_on_view(view, XLE, domains=("energy",), journal=False, news_path=tmp_path / NO_NEWS)
    assert [t.evidence for t in result.report.fired] == [["e1", "e2"]]
    cited = result.citations["energy"]
    assert [c.event_id for c in cited] == ["e1", "e2"]
    graph = gs.build_graph(view, gs.SIGNAL_SCOPE)
    for c in cited:
        assert c.path is not None and c.path.nodes[-1] == ("event", c.event_id)
        assert all(graph.has_edge(h.source, h.target) for h in c.path.hops)


@respx.mock
def test_a_made_up_citation_is_dropped_and_a_claim_with_none_is_too(tmp_path: Path, view: GraphView) -> None:
    respx.post(_URL).mock(side_effect=_script(_reply(_claim("e1", "invented")), _reply(UPHELD)))
    kept = rt.debate_on_view(view, XLE, domains=("energy",), journal=False, news_path=tmp_path / NO_NEWS)
    assert kept.report.fired[0].evidence == ["e1"]

    route = respx.post(_URL).mock(side_effect=_script(_reply(_claim("invented", "e4"))))
    sent = route.call_count
    dropped = rt.debate_on_view(view, XLE, domains=("energy",), journal=False, news_path=tmp_path / NO_NEWS)
    assert dropped.report.fired == [] and dropped.report.outcomes["energy"]["outcome"] == "uncited"
    assert route.call_count == sent + 1                    # the adversary was not paid to read it; e4 is disaster


@respx.mock
def test_an_agent_can_search_then_answer_and_thinking_goes_back_unchanged(tmp_path: Path, view: GraphView) -> None:
    thinking = {"type": "thinking", "thinking": "", "signature": "sig-1"}
    tool_use = {"type": "tool_use", "id": "tu_1", "name": "search_text", "input": {"query": "opec"}}
    first = httpx.Response(200, json={"content": [thinking, tool_use], "stop_reason": "tool_use",
                                      "usage": {"input_tokens": 50, "output_tokens": 5}})
    route = respx.post(_URL).mock(side_effect=_script(first, _reply(_claim("e2")), _reply(UPHELD)))
    result = rt.debate_on_view(view, XLE, domains=("energy",), journal=False, news_path=tmp_path / NO_NEWS)
    assert result.report.fired[0].evidence == ["e2"]
    second = json.loads(route.calls[1].request.content)
    assert second["messages"][1] == {"role": "assistant", "content": [thinking, tool_use]}
    result_block = second["messages"][2]["content"][0]
    assert result_block["tool_use_id"] == "tu_1" and "OPEC" in result_block["content"]
    assert "tool_choice" not in second                     # forced tool use returns a 400 on current models
    assert result.report.outcomes["energy"]["calls"][0]["tool_calls"] == 1


@respx.mock
def test_a_model_that_never_stops_searching_ends_as_a_failure(tmp_path: Path, view: GraphView) -> None:
    tool_use = {"type": "tool_use", "id": "tu_1", "name": "search_text", "input": {"query": "opec"}}
    respx.post(_URL).mock(return_value=httpx.Response(200, json={"content": [tool_use], "stop_reason": "tool_use", "usage": {}}))
    result = rt.debate_on_view(view, XLE, domains=("energy",), journal=False, news_path=tmp_path / NO_NEWS)
    assert result.report.errors == {"energy": "tool_turn_limit"}


@respx.mock
def test_earlier_claims_are_shown_only_if_made_before_the_cutoff(tmp_path: Path, store: GraphStore) -> None:
    journal, news = tmp_path / "agents.jsonl", tmp_path / NO_NEWS
    respx.post(_URL).mock(side_effect=_script(_reply(_claim("e1")), _reply(UPHELD)))
    rt.debate_on_view(store.as_of_view(T0 + timedelta(hours=1, minutes=30)), XLE, domains=("energy",),
                      journal_path=journal, news_path=news)
    memory = rt.ThesisMemory(journal)
    at_debate = T0 + timedelta(hours=1, minutes=30)
    assert memory.before(at_debate) == []                  # strictly before, so not its own row
    assert memory.before(at_debate - timedelta(hours=1)) == []
    later = memory.before(AFTER)
    assert later[0]["thesis"] == "Supply stress" and later[0]["verdict"] == "UPHELD"
    assert memory.before(AFTER, domains=["macro"]) == []

    route = respx.post(_URL).mock(side_effect=_script(_reply(_claim("e1")), _reply(UPHELD)))
    sent = route.call_count
    rt.debate_on_view(store.as_of_view(AFTER), XLE, domains=("energy",), journal=False, news_path=news,
                      journal_path=journal)
    assert "earlier_claims" in route.calls[sent].request.content.decode()

    route = respx.post(_URL).mock(side_effect=_script(_reply(_claim("e1")), _reply(UPHELD)))
    sent = route.call_count
    rt.debate_on_view(store.as_of_view(T0 + timedelta(hours=1, minutes=1)), XLE, domains=("energy",), journal=False,
                      news_path=news, journal_path=journal)
    assert "earlier_claims" not in route.calls[sent].request.content.decode()   # a replay before it cannot read it


def test_memory_skips_rows_it_cannot_place_in_time(tmp_path: Path) -> None:
    journal = tmp_path / "agents.jsonl"
    row = {"as_of": "", "outcomes": {"energy": {"outcome": "fired", "claim": {"thesis": "x"}}}}
    journal.write_text(json.dumps(row) + "\n{broken\n", encoding="utf-8")
    assert rt.ThesisMemory(journal).before(AFTER) == []
    assert rt.ThesisMemory(tmp_path / "absent.jsonl").before(AFTER) == []


@respx.mock
def test_the_journal_traces_what_each_debate_read_and_did(tmp_path: Path, view: GraphView) -> None:
    journal = tmp_path / "agents.jsonl"
    respx.post(_URL).mock(side_effect=_script(_reply(_claim("e1")), _reply(UPHELD)))
    result = rt.debate_on_view(view, XLE, domains=("energy",), journal_path=journal, news_path=tmp_path / NO_NEWS)
    row = json.loads(journal.read_text(encoding="utf-8").splitlines()[0])
    assert row["as_of"] == AFTER.isoformat() and row["target"] == ["symbol", "XLE"]
    assert row["evidence_hash"] == result.bundle.content_hash
    assert set(row["evidence_ids"]) == {i.event_id for i in result.bundle.items}
    specialist, adversary = row["outcomes"]["energy"]["calls"]
    assert specialist["model"] and len(specialist["prompt_sha"]) == 16 and specialist["stop_reason"] == "end_turn"
    assert (specialist["input_tokens"], specialist["output_tokens"]) == (100, 20)
    assert specialist["prompt_sha"] != adversary["prompt_sha"]


# ---------------------------------------------------------------- failure is told apart from silence


@respx.mock
def test_low_credit_is_a_failure_not_a_quiet_world(tmp_path: Path, view: GraphView) -> None:
    respx.post(_URL).mock(return_value=httpx.Response(400, json={
        "type": "error", "error": {"type": "invalid_request_error", "message": "Your credit balance is too low"}}))
    result = rt.debate_on_view(view, XLE, domains=("energy",), journal=False, news_path=tmp_path / NO_NEWS)
    assert result.report.fired == []
    assert result.report.errors == {"energy": "api_400:invalid_request_error"}


@respx.mock
def test_a_quiet_answer_has_no_error(tmp_path: Path, view: GraphView) -> None:
    respx.post(_URL).mock(return_value=_reply(_claim("e1", direction="neutral", confidence=0.1)))
    result = rt.debate_on_view(view, XLE, domains=("energy",), journal=False, news_path=tmp_path / NO_NEWS)
    assert result.report.fired == [] and result.report.errors == {}
    assert result.report.outcomes["energy"]["outcome"] == "below_threshold"


def test_each_failure_names_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    from trading_live_claude.config import get_settings
    evidence = [{"id": "EV", "title": "energy outage"}]
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(_URL)
        cases = {"http_error": httpx.ConnectError("down"), "unparseable": _reply("not json at all"),
                 "api_529:overloaded_error": httpx.Response(529, json={"error": {"type": "overloaded_error"}})}
        for expected, reply in cases.items():
            route.mock(side_effect=[reply])  # one reply per case: only the specialist runs
            assert run_debate(evidence, domains=("energy",), journal=False).errors == {"energy": expected}
        route.mock(side_effect=[httpx.Response(200, json={"content": [], "stop_reason": "refusal"})])
        assert run_debate(evidence, domains=("energy",), journal=False).errors == {"energy": "refusal"}
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    get_settings.cache_clear()
    assert run_debate(evidence, domains=("energy",), journal=False).errors == {"energy": "no_api_key"}


@respx.mock
def test_an_adversary_failure_is_reported_and_drops_the_claim() -> None:
    respx.post(_URL).mock(side_effect=_script(_reply(_claim("EV")), httpx.Response(500, json={})))
    report = run_debate([{"id": "EV", "title": "energy outage"}], domains=("energy",), journal=False)
    assert report.fired == [] and report.errors == {"energy": "api_500"}
    assert report.outcomes["energy"]["outcome"] == "no_critique"


@respx.mock
def test_the_failure_is_on_the_journal_row(tmp_path: Path) -> None:
    respx.post(_URL).mock(return_value=httpx.Response(400, json={"error": {"type": "invalid_request_error"}}))
    journal = tmp_path / "agents.jsonl"
    run_debate([{"id": "EV", "title": "energy outage"}], domains=("energy",), journal_path=journal)
    row = json.loads(journal.read_text(encoding="utf-8").splitlines()[0])
    assert row["errors"] == {"energy": "api_400:invalid_request_error"}
    assert row["outcomes"]["energy"]["error"] == "api_400:invalid_request_error"


@respx.mock
def test_plain_debate_still_returns_a_list_and_sends_no_tools() -> None:
    route = respx.post(_URL).mock(side_effect=_script(_reply(_claim("EV")), _reply(UPHELD)))
    fired = debate([{"id": "EV", "title": "energy outage"}], domains=("energy",), journal=False)
    assert [t.thesis for t in fired] == ["Supply stress"] and fired[0].evidence == ["EV"]
    assert "tools" not in json.loads(route.calls[0].request.content)
