"""``PayrollProfile`` — the pay-computation jurisdiction seam.

Analogous to :class:`be.adapters.fiscal.base.FiscalProfile`: the profile owns the
locale-specific pay math so the payroll domain core stays locale-neutral. Concretely
it decides how a day's worked minutes split into a *regular* band and up to two
*overtime* tiers, how much additional weekly overtime a long week earns, what pay
multiplier each band carries, and which weekday a pay-week starts on (weekly-overtime
grouping). No jurisdiction constant ever lives in ``be.app.domains.payroll`` — only
here and in the concrete profiles.

The monthly-salary divisor is deliberately NOT owned here: it is data-driven per
``(year, month)`` via the ``monthly_working_hours`` table (scheduling domain). The
profile only supplies a :meth:`monthly_hours_fallback` for when that row is absent.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class OvertimeBreakdown:
    """A day's worked minutes split into pay bands (they sum to worked minutes).

    ``tier1``/``tier2`` are generic overtime bands (e.g. Israel's 125% / 150%); a
    locale with no overtime tiering returns everything as ``regular``.
    """

    regular_minutes: int
    tier1_minutes: int
    tier2_minutes: int


class PayrollProfile(ABC):
    """Vendor/jurisdiction-neutral pay-computation contract (pure, no I/O)."""

    @abstractmethod
    def daily_overtime(self, worked_minutes: int) -> OvertimeBreakdown:
        """Split a single day's *worked_minutes* into regular / tier1 / tier2 bands."""

    @abstractmethod
    def weekly_overtime(self, week_total_minutes: int, daily_ot_minutes: int) -> int:
        """Extra tier1 minutes a long week earns beyond the daily overtime already counted.

        *daily_ot_minutes* is the sum of that week's per-day ``tier1 + tier2`` so a
        locale never double-counts overtime already recognized daily.
        """

    @abstractmethod
    def overtime_multipliers(self) -> dict[str, float]:
        """Pay multiplier per band, keyed ``"regular"`` / ``"tier1"`` / ``"tier2"``."""

    @abstractmethod
    def week_start_dow(self) -> int:
        """Weekday a pay-week starts on, 0=Sun..6=Sat (weekly-overtime grouping)."""

    def monthly_hours_fallback(self) -> float | None:
        """Divisor for a monthly-salaried employee when the month has no configured hours.

        Default ``None`` = "cannot compute" (the domain then skips that employee's
        hourly derivation), matching the live behavior. A locale may override.
        """
        return None


__all__ = ["OvertimeBreakdown", "PayrollProfile"]
