#!/usr/bin/env python3
"""migrate-builtin-defaults — remove copies of the platform's built-in thresholds.

#3832 decision 7. The readers of conf.d (exporter, da-guard, tenant-api) build in
the thresholds the platform ships armed. A tree written before that carries
copies of those values: in the root `_defaults.yaml`, and in tenant files that
repeat the value they inherit. A copy pins the day's number, so the platform's
later recalibrations never reach it. This command finds them and, with
`--apply`, removes them through `da-guard write`.

It also rewrites what a tree used to switch such a key off with — the root
`defaults:` writing it as null, or the root `optional_overrides:` listing it —
to `<key>: "disable"`: since the built-in layer, null and the listing no longer
switch it off (decision 4; da-guard reports the listing as
`root_optional_builtin`).

⛔ Every judgement is a reader's, none is this tool's:
  * which keys are built in, their values and what the root does with each:
    `da-guard effective`'s `builtin_defaults`;
  * whether a deletion only removes a copy: the batch is applied to a scratch
    copy of the tree and `da-guard served-values` compared before and after;
    a candidate whose key serves anything different for any tenant (for
    example a tenant value that overrides a subdirectory's) is kept and named.
The rewrites to "disable" are meant to change what is served, so they are not
put through that comparison.

Deleting a copy is the one step that makes a tree depend on the built-in layer:
a reader without the table then serves nothing for that key. So deletions need
`--exporter-digest`, the built-in table digest of every deployed exporter
(`da_config_builtin_defaults_info{digest}`, one per cluster the conf.d feeds),
and are refused when any differs from this tool's table. The rewrites are not
gated (decision 9 does them before the upgrade).

A number equal to the platform's may be pinned on purpose, so nothing is written
without `--apply`, and `--keep <key>` leaves a key alone.

Usage:
  da-tools migrate-builtin-defaults --config-dir conf.d
  da-tools migrate-builtin-defaults --config-dir conf.d --apply \\
      --exporter-digest 522437716e2a [--exporter-digest …] [--keep pg_connections]

Exit codes: 0 previewed or written; 1 deletions refused (an exporter digest
differs from this tool's table) or a deletion the batch would make is not a
copy is reported; 2 caller error (unreadable tree, da-guard unusable, bad flag).
"""
from __future__ import annotations

import argparse
import math
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, NamedTuple, Optional

_THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_THIS_DIR))
sys.path.insert(0, str(_THIS_DIR.parent))

import _lib_confedit  # noqa: E402  (ADR-036 §2: conf.d goes to da-guard write)
import _lib_parse_cache  # noqa: E402
import _lib_tenant_values as tv  # noqa: E402
from _lib_exitcodes import EXIT_CALLER_ERROR, EXIT_OK, EXIT_VIOLATION  # noqa: E402
from _lib_io import YamlFileError  # noqa: E402  (a tree the readers drop files of)

DISABLE = "disable"
#: Served at one fixed instant on both sides of the comparison, so a schedule
#: cannot read as a change; `schedules=True` compares the whole day too.
_AT = "2026-01-01T12:00:00Z"


class Deletion(NamedTuple):
    file: str
    path: tuple[str, ...]   # ("defaults", key) or ("tenants", tenant, key)
    key: str
    value: Any


class Rewrite(NamedTuple):
    key: str
    why: str                # "null" or "optional_overrides"


class Plan(NamedTuple):
    root: Optional[str]     # the root defaults carrier, as parse-tree names it
    deletions: list[Deletion]
    rewrites: list[Rewrite]
    not_copies: list[tuple[Deletion, str]]
    digest: str


def _number(value: Any) -> Optional[float]:
    """*value* as a finite number when it is a plain scalar threshold, else None
    (a schedule, a mapping, `disable`, text that is no number)."""
    if isinstance(value, bool) or isinstance(value, (dict, list)):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _root_carrier(config_dir: str) -> tuple[Optional[str], dict]:
    """The root defaults carrier the exporter reads (parse-tree's choice) and its
    data; (None, {}) when the tree has none."""
    reader = _lib_parse_cache.reader_for(config_dir)
    selected = reader.tree().selected
    names = sorted(e.path for e in reader.listing().entries
                   if e.depth == 0 and e.config_file and e.cls == "defaults" and e.path in selected)
    if not names:
        return None, {}
    data = reader.read(names[0])
    return names[0], data if isinstance(data, dict) else {}


