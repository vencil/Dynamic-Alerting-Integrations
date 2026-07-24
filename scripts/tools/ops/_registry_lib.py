#!/usr/bin/env python3
"""_registry_lib.py — threshold-registry SoT loader / validator / query lib (TRK-339 WS1a / #1200).

Epic #1200 D2 (LOCKED, 2026-07-23): the threshold contract gets a standalone
YAML registry as its Source of Truth — language-neutral (helm/docs/portal can
read it directly, no Python import), JSON-Schema validatable (confd-schema
family gate). ``rule-packs/threshold-registry.yaml`` is that registry;
``docs/schemas/threshold-registry.schema.json`` is its shape contract.

TRANSITION STATE (this PR is the skeleton — WS1a step 1): the registry content
is MECHANICALLY EXTRACTED from ``scaffold_tenant.RULE_PACKS`` via
``build_registry_doc()`` (never hand-copied), and NO generator consumes it yet.
``scaffold_tenant.RULE_PACKS`` remains the operative contract until the rewire
PR flips the direction (scaffold becomes a generated artifact / runtime loader
of the registry — D2 migration shape, 31 import sites keep their API surface).
During the transition the two copies MUST stay semantically equal — that is
enforced by ``scripts/tools/lint/check_threshold_registry.py`` (schema
validation + equivalence assertion), wired as a pre-commit gate. On drift the
fix is: edit ``scaffold_tenant.RULE_PACKS`` (still operative), then run
``check_threshold_registry.py --regen`` to refresh the registry.

SCOPE (WS1a skeleton): threshold identity only — ``defaults`` +
``optional_overrides`` tiers ({pack, tier, value, unit, desc, metric_class,
critical variant}). ``state_filters`` / ``dimensional_example`` stay
scaffold-owned (they are state/config surface, not threshold identity; absorb
later if a consumer needs them). Extraction is FULL (all packs, all keys —
mechanical and cheap); the enforcement path this registry exists to serve
first is the 18-key repair line (#1196 / TRK-337).
"""
from __future__ import annotations

import os
import sys
from typing import Any, Optional

try:
    import yaml
except ImportError:  # pragma: no cover - environments without pyyaml
    yaml = None  # type: ignore

try:
    import jsonschema
except ImportError:  # pragma: no cover - environments without jsonschema
    jsonschema = None  # type: ignore

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
# scripts/tools/ops/ -> repo root is three levels up.
_REPO_ROOT = os.path.abspath(os.path.join(_THIS_DIR, "..", "..", ".."))

REGISTRY_PATH = os.path.join(_REPO_ROOT, "rule-packs", "threshold-registry.yaml")
SCHEMA_PATH = os.path.join(
    _REPO_ROOT, "docs", "schemas", "threshold-registry.schema.json"
)

CRITICAL_SUFFIX = "_critical"

# The two threshold tiers carried by the registry. Order matters: it is the
# extraction order, and a same-name key in both tiers of one pack would be a
# contract bug (build_registry_doc fails loudly on it).
TIERS = ("defaults", "optional_overrides")

_REGISTRY_HEADER = (
    "# threshold-registry.yaml — threshold 契約 SoT（TRK-339 WS1a / #1200 D2）\n"
    "#\n"
    "# 地位：epic #1200 D2（2026-07-23 LOCKED）拍板 threshold 契約收斂到本檔——\n"
    "# 語言中立（helm/docs/portal 直讀）、schema 可驗證\n"
    "# （docs/schemas/threshold-registry.schema.json）。\n"
    "#\n"
    "# ⚠️ 過渡期（本 skeleton PR）：生成消費者「尚未」接線——\n"
    "# scaffold_tenant.RULE_PACKS 仍是運作中的契約副本，本檔由它機械萃取\n"
    "# （scripts/tools/ops/_registry_lib.py build_registry_doc()）。兩者的語意等價\n"
    "# 由 pre-commit gate check_threshold_registry.py 強制（防雙 SoT 漂移）。\n"
    "# 改閾值請改 scaffold_tenant.RULE_PACKS，再跑：\n"
    "#   python3 scripts/tools/lint/check_threshold_registry.py --regen\n"
    "# 下一個 PR 起方向反轉：本檔為 SoT、scaffold RULE_PACKS 降為生成物/runtime 載入。\n"
    "#\n"
    "# 範圍：threshold 身分（defaults / optional_overrides 兩層）。state_filters 與\n"
    "# dimensional_example 仍歸 scaffold（非閾值身分）。萃取為全量；本 registry 首要\n"
    "# 服務的 enforcement 路徑是 18-key 修復線（#1196 / TRK-337）。\n"
)


