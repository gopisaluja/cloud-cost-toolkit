"""AWS waste checks: unattached EBS volumes, old snapshots, unassociated Elastic IPs."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from cloudcost.models import Finding, sort_by_cost
from cloudcost.pricing import AwsPrices
from cloudcost.providers.aws import AwsInventory

IDLE_EBS = "idle-ebs"
OLD_SNAPSHOTS = "old-snapshots"
UNUSED_EIPS = "unused-eips"

_PROVISIONED_PERF_TYPES = {"gp3", "io1", "io2"}

PriceLookup = Callable[[str], AwsPrices]
"""Region -> prices, typically ``PriceBook.aws``. Only called for regions with findings, so
scanning many empty regions does not trigger price fallbacks or warnings."""


def find_idle_ebs(inventory: AwsInventory, prices: PriceLookup) -> list[Finding]:
    """EBS volumes in the ``available`` state, i.e. not attached to any instance.

    Unattached volumes are billed exactly like attached ones (capacity plus any provisioned
    IOPS/throughput), so the full monthly price is reported.
    """
    findings = []
    for vol in inventory.list_volumes(status="available"):
        if vol.state != "available":  # defensive: fakes or future adapters may not filter
            continue
        parts = [f"{vol.volume_type} {vol.size_gib} GiB"]
        if vol.volume_type in _PROVISIONED_PERF_TYPES and vol.iops:
            parts.append(f"{vol.iops} IOPS")
        if vol.volume_type == "gp3" and vol.throughput_mibps:
            parts.append(f"{vol.throughput_mibps} MiB/s")
        if name := vol.tags.get("Name"):
            parts.append(f"Name={name}")
        parts.append(f"created {vol.create_time:%Y-%m-%d}")
        findings.append(
            Finding(
                provider="aws",
                check=IDLE_EBS,
                resource_id=vol.volume_id,
                location=vol.availability_zone,
                summary=", ".join(parts),
                monthly_cost=prices(inventory.region).ebs_volume_monthly(
                    vol.volume_type, vol.size_gib, vol.iops, vol.throughput_mibps
                ),
                details={
                    "volume_type": vol.volume_type,
                    "size_gib": vol.size_gib,
                    "iops": vol.iops,
                    "throughput_mibps": vol.throughput_mibps,
                    "created": vol.create_time.isoformat(),
                    "tags": dict(vol.tags),
                },
            )
        )
    return sort_by_cost(findings)


def find_old_snapshots(
    inventory: AwsInventory,
    prices: PriceLookup,
    older_than_days: int,
    *,
    include_ami_snapshots: bool = False,
    now: datetime | None = None,
) -> list[Finding]:
    """Account-owned snapshots at least ``older_than_days`` old.

    Snapshots that back a registered AMI are skipped unless ``include_ami_snapshots`` is set,
    because they cannot be removed without deregistering the image first. Each finding notes
    whether the source volume still exists: an orphaned snapshot is a stronger cleanup signal.

    The cost is an *upper bound* (source volume size x snapshot price): snapshots are
    incremental, so the billed size is usually smaller.
    """
    if older_than_days < 0:
        raise ValueError("older_than_days must be >= 0")
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=older_than_days)
    images_by_snapshot = inventory.snapshot_ids_used_by_images()
    existing_volumes = {v.volume_id for v in inventory.list_volumes()}

    findings = []
    for snap in inventory.list_snapshots():
        if snap.start_time > cutoff:
            continue
        image_ids = list(images_by_snapshot.get(snap.snapshot_id, ()))
        if image_ids and not include_ami_snapshots:
            continue
        source_exists = snap.volume_id in existing_volumes
        age_days = (now - snap.start_time).days
        parts = [f"{age_days} days old", f"{snap.volume_size_gib} GiB source volume"]
        if snap.storage_tier != "standard":
            parts.append(f"{snap.storage_tier} tier")
        if not source_exists:
            parts.append("source volume not found")
        if image_ids:
            parts.append("used by " + " ".join(image_ids))
        findings.append(
            Finding(
                provider="aws",
                check=OLD_SNAPSHOTS,
                resource_id=snap.snapshot_id,
                location=snap.region,
                summary=", ".join(parts),
                monthly_cost=prices(inventory.region).snapshot_monthly(
                    snap.volume_size_gib, snap.storage_tier
                ),
                details={
                    "age_days": age_days,
                    "started": snap.start_time.isoformat(),
                    "volume_id": snap.volume_id,
                    "source_volume_exists": source_exists,
                    "image_ids": image_ids,
                    "storage_tier": snap.storage_tier,
                    "description": snap.description,
                    "tags": dict(snap.tags),
                },
            )
        )
    return sort_by_cost(findings)


def find_unused_eips(inventory: AwsInventory, prices: PriceLookup) -> list[Finding]:
    """Elastic IPs not associated with an instance or network interface.

    AWS bills every public IPv4 address hourly; an unassociated Elastic IP is pure cost.
    """
    findings = []
    for addr in inventory.list_addresses():
        if addr.in_use:
            continue
        summary = f"{addr.public_ip} not associated"
        if name := addr.tags.get("Name"):
            summary += f", Name={name}"
        findings.append(
            Finding(
                provider="aws",
                check=UNUSED_EIPS,
                resource_id=addr.allocation_id or addr.public_ip,
                location=addr.region,
                summary=summary,
                monthly_cost=prices(inventory.region).public_ipv4_monthly(),
                details={"public_ip": addr.public_ip, "tags": dict(addr.tags)},
            )
        )
    return sort_by_cost(findings)
