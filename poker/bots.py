"""Offline bot strategies for the No-Limit table. No network access, no real money."""

from __future__ import annotations

import math
import random
from collections import deque

from poker.cards import evaluate
from poker.engine import Action, Snapshot


def estimate_equity(
    hole: tuple[int, int],
    board: tuple[int, ...],
    opponents: int,
    rng: random.Random,
    trials: int = 150,
) -> float:
    """Monte Carlo share of the pot (ties count fractionally) against random hands."""
    if opponents <= 0:
        return 1.0
    known = {*hole, *board}
    remaining = [c for c in range(52) if c not in known]
    need_board = 5 - len(board)
    need_cards = need_board + 2 * opponents
    share = 0.0
    for _ in range(trials):
        draw = rng.sample(remaining, need_cards)
        full_board = (*board, *draw[:need_board])
        hero = evaluate([*hole, *full_board])
        best_opp = None
        for i in range(opponents):
            start = need_board + 2 * i
            score = evaluate([draw[start], draw[start + 1], *full_board])
            if best_opp is None or score > best_opp:
                best_opp = score
        assert best_opp is not None
        if hero > best_opp:
            share += 1.0
        elif hero == best_opp:
            # Ties among the opponents too would split the pot further; this is an approximation.
            share += 0.5
    return share / trials


class EquityBot:
    """Bets and raises with strong equity, calls when the price is right, folds otherwise."""

    def __init__(self, rng: random.Random, trials: int = 150) -> None:
        self.rng = rng
        self.trials = trials

    def decide(self, snap: Snapshot) -> Action:
        eq = estimate_equity(snap.hole, snap.board, snap.active_opponents, self.rng, self.trials)
        return self.choose(eq, snap)

    def choose(self, eq: float, snap: Snapshot) -> Action:
        increment = max(snap.min_raise_to - snap.current_bet, 1)
        if snap.to_call == 0:
            if eq >= 0.6:
                return Action("raise", snap.current_bet + max(increment, snap.pot * 2 // 3))
            return Action("check")
        pot_odds = snap.to_call / (snap.pot + snap.to_call)
        if eq >= 0.75:
            return Action("raise", snap.current_bet + max(increment, snap.pot))
        if eq >= pot_odds + 0.05:
            return Action("call")
        return Action("fold")


class KellyVolBot(EquityBot):
    """EquityBot with raise sizes capped by a dynamic Kelly fraction of stack.

    Kelly fraction for an even-money bet with win probability p is 2p - 1 (zero when
    p <= 0.5). It is scaled by target_vol / realised_vol, where realised_vol is the
    standard deviation of recent per-hand returns (fraction of bankroll), so the bot
    bets smaller after volatile runs and larger when results are calm. The result is
    clamped to [fmin, fmax]. A raise above the cap becomes a call or check.
    """

    def __init__(
        self,
        rng: random.Random,
        trials: int = 30,
        target_vol: float = 0.05,
        fmin: float = 0.02,
        fmax: float = 0.5,
        window: int = 50,
    ) -> None:
        super().__init__(rng, trials)
        self.target_vol = target_vol
        self.fmin = fmin
        self.fmax = fmax
        self.returns: deque[float] = deque(maxlen=window)

    def observe(self, hand_return: float) -> None:
        """Record one hand's return as a fraction of the bankroll before the hand."""
        self.returns.append(hand_return)

    def realised_vol(self) -> float:
        n = len(self.returns)
        if n < 5:
            return self.target_vol  # too little history: neutral scaling
        mean = sum(self.returns) / n
        var = sum((r - mean) ** 2 for r in self.returns) / (n - 1)
        return math.sqrt(var)

    def fraction(self, eq: float) -> float:
        kelly = max(0.0, 2 * eq - 1)
        vol = self.realised_vol()
        scale = self.target_vol / vol if vol > 0 else 1.0
        return min(self.fmax, max(self.fmin, kelly * scale))

    def decide(self, snap: Snapshot) -> Action:
        eq = estimate_equity(snap.hole, snap.board, snap.active_opponents, self.rng, self.trials)
        action = self.choose(eq, snap)
        if action.kind != "raise":
            return action
        cap = snap.current_bet + int(self.fraction(eq) * snap.stack)
        if cap < snap.min_raise_to:
            return Action("call") if snap.to_call > 0 else Action("check")
        return Action("raise", min(action.to, cap))


class CallingStation:
    """Never folds, never raises. Calls any bet, checks otherwise."""

    def decide(self, snap: Snapshot) -> Action:
        return Action("call") if snap.to_call > 0 else Action("check")
