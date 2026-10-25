"""Provider-neutral result types produced by the checks."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Finding:
    """A single resource that looks like waste.

    ``monthly_cost`` is ``None`` when no price is known (for example an unfamiliar volume
    type); such findings are still reported so nothing is silently dropped.
    """

    provider: str
    check: str
    resource_id: str
    location: str
    summary: str
    monthly_cost: float | None
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "check": self.check,
            "resource_id": self.resource_id,
            "location": self.location,
            "summary": self.summary,
            "monthly_cost": None if self.monthly_cost is None else round(self.monthly_cost, 2),
            "details": dict(self.details),
        }


def sort_by_cost(findings: Iterable[Finding]) -> list[Finding]:
    """Most expensive first; unpriced findings last, then stable by location/resource id."""
    return sorted(
        findings,
        key=lambda f: (
            f.monthly_cost is None,
            -(f.monthly_cost or 0.0),
            f.location,
            f.resource_id,
        ),
    )


def total_cost(findings: Iterable[Finding]) -> float:
    """Sum of all known monthly costs (unpriced findings contribute nothing)."""
    return sum(f.monthly_cost for f in findings if f.monthly_cost is not None)
