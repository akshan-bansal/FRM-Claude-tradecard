"""Simulate a shoe and print the count, true count, and bet for each round.

Usage: python -m blackjack [--decks 6] [--rounds 20] [--seed 1] [--system "Hi-Lo"]
"""

from __future__ import annotations

import argparse
import random

from blackjack.counting import SYSTEMS, CountTracker, Shoe, bet_units


def main() -> None:
    parser = argparse.ArgumentParser(description="Blackjack counting demo")
    parser.add_argument("--decks", type=int, default=6)
    parser.add_argument("--rounds", type=int, default=20)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--system", default="Hi-Lo", choices=sorted(SYSTEMS))
    parser.add_argument("--seats", type=int, default=9, help="players at the table")
    parser.add_argument("--seat", type=int, default=2, help="our seat, 1 = first base")
    args = parser.parse_args()
    if not 1 <= args.seat <= args.seats:
        raise SystemExit("--seat must be between 1 and --seats")

    shoe = Shoe(args.decks, rng=random.Random(args.seed))
    tracker = CountTracker(shoe, SYSTEMS[args.system])
    print(
        f"{args.system}, {args.decks} decks, penetration {shoe.penetration:.0%}, "
        f"{args.seats} seats, we sit in seat {args.seat}"
    )
    print(f"{'round':>5} {'running':>8} {'true':>6} {'bet':>4}")

    for rnd in range(1, args.rounds + 1):
        if tracker.reshuffle_if_needed():
            print("      -- reshuffle --")
        # Seats 1 to (seat-1) are dealt before us, so their cards are visible
        # when we decide. Each seat and the dealer get two cards.
        for _ in range((args.seat - 1) * 2):
            tracker.deal()
        tc = tracker.true_count()
        print(
            f"{rnd:>5} {tracker.count.running:>+8d} {tc:>+6.2f} "
            f"{bet_units(tc):>4d}"
        )
        # Deal the rest of the table and the dealer, which we do not act on yet.
        remaining_cards = (args.seats - args.seat + 1) * 2 + 2
        for _ in range(remaining_cards):
            tracker.deal()


if __name__ == "__main__":
    main()
