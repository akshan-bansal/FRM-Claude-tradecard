"""Graph adapter: venue never read and residualised away, point in time, theme bridge, backbone, unknowns.

All inputs synthetic. No network.
"""
from __future__ import annotations

import ast
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from trading_live_claude.analysis import graph_adapter as ga
from trading_live_claude.analysis.feature_store import Instrument, Sources, build
from trading_live_claude.analysis.graph_adapter import (
    FEATURES,
    GkgArticle,
    GraphSources,
    backbone,
    graph_features,
    residualise,
)
from trading_live_claude.analysis.pit import TimeIndex
from trading_live_claude.news.gkg_themes import load_theme_map

CUT = datetime(2026, 6, 10, 20, tzinfo=UTC)
DOMS = ("conflict", "disaster", "economy", "energy", "military")
SYMS = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"]


def _art(at: datetime, guid: str, named: tuple[str, ...] = (), listed: tuple[str, ...] = (),
         domains: tuple[str, ...] | None = ()) -> tuple[datetime, GkgArticle]:
    return at, GkgArticle(guid, frozenset(named), frozenset(listed),
                          None if domains is None else frozenset(domains))


def _files(days: int, energy_today: float = 0.10) -> tuple[TimeIndex, TimeIndex]:
    """Hourly files for ``days`` days; energy share wobbles around 5% in the baseline, spikes today."""
    files, mapped = [], []
    for h in range(days * 24):
        at = CUT - timedelta(hours=h + 1) + timedelta(minutes=5)
        today = h < 24
        energy = energy_today if today else 0.05 + 0.005 * ((h // 24) % 3 - 1)
        counts = {d: 50 for d in DOMS} | {"energy": round(1000 * energy)}
        files.append((at, 1000))
        mapped.append((at, (1000, counts)))
    return TimeIndex(files), TimeIndex(mapped)


def _src(articles: list, own: list | None = None, edges: list | None = None, days: int = 35,
         energy_today: float = 0.10) -> GraphSources:
    files, mapped = _files(days, energy_today)
    return GraphSources(TimeIndex(articles), files, mapped, TimeIndex(own or []), TimeIndex(edges or []), DOMS)


def test_attention_share_is_a_share_of_title_mentions_known_before_the_cutoff() -> None:
    arts = [_art(CUT - timedelta(hours=2), "1", ("AAA",)), _art(CUT - timedelta(hours=3), "2", ("AAA",)),
            _art(CUT - timedelta(hours=4), "3", ("BBB",)), _art(CUT, "late", ("BBB",)),
            _art(CUT - timedelta(hours=5), "4", listed=("CCC",))]
    out = graph_features(_src(arts), CUT, SYMS)
    assert out["AAA"][0]["gx_att_share"] == round(2 / 3, 6)
    assert out["BBB"][0]["gx_att_share"] == round(1 / 3, 6)  # the article AT the cutoff is not known yet
    assert out["CCC"][0]["gx_att_share"] == 0.0  # only listed by GDELT, never named in a title


def test_stress_exposure_follows_the_symbol_into_a_surging_domain() -> None:
    arts = [_art(CUT - timedelta(hours=2), "1", ("AAA",), domains=("energy",)),
            _art(CUT - timedelta(hours=3), "2", ("BBB",), domains=("economy",))]
    out = graph_features(_src(arts), CUT, SYMS)
    assert out["AAA"][0]["gx_stress_exposure"] > 5  # energy is far above its 28-day baseline
    assert abs(out["BBB"][0]["gx_stress_exposure"]) < 1e-9  # economy is flat: nothing abnormal
    assert out["CCC"][0]["gx_stress_exposure"] is None  # no coverage of CCC: unknown, not zero


def test_rows_from_before_theme_capture_and_a_short_baseline_are_unknown() -> None:
    old = [_art(CUT - timedelta(hours=2), "1", ("AAA",), domains=None)]
    out = graph_features(_src(old), CUT, SYMS)
    assert out["AAA"][0]["gx_stress_exposure"] is None and out["AAA"][1]["themes"] == 0.0
    young = graph_features(_src([_art(CUT - timedelta(hours=2), "1", ("AAA",), domains=("energy",))], days=10),
                           CUT, SYMS)
    assert young["AAA"][0]["gx_stress_exposure"] is None  # under 22 baseline days


def test_without_gkg_coverage_every_feature_is_unknown() -> None:
    src = GraphSources(TimeIndex([]), TimeIndex([]), TimeIndex([]), TimeIndex([]), TimeIndex([]), DOMS)
    out = graph_features(src, CUT, SYMS)
    assert all(v is None for f, _ in out.values() for v in f.values())


BACKGROUND = ["CCC", "DDD", "EEE", "FFF"] * 10  # other symbols' coverage: what the null is measured against


def test_backbone_keeps_repeated_pairs_and_drops_chance_ones() -> None:
    week = [GkgArticle(str(i), frozenset({"AAA", "BBB"}), frozenset(), frozenset()) for i in range(6)]
    week += [GkgArticle(f"s{i}", frozenset({s}), frozenset(), frozenset()) for i, s in enumerate(BACKGROUND)]
    week += [GkgArticle("a1", frozenset({"AAA"}), frozenset(), frozenset()),
             GkgArticle("once", frozenset({"CCC", "DDD"}), frozenset(), frozenset())]
    nb = backbone(week, frozenset(SYMS))
    assert nb["AAA"] == {"BBB": 6} and "DDD" not in nb.get("CCC", {})  # a single co-mention is not a link


def test_a_pair_no_more_frequent_than_chance_is_not_a_link() -> None:
    # AAA and BBB each appear in every article: they co-occur exactly as often as independence predicts.
    week = [GkgArticle(str(i), frozenset({"AAA", "BBB"}), frozenset(), frozenset()) for i in range(10)]
    assert backbone(week, frozenset(SYMS)) == {}


def test_net_exposure_reads_a_neighbour_in_a_stressed_domain() -> None:
    arts = [_art(CUT - timedelta(hours=1, minutes=i), f"p{i}", ("AAA", "BBB")) for i in range(6)]
    arts += [_art(CUT - timedelta(hours=2), "e", ("BBB",), domains=("energy",))]
    arts += [_art(CUT - timedelta(hours=3, minutes=i), f"o{i}", (s,)) for i, s in enumerate(BACKGROUND)]
    out = graph_features(_src(arts), CUT, SYMS)
    assert out["AAA"][0]["gx_degree"] == 1.0
    assert out["AAA"][0]["gx_net_exposure"] is not None and out["AAA"][0]["gx_net_exposure"] > 0


def test_residualisation_removes_all_correlation_with_own_activity() -> None:
    values = {s: float(i) * 2 + (0.3 if i % 2 else -0.3) for i, s in enumerate(SYMS)}
    activity = {s: float(i) for i, s in enumerate(SYMS)}  # perfectly aligned with the feature's trend
    out, done = residualise(values, activity)
    xs, ys = [activity[s] for s in SYMS], [out[s] for s in SYMS]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)  # type: ignore[arg-type]
    assert done and abs(sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))) < 1e-9  # type: ignore[operator]
    assert abs(my - sum(values.values()) / len(values)) < 1e-9  # the level is kept


