"""Graph adapter: read the OSINT graph into per-symbol risk-lens features, with venue influence removed.

User direction (2026-10-30): read the OSINT graph and re-project it so the signal behind investor actions is
the intelligence, not the venue, and venue influence is minimal. Both design choices were option B:

* **Venue excluded AND residualised.** Edges that record the book's own activity (``traded``: a venue filled
  an order; ``scaled``: the router shrank an intent) and anything touching a ``venue`` node are never read
  as features. Each output feature is then residualised, per cutoff and across the symbol cross-section,
  against the symbol's own recent activity, so even indirect co-movement with where and what we traded is
  stripped. (Known cost, accepted: when news drives both our trades and the risk, part of a real signal goes
  with it.) Every row reports the share of the raw graph that was endogenous, i.e. what was removed.
* **Bridge through GDELT's own theme codes**, via the frozen ``config/gkg_theme_domains.yaml``.

Features (prefix ``gx_``), all from records strictly before the cutoff, one GDELT crawl clock throughout:

* ``gx_att_share``       the symbol's share of all basket title mentions in the last 24 h. A share, not a
                         count, so global news-volume swings cancel (the Hype Index, arXiv:2506.06329).
* ``gx_stress_exposure`` sum over domains of (the symbol's 24 h article share in the domain) x (the domain's
                         abnormal global intensity: z-score of today's share of ALL GKG articles against
                         the prior 28 days). Theme-built media-risk indices as in arXiv:2606.19972.
* ``gx_net_exposure``    co-mention spillover: neighbours' stress exposure, weighted by 7-day co-mention
                         counts over a noise-filtered network (co-occurrence network risk, arXiv:1706.05812;
                         the filter keeps a pair only if it co-occurs more than a degree-preserving binomial
                         null predicts, in the spirit of the noise-corrected backbone, arXiv:1701.07336).
* ``gx_degree``          number of backbone neighbours.

Why remove the venue: much of short-horizon price action is endogenous to trading itself (arXiv:1009.2928),
and returns a book's own flow inflates look like signal until the price pressure reverts (arXiv:2405.12768).
A lens that reads our own fills back would learn that "what we trade is risky". No weights are learned here;
under label scarcity, learned fusion weights were found unreliable (arXiv:2607.23682).

Contract, as for ``analysis.feature_store``: a ruler, outside the trading path. Unknown is None, never 0.
"""
from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from trading_live_claude.analysis.pit import TimeIndex, coverage_hours, parse_ts, read_jsonl
from trading_live_claude.news.gkg_themes import ThemeMap

ENDOGENOUS_PREDICATES = frozenset({"traded", "scaled"})  # the book's own activity
ENDOGENOUS_NODES = frozenset({"venue"})
FEATURES = ("gx_att_share", "gx_stress_exposure", "gx_net_exposure", "gx_degree")

WINDOW = timedelta(days=1)
NET_WINDOW = timedelta(days=7)
OWN_WINDOW = timedelta(days=7)
BASELINE_DAYS = 28
MIN_BASELINE_DAYS = 22  # ~80% of the baseline must be covered before a z-score is reported
MIN_COVERAGE = 0.5  # share of the window's hours with a GKG file
Z_BACKBONE = 1.645  # one-sided 95% against the binomial co-mention null
MIN_COMENTIONS = 2
MIN_CROSS_SECTION = 5  # symbols with a known value before a residualisation is attempted


@dataclass(frozen=True)
class GkgArticle:
    guid: str
    named: frozenset[str]  # symbols the title names
    listed: frozenset[str]  # symbols only in GDELT's organisation / name lists
    domains: frozenset[str] | None  # None: the row predates theme capture

    @property
    def symbols(self) -> frozenset[str]:
        return self.named | self.listed


