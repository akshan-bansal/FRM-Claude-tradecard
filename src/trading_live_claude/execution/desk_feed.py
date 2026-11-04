"""What the QuantPort.io desk panel reads from a running session, kept out of the HTTP layer.

Every function here reads this repository's own journals (``state/``) or the session's approval
store and returns what they hold. When a source is silent the field is null and named in
``placeholders``, never filled with a plausible number: a desk that showed a stand-in figure could
not be told apart from one showing a measurement.

The routes in :mod:`.approval_asgi` are thin wrappers over these, so the same rules hold for the live
panel and for the tests.
"""
from __future__ import annotations

import csv
import hashlib
import json
import socket
import sqlite3
import statistics
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
OVERLAY_MAX_AGE_S = 3600.0   # the live overlay refreshes every 15 min; an older row is not "current"
# The ATLAS tab's 120 meters, the ids audit.meters reports against. config/meter_catalog.csv belongs to
# scripts/atlas.py and reports/atlas.html; the page refuses a whole snapshot over an id it does not define.
DESK_CATALOG = Path("OS-InvestmentIntelligenceDashboard") / "meter_catalog.csv"


def _ts(raw: object) -> datetime | None:
    try:
        t = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def _jsonl(path: Path) -> Iterable[dict[str, Any]]:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                yield row


# --------------------------------------------------------------------------- live session strip


def session_marks(state_dir: Path, session_id: str) -> list[dict[str, str]]:
    path = Path(state_dir) / "paper_equity.csv"
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        return [r for r in csv.DictReader(fh) if r.get("session_id") == session_id]


def session_stats(store: Any, state_dir: Path | None, session_id: str | None,
                  started_at: datetime | None = None) -> dict[str, Any]:
    """The APPROVALS tab's live strip, in the ``StatsBody`` shape.

    Prompt counts come from ``state/approval.db`` (which carries ``issued_at``, so response time is
    measured, not asserted), scoped to prompts issued since ``started_at``: when the session wired its
    card. A shim that was not told (a standalone one) falls back to the session's first equity mark,
    which can miss a prompt issued before the first poll marked, and ``prompt_scope`` says which.
    Without either the counts cover the whole store.
    """
    placeholders: list[str] = ["overlay_scalar", "overlay_risk_zone"]
    marks = session_marks(state_dir, session_id) if state_dir and session_id else []
    since = started_at or (_ts(marks[0]["ts"]) if marks else None)
    scope = ("since this session started" if started_at else
             "since this session's first mark" if since else "whole approval store")
    out: dict[str, Any] = {"session_id": session_id if marks else None, "prompt_scope": scope}
    if marks:
        eq = [float(r["equity"]) for r in marks]
        out.update(starting_equity=eq[0], session_equity=eq[-1],
                   peak_equity=float(marks[-1]["peak_equity"]),
                   max_drawdown_pct=100.0 * max(float(r["drawdown_pct"]) for r in marks))
    else:
        placeholders += ["session_id", "starting_equity", "session_equity", "peak_equity",
                         "max_drawdown_pct"]
        out.update(starting_equity=None, session_equity=None, peak_equity=None, max_drawdown_pct=None)

    rows = _prompt_rows(Path(state_dir) / "approval.db") if state_dir else None
    if rows is None:  # no database: the in-memory store is all there is, and it keeps no issue time
        book = store.passbook(limit=10000, offset=0)
        verdicts = [e.verdict for e in book]
        took: list[float] = []
        placeholders.append("avg_ttl_response")
    else:
        if since:
            rows = [r for r in rows if (_ts(r["issued_at"]) or since) >= since]
        verdicts = [r["verdict"] for r in rows if r["verdict"]]
        # EXPIRED rows are left out: their resolved_at is when the sweep noticed, not a decision.
        took = []
        for r in rows:
            issued, resolved = _ts(r["issued_at"]), _ts(r["resolved_at"])
            if r["verdict"] in ("ACCEPT", "DECLINE") and issued and resolved:
                took.append((resolved - issued).total_seconds())
    acc, dec, exp = (verdicts.count(v) for v in ("ACCEPT", "DECLINE", "EXPIRED"))

    reasons = [r for r in _jsonl(Path(state_dir) / "rejected.jsonl")] if state_dir else []
    if since:
        reasons = [r for r in reasons if (_ts(r.get("ts")) or since) >= since]
    last = reasons[-1] if reasons else {}
    last_reason = "; ".join(str(x) for x in (last.get("reasons") or [])) or str(last.get("reason") or "")
    out.update(
        acceptance_rate=acc / (acc + dec) if acc + dec else 0.0,
        intents_total=len(verdicts), intents_approved=acc, intents_declined=dec, intents_expired=exp,
        intents_pending=len(store.pending()),
        avg_ttl_response=statistics.median(took) if took else None,
        gate_rejections=len(reasons), gate_scope=scope if since else "journal to date",
        last_gate_reason=last_reason, overlay_scalar=None, overlay_risk_zone=None,
        placeholders=placeholders)
    return out