def _load_scaffold():
    """Lazy import of scaffold_tenant (same directory)."""
    if _THIS_DIR not in sys.path:
        sys.path.insert(0, _THIS_DIR)
    import scaffold_tenant  # noqa: E402
    return scaffold_tenant


# ---------------------------------------------------------------------------
# Extraction (scaffold RULE_PACKS -> registry doc) — mechanical, never hand-copy
# ---------------------------------------------------------------------------

def build_registry_doc(rule_packs: Optional[dict] = None) -> dict:
    """Mechanically extract the registry document from RULE_PACKS.

    Per pack: {display, exporter, default_on, rule_pack_file}.
    Per key : {pack, tier, value, unit, desc[, metric_class][, critical_of]}.
    ``critical_of`` is derived (never authored): key ends with ``_critical`` AND
    its base key exists in the same pack -> the base key name.

    Fails loudly on identity collisions (same key in two tiers of one pack, or
    in two packs) — a collision would silently shadow a contract entry.
    """
    if rule_packs is None:
        rule_packs = _load_scaffold().RULE_PACKS

    packs: dict[str, Any] = {}
    keys: dict[str, Any] = {}
    for pack_name, pack in rule_packs.items():
        packs[pack_name] = {
            "display": pack["display"],
            "exporter": pack["exporter"],
            "default_on": bool(pack.get("default_on", False)),
            "rule_pack_file": pack["rule_pack_file"],
        }
        combined: dict[str, tuple[str, dict]] = {}
        for tier in TIERS:
            for key, info in (pack.get(tier) or {}).items():
                if key in combined:
                    raise ValueError(
                        f"threshold key {key!r} appears in BOTH tiers of pack "
                        f"{pack_name!r} — ambiguous contract identity"
                    )
                combined[key] = (tier, info)
        for key, (tier, info) in combined.items():
            if key in keys:
                raise ValueError(
                    f"threshold key {key!r} appears in packs "
                    f"{keys[key]['pack']!r} AND {pack_name!r} — ambiguous "
                    "contract identity"
                )
            entry: dict[str, Any] = {
                "pack": pack_name,
                "tier": tier,
                "value": info["value"],
                "unit": info["unit"],
                "desc": info["desc"],
            }
            if "metric_class" in info:
                entry["metric_class"] = info["metric_class"]
            if key.endswith(CRITICAL_SUFFIX):
                base = key[: -len(CRITICAL_SUFFIX)]
                if base in combined:
                    entry["critical_of"] = base
            keys[key] = entry
    return {"version": 1, "packs": packs, "keys": keys}


def write_registry(
    path: Optional[str] = None, rule_packs: Optional[dict] = None
) -> dict:
    """Regenerate the registry from RULE_PACKS and write it to ``path``.

    Returns a summary dict {path, packs, keys}. Uses write_text_secure (the
    repo SAST convention chmod for write-mode files, tests/shared/test_sast.py).
    """
    if yaml is None:
        raise RuntimeError("pyyaml required")
    sys.path.insert(0, _THIS_DIR)
    sys.path.insert(0, os.path.join(_THIS_DIR, ".."))
    from _lib_python import write_text_secure  # noqa: E402

    out = path or REGISTRY_PATH
    doc = build_registry_doc(rule_packs)
    body = yaml.safe_dump(
        doc, sort_keys=False, allow_unicode=True, default_flow_style=False
    )
    write_text_secure(out, _REGISTRY_HEADER + body)
    return {"path": out, "packs": len(doc["packs"]), "keys": len(doc["keys"])}


# ---------------------------------------------------------------------------
# Loading + schema validation
# ---------------------------------------------------------------------------

