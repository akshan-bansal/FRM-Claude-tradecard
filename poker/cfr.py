"""Self-play training by counterfactual regret minimisation (CFR) for Kuhn poker.

Kuhn poker is the smallest poker game: three cards (J, Q, K), one ante each, one
betting round with at most one bet and one raise. Its Nash equilibrium value for
the first player is known (-1/18), so it is a check that the trainer works before
it is pointed at the no-limit table.

Offline only. Nothing here connects to any online service.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import permutations
from typing import Final

CARDS: Final[tuple[str, ...]] = ("J", "Q", "K")
ACTIONS: Final[tuple[str, ...]] = ("p", "b")  # pass/check, bet/call
KUHN_GAME_VALUE: Final[float] = -1 / 18  # first player's value at equilibrium


def terminal_value(cards: tuple[int, int], history: str) -> float | None:
    """Payoff for the player to act at this node if the hand is over, else None.

    The player to act at a terminal node is len(history) % 2. Ante is 1 each.
    """
    if history == "pp":  # both checked: showdown for 1
        return 1.0 if cards[len(history) % 2] > cards[1 - len(history) % 2] else -1.0
    if history.endswith("bp"):  # bet, then a fold: the bettor wins 1
        return 1.0  # the player to act is the bettor, who was not the folder
    if history.endswith("bb"):  # bet and call: showdown for 2
        cur = len(history) % 2
        return 2.0 if cards[cur] > cards[1 - cur] else -2.0
    return None


@dataclass
class Node:
    regret_sum: list[float] = field(default_factory=lambda: [0.0, 0.0])
    strategy_sum: list[float] = field(default_factory=lambda: [0.0, 0.0])

    def strategy(self, reach: float) -> list[float]:
        """Regret matching: play actions in proportion to positive regret."""
        positive = [max(r, 0.0) for r in self.regret_sum]
        total = sum(positive)
        strat = [p / total for p in positive] if total > 0 else [0.5, 0.5]
        for a in range(2):
            self.strategy_sum[a] += reach * strat[a]
        return strat

    def average(self) -> list[float]:
        total = sum(self.strategy_sum)
        if total <= 0:
            return [0.5, 0.5]
        return [s / total for s in self.strategy_sum]


class KuhnCFR:
    """Vanilla CFR over every deal of Kuhn poker (exact expected values, no sampling)."""

    def __init__(self) -> None:
        self.nodes: dict[str, Node] = {}

    def _cfr(self, cards: tuple[int, int], history: str, p0: float, p1: float) -> float:
        term = terminal_value(cards, history)
        if term is not None:
            return term
        player = len(history) % 2
        info = CARDS[cards[player]] + history
        node = self.nodes.setdefault(info, Node())
        reach = p0 if player == 0 else p1
        strat = node.strategy(reach)
        util = [0.0, 0.0]
        node_util = 0.0
        for a, act in enumerate(ACTIONS):
            if player == 0:
                util[a] = -self._cfr(cards, history + act, p0 * strat[a], p1)
            else:
                util[a] = -self._cfr(cards, history + act, p0, p1 * strat[a])
            node_util += strat[a] * util[a]
        opp_reach = p1 if player == 0 else p0
        for a in range(2):
            node.regret_sum[a] += opp_reach * (util[a] - node_util)
        return node_util

    def train(self, iterations: int) -> float:
        """Run CFR; returns the first player's average expected value over the run."""
        total = 0.0
        deals = list(permutations(range(len(CARDS)), 2))  # all 6 deals, each equally likely
        for _ in range(iterations):
            for cards in deals:
                total += self._cfr(cards, "", 1.0, 1.0) / len(deals)
        return total / iterations

    def average_strategy(self, info: str) -> list[float]:
        return self.nodes[info].average()
