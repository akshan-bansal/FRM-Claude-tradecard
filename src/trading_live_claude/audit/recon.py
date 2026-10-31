"""Execution reconciliation: pull executions back from the broker and tie each to its intent.

``place_order`` returns an acknowledgement, not an execution. Tax and audit need the broker's own
report: price, quantity, fee, currency, time and execution id. This module fetches it with the
broker's ``executions()`` and writes ``EXECUTION_REPORTED`` rows, joined to the intent through the
broker order id recorded on the ``FILLED`` / ``PARTIAL`` row.

Anything that does not tie up is written as ``RECON_BREAK``, once per finding:

* ``execution_without_intent``: the broker reports an execution no ledger order accounts for (a
  manual trade, another tool on the account, or a ledger gap);
* ``order_without_execution``: an order acknowledged more than ``grace_s`` ago with no execution;
* ``quantity_mismatch``: executions on an order that do not add up to what was placed.

Paper order ids restart at 1 every session, so paper orders are keyed by session; a live broker's
order ids are its own and key globally.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ..logging_setup import get_logger
from .ledger import Ledger, read_stream
from .projection import streams

log = get_logger(__name__)


@dataclass
class ReconSummary:
    fetched: int = 0
    reported: int = 0
    breaks: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None


def _all_rows(ledger_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for _name, paths in streams(ledger_dir).items():
        for path in paths:
            rows.extend(read_stream(path))
    return rows


def _order_key(row: Mapping[str, Any]) -> tuple[str, str] | None:
    exe = row.get("execution") or {}
    broker = str(exe.get("broker") or (row.get("payload") or {}).get("broker") or "")
    oid = row.get("broker_order_id")
    if oid is None:
        return None
    scope = str(row.get("session_id") or "") if broker == "paper" else broker
    return scope, str(oid)


def _exec_key(e: Mapping[str, Any], broker_name: str, session_id: str | None) -> tuple[str, str]:
    scope = str(session_id or "") if broker_name == "paper" else broker_name
    return scope, str(e.get("order_id"))


def reconcile(writer: Ledger, broker: Any, account: str, *, since: datetime | None = None,
              grace_s: float = 900.0, now: datetime | None = None,
              order_ids: Iterable[str] | None = None) -> ReconSummary:
    """Fetch executions and write what is new. Never raises: a failure is in ``error``.

    ``writer`` is the ledger the rows go to (a session's own stream in-process, or the shared
    ``recon`` stream from the CLI); every stream in its directory is read for what is already
    known, so a second run writes nothing twice. ``order_ids`` limits the pull to those orders
    (the Router's immediate check after a placement).
    """
    now = now or datetime.now(UTC)
    summary = ReconSummary()
    broker_name = str(getattr(broker, "name", ""))
    session_id = getattr(broker, "session_id", None)
    fetch = getattr(broker, "executions", None)
    if not callable(fetch):
        summary.error = f"broker {broker_name!r} cannot report executions"
        return summary
    try:
        execs = [e.model_dump(mode="json") if hasattr(e, "model_dump") else dict(e)
                 for e in fetch(account, since=since)]
    except Exception as e:                              # a broker hiccup must not break a session
        summary.error = f"{type(e).__name__}: {e}"
        log.warning("recon.fetch_failed", broker=broker_name, error=summary.error)
        return summary
    summary.fetched = len(execs)
    wanted = {str(o) for o in order_ids} if order_ids is not None else None

    rows = _all_rows(writer.dir)
    known_execs = {str((r.get("payload") or {}).get("exec_id")) for r in rows
                   if r.get("event") == "EXECUTION_REPORTED"}
    known_breaks = {(str((r.get("payload") or {}).get("kind")), str((r.get("payload") or {}).get("key")))
                    for r in rows if r.get("event") == "RECON_BREAK"}
    orders: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rows:
        if r.get("event") in ("FILLED", "PARTIAL"):
            key = _order_key(r)
            if key is not None:
                orders[key] = r

    executed: dict[tuple[str, str], float] = defaultdict(float)
    for r in rows:
        if r.get("event") == "EXECUTION_REPORTED":
            p = r.get("payload") or {}
            executed[(str(p.get("_scope", "")), str(p.get("order_id")))] += float(p.get("quantity") or 0)

    def brk(kind: str, key: str, payload: dict[str, Any], **kw: Any) -> None:
        if (kind, key) in known_breaks:
            return
        known_breaks.add((kind, key))
        row = writer.append("RECON_BREAK", {"kind": kind, "key": key, **payload}, **kw)
        summary.breaks.append({"kind": kind, "key": key, **payload})
        if row is None:
            log.error("recon.break_not_written", kind=kind, key=key)

    for e in execs:
        if wanted is not None and str(e.get("order_id")) not in wanted:
            continue
        if str(e["exec_id"]) in known_execs:
            continue
        key = _exec_key(e, broker_name, session_id)
        order = orders.get(key)
        intent_id = order.get("intent_id") if order else None
        payload = {**e, "_scope": key[0], "matched": order is not None}
        writer.append("EXECUTION_REPORTED", payload, intent_id=intent_id,
                      broker_order_id=e.get("order_id"),
                      strategy_id=order.get("strategy_id") if order else None,
                      execution={"source": "broker execution report", "broker": broker_name})
        known_execs.add(str(e["exec_id"]))
        executed[key] += float(e.get("quantity") or 0)
        summary.reported += 1
        if order is None:
            brk("execution_without_intent", str(e["exec_id"]),
                {"symbol": e.get("symbol"), "order_id": e.get("order_id"),
                 "quantity": e.get("quantity"), "price": e.get("price")},
                broker_order_id=e.get("order_id"))

    if wanted is None:                                   # a full pass also looks for silence
        for key, order in orders.items():
            scope = key[0]
            if broker_name == "paper" and scope != str(session_id or ""):
                continue                                 # another paper session's orders
            if broker_name != "paper" and scope != broker_name:
                continue
            placed = float((order.get("payload") or {}).get("shares") or 0)
            age = (now - datetime.fromisoformat(str(order["ts"]))).total_seconds()
            got = executed.get(key, 0.0)
            if age < grace_s:
                continue
            if got == 0:
                brk("order_without_execution", f"{key[0]}:{key[1]}",
                    {"order_id": key[1], "symbol": (order.get("payload") or {}).get("symbol"),
                     "placed_quantity": placed, "age_seconds": age},
                    intent_id=order.get("intent_id"), broker_order_id=key[1])
            elif abs(got - placed) > 1e-9:
                brk("quantity_mismatch", f"{key[0]}:{key[1]}",
                    {"order_id": key[1], "placed_quantity": placed, "executed_quantity": got},
                    intent_id=order.get("intent_id"), broker_order_id=key[1])
    return summary


def report_order(writer: Ledger | None, broker: Any, account: str, order_id: Any, *,
                 placed_at: datetime) -> ReconSummary | None:
    """The Router's immediate pull after a placement: executions for this one order, if any yet.

    A paper fill is reported at once; a live order usually has none this soon, and the periodic
    ``trading audit recon`` (or the session's own pass at shutdown) picks it up later.
    """
    if writer is None or order_id is None:
        return None
    return reconcile(writer, broker, account, since=placed_at - timedelta(minutes=5),
                     order_ids=[str(order_id)])
