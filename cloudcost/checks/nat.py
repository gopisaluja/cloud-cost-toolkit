"""NAT gateway data processing vs. S3/DynamoDB gateway VPC endpoints.

Traffic from private subnets to S3 or DynamoDB normally leaves through a NAT gateway and is
billed per GB processed. A *gateway* VPC endpoint for S3 or DynamoDB routes that traffic
privately with no hourly or per-GB charge, so the processing cost for that share of traffic
goes away. The rest of the NAT bill (hourly charge, other destinations) is unchanged.

    savings/month = processed GB/month x endpoint share x NAT price per GB
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from cloudcost import HOURS_PER_MONTH
from cloudcost.errors import ConfigurationError, PricingError
from cloudcost.pricing import AwsPrices, monthly_from_hourly
from cloudcost.providers.aws import AwsInventory

BYTES_PER_GB = 1024**3
OUTBOUND_METRIC = "BytesOutToDestination"
INBOUND_METRIC = "BytesInFromDestination"
MANUAL_INPUT = "(manual input)"


@dataclass(frozen=True)
class NatEndpointEstimate:
    nat_gateway_id: str
    region: str
    gb_per_month: float
    endpoint_share: float  # 0.0 - 1.0
    price_per_gb: float
    nat_hourly_monthly: float | None = None

    @property
    def current_processing_cost(self) -> float:
        return self.gb_per_month * self.price_per_gb

    @property
    def monthly_savings(self) -> float:
        return self.current_processing_cost * self.endpoint_share

    @property
    def remaining_processing_cost(self) -> float:
        return self.current_processing_cost - self.monthly_savings

    def as_dict(self) -> dict[str, Any]:
        return {
            "nat_gateway_id": self.nat_gateway_id,
            "region": self.region,
            "gb_per_month": round(self.gb_per_month, 2),
            "endpoint_share": self.endpoint_share,
            "price_per_gb": self.price_per_gb,
            "current_processing_cost": round(self.current_processing_cost, 2),
            "monthly_savings": round(self.monthly_savings, 2),
            "remaining_processing_cost": round(self.remaining_processing_cost, 2),
            "nat_hourly_monthly": (
                None if self.nat_hourly_monthly is None else round(self.nat_hourly_monthly, 2)
            ),
        }


def estimate(
    gb_per_month: float,
    endpoint_share: float,
    prices: AwsPrices,
    nat_gateway_id: str = MANUAL_INPUT,
) -> NatEndpointEstimate:
    """Pure cost model for one NAT gateway (or a manually supplied traffic figure)."""
    if gb_per_month < 0:
        raise ConfigurationError("GB per month cannot be negative")
    if not 0.0 <= endpoint_share <= 1.0:
        raise ConfigurationError("endpoint share must be between 0% and 100%")
    if prices.nat_gateway_gb is None:
        raise PricingError(f"no nat_gateway_gb price for region {prices.region}")
    hourly = (
        None if prices.nat_gateway_hour is None else monthly_from_hourly(prices.nat_gateway_hour)
    )
    return NatEndpointEstimate(
        nat_gateway_id=nat_gateway_id,
        region=prices.region,
        gb_per_month=gb_per_month,
        endpoint_share=endpoint_share,
        price_per_gb=prices.nat_gateway_gb,
        nat_hourly_monthly=hourly,
    )


def processed_metrics(outbound_only: bool = False) -> tuple[str, ...]:
    """CloudWatch metrics whose sum approximates bytes billed for NAT data processing.

    Processing is charged in both directions, so by default requests leaving towards the
    destination and responses coming back are added. ``outbound_only`` gives a lower bound.
    """
    return (OUTBOUND_METRIC,) if outbound_only else (OUTBOUND_METRIC, INBOUND_METRIC)


def measure_gb_per_month(
    inventory: AwsInventory,
    nat_gateway_id: str,
    days: int,
    *,
    metrics: Sequence[str] = (OUTBOUND_METRIC, INBOUND_METRIC),
    now: datetime | None = None,
) -> float:
    """Bytes processed over the last ``days`` days, scaled to a 730-hour month, in GiB."""
    if days <= 0:
        raise ConfigurationError("days must be positive")
    end = now or datetime.now(UTC)
    start = end - timedelta(days=days)
    total_bytes = sum(inventory.sum_nat_metric(nat_gateway_id, m, start, end) for m in metrics)
    return (total_bytes / BYTES_PER_GB) * HOURS_PER_MONTH / (days * 24)


def estimate_from_cloudwatch(
    inventory: AwsInventory,
    prices: Callable[[str], AwsPrices],
    *,
    days: int,
    endpoint_share: float,
    nat_gateway_ids: Iterable[str] = (),
    outbound_only: bool = False,
    now: datetime | None = None,
) -> list[NatEndpointEstimate]:
    """One estimate per available NAT gateway in the inventory's region, biggest saving first."""
    gateways = list(inventory.list_nat_gateways())
    wanted = set(nat_gateway_ids)
    if wanted:
        missing = wanted - {g.nat_gateway_id for g in gateways}
        if missing:
            raise ConfigurationError(
                f"NAT gateway(s) not found or not available in {inventory.region}: "
                + ", ".join(sorted(missing))
            )
        gateways = [g for g in gateways if g.nat_gateway_id in wanted]

    if not gateways:
        return []
    region_prices = prices(inventory.region)
    metrics = processed_metrics(outbound_only)
    results = [
        estimate(
            measure_gb_per_month(inventory, g.nat_gateway_id, days, metrics=metrics, now=now),
            endpoint_share,
            region_prices,
            nat_gateway_id=g.nat_gateway_id,
        )
        for g in gateways
    ]
    return sorted(results, key=lambda e: -e.monthly_savings)


def assumptions(
    endpoint_share: float, *, days: int | None = None, outbound_only: bool = False
) -> list[str]:
    """Human-readable assumptions printed with every NAT estimate."""
    if days is None:
        source = "Processed GB/month was supplied with --gb-per-month."
    else:
        metrics = " + ".join(processed_metrics(outbound_only))
        source = (
            f"Processed GB/month = CloudWatch {metrics} over the last {days} day(s), "
            f"scaled to a {HOURS_PER_MONTH}-hour month (1 GB = 1024^3 bytes)."
        )
    share = f"Endpoint share {endpoint_share:.0%} is an input, not a measurement: "
    share += "CloudWatch does not show destinations; measure it with VPC Flow Logs."
    if endpoint_share >= 1.0:
        share += " 100% is an upper bound."
    return [
        source,
        share,
        "S3 and DynamoDB gateway endpoints have no hourly or per-GB charge.",
        "Gateway endpoints only serve S3/DynamoDB in the VPC's own region; cross-region "
        "and internet-bound traffic still uses the NAT gateway.",
        "The NAT hourly charge is unchanged unless the gateway can be removed; it is shown "
        "for context only.",
        "Not modelled: cross-AZ transfer, internet egress, interface endpoints (not free).",
    ]
