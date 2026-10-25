from __future__ import annotations

from collections.abc import Iterator

import pytest
from moto import mock_aws

from cloudcost.pricing import PriceBook


@pytest.fixture(autouse=True)
def _offline_cloud_env(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory):
    """Make sure no test can pick up real credentials, profiles, or projects."""
    for var in (
        "AWS_PROFILE",
        "AWS_DEFAULT_PROFILE",
        "AWS_SESSION_TOKEN",
        "AWS_SECURITY_TOKEN",
        "AWS_REGION",
        "CLOUDCOST_PRICING",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_CLOUD_PROJECT",
    ):
        monkeypatch.delenv(var, raising=False)
    empty = tmp_path_factory.mktemp("aws")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_CONFIG_FILE", str(empty / "config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(empty / "credentials"))
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    # Otherwise moto pre-populates ~1,300 public AMIs and their snapshots in every region.
    monkeypatch.setenv("MOTO_EC2_LOAD_DEFAULT_AMIS", "false")


@pytest.fixture
def moto_aws() -> Iterator[None]:
    with mock_aws():
        yield


@pytest.fixture
def book() -> PriceBook:
    return PriceBook.load()
