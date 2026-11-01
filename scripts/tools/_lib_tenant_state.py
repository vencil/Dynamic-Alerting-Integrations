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
fact that it needs a tenant-api with ``state show`` (the ``schema`` decides,
never a version number).

A record with a problem is never read as empty: :meth:`TenantState.problem`
names it, and the accessors of a record raise :class:`RecordProblem` for it.
A record the store does not have is not listed, which means empty, as in
the server.
"""
from __future__ import annotations

import errno
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from _lib_paths import classify_path  # noqa: E402  (#3276)

KEY_GROUPS = "_groups.yaml"
KEY_VIEWS = "_views.yaml"
KEY_FEDERATION_POLICY = "_federation_policy.yaml"
KEY_ACCOUNT_REGISTRY = "_account_registry.yaml"
SUBSET_PREFIX = "_federation."
SUBSET_SUFFIX = ".yaml"

#: The one document schema this module reads (`tenant-api state show`).
STATE_SHOW_SCHEMA = "tenant-api.state-show/v1"
#: The problem kinds `state show` writes.
PROBLEM_KINDS = ("unreadable", "rejected")

_GET_ON_CLUSTER = "kubectl exec deploy/tenant-api -c tenant-api -- tenant-api state show > state.json"
_GET_FROM_BACKUP = ("docker run -i --rm <the deployed tenant-api image> state show "
                    "--from - < backup.yaml > state.json")

#: Every refusal this module writes, per language — the one place it is
#: written (#3512). ``lang`` is the line the message lands in, the caller's:
#: there is no default, so a Chinese tool cannot get English by forgetting
#: it (the `_lib_paths._DETAIL` rule). Same keys in every language.
_MSG: dict[str, dict[str, str]] = {
    "en": {
        "how_to": ("--state takes the JSON `tenant-api state show` prints (a tenant-api "
                   f"that has `state show`): on a cluster `{_GET_ON_CLUSTER}`; from a "
                   f"backup `{_GET_FROM_BACKUP}`"),
        "not_json": ("not JSON (a saved ConfigMap or a state directory is not read any "
                     "more: tenant-api reads its own records)"),
        "bad_schema": "not a {schema} document (schema {got!r})",
        "records_not_list": "`records` is not a list",
        "empty": ("holds no records — the state store is empty, so nothing can be read "
                  "from it"),
        "not_object": "records[{i}] is not an object",
        "bad_key": "records[{i}].key {key!r} is not a tenant-api state record key",
        "dup_key": "records[{i}]: key {key!r} is listed twice",
        "one_of": "records[{i}] ({key}): exactly one of `record` and `problem` must be set",
        "bad_problem": ("records[{i}] ({key}): `problem` is not "
                        "{{kind: unreadable|rejected, message}}"),
        "record_not_object": "records[{i}] ({key}): `record` is not an object",
        "directory": ("a directory (a state directory is not read any more: tenant-api "
                      "reads its own records)"),
        "unreadable": "{err}",
    },
    "zh": {
        "how_to": ("--state 要的是 `tenant-api state show` 印出的 JSON（需要有 `state show` "
                   f"的 tenant-api）：叢集上 `{_GET_ON_CLUSTER}`；從備份 `{_GET_FROM_BACKUP}`"),
        "not_json": "不是 JSON（已不再讀存下來的 ConfigMap 或 state 目錄：tenant-api 讀自己的紀錄）",
        "bad_schema": "不是 {schema} 文件（schema 為 {got!r}）",
        "records_not_list": "`records` 不是 list",
        "empty": "沒有任何紀錄——state store 是空的，讀不到任何東西",
        "not_object": "records[{i}] 不是物件",
        "bad_key": "records[{i}].key {key!r} 不是 tenant-api 的 state 紀錄 key",
        "dup_key": "records[{i}]：key {key!r} 出現兩次",
        "one_of": "records[{i}]（{key}）：`record` 與 `problem` 必須恰好設一個",
        "bad_problem": "records[{i}]（{key}）：`problem` 不是 {{kind: unreadable|rejected, message}}",
        "record_not_object": "records[{i}]（{key}）：`record` 不是物件",
        "directory": "是目錄（已不再讀 state 目錄：tenant-api 讀自己的紀錄）",
        "unreadable": "無法讀取（{err}）",
    },
}
LANGS = tuple(_MSG)


def how_to_get_state(lang: str) -> str:
    """How to get the JSON this module reads, in *lang*; part of every
    refusal of an input."""
    return _MSG[lang]["how_to"]


def subset_key(tenant: str) -> str:
    """The record key of *tenant*'s federation subset."""
    return SUBSET_PREFIX + tenant + SUBSET_SUFFIX


