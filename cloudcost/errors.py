"""Exception types raised by cloudcost.

Everything the CLI should turn into a clean, one-line error message derives from
:class:`CloudCostError`; anything else is a bug and is allowed to surface with a traceback.
"""


class CloudCostError(Exception):
    """Base class for expected, user-facing errors."""


class PricingError(CloudCostError):
    """The pricing file is missing, malformed, or has no entry for the requested region."""


class ConfigurationError(CloudCostError):
    """Missing or contradictory runtime configuration (region, project, CLI options)."""
