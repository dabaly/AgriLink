"""Date presentation helpers for AgriLink's Kenya-facing pages."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

NAIROBI = ZoneInfo("Africa/Nairobi")


def format_nairobi_datetime(value: datetime | None) -> str:
    if value is None:
        return ""
    value = value.replace(tzinfo=UTC) if value.tzinfo is None else value
    return value.astimezone(NAIROBI).strftime("%d %b %Y, %H:%M EAT")
