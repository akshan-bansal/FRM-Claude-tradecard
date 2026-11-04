"""Point-in-time read model of the intel graph journal: the base the graph search is built on.

``state/intel_graph.jsonl`` stays the write path. This is a SQLite index over it, kept beside it and
caught up with :meth:`GraphStore.sync_journal`. Its one job is that **a reader can only ever ask what was
knowable at a moment**: :class:`GraphStore` has no method that returns edges, so every read goes through a
:class:`GraphView` bound to a cutoff, and every row a view returns comes from :meth:`GraphView._select`,
which always applies that cutoff.

The rules a view holds to:

* An edge is visible only when ``as_of`` is strictly before the cutoff. ``as_of`` is when the information
  became known here (a poll's read time, a collector's arrival time, a fill's time), not when an event
  happened. Same convention as ``analysis/graph_adapter.py``.
* An edge with a missing or unparseable ``as_of`` is never visible: nothing can show it was known in time.
  :meth:`GraphStore.stats` counts them.
* Times compare as instants, not strings, so ``-05:00`` and ``Z`` stamps order correctly. A naive stamp is
  read as UTC, as in ``intel/graph.py``; a naive cutoff is refused, because that would be a guess.
* Decay is computed at the cutoff, not at the wall clock, so a replay sees the weights it would have seen.

``wash_journal_file`` rewrites the journal, decaying weights and pruning old rows. An edge's identity
therefore leaves out its weight: a washed row is the same edge, and the store keeps the weight it first saw.
Rows pruned from the journal stay here, which is what makes older views possible. The store is still
derived: delete it and it rebuilds from the journal, keeping only what the journal still holds.

Not on the trading path. Nothing in the order flow imports it yet.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Hashable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import Any

from trading_live_claude.intel.graph import (
    DecayPolicy,
    Edge,
    NodeType,
    Predicate,
    default_graph_journal,
    edge_persistence,
    recent_events_from_graph,
    wash_edges,
)
from trading_live_claude.logging_setup import get_logger

log = get_logger(__name__)

SCHEMA_VERSION = 1
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MICROSECOND = timedelta(microseconds=1)
_COLUMNS = "subject_type, subject_id, predicate, object_type, object_id, weight, as_of, meta"
_SCHEMA = """
CREATE TABLE IF NOT EXISTS edges (
    edge_key TEXT PRIMARY KEY,
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    predicate TEXT NOT NULL,
    object_type TEXT NOT NULL,
    object_id TEXT NOT NULL,
    weight REAL NOT NULL,
    as_of TEXT NOT NULL,
    as_of_us INTEGER,
    meta TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS edges_as_of ON edges (as_of_us);
CREATE INDEX IF NOT EXISTS edges_predicate ON edges (predicate, object_type, object_id, as_of_us);
CREATE INDEX IF NOT EXISTS edges_subject ON edges (subject_type, subject_id, as_of_us);
CREATE INDEX IF NOT EXISTS edges_object ON edges (object_type, object_id, as_of_us);
CREATE TABLE IF NOT EXISTS journal_sync (
    path TEXT PRIMARY KEY,
    byte_offset INTEGER NOT NULL,
    head_sha TEXT NOT NULL
);
"""


def default_graph_store() -> Path:
    """The store beside the graph journal, under the configured ``state_dir``."""
    return default_graph_journal().with_suffix(".sqlite")


def stamp_to_us(stamp: str) -> int | None:
    """Microseconds since the epoch for an ISO-8601 stamp, or ``None`` when it cannot be read."""
    if not stamp:
        return None
    try:
        when = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return (when - _EPOCH) // _MICROSECOND


def edge_key(edge: Edge) -> str:
    """An edge's identity. Leaves out the weight, which a wash rewrites."""
    body = json.dumps([edge.subject, edge.predicate, edge.object, edge.as_of, edge.meta],
                      sort_keys=True, default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _row_to_edge(row: Sequence[Any]) -> Edge:
    return Edge(subject=(row[0], row[1]), predicate=row[2], object=(row[3], row[4]),
                weight=float(row[5]), as_of=str(row[6]), meta=json.loads(row[7]))


class GraphStore:
    """The SQLite index. Writes edges in and hands out views; it never returns an edge itself."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_graph_store()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, timeout=30.0)
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, SCHEMA_VERSION):
            self._conn.close()
            raise RuntimeError(f"{self.path} has schema {version}, this code reads {SCHEMA_VERSION}; "
                               "it is derived, so delete it and sync again")
        self._conn.executescript(_SCHEMA)
        self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> GraphStore:
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                 tb: TracebackType | None) -> None:
        self.close()

    def _insert(self, edges: Iterable[Edge]) -> int:
        before = self._conn.total_changes
        self._conn.executemany(
            f"INSERT OR IGNORE INTO edges (edge_key, {_COLUMNS}, as_of_us) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ((edge_key(e), e.subject[0], e.subject[1], e.predicate, e.object[0], e.object[1], float(e.weight),
              e.as_of, json.dumps(e.meta, sort_keys=True, default=str), stamp_to_us(e.as_of)) for e in edges))
        return self._conn.total_changes - before

    def ingest(self, edges: Iterable[Edge]) -> int:
        """Add edges, ignoring any already held. Returns how many were new."""
        added = self._insert(edges)
        self._conn.commit()
        return added

    def sync_journal(self, path: str | Path | None = None) -> int:
        """Catch up with the journal from where the last sync stopped. Returns how many edges were new.

        A half-written last line waits for the next sync. A journal that shrank or whose first line changed
        was rewritten by a wash, so it is read again from the top, which the edge identity makes harmless.
        """
        journal = Path(path) if path is not None else default_graph_journal()
        if not journal.exists():
            return 0
        key = str(journal.resolve())
        with journal.open("rb") as fh:
            head = fh.readline()
            head_sha = hashlib.sha256(head).hexdigest()
            size = fh.seek(0, 2)
            row = self._conn.execute("SELECT byte_offset, head_sha FROM journal_sync WHERE path = ?",
                                     (key,)).fetchone()
            offset = row[0] if row is not None and row[1] == head_sha and row[0] <= size else 0
            fh.seek(offset)
            data = fh.read()
        complete = data.rfind(b"\n") + 1
        edges: list[Edge] = []
        for line in data[:complete].decode("utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                edges.append(Edge.from_row(json.loads(line)))
            except Exception:
                log.warning("graph_store.sync_skipped_row", path=key)
        added = self._insert(edges)
        self._conn.execute("INSERT OR REPLACE INTO journal_sync (path, byte_offset, head_sha) VALUES (?, ?, ?)",
                           (key, offset + complete, head_sha))
        self._conn.commit()
        return added

    def as_of_view(self, cutoff: datetime) -> GraphView:
        """What was knowable strictly before ``cutoff``, which must carry a timezone."""
        if cutoff.tzinfo is None or cutoff.utcoffset() is None:
            raise ValueError("as_of_view needs a timezone-aware cutoff")
        return GraphView(self._conn, cutoff.astimezone(UTC))

    def stats(self) -> dict[str, Any]:
        """Counts only: how many edges are held, and how many no view can ever show."""
        total, undated, first, last = self._conn.execute(
            "SELECT COUNT(*), SUM(as_of_us IS NULL), MIN(as_of_us), MAX(as_of_us) FROM edges").fetchone()
        to_iso = lambda us: None if us is None else (_EPOCH + us * _MICROSECOND).isoformat()  # noqa: E731
        return {"edges": total, "undated": undated or 0, "first_as_of": to_iso(first), "last_as_of": to_iso(last)}


@dataclass(frozen=True)
class GraphView:
    """The graph as it stood just before ``cutoff``. Every read below goes through :meth:`_select`."""

    _conn: sqlite3.Connection
    cutoff: datetime

    def _select(self, where: str = "", params: Sequence[Any] = ()) -> list[Edge]:
        """The only row read in this module. Oldest first, by time and not by journal position."""
        clause = f" AND ({where})" if where else ""
        rows = self._conn.execute(
            f"SELECT {_COLUMNS} FROM edges WHERE as_of_us IS NOT NULL AND as_of_us < ?{clause} "
            "ORDER BY as_of_us, rowid",
            (stamp_to_us(self.cutoff.isoformat()), *params)).fetchall()
        return [_row_to_edge(r) for r in rows]

    def edges(self, *, subject: tuple[NodeType, str] | None = None, predicate: Predicate | None = None,
              object: tuple[NodeType, str] | None = None, since: datetime | None = None) -> list[Edge]:
        """Edges matching every filter given. ``since`` is an inclusive lower bound on ``as_of``."""
        clauses: list[str] = []
        params: list[Any] = []
        if subject is not None:
            clauses.append("subject_type = ? AND subject_id = ?")
            params.extend(subject)
        if predicate is not None:
            clauses.append("predicate = ?")
            params.append(predicate)
        if object is not None:
            clauses.append("object_type = ? AND object_id = ?")
            params.extend(object)
        if since is not None:
            if since.tzinfo is None or since.utcoffset() is None:
                raise ValueError("since needs a timezone")
            clauses.append("as_of_us >= ?")
            params.append(stamp_to_us(since.isoformat()))
        return self._select(" AND ".join(clauses), params)

    def washed(self, policies: dict[Predicate, DecayPolicy] | None = None) -> list[Edge]:
        """Visible edges with decay and TTL applied as of the cutoff, not as of today."""
        return wash_edges(self.edges(), policies=policies, now=self.cutoff)

    def persistence(self, predicate: Predicate, object: tuple[NodeType, str],
                    max_age_hours: float | None = None) -> int:
        """:func:`~trading_live_claude.intel.graph.edge_persistence` as of the cutoff."""
        polls = self._select("subject_type = 'poll' OR object_type = 'poll' OR predicate = ?", (predicate,))
        return edge_persistence(polls, predicate=predicate, object=object, max_age_hours=max_age_hours,
                                now=self.cutoff)

    def recent_events(self, limit: int = 40) -> list[dict[str, object]]:
        """:func:`~trading_live_claude.intel.graph.recent_events_from_graph` as of the cutoff."""
        return recent_events_from_graph(self._select("subject_type = 'event' OR object_type = 'event'"), limit=limit)

    def cached(self) -> CachedGraphView:
        """This view with its reads remembered, for a run of searches that should agree with each other."""
        return CachedGraphView(self._conn, self.cutoff)


@dataclass(frozen=True)
class CachedGraphView(GraphView):
    """A view that reads each query once. Same cutoff and same guard: it remembers what :meth:`GraphView._select`
    returned and never goes around it. Hold one for one piece of work, because it will not see an edge ingested
    after its first read, even one stamped before the cutoff.
    """

    _memo: dict[Hashable, Any] = field(default_factory=dict, compare=False, repr=False)

    def _select(self, where: str = "", params: Sequence[Any] = ()) -> list[Edge]:
        key = ("select", where, tuple(params))
        if key not in self._memo:
            self._memo[key] = super()._select(where, params)
        return list(self._memo[key])

    def cached(self) -> CachedGraphView:
        return self

    def remember(self, key: Hashable, make: Callable[[], Any]) -> Any:
        """Something derived from this view's reads, computed once. Callers must not change what they get."""
        if key not in self._memo:
            self._memo[key] = make()
        return self._memo[key]