def test_no_activity_variation_means_nothing_to_project_out() -> None:
    values = {s: float(i) for i, s in enumerate(SYMS)}
    assert residualise(values, dict.fromkeys(SYMS, 0.0)) == (values, False)
    assert residualise({"AAA": 1.0, "BBB": None}, {"AAA": 3.0})[1] is False  # too small a cross-section


def _news() -> list:
    return [_art(CUT - timedelta(hours=1, minutes=i), f"n{i}", (s,), domains=("energy",))
            for i, s in enumerate(["AAA", "AAA", "AAA", "BBB", "BBB", "CCC", "DDD", "EEE", "FFF"])]


def test_venue_edges_are_never_read_as_features() -> None:
    """Equal trading on every symbol leaves nothing to project out, so the output must equal the output
    with no trading at all: the venue edges themselves contribute nothing."""
    plain = graph_features(_src(_news()), CUT, SYMS)
    own = [(CUT - timedelta(hours=k), s) for k in range(1, 6) for s in SYMS]
    edges = [(t, True) for t, _ in own] + [(CUT - timedelta(hours=k), False) for k in range(1, 11)]
    traded = graph_features(_src(_news(), own=own, edges=edges), CUT, SYMS)
    for s in SYMS:
        assert traded[s][0] == plain[s][0] and traded[s][1]["residualised"] is False
    assert traded["AAA"][1]["venue_share_raw"] == round(30 / 40, 4)  # reported: what was removed
    assert not any(f.startswith(("gx_own", "gx_venue")) for f in FEATURES)


