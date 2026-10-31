"""Asset-class integrity across the plug-ins: every per-class table covers every overlay class, and every
basket instrument resolves to the same class in every module that classifies it.

Written 2026-10-30 after the audit found GC/SI/MGC/SIL read as equity by the live overlay while the feature
store, the asset spec and the card thesis called them futures. Synthetic and config-only; no network.
"""
from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from trading_live_claude.analysis import calibration
from trading_live_claude.analysis import feature_store as fs
from trading_live_claude.analysis.asset_spec import _FUTURES_REGISTRY, FutureSpec, spec_for
from trading_live_claude.analysis.feature_store import (
    Article,
    Instrument,
    Sources,
    TimeIndex,
    build,
    universe,
)
from trading_live_claude.atlas import load_catalog, take_snapshot
from trading_live_claude.brokers.models import OrderAction
from trading_live_claude.data.futures_calendar import ROOT_CALENDARS
from trading_live_claude.execution.router import OrderIntent
from trading_live_claude.intel import confluence, routing
from trading_live_claude.intel.overlay import (
    OVERLAY_CLASSES,
    IntelSnapshot,
    OverlayDecision,
    RiskOverlay,
)
from trading_live_claude.intel.routing import (
    OverlayProvider,
    classify_instrument,
    classify_symbol,
    futures_overrides,
)
from trading_live_claude.intel.vs_engine import VSInvestmentEngine, asset_class_for
from trading_live_claude.news.runner import load_config

ROOT = Path(__file__).resolve().parent.parent
BASKET = universe(ROOT / "config" / "basket_seed.json")
IB_ROOTS = [i.symbol for i in BASKET if i.venue == "ib"]
BROKER = {"qt": "questrade", "ib": "ib", "kraken": "kraken"}  # basket venue -> broker name on the card


# --------------------------------------------------------------------------- every table covers every class


@pytest.mark.parametrize("name, table", [
    ("overlay decisions", RiskOverlay().evaluate(IntelSnapshot())),
    ("confluence attention weights", confluence.ATTENTION),
    ("persistence-gate domains", routing._CLASS_TO_DOMAINS),
    ("calibration defaults", calibration._CLASS_DEFAULTS),
])
def test_every_per_class_table_covers_every_overlay_class(name: str, table: dict[str, Any]) -> None:
    assert set(table) == set(OVERLAY_CLASSES), name


def test_the_atlas_shows_an_overlay_scalar_for_every_class(tmp_path: Path) -> None:
    cat_id, meters = load_catalog(ROOT / "config" / "meter_catalog.csv")
    now = datetime(2026, 10, 30, 12, tzinfo=UTC)
    row = {"as_of": (now - timedelta(minutes=5)).isoformat(), "snapshot": {"source_age_hours": {"news": 1.0}},
           "decisions": {c: {"scalar": 0.5 + i / 20} for i, c in enumerate(OVERLAY_CLASSES)}}
    (tmp_path / "intel_overlay.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    got = {r["id"]: r["value"] for r in take_snapshot(tmp_path, ROOT, meters, cat_id, now=now)["readings"]}
    assert {f"OV.{c.upper()}": 0.5 + i / 20 for i, c in enumerate(OVERLAY_CLASSES)}.items() <= got.items()


# --------------------------------------------------------------------------- one class per instrument


def test_every_basket_instrument_has_one_class_in_every_module() -> None:
    for i in BASKET:
        broker = BROKER[i.venue]
        seen = {"feature store": i.asset_class, "resolver": classify_instrument(i.symbol, broker),
                "asset spec": spec_for(i.symbol).asset_class, "card thesis": asset_class_for(broker, i.symbol)}
        assert len(set(seen.values())) == 1, f"{i.symbol}: {seen}"


def test_every_ib_root_is_a_registered_future_with_contract_specs() -> None:
    assert IB_ROOTS == ["GC", "MGC", "SI", "SIL"]
    for r in IB_ROOTS:
        assert r in ROOT_CALENDARS, r
        spec = _FUTURES_REGISTRY[r]
        assert isinstance(spec, FutureSpec) and spec.exchange_group == "COMEX" and spec.tick_value > 0
        assert calibration.profile_for(r) == calibration._refine(calibration._CLASS_DEFAULTS["future"], spec)


def test_the_paper_sessions_give_futures_roots_the_futures_overlay() -> None:
    # Why the overrides exist: by ticker alone every root reads as a stock (SIL is also a miners ETF).
    assert {classify_symbol(r) for r in IB_ROOTS} == {"equity"}
    snap = IntelSnapshot(strategic_risk=82.0, energy_stress=0.7)
    provider = OverlayProvider(lambda: snap, class_overrides=futures_overrides(IB_ROOTS), journal=False)
    for r in IB_ROOTS:
        decision = provider(r)
        assert decision is not None and decision.asset_class == "future", r
        assert classify_symbol(r, futures_overrides(IB_ROOTS)) == "future"  # what PersistenceGate reads
    assert provider("XIC.TO").asset_class == "equity"  # an override covers only the roots it names


@pytest.mark.parametrize("script, ctor", [("paper_ib.py", "OverlayProvider"), ("sweep_ib.py", "PersistenceGate")])
def test_the_ib_scripts_pass_their_futures_roots_to_the_class_lookup(script: str, ctor: str) -> None:
    import ast

    tree = ast.parse((ROOT / "scripts" / script).read_text(encoding="utf-8"))
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "id", None) == ctor]
    assert calls and all(any(k.arg == "class_overrides" and isinstance(k.value, ast.Call)
                             and getattr(k.value.func, "id", None) == "futures_overrides" for k in c.keywords)
                         for c in calls), f"{script}: every {ctor}(...) needs class_overrides=futures_overrides(...)"


