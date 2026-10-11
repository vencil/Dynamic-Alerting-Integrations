"""Which config fields hold a credential, and how a report prints them.

One rule for every place that shows receiver configuration: tenant-api's
masked reads (`components/tenant-api/internal/credmask`, #1560) and the
reports config-diff posts as PR comments (mailed to every watcher, kept after
a history rewrite purges the file). `SECRET_FIELDS` is credmask's
`credentialKeys`, compared case-insensitively as there;
tests/ops/test_lib_redact.py reads the Go map and fails when the two differ.

As in credmask the rule is by key name anywhere in the value, not by receiver
path, and containers are redacted whole: `http_config`, `tls_config` and the
header maps can carry a secret in more places than a field list keeps up
with. It follows Alertmanager, which types these fields `Secret` /
`SecretURL` and marshals them as `<secret>`.

A changed credential is still reported as changed — like `kubectl diff`,
which prints a changed Secret value as `*** (before)` / `*** (after)` — with
both values replaced by `REDACTED`.
"""
from __future__ import annotations

from typing import Any

REDACTED = "<redacted>"

# Mirror of credmask.credentialKeys (Go), lower case.
SECRET_FIELDS = frozenset({
    "http_config", "tls_config", "headers", "http_headers",
    "proxy_connect_header", "endpoint_params",
    "url", "api_url", "webhook_url", "proxy_url",
    "routing_key", "service_key", "auth_password", "auth_secret", "password",
    "bearer_token", "credentials", "client_secret", "api_key", "token",
    "bot_token", "user_key",
})


def is_secret_field(name: object) -> bool:
    return str(name).lower() in SECRET_FIELDS


def redact(value: Any) -> Any:
    """`value` with every credential field inside it replaced by REDACTED.

    Walks mappings and lists: adding a whole receiver, or a list of them,
    carries its credentials inside a value that is not itself named like one.
    """
    if isinstance(value, dict):
        return {k: (REDACTED if is_secret_field(k) and v is not None else redact(v))
                for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


def redact_pair(key: object, old: Any, new: Any) -> tuple[Any, Any]:
    """The two sides of one changed field as a report prints them: REDACTED
    for a credential field (None stays None, so added / removed still read),
    every credential inside them redacted otherwise."""
    if is_secret_field(key):
        return (None if old is None else REDACTED, None if new is None else REDACTED)
    return redact(old), redact(new)
