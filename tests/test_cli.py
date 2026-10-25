"""CLI wiring: options, region handling, output formats, exit codes, error messages."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any

import boto3
import pytest
from click.testing import CliRunner, Result

from cloudcost.cli import cli
from cloudcost.providers.aws import ElasticIp, NatGateway
from cloudcost.providers.gcp import Address

from .fakes import NOW, FakeAwsInventory, FakeGcpInventory, snapshot, volume

GIB = 1024**3


def invoke(args: list[str], **overrides: Any) -> Result:
    overrides.setdefault("now", lambda: NOW)
    return CliRunner().invoke(cli, args, obj=overrides)


def fake_aws_factory(inventories: dict[str, FakeAwsInventory]):
    def factory(region: str, profile: str | None) -> FakeAwsInventory:
        return inventories[region]

    return factory


def no_cloud(*_args: Any) -> Any:
    raise AssertionError("this command must not touch the cloud")


def test_idle_ebs_json_merges_regions_and_uses_regional_prices() -> None:
    inventories = {
        "us-east-1": FakeAwsInventory("us-east-1", volumes=[volume("vol-use1", "gp2", 100)]),
        "eu-west-1": FakeAwsInventory(
            "eu-west-1",
            volumes=[volume("vol-euw1", "gp2", 100, availability_zone="eu-west-1a")],
        ),
    }
    result = invoke(
        ["-f", "json", "aws", "--region", "us-east-1", "--region", "eu-west-1", "idle-ebs"],
        aws_factory=fake_aws_factory(inventories),
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert [(r["resource_id"], r["monthly_cost"]) for r in payload["rows"]] == [
        ("vol-euw1", 11.0),
        ("vol-use1", 10.0),
    ]
    assert payload["total"] == 21.0
    assert any("verify" in note for note in payload["notes"])


def test_fail_on_findings_sets_exit_code() -> None:
    busy = FakeAwsInventory(volumes=[volume("vol-1")])
    clean = FakeAwsInventory()
    args = ["--fail-on-findings", "aws", "--region", "us-east-1", "idle-ebs"]

    assert invoke(args, aws_factory=lambda r, p: busy).exit_code == 3
    assert invoke(args, aws_factory=lambda r, p: clean).exit_code == 0


def test_old_snapshots_passes_days_and_ami_flag() -> None:
    inv = FakeAwsInventory(
        snapshots=[snapshot("snap-ami", 40), snapshot("snap-new", 5)],
        images_by_snapshot={"snap-ami": ["ami-1"]},
    )
    base = ["-f", "csv", "aws", "--region", "us-east-1", "old-snapshots", "--days", "30"]

    without = invoke(base, aws_factory=lambda r, p: inv)
    assert list(csv.DictReader(io.StringIO(without.stdout))) == []

    with_ami = invoke([*base, "--include-ami-snapshots"], aws_factory=lambda r, p: inv)
    rows = list(csv.DictReader(io.StringIO(with_ami.stdout)))
    assert [r["resource_id"] for r in rows] == ["snap-ami"]


def test_nat_manual_mode_needs_no_cloud_access() -> None:
    result = invoke(
        [
            "aws",
            "--region",
            "us-east-1",
            "nat-vs-endpoint",
            "--gb-per-month",
            "2000",
            "--endpoint-share",
            "60",
        ],
        aws_factory=no_cloud,
        region_resolver=no_cloud,
    )
    assert result.exit_code == 0, result.output
    assert "estimated monthly savings: 54.00 USD" in result.stdout  # 2000 x 0.045 x 60%
    assert "--gb-per-month" in result.stdout  # assumptions are printed


def test_nat_cloudwatch_mode_uses_inventory() -> None:
    inv = FakeAwsInventory(
        nat_gateways=[NatGateway("nat-1", "us-east-1", "vpc-1", "subnet-1", "available")],
        metric_bytes={("nat-1", "BytesOutToDestination"): 720 * GIB},
    )
    result = invoke(
        [
            "-f",
            "json",
            "aws",
            "--region",
            "us-east-1",
            "nat-vs-endpoint",
            "--days",
            "30",
            "--outbound-only",
        ],
        aws_factory=lambda r, p: inv,
    )
    assert result.exit_code == 0, result.output
    [row] = json.loads(result.stdout)["rows"]
    assert row["nat_gateway_id"] == "nat-1"
    assert row["gb_per_month"] == pytest.approx(730.0)  # 720 GiB in 720 h -> 730 h month
    assert [c[1] for c in inv.metric_calls] == ["BytesOutToDestination"]


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (
            [
                "aws",
                "--region",
                "us-east-1",
                "--region",
                "eu-west-1",
                "nat-vs-endpoint",
                "--gb-per-month",
                "10",
            ],
            "exactly one region",
        ),
        (
            [
                "aws",
                "--region",
                "us-east-1",
                "nat-vs-endpoint",
                "--gb-per-month",
                "10",
                "--nat-gateway-id",
                "nat-1",
            ],
            "CloudWatch mode",
        ),
        (["aws", "--region", "us-east-1", "nat-vs-endpoint", "--endpoint-share", "150"], "150"),
        (["aws", "--region", "us-east-1", "--all-regions", "idle-ebs"], "not both"),
    ],
)
def test_invalid_option_combinations(args: list[str], message: str) -> None:
    result = invoke(args, aws_factory=no_cloud, region_resolver=no_cloud)
    assert result.exit_code != 0
    assert message in result.output


def test_missing_region_gives_actionable_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AWS_DEFAULT_REGION")
    result = invoke(["aws", "unused-eips"], aws_factory=no_cloud)
    assert result.exit_code == 1
    assert "no AWS region configured" in result.output
    assert "Traceback" not in result.output


def test_custom_pricing_file_and_fallback_warning(tmp_path: Path) -> None:
    prices = tmp_path / "prices.yaml"
    prices.write_text(
        "note: internal rate card\n"
        "aws:\n"
        "  fallback_region: eu-central-1\n"
        "  regions:\n"
        "    eu-central-1:\n"
        "      public_ipv4_hour: 0.01\n"
    )
    inv = FakeAwsInventory(
        "me-south-1", addresses=[ElasticIp("192.0.2.1", "me-south-1", "eipalloc-1")]
    )
    result = invoke(
        ["--pricing", str(prices), "-f", "json", "aws", "--region", "me-south-1", "unused-eips"],
        aws_factory=lambda r, p: inv,
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["rows"][0]["monthly_cost"] == pytest.approx(7.3)
    assert "warning: no aws prices for region 'me-south-1'" in result.stderr


def test_broken_pricing_file_is_a_clean_error(tmp_path: Path) -> None:
    prices = tmp_path / "prices.yaml"
    prices.write_text("aws:\n  regions:\n    us-east-1:\n      nat_gateway_gb: -5\n")
    result = invoke(["--pricing", str(prices), "aws", "--region", "us-east-1", "unused-eips"])
    assert result.exit_code == 1
    assert "cannot be negative" in result.output


def test_gcp_commands_use_project_and_render_csv() -> None:
    seen: list[str | None] = []

    def factory(project: str | None) -> FakeGcpInventory:
        seen.append(project)
        return FakeGcpInventory(
            project=project or "adc-default",
            addresses=[Address("spare-ip", "us-central1", "203.0.113.9", "RESERVED", "EXTERNAL")],
        )

    result = invoke(
        ["-f", "csv", "gcp", "--project", "p-123", "unused-addresses"], gcp_factory=factory
    )
    assert result.exit_code == 0, result.output
    rows = list(csv.DictReader(io.StringIO(result.stdout)))
    assert [(r["resource_id"], r["monthly_cost"]) for r in rows] == [("spare-ip", "7.3")]

    result = invoke(["gcp", "idle-disks", "--min-idle-days", "7"], gcp_factory=factory)
    assert result.exit_code == 0, result.output
    assert "Nothing found." in result.stdout
    assert seen == ["p-123", None]


@pytest.mark.usefixtures("moto_aws")
def test_end_to_end_against_moto_in_two_regions() -> None:
    for region in ("us-east-1", "eu-west-1"):
        boto3.client("ec2", region_name=region).allocate_address(Domain="vpc")

    result = invoke(
        ["-f", "json", "aws", "--region", "us-east-1", "--region", "eu-west-1", "unused-eips"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert sorted(r["location"] for r in payload["rows"]) == ["eu-west-1", "us-east-1"]
    assert payload["total"] == pytest.approx(7.3)


@pytest.mark.usefixtures("moto_aws")
def test_all_regions_discovers_enabled_regions() -> None:
    boto3.client("ec2", region_name="ap-south-1").allocate_address(Domain="vpc")
    result = invoke(["-f", "json", "aws", "--all-regions", "unused-eips"])
    assert result.exit_code == 0, result.output
    assert [r["location"] for r in json.loads(result.stdout)["rows"]] == ["ap-south-1"]
    # Empty regions are never priced, so they do not produce price-fallback warnings.
    assert "warning" not in result.stderr


def test_version_and_help() -> None:
    assert CliRunner().invoke(cli, ["--version"]).output.startswith("cloudcost, version")
    help_text = CliRunner().invoke(cli, ["aws", "--help"]).output
    for command in ("idle-ebs", "old-snapshots", "unused-eips", "nat-vs-endpoint"):
        assert command in help_text
