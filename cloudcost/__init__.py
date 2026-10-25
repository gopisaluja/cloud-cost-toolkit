"""cloudcost: read-only discovery of common cloud waste with monthly cost estimates."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("cloud-cost-toolkit")
except PackageNotFoundError:  # pragma: no cover - running from a source tree without install
    __version__ = "0.0.0"

HOURS_PER_MONTH = 730
"""Hours in an average month; the convention both AWS and GCP use on their pricing pages."""

__all__ = ["HOURS_PER_MONTH", "__version__"]
