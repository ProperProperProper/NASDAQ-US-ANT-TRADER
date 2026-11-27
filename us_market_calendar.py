"""Small dependency-free US equity-session calendar for runtime safety."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    day = date(year, month, 1)
    day += timedelta(days=(weekday - day.weekday()) % 7 + 7 * (n - 1))
    return day


def _last_weekday(year: int, month: int, weekday: int) -> date:
    day = date(year + (month == 12), 1 if month == 12 else month + 1, 1) - timedelta(days=1)
    return day - timedelta(days=(day.weekday() - weekday) % 7)


def _observed(day: date) -> date:
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def good_friday(year: int) -> date:
    """Return Good Friday using the Gregorian computus."""
    a = year % 19; b = year // 100; c = year % 100; d = b // 4; e = b % 4
    f = (b + 8) // 25; g = (b - f + 1) // 3; h = (19 * a + b - d - g + 15) % 30
    i = c // 4; k = c % 4; l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451; month = (h + l - 7 * m + 114) // 31
    easter = date(year, month, (h + l - 7 * m + 114) % 31 + 1)
    return easter - timedelta(days=2)


def closed_dates(year: int) -> set[date]:
    return {
        _observed(date(year, 1, 1)),
        _nth_weekday(year, 1, 0, 3),
        _nth_weekday(year, 2, 0, 3),
        good_friday(year),
        _last_weekday(year, 5, 0),
        _observed(date(year, 6, 19)),
        _observed(date(year, 7, 4)),
        _nth_weekday(year, 9, 0, 1),
        _nth_weekday(year, 11, 3, 4),
        _observed(date(year, 12, 25)),
    }


def early_close_dates(year: int) -> set[date]:
    thanksgiving = _nth_weekday(year, 11, 3, 4)
    return {thanksgiving + timedelta(days=1), date(year, 12, 24)} - closed_dates(year)


def session_close(day: date) -> time | None:
    if day in closed_dates(day.year) or day.weekday() >= 5:
        return None
    return time(13, 0) if day in early_close_dates(day.year) else time(16, 0)


def session_phase(day: date, clock: time) -> str:
    close = session_close(day)
    if close is None:
        return "CLOSED"
    if clock < time(7, 0):
        return "OFF_HOURS"
    if clock < time(9, 30):
        return "PREMARKET"
    if clock < close:
        return "RTH"
    return "CLOSED"


def session_exit_time(day: date) -> time | None:
    close = session_close(day)
    if close is None:
        return None
    return (datetime.combine(date(2000, 1, 1), close) - timedelta(minutes=30)).time()
