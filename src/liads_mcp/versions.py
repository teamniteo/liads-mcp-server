"""The pinned LinkedIn-Version and its sunset.

LinkedIn supports each monthly version for at least a year
(https://learn.microsoft.com/en-us/linkedin/marketing/versioning).
"""

from __future__ import annotations

from datetime import date

DEFAULT_VERSION = "202610"
WARN_DAYS = 60


def sunset(version: str) -> date:
    year, month = int(version[:4]), int(version[4:])
    return date(year + 1, month, 15)


def version_status(version: str, today: date | None = None) -> dict:
    today = today or date.today()
    ends = sunset(version)
    days = (ends - today).days
    if days < 0:
        state = "sunset"
    elif days <= WARN_DAYS:
        state = "ends soon"
    else:
        state = "ok"
    return {"version": version, "supported_until": ends.isoformat(), "days_left": days, "state": state}
