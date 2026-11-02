"""``trading audit ...``: verify, trace, replay, reconcile, export and anchor the audit record.

Every command reads ``state/ledger`` and writes only to the ledger's shared ``control`` / ``recon``
streams, to ``reports/``, to the destination it is given, or to a git tag. None of them place,
cancel or change an order.
"""
from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

console = Console()
audit_app = typer.Typer(help="Audit trail: verify, trace, replay, reconcile, export, anchor.")
REPO = Path(__file__).resolve().parents[3]


def _state() -> Path:
    from ..config import get_settings
    return Path(get_settings().state_dir)


def _ledger_dir() -> Path:
    return _state() / "ledger"


def _print_json(obj: Any) -> None:
    console.print_json(json.dumps(obj, default=str))


@audit_app.command("verify")
def verify(stream: str = typer.Option("", help="Verify one stream instead of all."),
           tags: bool = typer.Option(False, help="Also check every anchored day against its git tag."),
           ) -> None:
    """Recompute every hash chain, re-verify every card signature in the ledger, check anchors."""
    from ..execution.approval import verify_audit_record
    from .anchor import anchored_days, verify_day
    from .ledger import read_stream, verify_chain
    from .projection import streams

    found = streams(_ledger_dir())
    if stream:
        found = {k: v for k, v in found.items() if k == stream}
    if not found:
        console.print(f"[yellow]no ledger streams under {_ledger_dir()}[/yellow]")
        raise typer.Exit(code=0 if not stream else 1)
    failed = 0
    t = Table("stream", "rows", "files", "chain", "signatures")
    for name, paths in sorted(found.items()):
        rows = [r for p in paths for r in read_stream(p)]
        ok, reason = verify_chain(rows)
        sig_ok = sig_bad = 0
        for r in rows:
            if r.get("event") != "SIGNED":
                continue
            p = r.get("payload") or {}
            good, _why = verify_audit_record({"canonical": p.get("canonical"),
                                              "signature": r.get("signature"),
                                              "pubkey_pem": p.get("pubkey_pem"),
                                              "signature_alg": p.get("signature_alg")})
            sig_ok += good
            sig_bad += not good
        failed += (not ok) + (sig_bad > 0)
        t.add_row(name, str(len(rows)), str(len(paths)), "ok" if ok else f"[red]{reason}[/red]",
                  f"{sig_ok} ok" + (f", [red]{sig_bad} FAIL[/red]" if sig_bad else ""))
    console.print(t)
    if tags:
        for day in sorted(anchored_days(_state())):
            ok, reason = verify_day(_ledger_dir(), REPO, date.fromisoformat(day))
            failed += not ok
            console.print(f"anchor {day}: " + ("ok" if ok else f"[red]{reason}[/red]"))
    if failed:
        console.print(f"[red]{failed} check(s) failed: the record has been altered or is incomplete.[/red]")
        raise typer.Exit(code=1)


@audit_app.command("trace")
def trace(intent_id: str) -> None:
    """Every row for one intent, signal to execution, across streams."""
    from .queries import all_rows
    rows = sorted((r for r in all_rows(_ledger_dir()) if r.get("intent_id") == intent_id),
                  key=lambda r: str(r.get("ts")))
    if not rows:
        console.print(f"[yellow]no rows for intent {intent_id}[/yellow]")
        raise typer.Exit(code=1)
    t = Table("ts", "stream", "seq", "event", "detail")
    for r in rows:
        p = r.get("payload") or {}
        detail = ", ".join(f"{k}={p[k]}" for k in ("symbol", "action", "side", "shares", "quantity",
                                                   "price", "verdict", "rejected_reasons", "error",
                                                   "kind") if p.get(k) not in (None, "", []))
        t.add_row(str(r.get("ts"))[:23], str(r.get("stream")), str(r.get("seq")),
                  str(r.get("event")), detail[:120])
    console.print(t)


@audit_app.command("replay")
def replay_cmd(session: str = typer.Option("", help="Only this session id."),
               show_matches: bool = typer.Option(False, help="List matching rows too.")) -> None:
    """Re-run every recorded gate decision from its recorded inputs; exit 1 on any difference."""
    from .queries import all_rows
    from .replay import replay
    results = replay(all_rows(_ledger_dir()), session_id=session or None)
    counts: dict[str, int] = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
        if r["status"] != "match" or show_matches:
            _print_json(r)
    console.print(counts or "no gate decisions recorded")
    if counts.get("differs"):
        raise typer.Exit(code=1)


