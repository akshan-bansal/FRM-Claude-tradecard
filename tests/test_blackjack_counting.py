"""Tests for the blackjack counting module."""

from __future__ import annotations

import random

import pytest

from blackjack.counting import (
    HI_LO,
    HI_OPT_I,
    KO,
    OMEGA_II,
    RANKS,
    SYSTEMS,
    CountingSystem,
    CountTracker,
    RunningCount,
    Shoe,
    bet_units,
    estimated_player_edge,
    index_play,
    take_insurance,
)


@pytest.mark.parametrize("system", [HI_LO, HI_OPT_I, OMEGA_II])
def test_balanced_systems_sum_to_zero_per_deck(system: CountingSystem) -> None:
    assert system.balanced
    assert system.full_deck_sum == 0


def test_ko_is_unbalanced_by_four_per_deck() -> None:
    assert not KO.balanced
    assert KO.full_deck_sum == 4


def test_systems_registry_has_every_system() -> None:
    assert set(SYSTEMS) == {"Hi-Lo", "Hi-Opt I", "Omega II", "KO"}


def test_system_rejects_missing_or_extra_ranks() -> None:
    tags = dict(HI_LO.tags)
    del tags["A"]
    with pytest.raises(ValueError, match="missing"):
        CountingSystem("bad", tags)
    tags = dict(HI_LO.tags)
    tags["1"] = 0
    with pytest.raises(ValueError, match="extra"):
        CountingSystem("bad", tags)


def test_hi_lo_tags() -> None:
    assert [HI_LO.tag(r) for r in RANKS] == [1, 1, 1, 1, 1, 0, 0, 0, -1, -1, -1, -1, -1]


def test_unknown_rank_raises() -> None:
    with pytest.raises(ValueError, match="unknown rank"):
        HI_LO.tag("1")


def test_running_count_accumulates() -> None:
    count = RunningCount(HI_LO)
    count.observe_many(["2", "5", "K", "A", "7"])  # +1 +1 -1 -1 0
    assert count.running == 0
    assert count.cards_seen == 5
    count.reset()
    assert count.running == 0 and count.cards_seen == 0


def test_true_count_divides_by_decks_remaining() -> None:
    count = RunningCount(HI_LO)
    count.running = 6
    assert count.true_count(cards_remaining=104) == pytest.approx(3.0)  # 2 decks


def test_true_count_floors_decks_at_half_deck() -> None:
    count = RunningCount(HI_LO)
    count.running = 2
    # 10 cards is 0.19 decks, floored to 0.5 decks: 2 / 0.5 = 4.
    assert count.true_count(cards_remaining=10) == pytest.approx(4.0)


def test_true_count_rejects_empty_shoe() -> None:
    with pytest.raises(ValueError):
        RunningCount(HI_LO).true_count(0)


@pytest.mark.parametrize(
    ("tc", "expected"),
    [(-3.0, 1), (1.9, 1), (2.0, 2), (3.7, 4), (4.0, 6), (5.0, 8), (9.0, 8)],
)
def test_bet_units_spread(tc: float, expected: int) -> None:
    assert bet_units(tc) == expected


def test_bet_units_respects_limits() -> None:
    assert bet_units(0.0, min_units=2) == 2
    assert bet_units(9.0, max_units=4) == 4
    with pytest.raises(ValueError):
        bet_units(0.0, min_units=0)


def test_estimated_edge_rule_of_thumb() -> None:
    assert estimated_player_edge(0.0) == pytest.approx(-0.005)
    assert estimated_player_edge(2.0) == pytest.approx(0.005)


def test_insurance_threshold() -> None:
    assert not take_insurance(2.9)
    assert take_insurance(3.0)


@pytest.mark.parametrize(
    ("total", "upcard", "tc", "expected"),
    [
        (16, "10", 0.0, "stand"),
        (16, "10", -0.5, None),
        (15, "10", 4.0, "stand"),
        (15, "10", 3.5, None),
        (12, "5", -2.0, "stand"),
        (12, "5", -1.0, None),
        (13, "2", -1.0, "stand"),
        (13, "2", 0.0, None),
        (9, "2", 1.0, "double"),
        (10, "A", 3.9, None),
        (10, "A", 4.0, "double"),
        (12, "3", 1.5, None),
        (12, "3", 2.0, "stand"),
    ],
)
def test_index_plays(total: int, upcard: str, tc: float, expected: str | None) -> None:
    assert index_play(total, upcard, tc) == expected


def test_index_play_unknown_pair_returns_none() -> None:
    assert index_play(7, "10", 10.0) is None


def test_shoe_composition() -> None:
    shoe = Shoe(num_decks=2, rng=random.Random(0))
    dealt = [shoe.draw() for _ in range(shoe.total_cards)]
    for rank in RANKS:
        assert dealt.count(rank) == 8
    with pytest.raises(RuntimeError):
        shoe.draw()


def test_shoe_rejects_bad_arguments() -> None:
    with pytest.raises(ValueError):
        Shoe(num_decks=0)
    with pytest.raises(ValueError):
        Shoe(penetration=0.0)


def test_shoe_needs_shuffle_at_penetration() -> None:
    shoe = Shoe(num_decks=1, penetration=0.5, rng=random.Random(1))
    for _ in range(25):
        shoe.draw()
    assert not shoe.needs_shuffle
    shoe.draw()
    assert shoe.needs_shuffle


def test_full_shoe_hi_lo_returns_to_zero() -> None:
    shoe = Shoe(num_decks=6, rng=random.Random(42))
    tracker = CountTracker(shoe, HI_LO)
    while shoe.cards_remaining:
        tracker.deal()
    assert tracker.count.running == 0


def test_tracker_resets_count_on_reshuffle() -> None:
    shoe = Shoe(num_decks=1, penetration=0.5, rng=random.Random(7))
    tracker = CountTracker(shoe, HI_LO)
    assert not tracker.reshuffle_if_needed()
    for _ in range(26):
        tracker.deal()
    assert tracker.reshuffle_if_needed()
    assert tracker.count.running == 0
    assert tracker.shoe.cards_remaining == 52