def test_the_resolver_lets_the_venue_settle_what_the_ticker_cannot() -> None:
    assert classify_instrument("GC", "ib") == "future" and classify_instrument("GC", "questrade") == "equity"
    assert classify_instrument("XIC.TO", "ib") == "equity"  # an IB stock is not a future
    assert classify_instrument("BTC/USD", "kraken") == "crypto" and classify_instrument("CGL.TO", "qt") == "precious_metals"
    assert classify_instrument("GC", "ib", {"GC": "commodity"}) == "commodity"  # explicit overrides win


# --------------------------------------------------------------------------- the card names the right class


def _intent(symbol: str) -> OrderIntent:
    return OrderIntent(symbol=symbol, action=OrderAction.BUY, shares=1, entry=100.0, stop=95.0, target=110.0,
                       strategy="bollinger", risk_dollars=5.0, account_number="paper-001", symbolId=1)


@pytest.mark.parametrize("broker, symbol, cls", [
    ("questrade", "CGL.TO", "precious_metals"), ("questrade", "DBC", "commodity"), ("questrade", "NVDA", "equity"),
    ("ib", "GC", "future"), ("ib", "SIL", "future"), ("kraken", "BTC/USD", "crypto"),
])
def test_the_card_thesis_reports_the_symbols_own_class(tmp_path: Path, broker: str, symbol: str, cls: str) -> None:
    # Every class at its own distinct scalar, so the clause can only have come from the right one.
    decisions = [OverlayDecision(c, 0.40 + i / 100, False, [], {}) for i, c in enumerate(OVERLAY_CLASSES)]
    want = next(d for d in decisions if d.asset_class == cls)
    thesis, _ = VSInvestmentEngine(writeup_dir=tmp_path).explain(
        _intent(symbol), broker=broker, overlay_snapshot=IntelSnapshot(), overlay_decisions=decisions)
    assert f"{cls} scalar {want.scalar:.2f}" in thesis, thesis
    assert not [c for c in OVERLAY_CLASSES if c != cls and f"{c} scalar" in thesis]


# --------------------------------------------------------------------------- micros read the parent's news


def test_every_basket_symbol_reads_news_through_symbols_that_have_aliases() -> None:
    aliases = load_config(ROOT / "config" / "news_sources.yaml", None).aliases
    assert {i.symbol: i.news_from for i in BASKET if i.news_from} == {"MGC": "GC", "SIL": "SI"}
    for i in BASKET:
        assert aliases.get(i.news_symbol), f"{i.symbol} reads news as {i.news_symbol}, which has no aliases"
    assert {"GC", "SI"} <= set(fs.news_symbols([i for i in BASKET if i.news_from]))  # parents get loaded


DAY = date(2026, 6, 10)
CUT = datetime(2026, 6, 10, 20, tzinfo=UTC)
GC = Instrument("GC", "ib", "future")
MGC = Instrument("MGC", "ib", "future", news_from="GC")


def _src(**kw: Any) -> Sources:
    hourly = TimeIndex((CUT - timedelta(days=40) + timedelta(hours=h, minutes=5), None) for h in range(42 * 24))
    base: dict[str, Any] = dict(live={}, gkg={}, live_passes=hourly, gkg_files=TimeIndex([]), overlay=TimeIndex([]),
                                graph_events=TimeIndex([]), graph_since=None)
    base.update(kw)
    return Sources(**base)


def test_a_micro_takes_its_parents_mention_counts_and_says_so(tmp_path: Path) -> None:
    gold = TimeIndex([(CUT - timedelta(hours=h), Article(CUT - timedelta(hours=h), f"gold {h}", "wire", True))
                      for h in (1, 2, 3)])
    rows = {r["symbol"]: r for r in build(tmp_path / "s.jsonl", _src(live={"GC": gold}), [GC, MGC], [DAY],
                                          now=CUT + timedelta(minutes=5))}
    assert rows["MGC"]["features"]["live_title_24h"] == rows["GC"]["features"]["live_title_24h"] == 3
    assert rows["MGC"]["coverage"]["news_from"] == "GC" and "news_from" not in rows["GC"]["coverage"]


def test_a_micro_stays_out_of_the_graph_cross_section(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[list[str]] = []

    def fake(graph: object, cutoff: datetime, symbols: list[str]) -> dict[str, tuple[dict, dict]]:
        asked.append(list(symbols))
        return {s: ({"gx_att_share": 0.25}, {"gkg": 0.9}) for s in symbols}

    monkeypatch.setattr(fs, "graph_features", fake)
    rows = {r["symbol"]: r for r in build(tmp_path / "s.jsonl", _src(graph=object()), [GC, MGC], [DAY],
                                          now=CUT + timedelta(minutes=5))}
    assert asked == [["GC"]]  # no zero-mention MGC row in the backbone or the residualisation
    assert rows["MGC"]["features"]["gx_att_share"] == 0.25
    assert rows["MGC"]["coverage"]["gx"] == {"gkg": 0.9, "inherited_from": "GC"}
