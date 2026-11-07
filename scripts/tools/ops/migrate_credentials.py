#!/usr/bin/env python3
"""migrate_credentials.py — find receiver credentials written out in conf.d and
move them into Secret references (#1560 ⑤-a). REPORT ONLY: nothing refuses a
written-out credential yet; the route generator will (⑤-b), by the same
judgement (`_lib_validation.plaintext_credential_findings`).

Reads every receiver where a file writes it (`_grar_parse.raw_receivers`:
tenants' `_routing` / `overrides` / `routes` / `_routing_enforced`, and the
platform's `_routing_defaults`, routing profiles and `_routing_enforced`) and
produces three things:

1. The rewrite: each credential a tenant writes out where only a reference is
   taken becomes `{secret: {key: <key>}}`, the key derived from where it
   stands (`receiver.api_url`, `overrides.0.receiver.http_config.basic_auth.
   password`). Shown as a unified diff on stdout (every credential value in it
   reads `<redacted:N>`); `--write` applies it, through `da-guard write`
   (ADR-036 §2: Go writes conf.d). A list holding a rewritten value is
   written whole: the comments inside it are not kept.
2. The Secrets: with `--manifests-out DIR`, one Kubernetes Secret
   `da-<tenant>` per tenant (stringData), mode 0600 from creation. DIR may not
   be inside a git worktree, nor be or hold a conf.d file.
3. The rotation list: file, line, field and tenant of every credential that
   was written out (each was in the repository: rotate it — see
   docs/internal/secret-leak-remediation-sop.md). Never the value.

Left for a person (listed with the reason): credentials in platform places
(`_routing_defaults`, profiles, the root `_routing_enforced`, a tenant's
`_routing_enforced`), a webhook url with userinfo, the four proxy credentials
(the platform sets HTTPS_PROXY on Alertmanager from a Secret and the tenant
writes `proxy_from_environment: true`), a key that cannot be derived or is
taken. The rewritten tree is then judged by the generator's own receiver
binding (`secret_binding_findings`, under `--policy`): what would still be
refused is listed, and the run does not count as done.

⛔ No credential value reaches stdout, stderr or the JSON report: values go
only into the files under --manifests-out (`_write_manifest`).

Exit: 0 nothing written out; 1 found (the report is the answer); 2 usage /
I/O error, no usable da-guard, a file that cannot be read.

Usage:
    migrate_credentials.py --config-dir conf.d
    migrate_credentials.py --config-dir conf.d --manifests-out ~/secrets --write
    migrate_credentials.py --config-dir conf.d --policy policy.yaml --json
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import difflib
import io
import json
import os
import shutil
import sys
import tempfile
from typing import Any, NamedTuple, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
_TOOLS = os.path.dirname(_HERE)
for _p in (_TOOLS, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import _lib_confedit  # noqa: E402  (ADR-036 §2: Go writes conf.d)
import _lib_exitcodes  # noqa: E402
import _lib_io  # noqa: E402
import _lib_parse_cache  # noqa: E402
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, Report, die_caller_error  # noqa: E402
from _lib_parse_tree import ParseTreeError  # noqa: E402
from _lib_paths import classify_path  # noqa: E402
from _lib_validation import detect_cli_lang  # noqa: E402
from _lib_validation import (  # noqa: E402
    NEEDS_REFERENCE,
    PROXY_CREDENTIAL,
    URL_USERINFO,
    value_paths_to_mask,
    find_credential_refs,
    plaintext_credential_findings,
    ref_path_text,
    ref_value_at,
    secret_ref_problem,
)
from _grar_merge import OWN_NAME_PREFIX  # noqa: E402
from _grar_parse import RawReceiver, raw_receivers  # noqa: E402

TOOL = "migrate-credentials"
SCHEMA = "da-tools.migrate-credentials/v1"
#: How a credential value reads in the diff.
MASK = "<redacted:{}>"
#: A one-line stand-in for a value whose line is looked up (`_locate`).
_LOCATE = "@@da-migrate-locate-{}@@"

#: Why a finding is left for a person (``Item.reason``).
REASONS = {
    "platform_place": "a platform place every tenant that inherits it receives: a "
                      "shared Secret the deployment policy lists ({secret: {shared: "
                      "<alias>, key: <key>}}), or {secret: {key: <key>}} with the key in "
                      "each such tenant's da-<tenant>",
    "platform_enforced": "the platform's _routing_enforced (no tenant owns it): a shared "
                         "Secret the deployment policy lists ({secret: {shared: <alias>, "
                         "key: <key>}})",
    "unread_place": "a tenant's _routing_enforced is read by no plane: delete it",
    URL_USERINFO: "the URL carries userinfo: put the whole URL in the tenant's Secret "
                  "and write {secret: {key: <key>}}, or drop the userinfo",
    PROXY_CREDENTIAL: "a proxy credential the tenant may not write: drop it — the "
                      "platform gives Alertmanager HTTPS_PROXY from a Secret, and the "
                      "receiver writes http_config.proxy_from_environment: true",
    "not_a_string": "the value is not a string: write it as a string first",
    "key_invalid": "no Secret data key can be derived from this path "
                   "([-._a-zA-Z0-9]+, at most 253): choose one and rewrite it by hand",
    "key_taken": "the derived Secret data key is taken in this tenant: choose another "
                 "and rewrite it by hand",
    "list_not_rewritable": "the list holding it has a value `da-guard write` cannot "
                           "write back as read: rewrite it by hand",
    "rewrite_refused": "`da-guard write` refused the rewrite of this file",
}


class Item(NamedTuple):
    """One credential written out (a ``PlaintextFinding`` placed in its file)."""

    file: str
    path: tuple          # from the file's top level
    receiver_path: tuple  # the receiver's, from the file's top level
    field: str           # the receiver-relative field, as text
    tenant: Optional[str]
    category: str
    entry: Optional[str] = None      # the Secret data key, when rewritten
    reason: Optional[str] = None     # why not, otherwise


#: --help in the operator's language (dev-rules §9: detect_cli_lang).
_HELP = {
    "en": {
        "description": "Find receiver credentials written out in conf.d, rewrite the ones "
                       "a tenant owns as Secret references, and list what to rotate "
                       "(#1560; report only: nothing is refused yet).",
        "config_dir": "conf.d root (read through da-guard)",
        "write": "apply the rewrite to the conf.d files (default: print the diff); "
                 "needs --manifests-out",
        "manifests_out": "write one Kubernetes Secret da-<tenant> per tenant into DIR "
                         "(mode 0600; DIR may not be inside a git worktree)",
        "json": "print the report as one JSON document",
    },
    "zh": {
        "description": "找出 conf.d 裡明文寫出的 receiver 憑證：租戶自己的改寫成 Secret "
                       "參照，並列出要輪替的位置（#1560；只報告，目前不拒收）。",
        "config_dir": "conf.d 根目錄（經 da-guard 讀）",
        "write": "把改寫寫回 conf.d（預設只印 diff）；須同時給 --manifests-out",
        "manifests_out": "在 DIR 為每個租戶寫一份 Kubernetes Secret da-<tenant>"
                         "（0600；DIR 不得在 git worktree 內）",
        "json": "報告以一份 JSON 文件輸出",
    },
}


def _parse_args(argv: Optional[list[str]]) -> argparse.Namespace:
    import generate_alertmanager_routes as gar
    lang = "zh" if detect_cli_lang() == "zh" else "en"
    h = _HELP[lang]
    p = argparse.ArgumentParser(prog="da-tools migrate-credentials",
                                description=h["description"])
    _lib_io.add_config_dir_arg(p, help_text=h["config_dir"])
    p.add_argument("--write", action="store_true", help=h["write"])
    p.add_argument("--manifests-out", metavar="DIR", default=None, help=h["manifests_out"])
    gar.add_policy_argument(p, lang=lang)
    p.add_argument("--json", action="store_true", dest="json_output", help=h["json"])
    return p.parse_args(argv)


def _git_worktree_of(path: str) -> Optional[str]:
    """The git worktree *path* (existing or not) is inside, or None: the
    nearest directory up from its real path holding a `.git` entry."""
    here = os.path.realpath(os.path.abspath(path))
    while True:
        if os.path.lexists(os.path.join(here, ".git")):
            return here
        parent = os.path.dirname(here)
        if parent == here:
            return None
        here = parent


def _check_manifests_dir(out: str, config_dir: str, config_files: list[str],
                         names: list[str]) -> None:
    """Refuse (rc 2, before anything is written) a --manifests-out the
    Secrets must not land in: inside a git worktree, the conf.d or inside it,
    a path that is not a directory, or a manifest that is a conf.d file."""
    worktree = _git_worktree_of(out)
    if worktree is not None:
        die_caller_error(f"ERROR: --manifests-out {_lib_io.safe_label(out)} is inside the "
                         f"git worktree {_lib_io.safe_label(worktree)}: Secrets must not "
                         "be written where they can be committed; nothing was written")
    real_out = os.path.realpath(out)
    real_conf = os.path.realpath(config_dir)
    if real_out == real_conf or real_out.startswith(real_conf.rstrip(os.sep) + os.sep):
        die_caller_error("ERROR: --manifests-out is the conf.d or inside it; nothing was "
                         "written")
    if os.path.lexists(out) and not os.path.isdir(out):
        die_caller_error(f"ERROR: --manifests-out {_lib_io.safe_label(out)} is not a "
                         "directory; nothing was written")
    for conf in config_files:
        for name in names:
            if _lib_io.same_file(os.path.join(out, name), conf):
                die_caller_error(f"ERROR: --manifests-out {_lib_io.safe_label(name)} is the "
                                 f"conf.d file {_lib_io.safe_label(conf)}; nothing was "
                                 "written")


def _object_name(tenant: str) -> str:
    """The name of *tenant*'s own Secret, ``da-<tenant>`` — what
    ``_grar_merge.own_secret_name`` gives (pinned equal by
    tests/ops/test_migrate_credentials.py), spelled here so that the name this
    report prints is not read as the value of a function named for secrets."""
    return f"{OWN_NAME_PREFIX}{tenant}"


def _slot_of(receiver_path: tuple) -> str:
    """A tenant receiver's place in its `_routing`, as a key prefix:
    ``receiver``, ``overrides.<i>.receiver``, ``routes.<i>.receiver``."""
    return ".".join(str(p) for p in receiver_path[3:])  # after tenants.<t>._routing


def _derived_entry(r: RawReceiver, field_path: tuple) -> Optional[str]:
    """The Secret data key of the credential at *field_path* of tenant
    receiver *r*, or None when it is not a valid key (``secret_ref_problem``)."""
    entry = ".".join([_slot_of(r.path)] + [str(p) for p in field_path])
    if secret_ref_problem({"secret": {"key": entry}}) is not None:
        return None
    return entry


def _existing_entries(raws: list[RawReceiver]) -> dict[str, set[str]]:
    """Per tenant, the data keys its receivers already reference in its own
    Secret (no ``shared``)."""
    out: dict[str, set[str]] = {}
    for r in raws:
        if r.tenant is None:
            continue
        for _site, path in find_credential_refs(r.receiver):
            inner = ref_value_at(r.receiver, path)["secret"]
            if "shared" not in inner and isinstance(inner.get("key"), str):
                out.setdefault(r.tenant, set()).add(inner["key"])
    return out


def _judge(raws: list[RawReceiver]) -> list[Item]:
    """Every credential written out, rewritable or with the reason it is not."""
    taken = _existing_entries(raws)
    items: list[Item] = []
    for r in raws:
        for f in plaintext_credential_findings(r.receiver):
            base = Item(r.file, r.path + f.path, r.path, ref_path_text(f.path), r.tenant,
                        f.category)
            if f.category != NEEDS_REFERENCE:
                items.append(base._replace(reason=f.category))
            elif r.tenant is None:
                items.append(base._replace(reason="platform_enforced"
                                           if r.place == "routing_enforced"
                                           else "platform_place"))
            elif r.place != "tenant_routing":
                items.append(base._replace(reason="unread_place"))
            elif not isinstance(ref_value_at(r.receiver, f.path), str):
                items.append(base._replace(reason="not_a_string"))
            else:
                entry = _derived_entry(r, f.path)
                items.append(base._replace(entry=entry) if entry is not None
                             else base._replace(reason="key_invalid"))
    counts: dict[tuple[str, str], int] = {}
    for it in items:
        if it.entry is not None:
            counts[(it.tenant, it.entry)] = counts.get((it.tenant, it.entry), 0) + 1
    return [it._replace(entry=None, reason="key_taken")
            if it.entry is not None and (counts[(it.tenant, it.entry)] > 1
                                         or it.entry in taken.get(it.tenant, ()))
            else it for it in items]


# ── edits: what `da-guard write` is asked to do ─────────────────────────

def _str_node(text: str) -> dict:
    return {"text": text, "tag": "!!str", "value": text}


def _ref_node(entry: str) -> dict:
    return {"secret": {"key": _str_node(entry)}}


def _node_at(node: Any, path: tuple) -> Any:
    for part in path:
        node = node[part]
    return node


def _put(node: Any, path: tuple, value: Any) -> None:
    _node_at(node, path[:-1])[path[-1]] = value


def _ops(file_node: Any, replacements: dict[tuple, Any]) -> list[dict]:
    """The `da-guard write` operations that put each tagged node of
    *replacements* at its path of *file_node* (the file's parse-tree nodes).
    The edit addresses mappings only: a path through a list sets that list
    whole, rebuilt from *file_node* (``node_value``; ValueError when a value
    in it cannot be written back as read)."""
    direct: dict[tuple, Any] = {}
    lists: dict[tuple, list[tuple[tuple, Any]]] = {}
    for path, value in replacements.items():
        cut = next((i for i, p in enumerate(path) if isinstance(p, int)), None)
        if cut is None:
            direct[path] = value
        else:
            lists.setdefault(path[:cut], []).append((path[cut:], value))
    ops = [_lib_confedit.set_op(list(p), _lib_confedit.node_value(v))
           for p, v in sorted(direct.items(), key=lambda kv: [str(x) for x in kv[0]])]
    for prefix in sorted(lists, key=lambda p: [str(x) for x in p]):
        whole = copy.deepcopy(_node_at(file_node, prefix))
        for rel, value in lists[prefix]:
            _put(whole, rel, value)
        ops.append(_lib_confedit.set_op(list(prefix), _lib_confedit.node_value(whole)))
    return ops


def _list_prefixes(paths: list[tuple]) -> set[tuple]:
    return {p[:next(i for i, x in enumerate(p) if isinstance(x, int))]
            for p in paths if any(isinstance(x, int) for x in p)}


def _dry_run(config_dir: str, edits: dict[str, list[dict]]) -> dict[str, str]:
    """{file: the text a dry run of `da-guard write` gives it} for *edits*, or
    raises ``_lib_confedit.EditsRefused`` / ``EditCallerError`` /
    ``ParseTreeError`` (nothing is written either way)."""
    batch = [(rel, ops) for rel, ops in sorted(edits.items()) if ops]
    results = _lib_confedit.edit_all(config_dir, batch, dry_run=True)
    return {r["path"]: r.get("content") or "" for r in results}


def _refusal_text(exc: BaseException) -> str:
    """Why a dry run failed, without da-guard's message (which may quote the
    file): the refusal reasons, or the exception's class."""
    refusals = getattr(exc, "refusals", None)
    if refusals:
        return ", ".join(sorted({f"{r.path}: {r.reason}" for r in refusals}))
    reason = getattr(exc, "reason", None)
    return f"{type(exc).__name__}{f' ({reason})' if reason else ''}"


