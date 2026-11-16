"""Half-open interval algebra and survey-day helpers.

All persisted timestamps are timezone-aware UTC. Survey days, required diary
windows and program slots are converted to absolute instants in the service
timezone before any intersection is taken.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc


class TimeRangeError(ValueError):
    pass


def parse_hhmm(value: str) -> int:
    """Parse 'HH:MM' allowing hour >= 24 to denote the next day."""
    try:
        hours, minutes = value.split(":")
        h, m = int(hours), int(minutes)
    except (AttributeError, ValueError) as exc:
        raise TimeRangeError(f"invalid HH:MM value: {value!r}") from exc
    if h < 0 or m < 0 or m >= 60:
        raise TimeRangeError(f"invalid HH:MM value: {value!r}")
    return h * 60 + m


def local_instant(day: date | str, minute_offset: int, tz: ZoneInfo) -> datetime:
    """Local date + minutes-from-midnight -> aware UTC instant."""
    if isinstance(day, str):
        day = date.fromisoformat(day)
    base = datetime.combine(day, time(0, 0), tzinfo=tz)
    return (base + timedelta(minutes=minute_offset)).astimezone(UTC)


def parse_aware(value: str | datetime, field: str = "timestamp") -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise TimeRangeError(f"invalid ISO timestamp for {field}: {value!r}") from exc
    if dt.tzinfo is None:
        raise TimeRangeError(f"{field} must carry a UTC offset: {value!r}")
    return dt.astimezone(UTC)


def require_interval(start: datetime, end: datetime) -> None:
    if end <= start:
        raise TimeRangeError("interval end must be strictly after start")


def clip(start: datetime, end: datetime, lo: datetime, hi: datetime) -> tuple[datetime, datetime] | None:
    s, e = max(start, lo), min(end, hi)
    if e <= s:
        return None
    return (s, e)


def overlap_length(start: datetime, end: datetime, lo: datetime, hi: datetime) -> float:
    seg = clip(start, end, lo, hi)
    return 0.0 if seg is None else (seg[1] - seg[0]).total_seconds() / 60.0


def merge(intervals: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    ordered = sorted((s, e) for s, e in intervals if e > s)
    merged: list[list[datetime]] = []
    for s, e in ordered:
        if merged and s <= merged[-1][1]:
            if e > merged[-1][1]:
                merged[-1][1] = e
        else:
            merged.append([s, e])
    return [(s, e) for s, e in merged]  # type: ignore[misc]


def subtract(
    intervals: list[tuple[datetime, datetime]],
    cuts: list[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime]]:
    result = merge(intervals)
    for cs, ce in merge(cuts):
        next_result: list[tuple[datetime, datetime]] = []
        for s, e in result:
            if ce <= s or cs >= e:
                next_result.append((s, e))
                continue
            if s < cs:
                next_result.append((s, cs))
            if ce < e:
                next_result.append((ce, e))
        result = next_result
    return result


def length_minutes(intervals: list[tuple[datetime, datetime]]) -> float:
    return sum((e - s).total_seconds() / 60.0 for s, e in intervals)


def missing_segments(
    window: tuple[datetime, datetime],
    covered: list[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime]]:
    return subtract([window], covered)


def survey_day_of(instant: datetime, tz: ZoneInfo) -> date:
    return instant.astimezone(tz).date()


def day_window(day: date, start_minute: int, end_minute: int, tz: ZoneInfo) -> tuple[datetime, datetime]:
    if end_minute <= start_minute:
        raise TimeRangeError("daily window end must be after start (use >=24:00 for cross-midnight)")
    return (local_instant(day, start_minute, tz), local_instant(day, end_minute, tz))


def slot_interval(
    air_date: date,
    start_local: str,
    end_local: str,
    tz: ZoneInfo,
) -> tuple[datetime, datetime]:
    start_min = parse_hhmm(start_local)
    end_min = parse_hhmm(end_local)
    if end_min <= start_min:
        end_min += 24 * 60
    return (local_instant(air_date, start_min, tz), local_instant(air_date, end_min, tz))


def split_by_survey_day(
    start: datetime,
    end: datetime,
    tz: ZoneInfo,
    days: list[date],
) -> dict[date, float]:
    """Minutes of [start, end) falling inside each requested survey day's midnight-to-midnight."""
    result: dict[date, float] = {}
    for day in days:
        lo = local_instant(day, 0, tz)
        hi = local_instant(day, 24 * 60, tz)
        minutes = overlap_length(start, end, lo, hi)
        if minutes > 0:
            result[day] = minutes
    return result


def iso(dt: datetime | date | None) -> str | None:
    if dt is None:
        return None
    if isinstance(dt, datetime):
        return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return dt.isoformat()


def fmt_ranges(ranges: list[tuple[datetime, datetime]], tz: ZoneInfo) -> list[dict[str, str]]:
    return [
        {"start": s.astimezone(tz).isoformat(), "end": e.astimezone(tz).isoformat()}
        for s, e in ranges
    ]
