"""The delay study on synthetic bars with a delay planted in them."""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from trading_live_claude.analysis import listing_delay as ld


def _bars(prices: list[float], start: str, step_h: int = 1) -> pd.DataFrame:
    index = pd.date_range(start, periods=len(prices), freq=f"{step_h}h", tz="UTC")
    p = np.array(prices)
    return pd.DataFrame({"open": np.r_[p[0], p[:-1]], "close": p, "end": index + pd.Timedelta(hours=step_h)},
                        index=index)


def _flat_sigma(move: pd.Series) -> pd.Series:
    return pd.Series(0.001, index=move.index)


T0, T1 = pd.Timestamp("2026-03-02 05:00", tz="UTC"), pd.Timestamp("2026-03-02 06:00", tz="UTC")


def test_a_closed_target_splits_into_wait_and_reaction() -> None:
    # Target reopens at 14:00, eight hours after the event ends; half the move arrives in its second bar.
    target = pd.concat([_bars([100.0], "2026-03-01 20:00"), _bars([100.5, 101.1, 102.0], "2026-03-02 14:00")])
    m = ld.measure_event(T0, T1, np.log(1.02), target)
    assert m is not None and not m["censored"]
    assert m["closure_wait_h"] == 8.0 and m["total_h"] == 10.0 and m["reaction_h"] == 2.0


def test_a_move_already_in_the_opening_gap_has_no_reaction_time() -> None:
    target = pd.concat([_bars([100.0], "2026-03-01 20:00"),
                        pd.DataFrame({"open": [101.5], "close": [101.6],
                                      "end": [pd.Timestamp("2026-03-02 15:00", tz="UTC")]},
                                     index=[pd.Timestamp("2026-03-02 14:00", tz="UTC")])])
    m = ld.measure_event(T0, T1, np.log(1.02), target)
    assert m is not None and m["gap_capture"] > 0.5 and m["reaction_h"] == 1.0 and m["closure_wait_h"] == 8.0


def test_a_same_bar_move_is_zero_delay() -> None:
    target = _bars([100.0, 100.0, 102.0], "2026-03-02 03:00")
    m = ld.measure_event(T0, T1, np.log(1.02), target)
    assert m is not None and m["total_h"] == 0.0 and m["closure_wait_h"] == 0.0


def test_a_target_that_never_follows_is_censored_not_dropped() -> None:
    target = pd.concat([_bars([100.0], "2026-03-01 20:00"), _bars([100.0] * 20, "2026-03-02 14:00")])
    m = ld.measure_event(T0, T1, np.log(1.02), target)
    assert m is not None and m["censored"] is True and m["total_h"] == 22.0


def test_a_downward_move_needs_a_downward_follow() -> None:
    target = pd.concat([_bars([100.0], "2026-03-01 20:00"), _bars([101.0, 102.0], "2026-03-02 14:00")])
    m = ld.measure_event(T0, T1, np.log(0.98), target)
    assert m is not None and m["censored"] is True


def test_an_opening_gap_in_the_source_is_not_an_event() -> None:
    # The price jumps 5% between bars but every bar is flat inside: the other listing's move arriving.
    source = pd.DataFrame({"open": [100.0, 105.0, 105.0], "close": [100.0, 105.0, 105.0]},
                          index=pd.date_range("2026-03-02", periods=3, freq="h", tz="UTC"))
    source["end"] = source.index + pd.Timedelta(hours=1)
    assert ld.find_events(source, sigma_fn=_flat_sigma).empty


def test_a_cluster_counts_once() -> None:
    source = _bars([100.0, 102.0, 104.0, 106.0], "2026-03-02 00:00")
    assert len(ld.find_events(source, sigma_fn=_flat_sigma)) == 1


def test_gjr_sigma_does_not_see_the_future() -> None:
    from trading_live_claude.risk.spectral_var import load_engine
    r = np.random.default_rng(1).standard_t(5, 900) * 0.01
    base = ld.gjr_sigma(pd.Series(r), engine=load_engine())
    shocked = r.copy()
    shocked[700] = 0.5
    after = ld.gjr_sigma(pd.Series(shocked), engine=load_engine())
    # Same up to and including bar 700: its own forecast cannot know the shock. Later bars do know.
    assert np.allclose(base[:701].dropna(), after[:701].dropna())
    assert after[701] > base[701]
    assert base[:504].isna().all()


