"""No-Limit Texas hold'em hand engine: blinds, betting rounds, side pots, showdown.

Offline only. No network, no real money. Chips are integers.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol

from poker.cards import Deck, card_str, evaluate

ActionKind = Literal["fold", "check", "call", "raise"]


@dataclass(frozen=True)
class Action:
    kind: ActionKind
    # For "raise": the total bet this street to raise to (not the increment).
    to: int = 0


@dataclass(frozen=True)
class Snapshot:
    """What a strategy may see when it acts. Opponents' hole cards are hidden."""

    seat: int
    hole: tuple[int, int]
    board: tuple[int, ...]
    street: str
    pot: int
    to_call: int
    current_bet: int  # highest total bet this street; raises are measured from here
    min_raise_to: int
    max_raise_to: int
    stack: int
    active_opponents: int


class Strategy(Protocol):
    def decide(self, snap: Snapshot) -> Action: ...


@dataclass
class Seat:
    index: int
    name: str
    stack: int
    strategy: Strategy
    hole: tuple[int, int] = (-1, -1)
    bet: int = 0  # chips committed this street
    total: int = 0  # chips committed this hand
    folded: bool = False
    all_in: bool = False

    @property
    def live(self) -> bool:
        return not self.folded


@dataclass
class HandResult:
    board: tuple[int, ...]
    winnings: dict[int, int]  # seat index -> net chips won this hand (can be negative)
    showdown: bool
    log: list[str] = field(default_factory=list)


def build_pots(totals: dict[int, int], folded: set[int]) -> list[tuple[int, set[int]]]:
    """Split contributions into a main pot and side pots.

    Returns (amount, eligible seats) per pot, lowest level first. Folded seats
    contribute chips but cannot win them.
    """
    pots: list[tuple[int, set[int]]] = []
    carry = 0
    prev = 0
    for level in sorted({t for t in totals.values() if t > 0}):
        amount = carry + sum(
            min(t, level) - min(t, prev) for t in totals.values()
        )
        eligible = {s for s, t in totals.items() if t >= level and s not in folded}
        prev = level
        if not eligible:
            carry = amount  # unreachable in normal play; never drop chips
            continue
        carry = 0
        pots.append((amount, eligible))
    return pots


