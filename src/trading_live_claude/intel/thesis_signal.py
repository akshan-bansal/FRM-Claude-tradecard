"""Thesis signals in shadow: what a thesis would have said, recorded before the outcome is known.

**Nothing here acts.** A signal is a row in the hash-chained ledger, stream ``thesis``. No order, gate or runner
reads it, this module imports nothing from the order path, and :class:`ThesisSignal` refuses any stage but
``shadow``. It builds the record the promotion gate will judge (``NEXT_SESSION.md`` section 17, phase 5). The chain
is why the ledger holds it: a signal cannot be quietly added or edited after the outcome is known. A pass run
for a past moment is marked ``replay`` and must never count as forward evidence.

Rule theses (:func:`~trading_live_claude.intel.interpret.interpret`) name themes and never say which way.
  :data:`THEME_DIRECTIONS` states it, one entry per thesis and theme, drafted from each thesis's own wording for the
  user to review. Where a thesis says it is not a directional call, the entry is ``unsigned`` and logs as ``watch``,
  which can never be promoted. Every row carries :data:`DIRECTION_TABLE_VERSION`, so a later change stays visible.

An adverse read yields ``veto`` and ``exit``; a constructive one yields ``confirm`` and ``entry``. Phase 5 judges each
action separately, with ``entry`` held to the strictest bar. Anything about disasters yields nothing, by the user's
decision: the desk does not seek to profit on them. ``strength`` is uncalibrated; ``inputs`` keeps what it came from.
"""
from __future__ import annotations

import hashlib
from collections.abc import Collection, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from trading_live_claude.audit.ledger import Ledger
from trading_live_claude.config.settings import get_settings
from trading_live_claude.intel.confluence import confluence
from trading_live_claude.intel.graph_store import GraphStore, stamp_to_us
from trading_live_claude.intel.history import default_overlay_journal
from trading_live_claude.intel.interpret import THEME_EXEMPLARS, Thesis, interpret
from trading_live_claude.intel.overlay import IntelSnapshot
from trading_live_claude.intel.routing import classify_symbol
from trading_live_claude.intel.thesis_intensity import intensity, load_history
from trading_live_claude.logging_setup import get_logger

log = get_logger(__name__)

Action = Literal["confirm", "veto", "exit", "entry", "watch"]
Sign = Literal["adverse", "constructive", "unsigned"]

SHADOW = "shadow"
LEDGER_STREAM = "thesis"
DEFAULT_HORIZON_DAYS = 5
MAX_SNAPSHOT_AGE_HOURS = 6.0
NULL_THESIS = "No notable configuration"
_BAND_WEIGHT = {"high": 1.0, "moderate": 0.6, "tentative": 0.3}
_ACTIONS: Mapping[str, tuple[Action, ...]] = {"adverse": ("veto", "exit"), "constructive": ("confirm", "entry"),
                                              "unsigned": ("watch",), "neutral": ("watch",)}

DIRECTION_TABLE_VERSION = "draft-2026-11-04"
# (thesis, theme) -> what the thesis's own wording implies for that theme's names. Draft: awaiting the user's review.
THEME_DIRECTIONS: Mapping[tuple[str, str], Sign] = {
    # "Favour cheap convexity": protection is cheap while the driver of risk is rising.
    ("Complacency divergence", "volatility_convexity"): "constructive",
    ("Complacency divergence", "safe_haven"): "constructive",
    # "in both directions": supply disruption helps producers, demand destruction hurts consumers.
    ("Energy event concentration", "energy"): "unsigned",
    ("Energy event concentration", "materials"): "unsigned",
    # "as a hedge on escalation, not as a directional bet".
    ("Conflict escalation watch", "defense_geopolitical"): "unsigned",
    ("Conflict escalation watch", "safe_haven"): "unsigned",
    # Stretched greed "thins the buffer against bad news": own the protection.
    ("Sentiment stretch — greed", "volatility_convexity"): "constructive",
    # "producers positioned for physical scarcity", though it also says not to size on this alone.
    ("Commodity carry-inversion proxy", "energy"): "constructive",
    ("Commodity carry-inversion proxy", "materials"): "constructive",
    # "Historically the risk asset gives first": "Prefer trimming into commodity / EM strength".
    ("Dollar strength divergence — risk-on into a strong USD", "dollar"): "constructive",
    ("Dollar strength divergence — risk-on into a strong USD", "materials"): "adverse",
    ("Dollar strength divergence — risk-on into a strong USD", "emerging_markets"): "adverse",
    # "the driver is demand destruction": it reads the dollar's fall as a fact, not a call on it.
    ("Dollar weakness divergence — demand rather than currency", "dollar"): "unsigned",
    ("Dollar weakness divergence — demand rather than currency", "materials"): "adverse",
}


