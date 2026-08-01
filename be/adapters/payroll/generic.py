"""``GenericPayrollProfile`` — locale-neutral, config-driven pay math.

The OSS default and the fakes-only test profile. With its default construction it is
maximally simple: NO overtime tiering (every worked minute is regular, all multipliers
1.0, no weekly overtime) and a Sunday week start. Every threshold/multiplier is a
constructor argument, so a deployment (or a test) can dial in overtime bands without a
new subclass — the jurisdiction constants stay out of the domain code either way.
"""

from __future__ import annotations

from be.adapters.payroll.base import OvertimeBreakdown, PayrollProfile


class GenericPayrollProfile(PayrollProfile):
    """A minimal, dependency-free payroll profile with configurable overtime bands."""

    def __init__(
        self,
        *,
        daily_regular_cap: int | None = None,
        daily_tier1_cap: int | None = None,
        weekly_threshold: int | None = None,
        tier1_multiplier: float = 1.0,
        tier2_multiplier: float = 1.0,
        week_start_dow: int = 0,
        monthly_hours_fallback: float | None = None,
    ) -> None:
        """Configure the pay bands.

        * ``daily_regular_cap`` — minutes paid at 1.0 before tier1 begins; ``None`` =
          no daily overtime (everything regular).
        * ``daily_tier1_cap`` — tier1 minutes after the regular cap before tier2 begins;
          ``None`` = the remainder is all tier1 (no tier2).
        * ``weekly_threshold`` — weekly minutes above which extra tier1 is earned;
          ``None`` = no weekly overtime.
        """
        self._daily_regular_cap = daily_regular_cap
        self._daily_tier1_cap = daily_tier1_cap
        self._weekly_threshold = weekly_threshold
        self._tier1_multiplier = tier1_multiplier
        self._tier2_multiplier = tier2_multiplier
        self._week_start_dow = week_start_dow
        self._monthly_hours_fallback = monthly_hours_fallback

    def daily_overtime(self, worked_minutes: int) -> OvertimeBreakdown:
        worked = max(0, worked_minutes)
        if self._daily_regular_cap is None:
            return OvertimeBreakdown(worked, 0, 0)
        regular = min(worked, self._daily_regular_cap)
        after = max(worked - self._daily_regular_cap, 0)
        if self._daily_tier1_cap is None:
            return OvertimeBreakdown(regular, after, 0)
        tier1 = min(after, self._daily_tier1_cap)
        tier2 = max(after - self._daily_tier1_cap, 0)
        return OvertimeBreakdown(regular, tier1, tier2)

    def weekly_overtime(self, week_total_minutes: int, daily_ot_minutes: int) -> int:
        if self._weekly_threshold is None:
            return 0
        excess = max(week_total_minutes - self._weekly_threshold, 0)
        return max(excess - daily_ot_minutes, 0)

    def overtime_multipliers(self) -> dict[str, float]:
        return {
            "regular": 1.0,
            "tier1": self._tier1_multiplier,
            "tier2": self._tier2_multiplier,
        }

    def week_start_dow(self) -> int:
        return self._week_start_dow

    def monthly_hours_fallback(self) -> float | None:
        return self._monthly_hours_fallback


__all__ = ["GenericPayrollProfile"]
