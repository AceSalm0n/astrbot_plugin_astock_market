"""Use verified exchange calendars for dated market features."""

import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

BEIJING_TZ = timezone(timedelta(hours=8))


@dataclass(frozen=True)
class TradingCalendar:
    """Store verified annual weekday closures; uncovered years are unknown."""

    years: dict[int, frozenset[date]]

    def session_dates(self, start: date, end: date) -> tuple[date, ...]:
        """List sessions without treating missing annual data as open days.

        Args:
            start: Inclusive first date.
            end: Inclusive last date.

        Returns:
            Known trading dates in chronological order.

        Raises:
            ValueError: Any requested year is not verified.
        """
        if any(year not in self.years for year in range(start.year, end.year + 1)):
            raise ValueError("Trading calendar year is not covered")
        return tuple(
            current
            for offset in range(max(0, (end - start).days + 1))
            if (current := start + timedelta(days=offset)).weekday() < 5
            and current not in self.years[current.year]
        )

    def is_session(self, day: date) -> bool:
        """Return whether the covered date is an exchange session."""
        return bool(self.session_dates(day, day))


def load_trading_calendar(path: Path) -> TradingCalendar:
    """Read only explicit, sourced annual closures; never fill unknown years.

    Args:
        path: Bundled annual calendar JSON.

    Returns:
        Validated common Shanghai/Shenzhen calendar.

    Raises:
        ValueError: Metadata or closure dates are invalid.
        OSError: The calendar cannot be read.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1 or not isinstance(data.get("years"), dict):
        raise ValueError("Invalid trading calendar schema")
    years = {}
    for raw_year, annual in data["years"].items():
        year = int(raw_year)
        if not annual.get("sources") or not isinstance(
            annual.get("closed_weekdays"), list
        ):
            raise ValueError("Missing annual calendar metadata")
        date.fromisoformat(annual["verified_at"])
        days = frozenset(
            date.fromisoformat(value) for value in annual["closed_weekdays"]
        )
        if any(day.year != year or day.weekday() >= 5 for day in days):
            raise ValueError("Invalid weekday closure")
        years[year] = days
    if not years:
        raise ValueError("Empty trading calendar")
    return TradingCalendar(years)


def resolve_baseline(now: datetime, calendar: TradingCalendar) -> date | None:
    """Resolve the required daily baseline with a 15:15 source-update buffer.

    Args:
        now: Aware query time.
        calendar: Verified annual dates.

    Returns:
        Last required completed session, or None if coverage is unknown.
    """
    if now.tzinfo is None:
        return None
    local = now.astimezone(BEIJING_TZ)
    current = local.date()
    if current.year not in calendar.years:
        return None
    if local.time() < time(15, 15):
        current -= timedelta(days=1)
    for _ in range(370):
        try:
            if calendar.session_dates(current, current):
                return current
        except ValueError:
            return None
        current -= timedelta(days=1)
    return None
