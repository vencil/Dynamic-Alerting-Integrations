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

A rule by key name cannot see a credential that sits in a field not named
like one: a YAML alias (`url: &u …` and `owner: *u`) or a plain copy puts
the same string there, and loading the YAML leaves nothing to tell the two
apart (#2865). `credential_values` collects what the credential fields hold,
and `mask_values` replaces those strings wherever else they occur.
"""
from __future__ import annotations

import re
from typing import Any, Callable

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


def credential_values(value: Any, *, text_key: Callable[[str], bool] = lambda _k: False) -> set[str]:
    """Every non-blank string inside a credential field of `value`, a
    container field taken whole as `redact` takes it; numbers and booleans
    are not collected. A key `text_key` accepts whose value is a string
    (a receiver written as text, which no key name can see into) is
    collected whole too."""
    out: set[str] = set()

    def strings(v: Any) -> None:
        if isinstance(v, str):
            if v.strip():
                out.add(v)
        elif isinstance(v, dict):
            for x in v.values():
                strings(x)
        elif isinstance(v, (list, tuple, set, frozenset)):
            for x in v:
                strings(x)

    def walk(v: Any) -> None:
        if isinstance(v, dict):
            for k, x in v.items():
                if is_secret_field(k):
                    strings(x)
                elif isinstance(x, str) and text_key(str(k)):
                    strings(x)
                else:
                    walk(x)
        elif isinstance(v, (list, tuple, set, frozenset)):
            for x in v:
                walk(x)

    walk(value)
    return out


def mask_values(value: Any, secrets: set[str] | frozenset[str]) -> tuple[Any, int]:
    """`value` with every occurrence of a string in `secrets`, inside any
    string or mapping key in it, replaced by REDACTED; and how many were.

    One pass of one pattern, longest first: a secret that contains another
    is masked whole, and REDACTED put in is never matched again. Named
    tuples keep their type; any other object is printed by `str` (as the
    JSON report's `default=str` would) and masked."""
    if not secrets:
        return value, 0
    pattern = re.compile("|".join(re.escape(s) for s in sorted(secrets, key=len, reverse=True)))
    count = 0

    def text(s: str) -> str:
        nonlocal count
        new, n = pattern.subn(REDACTED, s)
        count += n
        return new

    def walk(v: Any) -> Any:
        if isinstance(v, str):
            return text(v)
        if v is None or isinstance(v, (bool, int, float)):
            return v
        if isinstance(v, dict):
            return {(text(k) if isinstance(k, str) else walk(k)): walk(x) for k, x in v.items()}
        if isinstance(v, tuple) and hasattr(v, "_fields"):
            return type(v)._make(walk(x) for x in v)
        if isinstance(v, (list, tuple, set, frozenset)):
            return type(v)(walk(x) for x in v)
        return text(str(v))

    return walk(value), count
