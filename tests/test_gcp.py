"""GCP adapter (with fake compute clients) and checks (with a fake inventory)."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import google.auth
import pytest

from cloudcost.checks.gcp import find_idle_disks, find_unused_addresses
from cloudcost.errors import ConfigurationError
from cloudcost.pricing import PriceBook
from cloudcost.providers.gcp import Address, ComputeGcpInventory, Disk, parse_timestamp

from .fakes import API, NOW, FakeComputeClient, FakeGcpInventory, api_address, api_disk, days_ago

# --------------------------------------------------------------------------------- adapter


def test_adapter_parses_zonal_and_regional_disks() -> None:
    client = FakeComputeClient(
        aggregated={
            "zones/us-central1-a": SimpleNamespace(
                disks=[
                    api_disk(
                        "scratch",
                        zone="us-central1-a",
                        size_gb=200,
                        type_=f"{API}/zones/us-central1-a/diskTypes/pd-ssd",
                        last_detach_timestamp="2026-05-01T10:00:00.000-07:00",
                        labels={"team": "data"},
                    )
                ]
            ),
            # Scopes without disks come back with only a warning and no `disks` field.
            "zones/us-central1-b": SimpleNamespace(),
            "regions/europe-west1": SimpleNamespace(
                disks=[
                    api_disk(
                        "ha-db",
                        region="europe-west1",
                        type_=f"{API}/regions/europe-west1/diskTypes/pd-balanced",
                        users=[f"{API}/zones/europe-west1-b/instances/db-1"],
                    )
                ]
            ),
        }
    )
    inventory = ComputeGcpInventory("demo-project", disks_client=client)

    scratch, ha_db = inventory.list_disks()

    assert client.calls == [{"project": "demo-project"}]
    assert (scratch.location, scratch.region, scratch.regional) == (
        "us-central1-a",
        "us-central1",
        False,
    )
    assert (scratch.disk_type, scratch.size_gb) == ("pd-ssd", 200)
    assert scratch.last_detached == datetime(2026, 5, 1, 17, 0, tzinfo=UTC)
    assert scratch.labels == {"team": "data"}
    assert (ha_db.location, ha_db.region, ha_db.regional) == ("europe-west1", "europe-west1", True)
    assert ha_db.users == (f"{API}/zones/europe-west1-b/instances/db-1",)


def test_adapter_lists_regional_and_global_addresses_without_duplicates() -> None:
    regional = FakeComputeClient(
        aggregated={
            "regions/us-central1": SimpleNamespace(
                addresses=[
                    api_address("nat-ip", region="us-central1"),
                    api_address("internal", region="us-central1", address_type="INTERNAL"),
                ]
            ),
            "global": SimpleNamespace(addresses=[api_address("lb-ip")]),  # also listed below
        }
    )
    global_client = FakeComputeClient(items=[api_address("lb-ip", address_type="")])
    inventory = ComputeGcpInventory(
        "demo-project", addresses_client=regional, global_addresses_client=global_client
    )

    addresses = list(inventory.list_addresses())

    assert [(a.name, a.region) for a in addresses] == [
        ("nat-ip", "us-central1"),
        ("internal", "us-central1"),
        ("lb-ip", "global"),
    ]
    assert addresses[2].address_type == "EXTERNAL"  # API default when unspecified


def test_default_project_comes_from_adc(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(google.auth, "default", lambda: (object(), "adc-project"))
    assert ComputeGcpInventory.from_default_credentials().project == "adc-project"
    assert ComputeGcpInventory.from_default_credentials("explicit").project == "explicit"

    monkeypatch.setattr(google.auth, "default", lambda: (object(), None))
    with pytest.raises(ConfigurationError, match="--project"):
        ComputeGcpInventory.from_default_credentials()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-01-10T08:00:00.000-08:00", datetime(2026, 1, 10, 16, 0, tzinfo=UTC)),
        ("2026-01-10T16:00:00Z", datetime(2026, 1, 10, 16, 0, tzinfo=UTC)),
        ("", None),
        ("not-a-date", None),
    ],
)
def test_parse_timestamp(raw: str, expected: datetime | None) -> None:
    assert parse_timestamp(raw) == expected


# ---------------------------------------------------------------------------------- checks


def _disk(name: str, **kwargs) -> Disk:
    defaults = {
        "location": "us-central1-a",
        "region": "us-central1",
        "disk_type": "pd-balanced",
        "size_gb": 100,
        "status": "READY",
        "created": days_ago(60),
    }
    defaults.update(kwargs)
    return Disk(name=name, **defaults)


def test_idle_disks_pricing_and_filtering(book: PriceBook) -> None:
    inventory = FakeGcpInventory(
        disks=[
            _disk("attached", users=("instances/vm-1",)),
            _disk("creating", status="CREATING"),
            _disk("ssd", disk_type="pd-ssd"),
            _disk("regional", location="europe-west1", region="europe-west1", regional=True),
            _disk("exotic", disk_type="hyperdisk-ml"),
        ]
    )
    findings = find_idle_disks(inventory, book.gcp, now=NOW)

    assert [(f.resource_id, f.monthly_cost) for f in findings] == [
        ("regional", pytest.approx(20.0)),  # 100 GB x 0.10 x 2 (regional)
        ("ssd", pytest.approx(17.0)),
        ("exotic", None),
    ]
    assert "regional" in findings[0].summary
    assert findings[0].details["project"] == "demo-project"


def test_idle_disks_min_idle_days(book: PriceBook) -> None:
    inventory = FakeGcpInventory(
        disks=[
            _disk("recently-detached", last_detached=days_ago(3)),
            _disk("long-detached", last_detached=days_ago(45)),
            _disk("never-attached-new", created=days_ago(2)),
            _disk("never-attached-old", created=days_ago(20)),
            _disk("unknown-age", created=None),
        ]
    )
    names = {f.resource_id for f in find_idle_disks(inventory, book.gcp, min_idle_days=7, now=NOW)}
    assert names == {"long-detached", "never-attached-old", "unknown-age"}

    all_names = {f.resource_id for f in find_idle_disks(inventory, book.gcp, now=NOW)}
    assert len(all_names) == 5


def test_idle_disks_in_unpriced_region_fall_back_with_warning(book: PriceBook) -> None:
    inventory = FakeGcpInventory(
        disks=[_disk("far", location="southamerica-east1-a", region="southamerica-east1")]
    )
    [finding] = find_idle_disks(inventory, book.gcp, now=NOW)
    assert finding.monthly_cost == pytest.approx(10.0)  # us-central1 pd-balanced
    assert any("southamerica-east1" in w for w in book.warnings)


def test_unused_addresses_only_reserved_external(book: PriceBook) -> None:
    def addr(name: str, **kwargs) -> Address:
        defaults = {
            "region": "us-central1",
            "address": "203.0.113.5",
            "status": "RESERVED",
            "address_type": "EXTERNAL",
        }
        defaults.update(kwargs)
        return Address(name=name, **defaults)

    inventory = FakeGcpInventory(
        addresses=[
            addr("free"),
            addr("lb-free", region="global"),
            addr("busy", status="IN_USE", users=("forwardingRules/fr-1",)),
            addr("private", address_type="INTERNAL"),
            addr("reserving", status="RESERVING"),
        ]
    )
    findings = find_unused_addresses(inventory, book.gcp)

    assert sorted(f.resource_id for f in findings) == ["free", "lb-free"]
    assert all(f.monthly_cost == pytest.approx(7.3) for f in findings)
    assert book.warnings == []  # "global" has its own entry in the price list