def _marker_lines(original: str, located: str, markers: dict[str, Any]) -> dict[Any, int]:
    """{markers[m]: line in *original*} for each marker m found in *located*
    (*original* with values replaced by one-line markers by `da-guard
    write`): the marker's line is mapped back through a line diff, taking
    the first original line of the changed block that starts with the same
    key text (``key:`` before the marker) — a value written on several lines
    collapses to one, so offsets alone would drift."""
    old, new = original.splitlines(), located.splitlines()
    out: dict[Any, int] = {}
    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        cursor = i1
        for j in range(j1, j2):
            line = new[j]
            for marker, what in markers.items():
                at = line.find(marker)
                if at < 0:
                    continue
                # The key text before the marker, without its indentation:
                # the encoder may indent differently from the file.
                key = line[:at].rstrip(" \"'").lstrip()
                for k in range(cursor, i2):
                    if key and old[k].lstrip().startswith(key):
                        out[what] = k + 1
                        cursor = k + 1
                        break
    return out


class Plan(NamedTuple):
    diff: str
    rewritten: dict[str, str]     # file → text with the rewrite (no masking)
    lines: dict[tuple, int]       # (file, path) → line
    exact: set                    # (file, path) whose line is the value's own
    notes: list[str]
    items: list[Item]


def _plan(config_dir: str, reader: Any, items: list[Item], raws: list[RawReceiver]) -> Plan:
    notes: list[str] = []
    by_file: dict[str, list[Item]] = {}
    for it in items:
        by_file.setdefault(it.file, []).append(it)
    nodes = {f: reader.node(os.path.join(config_dir, f)) for f in by_file}

    # 1. lines: each finding's value (or the list holding it) → a marker.
    locate: dict[str, list[dict]] = {}
    marks: dict[str, dict[str, Any]] = {}
    exact: set = set()
    for f, its in by_file.items():
        reps: dict[tuple, Any] = {}
        mk: dict[str, Any] = {}
        for it in its:
            prefixes = _list_prefixes([it.path])
            where = next(iter(prefixes)) if prefixes else it.path
            if not prefixes:
                exact.add((f, it.path))
            if where not in reps:
                m = _LOCATE.format(len(mk))
                reps[where] = _str_node(m)
                mk[m] = where
        try:
            locate[f] = _ops(nodes[f], reps)
            marks[f] = mk
        except ValueError:
            pass
    lines: dict[tuple, int] = {}
    try:
        located = _dry_run(config_dir, locate)
        for f, text in located.items():
            original = _read_text(os.path.join(config_dir, f))
            found = _marker_lines(original, text, marks[f])
            for it in by_file[f]:
                prefixes = _list_prefixes([it.path])
                where = next(iter(prefixes)) if prefixes else it.path
                if where in found:
                    lines[(f, it.path)] = found[where]
    except (_lib_confedit.EditsRefused, _lib_confedit.EditCallerError, ParseTreeError) as exc:
        notes.append(f"line numbers unavailable: {_refusal_text(exc)}")

    # 2. the rewrite, and the masked diff of it.
    masks_by_file: dict[str, dict[tuple, Any]] = {}
    for r in raws:
        if r.file in by_file:
            for p in value_paths_to_mask(r.receiver):
                masks_by_file.setdefault(r.file, {})[r.path + p] = None
    plain: dict[str, list[dict]] = {}
    before: dict[str, list[dict]] = {}
    after: dict[str, list[dict]] = {}
    final = list(items)
    for f, its in by_file.items():
        rewrite = {it.path: _ref_node(it.entry) for it in its if it.entry is not None}
        if not rewrite:
            continue
        masked = {p: _str_node(MASK.format(i))
                  for i, p in enumerate(sorted(masks_by_file.get(f, {}),
                                               key=lambda q: [str(x) for x in q]))}
        try:
            plain[f] = _ops(nodes[f], rewrite)
            before[f] = _ops(nodes[f], masked)
            after[f] = _ops(nodes[f], {**masked, **rewrite})
        except ValueError:
            final = [it._replace(entry=None, reason="list_not_rewritable")
                     if it.file == f and it.entry is not None else it for it in final]
            plain.pop(f, None)
            before.pop(f, None)
            continue
        for prefix in sorted(_list_prefixes(list(rewrite)), key=lambda p: [str(x) for x in p]):
            notes.append(f"{f}: {'.'.join(str(x) for x in prefix)} is a list written whole: "
                         "the comments inside it are not kept")
    rewritten: dict[str, str] = {}
    diff = ""
    if plain:
        try:
            rewritten = _dry_run(config_dir, plain)
        except (_lib_confedit.EditsRefused, _lib_confedit.EditCallerError,
                ParseTreeError) as exc:
            notes.append(f"the rewrite was refused: {_refusal_text(exc)}")
            final = [it._replace(entry=None, reason="rewrite_refused")
                     if it.file in plain and it.entry is not None else it for it in final]
            plain = {}
    if plain:
        try:
            old = _dry_run(config_dir, before)
            new = _dry_run(config_dir, after)
            diff = "".join(
                "".join(difflib.unified_diff(old[f].splitlines(True), new[f].splitlines(True),
                                             fromfile=f"a/{f}", tofile=f"b/{f}"))
                for f in sorted(new))
        except (_lib_confedit.EditsRefused, _lib_confedit.EditCallerError,
                ParseTreeError) as exc:
            notes.append(f"the diff could not be shown: {_refusal_text(exc)}")
    return Plan(diff, rewritten, lines, exact, notes, final)