@audit_app.command("rebuild")
def rebuild_cmd() -> None:
    """Rebuild the disposable SQLite projection at state/ledger_projection.db."""
    from .projection import rebuild
    summary = rebuild(_ledger_dir(), _state() / "ledger_projection.db")
    _print_json({k: v for k, v in summary.items() if k != "streams"})
    if not summary["chains_ok"]:
        raise typer.Exit(code=1)


@audit_app.command("recon")
def recon_cmd(venue: str = typer.Argument(..., help="questrade | kraken | ib"),
              days: int = typer.Option(7, help="Look back this many days."),
              grace: float = typer.Option(900.0, help="Seconds before a silent order is a break."),
              ) -> None:
    """Pull executions from a live broker (read-only) and reconcile them into the ledger."""
    from ..config import get_settings
    from .ledger import Ledger
    from .recon import reconcile
    settings = get_settings()
    if venue == "questrade":
        from ..cli import _make_questrade
        broker: Any = _make_questrade(settings)
        account = settings.questrade_account_number or broker.accounts()[0].number
    elif venue == "kraken":
        from ..brokers.kraken import KrakenBroker
        broker = KrakenBroker(api_key=settings.kraken_api_key or "",
                              api_secret=settings.kraken_api_secret or "", enable_live_orders=False)
        account = "kraken"
    elif venue == "ib":
        from ..brokers.ib import IBBroker
        broker = IBBroker(host=settings.ib_host, port=settings.ib_paper_port,
                          client_id=settings.ib_client_id)
        account = getattr(settings, "ib_account", "") or ""
    else:
        console.print("[red]venue must be questrade, kraken or ib[/red]")
        raise typer.Exit(code=2)
    writer = Ledger(_ledger_dir(), stream="recon", mode="recon", shared=True)
    s = reconcile(writer, broker, account, since=datetime.now(UTC) - timedelta(days=days),
                  grace_s=grace)
    _print_json({"fetched": s.fetched, "reported": s.reported, "breaks": s.breaks,
                 "error": s.error})
    if s.error or s.breaks:
        raise typer.Exit(code=1)


@audit_app.command("fx-fetch")
def fx_fetch(currency: str = typer.Option("USD"), start: str = typer.Option(...),
             end: str = typer.Option("")) -> None:
    """Download Bank of Canada daily rates into state/fx (explicit; exports never fetch)."""
    from .semantics import BankOfCanadaFX
    added = BankOfCanadaFX(_state() / "fx").fetch(
        currency, date.fromisoformat(start), date.fromisoformat(end) if end else date.today())
    console.print(f"{added} new {currency} observations cached under {_state() / 'fx'}")


@audit_app.command("export")
def export_cmd(year: int = typer.Option(..., help="Tax year."),
               out: Path = typer.Option(Path("reports"), help="Directory for the report."),
               affiliated: Path | None = typer.Option(
                   None, help="CSV of executions in a spouse's or registered account "
                              "(account,symbol,side,quantity,time[,venue]) for the "
                              "superficial-loss test.")) -> None:
    """Tax-year export in CAD: ACB, gains, superficial-loss flags. Records its own hash."""
    from .queries import export_year
    res = export_year(_ledger_dir(), _state(), year, out, affiliated=affiliated)
    _print_json(res)
    if res["unknown"]:
        console.print(f"[yellow]{res['unknown']} row(s) have facts that could not be determined "
                      "(see unknown_reasons; often a missing FX rate: run `trading audit fx-fetch`).[/yellow]")


