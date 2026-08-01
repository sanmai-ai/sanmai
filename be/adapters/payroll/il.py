"""``IsraeliPayrollProfile`` — the Israeli labor-law pay math.

This is the ONLY module that encodes Israeli overtime rules, kept out of the domain
core behind :class:`be.adapters.payroll.base.PayrollProfile`:

* Daily: 0-8h (0-480 min) = 100%; the next 2h (480-600 min) = 125%; beyond 600 min
  = 150%.
* Weekly (Sun-Sat): a week over 42h (2520 min) earns additional 125% on the excess
  that was not already counted as daily overtime.
* The pay-week starts on Sunday (0).

Selected via ``SANMAI_PAYROLL_PROFILE=il``; the generic profile is otherwise the
locale-neutral default.
"""

from __future__ import annotations

from be.adapters.payroll.generic import GenericPayrollProfile

# Israeli labor-law constants (minutes) — live only here.
_DAILY_REGULAR_CAP = 480  # 8h at 100%
_DAILY_TIER1_CAP = 120  # next 2h (480-600) at 125%
_WEEKLY_THRESHOLD = 2520  # 42h/week before additional weekly 125%


class IsraeliPayrollProfile(GenericPayrollProfile):
    """Israeli overtime tiers/thresholds, expressed via the generic config seam."""

    def __init__(self) -> None:
        super().__init__(
            daily_regular_cap=_DAILY_REGULAR_CAP,
            daily_tier1_cap=_DAILY_TIER1_CAP,
            weekly_threshold=_WEEKLY_THRESHOLD,
            tier1_multiplier=1.25,
            tier2_multiplier=1.5,
            week_start_dow=0,  # Sunday
        )


__all__ = ["IsraeliPayrollProfile"]