def _read_text(path: str) -> str:
    with open(path, encoding="utf-8", newline="") as fh:
        return fh.read()


# ── the rewritten tree, judged by the generator ─────────────────────────

def _still_refused(config_dir: str, rewritten: dict[str, str], policy: Any) -> list[dict]:
    """The generator's Secret-reference findings (``secret_ref_*``) on a copy
    of the tree with *rewritten* in place, under *policy*: what the rewrite
    leaves refused. Each as kind / tenant / field — never the line, which
    may quote a written value. The run's own output is not shown (it is the
    generator's, about a copy)."""
    import generate_alertmanager_routes as gar
    with tempfile.TemporaryDirectory(prefix="da-migrate-credentials-") as tmp:
        copy_root = os.path.join(tmp, "conf.d")
        shutil.copytree(config_dir, copy_root, symlinks=True)
        for rel, text in rewritten.items():
            target = os.path.join(copy_root, rel)
            with open(target, "w", encoding="utf-8", newline="") as fh:
                fh.write(text)
            os.chmod(target, 0o600)
        sink = io.StringIO()
        try:
            with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                tree = gar.load_tenant_tree(copy_root)
                _r, _v, warnings = gar.generate_routes(
                    tree.routing_configs, allowed_domains=policy.allowed_domains,
                    enforced_routing=tree.enforced_routing, tenants=tree.dedup_configs,
                    secrets=policy.secret_binding())
        except SystemExit:
            return [{"kind": "tree_refused", "tenant": None, "field": None}]
        finally:
            _lib_parse_cache.forget(copy_root)
    out = []
    for w in warnings:
        kind = getattr(w, "kind", "")
        if isinstance(kind, str) and kind.startswith("secret_ref_"):
            row = {"kind": kind, "tenant": getattr(w, "tenant", None),
                   "field": getattr(w, "field", None)}
            if row not in out:
                out.append(row)
    return out


