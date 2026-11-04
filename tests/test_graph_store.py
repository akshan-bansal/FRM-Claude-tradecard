"""The graph store's promise: a reader can never see an edge from at or after its cutoff."""
from __future__ import annotations

import ast
import inspect
import json
import random
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from trading_live_claude.intel import graph_store
from trading_live_claude.intel.graph import (
    Edge,
    append_edges,
    default_graph_journal,
    event_records_to_edges,
)
from trading_live_claude.intel.graph_store import (
    GraphStore,
    GraphView,
    default_graph_store,
    stamp_to_us,
)

T0 = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def _edge(at: datetime | str, *, domain: str = "conflict", poll: str | None = None, weight: float = 3.0,
          predicate: str = "elevated_in") -> Edge:
    stamp = at if isinstance(at, str) else at.isoformat()
    return Edge(("poll", poll or stamp), predicate, ("domain", domain), weight=weight, as_of=stamp)  # type: ignore[arg-type]


@pytest.fixture()
def store(tmp_path: Path) -> GraphStore:
    with GraphStore(tmp_path / "g.sqlite") as s:
        yield s


def _every_read(view: GraphView) -> dict[str, list[Edge]]:
    """Every way a view returns edges. A new reader must be added here (see the surface test below)."""
    return {"edges": view.edges(), "by_predicate": view.edges(predicate="elevated_in"),
            "by_object": view.edges(object=("domain", "conflict")),
            "since": view.edges(since=T0 - timedelta(days=400)), "washed": view.washed(policies={})}


# ---------------------------------------------------------------- no future edge, the phase's acceptance test


def test_no_read_returns_an_edge_at_or_after_the_cutoff_on_a_shuffled_journal(tmp_path: Path) -> None:
    rng = random.Random(20261103)
    offsets = [timedelta(minutes=rng.randrange(0, 60 * 24 * 60), microseconds=rng.randrange(1_000_000))
               for _ in range(3000)]
    zones = [UTC, timezone(timedelta(hours=-5)), timezone(timedelta(hours=9, minutes=30))]
    edges = [_edge((T0 + o).astimezone(rng.choice(zones)), domain=rng.choice(["conflict", "energy"]))
             for o in offsets]
    rng.shuffle(edges)                                    # journal order says nothing about time
    journal = tmp_path / "g.jsonl"
    append_edges(edges, path=journal)
    with GraphStore(tmp_path / "g.sqlite") as store:
        assert store.sync_journal(journal) == len(edges)
        cutoffs = [T0 + rng.choice(offsets) for _ in range(40)] + [T0 - timedelta(days=1), T0 + timedelta(days=99)]
        for cutoff in cutoffs:
            view = store.as_of_view(cutoff)
            for name, got in _every_read(view).items():
                assert all(datetime.fromisoformat(e.as_of) < cutoff for e in got), name
            truth = {e.as_of for e in edges if datetime.fromisoformat(e.as_of) < cutoff}
            assert {e.as_of for e in view.edges()} == truth   # and nothing knowable is withheld


def test_an_edge_stamped_exactly_at_the_cutoff_is_not_visible(store: GraphStore) -> None:
    store.ingest([_edge(T0)])
    assert store.as_of_view(T0).edges() == []
    assert len(store.as_of_view(T0 + timedelta(microseconds=1)).edges()) == 1


def test_a_view_cannot_be_changed_by_what_arrives_later(store: GraphStore) -> None:
    store.ingest([_edge(T0)])
    view = store.as_of_view(T0 + timedelta(hours=1))
    before = view.edges()
    store.ingest([_edge(T0 + timedelta(hours=2)), _edge(T0 + timedelta(hours=3))])
    assert view.edges() == before


def test_times_compare_as_instants_not_as_strings(store: GraphStore) -> None:
    # As text the first sorts after the cutoff; as an instant it is an hour before, and the reverse below.
    earlier = datetime(2026, 6, 1, 8, 0, tzinfo=timezone(timedelta(hours=-3)))
    later = datetime(2026, 6, 1, 20, 0, tzinfo=timezone(timedelta(hours=7)))
    assert earlier.astimezone(UTC) == T0 - timedelta(hours=1) and later.astimezone(UTC) == T0 + timedelta(hours=1)
    store.ingest([_edge(earlier), _edge(later), _edge("2026-06-01T11:59:00Z")])
    assert {e.as_of for e in store.as_of_view(T0).edges()} == {earlier.isoformat(), "2026-06-01T11:59:00Z"}


