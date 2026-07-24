"""Unit tests for the pure salience math (:mod:`lithos.lcma.salience`).

These pin the two invariants the recalibration (task ``e7d8ef60``) depends on:
decay erodes toward a non-zero floor rather than to zero, and the usage signal is a
bounded, monotonic, non-decaying function of the live counters.
"""

import math

import pytest

from lithos.lcma.salience import (
    DEFAULT_SALIENCE,
    decay_amount,
    recalibration_eligible,
    usage_score,
)

# Representative decay params (mirror the LcmaConfig defaults).
_DECAY = {"per_day": 0.005, "daily_cap": 0.1, "floor": 0.3}
_USAGE = {
    "freq_weight": 0.6,
    "recency_weight": 0.4,
    "recency_halflife_days": 14.0,
    "freq_norm_k": 20.0,
}


class TestDecayAmount:
    def test_linear_below_cap(self):
        # 14 idle days * 0.005 = 0.07, below the 0.1 cap and within headroom.
        assert decay_amount(0.5, 14, **_DECAY) == pytest.approx(0.07)

    def test_capped_per_application(self):
        # 100 idle days would be 0.5 linear, but the daily cap is 0.1 and headroom
        # (0.5 - 0.3) is 0.2, so the cap binds.
        assert decay_amount(0.5, 100, **_DECAY) == pytest.approx(0.1)

    def test_never_decays_below_floor(self):
        # A node just above the floor loses only the remaining headroom.
        assert decay_amount(0.34, 100, **_DECAY) == pytest.approx(0.04)

    def test_zero_at_floor(self):
        assert decay_amount(0.3, 100, **_DECAY) == 0.0

    def test_zero_below_floor(self):
        # Already pushed below the floor by explicit feedback — decay adds nothing.
        assert decay_amount(0.1, 100, **_DECAY) == 0.0

    def test_headroom_binds_before_cap(self):
        # 0.32 has only 0.02 of headroom; even a large idle count can't exceed it.
        assert decay_amount(0.32, 50, **_DECAY) == pytest.approx(0.02)

    def test_negative_days_clamped(self):
        assert decay_amount(0.5, -3, **_DECAY) == 0.0


class TestUsageScore:
    def test_bounded_unit_interval(self):
        for rc in (0, 1, 5, 50, 5000):
            for d in (None, 0.0, 3.0, 100.0):
                s = usage_score(rc, d, **_USAGE)
                assert 0.0 <= s <= 1.0

    def test_monotonic_in_retrieval_count(self):
        prev = -1.0
        for rc in (0, 1, 2, 5, 20, 100):
            s = usage_score(rc, 0.0, **_USAGE)
            assert s >= prev
            prev = s

    def test_decreasing_in_recency(self):
        prev = 2.0
        for d in (0.0, 7.0, 14.0, 28.0, 100.0):
            s = usage_score(10, d, **_USAGE)
            assert s <= prev
            prev = s

    def test_never_used_has_no_recency(self):
        # None recency == same as an infinitely-old last use (recency term -> 0).
        assert usage_score(10, None, **_USAGE) < usage_score(10, 0.0, **_USAGE)

    def test_recency_halves_at_halflife(self):
        # Pure recency contribution (zero frequency weight) halves at the half-life.
        params = {**_USAGE, "freq_weight": 0.0, "recency_weight": 1.0}
        at_zero = usage_score(0, 0.0, **params)
        at_halflife = usage_score(0, 14.0, **params)
        assert math.isclose(at_halflife, at_zero / 2, rel_tol=1e-9)

    def test_zero_when_cold_and_unused(self):
        assert usage_score(0, None, **_USAGE) == 0.0


class TestRecalibrationEligible:
    def test_below_floor_no_feedback_is_eligible(self):
        assert recalibration_eligible(0.05, 0.3, misleading_count=0, ignored_count=0, cited_count=0)

    def test_at_or_above_floor_not_eligible(self):
        assert not recalibration_eligible(
            0.3, 0.3, misleading_count=0, ignored_count=0, cited_count=0
        )
        assert not recalibration_eligible(
            0.9, 0.3, misleading_count=0, ignored_count=0, cited_count=0
        )

    def test_misleading_is_protected(self):
        assert not recalibration_eligible(
            0.05, 0.3, misleading_count=1, ignored_count=0, cited_count=0
        )

    def test_chronic_ignored_is_protected(self):
        assert not recalibration_eligible(
            0.05, 0.3, misleading_count=0, ignored_count=6, cited_count=0
        )

    def test_light_ignore_is_eligible(self):
        assert recalibration_eligible(0.05, 0.3, misleading_count=0, ignored_count=2, cited_count=0)

    def test_ignored_but_more_cited_is_eligible(self):
        # ignored 6 but cited 7 -> not chronic (ignored not > cited) -> still eligible.
        assert recalibration_eligible(0.05, 0.3, misleading_count=0, ignored_count=6, cited_count=7)


def test_default_salience_constant():
    assert DEFAULT_SALIENCE == 0.5