def load_registry(path: Optional[str] = None) -> dict:
    """Load the committed registry document (the whole doc, not just keys)."""
    if yaml is None:
        raise RuntimeError("pyyaml required")
    p = path or REGISTRY_PATH
    with open(p, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def validate_registry(
    doc: dict, schema_path: Optional[str] = None
) -> list[str]:
    """Validate a registry doc against the JSON Schema. Returns error strings.

    Empty list == valid. Uses Draft-07 (the docs/schemas/ family convention).
    """
    if jsonschema is None:
        raise RuntimeError("jsonschema required")
    import json

    sp = schema_path or SCHEMA_PATH
    with open(sp, encoding="utf-8") as fh:
        schema = json.load(fh)
    validator = jsonschema.Draft7Validator(schema)
    errors = []
    for err in sorted(validator.iter_errors(doc), key=lambda e: list(e.absolute_path)):
        where = "/".join(str(p) for p in err.absolute_path) or "<root>"
        errors.append(f"schema: {where}: {err.message}")
    return errors


# ---------------------------------------------------------------------------
# Convenience queries
# ---------------------------------------------------------------------------

def registry_keys(doc: dict) -> dict[str, dict]:
    """The flat ``keys`` mapping of a registry doc (or {})."""
    return doc.get("keys", {}) or {}


def keys_by_pack(doc: dict) -> dict[str, dict[str, dict]]:
    """Group the flat keys map by pack: {pack: {key: entry}}."""
    out: dict[str, dict[str, dict]] = {}
    for key, entry in registry_keys(doc).items():
        out.setdefault(entry.get("pack", "?"), {})[key] = entry
    return out


def keys_in_tier(doc: dict, tier: str) -> dict[str, dict]:
    """All keys of one tier ('defaults' / 'optional_overrides')."""
    return {
        k: e for k, e in registry_keys(doc).items() if e.get("tier") == tier
    }


def default_value(doc: dict, key: str):
    """The registry default value for ``key`` (None if absent)."""
    entry = registry_keys(doc).get(key)
    return None if entry is None else entry.get("value")


# ---------------------------------------------------------------------------
# Transition-period equivalence (anti dual-SoT drift)
# ---------------------------------------------------------------------------

def diff_docs(committed: dict, fresh: dict) -> list[str]:
    """Semantic diff of a committed registry vs a fresh scaffold extraction.

    Returns one message per divergence (empty == semantically equal). Messages
    are per-field so the gate output is directly actionable.
    """
    diffs: list[str] = []

    c_packs = committed.get("packs", {}) or {}
    f_packs = fresh.get("packs", {}) or {}
    for name in sorted(set(f_packs) - set(c_packs)):
        diffs.append(f"pack {name!r}: in scaffold RULE_PACKS but missing from registry")
    for name in sorted(set(c_packs) - set(f_packs)):
        diffs.append(f"pack {name!r}: in registry but missing from scaffold RULE_PACKS")
    for name in sorted(set(c_packs) & set(f_packs)):
        for field in sorted(set(c_packs[name]) | set(f_packs[name])):
            cv, fv = c_packs[name].get(field), f_packs[name].get(field)
            if cv != fv:
                diffs.append(
                    f"pack {name!r}.{field}: registry={cv!r} vs scaffold={fv!r}"
                )

    c_keys = committed.get("keys", {}) or {}
    f_keys = fresh.get("keys", {}) or {}
    for key in sorted(set(f_keys) - set(c_keys)):
        diffs.append(f"key {key!r}: in scaffold RULE_PACKS but missing from registry")
    for key in sorted(set(c_keys) - set(f_keys)):
        diffs.append(f"key {key!r}: in registry but missing from scaffold RULE_PACKS")
    for key in sorted(set(c_keys) & set(f_keys)):
        for field in sorted(set(c_keys[key]) | set(f_keys[key])):
            cv, fv = c_keys[key].get(field), f_keys[key].get(field)
            if cv != fv:
                diffs.append(
                    f"key {key!r}.{field}: registry={cv!r} vs scaffold={fv!r}"
                )

    if committed.get("version") != fresh.get("version"):
        diffs.append(
            f"version: registry={committed.get('version')!r} vs "
            f"expected={fresh.get('version')!r}"
        )
    return diffs


def diff_vs_scaffold(doc: dict, rule_packs: Optional[dict] = None) -> list[str]:
    """Diff a loaded registry doc against a fresh RULE_PACKS extraction."""
    return diff_docs(doc, build_registry_doc(rule_packs))
