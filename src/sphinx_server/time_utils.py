"""Helpers for converting UTC timestamps into localized display strings."""

from __future__ import annotations

from datetime import datetime, timezone


def format_local_datetime(dt: datetime | None, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """Return a localized string for a UTC timestamp.

    The input datetime objects in the database are stored as naive UTC values;
    this helper attaches the UTC timezone before converting them to the local
    timezone for display purposes.

    :param dt: Naive UTC datetime or timezone-aware datetime.
    :param fmt: ``strftime``-compatible format string.
    :returns: Local timezone string or ``"-"`` when ``dt`` is missing.
    """
    if not dt:
        return "-"
    aware = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    local_dt = aware.astimezone()  # Convert to system local timezone.
    return local_dt.strftime(fmt)

def convert_datetime_to_local(dt: datetime | None) -> datetime | None:
    """Convert a UTC datetime to local timezone.

    The input datetime objects in the database are stored as naive UTC values;
    this helper attaches the UTC timezone before converting them to the local
    timezone for display purposes.

    :param dt: Naive UTC datetime or timezone-aware datetime.
    :returns: Local timezone datetime or ``None`` when ``dt`` is missing.
    """
    if not dt:
        return None
    aware = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    local_dt = aware.astimezone()  # Convert to system local timezone.
    return local_dt