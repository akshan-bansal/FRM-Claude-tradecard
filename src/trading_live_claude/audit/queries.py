"""Named audit queries over the ledger (AUDIT_SCOPE.md 5.3), and the tax-year export.

Each query answers one auditor's question directly from the record:

* ``superficial``: dispositions at a loss with identical property acquired in the 61-day window.
* ``approved-vs-executed``: every card-approved intent, the bytes approved, and whether what the
  broker executed matches them (symbol, side, quantity, venue).
* ``replay``: gate decisions that do not replay to the recorded outcome.
* ``fast-cancels``: orders cancelled within N seconds of dispatch.
* ``by-version``: every row written under a given code, strategy or gate version.
* ``breaks``: every reconciliation break.
"""
from __future__ import annotations

import csv
import json
from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from .ledger import Ledger, read_stream
from .projection import streams
from .semantics import BankOfCanadaFX, classify, conduct_flags, totals

QUERIES = ("fill-quality", "session-plans", "superficial", "approved-vs-executed", "replay", "fast-cancels", "by-version",
           "breaks")


def all_rows(ledger_dir: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for _name, paths in sorted(streams(Path(ledger_dir)).items()):
        for path in paths:
            out.extend(read_stream(path))
    return out


def chain_heads(ledger_dir: Path) -> dict[str, str]:
    heads: dict[str, str] = {}
    for name, paths in sorted(streams(Path(ledger_dir)).items()):
        rows = [r for p in paths for r in read_stream(p)]
        if rows:
            heads[name] = f"{rows[-1]['seq']}:{rows[-1]['row_hash']}"
    return heads


def superficial(rows: Iterable[Mapping[str, Any]], fx: BankOfCanadaFX, *,
                affiliated: Iterable[Mapping[str, Any]] = ()) -> list[dict[str, Any]]:
    return [s for s in classify(rows, fx, affiliated=affiliated)
            if (s.get("superficial") or {}).get("acquired_qty")]


def _canonical_fields(canonical: str) -> dict[str, str]:
    keys = ("broker", "action", "symbol", "shares", "entry", "notional_usd", "account",
            "intent_id", "nonce")
    parts = canonical.split("|")
    return dict(zip(keys, parts, strict=False)) if len(parts) == len(keys) else {}


def approved_vs_executed(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    rows = list(rows)
    signed = {str(r["intent_id"]): r for r in rows
              if r.get("event") == "SIGNED" and (r.get("payload") or {}).get("verdict") == "ACCEPT"}
    execs: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    venues: dict[str, str] = {}
    for r in rows:
        iid = str(r.get("intent_id") or "")
        if r.get("event") == "EXECUTION_REPORTED" and iid:
            execs[iid].append(r.get("payload") or {})
        if r.get("event") == "BROKER_SUBMITTED" and iid:
            p = r.get("payload") or {}
            venues[iid] = str(p.get("venue") or p.get("broker") or "")
    out = []
    for iid, s in signed.items():
        c = _canonical_fields(str((s.get("payload") or {}).get("canonical") or ""))
        ex = execs.get(iid, [])
        qty = sum(float(e.get("quantity") or 0) for e in ex)
        problems = []
        if not ex:
            problems.append("no execution reported")
        else:
            if any(str(e.get("symbol")) != c.get("symbol") for e in ex):
                problems.append("symbol differs from the approved bytes")
            if any(str(e.get("side")) != c.get("action") for e in ex):
                problems.append("side differs from the approved bytes")
            if c.get("shares") and abs(qty - float(c["shares"])) > 1e-9:
                problems.append(f"executed {qty} vs approved {c['shares']}")
        if c.get("broker") and venues.get(iid) and venues[iid] != c["broker"]:
            problems.append(f"dispatched to {venues[iid]}, approved for {c['broker']}")
        out.append({"intent_id": iid, "approved": c, "executed_quantity": qty,
                    "executions": [e.get("exec_id") for e in ex],
                    "status": "match" if not problems else "MISMATCH", "problems": problems})
    return out


def fast_cancels(rows: Iterable[Mapping[str, Any]], seconds: float) -> list[dict[str, Any]]:
    return [f for f in conduct_flags(rows, fast_cancel_s=seconds)
            if f["flag"] == "cancel_before_fill" and f["fast"]] + \
           [f for f in conduct_flags(rows) if f["flag"] == "self_match_risk"]


def by_version(rows: Iterable[Mapping[str, Any]], version: str) -> list[dict[str, Any]]:
    rows = list(rows)
    sessions = {str(r.get("session_id")) for r in rows if r.get("event") == "SESSION_STARTED"
                and str((r.get("payload") or {}).get("code", "")).startswith(version)}
    return [{"stream": r.get("stream"), "seq": r.get("seq"), "event": r.get("event"),
             "intent_id": r.get("intent_id"), "ts": r.get("ts")}
            for r in rows
            if version in (str(r.get("strategy_version") or ""), str(r.get("risk_check_version") or ""))
            or str(r.get("session_id")) in sessions]


def _fx_factor(frm: object, to: object, ts: object, fx: Any) -> float | None:
    """What one unit of ``frm`` is worth in ``to`` on the row's day. None when it cannot be said."""
    if not frm or not to or str(frm).upper() == str(to).upper():
        return 1.0
    if fx is None:
        return None
    try:
        on = datetime.fromisoformat(str(ts)).date()
    except ValueError:
        return None
    a, b = fx.rate(str(frm), on).rate, fx.rate(str(to), on).rate
    return a / b if a and b else None


def fill_quality(rows: Iterable[Mapping[str, Any]], fx: Any = None) -> list[dict[str, Any]]:
    """What fills actually cost, per stream, against the price the gate saw when it decided.

    Slippage is signed against the trader, so it covers half the spread as well as any move. Fees come from
    the execution report. Compare a paper stream with its money stream to see whether paper's costs
    are honest, then set ``paper_costs`` to what was measured. A money fill reports its listing currency
    while the gate decided in the home currency, so it is converted at that day's rate. One that cannot be
    converted is counted in ``unconverted``, not read as an enormous slippage.
    """
    rows = list(rows)
    reference = {r.get("intent_id"): (r.get("payload") or {}) for r in rows if r.get("event") == "RISK_CHECK"}
    groups: dict[tuple[str, str], dict[str, float]] = {}
    for r in rows:
        if r.get("event") != "EXECUTION_REPORTED":
            continue
        p = r.get("payload") or {}
        ref = reference.get(r.get("intent_id")) or {}
        entry = float(ref.get("entry") or 0.0)
        qty, price = float(p.get("quantity") or 0.0), float(p.get("price") or 0.0)
        if entry <= 0 or qty <= 0 or price <= 0:
            continue
        g = groups.setdefault((str(r.get("stream")), str(r.get("mode"))),
                              {"fills": 0, "notional": 0.0, "slip": 0.0, "fees": 0.0, "unconverted": 0})
        factor = _fx_factor(p.get("currency"), ref.get("price_currency"), r.get("ts"), fx)
        if factor is None:
            g["unconverted"] += 1
            continue
        price *= factor
        against = (price - entry) if p.get("side") == "Buy" else (entry - price)
        g["fills"] += 1
        g["notional"] += qty * price
        g["slip"] += against / entry * 10_000.0 * qty * price
        g["fees"] += float(p.get("fee") or 0.0) * factor
    return [{"stream": s, "mode": m, "fills": int(g["fills"]), "notional": round(g["notional"], 2),
             "slippage_bps": round(g["slip"] / g["notional"], 2) if g["notional"] else 0.0,
             "fee_bps": round(g["fees"] / g["notional"] * 10_000.0, 2) if g["notional"] else 0.0,
             "all_in_bps": (round(g["slip"] / g["notional"] + g["fees"] / g["notional"] * 10_000.0, 2)
                            if g["notional"] else 0.0),
             "unconverted": int(g["unconverted"])}
            for (s, m), g in sorted(groups.items())]


def session_plans(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Each session's settings fingerprint. A money session should match the paper sessions behind it."""
    return [{"ts": r.get("ts"), "stream": r.get("stream"), "mode": r.get("mode"),
             "session_id": r.get("session_id"), "venue": (r.get("payload") or {}).get("plan", {}).get("venue"),
             "fingerprint": (r.get("payload") or {}).get("fingerprint")}
            for r in rows if r.get("event") == "SESSION_PLAN"]


def breaks(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [{"stream": r.get("stream"), "seq": r.get("seq"), "ts": r.get("ts"),
             **(r.get("payload") or {})} for r in rows if r.get("event") == "RECON_BREAK"]


def load_affiliated(path: Path | None) -> list[dict[str, Any]]:
    """Executions in accounts this repo does not trade. CSV columns: account, symbol, side
    (Buy/Sell), quantity, time (ISO), and optionally venue."""
    if path is None:
        return []
    with Path(path).open(newline="", encoding="utf-8") as f:
        return [dict(r, quantity=float(r["quantity"])) for r in csv.DictReader(f)]


def export_year(ledger_dir: Path, state_dir: Path, year: int, out_dir: Path, *,
                affiliated: Path | None = None) -> dict[str, Any]:
    """Write the tax-year report (CSV of every execution's facts plus a JSON summary) and record
    its hash in the ledger, so the report on file can later be matched to the record it came from.
    """
    rows = all_rows(ledger_dir)
    fx = BankOfCanadaFX(Path(state_dir) / "fx")
    sems = classify(rows, fx, affiliated=load_affiliated(affiliated))
    year_rows = [s for s in sems if str(s.get("time", "")).startswith(str(year))]
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / f"tax_{year}_executions.csv"
    cols = ["time", "exec_id", "intent_id", "symbol", "venue", "property", "tax_event", "side",
            "quantity", "price", "currency", "currency_source", "fx_rate", "fx_date", "fx_source",
            "fee", "fee_currency", "acb_before_cad", "acb_after_cad", "qty_after", "gain_cad",
            "superficial_denied_cad", "superficial_status", "superficial_carried_to",
            "holding_days_fifo", "unknown_reasons", "stream", "seq"]
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for s in year_rows:
            lot = s.get("lot_effect") if isinstance(s.get("lot_effect"), dict) else {}
            sup = s.get("superficial") or {}
            w.writerow({
                "time": s["time"], "exec_id": s["exec_id"], "intent_id": s.get("intent_id"),
                "symbol": s["symbol"], "venue": s.get("venue"), "property": s["property"],
                "tax_event": s["tax_event"], "side": s["side"], "quantity": s["quantity"],
                "price": s["price"], "currency": s.get("currency"),
                "currency_source": s.get("currency_source"), "fx_rate": s["fx"]["rate"],
                "fx_date": s["fx"]["date"], "fx_source": s["fx"]["source"], "fee": s["fee"],
                "fee_currency": s.get("fee_currency"),
                "acb_before_cad": (lot.get("before") or {}).get("acb_cad"),
                "acb_after_cad": (lot.get("after") or {}).get("acb_cad"),
                "qty_after": (lot.get("after") or {}).get("qty"),
                "gain_cad": s.get("gain_cad") if s["tax_event"] == "disposition" else "",
                "superficial_denied_cad": sup.get("denied_loss_cad", ""),
                "superficial_status": sup.get("status", ""),
                "superficial_carried_to": sup.get("carried_to", ""),
                "holding_days_fifo": s.get("holding_days_fifo", ""),
                "unknown_reasons": "; ".join(s.get("unknown_reasons") or []),
                "stream": s.get("stream"), "seq": s.get("seq"),
            })
    summary = {"generated_at": datetime.now(UTC).isoformat(), "year": year,
               "chain_heads": chain_heads(ledger_dir), "vocab": 1,
               "totals": totals(sems, year), "conduct": conduct_flags(rows),
               "csv": csv_path.name,
               "csv_sha256": sha256(csv_path.read_bytes()).hexdigest(),
               "note": "Record-keeping aid, not tax advice. Verify with your accountant."}
    json_path = out_dir / f"tax_{year}_summary.json"
    json_path.write_text(json.dumps(summary, indent=1, sort_keys=True, default=str),
                         encoding="utf-8")
    Ledger(Path(ledger_dir), stream="control", shared=True).append("EXTERNAL_REFERENCE", {
        "kind": "tax_export", "year": year, "csv_sha256": summary["csv_sha256"],
        "summary_sha256": sha256(json_path.read_bytes()).hexdigest(),
        "chain_heads": summary["chain_heads"]})
    return {"csv": str(csv_path), "summary": str(json_path), "rows": len(year_rows),
            "unknown": summary["totals"]["rows_with_unknown_facts"]}
