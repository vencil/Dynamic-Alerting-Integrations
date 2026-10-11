"""_lib_tenant_state.py — Read tenant-api's state store from a backup (#2883).

Since #2856, groups, saved views, the federation whitelist and per-tenant
subsets, and the AccountID registry are tenant-api's own records: they live
in the ``tenant-api-state`` ConfigMap (Kubernetes) or one plain directory
(single host), not in the tenant config tree. Tools that need them read a copy:

* a state directory — one file per record key;
* ``kubectl get configmap tenant-api-state -o yaml`` (or ``-o json``) saved
  to a file — its ``data`` map.

Both are "record key → YAML text"; the keys keep the file names these records had before they moved
(``_groups.yaml``, ``_views.yaml``, ``_federation_policy.yaml``,
``_federation.<tenant>.yaml``, ``_account_registry.yaml``). Each record is
read with the strict loader the tenant config tools use: a key written twice is an
error, and tenant ids are their source text (``members: [010]`` names
tenant "010", not 8).

An unreadable input raises :class:`_lib_io.YamlFileError` naming it, so a
CLI ``main`` wrapped in ``exit_on_yaml_file_error`` exits 2.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

import yaml  # noqa: E402

from _lib_io import YamlFileError, strict_load_exporter_keys  # noqa: E402

KEY_GROUPS = "_groups.yaml"
KEY_VIEWS = "_views.yaml"
KEY_FEDERATION_POLICY = "_federation_policy.yaml"
KEY_ACCOUNT_REGISTRY = "_account_registry.yaml"
SUBSET_PREFIX = "_federation."
SUBSET_SUFFIX = ".yaml"


def subset_key(tenant: str) -> str:
    """The record key of *tenant*'s federation subset."""
    return SUBSET_PREFIX + tenant + SUBSET_SUFFIX


class StateInputError(ValueError):
    """The --state input is not a state directory or a ConfigMap document."""


@dataclass
class TenantState:
    """The records of one state store copy, as key → YAML text."""

    source: str
    records: dict[str, str] = field(default_factory=dict)

    def load(self, key: str, **loader_opts: Any) -> Any:
        """Parse record *key* strictly; None when the copy lacks it."""
        text = self.records.get(key)
        if text is None:
            return None
        try:
            return strict_load_exporter_keys(text, **loader_opts)
        except yaml.YAMLError as exc:
            raise YamlFileError(f"{self.source} [{key}]", exc) from exc

    def groups(self) -> dict[str, dict[str, Any]]:
        """``groups:`` of ``_groups.yaml`` (empty when absent)."""
        doc = self.load(KEY_GROUPS, raw_text_sequences=("members",)) or {}
        groups = doc.get("groups") if isinstance(doc, dict) else None
        if groups is None:
            return {}
        if not isinstance(groups, dict):
            raise YamlFileError(f"{self.source} [{KEY_GROUPS}]",
                                ValueError("`groups:` is not a mapping"))
        return {gid: g for gid, g in groups.items() if isinstance(g, dict)}

    def views(self) -> dict[str, dict[str, Any]]:
        """``views:`` of ``_views.yaml`` (empty when absent). tenant-api
        writes filter values as strings (yaml.v3 quotes ``"010"``), so a
        value equal to a tenant id reads back as that id."""
        doc = self.load(KEY_VIEWS) or {}
        views = doc.get("views") if isinstance(doc, dict) else None
        if views is None:
            return {}
        if not isinstance(views, dict):
            raise YamlFileError(f"{self.source} [{KEY_VIEWS}]",
                                ValueError("`views:` is not a mapping"))
        return {vid: v for vid, v in views.items() if isinstance(v, dict)}

    def account_id(self, tenant: str) -> Any:
        """*tenant*'s AccountID in the registry, or None."""
        doc = self.load(KEY_ACCOUNT_REGISTRY) or {}
        allocs = doc.get("allocations") if isinstance(doc, dict) else None
        if not isinstance(allocs, dict):
            return None
        return allocs.get(tenant)


def load_state(path: str | os.PathLike[str]) -> TenantState:
    """Read a state store copy: a directory, or a ConfigMap YAML/JSON file."""
    p = Path(path)
    if p.is_dir():
        records = {}
        for entry in sorted(p.iterdir()):
            if not entry.is_file() or entry.name.startswith("."):
                continue  # tenant-api's own lock and temp files
            records[entry.name] = _read_text(entry)
        return TenantState(source=str(p), records=records)
    if not p.is_file():
        raise StateInputError(f"--state {p}: not a file or directory")
    text = _read_text(p)
    try:
        doc = strict_load_exporter_keys(text)
    except yaml.YAMLError as exc:
        raise YamlFileError(str(p), exc) from exc
    if not isinstance(doc, dict) or doc.get("kind") != "ConfigMap":
        raise StateInputError(
            f"--state {p}: not a ConfigMap document "
            "(save `kubectl get configmap tenant-api-state -o yaml`, or pass the state directory)")
    data = doc.get("data") or {}
    if not isinstance(data, dict) or not all(isinstance(v, str) for v in data.values()):
        raise StateInputError(f"--state {p}: `data` is not a map of record key to text")
    return TenantState(source=str(p), records={str(k): v for k, v in data.items()})


def _read_text(p: Path) -> str:
    try:
        return p.read_bytes().decode("utf-8")
    except UnicodeDecodeError as exc:
        raise YamlFileError(str(p), exc) from exc
    except OSError as exc:
        raise StateInputError(f"--state {p}: {exc}") from exc

