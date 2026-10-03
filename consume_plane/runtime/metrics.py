"""In-process counters and gauges."""
from __future__ import annotations

from collections import defaultdict


def _key(name: str, labels: dict[str, str]) -> tuple[str, tuple[tuple[str, str], ...]]:
    return name, tuple(sorted(labels.items()))


class Metrics:
    def __init__(self):
        self._counters: dict = defaultdict(float)
        self._gauges: dict = {}

    def inc(self, name: str, by: float = 1.0, **labels: str) -> None:
        self._counters[_key(name, labels)] += by

    def set(self, name: str, value: float, **labels: str) -> None:
        self._gauges[_key(name, labels)] = value

    def get(self, name: str, **labels: str) -> float | None:
        k = _key(name, labels)
        return self._counters.get(k, self._gauges.get(k))

    def snapshot(self) -> list[dict]:
        rows = [{"type": "counter", "name": n, "labels": dict(l), "value": v} for (n, l), v in self._counters.items()]
        rows += [{"type": "gauge", "name": n, "labels": dict(l), "value": v} for (n, l), v in self._gauges.items()]
        return sorted(rows, key=lambda r: (r["name"], sorted(r["labels"].items())))
