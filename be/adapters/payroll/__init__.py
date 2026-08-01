"""Payroll profiles — the jurisdiction seam for pay computation.

A :class:`PayrollProfile` owns ONLY the locale-specific pay math (daily/weekly
overtime tiering + multipliers + which weekday starts a week). The payroll domain
core is locale-neutral and computes money by calling the injected profile, so no
labor-law constants ever live in ``be.app.domains.payroll``.
"""
