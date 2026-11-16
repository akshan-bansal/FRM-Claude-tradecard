"""Run the hero's equity curve across a grid of table settings, offline.

Each run starts the hero with NOTIONAL_BANKROLL (1 chip = $1). Every seat starts with
the same notional stack. The curve is the hero's bankroll after each hand. Nothing here
connects to any online service or moves real money.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from itertools import product
from pathlib import Path

import time

from poker.bots import CallingStation, EquityBot, KellyVolBot
from poker.cards import Deck
from poker.engine import Seat, Table

NOTIONAL_BANKROLL = 1000
WALL_BUDGET_S = 600  # the grid stops starting new runs after 10 minutes


@dataclass(frozen=True)
class RunConfig:
    opponents: int
    big_blind: int
    seed: int
    hands: int
    trials: int
    sizing: str = "flat"  # "flat" (EquityBot) or "kelly_vol" (KellyVolBot)


def run_one(cfg: RunConfig) -> list[int]:
    """Return the hero's bankroll after each hand, starting at NOTIONAL_BANKROLL."""
    rng = random.Random(cfg.seed)
    hero: EquityBot = (
        KellyVolBot(rng, cfg.trials) if cfg.sizing == "kelly_vol" else EquityBot(rng, cfg.trials)
    )
    seats = [Seat(0, "Hero", NOTIONAL_BANKROLL, hero)]
    for i in range(1, cfg.opponents + 1):
        strategy = EquityBot(rng, cfg.trials) if i % 2 else CallingStation()
        seats.append(Seat(i, f"Opp{i}", NOTIONAL_BANKROLL, strategy))
    table = Table(
        seats,
        small_blind=max(1, cfg.big_blind // 2),
        big_blind=cfg.big_blind,
        deck=Deck(rng),
    )
    curve = [NOTIONAL_BANKROLL]
    for _ in range(cfg.hands):
        if seats[0].stack <= 0 or sum(1 for s in seats if s.stack > 0) < 2:
            break
        before = seats[0].stack
        result = table.play_hand()
        if isinstance(hero, KellyVolBot) and before > 0:
            hero.observe(result.winnings[0] / before)
        curve.append(seats[0].stack)
    return curve


def run_grid(
    opponents: list[int],
    big_blinds: list[int],
    seeds: list[int],
    hands: int,
    trials: int,
    sizing: str = "flat",
    budget_s: float = WALL_BUDGET_S,
) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    started = time.monotonic()
    for opp, bb, seed in product(opponents, big_blinds, seeds):
        if time.monotonic() - started > budget_s:
            break  # budget spent: report what finished rather than overrun
        cfg = RunConfig(opp, bb, seed, hands, trials, sizing)
        curve = run_one(cfg)
        results.append({
            "sizing": sizing,
            "opponents": opp,
            "big_blind": bb,
            "seed": seed,
            "hands": len(curve) - 1,
            "final": curve[-1],
            "net": curve[-1] - NOTIONAL_BANKROLL,
            "min": min(curve),
            "max": max(curve),
            "busted": curve[-1] <= 0,
            "curve": curve,
        })
    return results


def write_results(results: list[dict[str, object]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(results), encoding="utf-8")


if __name__ == "__main__":
    import sys

    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("grid_results.json")
    sizing = sys.argv[2] if len(sys.argv) > 2 else "flat"
    hands = int(sys.argv[3]) if len(sys.argv) > 3 else 100
    res = run_grid(
        opponents=[2, 4, 6],
        big_blinds=[2, 5, 10],
        seeds=[1, 2],
        hands=hands,
        trials=30,
        sizing=sizing,
    )
    write_results(res, out)
    print(f"{len(res)} of 18 runs finished within the {WALL_BUDGET_S}s budget ({sizing})")
    for r in res:
        print(f"opp={r['opponents']} bb={r['big_blind']} seed={r['seed']} "
              f"hands={r['hands']} final=${r['final']} net={r['net']:+d}")
