"""JSON helpers that keep Decimals/datetimes serialisable in JSONB."""
from __future__ import annotations

import json
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

from psycopg.types.json import Jsonb


def _default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, set):
        return sorted(value)
    raise TypeError(f"not JSON serialisable: {type(value)!r}")


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=_default, sort_keys=True, separators=(",", ":"))


def loads(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (dict, list, int, float, bool)):
        return value
    return json.loads(value)


def jb(value: Any) -> Jsonb:
    return Jsonb(value, dumps=dumps)


def canonical_hash(value: Any) -> str:
    import hashlib

    return hashlib.sha256(dumps(value).encode("utf-8")).hexdigest()
