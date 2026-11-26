"""Rest.li 2.0 encoding for query parameters.

Objects become (key:value,...), lists List(a,b); strings are percent-encoded,
including the reserved characters ( ) , ' : so URNs survive.
"""

from __future__ import annotations

from urllib.parse import quote


def _str(value: str) -> str:
    return quote(value, safe="")


def encode(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return _str(value)
    if isinstance(value, dict):
        return "(" + ",".join(f"{_str(str(k))}:{encode(v)}" for k, v in value.items()) + ")"
    if isinstance(value, (list, tuple)):
        return "List(" + ",".join(encode(v) for v in value) + ")"
    raise TypeError(f"cannot encode {type(value).__name__}")


def query(params: dict) -> str:
    """Query string; dict and list values get Rest.li encoding, None is skipped."""
    return "&".join(f"{_str(k)}={encode(v)}" for k, v in params.items() if v is not None)


def date_range(start: str, end: str) -> dict:
    """'2026-11-01', '2026-11-30' -> Rest.li dateRange object."""

    def d(s: str) -> dict:
        y, m, day = (int(x) for x in s.split("-"))
        return {"year": y, "month": m, "day": day}

    return {"start": d(start), "end": d(end)}
