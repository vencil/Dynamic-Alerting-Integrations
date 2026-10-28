"""_lib_tenant_state.py — Read tenant-api's state records as tenant-api reads them (#2883, ADR-036 §12).

Since #2856, groups, saved views, the federation whitelist and per-tenant
subsets, and the AccountID registry are tenant-api's own records: they live
in the ``tenant-api-state`` ConfigMap (Kubernetes) or one plain directory
(single host), not in the tenant config tree.

What a record means is decided by tenant-api, the program that reads it
(ADR-036 §12). So the tools do not read the records' YAML themselves: they
read the JSON ``tenant-api state show`` prints, where every record has gone
through the server's own parse function and is either decoded (``record``,
with the field names of the record's YAML) or carries why tenant-api cannot
use it (``problem``: kind ``unreadable`` or ``rejected``):

    kubectl exec deploy/tenant-api -c tenant-api -- tenant-api state show > state.json
    docker run -i --rm <the deployed tenant-api image> state show --from - < backup.yaml > state.json

``--state`` takes that file (``-`` for standard input). Anything else — the
old inputs (a saved ConfigMap, a state directory), a document of a schema
this module does not know, a document without records — is refused with
:class:`StateInputError`, whose message names the commands above and the
tenant-api version that has ``state show``.

A record with a problem is never read as empty: :meth:`TenantState.problem`
names it, and the accessors of a record raise :class:`RecordProblem` for it.
A record the store does not have is not listed, which means empty, as in
the server.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

KEY_GROUPS = "_groups.yaml"
KEY_VIEWS = "_views.yaml"
KEY_FEDERATION_POLICY = "_federation_policy.yaml"
KEY_ACCOUNT_REGISTRY = "_account_registry.yaml"
SUBSET_PREFIX = "_federation."
SUBSET_SUFFIX = ".yaml"

#: The one document schema this module reads (`tenant-api state show`).
STATE_SHOW_SCHEMA = "tenant-api.state-show/v1"
#: The first tenant-api release with `state show` (changelog.d/3206-tenant-api-state-show.md).
MIN_TENANT_API_VERSION = "2.9.24"
#: The problem kinds `state show` writes.
PROBLEM_KINDS = ("unreadable", "rejected")

#: How to get the JSON this module reads; part of every refusal of an input.
HOW_TO_GET_STATE = (
    f"--state takes the JSON `tenant-api state show` prints (tenant-api v{MIN_TENANT_API_VERSION} "
    "or later): on a cluster `kubectl exec deploy/tenant-api -c tenant-api -- tenant-api state show "
    "> state.json`; from a backup `docker run -i --rm <the deployed tenant-api image> state show "
    "--from - < backup.yaml > state.json`")


def subset_key(tenant: str) -> str:
    """The record key of *tenant*'s federation subset."""
    return SUBSET_PREFIX + tenant + SUBSET_SUFFIX


class StateInputError(ValueError):
    """The --state input is not a `tenant-api state show` document this module reads."""


@dataclass(frozen=True)
class Problem:
    """Why tenant-api cannot use a record."""

    kind: str
    message: str


class RecordProblem(Exception):
    """A record was asked for whose `state show` entry carries a problem:
    tenant-api cannot use it, so its content is undetermined — never empty."""

    def __init__(self, source: str, key: str, problem: Problem) -> None:
        super().__init__(f"{source} [{key}]: tenant-api cannot use this record "
                         f"({problem.kind}): {problem.message}")
        self.key = key
        self.problem = problem


@dataclass
class TenantState:
    """One `state show` document: record key → decoded record or problem."""

    source: str
    tenant_api_version: str = ""
    decoded: dict[str, Any] = field(default_factory=dict)
    problems: dict[str, Problem] = field(default_factory=dict)

    @property
    def keys(self) -> list[str]:
        """Every record key the store holds, sorted."""
        return sorted({*self.decoded, *self.problems})

    def has(self, key: str) -> bool:
        """Whether the store holds record *key* (usable or not)."""
        return key in self.decoded or key in self.problems

    def problem(self, key: str) -> Problem | None:
        """Why tenant-api cannot use record *key*, or None."""
        return self.problems.get(key)

    def record(self, key: str) -> Any:
        """Record *key* as tenant-api decoded it; None when the store lacks
        it. Raises RecordProblem when tenant-api cannot use it."""
        if key in self.problems:
            raise RecordProblem(self.source, key, self.problems[key])
        return self.decoded.get(key)

    def _map(self, key: str, field_name: str) -> dict[str, dict[str, Any]]:
        doc = self.record(key) or {}
        got = doc.get(field_name) if isinstance(doc, dict) else None
        return {k: v for k, v in (got or {}).items() if isinstance(v, dict)}

    def groups(self) -> dict[str, dict[str, Any]]:
        """``groups`` of ``_groups.yaml`` (empty when the store lacks it)."""
        return self._map(KEY_GROUPS, "groups")

    def views(self) -> dict[str, dict[str, Any]]:
        """``views`` of ``_views.yaml`` (empty when the store lacks it)."""
        return self._map(KEY_VIEWS, "views")

    def account_id(self, tenant: str) -> Any:
        """*tenant*'s AccountID in the registry, or None."""
        doc = self.record(KEY_ACCOUNT_REGISTRY) or {}
        allocs = doc.get("allocations") if isinstance(doc, dict) else None
        return allocs.get(tenant) if isinstance(allocs, dict) else None