def _prompt_rows(db: Path) -> list[dict[str, Any]] | None:
    if not db.exists():
        return None
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        con.row_factory = sqlite3.Row
        return [dict(r) for r in con.execute("SELECT issued_at, resolved_at, verdict FROM intents")]
    finally:
        con.close()


# --------------------------------------------------------------------------- atlas


def _bounds(raw: str) -> list[float] | None:
    """A meter's dial scale. An unparseable scale is no scale, not a guessed one."""
    try:
        lo, hi = json.loads(raw)
        return [float(lo), float(hi)]
    except (ValueError, TypeError):
        return None


def catalog_rows(root: Path = REPO_ROOT) -> tuple[str, list[dict[str, Any]]]:
    """The 120-meter catalogue in the shape the desk's ATLAS tab draws.

    It states what the code defines, so the file's value and status columns stay behind: a reading only
    reaches the page from a snapshot, carrying its own source, scope and timestamp.
    """
    path = root / DESK_CATALOG
    if not path.exists():
        return "", []
    text = path.read_text(encoding="utf-8")
    rows = [{"id": r["id"], "group": r["group"], "label": r["label"], "source": r["source"],
             "formula": r["formula"], "unit": r["unit"], "bounds": _bounds(r["bounds"]),
             "kind": r["kind"], "support": r["support"], "url": r["url"]}
            for r in csv.DictReader(text.splitlines())]
    return hashlib.sha256(text.encode()).hexdigest()[:12], sorted(rows, key=lambda r: r["id"])


def _newest_session(state_dir: Path) -> str:
    """The session of the last equity row, for a reader that was not told which book it is on."""
    path = state_dir / "paper_equity.csv"
    if not path.exists():
        return ""
    with path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    return (rows[-1].get("session_id") or "") if rows else ""


def meters_snapshot(state_dir: Path, root: Path = REPO_ROOT, *, session_id: str | None = None,
                    currency: str | None = None, now: datetime | None = None) -> dict[str, Any]:
    """An atlas snapshot taken from the journals now, in the desk's wire schema (schema_version 1).

    Each reading keeps the journal row's own timestamp, so the panel ages it to STALE on its clock.
    """
    from ..audit.meters import snapshot
    from ..config.settings import get_settings
    path = root / DESK_CATALOG
    if not path.exists():
        return {"schema_version": 1, "mode": "NO CATALOGUE", "readings": {},
                "notes": [f"no meter catalogue at {path}"]}
    state = Path(state_dir)
    return snapshot(state, session_id or _newest_session(state), currency or get_settings().account_currency,
                    now=now)


# --------------------------------------------------------------------------- IB access


