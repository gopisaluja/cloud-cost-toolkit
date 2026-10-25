"""GCP waste checks: unattached persistent disks and reserved-but-unused external IPs."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from cloudcost.models import Finding, sort_by_cost
from cloudcost.pricing import GcpPrices
from cloudcost.providers.gcp import GcpInventory

IDLE_DISKS = "idle-disks"
UNUSED_ADDRESSES = "unused-addresses"

PriceLookup = Callable[[str], GcpPrices]
"""Region -> prices, typically ``PriceBook.gcp``. One project spans many regions."""


def find_idle_disks(
    inventory: GcpInventory,
    prices: PriceLookup,
    *,
    min_idle_days: int = 0,
    now: datetime | None = None,
) -> list[Finding]:
    """``READY`` disks with no attached instances.

    With ``min_idle_days`` > 0, disks detached (or, if never attached, created) more recently
    than that are skipped. Disks whose idle time cannot be determined are still reported.
    """
    if min_idle_days < 0:
        raise ValueError("min_idle_days must be >= 0")
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=min_idle_days)

    findings = []
    for disk in inventory.list_disks():
        if disk.users or disk.status != "READY":
            continue
        idle_since = disk.last_detached or disk.created
        if min_idle_days and idle_since is not None and idle_since > cutoff:
            continue
        parts = [f"{disk.disk_type} {disk.size_gb} GB"]
        if disk.regional:
            parts.append("regional")
        if idle_since is not None:
            verb = "detached" if disk.last_detached else "created, never attached"
            parts.append(f"{verb} {idle_since:%Y-%m-%d}")
        findings.append(
            Finding(
                provider="gcp",
                check=IDLE_DISKS,
                resource_id=disk.name,
                location=disk.location,
                summary=", ".join(parts),
                monthly_cost=prices(disk.region).disk_monthly(
                    disk.disk_type, disk.size_gb, regional=disk.regional
                ),
                details={
                    "project": inventory.project,
                    "disk_type": disk.disk_type,
                    "size_gb": disk.size_gb,
                    "regional": disk.regional,
                    "idle_since": idle_since.isoformat() if idle_since else None,
                    "labels": dict(disk.labels),
                },
            )
        )
    return sort_by_cost(findings)


def find_unused_addresses(inventory: GcpInventory, prices: PriceLookup) -> list[Finding]:
    """Static *external* addresses that are reserved but not used by any resource.

    Internal addresses are not billed as unused static IPs and are ignored.
    """
    findings = []
    for addr in inventory.list_addresses():
        if addr.address_type != "EXTERNAL" or addr.status != "RESERVED" or addr.users:
            continue
        findings.append(
            Finding(
                provider="gcp",
                check=UNUSED_ADDRESSES,
                resource_id=addr.name,
                location=addr.region,
                summary=f"{addr.address} reserved, not in use",
                monthly_cost=prices(addr.region).static_ip_unused_monthly(),
                details={
                    "project": inventory.project,
                    "address": addr.address,
                    "labels": dict(addr.labels),
                },
            )
        )
    return sort_by_cost(findings)