def is_state_key(key: str) -> bool:
    """Whether *key* is a record key tenant-api keeps."""
    if key in (KEY_GROUPS, KEY_VIEWS, KEY_FEDERATION_POLICY, KEY_ACCOUNT_REGISTRY):
        return True
    tenant = key[len(SUBSET_PREFIX):-len(SUBSET_SUFFIX)] if (
        key.startswith(SUBSET_PREFIX) and key.endswith(SUBSET_SUFFIX)) else ""
    return bool(tenant) and not any(c in tenant for c in "/\\") and ".." not in tenant


def _refuse(source: str, why: str) -> StateInputError:
    return StateInputError(f"--state {source}: {why}. {HOW_TO_GET_STATE}")


def load_state(path: str | os.PathLike[str]) -> TenantState:
    """Read a `tenant-api state show` document from *path* (``-``: stdin).

    Raises StateInputError for anything else, naming how to get one: the
    old inputs (a directory, a saved ConfigMap), text that is not JSON, a
    document whose ``schema`` is missing or unknown (a tenant-api without
    `state show`, or a newer one), a malformed entry, and a document with no
    records — read as a state store that would report "no references" and
    let an offboarding go ahead unchecked.
    """
    source = "<stdin>" if str(path) == "-" else str(path)
    raw = _read(path, source)
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise _refuse(source, "not JSON (a saved ConfigMap or a state directory is not read "
                              "any more: tenant-api reads its own records)") from None
    if not isinstance(doc, dict) or doc.get("schema") != STATE_SHOW_SCHEMA:
        got = doc.get("schema") if isinstance(doc, dict) else None
        raise _refuse(source, f"not a {STATE_SHOW_SCHEMA} document (schema {got!r})")
    records = doc.get("records")
    if not isinstance(records, list):
        raise _refuse(source, "`records` is not a list")
    st = TenantState(source=source, tenant_api_version=str(doc.get("tenant_api_version") or ""))
    for i, r in enumerate(records):
        _add(st, r, i, source)
    if not st.keys:
        raise StateInputError(
            f"--state {source}: holds no records — the state store is empty "
            "(if it really is, use --skip-state-check)")
    return st


def _add(st: TenantState, r: Any, i: int, source: str) -> None:
    if not isinstance(r, dict):
        raise _refuse(source, f"records[{i}] is not an object")
    key = r.get("key")
    if not isinstance(key, str) or not is_state_key(key):
        raise _refuse(source, f"records[{i}].key {key!r} is not a tenant-api state record key")
    if st.has(key):
        raise _refuse(source, f"records[{i}]: key {key!r} is listed twice")
    rec, prob = r.get("record"), r.get("problem")
    if (rec is None) == (prob is None):
        raise _refuse(source, f"records[{i}] ({key}): exactly one of `record` and `problem` "
                              "must be set")
    if prob is not None:
        if (not isinstance(prob, dict) or prob.get("kind") not in PROBLEM_KINDS
                or not isinstance(prob.get("message"), str)):
            raise _refuse(source, f"records[{i}] ({key}): `problem` is not "
                                  "{kind: unreadable|rejected, message}")
        st.problems[key] = Problem(prob["kind"], prob["message"])
        return
    if not isinstance(rec, dict):
        raise _refuse(source, f"records[{i}] ({key}): `record` is not an object")
    st.decoded[key] = rec


def _read(path: str | os.PathLike[str], source: str) -> bytes:
    if str(path) == "-":
        return sys.stdin.buffer.read()
    p = Path(path)
    if p.is_dir():
        raise _refuse(source, "a directory (a state directory is not read any more: "
                              "tenant-api reads its own records)")
    try:
        return p.read_bytes()
    except OSError as exc:
        raise StateInputError(f"--state {source}: {exc}") from exc
