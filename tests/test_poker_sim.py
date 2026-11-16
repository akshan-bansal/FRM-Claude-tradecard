"""Tests for the offline No-Limit hold'em simulator."""

from __future__ import annotations

import random

import pytest

from poker.bots import CallingStation, EquityBot
from poker.cards import (
    FLUSH,
    FULL_HOUSE,
    HIGH_CARD,
    PAIR,
    STRAIGHT,
    STRAIGHT_FLUSH,
    TRIPS,
    TWO_PAIR,
    Deck,
    card,
    evaluate,
)
from poker.engine import Action, Seat, Snapshot, Table, build_pots


def hand(*cards: str) -> tuple[int, ...]:
    return tuple(card(c) for c in cards)


def test_card_parse_round_trip() -> None:
    assert card("As") == 12 * 4 + 3
    with pytest.raises(ValueError):
        card("Zz")


@pytest.mark.parametrize(
    ("cards", "category"),
    [
        (("As", "Ks", "Qs", "Js", "Ts", "2d", "3c"), STRAIGHT_FLUSH),
        (("Ah", "2d", "3c", "4h", "5s", "Kd", "9c"), STRAIGHT),  # the wheel
        (("Ah", "Ad", "Ac", "Kh", "Kd", "2c", "3c"), FULL_HOUSE),
        (("2h", "7h", "9h", "Jh", "Kh", "Ac", "3s"), FLUSH),
        (("Ah", "Ad", "Ac", "Kh", "Qd", "2c", "3c"), TRIPS),
        (("Ah", "Ad", "Kc", "Kh", "Qd", "2c", "3c"), TWO_PAIR),
        (("Ah", "Ad", "Kc", "5h", "Qd", "2c", "3s"), PAIR),
        (("Ah", "Jd", "Kc", "5h", "9d", "2c", "3s"), HIGH_CARD),
    ],
)
def test_category(cards: tuple[str, ...], category: int) -> None:
    assert evaluate(hand(*cards))[0] == category


def test_wheel_is_lower_than_six_high_straight() -> None:
    wheel = evaluate(hand("Ah", "2d", "3c", "4h", "5s"))
    six_high = evaluate(hand("2h", "3d", "4c", "5h", "6s"))
    assert six_high > wheel


def test_kickers_decide_pairs() -> None:
    a = evaluate(hand("Ah", "Ad", "Kc", "5h", "3d"))
    b = evaluate(hand("Ac", "As", "Qc", "5s", "3s"))
    assert a > b


def test_full_house_beats_flush() -> None:
    fh = evaluate(hand("2h", "2d", "2c", "9h", "9d"))
    flush = evaluate(hand("Ah", "Kh", "Qh", "Jh", "3h"))
    assert fh > flush


def test_build_pots_three_levels() -> None:
    pots = build_pots({0: 100, 1: 50, 2: 200}, folded=set())
    assert pots == [(150, {0, 1, 2}), (100, {0, 2}), (100, {2})]
    assert sum(amount for amount, _ in pots) == 350


def test_build_pots_folded_chips_stay_but_cannot_win() -> None:
    pots = build_pots({0: 100, 1: 100, 2: 40}, folded={2})
    assert pots == [(120, {0, 1}), (120, {0, 1})]
    assert sum(amount for amount, _ in pots) == 240


class Scripted:
    """Plays a fixed list of actions, then checks or folds."""

    def __init__(self, *actions: Action) -> None:
        self.actions = list(actions)

    def decide(self, snap: Snapshot) -> Action:
        if self.actions:
            return self.actions.pop(0)
        return Action("check") if snap.to_call == 0 else Action("fold")


def fixed_deck(*cards: str) -> Deck:
    """A deck that deals the given cards first (in order), then the rest."""

    class Rigged(Deck):
        def reset(self) -> None:
            deal = [card(c) for c in cards]
            rest = [c for c in range(52) if c not in deal]
            self._cards = list(reversed(rest)) + list(reversed(deal))

    return Rigged(random.Random(0))


def test_everyone_folds_to_big_blind() -> None:
    seats = [
        Seat(0, "A", 100, Scripted(Action("fold"))),
        Seat(1, "SB", 100, Scripted(Action("fold"))),
        Seat(2, "BB", 100, Scripted()),
    ]
    table = Table(seats, small_blind=1, big_blind=2, deck=Deck(random.Random(3)))
    result = table.play_hand()
    assert not result.showdown
    # The button moves to seat 1 before the first hand, so seat 2 posts the small
    # blind and seat 0 the big blind. Both folds leave the big blind uncontested.
    assert result.winnings == {0: 1, 1: 0, 2: -1}
    assert sum(s.stack for s in seats) == 300


def test_raise_below_minimum_is_clamped_to_min_raise() -> None:
    # Seat 1 acts first preflop and tries to raise to 3 (BB is 2): legal minimum is 4.
    seats = [
        Seat(0, "A", 100, Scripted(Action("fold"))),
        Seat(1, "B", 100, Scripted(Action("raise", 3))),
        Seat(2, "C", 100, Scripted(Action("fold"))),
    ]
    table = Table(seats, small_blind=1, big_blind=2, deck=Deck(random.Random(5)))
    result = table.play_hand()
    assert any("raises to 4" in line for line in result.log)


def test_side_pot_all_in_pays_correct_winners() -> None:
    # Seat 0 is short (30) and all in; seats 1 and 2 play for more. Seat 2 holds the best hand.
    seats = [
        Seat(0, "Short", 30, Scripted(Action("raise", 30))),
        Seat(1, "Mid", 100, Scripted(Action("call"), Action("check"))),
        Seat(2, "Deep", 100, Scripted(Action("check"), Action("check"))),
    ]
    deck = fixed_deck("2c", "3c", "7d", "7h", "Ad", "Kd", "Qc", "Js", "Ts", "5s", "9d", "4c")
    table = Table(seats, small_blind=1, big_blind=2, deck=deck)
    table.button = 0
    result = table.play_hand()
    assert sum(result.winnings.values()) == 0
    assert all(s.stack >= 0 for s in seats)


def test_chips_conserved_over_many_hands() -> None:
    rng = random.Random(11)
    seats = [Seat(0, "Hero", 200, EquityBot(rng, trials=40))]
    seats += [Seat(i, f"Opp{i}", 200, CallingStation() if i % 2 == 0 else EquityBot(rng, trials=40)) for i in range(1, 5)]
    table = Table(seats, small_blind=1, big_blind=2, deck=Deck(rng))
    total = sum(s.stack for s in seats)
    for _ in range(120):
        if sum(1 for s in seats if s.stack > 0) < 2:
            break
        table.play_hand()
        assert sum(s.stack for s in seats) == total
        assert all(s.stack >= 0 for s in seats)
