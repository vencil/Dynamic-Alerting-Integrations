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
    `da-guard effective`'s `builtin_defaults`; which file a tenant's value
    comes from: its `key_sources`; which root items are listed: the
    parse-tree node's scalar text (what the exporter reads), re-emitted as
    read when the list is rewritten;
  * whether a deletion only removes a copy: the batch is applied to a scratch
    copy of the tree on top of the rewrites, and `da-guard served-values`
    compared with the rewrites alone. A key whose served state changes has
    each of its candidates tried on its own; one that still changes something
    (for example a tenant value that overrides a subdirectory's, or a tenant
    value that keeps a key on under a root switched off) is kept and named.

Both writes need the exporters to carry the built-in table. A deleted copy
leaves a reader without the table nothing to serve for that key; a root
`"disable"` makes a released exporter without it drop the whole root
`_defaults.yaml` (v2.9.0: `entire block dropped`), every tenant's root rows
with it. So any write needs `--exporter-digest`, the built-in table digest of
every exporter that reads the tree (`da_config_builtin_defaults_info{digest}`,
one per cluster the conf.d feeds), and nothing is written when one is missing
or any differs from this tool's table (decisions 5, 7 and 9: the tree changes
in the same change that upgrades the exporters).

A number equal to the platform's may be pinned on purpose, so nothing is written
without `--apply`, and `--keep <key>` leaves a key alone.

