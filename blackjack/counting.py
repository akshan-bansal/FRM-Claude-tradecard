"""Blackjack card counting: tag systems, running and true count, bet spread, index plays.

Standard library only. Ranks are strings: "2".."10", "J", "Q", "K", "A".
Every ten-valued card (10, J, Q, K) shares the rank tag of "10" for index plays.
"""

from __future__ import annotations

import math
import random
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Final

RANKS: Final[tuple[str, ...]] = (
    "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A",
)
CARDS_PER_DECK: Final[int] = 52
SUITS_PER_DECK: Final[int] = 4
# Floor for decks remaining when computing true count, so the last few cards
# do not blow the true count up to infinity.
MIN_DECKS_REMAINING: Final[float] = 0.5


@dataclass(frozen=True)
class CountingSystem:
    """A tag system: the value each rank adds to the running count."""

    name: str
    tags: Mapping[str, int]

    def __post_init__(self) -> None:
        missing = set(RANKS) - set(self.tags)
        extra = set(self.tags) - set(RANKS)
        if missing or extra:
            raise ValueError(
                f"{self.name}: tags must cover exactly {RANKS}; "
                f"missing={sorted(missing)} extra={sorted(extra)}"
            )

    def tag(self, rank: str) -> int:
        try:
            return self.tags[rank]
        except KeyError as exc:
            raise ValueError(f"unknown rank: {rank!r}") from exc

    @property
    def full_deck_sum(self) -> int:
        """Sum of all tags in one 52-card deck. Zero means the system is balanced."""
        return SUITS_PER_DECK * sum(self.tags[r] for r in RANKS)

    @property
    def balanced(self) -> bool:
        return self.full_deck_sum == 0


def _system(name: str, values: tuple[int, ...]) -> CountingSystem:
    """Build a system from tag values for RANKS, in order (2 through A)."""
    return CountingSystem(name, dict(zip(RANKS, values, strict=True)))


# Hi-Lo: 2-6 +1, 7-9 0, 10-A -1. Balanced, the most common practical system.
HI_LO = _system("Hi-Lo", (1, 1, 1, 1, 1, 0, 0, 0, -1, -1, -1, -1, -1))
# Hi-Opt I: 3-6 +1, 10-K -1, everything else 0. Balanced.
HI_OPT_I = _system("Hi-Opt I", (0, 1, 1, 1, 1, 0, 0, 0, -1, -1, -1, -1, 0))
# Omega II: 2,3,7 +1; 4,5,6 +2; 9 -1; 10-K -2; A and 8 zero. Balanced.
OMEGA_II = _system("Omega II", (1, 1, 2, 2, 2, 1, 0, -1, -2, -2, -2, -2, 0))
# KO (Knock-Out): 2-7 +1, 8-9 0, 10-A -1. Unbalanced (+4 per deck), so it
# needs no true-count division and is played without a conversion.
KO = _system("KO", (1, 1, 1, 1, 1, 1, 0, 0, -1, -1, -1, -1, -1))

SYSTEMS: Final[dict[str, CountingSystem]] = {
    s.name: s for s in (HI_LO, HI_OPT_I, OMEGA_II, KO)
}


class RunningCount:
    """Tracks the running count for one shoe under a given tag system."""

    def __init__(self, system: CountingSystem = HI_LO) -> None:
        self.system = system
        self.running = 0
        self.cards_seen = 0

    def observe(self, rank: str) -> None:
        self.running += self.system.tag(rank)
        self.cards_seen += 1

    def observe_many(self, ranks: Iterable[str]) -> None:
        for rank in ranks:
            self.observe(rank)

    def reset(self) -> None:
        self.running = 0
        self.cards_seen = 0

    def true_count(self, cards_remaining: int) -> float:
        """Running count divided by decks left (floored at MIN_DECKS_REMAINING)."""
        if cards_remaining <= 0:
            raise ValueError("cards_remaining must be positive")
        decks = max(cards_remaining / CARDS_PER_DECK, MIN_DECKS_REMAINING)
        return self.running / decks