def test_trading_concentrated_on_one_symbol_is_projected_out() -> None:
    """The book traded AAA, which is also the most-covered symbol. After residualisation no output
    feature moves with own activity across the cross-section."""
    own = [(CUT - timedelta(hours=k), "AAA") for k in range(1, 30)] + [(CUT - timedelta(hours=1), "BBB")]
    out = graph_features(_src(_news(), own=own), CUT, SYMS)
    act = [out[s][1]["own_activity_7d"] for s in SYMS]
    assert out["AAA"][1]["residualised"] is True
    for name in ("gx_att_share", "gx_stress_exposure"):
        ys = [out[s][0][name] for s in SYMS]
        ma, my = sum(act) / len(act), sum(ys) / len(ys)
        assert abs(sum((a - ma) * (y - my) for a, y in zip(act, ys, strict=True))) < 1e-4, name


def test_loading_reads_endogenous_edges_only_for_residualisation(tmp_path: Path) -> None:
    (tmp_path / "gdelt_gkg").mkdir()
    (tmp_path / "gdelt_gkg" / "2026-06.jsonl").write_text("\n".join(json.dumps(r) for r in [
        {"guid": "g1", "arrived_utc": "2026-06-10T18:00:00+00:00", "symbols": ["AAA"], "org_symbols": [],
         "themes": ["ENV_OIL", "TAX_FNCACT"]},
        {"guid": "g1", "arrived_utc": "2026-06-10T18:00:00+00:00", "symbols": ["AAA"], "org_symbols": []},
        {"guid": "g2", "arrived_utc": "2026-06-10T18:15:00+00:00", "symbols": [], "org_symbols": ["BBB"]},
    ]) + "\n", encoding="utf-8")
    (tmp_path / "gdelt_gkg" / "progress.jsonl").write_text(json.dumps(
        {"stamp": "20260610180000", "status": "ok", "rows": 900, "domain_counts": {"energy": 9},
         "theme_map": "stale-map"}) + "\n", encoding="utf-8")
    (tmp_path / "intel_graph.jsonl").write_text("\n".join(json.dumps(r) for r in [
        {"subject": ["venue", "kraken"], "predicate": "traded", "object": ["symbol", "AAA"], "as_of": "2026-06-10T17:00:00+00:00"},
        {"subject": ["poll", "p"], "predicate": "scaled", "object": ["symbol", "BBB"], "as_of": "2026-06-10T17:00:00+00:00"},
        {"subject": ["event", "e"], "predicate": "mentions", "object": ["symbol", "AAA"], "as_of": "2026-06-10T17:00:00+00:00"},
    ]) + "\n", encoding="utf-8")
    tm = load_theme_map(Path(__file__).resolve().parent.parent / "config" / "gkg_theme_domains.yaml")
    src = GraphSources.load(tmp_path, ["AAA", "BBB"], tm)
    assert [a.guid for a in src.articles.items] == ["g1", "g2"]  # duplicate record id dropped
    assert src.articles.items[0].domains == frozenset({"energy"}) and src.articles.items[1].domains is None
    assert sorted(src.own.items) == ["AAA", "BBB"] and src.edges.items == [True, True, False]
    assert len(src.files.items) == 1 and src.files_mapped.items == []  # built under another map: unknown


def test_the_feature_store_carries_the_gx_family_per_cross_section(tmp_path: Path) -> None:
    arts = [_art(CUT - timedelta(hours=2), "1", ("AAA",), domains=("energy",))]
    src = Sources(live={}, gkg={}, live_passes=TimeIndex([]), gkg_files=TimeIndex([]), overlay=TimeIndex([]),
                  graph_events=TimeIndex([]), graph=_src(arts))
    insts = [Instrument(s, "qt", "equity") for s in SYMS]
    rows = build(tmp_path / "store.jsonl", src, insts, [date(2026, 6, 10)], now=CUT + timedelta(minutes=5))
    aaa = next(r for r in rows if r["symbol"] == "AAA")
    assert set(FEATURES) <= set(aaa["features"]) and aaa["features"]["gx_att_share"] == 1.0
    assert aaa["coverage"]["gx"]["gkg"] == 1.0


def test_the_adapter_is_outside_the_trading_path() -> None:
    tree = ast.parse(Path(ga.__file__).read_text(encoding="utf-8"))
    mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    forbidden = ("execution", "risk", "brokers", "strategies", "monitor", "daemon", "tune", "cli")
    assert not {m for m in mods for f in forbidden if f".{f}" in f".{m}"}