# ── the Secrets (the one path a value is written to) ────────────────────

def _manifest_text(name: str, pairs: list[tuple[str, str]]) -> str:
    """A Kubernetes Secret *name* with *pairs* as stringData. Every scalar is
    a JSON string, which YAML reads as the same string."""
    lines = ["apiVersion: v1", "kind: Secret", "metadata:",
             f"  name: {json.dumps(name)}", "type: Opaque", "stringData:"]
    lines += [f"  {json.dumps(k)}: {json.dumps(v)}" for k, v in pairs]
    return "\n".join(lines) + "\n"


def _write_manifest(path: str, text: str) -> None:
    """Write *text* to *path*, created 0600 (the kernel open; an existing
    file this user owns is set to 0600 before a byte is written)."""
    _lib_io.write_operator_output_or_die(path, text, flag="--manifests-out", mode=0o600)


def _write_manifests(out: str, raws: list[RawReceiver], items: list[Item]) -> list[dict]:
    receivers = {(r.file, r.path): r.receiver for r in raws}
    by_tenant: dict[str, list[tuple[str, str]]] = {}
    for it in items:
        if it.entry is None:
            continue
        value = ref_value_at(receivers[(it.file, it.receiver_path)],
                             it.path[len(it.receiver_path):])
        by_tenant.setdefault(it.tenant, []).append((it.entry, value))
    try:
        os.makedirs(out, mode=0o700, exist_ok=True)
    except OSError as exc:
        die_caller_error(f"ERROR: cannot create --manifests-out {_lib_io.safe_label(out)}: "
                         f"{exc.strerror}; nothing was written")
    written = []
    for tenant in sorted(by_tenant):
        name = _object_name(tenant)
        path = os.path.join(out, f"{name}.yaml")
        _write_manifest(path, _manifest_text(name, sorted(by_tenant[tenant])))
        written.append({"name": name, "tenant": tenant, "path": path,
                        "entries": sorted(e for e, _v in by_tenant[tenant])})
    return written


