"""Rebuild ``pwa/desk.html`` (QuantPort.io) from the journals: the desk panel's data is baked in at build time.

Why baked in rather than fetched: the panel can be opened as a static file, and a fetch would mean an
endpoint that exposes the whole journal. This script reads ``state/`` directly, computes every figure
the panel draws, and injects one JSON blob into ``pwa/desk.template.html``. Re-run it whenever the
numbers should catch up. Served from a running session's shim (``/desk``), the page adds a live layer
on top: pending prompts, the session strip, baskets, the IB probe and live atlas readings.

The output is journal-derived, so ``pwa/desk.html`` is gitignored and never committed.

**Data that cannot be represented faithfully is excluded, not annotated.** A session that has not
closed flat has open positions marked at whatever the last poll saw and a realised P&L that says
nothing about them, so it stays out of every aggregate and the scope of what remains is stated
instead (``data.scope``). The same rule drops sessions with too few marks to compute a ratio, and
sessions whose return is so small the ratio is a denominator artefact. EXPIRED prompts are kept out of
card response time (their ``resolved_at`` is the sweep, not a decision), positions still open are kept
out of every P&L figure with the count reported, and graph fills from sessions no fill journal knows
are left out of the GRAPH tab with the count stated.

    python scripts/build_desk_dash.py
    python scripts/build_desk_dash.py --state-dir state --out pwa/desk.html
"""
from __future__ import annotations

import argparse
import collections
import contextlib
import csv
import json
import math
import sqlite3
import statistics
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")    # type: ignore[union-attr]
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")    # type: ignore[union-attr]
    except Exception:                                                  # pragma: no cover
        pass

MIN_MARKS = 25          # a session needs this many equity marks before a ratio means anything
TRADING_DAYS = 365      # the desk's crypto sleeve trades every day; ratios annualise on 365
NOISE_FLOOR = 0.0005    # |return| under 5bp: sortino/dd is a denominator artefact, so the session is
                        # not fit for the ranking and is excluded from it
FLAT_EPS = 0.01         # positions_value at or below this counts as a closed book
TAPE_ROWS = 40


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                out.append(row)
    return out


def _ts(raw: object) -> datetime | None:
    try:
        t = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def session_metrics(rows: list[dict[str, str]]) -> dict[str, Any]:
    """Sortino, Sharpe, max drawdown and sortino_over_dd for one session's equity marks."""
    eq = [float(r["equity"]) for r in rows]
    rets = [(eq[i] - eq[i - 1]) / eq[i - 1] for i in range(1, len(eq)) if eq[i - 1]]
    down = [r for r in rets if r < 0]
    peak, dd = eq[0], 0.0
    for v in eq:
        peak = max(peak, v)
        dd = max(dd, (peak - v) / peak)
    mean = statistics.fmean(rets) if rets else 0.0
    dstd = (statistics.pstdev(down) if len(down) > 1 else 0.0) or 1e-9
    sstd = (statistics.pstdev(rets) if len(rets) > 1 else 0.0) or 1e-9
    sortino = mean / dstd * math.sqrt(TRADING_DAYS)
    return {
        "rows": len(rows), "start": rows[0]["ts"][:16], "eq0": eq[0], "eq1": eq[-1],
        "ret": eq[-1] / eq[0] - 1, "maxdd": dd, "sortino": sortino,
        "sharpe": mean / sstd * math.sqrt(TRADING_DAYS),
        "sod": (sortino / dd) if dd else 0.0,
        "realized": float(rows[-1]["realized_pnl"]),
    }


def session_fitness(marks: list[dict[str, str]]) -> tuple[bool, str]:
    """``(fit, reason_if_not)`` for one session's equity marks.

    A session that has not closed flat is the important case: its open positions are marked at the
    last poll's prices and its realised P&L says nothing about them, so nothing derived from it can
    be represented faithfully. It is excluded from every aggregate rather than shown with a warning.
    """
    if abs(float(marks[-1]["positions_value"])) > FLAT_EPS:
        return False, "not closed flat"
    if len(marks) < MIN_MARKS:
        return False, f"under {MIN_MARKS} marks"
    if abs(session_metrics(marks)["ret"]) < NOISE_FLOOR:
        return False, "return under 5bp"
    return True, ""


