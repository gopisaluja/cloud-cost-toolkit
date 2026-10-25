"""``cloudcost`` command-line interface.

The CLI is a thin shell: it resolves regions/projects, builds inventories through injectable
factories, calls the pure check functions, and renders a :class:`~cloudcost.report.Report`.
Tests swap the factories for fakes via ``CliRunner.invoke(cli, ..., obj={...})``.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import click
from botocore.exceptions import BotoCoreError, ClientError
from google.api_core.exceptions import GoogleAPIError
from google.auth.exceptions import GoogleAuthError

from cloudcost import __version__
from cloudcost.checks import aws as aws_checks
from cloudcost.checks import gcp as gcp_checks
from cloudcost.checks import nat
from cloudcost.errors import CloudCostError, ConfigurationError
from cloudcost.models import Finding, sort_by_cost
from cloudcost.pricing import PriceBook
from cloudcost.providers.aws import AwsInventory, Boto3AwsInventory, enabled_regions, make_session
from cloudcost.providers.gcp import ComputeGcpInventory, GcpInventory
from cloudcost.report import FORMATS, Format, Report, findings_report, nat_report, render

AwsFactory = Callable[[str, str | None], AwsInventory]
"""(region, profile) -> inventory"""
GcpFactory = Callable[[str | None], GcpInventory]
"""(project or None for the ADC default) -> inventory"""
RegionResolver = Callable[[str | None, bool], list[str]]
"""(profile, all_regions) -> regions, used when --region is not given"""

FINDINGS_EXIT_CODE = 3  # 2 is click's exit status for usage errors


def default_aws_factory(region: str, profile: str | None) -> AwsInventory:
    return Boto3AwsInventory(region, make_session(profile))


def default_region_resolver(profile: str | None, all_regions: bool) -> list[str]:
    session = make_session(profile)
    if all_regions:
        return enabled_regions(session)
    if session.region_name:
        return [session.region_name]
    raise ConfigurationError(
        "no AWS region configured: pass --region or --all-regions, "
        "or set AWS_REGION / a region in your AWS profile"
    )


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class App:
    """Per-invocation state shared by all commands."""

    prices: PriceBook
    fmt: Format = "table"
    fail_on_findings: bool = False
    aws_factory: AwsFactory = default_aws_factory
    gcp_factory: GcpFactory = ComputeGcpInventory.from_default_credentials
    region_resolver: RegionResolver = default_region_resolver
    now: Callable[[], datetime] = _utcnow
    aws_regions: tuple[str, ...] = ()
    aws_all_regions: bool = False
    aws_profile: str | None = None
    gcp_project: str | None = None

    def resolve_aws_regions(self) -> list[str]:
        if self.aws_regions and self.aws_all_regions:
            raise ConfigurationError("use either --region or --all-regions, not both")
        if self.aws_regions:
            return list(dict.fromkeys(self.aws_regions))  # de-duplicate, keep order
        return self.region_resolver(self.aws_profile, self.aws_all_regions)

    def aws_inventories(self) -> Iterator[AwsInventory]:
        for region in self.resolve_aws_regions():
            yield self.aws_factory(region, self.aws_profile)

    def price_note(self) -> str:
        note = f": {self.prices.note}" if self.prices.note else ""
        return f"Prices from {self.prices.source}{note}"

    def emit(self, report: Report, *, has_findings: bool) -> None:
        render(report, self.fmt, sys.stdout)
        for warning in self.prices.warnings:
            click.echo(f"warning: {warning}", err=True)
        if self.fail_on_findings and has_findings:
            raise click.exceptions.Exit(FINDINGS_EXIT_CODE)

    def emit_findings(self, title: str, findings: Sequence[Finding], notes: Sequence[str]) -> None:
        report = findings_report(
            title, sort_by_cost(findings), self.prices.currency, [*notes, self.price_note()]
        )
        self.emit(report, has_findings=bool(findings))


class _CloudCostGroup(click.Group):
    """Turns expected errors (bad config, missing credentials, API errors) into one-line
    messages with exit status 1 instead of tracebacks."""

    def invoke(self, ctx: click.Context) -> Any:
        try:
            return super().invoke(ctx)
        except CloudCostError as exc:
            raise click.ClickException(str(exc)) from exc
        except (BotoCoreError, ClientError) as exc:
            raise click.ClickException(f"AWS: {exc}") from exc
        except GoogleAuthError as exc:
            raise click.ClickException(f"GCP credentials: {exc}") from exc
        except GoogleAPIError as exc:
            raise click.ClickException(f"GCP API: {exc}") from exc


@click.group(cls=_CloudCostGroup, context_settings={"help_option_names": ["-h", "--help"]})
@click.option(
    "--pricing",
    type=click.Path(dir_okay=False, path_type=Path),
    envvar="CLOUDCOST_PRICING",
    help="YAML price list to use instead of the built-in example prices.",
)
@click.option(
    "-f",
    "--format",
    "fmt",
    type=click.Choice(FORMATS),
    default="table",
    show_default=True,
    help="Output format.",
)
@click.option(
    "--fail-on-findings",
    is_flag=True,
    help=f"Exit with status {FINDINGS_EXIT_CODE} when anything is reported (CI / scheduled jobs).",
)
@click.version_option(__version__, prog_name="cloudcost")
@click.pass_context
def cli(ctx: click.Context, pricing: Path | None, fmt: Format, fail_on_findings: bool) -> None:
    """Find cloud waste and estimate its monthly cost.

    Read-only by design: cloudcost never modifies or deletes anything. Credentials come
    from the standard boto3 chain (AWS) and Application Default Credentials (GCP).
    """
    overrides: dict[str, Any] = ctx.obj if isinstance(ctx.obj, dict) else {}
    ctx.obj = App(
        prices=PriceBook.load(pricing), fmt=fmt, fail_on_findings=fail_on_findings, **overrides
    )


# --------------------------------------------------------------------------------------- AWS


@cli.group()
@click.option(
    "--region",
    "regions",
    multiple=True,
    metavar="REGION",
    help="Region to scan; repeat for several. Default: the SDK's configured region.",
)
@click.option("--all-regions", is_flag=True, help="Scan every region enabled for the account.")
@click.option("--profile", default=None, help="Named AWS profile (default: credential chain).")
@click.pass_obj
def aws(app: App, regions: tuple[str, ...], all_regions: bool, profile: str | None) -> None:
    """AWS checks (read-only EC2 and CloudWatch calls)."""
    app.aws_regions = regions
    app.aws_all_regions = all_regions
    app.aws_profile = profile


@aws.command("idle-ebs")
@click.pass_obj
def aws_idle_ebs(app: App) -> None:
    """Unattached EBS volumes and what they cost per month."""
    findings = [
        finding
        for inv in app.aws_inventories()
        for finding in aws_checks.find_idle_ebs(inv, app.prices.aws)
    ]
    app.emit_findings(
        "AWS: unattached EBS volumes",
        findings,
        ["Unattached volumes are billed in full (capacity + provisioned IOPS/throughput)."],
    )


@aws.command("old-snapshots")
@click.option(
    "--days",
    type=click.IntRange(min=0),
    default=90,
    show_default=True,
    help="Report snapshots at least this many days old.",
)
@click.option(
    "--include-ami-snapshots", is_flag=True, help="Also report snapshots that back an AMI."
)
@click.pass_obj
def aws_old_snapshots(app: App, days: int, include_ami_snapshots: bool) -> None:
    """EBS snapshots owned by this account and older than --days."""
    now = app.now()
    findings = [
        finding
        for inv in app.aws_inventories()
        for finding in aws_checks.find_old_snapshots(
            inv,
            app.prices.aws,
            days,
            include_ami_snapshots=include_ami_snapshots,
            now=now,
        )
    ]
    notes = [
        "Snapshot cost is an upper bound (source volume size x snapshot price); snapshots "
        "are incremental, so the billed size is usually smaller."
    ]
    if not include_ami_snapshots:
        notes.append("Snapshots backing registered AMIs are excluded (--include-ami-snapshots).")
    app.emit_findings(f"AWS: EBS snapshots older than {days} days", findings, notes)


@aws.command("unused-eips")
@click.pass_obj
def aws_unused_eips(app: App) -> None:
    """Elastic IPs not associated with an instance or network interface."""
    findings = [
        finding
        for inv in app.aws_inventories()
        for finding in aws_checks.find_unused_eips(inv, app.prices.aws)
    ]
    app.emit_findings(
        "AWS: unassociated Elastic IPs",
        findings,
        ["Public IPv4 addresses are billed hourly whether or not they are associated."],
    )


@aws.command("nat-vs-endpoint")
@click.option(
    "--gb-per-month",
    type=click.FloatRange(min=0),
    default=None,
    help="NAT-processed traffic in GB/month. Skips CloudWatch (no AWS calls are made).",
)
@click.option(
    "--endpoint-share",
    type=click.FloatRange(0, 100),
    default=100.0,
    show_default=True,
    help="Percent of NAT traffic destined for S3/DynamoDB in the same region.",
)
@click.option(
    "--days",
    type=click.IntRange(1, 455),
    default=30,
    show_default=True,
    help="CloudWatch lookback window in days.",
)
@click.option(
    "--nat-gateway-id",
    "nat_gateway_ids",
    multiple=True,
    help="Only these NAT gateways (repeatable; requires a single region).",
)
@click.option(
    "--outbound-only",
    is_flag=True,
    help="Count only BytesOutToDestination (a lower bound on processed bytes).",
)
@click.pass_obj
def aws_nat_vs_endpoint(
    app: App,
    gb_per_month: float | None,
    endpoint_share: float,
    days: int,
    nat_gateway_ids: tuple[str, ...],
    outbound_only: bool,
) -> None:
    """Estimate NAT gateway processing cost that an S3/DynamoDB gateway endpoint would remove."""
    share = endpoint_share / 100
    if gb_per_month is not None:
        if nat_gateway_ids or outbound_only:
            raise ConfigurationError(
                "--nat-gateway-id/--outbound-only apply to CloudWatch mode, not --gb-per-month"
            )
        regions = app.resolve_aws_regions()
        if len(regions) != 1:
            raise ConfigurationError("--gb-per-month needs exactly one region for pricing")
        estimates = [nat.estimate(gb_per_month, share, app.prices.aws(regions[0]))]
        notes = nat.assumptions(share)
    else:
        if nat_gateway_ids and len(app.resolve_aws_regions()) != 1:
            raise ConfigurationError("--nat-gateway-id needs exactly one --region")
        now = app.now()
        estimates = [
            estimate
            for inv in app.aws_inventories()
            for estimate in nat.estimate_from_cloudwatch(
                inv,
                app.prices.aws,
                days=days,
                endpoint_share=share,
                nat_gateway_ids=nat_gateway_ids,
                outbound_only=outbound_only,
                now=now,
            )
        ]
        notes = nat.assumptions(share, days=days, outbound_only=outbound_only)
    report = nat_report(estimates, app.prices.currency, [*notes, app.price_note()])
    app.emit(report, has_findings=any(e.monthly_savings > 0 for e in estimates))


# --------------------------------------------------------------------------------------- GCP


@cli.group()
@click.option(
    "--project", default=None, help="Project ID (default: from Application Default Credentials)."
)
@click.pass_obj
def gcp(app: App, project: str | None) -> None:
    """GCP checks (read-only Compute Engine calls)."""
    app.gcp_project = project


@gcp.command("idle-disks")
@click.option(
    "--min-idle-days",
    type=click.IntRange(min=0),
    default=0,
    show_default=True,
    help="Skip disks detached (or created, if never attached) more recently than this.",
)
@click.pass_obj
def gcp_idle_disks(app: App, min_idle_days: int) -> None:
    """Persistent disks not attached to any instance."""
    inventory = app.gcp_factory(app.gcp_project)
    findings = gcp_checks.find_idle_disks(
        inventory, app.prices.gcp, min_idle_days=min_idle_days, now=app.now()
    )
    app.emit_findings(
        f"GCP: unattached persistent disks in {inventory.project}",
        findings,
        [
            "Capacity cost only; provisioned IOPS/throughput (pd-extreme, Hyperdisk) is not "
            "included. Regional disks use regional_disk_multiplier from the pricing file."
        ],
    )


@gcp.command("unused-addresses")
@click.pass_obj
def gcp_unused_addresses(app: App) -> None:
    """Static external IP addresses that are reserved but not in use."""
    inventory = app.gcp_factory(app.gcp_project)
    findings = gcp_checks.find_unused_addresses(inventory, app.prices.gcp)
    app.emit_findings(
        f"GCP: reserved but unused external addresses in {inventory.project}",
        findings,
        ["Internal addresses are ignored; they are not billed as unused static IPs."],
    )


def main() -> None:
    cli(prog_name="cloudcost")
