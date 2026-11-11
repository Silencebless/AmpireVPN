"""Минимальные метрики в формате Prometheus (без зависимостей): счётчики, gauge и гистограммы.

Каждый процесс (api-реплики и worker) отдаёт свои значения на /metrics; Prometheus собирает их сам.
Задачи воркера (job_seconds, job_errors_total), платежи из воркера и panel_* живут в процессе worker —
его нужно скрейпить отдельным job (см. deploy/prometheus/prometheus.yml).
"""
from __future__ import annotations

from bisect import bisect_left
from typing import Any

Labels = tuple[tuple[str, str], ...]

# секундные корзины: от быстрых запросов к панели до долгих задач воркера
DEFAULT_BUCKETS: tuple[float, ...] = (0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0)


def _labels(labels: dict[str, Any]) -> Labels:
    return tuple(sorted((k, str(v)) for k, v in labels.items()))


def _esc(v: str) -> str:
    return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _fmt(name: str, labels: Labels, value: float) -> str:
    if labels:
        inner = ",".join(f'{k}="{_esc(v)}"' for k, v in labels)
        return f"{name}{{{inner}}} {value:g}"
    return f"{name} {value:g}"


class _Histogram:
    __slots__ = ("bounds", "counts", "total", "count")

    def __init__(self, bounds: tuple[float, ...]) -> None:
        self.bounds = bounds
        self.counts = [0] * (len(bounds) + 1)  # последняя корзина — «+Inf»
        self.total = 0.0
        self.count = 0

    def add(self, value: float) -> None:
        self.counts[bisect_left(self.bounds, value)] += 1
        self.total += value
        self.count += 1


class Metrics:
    def __init__(self, prefix: str = "ampire_") -> None:
        self.prefix = prefix
        self._counters: dict[str, dict[Labels, float]] = {}
        self._gauges: dict[str, dict[Labels, float]] = {}
        self._hists: dict[str, dict[Labels, _Histogram]] = {}

    def inc(self, name: str, value: float = 1.0, **labels: Any) -> None:
        bucket = self._counters.setdefault(name, {})
        key = _labels(labels)
        bucket[key] = bucket.get(key, 0.0) + value

    def set_gauge(self, name: str, value: float, **labels: Any) -> None:
        self._gauges.setdefault(name, {})[_labels(labels)] = float(value)

    def observe(self, name: str, value: float, *, buckets: tuple[float, ...] | None = None, **labels: Any) -> None:
        series = self._hists.setdefault(name, {})
        key = _labels(labels)
        hist = series.get(key)
        if hist is None:
            hist = series[key] = _Histogram(buckets or DEFAULT_BUCKETS)
        hist.add(value)

    def render(self) -> str:
        out: list[str] = []
        p = self.prefix
        for name, series in sorted(self._counters.items()):
            out.append(f"# TYPE {p}{name} counter")
            out += [_fmt(p + name, k, v) for k, v in sorted(series.items())]
        for name, series in sorted(self._gauges.items()):
            out.append(f"# TYPE {p}{name} gauge")
            out += [_fmt(p + name, k, v) for k, v in sorted(series.items())]
        for name, series in sorted(self._hists.items()):
            out.append(f"# TYPE {p}{name} histogram")
            for k, h in sorted(series.items()):
                cum = 0
                for bound, c in zip(h.bounds, h.counts, strict=False):
                    cum += c
                    out.append(_fmt(f"{p}{name}_bucket", (*k, ("le", f"{bound:g}")), cum))
                out.append(_fmt(f"{p}{name}_bucket", (*k, ("le", "+Inf")), h.count))
                out.append(_fmt(f"{p}{name}_sum", k, h.total))
                out.append(_fmt(f"{p}{name}_count", k, h.count))
        return "\n".join(out) + "\n"


metrics = Metrics()
