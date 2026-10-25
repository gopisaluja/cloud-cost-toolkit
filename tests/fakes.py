"""In-memory implementations of the provider protocols, for logic-only tests."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from cloudcost.providers.aws import ElasticIp, NatGateway, Snapshot, Volume
from cloudcost.providers.gcp import Address, Disk

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def days_ago(days: float) -> datetime:
    return NOW - timedelta(days=days)


@dataclass
class FakeAwsInventory:
    """Ignores the ``status`` filter on purpose so the checks' own filtering is exercised."""

    region: str = "us-east-1"
    volumes: list[Volume] = field(default_factory=list)
    snapshots: list[Snapshot] = field(default_factory=list)
    images_by_snapshot: dict[str, list[str]] = field(default_factory=dict)
    addresses: list[ElasticIp] = field(default_factory=list)
    nat_gateways: list[NatGateway] = field(default_factory=list)
    metric_bytes: dict[tuple[str, str], float] = field(default_factory=dict)
    metric_calls: list[tuple[str, str, datetime, datetime]] = field(default_factory=list)

    def list_volumes(self, status: str | None = None) -> Iterable[Volume]:
        return list(self.volumes)

    def list_snapshots(self) -> Iterable[Snapshot]:
        return list(self.snapshots)

    def snapshot_ids_used_by_images(self) -> dict[str, list[str]]:
        return self.images_by_snapshot

    def list_addresses(self) -> Iterable[ElasticIp]:
        return list(self.addresses)

    def list_nat_gateways(self) -> Iterable[NatGateway]:
        return list(self.nat_gateways)

    def sum_nat_metric(
        self, nat_gateway_id: str, metric_name: str, start: datetime, end: datetime
    ) -> float:
        self.metric_calls.append((nat_gateway_id, metric_name, start, end))
        return self.metric_bytes.get((nat_gateway_id, metric_name), 0.0)


@dataclass
class FakeGcpInventory:
    project: str = "demo-project"
    disks: list[Disk] = field(default_factory=list)
    addresses: list[Address] = field(default_factory=list)

    def list_disks(self) -> Iterable[Disk]:
        return list(self.disks)

    def list_addresses(self) -> Iterable[Address]:
        return list(self.addresses)


def volume(volume_id: str, volume_type: str = "gp3", size: int = 100, **kwargs: Any) -> Volume:
    defaults: dict[str, Any] = {
        "region": "us-east-1",
        "availability_zone": "us-east-1a",
        "state": "available",
        "create_time": days_ago(30),
    }
    defaults.update(kwargs)
    return Volume(volume_id=volume_id, volume_type=volume_type, size_gib=size, **defaults)


def snapshot(snapshot_id: str, age_days: float, size: int = 100, **kwargs: Any) -> Snapshot:
    defaults: dict[str, Any] = {"region": "us-east-1", "volume_id": "vol-live"}
    defaults.update(kwargs)
    return Snapshot(
        snapshot_id=snapshot_id,
        volume_size_gib=size,
        start_time=days_ago(age_days),
        **defaults,
    )


class FakeComputeClient:
    """Mimics the parts of google-cloud-compute clients the adapter uses.

    ``aggregated_list`` yields ``(scope, scoped_list)`` pairs and ``list`` yields items, just
    like the real pagers; messages are ``SimpleNamespace`` objects with the same attribute
    names as the proto-plus types (``type_``, ``size_gb``, ...).
    """

    def __init__(
        self,
        aggregated: dict[str, SimpleNamespace] | None = None,
        items: list[Any] | None = None,
    ) -> None:
        self.aggregated = aggregated or {}
        self.items = items or []
        self.calls: list[dict[str, Any]] = []

    def aggregated_list(self, **kwargs: Any) -> Iterator[tuple[str, SimpleNamespace]]:
        self.calls.append(kwargs)
        return iter(self.aggregated.items())

    def list(self, **kwargs: Any) -> Iterator[Any]:
        self.calls.append(kwargs)
        return iter(self.items)


API = "https://www.googleapis.com/compute/v1/projects/demo-project"


def api_disk(name: str, *, zone: str = "", region: str = "", **kwargs: Any) -> SimpleNamespace:
    fields: dict[str, Any] = {
        "name": name,
        "zone": f"{API}/zones/{zone}" if zone else "",
        "region": f"{API}/regions/{region}" if region else "",
        "size_gb": 100,
        "type_": f"{API}/zones/{zone or 'x'}/diskTypes/pd-balanced",
        "users": [],
        "status": "READY",
        "creation_timestamp": "2026-01-10T08:00:00.000-08:00",
        "last_attach_timestamp": "",
        "last_detach_timestamp": "",
        "labels": {},
    }
    fields.update(kwargs)
    return SimpleNamespace(**fields)


def api_address(name: str, *, region: str = "", **kwargs: Any) -> SimpleNamespace:
    fields: dict[str, Any] = {
        "name": name,
        "region": f"{API}/regions/{region}" if region else "",
        "address": "203.0.113.10",
        "status": "RESERVED",
        "address_type": "EXTERNAL",
        "users": [],
        "labels": {},
    }
    fields.update(kwargs)
    return SimpleNamespace(**fields)
