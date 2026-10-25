"""Price lookup backed by a small YAML file.

The packaged ``data/pricing.yaml`` holds *example* prices for a handful of regions. Users are
expected to verify them and can point ``--pricing`` at their own file with the same shape.

The cost formulas live here, next to the prices they use, so that every check prices
resources the same way and the arithmetic can be unit-tested without any cloud SDK.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from cloudcost import HOURS_PER_MONTH
from cloudcost.errors import PricingError

# Allowed keys per region and whether each is a scalar price or a {type: price} table.
_AWS_REGION_KEYS: dict[str, type] = {
    "ebs_gb_month": dict,
    "ebs_iops_month": dict,
    "gp3_throughput_mibps_month": float,
    "snapshot_gb_month": float,
    "snapshot_archive_gb_month": float,
    "public_ipv4_hour": float,
    "nat_gateway_hour": float,
    "nat_gateway_gb": float,
}
_GCP_REGION_KEYS: dict[str, type] = {
    "disk_gb_month": dict,
    "static_ip_unused_hour": float,
}
_AWS_SECTION_KEYS = {"fallback_region", "gp3_included_iops", "gp3_included_throughput_mibps"}
_GCP_SECTION_KEYS = {"fallback_region", "regional_disk_multiplier"}


def monthly_from_hourly(rate_per_hour: float) -> float:
    """Convert an hourly rate into an average monthly cost (730 h)."""
    return rate_per_hour * HOURS_PER_MONTH


@dataclass(frozen=True)
class AwsPrices:
    """Prices for one AWS region. ``None`` means "not listed in the pricing file"."""

    region: str
    ebs_gb_month: Mapping[str, float] = field(default_factory=dict)
    ebs_iops_month: Mapping[str, float] = field(default_factory=dict)
    gp3_throughput_mibps_month: float | None = None
    snapshot_gb_month: float | None = None
    snapshot_archive_gb_month: float | None = None
    public_ipv4_hour: float | None = None
    nat_gateway_hour: float | None = None
    nat_gateway_gb: float | None = None
    gp3_included_iops: int = 3000
    gp3_included_throughput_mibps: int = 125

    def ebs_volume_monthly(
        self,
        volume_type: str,
        size_gib: int,
        iops: int | None = None,
        throughput_mibps: int | None = None,
    ) -> float | None:
        """Monthly cost of an EBS volume: capacity plus any separately billed performance.

        * ``gp3``: IOPS above 3,000 and throughput above 125 MiB/s are billed extra.
        * ``io1``/``io2``: every provisioned IOPS is billed (flat rate; io2 volume tiers
          above 32k IOPS are not modelled, so very large io2 volumes are over-estimated).
        * ``gp2``/``st1``/``sc1``/``standard``: capacity only; the API may report IOPS for
          gp2, but they are included in the storage price.
        """
        per_gb = self.ebs_gb_month.get(volume_type)
        if per_gb is None:
            return None
        cost = size_gib * per_gb

        iops_price = self.ebs_iops_month.get(volume_type)
        if iops and iops_price:
            included = self.gp3_included_iops if volume_type == "gp3" else 0
            cost += max(iops - included, 0) * iops_price

        if volume_type == "gp3" and throughput_mibps and self.gp3_throughput_mibps_month:
            extra = max(throughput_mibps - self.gp3_included_throughput_mibps, 0)
            cost += extra * self.gp3_throughput_mibps_month
        return cost

    def snapshot_monthly(self, size_gib: float, storage_tier: str = "standard") -> float | None:
        """Upper-bound monthly cost of a snapshot, treating it as a full copy of the volume."""
        rate = (
            self.snapshot_archive_gb_month if storage_tier == "archive" else self.snapshot_gb_month
        )
        return None if rate is None else size_gib * rate

    def public_ipv4_monthly(self) -> float | None:
        return None if self.public_ipv4_hour is None else monthly_from_hourly(self.public_ipv4_hour)


@dataclass(frozen=True)
class GcpPrices:
    """Prices for one GCP region (or ``global``)."""

    region: str
    disk_gb_month: Mapping[str, float] = field(default_factory=dict)
    static_ip_unused_hour: float | None = None
    regional_disk_multiplier: float = 2.0

    def disk_monthly(self, disk_type: str, size_gb: int, regional: bool = False) -> float | None:
        per_gb = self.disk_gb_month.get(disk_type)
        if per_gb is None:
            return None
        cost = size_gb * per_gb
        return cost * self.regional_disk_multiplier if regional else cost

    def static_ip_unused_monthly(self) -> float | None:
        if self.static_ip_unused_hour is None:
            return None
        return monthly_from_hourly(self.static_ip_unused_hour)


class PriceBook:
    """Validated view over a pricing document.

    Lookups for regions that are not in the file use the provider's ``fallback_region`` (if
    configured) and record a human-readable warning in :attr:`warnings`, which the CLI prints
    to stderr. Without a fallback, an unknown region raises :class:`PricingError`.
    """

    def __init__(self, data: Mapping[str, Any], source: str = "<memory>") -> None:
        _validate(data, source)
        self.source = source
        self.currency: str = str(data.get("currency", "USD"))
        self.note: str = str(data.get("note", ""))
        self._aws: Mapping[str, Any] = data.get("aws") or {}
        self._gcp: Mapping[str, Any] = data.get("gcp") or {}
        self.warnings: list[str] = []

    @classmethod
    def load(cls, path: str | Path | None = None) -> PriceBook:
        """Load ``path`` or, when omitted, the packaged example price list."""
        if path is None:
            ref = resources.files("cloudcost").joinpath("data/pricing.yaml")
            source = "built-in pricing.yaml"
            return cls(_parse_yaml(ref.read_text(encoding="utf-8"), source), source)
        p = Path(path)
        try:
            text = p.read_text(encoding="utf-8")
        except OSError as exc:
            raise PricingError(f"cannot read pricing file {p}: {exc.strerror}") from exc
        return cls(_parse_yaml(text, str(p)), str(p))

    def aws(self, region: str) -> AwsPrices:
        entry, resolved = self._region_entry("aws", self._aws, region)
        return AwsPrices(
            region=resolved,
            ebs_gb_month=dict(entry.get("ebs_gb_month") or {}),
            ebs_iops_month=dict(entry.get("ebs_iops_month") or {}),
            gp3_throughput_mibps_month=entry.get("gp3_throughput_mibps_month"),
            snapshot_gb_month=entry.get("snapshot_gb_month"),
            snapshot_archive_gb_month=entry.get("snapshot_archive_gb_month"),
            public_ipv4_hour=entry.get("public_ipv4_hour"),
            nat_gateway_hour=entry.get("nat_gateway_hour"),
            nat_gateway_gb=entry.get("nat_gateway_gb"),
            gp3_included_iops=int(self._aws.get("gp3_included_iops", 3000)),
            gp3_included_throughput_mibps=int(self._aws.get("gp3_included_throughput_mibps", 125)),
        )

    def gcp(self, region: str) -> GcpPrices:
        entry, resolved = self._region_entry("gcp", self._gcp, region)
        return GcpPrices(
            region=resolved,
            disk_gb_month=dict(entry.get("disk_gb_month") or {}),
            static_ip_unused_hour=entry.get("static_ip_unused_hour"),
            regional_disk_multiplier=float(self._gcp.get("regional_disk_multiplier", 2)),
        )

    def _region_entry(
        self, provider: str, section: Mapping[str, Any], region: str
    ) -> tuple[Mapping[str, Any], str]:
        regions: Mapping[str, Any] = section.get("regions") or {}
        if region in regions:
            return regions[region] or {}, region
        fallback = section.get("fallback_region")
        if fallback and fallback in regions:
            message = (
                f"no {provider} prices for region '{region}' in {self.source}; "
                f"using {fallback} prices instead"
            )
            if message not in self.warnings:
                self.warnings.append(message)
            return regions[fallback] or {}, fallback
        raise PricingError(
            f"no {provider} prices for region '{region}' in {self.source} "
            "and no usable fallback_region; add the region to your pricing file"
        )


def _parse_yaml(text: str, source: str) -> Mapping[str, Any]:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise PricingError(f"{source}: invalid YAML: {exc}") from exc
    if not isinstance(data, Mapping):
        raise PricingError(f"{source}: expected a mapping at the top level")
    return data


def _check_price(value: Any, where: str) -> None:
    # bool is an int subclass; "true" is never a meaningful price.
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise PricingError(f"{where}: expected a number, got {value!r}")
    if value < 0:
        raise PricingError(f"{where}: price cannot be negative ({value})")


def _validate(data: Mapping[str, Any], source: str) -> None:
    unknown_top = set(data) - {"note", "currency", "aws", "gcp"}
    if unknown_top:
        raise PricingError(f"{source}: unknown top-level key(s): {', '.join(sorted(unknown_top))}")

    for provider, section_keys, region_keys in (
        ("aws", _AWS_SECTION_KEYS, _AWS_REGION_KEYS),
        ("gcp", _GCP_SECTION_KEYS, _GCP_REGION_KEYS),
    ):
        section = data.get(provider)
        if section is None:
            continue
        if not isinstance(section, Mapping):
            raise PricingError(f"{source}: '{provider}' must be a mapping")
        unknown = set(section) - section_keys - {"regions"}
        if unknown:
            raise PricingError(f"{source}: unknown key(s) in '{provider}': {sorted(unknown)}")
        fallback = section.get("fallback_region")
        if fallback is not None and not isinstance(fallback, str):
            raise PricingError(f"{source}: {provider}.fallback_region must be a region name")
        for key in section_keys - {"fallback_region"}:
            if key in section:
                _check_price(section[key], f"{source}: {provider}.{key}")

        regions = section.get("regions") or {}
        if not isinstance(regions, Mapping):
            raise PricingError(f"{source}: '{provider}.regions' must be a mapping")
        for region, entry in regions.items():
            where = f"{source}: {provider}.regions.{region}"
            if entry is None:
                continue
            if not isinstance(entry, Mapping):
                raise PricingError(f"{where}: must be a mapping")
            for key, value in entry.items():
                kind = region_keys.get(key)
                if kind is None:
                    raise PricingError(f"{where}: unknown key '{key}'")
                if kind is dict:
                    if not isinstance(value, Mapping):
                        raise PricingError(f"{where}.{key}: expected a mapping of type -> price")
                    for type_name, price in value.items():
                        _check_price(price, f"{where}.{key}.{type_name}")
                else:
                    _check_price(value, f"{where}.{key}")