@pytest.mark.parametrize("stamp", ["", "not-a-date"])
def test_an_edge_with_no_readable_time_is_never_visible(store: GraphStore, stamp: str) -> None:
    store.ingest([_edge(stamp, poll="p")])
    assert store.as_of_view(T0 + timedelta(days=36500)).edges() == []
    assert store.stats()["undated"] == 1 and store.stats()["edges"] == 1


def test_a_naive_cutoff_is_refused(store: GraphStore) -> None:
    with pytest.raises(ValueError):
        store.as_of_view(datetime(2026, 6, 1, 12, 0))
    with pytest.raises(ValueError):
        store.as_of_view(T0).edges(since=datetime(2026, 6, 1))


def test_persistence_stops_at_the_cutoff(store: GraphStore) -> None:
    calm = T0 + timedelta(hours=3)
    store.ingest([_edge(T0 + timedelta(hours=i)) for i in range(3)])
    store.ingest([Edge(("poll", calm.isoformat()), "observed", ("domain", "energy"), as_of=calm.isoformat())])
    target = ("domain", "conflict")
    assert store.as_of_view(T0 + timedelta(hours=2, minutes=1)).persistence("elevated_in", target) == 3
    assert store.as_of_view(T0 + timedelta(hours=1, minutes=1)).persistence("elevated_in", target) == 2
    # The calm poll came later, so it cannot break a run read from before it, only one read from after.
    assert store.as_of_view(T0 + timedelta(hours=4)).persistence("elevated_in", target) == 0


def test_persistence_ages_against_the_cutoff_not_the_wall_clock(store: GraphStore) -> None:
    long_ago = datetime(2020, 1, 1, tzinfo=UTC)
    store.ingest([_edge(long_ago + timedelta(minutes=30 * i)) for i in range(6)])
    target = ("domain", "conflict")
    fresh = long_ago + timedelta(hours=3, minutes=30)
    assert store.as_of_view(fresh).persistence("elevated_in", target, max_age_hours=6.0) == 6
    stale = long_ago + timedelta(days=30)
    assert store.as_of_view(stale).persistence("elevated_in", target, max_age_hours=6.0) == 0


def test_events_show_only_what_was_known_at_the_cutoff(store: GraphStore) -> None:
    early, late = T0.isoformat(), (T0 + timedelta(hours=5)).isoformat()
    store.ingest(event_records_to_edges([{"id": "e1", "title": "First", "sources": ["ap"]}], domain="energy",
                                        poll_id=early, as_of=early))
    store.ingest(event_records_to_edges([{"id": "e2", "title": "Second", "sources": ["bbc"]}], domain="energy",
                                        poll_id=late, as_of=late))
    store.ingest(event_records_to_edges([{"id": "e1", "title": "First", "sources": ["reuters"]}], domain="energy",
                                        poll_id=late, as_of=late))
    records = store.as_of_view(T0 + timedelta(hours=1)).recent_events()
    assert [r["id"] for r in records] == ["e1"]
    assert records[0]["sources"] == ["ap"]                # the later corroboration has not happened yet
    assert {r["id"] for r in store.as_of_view(T0 + timedelta(hours=6)).recent_events()} == {"e1", "e2"}


def test_decay_is_computed_at_the_cutoff(store: GraphStore) -> None:
    store.ingest([_edge(T0, weight=8.0)])
    policies = {"elevated_in": graph_store.DecayPolicy(mode="exp", half_life_h=24.0)}
    weight = store.as_of_view(T0 + timedelta(hours=24)).washed(policies)[0].weight
    assert weight == pytest.approx(4.0)                   # one half-life, however long ago that really was


def test_the_only_way_to_read_rows_is_through_the_guarded_select() -> None:
    tree = ast.parse(inspect.getsource(graph_store))
    reads = []
    for fn in (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)):
        for node in ast.walk(fn):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and "FROM edges" in node.value:
                if node.value.lstrip().startswith("SELECT COUNT") or fn.name == "stats":
                    continue
                reads.append(fn.name)
    assert reads == ["_select"]
    source = inspect.getsource(GraphView._select)
    assert "as_of_us IS NOT NULL AND as_of_us < ?" in source