def _candidates(config_dir: str, keep: set[str]) -> Plan:
    tree = tv.load_effective_tree(config_dir)
    builtin = tree.builtin_defaults
    if builtin is None:
        raise tv.DaGuardError("da-guard ran no build of this tree (no builtin_defaults)")
    root, root_data = _root_carrier(config_dir)
    deletions: list[Deletion] = []
    rewrites: list[Rewrite] = []
    for key, entry in sorted(builtin.keys.items()):
        if entry.state == "overridden" and entry.root_value == entry.value and key not in keep:
            deletions.append(Deletion(root, ("defaults", key), key, entry.root_value))
    defaults = root_data.get("defaults") if isinstance(root_data.get("defaults"), dict) else {}
    listed = root_data.get("optional_overrides")
    listed = [str(x) for x in listed if isinstance(x, (str, int, float))] if isinstance(listed, list) else []
    for key in sorted(builtin.keys):
        if key in defaults and defaults[key] is None:
            rewrites.append(Rewrite(key, "null"))
        elif key in listed and key not in defaults:
            rewrites.append(Rewrite(key, "optional_overrides"))
    for tenant, te in sorted(tree.tenants.items()):
        for key, entry in sorted(builtin.keys.items()):
            src = te.key_sources.get(key)
            if src is None or src.layer != "tenant" or key in keep:
                continue
            if _number(te.effective_config.get(key)) == entry.value:
                deletions.append(Deletion(src.file, ("tenants", tenant, key), key,
                                          te.effective_config[key]))
    return Plan(root, deletions, rewrites, [], builtin.digest)


def _served(config_dir: str) -> dict:
    return tv.load_served_values(config_dir, at=_AT, schedules=True)


def _key_state(tenant: tv.TenantValues, key: str) -> tuple:
    sched = (tenant.schedules or {}).get(key)
    return (tenant.values.get(key), tenant.severities.get(key),
            tuple(sorted((s.name, tuple(sorted(s.labels.items()))) for s in tenant.series.get(key, ()))),
            repr(sched))


def _changed_keys(before: dict, after: dict) -> set[str]:
    """Keys that serve anything different for some tenant (a tenant that
    appears or disappears changes every key it has)."""
    out: set[str] = set()
    for tenant in set(before) | set(after):
        b, a = before.get(tenant), after.get(tenant)
        if b is None or a is None:
            out |= set((b or a).values)
            continue
        for key in set(b.values) | set(a.values) | set(b.series) | set(a.series):
            if _key_state(b, key) != _key_state(a, key):
                out.add(key)
    return out


def _edits(deletions: list[Deletion], rewrites: list[Rewrite], root: Optional[str],
           root_data: dict) -> list[tuple[str, list[dict]]]:
    by_file: dict[str, list[dict]] = {}
    for d in deletions:
        by_file.setdefault(d.file, []).append(_lib_confedit.unset_op(list(d.path)))
    if rewrites and root is not None:
        ops = by_file.setdefault(root, [])
        ops.append(_lib_confedit.ensure_map_op(["defaults"]))
        for r in rewrites:
            ops.append(_lib_confedit.set_op(["defaults", r.key], _lib_confedit.str_value(DISABLE)))
        listed = root_data.get("optional_overrides")
        drop = {r.key for r in rewrites} & {str(x) for x in listed} if isinstance(listed, list) else set()
        if drop and isinstance(listed, list):
            kept = [x for x in listed if str(x) not in drop]
            ops.append(_lib_confedit.set_op(["optional_overrides"], _lib_confedit.seq_value(
                [_lib_confedit.str_value(str(x)) for x in kept])) if kept
                else _lib_confedit.unset_op(["optional_overrides"]))
    return sorted(by_file.items())


