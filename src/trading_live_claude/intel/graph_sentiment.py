"""Headline sentiment from a graph, with no model and no API.

Headlines and their words form a bipartite graph. A short seed lexicon fixes the polarity of some words, and
polarity spreads: a headline reads as the mean of its words, and a word outside the lexicon reads as the damped mean
of the headlines it appears in, less the corpus mean so that common words stay near zero. Same input, same output.

Plugs into :func:`~trading_live_claude.intel.news_sentiment.run_pass` as its ``call``, so its scores land in the same
journal and the same ``sentiment_on`` edges, told apart by ``model`` and ``prompt_version``.

What it cannot do, and no test here shows otherwise: it has not been validated against outcomes, it does not read
negation or sarcasm, and a headline naming several symbols gives each the same score at a lower confidence.
Not on the trading path.
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from trading_live_claude.intel.news_sentiment import CallRecord

GRAPH_MODEL = "graph-lexicon-1"
GRAPH_VERSION = "graph-sentiment-1"
ITERATIONS = 2
DAMPING = 0.5
MIN_WORD_HEADLINES = 3       # a word in fewer headlines than this has no evidence to spread
FULL_CONFIDENCE_HITS = 3.0

SEEDS: Mapping[str, float] = {
    **dict.fromkeys(("surge", "surges", "rally", "rallies", "gain", "gains", "jump", "jumps", "soar", "soars", "rise",
                     "rises", "recover", "recovers", "rebound", "rebounds", "record", "approval", "approved",
                     "inflows", "adoption", "upgrade", "beat", "beats", "launches", "partnership", "bullish"), 1.0),
    **dict.fromkeys(("plunge", "plunges", "tumble", "tumbles", "fall", "falls", "drop", "drops", "slump", "slumps",
                     "sink", "sinks", "crash", "selloff", "hack", "hacked", "breach", "theft", "stolen", "exploit",
                     "outage", "lawsuit", "sued", "fraud", "ban", "delist", "delisting", "outflows", "liquidated",
                     "liquidations", "suspends", "halts", "bearish"), -1.0),
}
_WORD = re.compile(r"[a-z][a-z'-]+")


def _words(title: str) -> set[str]:
    return set(_WORD.findall(title.lower()))


def propagate(titles: Sequence[str], *, seeds: Mapping[str, float] = SEEDS) -> list[tuple[float, int]]:
    """(score in [-1, 1], words that carried a polarity) per title, in order. Depends only on ``titles``."""
    docs = [_words(t) for t in titles]
    where: dict[str, list[int]] = {}
    for i, words in enumerate(docs):
        for w in words:
            where.setdefault(w, []).append(i)
    polarity = {w: seeds[w] for w in where if w in seeds}
    scores = [0.0] * len(docs)
    for _ in range(ITERATIONS + 1):
        for i, words in enumerate(docs):
            carried = [polarity[w] for w in words if w in polarity]
            scores[i] = sum(carried) / len(carried) if carried else 0.0
        centre = sum(scores) / len(scores) if scores else 0.0
        for w, docs_with in where.items():
            if w not in seeds and len(docs_with) >= MIN_WORD_HEADLINES:
                polarity[w] = DAMPING * (sum(scores[i] for i in docs_with) / len(docs_with) - centre)
    return [(max(-1.0, min(1.0, s)), sum(w in seeds for w in docs[i])) for i, s in enumerate(scores)]


def graph_caller(titles: Sequence[str]) -> Callable[..., tuple[str | None, CallRecord]]:
    """A ``run_pass`` caller over this corpus. Confidence comes from lexicon hits, shared among the symbols named."""
    corpus = sorted(set(titles))                    # the order headlines arrive in must not change a score
    read = dict(zip(corpus, propagate(corpus), strict=True))

    def call(*, user: str) -> tuple[str | None, CallRecord]:
        out = []
        for h in json.loads(user):
            score, hits = read[h["headline"]]
            confidence = min(1.0, hits / FULL_CONFIDENCE_HITS) / len(h["symbols"])
            out.extend({"id": h["id"], "symbol": s, "score": round(score, 6), "confidence": round(confidence, 6)}
                       for s in h["symbols"])
        return json.dumps({"scores": out}), CallRecord(model=GRAPH_MODEL, prompt_sha=GRAPH_VERSION)

    return call


def corpus_titles(news_path: str | Path) -> list[str]:
    rows: list[dict[str, Any]] = []
    for line in Path(news_path).read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return [str(r["title"]) for r in rows if isinstance(r, dict) and r.get("title")]


__all__ = ["GRAPH_MODEL", "GRAPH_VERSION", "SEEDS", "corpus_titles", "graph_caller", "propagate"]