def test_the_store_itself_offers_no_edge_reader() -> None:
    public = {n for n, v in inspect.getmembers(GraphStore, inspect.isfunction) if not n.startswith("_")}
    assert public == {"as_of_view", "close", "ingest", "stats", "sync_journal"}
    readers = {n for n, v in inspect.getmembers(GraphView, inspect.isfunction) if not n.startswith("_")}
    assert readers == {"cached", "edges", "persistence", "recent_events", "washed"}  # add new readers to _every_read


# ---------------------------------------------------------------- staying caught up with the journal


def test_sync_reads_only_what_is_new_and_is_idempotent(tmp_path: Path, store: GraphStore) -> None:
    journal = tmp_path / "g.jsonl"
    append_edges([_edge(T0), _edge(T0 + timedelta(hours=1))], path=journal)
    assert store.sync_journal(journal) == 2
    assert store.sync_journal(journal) == 0
    append_edges([_edge(T0 + timedelta(hours=2))], path=journal)
    assert store.sync_journal(journal) == 1
    assert store.stats()["edges"] == 3


def test_a_half_written_line_waits_for_the_next_sync(tmp_path: Path, store: GraphStore) -> None:
    journal = tmp_path / "g.jsonl"
    append_edges([_edge(T0)], path=journal)
    row = json.dumps(_edge(T0 + timedelta(hours=1)).to_row())
    with journal.open("a", encoding="utf-8") as fh:
        fh.write(row[:20])
    assert store.sync_journal(journal) == 1
    with journal.open("a", encoding="utf-8") as fh:
        fh.write(row[20:] + "\n")
    assert store.sync_journal(journal) == 1


def test_a_corrupt_line_is_skipped_and_the_rest_are_kept(tmp_path: Path, store: GraphStore) -> None:
    journal = tmp_path / "g.jsonl"
    journal.write_text(json.dumps(_edge(T0).to_row()) + "\n{broken\n"
                       + json.dumps(_edge(T0 + timedelta(hours=1)).to_row()) + "\n", encoding="utf-8")
    assert store.sync_journal(journal) == 2


def test_a_washed_journal_keeps_identity_first_weight_and_pruned_history(tmp_path: Path, store: GraphStore) -> None:
    journal = tmp_path / "g.jsonl"
    append_edges([_edge(T0, weight=8.0), _edge(T0 + timedelta(hours=1), weight=8.0)], path=journal)
    store.sync_journal(journal)
    # A wash rewrites the file in place: the first edge decayed, the second pruned.
    journal.write_text(json.dumps(_edge(T0, weight=1.0).to_row()) + "\n", encoding="utf-8")
    assert store.sync_journal(journal) == 0
    edges = store.as_of_view(T0 + timedelta(days=1)).edges()
    assert [e.weight for e in edges] == [8.0, 8.0]
    append_edges([_edge(T0 + timedelta(hours=2))], path=journal)
    assert store.sync_journal(journal) == 1               # and it carries on from the rewritten file


def test_a_missing_journal_is_no_error(tmp_path: Path, store: GraphStore) -> None:
    assert store.sync_journal(tmp_path / "absent.jsonl") == 0


def test_the_store_survives_being_reopened(tmp_path: Path) -> None:
    journal = tmp_path / "g.jsonl"
    append_edges([_edge(T0)], path=journal)
    with GraphStore(tmp_path / "g.sqlite") as first:
        first.sync_journal(journal)
    with GraphStore(tmp_path / "g.sqlite") as second:
        assert second.sync_journal(journal) == 0
        assert len(second.as_of_view(T0 + timedelta(hours=1)).edges()) == 1


def test_edges_round_trip_intact(store: GraphStore) -> None:
    original = Edge(("venue", "kraken"), "traded", ("symbol", "SOL/USD"), weight=-12.5, as_of=T0.isoformat(),
                    meta={"action": "Sell", "qty": 2.0})
    store.ingest([original])
    assert store.as_of_view(T0 + timedelta(seconds=1)).edges() == [original]


def test_the_default_paths_follow_state_dir() -> None:
    assert default_graph_store().parent == default_graph_journal().parent
    assert default_graph_store().suffix == ".sqlite"


def test_a_bad_stamp_reads_as_none_and_a_naive_one_as_utc() -> None:
    assert stamp_to_us("nope") is None and stamp_to_us("") is None
    assert stamp_to_us("2026-06-01T12:00:00") == stamp_to_us("2026-06-01T12:00:00+00:00")
