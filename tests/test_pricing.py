from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from cloudcost.errors import PricingError
from cloudcost.pricing import PriceBook


def test_builtin_price_list_is_complete_and_flagged_as_example(book: PriceBook) -> None:
    assert "verify" in book.note.lower()
    assert book.currency == "USD"
    for region in ("us-east-1", "eu-west-1", "ap-south-1"):
        prices = book.aws(region)
        assert prices.region == region
        assert {"gp2", "gp3", "io1", "io2", "st1", "sc1"} <= set(prices.ebs_gb_month)
        assert prices.nat_gateway_gb
        assert prices.nat_gateway_hour
        assert prices.public_ipv4_hour
    for region in ("us-central1", "europe-west1", "asia-south1"):
        assert {"pd-standard", "pd-balanced", "pd-ssd"} <= set(book.gcp(region).disk_gb_month)
    assert book.warnings == []


@pytest.mark.parametrize(
    ("volume_type", "size", "iops", "throughput", "expected"),
    [
        pytest.param("gp3", 500, 3000, 125, 40.0, id="gp3-baseline-performance-is-free"),
        # 500 x 0.08 + (6000 - 3000) x 0.005 + (250 - 125) x 0.04
        pytest.param("gp3", 500, 6000, 250, 60.0, id="gp3-extra-iops-and-throughput"),
        pytest.param("gp3", 100, 2000, 100, 8.0, id="gp3-below-baseline-never-negative"),
        pytest.param("gp2", 100, 300, None, 10.0, id="gp2-iops-included-in-storage"),
        # 100 x 0.125 + 1000 x 0.065: every provisioned io2 IOPS is billed
        pytest.param("io2", 100, 1000, None, 77.5, id="io2-all-iops-billed"),
        pytest.param("sc1", 1000, None, None, 15.0, id="sc1-capacity-only"),
    ],
)
def test_ebs_volume_monthly_cost(
    book: PriceBook,
    volume_type: str,
    size: int,
    iops: int | None,
    throughput: int | None,
    expected: float,
) -> None:
    cost = book.aws("us-east-1").ebs_volume_monthly(volume_type, size, iops, throughput)
    assert cost == pytest.approx(expected)


def test_unknown_volume_type_is_unpriced_rather_than_guessed(book: PriceBook) -> None:
    assert book.aws("us-east-1").ebs_volume_monthly("gp9", 100) is None


def test_snapshot_and_hourly_conversions(book: PriceBook) -> None:
    prices = book.aws("us-east-1")
    assert prices.snapshot_monthly(100) == pytest.approx(5.0)
    assert prices.snapshot_monthly(100, "archive") == pytest.approx(1.25)
    assert prices.public_ipv4_monthly() == pytest.approx(0.005 * 730)


def test_gcp_regional_disks_use_multiplier(book: PriceBook) -> None:
    prices = book.gcp("us-central1")
    assert prices.disk_monthly("pd-ssd", 100) == pytest.approx(17.0)
    assert prices.disk_monthly("pd-ssd", 100, regional=True) == pytest.approx(34.0)
    assert prices.disk_monthly("hyperdisk-unknown", 100) is None
    assert prices.static_ip_unused_monthly() == pytest.approx(7.3)


def test_unknown_region_uses_fallback_and_warns_once(book: PriceBook) -> None:
    first = book.aws("sa-east-1")
    book.aws("sa-east-1")
    assert first.region == "us-east-1"
    assert len(book.warnings) == 1
    assert "sa-east-1" in book.warnings[0]


def test_unknown_region_without_fallback_is_an_error() -> None:
    book = PriceBook({"aws": {"regions": {"us-east-1": {"nat_gateway_gb": 0.045}}}})
    with pytest.raises(PricingError, match="eu-central-1"):
        book.aws("eu-central-1")


def test_missing_keys_are_allowed_and_yield_none() -> None:
    book = PriceBook({"aws": {"regions": {"us-east-1": {"nat_gateway_gb": 0.045}}}})
    prices = book.aws("us-east-1")
    assert prices.public_ipv4_monthly() is None
    assert prices.ebs_volume_monthly("gp3", 100) is None


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ({"aws": {"regions": {"us-east-1": {"ebs_gb_mnth": {}}}}}, "unknown key 'ebs_gb_mnth'"),
        ({"aws": {"regions": {"us-east-1": {"nat_gateway_gb": -1}}}}, "cannot be negative"),
        ({"aws": {"regions": {"us-east-1": {"nat_gateway_gb": "cheap"}}}}, "expected a number"),
        ({"aws": {"regions": {"us-east-1": {"nat_gateway_gb": True}}}}, "expected a number"),
        ({"aws": {"regions": {"us-east-1": {"ebs_gb_month": {"gp3": None}}}}}, "gp3"),
        ({"gcp": {"regions": {"us-central1": {"disk_gb_month": 0.04}}}}, "expected a mapping"),
        ({"gcp": {"fallback_region": 1}}, "fallback_region"),
        ({"azure": {}}, "unknown top-level"),
    ],
)
def test_invalid_pricing_documents_are_rejected(document: dict[str, Any], message: str) -> None:
    with pytest.raises(PricingError, match=message):
        PriceBook(document)


def test_load_from_file(tmp_path: Path) -> None:
    path = tmp_path / "prices.yaml"
    path.write_text(
        "currency: EUR\naws:\n  regions:\n    eu-central-1:\n      public_ipv4_hour: 0.01\n"
    )
    book = PriceBook.load(path)
    assert book.source == str(path)
    assert book.currency == "EUR"
    assert book.aws("eu-central-1").public_ipv4_monthly() == pytest.approx(7.3)


def test_load_errors_are_pricing_errors(tmp_path: Path) -> None:
    with pytest.raises(PricingError, match="cannot read"):
        PriceBook.load(tmp_path / "missing.yaml")
    bad = tmp_path / "bad.yaml"
    bad.write_text("aws: [unclosed\n")
    with pytest.raises(PricingError, match="invalid YAML"):
        PriceBook.load(bad)
    scalar = tmp_path / "scalar.yaml"
    scalar.write_text("42\n")
    with pytest.raises(PricingError, match="mapping at the top level"):
        PriceBook.load(scalar)