Exit codes: 0 previewed or written (a value equal to the built-in one that is
not a copy — it overrides another layer — is named and kept, which is no
failure); 1 nothing written because an exporter digest differs; 2 caller
error, including a write asked for without --exporter-digest.
"""
from __future__ import annotations

import argparse
import math
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, NamedTuple, Optional

_THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_THIS_DIR))
sys.path.insert(0, str(_THIS_DIR.parent))

import _lib_confedit  # noqa: E402  (ADR-036 §2: conf.d goes to da-guard write)
import _lib_exitcodes  # noqa: E402
import _lib_input  # noqa: E402
import _lib_parse_cache  # noqa: E402
import _lib_parse_tree  # noqa: E402
import _lib_tenant_values as tv  # noqa: E402
from _lib_exitcodes import EXIT_CALLER_ERROR, EXIT_OK, EXIT_VIOLATION  # noqa: E402
from _lib_io import YamlFileError, exit_on_yaml_file_error  # noqa: E402  (#1654)
from _lib_python import detect_cli_lang  # noqa: E402

DISABLE = "disable"
#: Served at one fixed instant on both sides of the comparison, with the whole
#: day's schedules, so a time-of-day cannot read as a change.
_AT = "2026-01-01T12:00:00Z"

_LANG = detect_cli_lang()
_HELP = {
    "description": {
        "en": "Remove copies of the platform's built-in thresholds from a conf.d tree, and "
              "rewrite built-in keys switched off with null or optional_overrides to "
              "\"disable\" (#3832). Preview unless --apply.",
        "zh": "刪除 conf.d 樹裡平台內建閾值的複製品，並把用 null 或 optional_overrides 關掉的"
              "內建鍵改寫成 \"disable\"（#3832）。預設只預覽，--apply 才寫入。",
    },
    "config_dir": {"en": "the conf.d tree", "zh": "conf.d 目錄"},
    "apply": {"en": "write the changes (default: preview)", "zh": "寫入（預設只預覽）"},
    "exporter_digest": {
        "en": "built-in table digest of an exporter that reads this tree "
              "(da_config_builtin_defaults_info); once per cluster; required to write",
        "zh": "讀這棵樹的 exporter 的內建表摘要（da_config_builtin_defaults_info）；每個叢集給一次；"
              "寫入時必填",
    },
    "keep": {"en": "leave this key's values alone (pinned on purpose); repeatable",
             "zh": "這個鍵的值不動（刻意釘住的數字）；可重複"},
}


def _h(key: str) -> str:
    return _HELP[key].get(_LANG, _HELP[key]["en"])


class Deletion(NamedTuple):
    file: str
    path: tuple[str, ...]   # ("defaults", key) or ("tenants", tenant, key)
    key: str
    value: Any


class Rewrite(NamedTuple):
    key: str
    why: str                # "null" or "optional_overrides"


class Root(NamedTuple):
    rel: Optional[str]      # the root defaults carrier, as parse-tree names it
    data: dict
    node: dict


class Plan(NamedTuple):
    root: Root
    deletions: list[Deletion]
    rewrites: list[Rewrite]
    kept: list[tuple[Deletion, str]]
    digest: str


def _number(value: Any) -> Optional[float]:
    """*value* as a finite number when it is a plain scalar threshold, else None
    (a schedule, a mapping, `disable`, text that is no number)."""
    if isinstance(value, bool) or isinstance(value, (dict, list)) or value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _root(config_dir: str) -> Root:
    """The root defaults carrier the exporter reads (parse-tree's choice): its
    data and its parse-tree node; Root(None, {}, {}) when the tree has none."""
    reader = _lib_parse_cache.reader_for(config_dir)
    selected = reader.tree().selected
    names = sorted(e.path for e in reader.listing().entries
                   if e.depth == 0 and e.config_file and e.cls == "defaults" and e.path in selected)
    if not names:
        return Root(None, {}, {})
    data, node = reader.read(names[0]), reader.node(names[0])
    return Root(names[0], data if isinstance(data, dict) else {},
                node if isinstance(node, dict) and not _lib_parse_tree.is_scalar(node) else {})


def _listed(root: Root) -> list:
    items = root.node.get("optional_overrides")
    return items if isinstance(items, list) else []


def _listed_text(item: Any) -> Optional[str]:
    return item["text"] if _lib_parse_tree.is_scalar(item) else None


class ProofError(RuntimeError):
    """The copy proof did not settle (the tree changed under the run, or the
    deletions keep changing what is served): nothing is written, rc 2."""


def _symlinked(config_dir: str, rel: str) -> bool:
    path = os.path.join(config_dir, rel)
    return os.path.realpath(path) != os.path.abspath(path)


def _candidates(config_dir: str, keep: set[str]) -> Plan:
    tree = tv.load_effective_tree(config_dir)
    builtin = tree.builtin_defaults
    if builtin is None:
        # A da-guard older than the built-in layer: the binary's fault (stale).
        raise tv.DaGuardError("da-guard ran no build of this tree (no builtin_defaults)",
                              None, "", stale=True)
    root = _root(config_dir)
    found: list[Deletion] = []
    for key, entry in sorted(builtin.keys.items()):
        if entry.state == "overridden" and entry.root_value == entry.value and key not in keep:
            found.append(Deletion(root.rel, ("defaults", key), key, entry.root_value))
    for tenant, te in sorted(tree.tenants.items()):
        for key, entry in sorted(builtin.keys.items()):
            src = te.key_sources.get(key)
            if src is None or src.layer != "tenant" or key in keep:
                continue
            if _number(te.effective_config.get(key)) == entry.value:
                found.append(Deletion(src.file, ("tenants", tenant, key), key,
                                      te.effective_config[key]))
    deletions, kept = [], []
    for d in found:
        if _symlinked(config_dir, d.file):
            kept.append((d, "its file is a symlink, which da-guard write does not write through"))
        else:
            deletions.append(d)
    defaults = root.data.get("defaults") if isinstance(root.data.get("defaults"), dict) else {}
    listed = {_listed_text(i) for i in _listed(root)}
    rewrites: list[Rewrite] = []
    for key in sorted(builtin.keys):
        if key in defaults and defaults[key] is None:
            rewrites.append(Rewrite(key, "null"))
        elif key in listed and key not in defaults:
            rewrites.append(Rewrite(key, "optional_overrides"))
    return Plan(root, deletions, rewrites, kept, builtin.digest)


def _edits(deletions: list[Deletion], rewrites: list[Rewrite],
           root: Root) -> list[tuple[str, list[dict]]]:
    by_file: dict[str, list[dict]] = {}
    for d in deletions:
        by_file.setdefault(d.file, []).append(_lib_confedit.unset_op(list(d.path)))
    if rewrites and root.rel is not None:
        ops = by_file.setdefault(root.rel, [])
        ops.append(_lib_confedit.ensure_map_op(["defaults"]))
        for r in rewrites:
            ops.append(_lib_confedit.set_op(["defaults", r.key], _lib_confedit.str_value(DISABLE)))
        drop = {r.key for r in rewrites}
        items = _listed(root)
        if any(_listed_text(i) in drop for i in items):
            # The other items are re-emitted as the reader read them, never
            # re-typed here (a `1e2` stays `1e2`).
            kept = [i for i in items if _listed_text(i) not in drop]
            ops.append(_lib_confedit.set_op(["optional_overrides"], _lib_confedit.node_value(kept))
                       if kept else _lib_confedit.unset_op(["optional_overrides"]))
    return sorted(by_file.items())


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


def _served_after(config_dir: str, deletions: list[Deletion], rewrites: list[Rewrite],
                  root: Root) -> dict:
    """served-values of a scratch copy of the tree with *rewrites* and
    *deletions* applied (symlinks followed: the copy holds their content)."""
    with tempfile.TemporaryDirectory(prefix="migrate-builtin-") as scratch:
        copy = Path(scratch) / "conf.d"
        shutil.copytree(config_dir, copy, symlinks=False)
        edits = _edits(deletions, rewrites, root)
        if edits:
            _lib_confedit.edit_all(copy, edits)
        return tv.load_served_values(copy, at=_AT, schedules=True)


def _prove_copies(config_dir: str, plan: Plan) -> Plan:
    """Keep only the deletions that change nothing served, judged on top of
    the rewrites the same run writes."""
    if not plan.deletions:
        return plan
    base = _served_after(config_dir, [], plan.rewrites, plan.root)
    deletions, kept = list(plan.deletions), list(plan.kept)
    for _ in range(len(plan.deletions) + 1):
        changed = _changed_keys(base, _served_after(config_dir, deletions, plan.rewrites, plan.root))
        if not changed:
            return plan._replace(deletions=deletions, kept=kept)
        suspects = [d for d in deletions if d.key in changed]
        if not suspects:
            break
        for d in suspects:
            if _changed_keys(base, _served_after(config_dir, [d], plan.rewrites, plan.root)):
                deletions.remove(d)
                kept.append((d, "removing it changes what /metrics serves"))
        if all(d in deletions for d in suspects):
            # Each is a copy alone but not together: keep them all, named.
            for d in suspects:
                deletions.remove(d)
                kept.append((d, "removing it together with the others changes what /metrics serves"))
    raise ProofError("the deletions could not be proven to change nothing served")


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
        print(f"  {plan.root.rel}: defaults.{r.key} = \"{DISABLE}\" (was {r.why})")
    for d, why in plan.kept:
        print(f"  kept {_show(d)} — equal to the built-in value, but {why}")
    if keep:
        print(f"kept by --keep: {', '.join(sorted(keep))}")
    if not apply:
        print("preview only: nothing written (--apply writes)")
        return EXIT_OK
    if not (plan.deletions or plan.rewrites):
        print("nothing written")
        return EXIT_OK
    # Why both writes are gated: an exporter without the built-in table serves
    # nothing for a deleted copy, and drops the whole root _defaults.yaml over
    # a root "disable" (released versions; decision 5).
    consequence = ("an exporter without this table serves nothing for a key whose copy is "
                   "deleted, and drops the whole root _defaults.yaml over a \"disable\"")
    if not digests:
        print("not written: deleting a copy or writing \"disable\" needs --exporter-digest for "
              "every exporter that reads this tree (da_config_builtin_defaults_info), and none "
              f"was given; {consequence}", file=sys.stderr)
        return EXIT_CALLER_ERROR
    wrong = sorted({d for d in digests if d != plan.digest})
    if wrong:
        print(f"refused: exporter digest(s) {', '.join(wrong)} differ from this tool's built-in "
              f"table {plan.digest}; nothing written: {consequence} (change the tree in the same "
              "change that upgrades the exporters)", file=sys.stderr)
        return EXIT_VIOLATION
    edits = _edits(plan.deletions, plan.rewrites, plan.root)
    _lib_confedit.edit_all(config_dir, edits)
    print(f"written: {len(plan.deletions)} deletion(s), {len(plan.rewrites)} rewrite(s), "
          f"{len(edits)} file(s)")
    return EXIT_OK


@exit_on_yaml_file_error  # #1654: an unreadable YAML file → rc 2, named
def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="migrate-builtin-defaults", description=_h("description"))
    # An empty conf.d is refused naming the flag (#3751), as explain-keys'.
    _lib_input.add_input(p, "--config-dir", select=_lib_input.DA_GUARD_LISTS,
                         empty=_lib_input.EMPTY_REFUSED, required=True, help=_h("config_dir"))
    p.add_argument("--apply", action="store_true", help=_h("apply"))
    p.add_argument("--exporter-digest", action="append", default=[], metavar="DIGEST",
                   help=_h("exporter_digest"))
    p.add_argument("--keep", action="append", default=[], metavar="KEY", help=_h("keep"))
    args = p.parse_args(argv)
    # Every refusal names the tree it was about (a file of it may have gone
    # mid-run, and da-guard names that file relative to the tree).
    where = f"--config-dir {args.config_dir}"
    try:
        # One reader of --config-dir for the run (the tree the parser judged).
        with _lib_parse_cache.session():
            # #3751: judged once — the path, a root da-guard cannot list, an
            # empty tree — each refusal naming --config-dir (CallerError: rc 2).
            [conf_d] = _lib_parse_cache.declared_trees(args, "config_dir", lang=_LANG)
            return run(str(conf_d), apply=args.apply, digests=args.exporter_digest,
                       keep=set(args.keep))
    except (ProofError, tv.DaGuardError, tv.DaGuardNotFoundError, YamlFileError,
            _lib_confedit.EditError,
            _lib_confedit.EditsRefused, _lib_confedit.PartialWrite, OSError, ValueError,
            _lib_parse_tree.ParseTreeError) as exc:
        # ValueError: a kept `optional_overrides:` item the edit cannot write back
        # exactly as written (`_lib_confedit.node_value`) — named, nothing written.
        print(f"error: {where}: {' '.join(str(exc).split())}", file=sys.stderr)
        return EXIT_CALLER_ERROR


if __name__ == "__main__":
    _lib_exitcodes.run(main)
