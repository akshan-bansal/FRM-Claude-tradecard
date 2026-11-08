"""Cards, deck, and hand evaluation for Texas hold'em.

A card is an int 0..51: rank = card // 4 (0 = deuce ... 12 = ace), suit = card % 4.
Hand scores are tuples that compare directly: higher tuple wins.
"""

from __future__ import annotations

import random
from collections import Counter
from itertools import combinations
from typing import Final

RANK_CHARS: Final[str] = "23456789TJQKA"
SUIT_CHARS: Final[str] = "cdhs"

# Category codes in the score tuple (higher is better).
HIGH_CARD, PAIR, TWO_PAIR, TRIPS, STRAIGHT, FLUSH, FULL_HOUSE, QUADS, STRAIGHT_FLUSH = range(9)
CATEGORY_NAMES: Final[dict[int, str]] = {
    HIGH_CARD: "High card",
    PAIR: "Pair",
    TWO_PAIR: "Two pair",
    TRIPS: "Three of a kind",
    STRAIGHT: "Straight",
    FLUSH: "Flush",
    FULL_HOUSE: "Full house",
    QUADS: "Four of a kind",
    STRAIGHT_FLUSH: "Straight flush",
}


def card(text: str) -> int:
    """Parse a card like 'As' or 'Td'."""
    if len(text) != 2:
        raise ValueError(f"bad card: {text!r}")
    rank = RANK_CHARS.find(text[0].upper())
    suit = SUIT_CHARS.find(text[1].lower())
    if rank < 0 or suit < 0:
        raise ValueError(f"bad card: {text!r}")
    return rank * 4 + suit


def card_str(c: int) -> str:
    return RANK_CHARS[c // 4] + SUIT_CHARS[c % 4]


class Deck:
    def __init__(self, rng: random.Random | None = None) -> None:
        self._rng = rng if rng is not None else random.Random()
        self._cards: list[int] = []
        self.reset()

    def reset(self) -> None:
        self._cards = list(range(52))
        self._rng.shuffle(self._cards)

    def draw(self) -> int:
        if not self._cards:
            raise RuntimeError("deck is empty")
        return self._cards.pop()


def _score_five(cards: tuple[int, ...]) -> tuple[int, ...]:
    ranks = sorted((c // 4 for c in cards), reverse=True)
    suits = {c % 4 for c in cards}
    flush = len(suits) == 1

    unique = sorted(set(ranks), reverse=True)
    straight_high: int | None = None
    if len(unique) == 5:
        if unique[0] - unique[4] == 4:
            straight_high = unique[0]
        elif unique == [12, 3, 2, 1, 0]:  # the wheel: A-2-3-4-5
            straight_high = 3

    if straight_high is not None and flush:
        return (STRAIGHT_FLUSH, straight_high)
    if straight_high is not None:
        return (STRAIGHT, straight_high)
    if flush:
        return (FLUSH, *ranks)

    # Group by (count, rank), most common first, so kickers sort correctly.
    groups = sorted(Counter(ranks).items(), key=lambda g: (g[1], g[0]), reverse=True)
    counts = [n for _, n in groups]
    ordered = [r for r, _ in groups]
    if counts == [4, 1]:
        return (QUADS, *ordered)
    if counts == [3, 2]:
        return (FULL_HOUSE, *ordered)
    if counts == [3, 1, 1]:
        return (TRIPS, *ordered)
    if counts == [2, 2, 1]:
        return (TWO_PAIR, *ordered)
    if counts == [2, 1, 1, 1]:
        return (PAIR, *ordered)
    return (HIGH_CARD, *ordered)


def evaluate(cards: list[int] | tuple[int, ...]) -> tuple[int, ...]:
    """Best five-card score out of five to seven cards."""
    if not 5 <= len(cards) <= 7:
        raise ValueError("need 5 to 7 cards")
    return max(_score_five(combo) for combo in combinations(cards, 5))


def category(score: tuple[int, ...]) -> str:
    return CATEGORY_NAMES[score[0]]