class Table:
    """Plays hands at a fixed table. Chips are conserved across hands."""

    def __init__(
        self,
        seats: Sequence[Seat],
        small_blind: int = 1,
        big_blind: int = 2,
        deck: Deck | None = None,
    ) -> None:
        if len(seats) < 2:
            raise ValueError("need at least 2 seats")
        if small_blind < 1 or big_blind < small_blind:
            raise ValueError("bad blinds")
        self.seats = list(seats)
        self.small_blind = small_blind
        self.big_blind = big_blind
        self.deck = deck if deck is not None else Deck()
        self.button = 0
        self._board: list[int] = []

    # ---- helpers -------------------------------------------------------

    def _next_seat(self, index: int, pred: Callable[[Seat], bool]) -> int | None:
        n = len(self.seats)
        for step in range(1, n + 1):
            seat = self.seats[(index + step) % n]
            if pred(seat):
                return seat.index
        return None

    def _commit(self, seat: Seat, amount: int) -> None:
        amount = min(amount, seat.stack)
        seat.stack -= amount
        seat.bet += amount
        seat.total += amount
        if seat.stack == 0:
            seat.all_in = True

    # ---- hand ----------------------------------------------------------

    def play_hand(self) -> HandResult:
        seated = [s for s in self.seats if s.stack > 0]
        if len(seated) < 2:
            raise RuntimeError("fewer than 2 players with chips")
        starting = {s.index: s.stack for s in self.seats}
        for s in self.seats:
            s.bet = s.total = 0
            s.folded = s.all_in = s.stack == 0
            s.hole = (-1, -1)

        self.button = self._next_seat(self.button, lambda s: s.stack > 0)
        assert self.button is not None
        log: list[str] = []
        self.deck.reset()
        for s in self.seats:
            if s.stack > 0:
                s.hole = (self.deck.draw(), self.deck.draw())
                s.folded = False

        # Blinds. Heads-up the button posts the small blind.
        live = [s for s in self.seats if not s.folded]
        if len(live) == 2:
            sb_seat = self.button
            bb_seat = self._next_seat(sb_seat, lambda s: not s.folded)
        else:
            sb_seat = self._next_seat(self.button, lambda s: not s.folded)
            bb_seat = self._next_seat(sb_seat, lambda s: not s.folded)
        assert sb_seat is not None and bb_seat is not None
        self._commit(self.seats[sb_seat], self.small_blind)
        self._commit(self.seats[bb_seat], self.big_blind)
        log.append(f"blinds {self.small_blind}/{self.big_blind}")

        self._board = []
        showdown = False
        street = "preflop"
        first = self._next_seat(bb_seat, lambda s: not s.folded and not s.all_in)
        min_raise = self.big_blind

        while True:
            min_raise = self._betting_round(street, first, min_raise, log, bb_seat)
            if sum(1 for s in self.seats if not s.folded) == 1:
                break
            if street == "river":
                showdown = True
                break
            # Deal the next street. Burn cards are omitted (no effect on the game).
            count = 3 if street == "preflop" else 1
            for _ in range(count):
                self._board.append(self.deck.draw())
            street = {"preflop": "flop", "flop": "turn", "turn": "river"}[street]
            log.append(f"{street}: {' '.join(card_str(c) for c in self._board)}")
            for s in self.seats:
                s.bet = 0
            first = self._next_seat(self.button, lambda s: not s.folded and not s.all_in)
            min_raise = self.big_blind
            if first is None:  # everyone left is all-in: run out the board
                continue

        board_cards = tuple(self._board)
        self._settle(board_cards, showdown, log)
        winnings = {s.index: s.stack - starting[s.index] for s in self.seats}
        result = HandResult(board_cards, winnings, showdown, log)
        self.button = self._next_seat(self.button, lambda s: s.stack > 0) or self.button
        return result

    def _betting_round(
        self,
        street: str,
        first: int | None,
        min_raise: int,
        log: list[str],
        bb_seat: int,
    ) -> int:
        """Run one betting round. Returns the minimum raise increment in effect."""
        if first is None:
            return min_raise
        current = max(s.bet for s in self.seats)
        queue: deque[int] = deque()
        n = len(self.seats)
        for step in range(n):
            s = self.seats[(first + step) % n]
            if not s.folded and not s.all_in:
                queue.append(s.index)

        while queue:
            if sum(1 for s in self.seats if not s.folded) == 1:
                break
            seat = self.seats[queue.popleft()]
            if seat.folded or seat.all_in:
                continue
            to_call = current - seat.bet
            max_to = seat.bet + seat.stack
            min_to = current + min_raise
            snap = Snapshot(
                seat=seat.index,
                hole=seat.hole,
                board=self._board_so_far(),
                street=street,
                pot=sum(s.total for s in self.seats),
                to_call=to_call,
                current_bet=current,
                min_raise_to=min(min_to, max_to),
                max_raise_to=max_to,
                stack=seat.stack,
                active_opponents=sum(
                    1 for s in self.seats if s.index != seat.index and not s.folded
                ),
            )
            action = seat.strategy.decide(snap)
            action = self._legalise(action, to_call, snap, current)

            if action.kind == "fold":
                seat.folded = True
                log.append(f"{seat.name} folds")
            elif action.kind == "check":
                log.append(f"{seat.name} checks")
            elif action.kind == "call":
                self._commit(seat, to_call)
                log.append(f"{seat.name} calls {to_call}")
            else:  # raise
                raise_to = min(action.to, max_to)
                increment = raise_to - current
                self._commit(seat, raise_to - seat.bet)
                if raise_to > current:
                    if increment >= min_raise:
                        min_raise = increment
                    current = raise_to
                    # Everyone else who can still act must respond to this raise.
                    for step in range(1, n):
                        other = self.seats[(seat.index + step) % n]
                        if not other.folded and not other.all_in and other.index not in queue:
                            queue.append(other.index)
                log.append(f"{seat.name} raises to {raise_to}")
        return min_raise

    def _board_so_far(self) -> tuple[int, ...]:
        return self._board

    def _legalise(self, action: Action, to_call: int, snap: Snapshot, current: int) -> Action:
        """Clamp an illegal action to the nearest legal one."""
        if action.kind == "check" and to_call > 0:
            return Action("fold") if to_call > snap.stack else Action("call")
        if action.kind == "call" and to_call == 0:
            return Action("check")
        if action.kind == "raise":
            if snap.max_raise_to <= current:  # cannot raise: only call/check remain
                return Action("call") if to_call > 0 else Action("check")
            to = max(action.to, snap.min_raise_to)
            to = min(to, snap.max_raise_to)
            return Action("raise", to)
        return action

    def _settle(self, board: tuple[int, ...], showdown: bool, log: list[str]) -> None:
        contrib = {s.index: s.total for s in self.seats}
        folded = {s.index for s in self.seats if s.folded}
        pots = build_pots(contrib, folded)
        scores: dict[int, tuple[int, ...]] = {}
        if showdown:
            for s in self.seats:
                if not s.folded:
                    scores[s.index] = evaluate([*s.hole, *board])
        for amount, eligible in pots:
            if len(eligible) == 1 or not showdown:
                winners = sorted(eligible)
            else:
                best = max(scores[i] for i in eligible)
                winners = sorted(i for i in eligible if scores[i] == best)
            share, odd = divmod(amount, len(winners))
            for i, w in enumerate(winners):
                self.seats[w].stack += share + (odd if i == 0 else 0)
            log.append(f"pot {amount} to {', '.join(self.seats[w].name for w in winners)}")
