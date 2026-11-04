"""Graph sentiment: deterministic, no API, spreads only with evidence, and writes through the same pass as the LLM."""
from __future__ import annotations

import json
from pathlib import Path

from trading_live_claude.intel.graph import load_edges
from trading_live_claude.intel.graph_sentiment import (
    GRAPH_MODEL,
    GRAPH_VERSION,
    corpus_titles,
    graph_caller,
    propagate,
)
from trading_live_claude.intel.news_sentiment import run_pass


def test_the_lexicon_signs_a_headline_and_silence_is_zero() -> None:
    (up, up_hits), (down, down_hits), (flat, flat_hits) = propagate(
        ["Bitcoin surges after approval", "Exchange hack: funds stolen", "Committee meets on Tuesday"])
    assert up > 0 and down < 0 and flat == 0.0
    assert (up_hits, down_hits, flat_hits) == (2, 2, 0)


def test_a_word_spreads_only_with_enough_headlines() -> None:
    rare = propagate(["zcash hack", "zcash stolen", "zcash soon", "markets open"])[2]
    assert rare[0] < 0 and rare[1] == 0                      # three headlines carry "zcash" to the third
    assert propagate(["zcash hack", "zcash soon"])[1][0] == 0.0


def test_same_corpus_same_scores_in_any_order() -> None:
    titles = ["Bitcoin surges", "Ether falls", "Bitcoin and Ether trade", "Solana rallies"]
    a = json.dumps([{"id": "x", "symbols": ["S"], "headline": t} for t in titles])
    first = graph_caller(titles)(user=a)[0]
    assert first == graph_caller(list(reversed(titles)))(user=a)[0]


def test_it_writes_through_the_pass_without_any_key(tmp_path: Path) -> None:
    news = tmp_path / "news.jsonl"
    news.write_text(json.dumps({"source": "s", "content_hash": "h", "title": "Bitcoin hack, Ether falls",
                                "symbols": ["BTC/USD", "ETH/USD"], "arrived_utc": "2026-11-04T12:00:00+00:00"}) + "\n",
                    encoding="utf-8")
    paths = {"news_path": news, "scores_path": tmp_path / "s.jsonl", "graph_path": tmp_path / "g.jsonl"}
    r = run_pass(call=graph_caller(corpus_titles(news)), version=GRAPH_VERSION, **paths)
    assert r.scored == 2 and r.failed_batches == 0
    rows = [json.loads(x) for x in paths["scores_path"].read_text(encoding="utf-8").splitlines()]
    assert {x["model"] for x in rows} == {GRAPH_MODEL} and {x["prompt_version"] for x in rows} == {GRAPH_VERSION}
    assert rows[0]["score"] < 0 and rows[0]["confidence"] < 0.5   # shared between two symbols
    assert load_edges(paths["graph_path"])[0].meta["prompt_version"] == GRAPH_VERSION
    # A rerun has nothing left.
    assert run_pass(call=graph_caller(corpus_titles(news)), version=GRAPH_VERSION, **paths).scored == 0