def test_meeting_window_covers_the_decision_and_next_day() -> None:
    days = [date(2026, 3, 18)]
    for ts, want in (("2026-03-17 12:00", False), ("2026-03-18 12:00", True), ("2026-03-19 12:00", True),
                     ("2026-03-20 12:00", False)):
        assert ld.in_meeting_window(pd.Timestamp(ts, tz="UTC").to_pydatetime(), days) is want


def test_the_calendar_is_read_and_ecb_history_is_not_assumed() -> None:
    m = ld.load_meetings()
    assert date(2026, 10, 28) in m["FED"] and date(2026, 10, 30) in m["BOJ"] and date(2026, 10, 29) in m["ECB"]
    events = pd.DataFrame({"time": pd.to_datetime(["2026-03-02", "2026-06-02"], utc=True)})
    assert ld.covered_banks(events, m) == ["FED", "BOJ"]


def _synthetic(n: int = 120, effect: float = 1.5) -> tuple[pd.DataFrame, dict[str, list[date]]]:
    rng = np.random.default_rng(3)
    times = pd.date_range("2026-01-05", periods=n, freq="3D", tz="UTC")
    meetings = {"FED": [*(t.date() for t in times[::5]), times[-1].date()], "BOJ": [times[0].date(), times[-1].date()],
                "ECB": [times[0].date(), times[-1].date()]}
    fed = np.array([t.date() in meetings["FED"] for t in times])
    y = 0.5 + effect * fed + rng.normal(0, 0.3, n)
    return pd.DataFrame({"pair": "x", "direction": "asia_to_us", "time": times, "abs_z": rng.uniform(3, 6, n),
                         "closure_wait_h": rng.uniform(0, 12, n), "overlap": rng.random(n) > 0.5,
                         "reaction_h": np.expm1(y), "censored": False, "fed": fed,
                         "ecb": False, "boj": False}), meetings


def test_the_model_finds_a_planted_meeting_effect_and_beats_the_mean_out_of_sample() -> None:
    events, meetings = _synthetic()
    r = ld.fit_delay_model(events, meetings)
    assert r["status"] == "ok" and "fed" in r["coefficients"]
    assert r["coefficients"]["fed"] == pytest.approx(1.5, abs=0.2) and r["p_values"]["fed"] < 0.01
    assert r["oos_mae_h"] < r["oos_mae_mean_baseline_h"]
    assert ld.delay_correlations(events, meetings)["fed"]["rho"] > 0.3


def test_the_model_finds_nothing_when_nothing_is_planted() -> None:
    events, meetings = _synthetic(effect=0.0)
    assert ld.fit_delay_model(events, meetings)["p_values"]["fed"] > 0.05


def test_too_few_events_gets_no_estimate() -> None:
    events, meetings = _synthetic(n=10)
    assert ld.fit_delay_model(events, meetings) == {"status": "insufficient", "events": 10, "needed": ld.MIN_EVENTS}
    assert ld.delay_correlations(events, meetings)["status"] == "insufficient"


def test_high_vol_days_associate_with_meetings_only_when_they_do() -> None:
    index = pd.date_range("2026-01-05 14:00", periods=120, freq="D", tz="UTC")
    meetings = {"FED": [*(d.date() for d in index[::10]), index[-1].date()], "ECB": [], "BOJ": []}
    window = np.isin(np.arange(120) % 10, (0, 1)) | (np.arange(120) == 119)
    close = 100.0 * (1 + 0.02 * window)
    bars = pd.DataFrame({"open": 100.0, "close": close, "end": index}, index=index)
    r = ld.meeting_vol_association(bars, meetings, sigma_fn=_flat_sigma)
    assert list(r) == ["FED"] and r["FED"]["p"] < 0.001 and r["FED"]["odds_ratio"] > 10