@audit_app.command("query")
def query_cmd(name: str = typer.Argument(..., help="superficial | approved-vs-executed | replay | "
                                                   "fast-cancels | by-version | breaks | fill-quality | "
                                                   "session-plans"),
              arg: str = typer.Option("", help="by-version: the version; fast-cancels: seconds."),
              affiliated: Path | None = typer.Option(None)) -> None:
    """Run a named audit query and print its rows as JSON."""
    from . import queries as q
    from .replay import replay
    from .semantics import BankOfCanadaFX
    rows = q.all_rows(_ledger_dir())
    if name == "superficial":
        res: Any = q.superficial(rows, BankOfCanadaFX(_state() / "fx"),
                                 affiliated=q.load_affiliated(affiliated))
    elif name == "approved-vs-executed":
        res = q.approved_vs_executed(rows)
    elif name == "replay":
        res = [r for r in replay(rows) if r["status"] != "match"]
    elif name == "fast-cancels":
        res = q.fast_cancels(rows, float(arg or 5))
    elif name == "by-version":
        if not arg:
            console.print("[red]by-version needs --arg VERSION[/red]")
            raise typer.Exit(code=2)
        res = q.by_version(rows, arg)
    elif name == "breaks":
        res = q.breaks(rows)
    elif name == "fill-quality":
        res = q.fill_quality(rows)
    elif name == "session-plans":
        res = q.session_plans(rows)
    else:
        console.print(f"[red]unknown query {name!r}; one of {', '.join(q.QUERIES)}[/red]")
        raise typer.Exit(code=2)
    _print_json(res)


@audit_app.command("anchor")
def anchor_cmd(day: str = typer.Option("", help="UTC day to anchor (default: every closed, "
                                                 "un-anchored day)."),
               sign: bool = typer.Option(True, "--sign/--no-sign",
                                         help="Sign the tag (git's key, or --signing-key)."),
               signing_key: str = typer.Option("", help="Key to sign with; with --ssh, the "
                                                        "public key file."),
               ssh: bool = typer.Option(False, help="Sign with an SSH key instead of GPG."),
               push: bool = typer.Option(True, "--push/--no-push", help="Push the tag."),
               remote: str = typer.Option("origin")) -> None:
    """Write each closed day's Merkle root into a signed git tag and push it off this machine."""
    from .anchor import anchored_days, day_anchor, ledger_days, push_tag, record_anchor, write_tag
    today = datetime.now(UTC).date()
    done = anchored_days(_state())
    days = [date.fromisoformat(day)] if day else [
        d for d in ledger_days(_ledger_dir()) if d < today and d.isoformat() not in done]
    if not days:
        console.print("nothing to anchor: every closed day already has a tag")
        return
    for d in days:
        if d >= today:
            console.print(f"[yellow]{d} is still open; its later rows would not be covered.[/yellow]")
        anchor = day_anchor(_ledger_dir(), d)
        tag = write_tag(REPO, anchor, sign=sign, signing_key=signing_key or None, ssh=ssh)
        if push:
            push_tag(REPO, tag["tag"], remote=remote)
        record_anchor(_state(), anchor, tag)
        console.print(f"{tag['tag']}  root {anchor['root'][:16]}…  {anchor['leaves']} rows  "
                      f"{'signed' if sign else '[yellow]UNSIGNED[/yellow]'}"
                      f"{'  pushed to ' + remote if push else ''}")


@audit_app.command("archive")
def archive_cmd(dest: Path = typer.Argument(..., help="Retention destination, outside state/.")) -> None:
    """Copy the closed audit record to DEST with a verified SHA-256 manifest."""
    from .retention import archive
    res = archive(_state(), dest)
    _print_json(res)
    if res["copy_mismatches"]:
        raise typer.Exit(code=1)


@audit_app.command("reference")
def reference_cmd(kind: str = typer.Option(..., help="terms | registration | adviser | other"),
                  file: Path = typer.Option(..., help="The document; only its hash is recorded."),
                  note: str = typer.Option("", help="What it is, e.g. 'Kraken API terms v2026-07'."),
                  ) -> None:
    """Record evidence (a terms version, a registration check, advice) by hash and date."""
    from hashlib import sha256

    from .ledger import Ledger
    digest = sha256(file.read_bytes()).hexdigest()
    Ledger(_ledger_dir(), stream="control", shared=True).append("EXTERNAL_REFERENCE", {
        "kind": kind, "file": file.name, "sha256": digest, "note": note,
        "recorded_at": datetime.now(UTC).isoformat()})
    console.print(f"recorded {kind} {file.name} sha256 {digest[:16]}…")
