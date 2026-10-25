# cloud-cost-toolkit

A small, read-only Python CLI (`cloudcost`) that finds common sources of cloud waste on AWS and
GCP and estimates what each one costs per month. It also models how much NAT gateway data
processing an S3/DynamoDB gateway VPC endpoint would remove.

It is built for the "first pass" of a FinOps review: list the obvious leftovers, put an
approximate price on them, and hand a JSON or CSV report to whoever owns the resources.
It never changes or deletes anything.

## What it finds

| Command | Finds | How the monthly cost is estimated |
| --- | --- | --- |
| `aws idle-ebs` | EBS volumes in the `available` state (attached to nothing) | GiB x price per GB-month, plus gp3 IOPS above 3,000 and throughput above 125 MiB/s, plus all provisioned io1/io2 IOPS |
| `aws old-snapshots --days N` | Account-owned EBS snapshots at least N days old; flags snapshots whose source volume no longer exists; skips snapshots behind a registered AMI unless `--include-ami-snapshots` | Upper bound: source volume size x snapshot price (standard or archive tier) |
| `aws unused-eips` | Elastic IPs not associated with an instance or network interface | Public IPv4 hourly price x 730 h |
| `aws nat-vs-endpoint` | NAT gateway data-processing cost that S3/DynamoDB gateway endpoints would remove | See [How the NAT vs. endpoint estimate works](#how-the-nat-vs-endpoint-estimate-works) |
| `gcp idle-disks` | `READY` persistent disks with no attached instances (optionally only those idle for `--min-idle-days`) | GB x price per GB-month; regional disks x `regional_disk_multiplier` |
| `gcp unused-addresses` | Static external IPs (regional and global) that are `RESERVED` but not used | Unused static IP hourly price x 730 h |

Every command supports `--format table` (default), `json` and `csv`.

## Example output

Illustrative data, generated against a mocked account:

```text
$ cloudcost aws --region us-east-1 idle-ebs
AWS: unattached EBS volumes

Resource               Location    Details                                                                  Est. monthly (USD)
---------------------  ----------  -----------------------------------------------------------------------  ------------------
vol-9e271f4d0ef14c6a0  us-east-1b  io2 200 GiB, 4000 IOPS, Name=old-db-replica, created 2025-12-02                      285.00
vol-10e5c237b5ca17f5a  us-east-1a  gp3 500 GiB, 6000 IOPS, 250 MiB/s, Name=etl-scratch, created 2026-03-15               60.00
vol-0c6c59b7ec8028dcb  us-east-1a  gp2 100 GiB, created 2025-09-30                                                       10.00

3 items; estimated monthly cost: 355.00 USD

Notes:
  - Unattached volumes are billed in full (capacity + provisioned IOPS/throughput).
  - Prices from built-in pricing.yaml: Example list prices only - verify against current provider pricing before relying on them.
```

```text
$ cloudcost aws --region us-east-1 old-snapshots --days 90
AWS: EBS snapshots older than 90 days

Resource                Location   Details                                                       Est. monthly (USD)
----------------------  ---------  ------------------------------------------------------------  ------------------
snap-1ef948296c8bf1743  us-east-1  120 days old, 250 GiB source volume, source volume not found               12.50
snap-caa47f5cf9f81b488  us-east-1  120 days old, 80 GiB source volume                                          4.00

2 items; estimated monthly cost: 16.50 USD
```

```text
$ cloudcost aws --region us-east-1 nat-vs-endpoint --endpoint-share 60
AWS: NAT gateway processing vs. S3/DynamoDB gateway endpoints

NAT gateway            Region     GB/month  Endpoint share  Processing now (USD)  Est. savings (USD)  Processing after (USD)  NAT hourly (USD)
---------------------  ---------  --------  --------------  --------------------  ------------------  ----------------------  ----------------
nat-3b838c986ab692322  us-east-1   2,433.3             60%                109.50               65.70                   43.80             32.85

1 item; estimated monthly savings: 65.70 USD

Notes:
  - Processed GB/month = CloudWatch BytesOutToDestination + BytesInFromDestination over the last 30 day(s), scaled to a 730-hour month (1 GB = 1024^3 bytes).
  - Endpoint share 60% is an input, not a measurement: CloudWatch does not show destinations; measure it with VPC Flow Logs.
  - S3 and DynamoDB gateway endpoints have no hourly or per-GB charge.
  - ...
```

```text
$ cloudcost gcp --project my-project idle-disks --min-idle-days 7
GCP: unattached persistent disks in my-project

Resource          Location       Details                                                  Est. monthly (USD)
----------------  -------------  -------------------------------------------------------  ------------------
old-jenkins-data  us-central1-a  pd-ssd 500 GB, detached 2026-08-21                                    85.00
pg-replica-ha     europe-west1   pd-balanced 200 GB, regional, detached 2026-10-03                     40.00
tmp-restore       us-central1-b  pd-standard 1000 GB, created, never attached 2026-10-12               40.00

3 items; estimated monthly cost: 165.00 USD
```

Machine-readable output for reports and dashboards:

```text
$ cloudcost -f csv aws --region us-east-1 idle-ebs
provider,check,resource_id,location,summary,monthly_cost
aws,idle-ebs,vol-9e271f4d0ef14c6a0,us-east-1b,"io2 200 GiB, 4000 IOPS, Name=old-db-replica, created 2025-12-02",285.0
...

$ cloudcost -f json aws --region us-east-1 unused-eips
{
  "report": "AWS: unassociated Elastic IPs",
  "currency": "USD",
  "count": 2,
  "total": 7.3,
  "total_label": "estimated monthly cost",
  "rows": [
    {
      "provider": "aws",
      "check": "unused-eips",
      "resource_id": "eipalloc-0577b04ee0443e89e",
      "location": "us-east-1",
      "summary": "203.0.113.24 not associated",
      "monthly_cost": 3.65,
      "details": { "public_ip": "203.0.113.24", "tags": {} }
    },
    ...
  ],
  "notes": ["..."]
}
```

JSON rows carry a `details` object with the raw attributes (sizes, tags/labels, ages, AMI ids,
whether the source volume still exists) so reports can be filtered or grouped downstream.

## Install

Requires Python 3.11+.

```bash
git clone <this repository>
cd cloud-cost-toolkit
python3 -m venv .venv && . .venv/bin/activate
pip install .            # or: pip install -e '.[dev]' for development
cloudcost --help
```

## Usage

```text
cloudcost [--pricing FILE] [-f table|json|csv] [--fail-on-findings] COMMAND ...

cloudcost aws [--region R]... [--all-regions] [--profile P] idle-ebs
cloudcost aws ... old-snapshots [--days 90] [--include-ami-snapshots]
cloudcost aws ... unused-eips
cloudcost aws ... nat-vs-endpoint [--endpoint-share PCT] [--days 30]
                                   [--nat-gateway-id ID]... [--outbound-only]
cloudcost aws --region R nat-vs-endpoint --gb-per-month GB [--endpoint-share PCT]

cloudcost gcp [--project ID] idle-disks [--min-idle-days N]
cloudcost gcp [--project ID] unused-addresses
```

Examples:

```bash
# Several regions in one report, written to CSV
cloudcost -f csv aws --region us-east-1 --region eu-west-1 idle-ebs > idle-ebs.csv

# Every enabled region, using a named profile
cloudcost aws --profile audit --all-regions unused-eips

# Snapshots older than 180 days, including those that back AMIs
cloudcost aws --region ap-south-1 old-snapshots --days 180 --include-ami-snapshots

# NAT estimate without any AWS access (e.g. from a billing export figure)
cloudcost aws --region eu-west-1 nat-vs-endpoint --gb-per-month 2000 --endpoint-share 60

# Scheduled job / CI gate: exit status 3 when anything is found
cloudcost --fail-on-findings -f json gcp --project my-project unused-addresses
```

Exit status: `0` success, `1` configuration/credential/API error (printed as a one-line
message), `2` invalid command-line usage, `3` findings present and `--fail-on-findings` was
given. Price-fallback warnings go to
stderr, so stdout stays clean JSON/CSV.

## Credentials and permissions

No credentials are stored or read by the tool itself; it uses the standard SDK chains:

- **AWS**: the boto3 default credential chain (environment variables, `~/.aws` profiles, SSO,
  instance/task roles). `--profile` selects a named profile. Without `--region` or
  `--all-regions`, the SDK's configured region is used.
- **GCP**: Application Default Credentials (`gcloud auth application-default login`, a
  workload identity, or `GOOGLE_APPLICATION_CREDENTIALS`). Without `--project`, the ADC
  project is used.

### AWS IAM policy (read-only)

Also in [`policies/aws-readonly-policy.json`](policies/aws-readonly-policy.json):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "CloudCostToolkitEc2ReadOnly",
      "Effect": "Allow",
      "Action": [
        "ec2:DescribeVolumes",
        "ec2:DescribeSnapshots",
        "ec2:DescribeImages",
        "ec2:DescribeAddresses",
        "ec2:DescribeNatGateways",
        "ec2:DescribeRegions"
      ],
      "Resource": "*"
    },
    {
      "Sid": "CloudCostToolkitNatMetrics",
      "Effect": "Allow",
      "Action": ["cloudwatch:GetMetricStatistics"],
      "Resource": "*"
    }
  ]
}
```

`ec2:DescribeRegions` is only needed for `--all-regions`; `cloudwatch:GetMetricStatistics`
only for `nat-vs-endpoint` without `--gb-per-month`. The AWS managed `ReadOnlyAccess` policy
also covers everything.

### GCP roles

`roles/compute.viewer` on the project is sufficient. A minimal custom role needs:
`compute.disks.list`, `compute.addresses.list`, `compute.globalAddresses.list`.

## Pricing

Prices come from a small YAML file. The built-in one,
[`cloudcost/data/pricing.yaml`](cloudcost/data/pricing.yaml), contains **example on-demand list
prices** for a few regions (`us-east-1`, `eu-west-1`, `ap-south-1`, `us-central1`,
`europe-west1`, `asia-south1`). They are illustrative, not authoritative, and not tied to any
date. **Verify them against the current AWS and Google Cloud pricing pages (or your own billing
data) before quoting any number**, and remember that contracts, credits, Savings Plans and
committed-use discounts change the real figure.

Use your own file with `--pricing my-prices.yaml` or `CLOUDCOST_PRICING=my-prices.yaml`:

```yaml
note: Internal rate card, reviewed by the platform team.
currency: USD
aws:
  fallback_region: us-east-1        # optional: used (with a warning) for unlisted regions
  regions:
    us-east-1:
      ebs_gb_month: {gp2: 0.10, gp3: 0.08, io2: 0.125}
      ebs_iops_month: {gp3: 0.005, io2: 0.065}
      gp3_throughput_mibps_month: 0.04
      snapshot_gb_month: 0.05
      snapshot_archive_gb_month: 0.0125
      public_ipv4_hour: 0.005
      nat_gateway_hour: 0.045
      nat_gateway_gb: 0.045