@dataclass
class GraphSources:
    articles: TimeIndex  # GkgArticle, de-duplicated by GKG record id
    files: TimeIndex  # every fetched GKG file: total articles
    files_mapped: TimeIndex  # files with domain counts under THIS theme map: (total, counts)
    own: TimeIndex  # symbol, one per endogenous edge on a symbol: used ONLY to residualise
    edges: TimeIndex  # bool per graph-journal edge: endogenous or not, to report what was removed
    domains: tuple[str, ...]
    _intensity: dict[datetime, dict[str, float] | None] = field(default_factory=dict)

    @classmethod
    def load(cls, state: Path, symbols: Iterable[str], theme_map: ThemeMap) -> GraphSources:
        syms = frozenset(symbols)
        gkg = state / "gdelt_gkg"
        seen: set[str] = set()
        arts: list[tuple[datetime, GkgArticle]] = []
        for p in sorted(gkg.glob("*.jsonl")):
            if p.name.endswith("progress.jsonl"):
                continue
            for r in read_jsonl(p):
                at, guid = parse_ts(r.get("arrived_utc")), str(r.get("guid", ""))
                named = frozenset(r.get("symbols") or ()) & syms
                listed = (frozenset(r.get("org_symbols") or ()) & syms) - named
                if at is None or not guid or guid in seen or not (named or listed):
                    continue
                seen.add(guid)
                themes = r.get("themes")
                arts.append((at, GkgArticle(guid, named, listed,
                                            theme_map.domains_for(themes) if themes is not None else None)))
        files, mapped = [], []
        for p in sorted(gkg.glob("*progress.jsonl")):
            for r in read_jsonl(p):
                if r.get("status") != "ok":
                    continue
                try:
                    at = datetime.strptime(str(r["stamp"]), "%Y%m%d%H%M%S").replace(tzinfo=UTC)
                except (KeyError, ValueError):
                    continue
                files.append((at, int(r.get("rows", 0))))
                if r.get("theme_map") == theme_map.id and isinstance(r.get("domain_counts"), dict):
                    mapped.append((at, (int(r.get("rows", 0)), dict(r["domain_counts"]))))
        own, edges = [], []
        for r in read_jsonl(state / "intel_graph.jsonl"):
            at = parse_ts(r.get("as_of"))
            if at is None:
                continue
            subj, obj = r.get("subject") or ["", ""], r.get("object") or ["", ""]
            endo = (r.get("predicate") in ENDOGENOUS_PREDICATES
                    or subj[0] in ENDOGENOUS_NODES or obj[0] in ENDOGENOUS_NODES)
            edges.append((at, endo))
            if endo and obj[0] == "symbol":
                own.append((at, str(obj[1])))
        return cls(TimeIndex(arts), TimeIndex(files), TimeIndex(mapped), TimeIndex(own), TimeIndex(edges),
                   theme_map.domains)


# --------------------------------------------------------------------------- pieces


def _intensity(src: GraphSources, end: datetime) -> dict[str, float] | None:
    """Each domain's share of ALL GKG articles in the 24 h before ``end``, or None if under-covered."""
    if end in src._intensity:
        return src._intensity[end]
    out: dict[str, float] | None = None
    if coverage_hours(src.files_mapped, end - WINDOW, end) >= MIN_COVERAGE:
        files = src.files_mapped.window(end - WINDOW, end)
        total = sum(t for t, _ in files)
        if total > 0:
            out = {d: sum(int(c.get(d, 0)) for _, c in files) / total for d in src.domains}
    src._intensity[end] = out
    return out


def domain_z(src: GraphSources, cutoff: datetime) -> dict[str, float | None] | None:
    """Abnormal domain intensity: today's share against the prior ``BASELINE_DAYS`` daily windows.

    None when today or the baseline is under-covered. Per domain, a flat baseline that today matches is
    0.0 (nothing abnormal); a flat baseline that today departs from has no scale and is None.
    """
    now = _intensity(src, cutoff)
    if now is None:
        return None
    base = [b for k in range(1, BASELINE_DAYS + 1) if (b := _intensity(src, cutoff - WINDOW * k)) is not None]
    if len(base) < MIN_BASELINE_DAYS:
        return None
    z: dict[str, float | None] = {}
    for d in src.domains:
        xs = [b[d] for b in base]
        mu = sum(xs) / len(xs)
        sd = math.sqrt(sum((x - mu) ** 2 for x in xs) / (len(xs) - 1))
        if sd > 1e-12:
            z[d] = (now[d] - mu) / sd
        else:
            z[d] = 0.0 if abs(now[d] - mu) <= 1e-12 else None
    return z


def backbone(articles: Sequence[GkgArticle], symbols: frozenset[str]) -> dict[str, dict[str, int]]:
    """Co-mention pairs that co-occur more than a degree-preserving binomial null predicts.

    Under the null two symbols land in an article independently, each at its own rate, so a pair's count is
    Binomial(N, p_i p_j). A pair is kept when it beats the null by ``Z_BACKBONE`` standard deviations and has
    at least ``MIN_COMENTIONS`` articles: loose GDELT organisation matches produce many one-off pairs.
    """
    sets = [s for a in articles if (s := a.symbols & symbols)]
    n = len(sets)
    deg: Counter[str] = Counter(x for s in sets for x in s)
    pairs: Counter[tuple[str, str]] = Counter(
        (a, b) for s in sets if len(s) > 1 for i, a in enumerate(sorted(s)) for b in sorted(s)[i + 1:])
    out: dict[str, dict[str, int]] = defaultdict(dict)
    for (a, b), k in pairs.items():
        p = (deg[a] / n) * (deg[b] / n)
        var = n * p * (1 - p)
        if k >= MIN_COMENTIONS and var > 0 and (k - n * p) / math.sqrt(var) > Z_BACKBONE:
            out[a][b] = out[b][a] = k
    return out