def _excluded(name: str) -> bool:
    return "disaster" in name.lower()


@dataclass(frozen=True)
class ThesisSignal:
    """One thing a thesis would have said about one symbol. A record, never an instruction."""

    signal_id: str
    thesis: str
    origin: Literal["rule", "agent"]
    symbol: str
    action: Action
    direction: str
    strength: float
    as_of: str
    expires_at: str
    horizon_days: int
    symbol_source: Literal["theme", "cited_event"]
    evidence_ids: tuple[str, ...] = ()
    evidence_hash: str = ""
    direction_table: str = ""
    replay: bool = False
    stage: str = SHADOW
    inputs: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.stage != SHADOW:
            raise ValueError(f"thesis signals are shadow only, got stage {self.stage!r}")
        if not 0.0 <= self.strength <= 1.0:
            raise ValueError(f"strength {self.strength} is outside [0, 1]")


def _signal_id(thesis: str, symbol: str, action: str, as_of: str) -> str:
    return hashlib.sha256(f"{thesis}|{symbol}|{action}|{as_of}".encode()).hexdigest()[:16]


def _build(*, thesis: str, origin: Literal["rule", "agent"], symbol: str, sign: str, strength: float, as_of: datetime,
           horizon_days: int, source: Literal["theme", "cited_event"], replay: bool, table: str = "",
           evidence: Sequence[str] = (), evidence_hash: str = "",
           inputs: Mapping[str, Any] | None = None) -> list[ThesisSignal]:
    stamp = as_of.isoformat()
    expires = (as_of + timedelta(days=horizon_days)).isoformat()
    return [ThesisSignal(signal_id=_signal_id(thesis, symbol, action, stamp), thesis=thesis, origin=origin, symbol=symbol,
                         action=action, direction=sign, strength=round(float(strength), 4), as_of=stamp,
                         expires_at=expires, horizon_days=horizon_days, symbol_source=source,
                         evidence_ids=tuple(evidence), evidence_hash=evidence_hash, direction_table=table,
                         replay=replay, inputs=dict(inputs or {}))
            for action in _ACTIONS[sign]]


def thesis_to_signals(thesis: Thesis, *, as_of: datetime, snapshot: IntelSnapshot | None = None,
                      history: Sequence[tuple[datetime, IntelSnapshot]] = (), universe: Collection[str] | None = None,
                      horizon_days: int = DEFAULT_HORIZON_DAYS, replay: bool = False) -> list[ThesisSignal]:
    """A rule thesis's signals, one set per exemplar of each theme. A theme the table does not list is unsigned."""
    if thesis.name == NULL_THESIS or _excluded(thesis.name):
        return []
    graded = intensity(thesis.name, snapshot, history) if snapshot is not None else None
    band = _BAND_WEIGHT.get(thesis.confidence, 0.3)
    strength = band * (graded.intensity if graded is not None else 1.0)
    scores = confluence(snapshot) if snapshot is not None else {}
    out: dict[str, ThesisSignal] = {}
    for theme in thesis.themes:
        sign = THEME_DIRECTIONS.get((thesis.name, theme), "unsigned")
        for symbol in THEME_EXEMPLARS.get(theme, ()):
            if universe is not None and symbol not in universe:
                continue
            asset_class = classify_symbol(symbol)
            score = scores.get(asset_class)
            inputs = {"theme": theme, "band": thesis.confidence, "asset_class": asset_class,
                      "intensity": None if graded is None else round(graded.intensity, 4),
                      "confluence_score": None if score is None else round(score.score, 2),
                      "confluence_stance": None if score is None else score.stance}
            for sig in _build(thesis=thesis.name, origin="rule", symbol=symbol, sign=sign, strength=strength, as_of=as_of,
                              horizon_days=horizon_days, source="theme", replay=replay, table=DIRECTION_TABLE_VERSION,
                              inputs=inputs):
                out.setdefault(sig.signal_id, sig)      # a symbol two themes share is one signal, the first theme's
    return list(out.values())