def _prove_copies(config_dir: str, plan: Plan) -> Plan:
    """Keep only the deletions that change nothing served: apply them to a
    scratch copy, compare served-values, drop every candidate of a key that
    changed, repeat until the batch changes nothing."""
    deletions = list(plan.deletions)
    not_copies: list[tuple[Deletion, str]] = []
    if not deletions:
        return plan
    before = _served(config_dir)
    root_data = _root_carrier(config_dir)[1]
    for _ in range(len(deletions) + 1):
        with tempfile.TemporaryDirectory(prefix="migrate-builtin-") as scratch:
            copy = Path(scratch) / "conf.d"
            shutil.copytree(config_dir, copy, symlinks=True)
            _lib_confedit.edit_all(copy, _edits(deletions, [], plan.root, root_data))
            changed = _changed_keys(before, _served(str(copy)))
        if not changed:
            break
        for d in [d for d in deletions if d.key in changed]:
            deletions.remove(d)
            not_copies.append((d, "removing it changes what /metrics serves"))
    return plan._replace(deletions=deletions, not_copies=not_copies)


def _show(d: Deletion) -> str:
    return f"{d.file}: {'.'.join(d.path)} = {d.value!r}"


def run(config_dir: str, *, apply: bool, digests: list[str], keep: set[str]) -> int:
    plan = _prove_copies(config_dir, _candidates(config_dir, keep))
    print(f"built-in table {plan.digest}")
    print(f"copies of a built-in value ({len(plan.deletions)}):")
    for d in plan.deletions:
        print(f"  delete {_show(d)}")
    print(f"switched off the old way, rewritten to \"{DISABLE}\" ({len(plan.rewrites)}):")
    for r in plan.rewrites:
        print(f"  {plan.root}: defaults.{r.key} = \"{DISABLE}\" (was {r.why})")
    for d, why in plan.not_copies:
        print(f"  kept {_show(d)} — equal to the built-in value, but {why}")
    if keep:
        print(f"kept by --keep: {', '.join(sorted(keep))}")
    rc = EXIT_VIOLATION if plan.not_copies else EXIT_OK
    if not apply:
        print("preview only: nothing written (--apply writes)")
        return rc
    deletions = plan.deletions
    if deletions:
        wrong = sorted({d for d in digests if d != plan.digest})
        if not digests:
            print(f"not deleted: deleting a copy needs --exporter-digest for every deployed "
                  f"exporter (da_config_builtin_defaults_info), and none was given", file=sys.stderr)
            deletions = []
        elif wrong:
            print(f"refused: exporter digest(s) {', '.join(wrong)} differ from this tool's built-in "
                  f"table {plan.digest}; an exporter without this table serves nothing for a key "
                  f"whose copy is deleted (upgrade it first)", file=sys.stderr)
            deletions, rc = [], EXIT_VIOLATION
    edits = _edits(deletions, plan.rewrites, plan.root, _root_carrier(config_dir)[1])
    if not edits:
        print("nothing written")
        return rc
    _lib_confedit.edit_all(config_dir, edits)
    print(f"written: {len(deletions)} deletion(s), {len(plan.rewrites)} rewrite(s), "
          f"{len(edits)} file(s)")
    return rc


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="migrate-builtin-defaults",
        description="Remove copies of the platform's built-in thresholds from a conf.d tree, "
                    "and rewrite keys switched off with null or optional_overrides to "
                    "\"disable\" (#3832). Preview unless --apply.")
    p.add_argument("--config-dir", required=True, help="the conf.d tree")
    p.add_argument("--apply", action="store_true", help="write the changes (default: preview)")
    p.add_argument("--exporter-digest", action="append", default=[], metavar="DIGEST",
                   help="built-in table digest of a deployed exporter "
                        "(da_config_builtin_defaults_info); once per cluster; required to delete")
    p.add_argument("--keep", action="append", default=[], metavar="KEY",
                   help="leave this key's values alone (pinned on purpose); repeatable")
    args = p.parse_args(argv)
    if not Path(args.config_dir).is_dir():
        print(f"error: --config-dir {args.config_dir}: not a directory", file=sys.stderr)
        return EXIT_CALLER_ERROR
    try:
        return run(args.config_dir, apply=args.apply, digests=args.exporter_digest,
                   keep=set(args.keep))
    except (tv.DaGuardError, tv.DaGuardNotFoundError, YamlFileError, _lib_confedit.EditError,
            _lib_confedit.EditsRefused, _lib_confedit.PartialWrite, OSError) as exc:
        print(f"error: {' '.join(str(exc).split())}", file=sys.stderr)
        return EXIT_CALLER_ERROR


if __name__ == "__main__":
    sys.exit(main())
