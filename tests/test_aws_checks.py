"""AWS checks: logic tests against fakes, plus moto-backed tests of the boto3 adapter."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import boto3
import pytest

from cloudcost.checks.aws import find_idle_ebs, find_old_snapshots, find_unused_eips
from cloudcost.pricing import PriceBook
from cloudcost.providers.aws import Boto3AwsInventory, ElasticIp

from .fakes import NOW, FakeAwsInventory, snapshot, volume

# ----------------------------------------------------------------------------- logic (fakes)


def test_idle_ebs_reports_only_unattached_volumes_most_expensive_first(book: PriceBook) -> None:
    inventory = FakeAwsInventory(
        volumes=[
            volume("vol-attached", "gp3", 1000, state="in-use"),
            volume("vol-gp2", "gp2", 100, tags={"Name": "old-build-cache"}),
            volume("vol-io2", "io2", 100, iops=1000),
            volume("vol-mystery", "gp9", 50),
        ]
    )
    findings = find_idle_ebs(inventory, book.aws)

    assert [f.resource_id for f in findings] == ["vol-io2", "vol-gp2", "vol-mystery"]
    assert findings[0].monthly_cost == pytest.approx(77.5)
    assert findings[1].monthly_cost == pytest.approx(10.0)
    assert findings[2].monthly_cost is None  # unpriced, still reported, sorted last
    assert "Name=old-build-cache" in findings[1].summary
    assert "1000 IOPS" in findings[0].summary
    assert all(f.check == "idle-ebs" and f.location == "us-east-1a" for f in findings)


def test_old_snapshots_threshold_is_inclusive(book: PriceBook) -> None:
    inventory = FakeAwsInventory(
        volumes=[volume("vol-live", state="in-use")],
        snapshots=[
            snapshot("snap-89d", 89),
            snapshot("snap-90d", 90),
            snapshot("snap-400d", 400, size=500),
        ],
    )
    findings = find_old_snapshots(inventory, book.aws, 90, now=NOW)

    assert [f.resource_id for f in findings] == ["snap-400d", "snap-90d"]
    assert findings[0].details["age_days"] == 400
    assert findings[0].monthly_cost == pytest.approx(500 * 0.05)


def test_old_snapshots_skip_ami_backed_unless_requested(book: PriceBook) -> None:
    inventory = FakeAwsInventory(
        volumes=[volume("vol-live", state="in-use")],
        snapshots=[snapshot("snap-ami", 200), snapshot("snap-plain", 200)],
        images_by_snapshot={"snap-ami": ["ami-111", "ami-222"]},
    )
    prices = book.aws

    default = find_old_snapshots(inventory, prices, 30, now=NOW)
    assert [f.resource_id for f in default] == ["snap-plain"]

    everything = find_old_snapshots(inventory, prices, 30, include_ami_snapshots=True, now=NOW)
    ami = next(f for f in everything if f.resource_id == "snap-ami")
    assert ami.details["image_ids"] == ["ami-111", "ami-222"]
    assert "used by ami-111 ami-222" in ami.summary


def test_old_snapshots_flag_missing_source_volume_and_archive_tier(book: PriceBook) -> None:
    inventory = FakeAwsInventory(
        volumes=[volume("vol-live", state="in-use")],
        snapshots=[
            snapshot("snap-orphan", 100, volume_id="vol-deleted"),
            snapshot("snap-archived", 100, storage_tier="archive"),
        ],
    )
    findings = {f.resource_id: f for f in find_old_snapshots(inventory, book.aws, 30, now=NOW)}

    assert findings["snap-orphan"].details["source_volume_exists"] is False
    assert "source volume not found" in findings["snap-orphan"].summary
    assert findings["snap-archived"].details["source_volume_exists"] is True
    assert findings["snap-archived"].monthly_cost == pytest.approx(100 * 0.0125)


def test_old_snapshots_rejects_negative_age(book: PriceBook) -> None:
    with pytest.raises(ValueError, match="older_than_days"):
        find_old_snapshots(FakeAwsInventory(), book.aws, -1, now=NOW)


def test_unused_eips_ignore_any_kind_of_association(book: PriceBook) -> None:
    inventory = FakeAwsInventory(
        addresses=[
            ElasticIp(
                "198.51.100.1",
                "us-east-1",
                "eipalloc-inst",
                association_id="eipassoc-1",
                instance_id="i-1",
            ),
            ElasticIp("198.51.100.2", "us-east-1", "eipalloc-eni", network_interface_id="eni-1"),
            ElasticIp("198.51.100.3", "us-east-1", "eipalloc-free", tags={"Name": "legacy-vpn"}),
        ]
    )
    findings = find_unused_eips(inventory, book.aws)

    assert [f.resource_id for f in findings] == ["eipalloc-free"]
    assert findings[0].monthly_cost == pytest.approx(3.65)
    assert "Name=legacy-vpn" in findings[0].summary


# ----------------------------------------------------------------------- adapter (moto)


def _run_instance(ec2) -> dict:
    return ec2.run_instances(
        ImageId="ami-12345678", MinCount=1, MaxCount=1, InstanceType="t3.micro"
    )["Instances"][0]


@pytest.mark.usefixtures("moto_aws")
def test_boto3_inventory_idle_ebs(book: PriceBook) -> None:
    ec2 = boto3.client("ec2", region_name="us-east-1")
    instance = _run_instance(ec2)
    orphan = ec2.create_volume(
        AvailabilityZone="us-east-1a",
        Size=200,
        VolumeType="gp3",
        TagSpecifications=[
            {"ResourceType": "volume", "Tags": [{"Key": "Name", "Value": "orphan"}]}
        ],
    )["VolumeId"]
    attached = ec2.create_volume(AvailabilityZone="us-east-1a", Size=50, VolumeType="gp2")[
        "VolumeId"
    ]
    ec2.attach_volume(VolumeId=attached, InstanceId=instance["InstanceId"], Device="/dev/sdf")

    findings = find_idle_ebs(Boto3AwsInventory("us-east-1"), book.aws)

    assert [f.resource_id for f in findings] == [orphan]
    assert findings[0].monthly_cost == pytest.approx(200 * 0.08)
    assert findings[0].details["tags"] == {"Name": "orphan"}


@pytest.mark.usefixtures("moto_aws")
def test_boto3_inventory_snapshots_ami_mapping_and_orphans(book: PriceBook) -> None:
    ec2 = boto3.client("ec2", region_name="us-east-1")
    instance = _run_instance(ec2)
    image_id = ec2.create_image(InstanceId=instance["InstanceId"], Name="golden")["ImageId"]
    doomed = ec2.create_volume(AvailabilityZone="us-east-1a", Size=40)["VolumeId"]
    orphan_snap = ec2.create_snapshot(VolumeId=doomed)["SnapshotId"]
    ec2.delete_volume(VolumeId=doomed)

    inventory = Boto3AwsInventory("us-east-1")
    used = inventory.snapshot_ids_used_by_images()
    assert list(used.values()) == [[image_id]]
    ami_snap = next(iter(used))

    # moto stamps snapshots with the current time, so evaluate them from 100 days ahead.
    later = datetime.now(UTC) + timedelta(days=100)
    prices = book.aws
    default = find_old_snapshots(inventory, prices, 90, now=later)
    assert [f.resource_id for f in default] == [orphan_snap]
    assert default[0].details["source_volume_exists"] is False
    assert default[0].monthly_cost == pytest.approx(40 * 0.05)

    with_ami = find_old_snapshots(inventory, prices, 90, include_ami_snapshots=True, now=later)
    assert {f.resource_id for f in with_ami} == {orphan_snap, ami_snap}

    assert find_old_snapshots(inventory, prices, 90) == []  # nothing is 90 days old today


@pytest.mark.usefixtures("moto_aws")
def test_boto3_inventory_unused_eips(book: PriceBook) -> None:
    ec2 = boto3.client("ec2", region_name="us-east-1")
    instance = _run_instance(ec2)
    used = ec2.allocate_address(Domain="vpc")["AllocationId"]
    ec2.associate_address(AllocationId=used, InstanceId=instance["InstanceId"])
    free = ec2.allocate_address(Domain="vpc")["AllocationId"]

    findings = find_unused_eips(Boto3AwsInventory("us-east-1"), book.aws)

    assert [f.resource_id for f in findings] == [free]
    assert findings[0].monthly_cost == pytest.approx(3.65)
