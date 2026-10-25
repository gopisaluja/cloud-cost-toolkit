"""Thin adapters between cloud SDKs and the provider-neutral checks.

Each module exposes:

* small frozen dataclasses describing only the fields the checks need,
* a ``Protocol`` the checks depend on (so tests can pass simple fakes), and
* one concrete implementation backed by the official SDK, using the default
  credential chain (boto3 / Google Application Default Credentials).

Adapters only *read*. No method in this package creates, modifies or deletes resources.
"""
