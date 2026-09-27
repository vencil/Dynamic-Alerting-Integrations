"""Which alerts read a threshold key — looked up in the rule packs, not guessed.

``config_diff`` and ``patch_config --diff`` both show, next to a changed key,
the alerts the change reaches. They used to derive that from the key's
spelling (``mysql_connections`` → ``*MysqlConnections*``), which names no real
alert: the rule pack calls it ``MariaDBHighConnections``, and the CI
blast-radius comment carried the invented pattern to reviewers.

The lookup reads what the packs actually say. An alert reaches a key when its
``expr`` mentions ``tenant:alert_threshold:<key>`` (or the ``tenant_version:``
spelling) — directly, or through recording rules it reads. The indirect path
is not optional: ``container_cpu`` / ``container_cpu_throttle`` /
``container_memory`` are compared inside a recording rule and no alert names
them directly, so a direct-only scan reports them as reaching nothing.

Where the packs live differs by layout, and neither is found by counting
directory levels (issue 1494):

* image — ``build.sh`` ``REPO_DATA_FILES`` ships ``rule-pack-*.yaml`` flat
  beside this module (paired by ``REQUIRED_DATA_FILES`` in
  ``check_build_completeness.py``);
* repo — ``rule-packs/`` under the project root, found by the shared markers.

When neither exists the answer is "unknown", never a guess: callers get
``None`` and one line on stderr says why.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import yaml

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, '..'))  # Repo subdir layout
from _lib_compat import PROJECT_ROOT_MARKERS  # noqa: E402

PACK_GLOB = "rule-pack-*.yaml"

_THRESHOLD_RE = re.compile(r"tenant(?:_version)?:alert_threshold:([A-Za-z0-9_]+)")
_TOKEN_RE = re.compile(r"[A-Za-z_:][A-Za-z0-9_:]*")

_INDEX_CACHE: "dict[str, tuple[str, ...]] | None" = None
_INDEX_LOADED = False
_WARNED = False


def find_rule_pack_paths(here: "Path | None" = None) -> "list[Path]":
    """The rule packs this layout carries, flat-first; [] when there are none."""
    here = Path(_THIS_DIR).resolve() if here is None else here
    flat = sorted(here.glob(PACK_GLOB))
    if flat:
        return flat
    # Bounded at the project root, for the reason _grar_validate gives: an
    # unbounded walk adopts a stray rule-packs/ from whatever sits above.
    root = next(
        (base for base in (here, *here.parents)
         if any((base / m).exists() for m in PROJECT_ROOT_MARKERS)),
        None,
    )
    if root is None:
        return []
    return sorted((root / "rule-packs").glob(PACK_GLOB))


def build_index(pack_paths) -> "dict[str, tuple[str, ...]]":
    """{threshold key: sorted alert names reaching it} over the given packs."""
    records: "dict[str, list[str]]" = {}
    alerts: "list[tuple[str, str]]" = []
    for path in pack_paths:
        with open(path, encoding="utf-8") as fh:
            doc = yaml.safe_load(fh) or {}
        for group in doc.get("groups") or []:
            for rule in group.get("rules") or []:
                if not isinstance(rule, dict):
                    continue
                expr = str(rule.get("expr", ""))
                if "record" in rule:
                    records.setdefault(str(rule["record"]), []).append(expr)
                elif "alert" in rule:
                    alerts.append((str(rule["alert"]), expr))

    def keys_in(expr, seen):
        keys = set(_THRESHOLD_RE.findall(expr))
        for token in set(_TOKEN_RE.findall(expr)) - seen:
            # A threshold recording rule is the key itself, already counted;
            # its body reads user_threshold, not other keys.
            if token in records and not _THRESHOLD_RE.fullmatch(token):
                seen.add(token)
                for body in records[token]:
                    keys |= keys_in(body, seen)
        return keys

    index: "dict[str, set[str]]" = {}
    for name, expr in alerts:
        for key in keys_in(expr, set()):
            index.setdefault(key, set()).add(name)
    return {key: tuple(sorted(names)) for key, names in index.items()}


def _warn_once(reason: str) -> None:
    global _WARNED
    if not _WARNED:
        _WARNED = True
        print(f"WARN: affected alerts unknown — {reason}", file=sys.stderr)


def _index() -> "dict[str, tuple[str, ...]] | None":
    global _INDEX_CACHE, _INDEX_LOADED
    if not _INDEX_LOADED:
        _INDEX_LOADED = True
        paths = find_rule_pack_paths()
        if not paths:
            _warn_once(f"no {PACK_GLOB} beside {_THIS_DIR} or under the "
                       "project's rule-packs/")
        else:
            try:
                _INDEX_CACHE = build_index(paths)
            except (OSError, yaml.YAMLError) as exc:
                _warn_once(f"rule packs unreadable: {exc}")
    return _INDEX_CACHE


def alerts_for_key(metric_key: str) -> "tuple[str, ...] | None":
    """Alert names that read ``metric_key``; () for none; None when unknown.

    A dimensional key (``redis_queue_length{queue="tasks"}``) reaches the same
    alerts as its base key.
    """
    index = _index()
    if index is None:
        return None
    return index.get(metric_key.split("{", 1)[0].strip(), ())