def histogram(values: list[float], bins: int) -> dict[str, Any]:
    lo, hi = min(values), max(values)
    width = (hi - lo) / bins or 1.0
    counts = [0] * bins
    for v in values:
        counts[min(bins - 1, int((v - lo) / width))] += 1
    return {"lo": lo, "hi": hi, "w": width, "bins": counts}


def last_prompt(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The most recent prompt that actually reached a card, for the panel's replay view.

    The real record, not a specimen: one row of ``intents``, shown with the verdict it was actually
    given. Its fingerprint is derived from the stored canonical bytes by the same function the shim
    serves with every prompt, so the abbreviations can be compared.
    """
    from trading_live_claude.execution.approval import fingerprint
    decided = [r for r in rows if r.get("canonical") and r.get("issued_at")]
    if not decided:
        return None
    r = max(decided, key=lambda x: str(x["issued_at"]))
    took: float | None = None
    ttl: int | None = None
    issued, resolved, expires = _ts(r["issued_at"]), _ts(r.get("resolved_at")), _ts(r.get("expires_at"))
    if issued and resolved and str(r["verdict"]) in ("ACCEPT", "DECLINE"):
        took = round((resolved - issued).total_seconds(), 1)
    if issued and expires:
        ttl = int((expires - issued).total_seconds())
    return {
        "symbol": r["symbol"], "action": r["action"], "shares": r["shares"],
        "entry": r["entry"], "stop": r["stop"], "notional_usd": r["notional_usd"],
        "risk_dollars": r["risk_dollars"], "strategy": r["strategy"], "account": r["account"],
        "mode": r["mode"], "broker": r["broker"], "thesis": r.get("thesis") or "",
        "fingerprint": fingerprint(str(r["canonical"]).encode("utf-8")),
        "verdict": str(r["verdict"] or "UNRESOLVED"), "took_s": took, "ttl_granted": ttl,
        "signed": bool(r.get("signature")), "issued_at": str(r["issued_at"]),
    }


def known_sessions(state: Path) -> set[str]:
    """Session ids the fill and equity journals know. A graph fill from any other session did not
    come from a session this journal records (a test run writing into state/, for instance)."""
    out = {str(r.get("session_id")) for r in read_jsonl(state / "paper_fills.jsonl")}
    if (state / "paper_equity.csv").exists():
        with (state / "paper_equity.csv").open(newline="", encoding="utf-8") as fh:
            out |= {r["session_id"] for r in csv.DictReader(fh)}
    return out - {"", "None"}


def build(state: Path) -> dict[str, Any]:
    data: dict[str, Any] = {}

    # ---- equity: per-session metrics, the richest curve, the ranking ---------------------- #
    rows: list[dict[str, str]] = []
    if (state / "paper_equity.csv").exists():
        with (state / "paper_equity.csv").open(newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
    sessions: dict[str, list[dict[str, str]]] = collections.defaultdict(list)
    for r in rows:
        sessions[r["session_id"]].append(r)
    ranked: list[dict[str, Any]] = []
    excluded: collections.Counter[str] = collections.Counter()
    fit_sessions: dict[str, list[dict[str, str]]] = {}
    for sid, marks in sessions.items():
        fit, reason = session_fitness(marks)
        if not fit:
            excluded[reason] += 1
            continue
        fit_sessions[sid] = marks
        m = session_metrics(marks)
        m["sid"] = sid[:8]
        ranked.append(m)
    ranked.sort(key=lambda m: -m["sod"])
    data["sessions"] = ranked[:9]
    data["ranked_count"] = len(ranked)
    data["session_count"] = len(sessions)
    data["plane"] = [{"sid": m["sid"], "dd": m["maxdd"], "sortino": m["sortino"],
                      "rows": m["rows"], "ret": m["ret"], "sod": m["sod"]} for m in ranked[:9]]
    data["scope"] = {
        "sessions_seen": len(sessions),
        "sessions_used": len(ranked),
        "excluded": sorted(excluded.items(), key=lambda kv: -kv[1]),
        "rule": ("Aggregates cover sessions that closed flat with enough marks to compute a ratio. A "
                 "session with positions still open is marked at the last poll's prices and its "
                 "realised P&L says nothing about them, so it is excluded rather than shown with a "
                 "warning. A running session joins once it closes flat."),
    }
    data["curve"] = data["curve_metrics"] = data["ret_hist"] = None
    if fit_sessions:
        richest = max(fit_sessions.values(), key=len)
        curve = [(r["ts"][11:16], round(float(r["equity"]), 2), round(float(r["drawdown_pct"]), 6))
                 for r in richest]
        step = max(1, len(curve) // 120)
        data["curve"] = {"sid": richest[0]["session_id"][:8], "points": curve[::step]}
        data["curve_metrics"] = session_metrics(richest)
        eq = [float(r["equity"]) for r in richest]
        bp = [(eq[i] - eq[i - 1]) / eq[i - 1] * 1e4 for i in range(1, len(eq)) if eq[i - 1]]
        if bp:
            data["ret_hist"] = {**histogram(bp, 15), "n": len(bp), "mean": statistics.fmean(bp),
                                "p05": sorted(bp)[max(0, int(len(bp) * 0.05))],
                                "downside": sum(1 for v in bp if v < 0)}

    # ---- fills: attribution, round trips, venue mix, activity ---------------------------- #
    fills = sorted(read_jsonl(state / "paper_fills.jsonl"), key=lambda r: str(r.get("fill_time", "")))
    data["fill_count"] = len(fills)
    book: dict[str, dict[str, Any]] = {}
    per_symbol: collections.Counter[str] = collections.Counter()
    fills_per_symbol: collections.Counter[str] = collections.Counter()
    trades: list[dict[str, Any]] = []
    cells: collections.Counter[tuple[int, int]] = collections.Counter()
    venues: collections.Counter[str] = collections.Counter()
    notional: dict[str, float] = collections.defaultdict(float)

    for f in fills:
        sym, qty, px = f["symbol"], float(f["quantity"]), float(f["price"])
        stamp = str(f.get("fill_time", ""))
        signed = qty if str(f.get("side")) == "Buy" else -qty
        fills_per_symbol[sym] += 1
        venues[str(f.get("venue") or "?")] += 1
        notional[str(f.get("venue") or "?")] += qty * px
        when = _ts(stamp)
        if when:
            when = when.astimezone(UTC)
            cells[(when.weekday(), when.hour)] += 1

        pos = book.get(sym)
        if pos is None:
            book[sym] = {"qty": signed, "avg": px, "open": stamp, "pnl": 0.0, "fills": 1}
            continue
        pos["fills"] += 1
        if (pos["qty"] > 0) == (signed > 0):                      # adding
            new = pos["qty"] + signed
            pos["avg"] = (pos["avg"] * pos["qty"] + px * signed) / new
            pos["qty"] = new
            continue
        closed = min(abs(pos["qty"]), abs(signed))                # reducing: realise
        realised = (1.0 if pos["qty"] > 0 else -1.0) * (px - pos["avg"]) * closed
        pos["pnl"] += realised
        per_symbol[sym] += realised
        pos["qty"] += signed
        if abs(pos["qty"]) < 1e-12:                               # episode closed
            opened, shut = _ts(pos["open"]), _ts(stamp)
            mins = (shut - opened).total_seconds() / 60 if opened and shut else 0.0
            trades.append({"sym": sym, "pnl": round(pos["pnl"], 2), "mins": round(mins, 1)})
            del book[sym]

    data["symbols"] = sorted(({"sym": s, "pnl": round(v, 2), "fills": fills_per_symbol[s]}
                              for s, v in per_symbol.items()), key=lambda d: d["pnl"])
    data["venues"] = [{"v": v, "fills": n, "notional": round(notional[v], 0)}
                      for v, n in venues.most_common()]
    data["heat"] = {"cells": [[d, h, n] for (d, h), n in sorted(cells.items())],
                    "max": max(cells.values()) if cells else 0}

    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] < 0]
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = -sum(t["pnl"] for t in losses)
    data["trades"] = {
        "n": len(trades), "wins": len(wins), "losses": len(losses),
        "win_rate": (len(wins) / len(trades)) if trades else 0.0,
        "avg_win": (gross_win / len(wins)) if wins else 0.0,
        "avg_loss": (gross_loss / len(losses)) if losses else 0.0,
        "profit_factor": (gross_win / gross_loss) if gross_loss else None,
        "expectancy": ((gross_win - gross_loss) / len(trades)) if trades else 0.0,
        "median_mins": statistics.median([t["mins"] for t in trades]) if trades else 0.0,
        # Positions still open are in no P&L figure on the panel. Their episodes have not closed, so
        # their P&L is not representable; the count is what makes the omission visible.
        "open_now": len(book),
        "scope": (f"{len(trades)} closed round trips across {len({t['sym'] for t in trades})} "
                  f"symbols; {len(book)} positions still open and excluded"),
    }
    data["trade_hist"] = histogram([t["pnl"] for t in trades], 11) if trades else None

    # ---- gate denials and per-strategy acceptance ----------------------------------------- #
    reasons: collections.Counter[str] = collections.Counter()
    for r in read_jsonl(state / "rejected.jsonl"):
        for reason in (r.get("reasons") or []):
            reasons[str(reason).split(" below ")[0].split(" >")[0][:46].strip()] += 1
    data["rejections"] = reasons.most_common(7)
    data["rejection_total"] = sum(reasons.values())

    orders = read_jsonl(state / "orders.jsonl")
    per_strategy: dict[str, dict[str, Any]] = collections.defaultdict(
        lambda: {"n": 0, "acc": 0, "reasons": collections.Counter()})
    for r in orders:
        s = per_strategy[str(r.get("strategy") or "?")]
        s["n"] += 1
        s["acc"] += 1 if r.get("accepted") else 0
        for reason in (r.get("rejected_reasons") or []):
            s["reasons"][str(reason).split(" below ")[0].split(" >")[0][:34]] += 1
    data["strategies"] = sorted(
        ({"name": k, "n": v["n"], "acc": v["acc"], "rate": v["acc"] / v["n"] if v["n"] else 0.0,
          "top": (v["reasons"].most_common(1)[0][0] if v["reasons"] else "")}
         for k, v in per_strategy.items()), key=lambda d: -d["n"])[:8]

    # ---- header chips: read, not asserted -------------------------------------------------- #
    from trading_live_claude.config.settings import get_settings
    halted = state / "HALTED"
    data["kill_switch"] = {"halted": halted.exists(),
                           "reason": (halted.read_text(encoding="utf-8").strip()[:80]
                                      if halted.exists() else ""),
                           "threshold_pct": 100.0 * get_settings().max_drawdown_kill_switch}
    modes = [str(r.get("mode")) for r in orders if r.get("mode")]
    data["mode"] = modes[-1] if modes else None

    # ---- approvals, integrity, graph, OASIS ------------------------------------------------ #
    data["approvals"] = approvals(state / "approval.db")
    data["integrity"] = integrity_model(state, orders)
    data["graph"] = graph_model(state / "intel_graph.jsonl", known_sessions(state))
    data["oasis"] = oasis_model(state / "intel_agents.jsonl")
    data["oasis"]["sim"] = sim_runs(state / "oasis_runs.jsonl")
    return data


def integrity_model(state: Path, orders: list[dict[str, Any]]) -> dict[str, Any]:
    """What the record holds from signal to fill, and whether any of it is hash-chained.

    This repo journals to plain appends (orders, rejections, fills, the approval store). There is no
    hash-chained ledger under ``state/ledger/``, so the panel says that, rather than showing chain
    checks or a tamper matrix measured on some other record.
    """
    ledger_dir = state / "ledger"
    ledger_files = sorted(p.name for p in ledger_dir.glob("*.jsonl")) if ledger_dir.is_dir() else []
    rejected = read_jsonl(state / "rejected.jsonl")
    fills = read_jsonl(state / "paper_fills.jsonl")
    prompts: list[dict[str, Any]] = []
    if (state / "approval.db").exists():
        con = sqlite3.connect(f"file:{state / 'approval.db'}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        prompts = [dict(r) for r in con.execute(
            "SELECT issued_at, resolved_at, verdict, symbol, action, shares, broker FROM intents")]
        con.close()
    card_rej = sum(1 for r in rejected if any(str(x).startswith("card:") for x in r.get("reasons") or []))
    verdicts = collections.Counter(str(p["verdict"]) for p in prompts if p["verdict"])
    funnel = [
        ["PROMPTS PUT TO THE CARD", len(prompts), "approval.db"],
        ["CARD ACCEPT", verdicts["ACCEPT"], "approval.db"],
        ["CARD DECLINE", verdicts["DECLINE"], "approval.db"],
        ["EXPIRED UNANSWERED", verdicts["EXPIRED"], "approval.db"],
        ["GATE REFUSALS", len(rejected) - card_rej, "rejected.jsonl"],
        ["CARD REFUSALS", card_rej, "rejected.jsonl"],
        ["ORDERS ROUTED", len(orders), "orders.jsonl"],
        ["ORDERS ACCEPTED", sum(1 for r in orders if r.get("accepted")), "orders.jsonl"],
        ["FILLS", len(fills), "paper_fills.jsonl"],
    ]
    tape: list[tuple[str, str, str, str]] = []
    for p in prompts:
        tape.append((str(p["issued_at"]), "PROMPT_ISSUED", "",
                     f"{p['symbol']} {p['action']} {p['shares']} · {p['broker']}"))
        if p["verdict"] and p["resolved_at"]:
            cls = {"ACCEPT": "sig", "DECLINE": "rej"}.get(str(p["verdict"]), "")
            tape.append((str(p["resolved_at"]), str(p["verdict"]), cls, str(p["symbol"])))
    for r in rejected:
        tape.append((str(r.get("ts")), "REJECTED", "rej",
                     f"{r.get('symbol')} {'; '.join(str(x) for x in r.get('reasons') or [])}"[:60]))
    for r in orders:
        tape.append((str(r.get("ts")), "ORDER_ROUTED" if r.get("accepted") else "ORDER_REFUSED",
                     "" if r.get("accepted") else "rej", f"{r.get('symbol')} {r.get('action')} {r.get('shares')}"))
    for f in fills:
        tape.append((str(f.get("fill_time")), "FILLED", "fill",
                     f"{f.get('symbol')} {f.get('side')} {f.get('quantity')} @{float(f.get('price') or 0):,.4g}"))
    dated = sorted(((when, e, c, s) for t, e, c, s in tape if (when := _ts(t))),
                   key=lambda x: x[0], reverse=True)
    return {"ledger": {"present": bool(ledger_files), "files": ledger_files},
            "funnel": funnel, "tape_total": len(dated),
            "tape": [[when.astimezone(UTC).strftime("%m-%d %H:%M"), e, c, s]
                     for when, e, c, s in dated[:TAPE_ROWS]]}


def sim_runs(path: Path) -> list[dict[str, Any]]:
    """Ingested OASIS VM runs (``sim.ingest`` validated each against its seed's budget), newest first."""
    out = []
    for r in read_jsonl(path):
        with contextlib.suppress(KeyError, TypeError, ValueError):
            out.append({
                "run_id": str(r["run_id"]), "issued": r.get("issued_utc"), "platform": r.get("platform"),
                "model": r.get("model"), "steps": int(r["n_steps"]), "agents": int(r["n_agents"]),
                "spent": float(r["spent_usd"]), "max_usd": float(r["max_usd"]),
                "over_budget": bool(r.get("over_budget")), "stopped": r.get("stopped"),
                "briefing_items": r.get("briefing_items"),
                "stances": [{"symbol": s, "mean": v.get("stance"), "dispersion": v.get("dispersion"),
                             "agents": v.get("n")} for s, v in sorted((r.get("by_symbol") or {}).items())]})
    return out[-8:][::-1]


def oasis_model(path: Path) -> dict[str, Any]:
    """Summarise a specialist/adversary debate journal, if one exists.

    Runs made without an API key (``api_key_present`` false) carry no claims, so they are counted but
    kept out of the outcome tally. No claim -> trade -> P&L join exists, so nothing here says whether
    a claim was right.
    """
    out: dict[str, Any] = {"present": path.exists(), "runs": 0, "no_key_runs": 0, "scored_runs": 0,
                           "outcomes": {}, "recent": [], "first": None, "last": None}
    rows = [r for r in read_jsonl(path) if isinstance(r.get("outcomes"), dict)]
    out["runs"] = len(rows)
    if not rows:
        return out
    out["first"], out["last"] = rows[0].get("ts"), rows[-1].get("ts")
    tally: collections.Counter[str] = collections.Counter()
    for r in rows:
        if not r.get("api_key_present"):
            out["no_key_runs"] += 1
            continue
        out["scored_runs"] += 1
        for o in r["outcomes"].values():
            tally[str(o.get("outcome"))] += 1
    out["outcomes"] = dict(tally)
    for r in reversed([x for x in rows if x.get("api_key_present")][-12:]):
        items = []
        for dom, o in r["outcomes"].items():
            claim = o.get("claim") or {}
            crit = o.get("critique") or {}
            fired = o.get("fired") or {}
            items.append({"domain": dom, "outcome": o.get("outcome"),
                          "thesis": claim.get("thesis"), "direction": claim.get("direction"),
                          "claimed": claim.get("confidence"), "final": fired.get("confidence"),
                          "verdict": crit.get("verdict"), "reason": crit.get("reason"),
                          "inference": claim.get("inference"),
                          "evidence": claim.get("evidence") or []})
        out["recent"].append({"ts": r.get("ts"), "as_of": r.get("as_of"),
                              "evidence_items": r.get("evidence_items"),
                              "fired_count": r.get("fired_count"), "items": items})
    return out


def graph_model(path: Path, sessions: set[str]) -> dict[str, Any]:
    """Collapse the intel graph to its schema: node TYPES as vertices, predicates as edges.

    Drawing the instance is unreadable at any size; drawing the MODEL (which kinds of thing exist and
    how they connect) is legible. Every count is read from the journal.

    A ``traded`` edge whose ``meta.session_id`` no fill or equity journal knows did not come from a
    session this record holds, so it is left out and counted in ``excluded``.

    **Thickness is edge COUNT, never summed weight.** ``weight`` is not one quantity: for ``traded`` it
    is notional dollars, for ``observed`` a decayed observation score, and other predicates carry other
    units. Counts are commensurable; weights are reported per predicate and never pooled.
    """
    if not path.exists():
        return {"nodes": [], "links": [], "edges_total": 0, "source": "state/intel_graph.jsonl",
                "present": False, "excluded": 0}
    node_ids: dict[str, set[str]] = collections.defaultdict(set)
    pair_count: collections.Counter[tuple[str, str, str]] = collections.Counter()
    pair_weight: collections.Counter[tuple[str, str, str]] = collections.Counter()
    total = excluded = 0
    for e in read_jsonl(path):
        try:
            s_t, s_i = e["subject"]
            o_t, o_i = e["object"]
            pred = e["predicate"]
        except (KeyError, ValueError, TypeError):
            continue
        sid = (e.get("meta") or {}).get("session_id") if isinstance(e.get("meta"), dict) else None
        if pred == "traded" and sid is not None and str(sid) not in sessions:
            excluded += 1
            continue
        total += 1
        node_ids[s_t].add(str(s_i))
        node_ids[o_t].add(str(o_i))
        key = (s_t, pred, o_t)
        pair_count[key] += 1
        with contextlib.suppress(TypeError, ValueError):
            pair_weight[key] += float(e.get("weight") or 0.0)
    nodes = [{"type": t, "n": len(ids)} for t, ids in sorted(node_ids.items(), key=lambda kv: -len(kv[1]))]
    links = [{"from": k[0], "pred": k[1], "to": k[2], "n": n, "w": round(pair_weight[k], 2)}
             for k, n in sorted(pair_count.items(), key=lambda kv: -kv[1])]
    return {"nodes": nodes, "links": links, "edges_total": total,
            "node_total": sum(len(v) for v in node_ids.values()),
            "source": "state/intel_graph.jsonl", "present": True, "excluded": excluded}


def approvals(db: Path) -> dict[str, Any]:
    empty = {"total": 0, "verdicts": {}, "signed": 0, "unsigned_decided": 0, "cards": [],
             "symbols": [], "decided": [], "median_s": 0, "max_s": 0, "median_use": 0,
             "notional": 0, "expired_rows": 0, "expired_note": "", "last_prompt": None,
             "brokers": {}, "modes": [], "signature_column": False, "sim_only": False}
    if not db.exists():
        return empty
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    cols = [r[1] for r in con.execute("PRAGMA table_info(intents)")]
    rows = [dict(zip(cols, r, strict=True)) for r in con.execute("SELECT * FROM intents")]
    cards = list(con.execute("SELECT card_id, created_at, revoked_at FROM cards"))
    con.close()

    decided = []
    for r in rows:
        issued, resolved, expires = _ts(r.get("issued_at")), _ts(r.get("resolved_at")), _ts(r.get("expires_at"))
        if not (issued and resolved and expires) or str(r["verdict"]) not in ("ACCEPT", "DECLINE"):
            continue                          # see expired_note
        took, ttl = (resolved - issued).total_seconds(), (expires - issued).total_seconds()
        decided.append({"v": str(r["verdict"]), "s": round(took, 2), "ttl": round(ttl, 1),
                        "use": round(took / ttl, 4) if ttl else 0.0,
                        "sym": r["symbol"], "broker": r["broker"]})
    decided.sort(key=lambda d: d["s"])
    signed = sum(1 for r in rows if r.get("signature"))
    active = [c for c in cards if not c[2]]
    return {
        "total": len(rows),
        "verdicts": dict(collections.Counter(str(r["verdict"]) for r in rows if r["verdict"])),
        "signed": signed,
        "signature_column": "signature" in cols,
        "unsigned_decided": sum(1 for r in rows if r["verdict"] in ("ACCEPT", "DECLINE")) - signed,
        "brokers": dict(collections.Counter(str(r["broker"]) for r in rows)),
        "modes": sorted({str(r["mode"]) for r in rows if r.get("mode")}),
        "symbols": collections.Counter(str(r["symbol"]) for r in rows).most_common(6),
        "cards": [{"id": c[0], "created": str(c[1])[:16],
                   "revoked": (str(c[2])[:16] if c[2] else None)} for c in cards],
        "sim_only": bool(active) and all(str(c[0]).startswith("card-sim") for c in active),
        "decided": decided,
        "expired_rows": sum(1 for r in rows if str(r["verdict"]) == "EXPIRED"),
        "median_s": round(statistics.median([d["s"] for d in decided]), 2) if decided else 0,
        "max_s": round(max([d["s"] for d in decided]), 2) if decided else 0,
        "median_use": round(statistics.median([d["use"] for d in decided]), 4) if decided else 0,
        "notional": round(sum(float(r["notional_usd"] or 0)
                              for r in rows if r["verdict"] == "ACCEPT"), 0),
        "expired_note": ("EXPIRED rows carry resolved_at from the sweep that noticed them, not a "
                         "card decision, so they are excluded from response time."),
        "last_prompt": last_prompt(rows),
    }


def atlas(state: Path) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, str]]:
    """The Evidence Atlas catalogue, one build-time snapshot from the journals, and its provenance."""
    from trading_live_claude.execution.desk_feed import catalog_rows, meters_snapshot
    cat_id, meters = catalog_rows(ROOT)
    if not meters:
        return [], {}, {}
    snap = meters_snapshot(state, ROOT)
    snaps: dict[str, Any] = {}
    if snap["readings"]:
        snaps["build"] = {
            "label": f"BUILD · journals at {snap['captured_at'][:16].replace('T', ' ')} UTC",
            "notice": (f"BUILD-TIME SNAPSHOT · read from {state.as_posix()}/ when this page was built, "
                       f"{snap['captured_at'][:19]} UTC. Each reading carries its journal row's own "
                       "timestamp and turns STALE on this page's clock; served from a running "
                       "session, the RUNTIME option reads the same journals live."),
            "readings": snap["readings"]}
    pin = {"text": f"CATALOGUE {cat_id} · config/meter_catalog.csv — what the atlas defines. Every "
                   "reading on this tab comes from a snapshot of the journals, or from nothing."}
    return meters, snaps, pin


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--state-dir", type=Path, default=ROOT / "state")
    ap.add_argument("--template", type=Path, default=ROOT / "pwa" / "desk.template.html")
    ap.add_argument("--out", type=Path, nargs="+", default=[ROOT / "pwa" / "desk.html"])
    args = ap.parse_args()

    data = build(args.state_dir)
    data["meters"], data["meter_snapshots"], data["meters_pin"] = atlas(args.state_dir)
    data["built_at"] = datetime.now(UTC).isoformat()
    page = args.template.read_text(encoding="utf-8")
    if "__DATA__" not in page:
        print(f"[dash] {args.template} has no __DATA__ placeholder.")
        return 1
    # "</" inside a JSON string would close the <script> it is embedded in.
    page = page.replace("__DATA__", json.dumps(data, separators=(",", ":")).replace("</", "<\\/"))
    for out in args.out:
        out.write_text(page, encoding="utf-8")
        print(f"[dash] {out}  ({out.stat().st_size:,} bytes)")

    a, t, sc, g = data["approvals"], data["trades"], data["scope"], data["graph"]
    print(f"[dash] atlas: {len(data['meters'])} defined meters · snapshots: "
          f"{', '.join(data['meter_snapshots']) or 'none'}")
    print(f"[dash] {data['fill_count']} fills · {sc['sessions_used']} of {sc['sessions_seen']} "
          f"sessions fit to aggregate · {len(data['symbols'])} symbols")
    for reason, n in sc["excluded"]:
        print(f"[dash]   excluded {n:>3}  {reason}")
    if data["curve"]:
        print(f"[dash] curve {data['curve']['sid']}: {len(data['curve']['points'])} marks, "
              f"sortino/dd {data['curve_metrics']['sod']:.1f}, maxdd {data['curve_metrics']['maxdd']:.3%}")
    print(f"[dash] trades: {t['scope']}")
    print(f"[dash] approvals {a['total']}: {a['verdicts']} · median {a['median_s']}s")
    print(f"[dash] gate denials {data['rejection_total']} across {len(data['rejections'])} reasons")
    print(f"[dash] graph: {g['edges_total']} edges kept, {g.get('excluded', 0)} from unknown sessions left out")
    return 0


if __name__ == "__main__":
    sys.exit(main())