class StateInputError(ValueError):
    """The --state input is not a `tenant-api state show` document this module
    reads. ``empty`` is True for a document with no records (each tool says
    what it can do instead: `offboard_tenant` has --skip-state-check)."""

    def __init__(self, message: str, *, empty: bool = False) -> None:
        super().__init__(message)
        self.empty = empty


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


def _say(lang: str, msg: str, **fields: Any) -> str:
    return _MSG[lang][msg].format(**fields)


def _refuse(source: str, lang: str, msg: str, **fields: Any) -> StateInputError:
    sep = "。" if lang == "zh" else ". "
    return StateInputError(f"--state {source}: {_say(lang, msg, **fields)}{sep}{how_to_get_state(lang)}")


def load_state(path: str | os.PathLike[str], *, lang: str) -> TenantState:
    """Read a `tenant-api state show` document from *path* (``-``: stdin).

    Raises StateInputError for anything else, naming how to get one: the
    old inputs (a directory, a saved ConfigMap), text that is not JSON, a
    document whose ``schema`` is missing or unknown (a tenant-api without
    `state show`, or a newer one), a malformed entry, and a document with no
    records — read as a state store that would report "no references" and
    let an offboarding go ahead unchecked. The refusal is in *lang* (one of
    ``LANGS``, the caller's own language; no default).
    """
    if lang not in _MSG:
        raise ValueError(f"lang must be one of {LANGS}, not {lang!r}")
    source = "<stdin>" if str(path) == "-" else str(path)
    raw = _read(path, source, lang)
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise _refuse(source, lang, "not_json") from None
    if not isinstance(doc, dict) or doc.get("schema") != STATE_SHOW_SCHEMA:
        got = doc.get("schema") if isinstance(doc, dict) else None
        raise _refuse(source, lang, "bad_schema", schema=STATE_SHOW_SCHEMA, got=got)
    records = doc.get("records")
    if not isinstance(records, list):
        raise _refuse(source, lang, "records_not_list")
    st = TenantState(source=source, tenant_api_version=str(doc.get("tenant_api_version") or ""))
    for i, r in enumerate(records):
        _add(st, r, i, source, lang)
    if not st.keys:
        raise StateInputError(f"--state {source}: {_say(lang, 'empty')}", empty=True)
    return st


def _add(st: TenantState, r: Any, i: int, source: str, lang: str) -> None:
    if not isinstance(r, dict):
        raise _refuse(source, lang, "not_object", i=i)
    key = r.get("key")
    if not isinstance(key, str) or not is_state_key(key):
        raise _refuse(source, lang, "bad_key", i=i, key=key)
    if st.has(key):
        raise _refuse(source, lang, "dup_key", i=i, key=key)
    rec, prob = r.get("record"), r.get("problem")
    if (rec is None) == (prob is None):
        raise _refuse(source, lang, "one_of", i=i, key=key)
    if prob is not None:
        if (not isinstance(prob, dict) or prob.get("kind") not in PROBLEM_KINDS
                or not isinstance(prob.get("message"), str)):
            raise _refuse(source, lang, "bad_problem", i=i, key=key)
        st.problems[key] = Problem(prob["kind"], prob["message"])
        return
    if not isinstance(rec, dict):
        raise _refuse(source, lang, "record_not_object", i=i, key=key)
    st.decoded[key] = rec


def _read(path: str | os.PathLike[str], source: str, lang: str) -> bytes:
    if str(path) == "-":
        return sys.stdin.buffer.read()
    # #3276: the shared judgement of a caller-given path ("any": a FIFO or a
    # process substitution is read as before). `is_dir()` raised on a path
    # under a parent the caller cannot enter (3.13): a traceback.
    # The caller's language, as every other --state refusal here (#3512).
    problem = classify_path(path, "any", lang=lang, flag="--state")
    if problem is not None:
        raise StateInputError(problem.message)
    p = Path(path)
    if p.is_dir():
        raise _refuse(source, lang, "directory")
    try:
        return p.read_bytes()
    except OSError as exc:
        err = (f"{errno.errorcode.get(exc.errno, exc.errno)}" if lang == "zh"
               else str(exc))  # en: the text it always printed
        raise StateInputError(f"--state {source}: {_say(lang, 'unreadable', err=err)}") from exc