def residualise(values: Mapping[str, float | None], activity: Mapping[str, float]) -> tuple[dict[str, float | None], bool]:
    """Remove the cross-sectional slope on own activity, keeping the level: f - beta (a - mean a).

    Not attempted (values returned as-is, flag False) when fewer than ``MIN_CROSS_SECTION`` symbols have a
    value or own activity does not vary across them: there is then nothing to project out.
    """
    known = [(s, v) for s, v in values.items() if v is not None]
    if len(known) < MIN_CROSS_SECTION:
        return dict(values), False
    a = [float(activity.get(s, 0.0)) for s, _ in known]
    f = [v for _, v in known]
    ma, mf = sum(a) / len(a), sum(f) / len(f)
    var = sum((x - ma) ** 2 for x in a)
    if var <= 0:
        return dict(values), False
    beta = sum((x - ma) * (y - mf) for x, y in zip(a, f, strict=True)) / var
    out = dict(values)
    for (s, v), x in zip(known, a, strict=True):
        out[s] = v - beta * (x - ma)
    return out, True


def venue_share(src: GraphSources, cutoff: datetime) -> float | None:
    """Share of graph-journal edges in the last 7 days that were the book's own activity (removed here)."""
    flags = src.edges.window(cutoff - OWN_WINDOW, cutoff)
    return round(sum(flags) / len(flags), 4) if flags else None


# --------------------------------------------------------------------------- the adapter


def graph_features(src: GraphSources, cutoff: datetime,
                   symbols: Iterable[str]) -> dict[str, tuple[dict[str, Any], dict[str, Any]]]:
    """(features, coverage) per symbol at one cutoff, over the cross-section ``symbols``."""
    syms = frozenset(symbols)
    cov = coverage_hours(src.files, cutoff - WINDOW, cutoff)
    vshare = venue_share(src, cutoff)
    own = Counter(src.own.window(cutoff - OWN_WINDOW, cutoff))

    def _cov(s: str, *, themes: float | None, resid: bool) -> dict[str, Any]:
        return {"gkg": cov, "themes": themes, "venue_share_raw": vshare, "residualised": resid,
                "own_activity_7d": own.get(s, 0)}

    if cov < MIN_COVERAGE:
        return {s: (dict.fromkeys(FEATURES), _cov(s, themes=None, resid=False)) for s in syms}

    day: list[GkgArticle] = src.articles.window(cutoff - WINDOW, cutoff)
    named = Counter(x for a in day for x in a.named & syms)
    total_named = sum(named.values())
    z = domain_z(src, cutoff)

    stress: dict[str, float | None] = {}
    theme_cov: dict[str, float | None] = {}
    for s in syms:
        mine = [a for a in day if s in a.symbols]
        tagged = [a for a in mine if a.domains is not None]
        theme_cov[s] = round(len(tagged) / len(mine), 4) if mine else None
        if z is None or not tagged:
            stress[s] = None
            continue
        shares = {d: sum(d in a.domains for a in tagged) / len(tagged) for d in src.domains}  # type: ignore[operator]
        if any(sh > 0 and z[d] is None for d, sh in shares.items()):
            stress[s] = None  # the symbol sits in a domain whose abnormality cannot be scaled today
            continue
        stress[s] = sum(sh * z[d] for d, sh in shares.items() if sh > 0)  # type: ignore[operator]

    nbrs = backbone(src.articles.window(cutoff - NET_WINDOW, cutoff), syms)
    net: dict[str, float | None] = {}
    for s in syms:
        known = [(w, stress[j]) for j, w in nbrs.get(s, {}).items() if stress.get(j) is not None]
        wsum = sum(w for w, _ in known)
        net[s] = sum(w * v for w, v in known) / wsum if wsum else None  # type: ignore[operator]

    raw: dict[str, dict[str, float | None]] = {
        "gx_att_share": {s: (named[s] / total_named) if total_named else None for s in syms},
        "gx_stress_exposure": stress,
        "gx_net_exposure": net,
        "gx_degree": {s: float(len(nbrs.get(s, {}))) for s in syms},
    }
    activity = {s: float(own.get(s, 0)) for s in syms}
    done: dict[str, bool] = {}
    for name, vals in raw.items():
        raw[name], done[name] = residualise(vals, activity)
    any_resid = any(done.values())
    return {s: ({name: (None if raw[name][s] is None else round(float(raw[name][s]), 6))  # type: ignore[arg-type]
                 for name in FEATURES},
                _cov(s, themes=theme_cov[s], resid=any_resid))
            for s in syms}