def ib_access() -> dict[str, Any]:
    """TCP-probe the ports an IB login opens, measured now.

    A connect shows that something is listening, nothing more: not the right account, not market-data
    permissions, not the API enabled. No credential is read, sent or stored, and nothing is ordered.
    """
    from ..config.settings import get_settings
    cfg = get_settings()
    targets = [
        ("TWS paper (socket API)", cfg.ib_host, 7497, "paper_trading_workstation"),
        ("TWS live (socket API)", cfg.ib_host, 7496, "live_trading_workstation"),
        ("IB Gateway paper (socket API)", cfg.ib_host, 4002, "paper_gateway"),
        ("IB Gateway live (socket API)", cfg.ib_host, 4001, "live_gateway"),
        ("Client Portal Gateway (web API)", cfg.ib_web_host, cfg.ib_web_port, "client_portal"),
    ]
    rows = []
    for label, host, port, key in targets:
        try:
            with socket.create_connection((host, port), timeout=0.6):
                up = True
        except OSError:
            up = False
        rows.append({"key": key, "label": label, "host": host, "port": port, "listening": up})
    return {"checked_at": datetime.now().astimezone().isoformat(),
            "configured_socket_port": cfg.ib_paper_port if cfg.ib_use_paper else None, "ports": rows}


# --------------------------------------------------------------------------- baskets


def current_overlay(state_dir: Path, *, now: datetime | None = None) -> tuple[Any, dict[str, Any], str]:
    """(snapshot, decisions, note) from the newest overlay journal row, if it is current.

    A row older than ``OVERLAY_MAX_AGE_S`` is not the overlay as it stands, so it is not offered as
    one: the note says how old the newest row is instead.
    """
    from ..intel.overlay import IntelSnapshot
    rows = list(_jsonl(Path(state_dir) / "intel_overlay.jsonl"))
    if not rows:
        return None, {}, "no overlay journal"
    row, now = rows[-1], now or datetime.now(UTC)
    at = _ts(row.get("as_of"))
    if at is None or (now - at).total_seconds() > OVERLAY_MAX_AGE_S:
        age = f"{(now - at).total_seconds() / 3600:.1f}h old" if at else "undated"
        return None, {}, f"newest overlay row is {age}; not shown as the current overlay"
    try:
        return IntelSnapshot(**row["snapshot"]), row.get("decisions") or {}, ""
    except (KeyError, TypeError):
        return None, {}, "newest overlay row does not parse"


def analyse_basket(venue: str, symbols: list[str], state_dir: Path, root: Path = REPO_ROOT) -> dict[str, Any]:
    """Run the engines over the typed symbols. Daily closes come only from Kraken's public OHLC, for
    pairs the crypto sleeve maps; every other venue's symbols report no statistics."""
    import pandas as pd

    from ..analysis.basket_report import build_report
    from ..analysis.universe import CRYPTO_SLEEVE
    from ..data.kraken_ohlc import kraken_ohlc
    from ..intel.routing import classify_instrument
    from .basket import normalize
    syms = list(normalize(symbols))
    broker = {"qt": "questrade"}.get(venue, venue)
    closes: dict[str, Any] = {}
    fetch_errors: dict[str, str] = {}
    if venue == "kraken":
        for sym in syms:
            entry = CRYPTO_SLEEVE.get(sym)
            if entry is None:
                fetch_errors[sym] = "no Kraken pair mapping in the crypto sleeve"
                continue
            try:
                df = kraken_ohlc(entry.pair, interval=1440, timeout=10.0)
                closes[sym] = pd.Series(df["close"].astype(float).values)
            except Exception as e:  # network/API: report, never invent
                fetch_errors[sym] = f"Kraken OHLC failed: {e}"
    snapshot, decisions, overlay_note = current_overlay(state_dir)
    try:
        seed = json.loads((root / "config" / "basket_seed.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        seed = {}
    rep = build_report(venue, syms, closes=closes, snapshot=snapshot, overlay_decisions=decisions,
                       asset_class_of={s: classify_instrument(s, broker) for s in syms},
                       launch_map=seed.get("strategy_map", {}).get(venue),
                       fallback_strategy=seed.get("fallback_strategy", {}).get(venue))
    if overlay_note:
        rep["withheld"]["overlay"] = overlay_note
    rep["fetch_errors"] = fetch_errors
    rep["generated_at"] = datetime.now().astimezone().isoformat()
    return rep
