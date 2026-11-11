"""JSON-кодек для кэша: умеет Decimal и datetime (без внешних зависимостей)."""
from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from typing import Any


def _default(o: Any) -> Any:
    if isinstance(o, Decimal):
        return {"__d": str(o)}
    if isinstance(o, datetime):
        return {"__t": o.isoformat()}
    if isinstance(o, (set, frozenset)):
        return sorted(o)
    raise TypeError(f"не сериализуется: {type(o).__name__}")


def _hook(d: dict) -> Any:
    if len(d) == 1:
        if "__d" in d:
            return Decimal(d["__d"])
        if "__t" in d:
            return datetime.fromisoformat(d["__t"])
    return d


def dumps(obj: Any) -> str:
    return json.dumps(obj, default=_default, ensure_ascii=False, separators=(",", ":"))


def loads(raw: str | bytes) -> Any:
    return json.loads(raw, object_hook=_hook)
