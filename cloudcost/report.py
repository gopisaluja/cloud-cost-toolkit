"""Render results as an aligned text table, JSON, or CSV."""

from __future__ import annotations

import csv
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, TextIO

from cloudcost.checks.nat import NatEndpointEstimate
from cloudcost.models import Finding, total_cost

Format = Literal["table", "json", "csv"]
FORMATS: tuple[Format, ...] = ("table", "json", "csv")


@dataclass(frozen=True)
class Column:
    key: str
    header: str
    kind: Literal["text", "money", "number", "percent"] = "text"
    in_table: bool = True  # JSON and CSV always include every column


@dataclass
class Report:
    title: str
    columns: Sequence[Column]
    rows: list[dict[str, Any]]
    currency: str = "USD"
    total: float | None = None
    total_label: str = "estimated monthly cost"
    notes: list[str] = field(default_factory=list)


FINDING_COLUMNS = (
    Column("provider", "Provider", in_table=False),
    Column("check", "Check", in_table=False),
    Column("resource_id", "Resource"),
    Column("location", "Location"),
    Column("summary", "Details"),
    Column("monthly_cost", "Est. monthly", "money"),
)

NAT_COLUMNS = (
    Column("nat_gateway_id", "NAT gateway"),
    Column("region", "Region"),
    Column("gb_per_month", "GB/month", "number"),
    Column("endpoint_share", "Endpoint share", "percent"),
    Column("price_per_gb", "Price/GB", "number", in_table=False),
    Column("current_processing_cost", "Processing now", "money"),
    Column("monthly_savings", "Est. savings", "money"),
    Column("remaining_processing_cost", "Processing after", "money"),
    Column("nat_hourly_monthly", "NAT hourly", "money"),
)


def findings_report(
    title: str, findings: Sequence[Finding], currency: str, notes: Iterable[str] = ()
) -> Report:
    return Report(
        title=title,
        columns=FINDING_COLUMNS,
        rows=[f.as_dict() for f in findings],
        currency=currency,
        total=total_cost(findings),
        notes=list(notes),
    )


def nat_report(
    estimates: Sequence[NatEndpointEstimate], currency: str, notes: Iterable[str] = ()
) -> Report:
    return Report(
        title="AWS: NAT gateway processing vs. S3/DynamoDB gateway endpoints",
        columns=NAT_COLUMNS,
        rows=[e.as_dict() for e in estimates],
        currency=currency,
        total=sum(e.monthly_savings for e in estimates),
        total_label="estimated monthly savings",
        notes=list(notes),
    )


def render(report: Report, fmt: Format, out: TextIO) -> None:
    if fmt == "json":
        _render_json(report, out)
    elif fmt == "csv":
        _render_csv(report, out)
    elif fmt == "table":
        _render_table(report, out)
    else:  # pragma: no cover - guarded by the CLI's Choice type
        raise ValueError(f"unknown format: {fmt}")


def _render_json(report: Report, out: TextIO) -> None:
    payload = {
        "report": report.title,
        "currency": report.currency,
        "count": len(report.rows),
        "total": None if report.total is None else round(report.total, 2),
        "total_label": report.total_label,
        "rows": report.rows,
        "notes": report.notes,
    }
    json.dump(payload, out, indent=2, default=str)
    out.write("\n")


def _render_csv(report: Report, out: TextIO) -> None:
    keys = [c.key for c in report.columns]
    writer = csv.DictWriter(out, fieldnames=keys, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for row in report.rows:
        writer.writerow({k: "" if row.get(k) is None else row.get(k) for k in keys})


def _cell(value: Any, kind: str) -> str:
    if value is None:
        return "n/a" if kind == "money" else "-"
    if kind == "money":
        return f"{value:,.2f}"
    if kind == "number":
        return f"{value:,.1f}"
    if kind == "percent":
        return f"{value:.0%}"
    return str(value)


def _render_table(report: Report, out: TextIO) -> None:
    out.write(f"{report.title}\n\n")
    if not report.rows:
        out.write("Nothing found.\n")
    else:
        columns = [c for c in report.columns if c.in_table]
        headers = [
            f"{c.header} ({report.currency})" if c.kind == "money" else c.header for c in columns
        ]
        body = [[_cell(row.get(c.key), c.kind) for c in columns] for row in report.rows]
        widths = [max(len(h), *(len(r[i]) for r in body)) for i, h in enumerate(headers)]

        def line(cells: Sequence[str]) -> str:
            padded = [
                cell.rjust(w) if col.kind != "text" else cell.ljust(w)
                for cell, w, col in zip(cells, widths, columns, strict=True)
            ]
            return "  ".join(padded).rstrip() + "\n"

        out.write(line(headers))
        out.write("  ".join("-" * w for w in widths) + "\n")
        for cells in body:
            out.write(line(cells))
        count = len(report.rows)
        out.write(f"\n{count} item{'s' if count != 1 else ''}")
        if report.total is not None:
            out.write(f"; {report.total_label}: {report.total:,.2f} {report.currency}")
        out.write("\n")
    if report.notes:
        out.write("\nNotes:\n")
        for note in report.notes:
            out.write(f"  - {note}\n")