# ── the report ──────────────────────────────────────────────────────────

def _row(it: Item, plan: Plan) -> dict:
    return {"file": it.file, "line": plan.lines.get((it.file, it.path)),
            "line_is_field": (it.file, it.path) in plan.exact,
            "field": it.field, "location": ref_path_text(it.path),
            "tenant": it.tenant, "category": it.category}


def _print_text(doc: dict) -> None:
    def where(row: dict) -> str:
        line = f":{row['line']}" if row["line"] is not None else ""
        held = "" if row["line_is_field"] or row["line"] is None else " (in the list there)"
        return f"{row['file']}{line} {row['location']}{held}"

    if doc["diff"]:
        print("== Rewrite (credential values read <redacted:N>) ==")
        print(doc["diff"], end="" if doc["diff"].endswith("\n") else "\n")
        print("(applied)" if doc["written"] else "(not applied: add --write)")
    if doc["manifests"]:
        print("== Kubernetes Secrets ==")
        for m in doc["manifests"]:
            dest = m["path"] or "not written: give --manifests-out DIR"
            print(f"  {m['name']} ({', '.join(m['entries'])}) -> {dest}")
    if doc["decisions"]:
        print("== Left for a person ==")
        for row in doc["decisions"]:
            print(f"  {where(row)} [{row['tenant'] or 'platform'}] {REASONS[row['reason']]}")
    if doc["rotate"]:
        print("== Rotate (written out in the repository; "
              "docs/internal/secret-leak-remediation-sop.md) ==")
        for row in doc["rotate"]:
            print(f"  {where(row)} [{row['tenant'] or 'platform'}] {row['category']}")
    if doc["blocked_after_rewrite"]:
        print("== Still refused after the rewrite (not done) ==")
        for row in doc["blocked_after_rewrite"]:
            print(f"  {row['kind']} [{row['tenant'] or 'platform'}] {row['field'] or ''}")
    for note in doc["notes"]:
        print(f"NOTE: {note}")
    if not doc["rotate"]:
        print("No credential is written out.")


