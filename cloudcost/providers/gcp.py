"""Read-only GCP inventory (Compute Engine disks and addresses) behind a small protocol."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from cloudcost.errors import ConfigurationError

GLOBAL = "global"


@dataclass(frozen=True)
class Disk:
    name: str
    location: str  # zone, or region for regional persistent disks
    region: str
    disk_type: str  # short name, e.g. "pd-balanced"
    size_gb: int
    status: str
    users: tuple[str, ...] = ()
    regional: bool = False
    created: datetime | None = None
    last_attached: datetime | None = None
    last_detached: datetime | None = None
    labels: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Address:
    name: str
    region: str  # "global" for global addresses
    address: str
    status: str  # RESERVED | RESERVING | IN_USE
    address_type: str  # EXTERNAL | INTERNAL
    users: tuple[str, ...] = ()
    labels: Mapping[str, str] = field(default_factory=dict)


class GcpInventory(Protocol):
    """Everything the GCP checks need from one project. Implementations must be read-only."""

    project: str

    def list_disks(self) -> Iterable[Disk]: ...

    def list_addresses(self) -> Iterable[Address]: ...


def last_segment(url: str) -> str:
    """``.../zones/us-central1-a`` -> ``us-central1-a`` (also accepts bare names)."""
    return url.rstrip("/").rsplit("/", 1)[-1] if url else ""


def zone_to_region(zone: str) -> str:
    """``us-central1-a`` -> ``us-central1``."""
    return zone.rsplit("-", 1)[0] if "-" in zone else zone


def parse_timestamp(value: str | None) -> datetime | None:
    """Parse the RFC 3339 timestamps Compute Engine returns; ``None`` if absent or invalid."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def disk_from_api(d: Any) -> Disk:
    zone = last_segment(getattr(d, "zone", "") or "")
    region_url = getattr(d, "region", "") or ""
    regional = not zone and bool(region_url)
    region = last_segment(region_url) if regional else zone_to_region(zone)
    return Disk(
        name=d.name,
        location=zone or region,
        region=region,
        disk_type=last_segment(d.type_),
        size_gb=int(d.size_gb or 0),
        status=d.status or "",
        users=tuple(d.users or ()),
        regional=regional,
        created=parse_timestamp(d.creation_timestamp),
        last_attached=parse_timestamp(d.last_attach_timestamp),
        last_detached=parse_timestamp(d.last_detach_timestamp),
        labels=dict(d.labels or {}),
    )


def address_from_api(a: Any, region: str) -> Address:
    return Address(
        name=a.name,
        region=region,
        address=a.address,
        status=a.status or "",
        # addressType defaults to EXTERNAL when it was not specified at reservation time.
        address_type=a.address_type or "EXTERNAL",
        users=tuple(a.users or ()),
        labels=dict(a.labels or {}),
    )


class ComputeGcpInventory:
    """:class:`GcpInventory` backed by ``google-cloud-compute`` and Application Default
    Credentials. Clients can be injected for testing; otherwise they are created lazily."""

    def __init__(
        self,
        project: str,
        *,
        disks_client: Any = None,
        addresses_client: Any = None,
        global_addresses_client: Any = None,
    ) -> None:
        self.project = project
        self._disks_client = disks_client
        self._addresses_client = addresses_client
        self._global_addresses_client = global_addresses_client

    @classmethod
    def from_default_credentials(cls, project: str | None = None) -> ComputeGcpInventory:
        if not project:
            import google.auth

            _, project = google.auth.default()
        if not project:
            raise ConfigurationError(
                "no GCP project: pass --project or set a default "
                "(gcloud config set project ..., or GOOGLE_CLOUD_PROJECT)"
            )
        return cls(project)

    def list_disks(self) -> Iterator[Disk]:
        if self._disks_client is None:
            from google.cloud import compute_v1

            self._disks_client = compute_v1.DisksClient()
        # aggregated_list yields (scope, DisksScopedList) for every zone and region.
        for _scope, scoped in self._disks_client.aggregated_list(project=self.project):
            for disk in getattr(scoped, "disks", None) or ():
                yield disk_from_api(disk)

    def list_addresses(self) -> Iterator[Address]:
        if self._addresses_client is None or self._global_addresses_client is None:
            from google.cloud import compute_v1

            self._addresses_client = self._addresses_client or compute_v1.AddressesClient()
            self._global_addresses_client = (
                self._global_addresses_client or compute_v1.GlobalAddressesClient()
            )
        for scope, scoped in self._addresses_client.aggregated_list(project=self.project):
            if not scope.startswith("regions/"):
                continue  # global addresses are listed separately below
            for address in getattr(scoped, "addresses", None) or ():
                region = last_segment(address.region) or scope.split("/", 1)[1]
                yield address_from_api(address, region)
        for address in self._global_addresses_client.list(project=self.project):
            yield address_from_api(address, GLOBAL)
