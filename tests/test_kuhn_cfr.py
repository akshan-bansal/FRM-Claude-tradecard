"""Kuhn poker CFR self-play converges to the known equilibrium."""

from __future__ import annotations

import pytest

from poker.cfr import KUHN_GAME_VALUE, KuhnCFR, terminal_value


def test_showdown_and_fold_payoffs() -> None:
    # K (2) beats J (0) at showdown after both check: +1 for K's holder.
    assert terminal_value((2, 0), "pp") == 1.0
    assert terminal_value((0, 2), "pp") == -1.0
    # Bet then fold: the bettor wins 1, and the player to act is the bettor.
    assert terminal_value((0, 2), "bp") == 1.0
    # Bet and call: showdown for 2.
    assert terminal_value((2, 0), "bb") == 2.0
    assert terminal_value((0, 2), "bb") == -2.0
    # Non-terminal.
    assert terminal_value((0, 2), "b") is None


@pytest.fixture(scope="module")
def trained() -> KuhnCFR:
    trainer = KuhnCFR()
    trainer.train(20_000)
    return trainer


def test_game_value_converges_to_minus_one_eighteenth(trained: KuhnCFR) -> None:
    # Re-run a fresh trainer and check the average value over the last stretch.
    fresh = KuhnCFR()
    fresh.train(5_000)
    value = fresh.train(5_000)
    assert value == pytest.approx(KUHN_GAME_VALUE, abs=0.01)


def test_equilibrium_family_King_bets_three_times_Jack(trained: KuhnCFR) -> None:
    # Kuhn's equilibrium is a family: the first player bets a Jack (bluff) with
    # probability alpha in [0, 1/3] and a King with probability 3*alpha.
    jack_bet = trained.average_strategy("J")[1]  # index 1 is bet
    king_bet = trained.average_strategy("K")[1]
    assert 0.0 <= jack_bet <= 1 / 3 + 0.02
    assert king_bet == pytest.approx(3 * jack_bet, abs=0.03)


def test_queen_opening_always_checks(trained: KuhnCFR) -> None:
    # Equilibrium: the first player with a Queen checks at the opening.
    assert trained.average_strategy("Q")[0] == pytest.approx(1.0, abs=0.02)
