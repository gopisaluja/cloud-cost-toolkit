"""Read-only AWS inventory (EC2 + CloudWatch) behind a small protocol."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

import boto3

NAT_NAMESPACE = "AWS/NATGateway"
SECONDS_PER_DAY = 86_400


@dataclass(frozen=True)
class Volume:
    volume_id: str
    region: str
    availability_zone: str
    volume_type: str
    size_gib: int
    state: str
    create_time: datetime
    iops: int | None = None
    throughput_mibps: int | None = None
    tags: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Snapshot:
    snapshot_id: str
    region: str
    volume_id: str | None
    volume_size_gib: int
    start_time: datetime
    storage_tier: str = "standard"
    description: str = ""
    tags: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ElasticIp:
    public_ip: str
    region: str
    allocation_id: str | None = None
    association_id: str | None = None
    instance_id: str | None = None
    network_interface_id: str | None = None
    tags: Mapping[str, str] = field(default_factory=dict)

    @property
    def in_use(self) -> bool:
        return bool(self.association_id or self.instance_id or self.network_interface_id)


@dataclass(frozen=True)
class NatGateway:
    nat_gateway_id: str
    region: str
    vpc_id: str
    subnet_id: str
    state: str
    tags: Mapping[str, str] = field(default_factory=dict)


class AwsInventory(Protocol):
    """Everything the AWS checks need from one region. Implementations must be read-only."""

    region: str

    def list_volumes(self, status: str | None = None) -> Iterable[Volume]: ...

    def list_snapshots(self) -> Iterable[Snapshot]:
        """Snapshots owned by the calling account."""
        ...

    def snapshot_ids_used_by_images(self) -> Mapping[str, Sequence[str]]:
        """Map snapshot id -> ids of the account's AMIs whose block devices reference it."""
        ...

    def list_addresses(self) -> Iterable[ElasticIp]: ...

    def list_nat_gateways(self) -> Iterable[NatGateway]: ...

    def sum_nat_metric(
        self, nat_gateway_id: str, metric_name: str, start: datetime, end: datetime
    ) -> float:
        """Sum of a NAT gateway CloudWatch metric (bytes) between ``start`` and ``end``."""
        ...


def _tags(raw: Sequence[Mapping[str, str]] | None) -> dict[str, str]:
    return {t["Key"]: t.get("Value", "") for t in raw or []}


class Boto3AwsInventory:
    """:class:`AwsInventory` backed by boto3 and the default credential chain."""

    def __init__(self, region: str, session: boto3.session.Session | None = None) -> None:
        self.region = region
        self._session = session or boto3.session.Session()
        self._ec2 = self._session.client("ec2", region_name=region)
        self._cloudwatch: Any = None

    def _paginate(self, operation: str, result_key: str, **kwargs: Any) -> Iterator[Any]:
        for page in self._ec2.get_paginator(operation).paginate(**kwargs):
            yield from page.get(result_key, [])

    def list_volumes(self, status: str | None = None) -> Iterator[Volume]:
        kwargs: dict[str, Any] = {}
        if status:
            kwargs["Filters"] = [{"Name": "status", "Values": [status]}]
        for v in self._paginate("describe_volumes", "Volumes", **kwargs):
            yield Volume(
                volume_id=v["VolumeId"],
                region=self.region,
                availability_zone=v.get("AvailabilityZone", self.region),
                volume_type=v.get("VolumeType", "unknown"),
                size_gib=int(v.get("Size", 0)),
                state=v.get("State", "unknown"),
                create_time=v["CreateTime"],
                iops=v.get("Iops"),
                throughput_mibps=v.get("Throughput"),
                tags=_tags(v.get("Tags")),
            )

    def list_snapshots(self) -> Iterator[Snapshot]:
        for s in self._paginate("describe_snapshots", "Snapshots", OwnerIds=["self"]):
            yield Snapshot(
                snapshot_id=s["SnapshotId"],
                region=self.region,
                volume_id=s.get("VolumeId"),
                volume_size_gib=int(s.get("VolumeSize", 0)),
                start_time=s["StartTime"],
                storage_tier=s.get("StorageTier", "standard"),
                description=s.get("Description", ""),
                tags=_tags(s.get("Tags")),
            )

    def snapshot_ids_used_by_images(self) -> dict[str, list[str]]:
        used: dict[str, list[str]] = {}
        # Disabled AMIs are still registered, so their snapshots are still locked.
        images = self._paginate("describe_images", "Images", Owners=["self"], IncludeDisabled=True)
        for image in images:
            for mapping in image.get("BlockDeviceMappings", []):
                snapshot_id = mapping.get("Ebs", {}).get("SnapshotId")
                if snapshot_id:
                    used.setdefault(snapshot_id, []).append(image["ImageId"])
        return used

    def list_addresses(self) -> Iterator[ElasticIp]:
        # DescribeAddresses is not paginated.
        for a in self._ec2.describe_addresses().get("Addresses", []):
            yield ElasticIp(
                public_ip=a.get("PublicIp", ""),
                region=self.region,
                allocation_id=a.get("AllocationId"),
                association_id=a.get("AssociationId"),
                instance_id=a.get("InstanceId") or None,
                network_interface_id=a.get("NetworkInterfaceId"),
                tags=_tags(a.get("Tags")),
            )

    def list_nat_gateways(self) -> Iterator[NatGateway]:
        filters = [{"Name": "state", "Values": ["available"]}]
        for n in self._paginate("describe_nat_gateways", "NatGateways", Filters=filters):
            yield NatGateway(
                nat_gateway_id=n["NatGatewayId"],
                region=self.region,
                vpc_id=n.get("VpcId", ""),
                subnet_id=n.get("SubnetId", ""),
                state=n.get("State", "unknown"),
                tags=_tags(n.get("Tags")),
            )

    def sum_nat_metric(
        self, nat_gateway_id: str, metric_name: str, start: datetime, end: datetime
    ) -> float:
        if self._cloudwatch is None:
            self._cloudwatch = self._session.client("cloudwatch", region_name=self.region)
        # Daily buckets: at most 455 datapoints for the longest allowed window (< 1,440 limit).
        response = self._cloudwatch.get_metric_statistics(
            Namespace=NAT_NAMESPACE,
            MetricName=metric_name,
            Dimensions=[{"Name": "NatGatewayId", "Value": nat_gateway_id}],
            StartTime=start,
            EndTime=end,
            Period=SECONDS_PER_DAY,
            Statistics=["Sum"],
        )
        return float(sum(dp.get("Sum", 0.0) for dp in response.get("Datapoints", [])))


def make_session(profile: str | None = None) -> boto3.session.Session:
    """A boto3 session using the default credential chain (optionally a named profile)."""
    return boto3.session.Session(profile_name=profile) if profile else boto3.session.Session()


def enabled_regions(session: boto3.session.Session) -> list[str]:
    """Regions enabled for the account (opted-in or opt-in-not-required)."""
    ec2 = session.client("ec2", region_name=session.region_name or "us-east-1")
    regions = ec2.describe_regions(AllRegions=False).get("Regions", [])
    return sorted(r["RegionName"] for r in regions)