class ThesisBook:
    """The shadow record: rows in the ledger's ``thesis`` stream, one per signal. It only writes and reads that stream."""

    def __init__(self, ledger_dir: str | Path | None = None) -> None:
        root = Path(ledger_dir) if ledger_dir is not None else Path(get_settings().state_dir) / "ledger"
        self._ledger = Ledger(root, stream=LEDGER_STREAM, mode=SHADOW, shared=True)

    def _payloads(self) -> list[dict[str, Any]]:
        return [dict(r["payload"], recorded_at=r.get("ts")) for r in self._ledger.rows() if r.get("event") == "THESIS_SIGNAL"]

    def record(self, signals: Sequence[ThesisSignal]) -> int:
        """Append signals not already held. Returns how many were new."""
        held = {p.get("signal_id") for p in self._payloads()}
        added = 0
        for sig in signals:
            if sig.signal_id in held:
                continue
            body = asdict(sig)
            body["evidence_ids"] = list(sig.evidence_ids)
            self._ledger.append("THESIS_SIGNAL", body)
            held.add(sig.signal_id)
            added += 1
        return added

    def signals(self, *, before: datetime | None = None, include_replay: bool = False) -> list[dict[str, Any]]:
        """Recorded signals as of ``before`` (strictly), oldest first. Replays only when asked for."""
        bound = stamp_to_us(before.isoformat()) if before is not None else None
        out = []
        for p in self._payloads():
            at = stamp_to_us(str(p.get("as_of") or ""))
            if at is None or (bound is not None and at >= bound):
                continue
            if p.get("replay") and not include_replay:
                continue
            out.append(p)
        return sorted(out, key=lambda p: stamp_to_us(str(p["as_of"])) or 0)

    def open_at(self, when: datetime, *, include_replay: bool = False) -> list[dict[str, Any]]:
        """Signals made before ``when`` and not yet expired at it."""
        now = stamp_to_us(when.isoformat()) or 0
        return [p for p in self.signals(before=when, include_replay=include_replay)
                if (stamp_to_us(str(p.get("expires_at") or "")) or 0) > now]

    def verify(self) -> tuple[bool, str]:
        return self._ledger.verify()


@dataclass(frozen=True)
class ShadowPass:
    """One pass: what it recorded, and in plain words what it could not do and why."""

    as_of: str
    replay: bool
    signals: tuple[ThesisSignal, ...]
    recorded: int
    snapshot_as_of: str | None
    notes: tuple[str, ...]


def latest_snapshot(cutoff: datetime, path: str | Path | None = None, *, max_age_hours: float = MAX_SNAPSHOT_AGE_HOURS
                    ) -> tuple[tuple[datetime, IntelSnapshot] | None, list[tuple[datetime, IntelSnapshot]], str]:
    """The newest real snapshot strictly before ``cutoff``, the history behind it, and why not if there is none."""
    journal = path if path is not None else default_overlay_journal()
    rows = [r for r in load_history(journal, now=cutoff) if r[0] < cutoff]
    if not rows:
        return None, [], "no real snapshot in the overlay journal before this time"
    newest = rows[-1]
    age = (cutoff - newest[0]).total_seconds() / 3600.0
    if age > max_age_hours:
        return None, rows, f"newest real snapshot is {age:.1f}h old, over the {max_age_hours:.0f}h limit"
    return newest, rows, ""


def run_shadow_pass(store: GraphStore, book: ThesisBook, *, cutoff: datetime | None = None,
                    universe: Collection[str] | None = None,
                    overlay_journal: str | Path | None = None, graph_journal: str | Path | None = None,
                    horizon_days: int = DEFAULT_HORIZON_DAYS) -> ShadowPass:
    """Read what is known, work out what theses would say, and record it. Touches no order path.

    ``cutoff`` runs the pass for a past moment and marks every signal ``replay``. A stale feed records nothing rather than dressing old news as today's.
    """
    replay = cutoff is not None
    at = cutoff if cutoff is not None else datetime.now().astimezone()
    store.sync_journal(graph_journal)
    notes: list[str] = []
    signals: list[ThesisSignal] = []
    current, history, why = latest_snapshot(at, overlay_journal)
    snapshot_as_of = None
    if current is None:
        notes.append(f"rule theses skipped: {why}")
    else:
        snapshot_as_of = current[0].isoformat()
        theses = interpret(current[1])
        for t in theses:
            signals += thesis_to_signals(t, as_of=at, snapshot=current[1], history=history, universe=universe,
                                         horizon_days=horizon_days, replay=replay)
    recorded = book.record(signals)
    log.info("thesis_shadow.pass", as_of=at.isoformat(), replay=replay, signals=len(signals), recorded=recorded)
    return ShadowPass(as_of=at.isoformat(), replay=replay, signals=tuple(signals), recorded=recorded,
                      snapshot_as_of=snapshot_as_of, notes=tuple(notes))