class Shoe:
    """A multi-deck shoe that reshuffles once penetration is reached."""

    def __init__(
        self,
        num_decks: int = 6,
        penetration: float = 0.75,
        rng: random.Random | None = None,
    ) -> None:
        if num_decks < 1:
            raise ValueError("num_decks must be at least 1")
        if not 0 < penetration <= 1:
            raise ValueError("penetration must be in (0, 1]")
        self.num_decks = num_decks
        self.penetration = penetration
        self._rng = rng if rng is not None else random.Random()
        self._cards: list[str] = []
        self.shuffle()

    @property
    def total_cards(self) -> int:
        return self.num_decks * CARDS_PER_DECK

    @property
    def cards_remaining(self) -> int:
        return len(self._cards)

    @property
    def decks_remaining(self) -> float:
        return self.cards_remaining / CARDS_PER_DECK

    @property
    def needs_shuffle(self) -> bool:
        """True once the dealt share of the shoe reaches the penetration point."""
        dealt = self.total_cards - self.cards_remaining
        return dealt >= self.total_cards * self.penetration

    def shuffle(self) -> None:
        self._cards = [
            rank for rank in RANKS for _ in range(SUITS_PER_DECK * self.num_decks)
        ]
        self._rng.shuffle(self._cards)

    def draw(self) -> str:
        if not self._cards:
            raise RuntimeError("shoe is empty; shuffle first")
        return self._cards.pop()


class CountTracker:
    """Ties a shoe to a running count: deals, counts, and reshuffles at penetration."""

    def __init__(self, shoe: Shoe, system: CountingSystem = HI_LO) -> None:
        self.shoe = shoe
        self.count = RunningCount(system)

    def deal(self) -> str:
        card = self.shoe.draw()
        self.count.observe(card)
        return card

    def true_count(self) -> float:
        return self.count.true_count(self.shoe.cards_remaining)

    def reshuffle_if_needed(self) -> bool:
        """Reshuffle and reset the count when the shoe is past penetration."""
        if not self.shoe.needs_shuffle:
            return False
        self.shoe.shuffle()
        self.count.reset()
        return True


def bet_units(true_count: float, *, min_units: int = 1, max_units: int = 8) -> int:
    """Bet spread on the true count: 1 unit below +2, then 2, 4, 6, 8 for +2..+5.

    Rounded down to whole true counts; capped at max_units.
    """
    if min_units < 1 or max_units < min_units:
        raise ValueError("need 1 <= min_units <= max_units")
    if true_count < 2:
        return min_units
    units = 2 * (math.floor(true_count) - 1)
    return max(min_units, min(max_units, units))


def estimated_player_edge(true_count: float, house_edge: float = 0.005) -> float:
    """Approximate player edge as a fraction of the bet.

    Rule of thumb for Hi-Lo: each +1 true count adds about 0.5% to the player's
    edge over the house edge (house_edge, a basic-strategy assumption).
    """
    return -house_edge + 0.005 * true_count


def take_insurance(true_count: float) -> bool:
    """Insurance is worth taking at a true count of +3 or more (Hi-Lo)."""
    return true_count >= 3


@dataclass(frozen=True)
class IndexPlay:
    """A count-based deviation from basic strategy for a hard total.

    Deviate to `action` when the true count is >= threshold (at_least=True)
    or <= threshold (at_least=False).
    """

    hard_total: int
    upcard: str
    action: str
    threshold: float
    at_least: bool


# The Illustrious 18 (Hi-Lo), hard totals only. Pair deviations, such as
# 10,10 vs 5 and 6, are not included.
ILLUSTRIOUS_18: Final[tuple[IndexPlay, ...]] = (
    IndexPlay(16, "10", "stand", 0, at_least=True),
    IndexPlay(15, "10", "stand", 4, at_least=True),
    IndexPlay(10, "10", "double", 4, at_least=True),
    IndexPlay(10, "A", "double", 4, at_least=True),
    IndexPlay(9, "2", "double", 1, at_least=True),
    IndexPlay(9, "7", "double", 3, at_least=True),
    IndexPlay(11, "A", "double", 1, at_least=True),
    IndexPlay(16, "9", "stand", 5, at_least=True),
    IndexPlay(12, "2", "stand", 3, at_least=True),
    IndexPlay(12, "3", "stand", 2, at_least=True),
    IndexPlay(12, "4", "stand", 0, at_least=True),
    IndexPlay(12, "5", "stand", -2, at_least=False),
    IndexPlay(12, "6", "stand", -1, at_least=False),
    IndexPlay(13, "2", "stand", -1, at_least=False),
    IndexPlay(13, "3", "stand", -2, at_least=False),
)


def index_play(hard_total: int, upcard: str, true_count: float) -> str | None:
    """Return the count-driven deviation, or None to follow basic strategy.

    Only applies to hard totals (no ace counted as 11). The caller decides
    whether the deviation is legal for the hand, for example double-down
    restrictions.
    """
    for play in ILLUSTRIOUS_18:
        if play.hard_total != hard_total or play.upcard != upcard:
            continue
        triggered = (
            true_count >= play.threshold
            if play.at_least
            else true_count <= play.threshold
        )
        if triggered:
            return play.action
    return None
