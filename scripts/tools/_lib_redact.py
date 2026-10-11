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



# A credential value shorter than this is masked only where it is a whole
# value: as a substring, a short value ("0", "80") would mask the digits of
# every timestamp, path and threshold in the report.
MIN_SUBSTRING_SECRET = 8


class Secrets:
    """The credential values `credential_values` collected. Its repr and str
    never show them, so a log line or a traceback that prints the object
    holding it does not leak them. Not a set: a set operation on it would
    return a plain set that prints them, so it has none; iterate it, or
    compare it with a set."""

    __slots__ = ("_values",)

    def __init__(self, values: Any = ()) -> None:
        self._values = frozenset(values)

    def __iter__(self):
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __contains__(self, value: object) -> bool:
        return value in self._values

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Secrets):
            return self._values == other._values
        if isinstance(other, (set, frozenset)):
            return self._values == other
        return NotImplemented

    __hash__ = None  # type: ignore[assignment]

    def __repr__(self) -> str:
        return f"Secrets(<{len(self)} value(s)>)"

    __str__ = __repr__


def _number(v: Any) -> float | None:
    """`v` (text, or an int / float — never a bool, which callers pass by)
    as a finite number, when it reads as one; else None. A number is printed
    differently by each program that passes it on (Go serves `12345678` as
    `1.2345678e+07`), so it is matched by value, not by text."""
    if isinstance(v, (int, float)):
        try:
            f = float(v)
        except OverflowError:
            return None
    elif isinstance(v, str):
        try:
            f = float(v.strip())
        except ValueError:
            return None
    else:
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def credential_values(value: Any, *, text_key: Callable[[str], bool] = lambda _k: False) -> Secrets:
    """Every non-blank string, and every number (as its text), inside a
    credential field of `value`, a container field taken whole as `redact`
    takes it; booleans are not collected. A key `text_key` accepts whose
    value is a string (a receiver written as text, which no key name can see
    into) is collected whole too."""
    out: set[str] = set()

    def strings(v: Any) -> None:
        if isinstance(v, bool):
            return
        if isinstance(v, (int, float)):
            out.add(str(v))
        elif isinstance(v, str):
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
    return Secrets(out)


def _long_number(s: str) -> float | None:
    n = _number(s)
    if n is None or (len(s.strip()) < MIN_SUBSTRING_SECRET and abs(n) < 10 ** (MIN_SUBSTRING_SECRET - 1)):
        return None
    return n


def mask_values(value: Any, secrets: set[str] | frozenset[str]) -> tuple[Any, int]:
    """`value` with every credential value in `secrets` replaced by REDACTED
    wherever it occurs — a whole string, a number whose text it is, a
    mapping key, or, for a value at least MIN_SUBSTRING_SECRET long, a part
    of a longer string — and how many were. A value that reads as a number
    and is long (MIN_SUBSTRING_SECRET characters) or large (that many digits)
    masks any number, or text reading as one, of the same value; a short
    small one (`X-Scope-OrgID: "1"`) only its own text, or every threshold
    of 1 would be masked.

    Substrings: one pass of one pattern, longest first, so a secret that
    contains another is masked whole and REDACTED put in is never matched
    again. A mapping key that masking changes is numbered (`<redacted> #2`)
    clear of every other key, so no two keys merge; keys it leaves alone keep
    their names. Named tuples keep their type; any other object is printed
    by `str` (as the JSON report's `default=str` would) and masked."""
    if not secrets:
        return value, 0
    whole = frozenset(secrets)
    numbers = {n for n in map(_long_number, whole) if n is not None}
    long_ = sorted((s for s in secrets if len(s) >= MIN_SUBSTRING_SECRET), key=len, reverse=True)
    pattern = re.compile("|".join(re.escape(s) for s in long_)) if long_ else None
    count = 0

    def text(s: str) -> str:
        nonlocal count
        if s in whole or (numbers and _number(s) in numbers):
            count += 1
            return REDACTED
        if pattern is None:
            return s
        new, n = pattern.subn(REDACTED, s)
        count += n
        return new

    def keys(mapping: dict) -> list:
        new = [text(k) if isinstance(k, str) else walk(k) for k in mapping]
        taken = {n for k, n in zip(mapping, new) if n == k}
        out = []
        for k, n in zip(mapping, new):
            if n != k:
                base, i = n, 2
                while n in taken:
                    n, i = f"{base} #{i}", i + 1
                taken.add(n)
            out.append(n)
        return out

    def walk(v: Any) -> Any:
        nonlocal count
        if isinstance(v, str):
            return text(v)
        if v is None or isinstance(v, bool):
            return v
        if isinstance(v, (int, float)):
            if str(v) in whole or _number(v) in numbers:
                count += 1
                return REDACTED
            return v
        if isinstance(v, dict):
            return {n: walk(x) for n, x in zip(keys(v), v.values())}
        if isinstance(v, tuple) and hasattr(v, "_fields"):
            return type(v)._make(walk(x) for x in v)
        if isinstance(v, (list, tuple, set, frozenset)):
            return type(v)(walk(x) for x in v)
        return text(str(v))

    return walk(value), count
