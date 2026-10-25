from __future__ import annotations

import csv
import io
import json

import pytest

from cloudcost.checks.nat import estimate
from cloudcost.models import Finding
from cloudcost.pricing import PriceBook
from cloudcost.report import findings_report, nat_report, render

FINDINGS = [
    Finding(
        "aws", "idle-ebs", "vol-big", "us-east-1a", "gp3 10000 GiB", 1234.5, {"size_gib": 10000}
    ),
    Finding("aws", "idle-ebs", "vol-odd", "us-east-1b", "gp9 5 GiB", None),
]


def _render(report, fmt: str) -> str:
    out = io.StringIO()
    render(report, fmt, out)
    return out.getvalue()


def test_table_shows_costs_totals_and_notes() -> None:
    text = _render(findings_report("AWS: test", FINDINGS, "USD", ["check prices"]), "table")
    lines = text.splitlines()

    assert lines[0] == "AWS: test"
    header = next(line for line in lines if line.startswith("Resource"))
    assert "Est. monthly (USD)" in header
    assert "Provider" not in header  # JSON/CSV-only column
    assert any("vol-big" in line and line.endswith("1,234.50") for line in lines)
    assert any("vol-odd" in line and line.endswith("n/a") for line in lines)
    assert "2 items; estimated monthly cost: 1,234.50 USD" in text
    assert "  - check prices" in text


def test_table_columns_are_aligned() -> None:
    lines = _render(findings_report("t", FINDINGS, "USD"), "table").splitlines()
    rule = next(line for line in lines if line.startswith("--"))
    rows = [line for line in lines if line.startswith("vol-")]
    assert all(len(row) == len(rule) for row in rows)  # right-aligned money column


def test_empty_table_says_so() -> None:
    text = _render(findings_report("AWS: nothing", [], "USD"), "table")
    assert "Nothing found." in text
    assert "items" not in text


def test_json_is_machine_readable() -> None:
    payload = json.loads(_render(findings_report("t", FINDINGS, "USD", ["n"]), "json"))
    assert payload["count"] == 2
    assert payload["total"] == 1234.5
    assert payload["notes"] == ["n"]
    assert payload["rows"][0]["details"] == {"size_gib": 10000}
    assert payload["rows"][1]["monthly_cost"] is None


def test_csv_has_stable_header_and_blank_unknown_costs() -> None:
    rows = list(csv.DictReader(io.StringIO(_render(findings_report("t", FINDINGS, "USD"), "csv"))))
    assert list(rows[0]) == [
        "provider",
        "check",
        "resource_id",
        "location",
        "summary",
        "monthly_cost",
    ]
    assert [r["monthly_cost"] for r in rows] == ["1234.5", ""]


def test_nat_report_totals_savings(book: PriceBook) -> None:
    prices = book.aws("us-east-1")
    report = nat_report([estimate(1000, 0.6, prices), estimate(200, 0.5, prices)], "USD")
    assert report.total == pytest.approx(27.0 + 4.5)

    text = _render(report, "table")
    assert "estimated monthly savings: 31.50 USD" in text
    assert "60%" in text
    assert json.loads(_render(report, "json"))["rows"][0]["monthly_savings"] == 27.0
