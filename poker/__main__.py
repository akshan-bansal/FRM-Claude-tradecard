"""Run a local No-Limit table: you (seat 0, a bot here) plus opponents, for N hands.

Usage: python -m poker [--opponents 4] [--hands 200] [--seed 1] [--stack 200] [--bb 2]
"""

from __future__ import annotations

import argparse
import random

from poker.bots import CallingStation, EquityBot
from poker.cards import Deck
from poker.engine import Seat, Table


def main() -> None:
    parser = argparse.ArgumentParser(description="Offline No-Limit hold'em simulator")
    parser.add_argument("--opponents", type=int, default=4)
    parser.add_argument("--hands", type=int, default=200)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--stack", type=int, default=200, help="starting chips per seat")
    parser.add_argument("--bb", type=int, default=2, help="big blind")
    args = parser.parse_args()
    if args.opponents < 1:
        raise SystemExit("--opponents must be at least 1")

    rng = random.Random(args.seed)
    seats = [Seat(0, "Hero", args.stack, EquityBot(rng))]
    for i in range(1, args.opponents + 1):
        # Alternate equity bots and calling stations so the table has variety.
        strategy = EquityBot(rng) if i % 2 else CallingStation()
        seats.append(Seat(i, f"Opp{i}", args.stack, strategy))

    table = Table(seats, small_blind=max(1, args.bb // 2), big_blind=args.bb, deck=Deck(rng))
    net = {s.index: 0 for s in seats}
    played = 0
    for _ in range(args.hands):
        if sum(1 for s in table.seats if s.stack > 0) < 2:
            break
        result = table.play_hand()
        played += 1
        for i, delta in result.winnings.items():
            net[i] += delta

    print(f"Played {played} hands, {len(seats)} seats, blinds {table.small_blind}/{table.big_blind}")
    for s in seats:
        print(f"  {s.name:<6} stack {s.stack:>5}  net {net[s.index]:>+6}")


if __name__ == "__main__":
    main()
