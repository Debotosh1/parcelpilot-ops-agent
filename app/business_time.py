"""Business-hours arithmetic.

The pack states SLA targets in "business hours" and "business days" but never
defines the working calendar. We therefore pin an explicit, configurable
assumption (09:00-18:00, Mon-Fri, Asia/Kolkata) in
`data/structured/rules/policy_rules.json` and surface it in every answer that
depends on it, rather than silently guessing.

All datetimes are naive and interpreted as Asia/Kolkata local time, matching
the dataset snapshot convention.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from dataclasses import dataclass


@dataclass(frozen=True)
class BusinessCalendar:
    start: time = time(9, 0)
    end: time = time(18, 0)
    working_weekdays: tuple[int, ...] = (0, 1, 2, 3, 4)  # Mon-Fri
    hours_per_business_day: float = 9.0
    timezone: str = "Asia/Kolkata"

    @classmethod
    def from_rules(cls, rules: dict) -> "BusinessCalendar":
        cal = (rules or {}).get("business_calendar", {})
        def _t(value: str, fallback: time) -> time:
            try:
                hh, mm = str(value).split(":")
                return time(int(hh), int(mm))
            except Exception:  # pragma: no cover - defensive
                return fallback
        return cls(
            start=_t(cal.get("start", "09:00"), time(9, 0)),
            end=_t(cal.get("end", "18:00"), time(18, 0)),
            working_weekdays=tuple(cal.get("working_weekdays", [0, 1, 2, 3, 4])),
            hours_per_business_day=float(cal.get("hours_per_business_day", 9)),
            timezone=str(cal.get("timezone", "Asia/Kolkata")),
        )

    # -- primitives --------------------------------------------------------
    def is_working_day(self, dt: datetime) -> bool:
        return dt.weekday() in self.working_weekdays

    def is_within_hours(self, dt: datetime) -> bool:
        return self.is_working_day(dt) and self.start <= dt.time() < self.end

    def _day_window(self, dt: datetime) -> tuple[datetime, datetime]:
        day = dt.date()
        return (
            datetime.combine(day, self.start),
            datetime.combine(day, self.end),
        )

    def next_open(self, dt: datetime) -> datetime:
        """First working instant at or after `dt`."""
        cursor = dt
        for _ in range(60):  # up to ~2 months of closures
            if self.is_working_day(cursor):
                open_at, close_at = self._day_window(cursor)
                if cursor < open_at:
                    return open_at
                if cursor < close_at:
                    return cursor
            cursor = datetime.combine(cursor.date() + timedelta(days=1), time(0, 0))
        raise RuntimeError("No working window found within 60 days")

    def add_business_minutes(self, start_dt: datetime, minutes: float) -> datetime:
        """Advance `minutes` of working time from `start_dt`."""
        remaining = float(minutes)
        cursor = self.next_open(start_dt)
        for _ in range(400):
            _, close_at = self._day_window(cursor)
            available = (close_at - cursor).total_seconds() / 60.0
            if remaining <= available:
                return cursor + timedelta(minutes=remaining)
            remaining -= available
            cursor = self.next_open(datetime.combine(cursor.date() + timedelta(days=1), time(0, 0)))
        raise RuntimeError("Business-minute addition did not converge")  # pragma: no cover

    def business_minutes_between(self, start_dt: datetime, end_dt: datetime) -> float:
        """Working minutes elapsed between two instants (0 if end <= start)."""
        if end_dt <= start_dt:
            return 0.0
        total = 0.0
        cursor = start_dt
        for _ in range(400):
            if cursor >= end_dt:
                break
            if not self.is_working_day(cursor):
                cursor = datetime.combine(cursor.date() + timedelta(days=1), time(0, 0))
                continue
            open_at, close_at = self._day_window(cursor)
            window_start = max(cursor, open_at)
            window_end = min(end_dt, close_at)
            if window_end > window_start:
                total += (window_end - window_start).total_seconds() / 60.0
            cursor = datetime.combine(cursor.date() + timedelta(days=1), time(0, 0))
        return total

    def add(self, start_dt: datetime, value: float, unit: str) -> datetime:
        """Add an SLA duration expressed in the pack's vocabulary."""
        unit = unit.lower()
        if unit in {"minutes", "minute"}:
            return start_dt + timedelta(minutes=value)
        if unit in {"hours", "hour"}:
            return start_dt + timedelta(hours=value)
        if unit in {"business_minutes"}:
            return self.add_business_minutes(start_dt, value)
        if unit in {"business_hours", "business_hour"}:
            return self.add_business_minutes(start_dt, value * 60)
        if unit in {"business_days", "business_day"}:
            return self.add_business_minutes(start_dt, value * self.hours_per_business_day * 60)
        raise ValueError(f"Unsupported SLA unit: {unit}")

    def elapsed(self, start_dt: datetime, end_dt: datetime, clock: str) -> float:
        """Elapsed minutes on either the 24x7 or the business clock."""
        if clock == "business":
            return self.business_minutes_between(start_dt, end_dt)
        return max(0.0, (end_dt - start_dt).total_seconds() / 60.0)


def humanize_minutes(minutes: float) -> str:
    minutes = int(round(minutes))
    if abs(minutes) < 60:
        return f"{minutes} min"
    hours, mins = divmod(abs(minutes), 60)
    sign = "-" if minutes < 0 else ""
    if hours < 24:
        return f"{sign}{hours}h {mins}m" if mins else f"{sign}{hours}h"
    days, rem_hours = divmod(hours, 24)
    return f"{sign}{days}d {rem_hours}h"
