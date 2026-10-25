from __future__ import annotations

from datetime import UTC, datetime, timedelta

import boto3
import pytest

from cloudcost.checks import nat
from cloudcost.errors import ConfigurationError, PricingError
from cloudcost.pricing import AwsPrices, PriceBook
from cloudcost.providers.aws import Boto3AwsInventory, NatGateway

from .fakes import NOW, FakeAwsInventory

GIB = 1024**3


def _gateway(nat_id: str) -> NatGateway:
    return NatGateway(nat_id, "us-east-1", "vpc-1", "subnet-1", "available")


def test_estimate_splits_processing_cost_by_endpoint_share(book: PriceBook) -> None:
    result = nat.estimate(1000, 0.6, book.aws("us-east-1"))

    assert result.current_processing_cost == pytest.approx(45.0)  # 1000 GB x 0.045
    assert result.monthly_savings == pytest.approx(27.0)  # 60% of it
    assert result.remaining_processing_cost == pytest.approx(18.0)
    assert result.nat_hourly_monthly == pytest.approx(0.045 * 730)  # context only
    assert result.nat_gateway_id == nat.MANUAL_INPUT


def test_estimate_uses_regional_price(book: PriceBook) -> None:
    result = nat.estimate(1000, 1.0, book.aws("ap-south-1"))
    assert result.monthly_savings == pytest.approx(56.0)


@pytest.mark.parametrize(("gb", "share"), [(-1, 0.5), (100, -0.1), (100, 1.01)])
def test_estimate_rejects_out_of_range_inputs(book: PriceBook, gb: float, share: float) -> None:
    with pytest.raises(ConfigurationError):
        nat.estimate(gb, share, book.aws("us-east-1"))


def test_estimate_requires_a_nat_price() -> None:
    with pytest.raises(PricingError, match="nat_gateway_gb"):
        nat.estimate(100, 0.5, AwsPrices(region="us-east-1"))


def test_measurement_sums_both_directions_and_normalises_to_730h_month() -> None:
    inventory = FakeAwsInventory(
        nat_gateways=[_gateway("nat-a")],
        metric_bytes={
            ("nat-a", "BytesOutToDestination"): 20 * GIB,
            ("nat-a", "BytesInFromDestination"): 10 * GIB,
        },
    )
    gb = nat.measure_gb_per_month(inventory, "nat-a", 30, now=NOW)

    assert gb == pytest.approx(30 * 730 / (30 * 24))  # 30 GiB in 720 h -> 730 h month
    assert {(c[1], c[2], c[3]) for c in inventory.metric_calls} == {
        ("BytesOutToDestination", NOW - timedelta(days=30), NOW),
        ("BytesInFromDestination", NOW - timedelta(days=30), NOW),
    }


def test_outbound_only_is_a_lower_bound() -> None:
    inventory = FakeAwsInventory(
        metric_bytes={
            ("nat-a", "BytesOutToDestination"): 7 * GIB,
            ("nat-a", "BytesInFromDestination"): 100 * GIB,
        }
    )
    metrics = nat.processed_metrics(outbound_only=True)
    gb = nat.measure_gb_per_month(inventory, "nat-a", 7, metrics=metrics, now=NOW)
    assert gb == pytest.approx(7 * 730 / (7 * 24))
    assert [c[1] for c in inventory.metric_calls] == ["BytesOutToDestination"]


def test_estimate_from_cloudwatch_filters_and_sorts(book: PriceBook) -> None:
    inventory = FakeAwsInventory(
        nat_gateways=[_gateway("nat-small"), _gateway("nat-big"), _gateway("nat-other")],
        metric_bytes={
            ("nat-small", "BytesOutToDestination"): 1 * GIB,
            ("nat-big", "BytesOutToDestination"): 500 * GIB,
        },
    )
    prices = book.aws

    everything = nat.estimate_from_cloudwatch(
        inventory, prices, days=30, endpoint_share=0.5, now=NOW
    )
    assert [e.nat_gateway_id for e in everything] == ["nat-big", "nat-small", "nat-other"]

    picked = nat.estimate_from_cloudwatch(
        inventory, prices, days=30, endpoint_share=0.5, nat_gateway_ids=["nat-small"], now=NOW
    )
    assert [e.nat_gateway_id for e in picked] == ["nat-small"]

    with pytest.raises(ConfigurationError, match="nat-typo"):
        nat.estimate_from_cloudwatch(
            inventory, prices, days=30, endpoint_share=0.5, nat_gateway_ids=["nat-typo"], now=NOW
        )


def test_assumptions_describe_the_inputs() -> None:
    manual = nat.assumptions(1.0)
    assert any("--gb-per-month" in line for line in manual)
    assert any("upper bound" in line for line in manual)

    measured = nat.assumptions(0.4, days=14, outbound_only=True)
    assert any("BytesOutToDestination over the last 14 day(s)" in line for line in measured)
    assert not any("BytesInFromDestination" in line for line in measured)
    assert any("40%" in line for line in measured)


@pytest.mark.usefixtures("moto_aws")
def test_boto3_inventory_reads_nat_gateways_and_cloudwatch(book: PriceBook) -> None:
    ec2 = boto3.client("ec2", region_name="us-east-1")
    cloudwatch = boto3.client("cloudwatch", region_name="us-east-1")
    vpc = ec2.create_vpc(CidrBlock="10.0.0.0/16")["Vpc"]["VpcId"]
    subnet = ec2.create_subnet(VpcId=vpc, CidrBlock="10.0.1.0/24")["Subnet"]["SubnetId"]
    allocation = ec2.allocate_address(Domain="vpc")["AllocationId"]
    nat_id = ec2.create_nat_gateway(SubnetId=subnet, AllocationId=allocation)["NatGateway"][
        "NatGatewayId"
    ]

    def put(gateway: str, metric: str, gib: float, days_back: int) -> None:
        cloudwatch.put_metric_data(
            Namespace="AWS/NATGateway",
            MetricData=[
                {
                    "MetricName": metric,
                    "Dimensions": [{"Name": "NatGatewayId", "Value": gateway}],
                    "Timestamp": datetime.now(UTC) - timedelta(days=days_back),
                    "Value": gib * GIB,
                    "Unit": "Bytes",
                }
            ],
        )

    put(nat_id, "BytesOutToDestination", 40, 1)
    put(nat_id, "BytesOutToDestination", 20, 3)
    put(nat_id, "BytesInFromDestination", 30, 2)
    put("nat-someone-else", "BytesOutToDestination", 9999, 1)  # different dimension
    put(nat_id, "BytesOutToDestination", 5000, 45)  # outside the 30-day window

    inventory = Boto3AwsInventory("us-east-1")
    assert [g.nat_gateway_id for g in inventory.list_nat_gateways()] == [nat_id]

    [result] = nat.estimate_from_cloudwatch(
        inventory, book.aws, days=30, endpoint_share=0.5, now=datetime.now(UTC)
    )
    expected_gb = 90 * 730 / (30 * 24)
    assert result.gb_per_month == pytest.approx(expected_gb)
    assert result.monthly_savings == pytest.approx(expected_gb * 0.045 * 0.5)