gcp:
  fallback_region: us-central1
  regional_disk_multiplier: 2
  regions:
    us-central1:
      disk_gb_month: {pd-standard: 0.04, pd-balanced: 0.10, pd-ssd: 0.17}
      static_ip_unused_hour: 0.01
    global:
      static_ip_unused_hour: 0.01
```

The file is validated on load: unknown keys (typos), negative or non-numeric prices are
rejected with a message naming the offending key. Missing keys are allowed; affected findings
are still listed with the cost shown as `n/a` rather than silently dropped. Regions without
an entry use `fallback_region` and print a warning; without a fallback they are an error.
Hourly prices are converted with 730 hours per month.

## How the NAT vs. endpoint estimate works

Traffic from private subnets to S3 or DynamoDB normally goes through a NAT gateway, which
charges per GB processed. A **gateway** VPC endpoint for S3 or DynamoDB is added to the
subnet route tables and carries that traffic privately with no hourly or per-GB charge. So the
saving is the NAT processing charge for the share of traffic that goes to those two services:

```text
processed GB/month   = (BytesOutToDestination + BytesInFromDestination) over the last --days
                       / 1024^3  x  730 / (days x 24)
current processing   = processed GB/month x nat_gateway_gb
estimated savings    = current processing x endpoint share
processing after     = current processing - estimated savings
NAT hourly (context) = nat_gateway_hour x 730          (unchanged unless the NAT is removed)
```

Worked example with the example `us-east-1` price of 0.045 per GB: 2,000 GB/month through the
NAT, of which 60% goes to S3 in the same region, gives 2,000 x 0.045 = 90.00 of processing,
of which 54.00 per month would no longer be charged.

Inputs and assumptions:

- **Volume**: read from CloudWatch (`AWS/NATGateway`, daily `Sum` per `NatGatewayId`) for all
  available NAT gateways in the region, or a subset via `--nat-gateway-id`. Data processing is
  billed in both directions, so the tool adds traffic sent to destinations
  (`BytesOutToDestination`) and responses received from them (`BytesInFromDestination`).
  `--outbound-only` uses `BytesOutToDestination` alone as a lower bound. Alternatively pass `--gb-per-month`
  (for example from a billing export) and no AWS calls are made.
- **Endpoint share** (`--endpoint-share`, default 100%) is an input. CloudWatch cannot tell
  where traffic goes, so measure it from VPC Flow Logs on the NAT gateway's network interface
  (bytes to the S3/DynamoDB prefix lists for the region, divided by total bytes). The default
  of 100% is an upper bound and the report says so.
- Gateway endpoints only serve S3/DynamoDB **in the same region** as the VPC. Cross-region
  buckets and internet-bound traffic keep using the NAT gateway.
- Not modelled: cross-AZ data transfer to a NAT gateway in another AZ, internet egress (not
  affected by the change), and *interface* endpoints, which do have hourly and per-GB charges.
- Bytes are converted with 1 GB = 1024^3 bytes; the difference from decimal GB (about 7%) is
  small compared with the uncertainty in the endpoint share.

The tool does not create the endpoint. That is a routing change and belongs in your
infrastructure-as-code, for example a Terraform `aws_vpc_endpoint` with
`vpc_endpoint_type = "Gateway"` attached to the private route tables, or
`aws ec2 create-vpc-endpoint --vpc-endpoint-type Gateway --service-name com.amazonaws.<region>.s3 --vpc-id ... --route-table-ids ...`.
Endpoint policies and bucket policies that use `aws:SourceIp` should be reviewed first, because
traffic through the endpoint no longer comes from the NAT gateway's public IP.

## Design decisions and trade-offs

**Read-only by design.** Every provider call is a `Describe*`, `List*`, or `GetMetricStatistics`.
The tool can run with a read-only role in production accounts, from a laptop or a scheduled CI
job, without a change-approval process. The IAM policy above is the full permission set.

**Estimates, not invoices.** Costs are computed from resource attributes and a price table, not
from AWS Cost Explorer or the GCP billing export. That has trade-offs:

- *For*: results are per resource and available immediately, including for resources created
  today; no billing-account access is needed; the arithmetic is transparent and unit-tested.
- *Against*: list prices ignore discounts and credits; snapshot sizes are upper bounds because
  snapshots are incremental; tiered pricing (for example io2 above 32k IOPS) is simplified.
  Cost Explorer, the Cost and Usage Report, or the GCP billing export in BigQuery remain the
  source of truth for what was actually charged. Use this tool to find candidates, and the
  billing data to confirm the size of the prize.

**Why it never deletes anything.** "Unattached" and "old" are signals, not proof of waste: a
volume may be kept deliberately for a rollback, a snapshot may be a compliance backup, an IP may
be allow-listed by a partner. Deletion is irreversible and needs an owner's decision. The
intended workflow is: run the report, route findings to owners using tags/labels in the JSON
`details`, and let cleanup happen through the normal change process (infrastructure-as-code,
lifecycle policies such as Amazon Data Lifecycle Manager, or a reviewed script). Keeping the
tool read-only also keeps its permission footprint small enough to run everywhere.

**Unpriced is not invisible.** If a volume type or region has no price, the finding is still
reported with `n/a` and the region falls back with a visible warning, instead of being skipped.

**Testable boundaries.** Each provider sits behind a small `Protocol`
(`AwsInventory`, `GcpInventory`) returning plain dataclasses. The checks are pure functions of
an inventory and a price lookup, and the CLI is a thin layer that injects factories, so:

- logic is tested with in-memory fakes (thresholds, AMI exclusion, idle-day filtering, sorting,
  unpriced handling);
- the boto3 adapter is tested end-to-end against [moto](https://github.com/getmoto/moto)
  (EC2 and CloudWatch, fully offline);
- the GCP adapter is tested with fake Compute clients that mimic the real pagers;
- the CLI is tested with `click.testing.CliRunner` and injected fakes.

**Extending with a new check** (for example idle load balancers):

1. Add a small dataclass and a method to the provider protocol in `cloudcost/providers/`, and
   implement it in the SDK-backed adapter (read-only calls only).
2. Add any new prices to the pricing schema in `cloudcost/pricing.py` and the YAML file.
3. Write a pure function in `cloudcost/checks/` that returns `Finding` objects.
4. Add a CLI command in `cloudcost/cli.py` (usually a few lines) and the IAM permission to the
   policy above.
5. Test the logic with a fake inventory and the adapter with moto or a fake client.

## Project layout

```text
cloudcost/
  cli.py              click commands; wiring only
  pricing.py          price file loading/validation and cost formulas
  report.py           table / JSON / CSV rendering
  models.py           Finding dataclass, sorting, totals
  checks/             pure check functions (aws.py, nat.py, gcp.py)
  providers/          protocols + boto3 / google-cloud-compute adapters
  data/pricing.yaml   example prices (verify before use)
policies/             sample read-only IAM policy
tests/                pytest suite (moto for AWS, fakes for GCP; no network access)
```

## Development

```bash
make install   # venv + editable install with dev extras
make lint      # ruff check + ruff format --check
make test      # pytest (offline)
make check     # both, as in CI
```

CI (`.github/workflows/ci.yml`) runs ruff and pytest on Python 3.11 and 3.12.

## Limitations

- Only the checks listed above; compute right-sizing, idle load balancers, and storage-class
  analysis are natural next additions.
- AWS does not expose when a volume was detached, so `idle-ebs` has no idle-age filter (GCP's
  `idle-disks` does, using `lastDetachTimestamp`).
- GCP disk estimates cover capacity only; provisioned IOPS/throughput for Hyperdisk or
  `pd-extreme` is not priced.
- Prices are per region from a static file; there is no live price API lookup.

## License

[MIT](LICENSE)