@_lib_parse_cache.exit_on_parse_tree_error
def main(argv: Optional[list[str]] = None) -> Any:
    import generate_alertmanager_routes as gar
    args = _parse_args(argv)
    problem = classify_path(args.config_dir, "dir", lang="en", flag="--config-dir")
    if problem is not None:
        die_caller_error(f"ERROR: {problem.message}")
    if args.write and args.manifests_out is None:
        die_caller_error("ERROR: --write needs --manifests-out DIR: the rewrite takes the "
                         "values out of conf.d, and the Secrets that hold them are written "
                         "there; nothing was written")
    try:
        policy = gar.resolve_policy(args.policy)
    except gar.PolicyInputError as exc:
        die_caller_error(str(exc))
    reader = _lib_parse_cache.reader_for(args.config_dir)
    found = raw_receivers(args.config_dir, reader)
    if found.unreadable:
        die_caller_error("ERROR: these files cannot be read, so the credentials they "
                         "write cannot be judged: " + "; ".join(
                             f"{_lib_io.safe_label(f)} ({_lib_io.safe_label(why)})"
                             for f, why in found.unreadable))
    items = _judge(found.receivers)
    if args.manifests_out is not None:
        names = [f"{_object_name(t)}.yaml"
                 for t in sorted({it.tenant for it in items if it.entry is not None})]
        conf_files = sorted({os.path.join(args.config_dir, r.file) for r in found.receivers})
        _check_manifests_dir(args.manifests_out, args.config_dir, conf_files, names)

    plan = _plan(args.config_dir, reader, items, found.receivers) if items else \
        Plan("", {}, {}, set(), [], [])
    items = plan.items
    blocked = (_still_refused(args.config_dir, plan.rewritten, policy)
               if plan.rewritten else [])
    manifests: list[dict] = []
    if args.manifests_out is not None and any(it.entry for it in items):
        manifests = _write_manifests(args.manifests_out, found.receivers, items)
    else:
        by_tenant: dict[str, list[str]] = {}
        for it in items:
            if it.entry is not None:
                by_tenant.setdefault(it.tenant, []).append(it.entry)
        manifests = [{"name": _object_name(t), "tenant": t, "path": None,
                      "entries": sorted(by_tenant[t])} for t in sorted(by_tenant)]
    written = False
    if args.write and plan.rewritten:
        entries = []
        for f in sorted(plan.rewritten):
            node = reader.node(os.path.join(args.config_dir, f))
            rewrite = {it.path: _ref_node(it.entry) for it in items
                       if it.file == f and it.entry is not None}
            entries.append(_lib_confedit.edit_entry(f, _ops(node, rewrite)))
        _lib_confedit.write_all_or_exit(args.config_dir, entries, tool=TOOL)
        written = True
    if blocked:
        plan.notes.append("the rewrite is not complete: the generator would still refuse "
                          "the references listed under blocked_after_rewrite")
    doc = {
        "schema": SCHEMA,
        "config_dir": args.config_dir,
        "written": written,
        "diff": plan.diff,
        "manifests": manifests,
        "decisions": [{**_row(it, plan), "reason": it.reason} for it in items
                      if it.entry is None],
        "rotate": [_row(it, plan) for it in items],
        "blocked_after_rewrite": blocked,
        "notes": plan.notes,
    }
    status = EXIT_VIOLATION if items else EXIT_OK
    if args.json_output:
        return Report(doc, status)
    _print_text(doc)
    return status


if __name__ == "__main__":
    _lib_exitcodes.run(main)
