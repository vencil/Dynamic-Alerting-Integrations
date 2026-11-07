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
   `da-<tenant>` per tenant (stringData), each a file directly in DIR, opened
   O_NOFOLLOW, mode 0600 from creation. A manifest inside a git worktree,
   in the conf.d, or whose path is already a symlink, a non-regular file or
   a file with another link is refused before anything is written.
3. The rotation list: file, line, field and tenant of every credential that
   was written out (each was in the repository: rotate it — see
   docs/internal/secret-leak-remediation-sop.md). Never the value.

Left for a person (listed with the reason): credentials in platform places,
in places no plane reads, a URL with userinfo, the four proxy credentials
(the platform sets HTTPS_PROXY on Alertmanager from a Secret and the tenant
writes `proxy_from_environment: true`), an invalid tenant id, a key that
cannot be derived or is taken, and a file whose rewritten text would still
hold a value it moves out (an anchor or merge key sharing it, a comment).
The rewritten tree is then judged by the generator (`--policy`): Secret
references it would still refuse, and tenants the rewrite would drop, are
listed, the run does not count as done, and `--write` writes nothing.

⛔ No credential value reaches stdout, stderr or the JSON report: values go
only into the files under --manifests-out (`_write_manifest`). Every value
read at a credential place is watched (`_Watch`), and no text leaves this
tool before it is checked against them: a diff that would show one is
withheld, any other line or string that would is replaced whole.

Exit: 0 nothing written out and every config file read; 1 found, or a file
no plane reads (the report is the answer); 2 usage / I/O error, no usable
da-guard, a file that cannot be read, a refused --manifests-out, a failed
write.

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
import re
import stat
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
from _lib_validation import detect_cli_lang, is_valid_tenant_id  # noqa: E402
from _lib_validation import (  # noqa: E402
    CREDENTIAL_REF_SITES,
    NEEDS_REFERENCE,
    PROXY_CREDENTIAL,
    URL_USERINFO,
    find_credential_refs,
    plaintext_credential_findings,
    ref_path_text,
    ref_value_at,
    secret_ref_problem,
    value_paths_to_mask,
)
from _grar_merge import FINDING_KINDS, OWN_NAME_PREFIX  # noqa: E402
from _grar_parse import RawReceiver, raw_receivers  # noqa: E402

TOOL = "migrate-credentials"
SCHEMA = "da-tools.migrate-credentials/v1"
#: How a credential value reads in the diff.
MASK = "<redacted:{}>"
#: A one-line stand-in for a value whose line is looked up (`_plan`).
_LOCATE = "@@da-migrate-locate-{}@@"
#: What replaces a whole string / line that would show a watched value.
WITHHELD = "<withheld: it would show a credential value>"

#: Why a finding is left for a person (``Item.reason``).
REASONS = {
    "tenant_id_invalid": "the tenant id is not a valid one (ADR-035): the generator renders "
                         "nothing for it; fix the id, then run again",
    "unread_place": "written where no plane reads it: delete it (the value is in the "
                    "repository all the same: rotate it)",
    "platform_place": "a platform place every tenant that inherits it receives: a "
                      "shared Secret the deployment policy lists ({secret: {shared: "
                      "<alias>, key: <key>}}), or {secret: {key: <key>}} with the key in "
                      "each such tenant's da-<tenant>",
    "platform_enforced": "the platform's _routing_enforced (no tenant owns it): a shared "
                         "Secret the deployment policy lists ({secret: {shared: <alias>, "
                         "key: <key>}})",
    URL_USERINFO: "the URL carries userinfo: put the whole URL in the tenant's Secret "
                  "and write {secret: {key: <key>}}, or drop the userinfo",
    "userinfo_platform": "the URL carries userinfo, in a platform place: put the whole URL "
                         "in a shared Secret the deployment policy lists and write "
                         "{secret: {shared: <alias>, key: <key>}}, or drop the userinfo",
    PROXY_CREDENTIAL: "a proxy credential no receiver may write: drop it — the platform "
                      "gives Alertmanager HTTPS_PROXY from a Secret, and the receiver "
                      "writes http_config.proxy_from_environment: true",
    "not_a_string": "the value is not a string: write it as a string first",
    "key_invalid": "no Secret data key can be derived from this path "
                   "([-._a-zA-Z0-9]+, at most 253): choose one and rewrite it by hand",
    "key_taken": "the derived Secret data key is taken in this tenant: choose another "
                 "and rewrite it by hand",
    "list_not_rewritable": "the list holding it has a value `da-guard write` cannot write "
                           "back as it was read (a number not in canonical decimal, a tag "
                           "it does not carry), and the edit writes a list whole: respell "
                           "that value, or rewrite this one by hand",
    "rewrite_refused": "`da-guard write` refused the rewrite of this file",
    "value_remains_in_file": "the rewritten file would still hold a value it moves out (an "
                             "anchor or merge key sharing it, a comment, another field): "
                             "nothing in this file is rewritten; remove the other "
                             "occurrence by hand, then run again",
    "diff_withheld": "the diff of this file would show a credential value (through a merge "
                     "key, a comment or another field holding it), so it is not shown; "
                     "review the rewrite with --write on a copy",
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


# ── R1: the one output check ────────────────────────────────────────────

class _Watch:
    """The values read at credential places this run (every value
    ``value_paths_to_mask`` names, whatever its policy), and THE check every
    text this tool emits goes through: a text holding one of them as a
    substring is withheld whole, never edited around the value."""

    def __init__(self) -> None:
        self._held: list[str] = []

    def load(self, raws: list[RawReceiver]) -> None:
        seen = set(self._held)
        for r in raws:
            for p in value_paths_to_mask(r.receiver):
                v = ref_value_at(r.receiver, p)
                text = (v if isinstance(v, str) else
                        str(v) if isinstance(v, (int, float)) and not isinstance(v, bool)
                        else "")
                if text and text not in seen:
                    seen.add(text)
                    self._held.append(text)

    def hits(self, text: str) -> bool:
        return any(v in text for v in self._held)

    def line(self, text: str) -> str:
        """*text*, or ``WITHHELD`` when it holds a watched value."""
        return WITHHELD if self.hits(text) else text

    def doc(self, value: Any) -> tuple[Any, bool]:
        """*value* (a JSON-able document) with every string that holds a
        watched value replaced whole by ``WITHHELD``; and whether any was."""
        if isinstance(value, str):
            return (WITHHELD, True) if self.hits(value) else (value, False)
        if isinstance(value, dict):
            out, hit = {}, False
            for k, v in value.items():
                key, kh = self.doc(k) if isinstance(k, str) else (k, False)
                val, vh = self.doc(v)
                out[key] = val
                hit = hit or kh or vh
            return out, hit
        if isinstance(value, list):
            items = [self.doc(v) for v in value]
            return [v for v, _h in items], any(h for _v, h in items)
        return value, False


_WATCH = _Watch()


def _die(message: str) -> None:
    """``die_caller_error`` through the output check."""
    die_caller_error("\n".join(_WATCH.line(m) for m in message.split("\n")))


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


def _object_name(tenant: str) -> str:
    """The name of *tenant*'s own Secret, ``da-<tenant>`` — what
    ``_grar_merge.own_secret_name`` gives (pinned equal by
    tests/ops/test_migrate_credentials.py), spelled here so that the name this
    report prints is not read as the value of a function named for secrets."""
    return f"{OWN_NAME_PREFIX}{tenant}"


# ── R3: where the manifests go ──────────────────────────────────────────

def _manifest_path(out: str, tenant: str) -> str:
    """The manifest of *tenant*: ``da-<tenant>.yaml`` directly in *out*.
    Refused (rc 2) for a tenant id ``is_valid_tenant_id`` refuses, or a
    path whose directory is not *out* itself."""
    if not is_valid_tenant_id(tenant):
        _die(f"ERROR: --manifests-out: tenant {_lib_io.safe_label(tenant)!r} is not a valid "
             "tenant id, so no manifest is named after it; nothing was written")
    path = os.path.join(out, f"{_object_name(tenant)}.yaml")
    if os.path.realpath(os.path.dirname(path)) != os.path.realpath(out):
        _die(f"ERROR: --manifests-out: {_lib_io.safe_label(path)} is not directly in "
             f"{_lib_io.safe_label(out)}; nothing was written")
    return path


def _check_manifest_targets(out: str, paths: list[str], config_dir: str,
                            config_files: list[str]) -> None:
    """Refuse (rc 2, before anything is written) a destination the Secrets
    must not land in: DIR not a directory, the conf.d or inside it, or
    inside a git worktree; a manifest whose real path is inside a git
    worktree, or whose path is already a symlink, not a regular file, a file
    with another hard link, or a conf.d file."""
    if os.path.lexists(out) and not os.path.isdir(out):
        _die(f"ERROR: --manifests-out {_lib_io.safe_label(out)} is not a directory; nothing "
             "was written")
    real_out = os.path.realpath(out)
    real_conf = os.path.realpath(config_dir)
    if real_out == real_conf or real_out.startswith(real_conf.rstrip(os.sep) + os.sep):
        _die("ERROR: --manifests-out is the conf.d or inside it; nothing was written")
    for where in [out, *paths]:
        worktree = _git_worktree_of(where)
        if worktree is not None:
            _die(f"ERROR: --manifests-out {_lib_io.safe_label(where)} is inside the git "
                 f"worktree {_lib_io.safe_label(worktree)}: Secrets must not be written "
                 "where they can be committed; nothing was written")
    for path in paths:
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            continue
        except OSError as exc:
            _die(f"ERROR: --manifests-out {_lib_io.safe_label(path)}: {exc.strerror}; "
                 "nothing was written")
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode) or st.st_nlink > 1:
            _die(f"ERROR: --manifests-out {_lib_io.safe_label(path)} is already there as a "
                 "symlink, a file that is not regular, or a file with another link; "
                 "remove it first; nothing was written")
        for conf in config_files:
            if _lib_io.same_file(path, conf):
                _die(f"ERROR: --manifests-out {_lib_io.safe_label(path)} is the conf.d file "
                     f"{_lib_io.safe_label(conf)}; nothing was written")


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


def _reason(r: RawReceiver, category: str, value: Any) -> Optional[str]:
    """Why the finding of *category* in receiver *r* is left for a person,
    or None when the tool rewrites it."""
    if not r.tenant_valid:
        return "tenant_id_invalid"
    if not r.read:
        return "unread_place"
    if category == URL_USERINFO:
        return "userinfo_platform" if r.tenant is None else URL_USERINFO
    if category != NEEDS_REFERENCE:
        return category
    if r.tenant is None:
        return "platform_enforced" if r.place == "routing_enforced" else "platform_place"
    if not isinstance(value, str):
        return "not_a_string"
    return None


def _judge(raws: list[RawReceiver]) -> list[Item]:
    """Every credential written out, rewritable or with the reason it is not."""
    taken = _existing_entries(raws)
    items: list[Item] = []
    for r in raws:
        for f in plaintext_credential_findings(r.receiver):
            base = Item(r.file, r.path + f.path, r.path, ref_path_text(f.path), r.tenant,
                        f.category)
            why = _reason(r, f.category, ref_value_at(r.receiver, f.path))
            if why is not None:
                items.append(base._replace(reason=why))
                continue
            entry = _derived_entry(r, f.path)
            items.append(base._replace(entry=entry) if entry is not None
                         else base._replace(reason="key_invalid"))
    counts: dict[tuple, int] = {}
    for it in items:
        if it.entry is not None:
            counts[(it.tenant, it.entry)] = counts.get((it.tenant, it.entry), 0) + 1
    return [it._replace(entry=None, reason="key_taken")
            if it.entry is not None and (counts[(it.tenant, it.entry)] > 1
                                         or it.entry in taken.get(it.tenant, ()))
            else it for it in items]


def _drop(items: list[Item], reason: str, *, file: Optional[str] = None,
          under: Optional[tuple] = None) -> list[Item]:
    """*items* with each rewritable one of *file* (or below path *under*)
    left for a person, with *reason*."""
    return [it._replace(entry=None, reason=reason)
            if it.entry is not None and (file is None or it.file == file)
            and (under is None or it.path[:len(under)] == under) else it
            for it in items]


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


def _key(path: tuple) -> list[str]:
    return [str(x) for x in path]


def _ops(file_node: Any, replacements: dict[tuple, Any]) -> tuple[list[dict], set[tuple]]:
    """The `da-guard write` operations that put each tagged node of
    *replacements* at its path of *file_node* (the file's parse-tree nodes),
    and the list prefixes that cannot be written. The edit addresses
    mappings only: a path through a list sets that list whole, rebuilt from
    *file_node* by ``node_value`` — one call per list, so only a list holding
    a value that cannot be written back as read is left out (R7)."""
    direct: dict[tuple, Any] = {}
    lists: dict[tuple, list[tuple[tuple, Any]]] = {}
    for path, value in replacements.items():
        cut = next((i for i, p in enumerate(path) if isinstance(p, int)), None)
        if cut is None:
            direct[path] = value
        else:
            lists.setdefault(path[:cut], []).append((path[cut:], value))
    ops = [_lib_confedit.set_op(list(p), _lib_confedit.node_value(v))
           for p, v in sorted(direct.items(), key=lambda kv: _key(kv[0]))]
    failed: set[tuple] = set()
    for prefix in sorted(lists, key=_key):
        whole = copy.deepcopy(_node_at(file_node, prefix))
        for rel, value in lists[prefix]:
            _put(whole, rel, value)
        try:
            ops.append(_lib_confedit.set_op(list(prefix), _lib_confedit.node_value(whole)))
        except ValueError:
            failed.add(prefix)
    return ops, failed


def _list_prefix(path: tuple) -> Optional[tuple]:
    cut = next((i for i, x in enumerate(path) if isinstance(x, int)), None)
    return None if cut is None else path[:cut]


def _dry_run(config_dir: str, edits: dict[str, list[dict]]) -> dict[str, str]:
    """{file: the text a dry run of `da-guard write` gives it} for *edits*, or
    raises ``_lib_confedit.EditsRefused`` / ``EditCallerError`` /
    ``ParseTreeError`` (nothing is written either way)."""
    batch = [(rel, ops) for rel, ops in sorted(edits.items()) if ops]
    results = _lib_confedit.edit_all(config_dir, batch, dry_run=True)
    return {r["path"]: r.get("content") or "" for r in results}


_EDIT_ERRORS = (_lib_confedit.EditsRefused, _lib_confedit.EditCallerError, ParseTreeError)


def _refusal_text(exc: BaseException) -> str:
    """Why a run of `da-guard write` failed, without da-guard's message
    (which may quote the file): the refusal reasons, or the exception's class."""
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
    withheld: list[str]           # files whose diff is not shown (R1)


def _read_text(path: str) -> str:
    with open(path, encoding="utf-8", newline="") as fh:
        return fh.read()


def _lines(config_dir: str, nodes: dict, by_file: dict, notes: list[str]
           ) -> tuple[dict[tuple, int], set]:
    """Each finding's line: its value (or the list holding it) set to a
    one-line marker in a dry run, mapped back to the file."""
    locate: dict[str, list[dict]] = {}
    marks: dict[str, dict[str, Any]] = {}
    exact: set = set()
    for f, its in by_file.items():
        reps: dict[tuple, Any] = {}
        mk: dict[str, Any] = {}
        for it in its:
            where = _list_prefix(it.path) or it.path
            if where == it.path:
                exact.add((f, it.path))
            if where not in reps:
                m = _LOCATE.format(len(mk))
                reps[where] = _str_node(m)
                mk[m] = where
        locate[f], _failed = _ops(nodes[f], reps)   # markers are strings: no list fails
        marks[f] = mk
    lines: dict[tuple, int] = {}
    try:
        located = _dry_run(config_dir, locate)
    except _EDIT_ERRORS as exc:
        notes.append(f"line numbers unavailable: {_refusal_text(exc)}")
        return lines, exact
    for f, text in located.items():
        found = _marker_lines(_read_text(os.path.join(config_dir, f)), text, marks[f])
        for it in by_file[f]:
            where = _list_prefix(it.path) or it.path
            if where in found:
                lines[(f, it.path)] = found[where]
    return lines, exact


def _plan(config_dir: str, reader: Any, items: list[Item], raws: list[RawReceiver]) -> Plan:
    notes: list[str] = []
    by_file: dict[str, list[Item]] = {}
    for it in items:
        by_file.setdefault(it.file, []).append(it)
    nodes = {f: reader.node(os.path.join(config_dir, f)) for f in by_file}
    lines, exact = _lines(config_dir, nodes, by_file, notes)
    receivers = {(r.file, r.path): r.receiver for r in raws}

    # The rewrite, file by file; a list that cannot be written back leaves
    # only the values in it to a person (R7).
    final = list(items)
    plain: dict[str, list[dict]] = {}
    for f in by_file:
        rewrite = {it.path: _ref_node(it.entry) for it in final
                   if it.file == f and it.entry is not None}
        if not rewrite:
            continue
        ops, failed = _ops(nodes[f], rewrite)
        for prefix in failed:
            final = _drop(final, "list_not_rewritable", file=f, under=prefix)
        rewrite = {p: v for p, v in rewrite.items() if _list_prefix(p) not in failed}
        if rewrite:
            plain[f] = ops
    rewritten: dict[str, str] = {}
    if plain:
        try:
            rewritten = _dry_run(config_dir, plain)
        except _EDIT_ERRORS as exc:
            notes.append(f"the rewrite was refused: {_refusal_text(exc)}")
            for f in plain:
                final = _drop(final, "rewrite_refused", file=f)
    # R2: a value moved out of a file must not still be in its text.
    for f in sorted(rewritten):
        moved = [ref_value_at(receivers[(it.file, it.receiver_path)],
                              it.path[len(it.receiver_path):])
                 for it in final if it.file == f and it.entry is not None]
        if any(isinstance(v, str) and v and v in rewritten[f] for v in moved):
            final = _drop(final, "value_remains_in_file", file=f)
            del rewritten[f]
    for f in sorted(rewritten):
        for prefix in sorted({_list_prefix(it.path) for it in final
                              if it.file == f and it.entry is not None} - {None}, key=_key):
            notes.append(f"{f}: {'.'.join(str(x) for x in prefix)} is a list written whole: "
                         "the comments inside it are not kept")

    # The diff of what is left, every watched value masked; a file whose
    # diff would still show one is withheld (R1).
    masks_by_file: dict[str, list[tuple]] = {}
    for r in raws:
        if r.file in rewritten:
            masks_by_file.setdefault(r.file, []).extend(
                r.path + p for p in value_paths_to_mask(r.receiver))
    before: dict[str, list[dict]] = {}
    after: dict[str, list[dict]] = {}
    for f in rewritten:
        masked = {p: _str_node(MASK.format(i))
                  for i, p in enumerate(sorted(set(masks_by_file.get(f, [])), key=_key))}
        rewrite = {it.path: _ref_node(it.entry) for it in final
                   if it.file == f and it.entry is not None}
        before[f], _f1 = _ops(nodes[f], masked)
        after[f], _f2 = _ops(nodes[f], {**masked, **rewrite})
    diff, withheld = "", []
    if rewritten:
        try:
            old = _dry_run(config_dir, before)
            new = _dry_run(config_dir, after)
        except _EDIT_ERRORS as exc:
            notes.append(f"the diff could not be shown: {_refusal_text(exc)}")
            old = new = {}
        parts = []
        for f in sorted(new):
            one = "".join(difflib.unified_diff(old.get(f, "").splitlines(True),
                                               new[f].splitlines(True),
                                               fromfile=f"a/{f}", tofile=f"b/{f}"))
            if _WATCH.hits(one):
                withheld.append(f)
                notes.append(f"{f}: the diff is not shown: it would show a credential value "
                             "(a merge key, a comment or another field holds it)")
            else:
                parts.append(one)
        diff = "".join(parts)
    return Plan(diff, rewritten, lines, exact, notes, final, withheld)


# ── the rewritten tree, judged by the generator ─────────────────────────

#: R9: what a blocked row may say. Kinds: the generator's Secret-reference
#: finding kinds (FINDING_KINDS) and this tool's own two; fields: the
#: receiver paths CREDENTIAL_REF_SITES names (each site, its destinations
#: and proxies, follow_redirects) under each routing slot.
TREE_REFUSED = "tree_refused"
TENANT_DROPPED = "tenant_dropped"
OTHER = "other"
_KINDS = frozenset({k for k in FINDING_KINDS if k.startswith("secret_ref_")}
                   | {TREE_REFUSED, TENANT_DROPPED})


def _field_templates() -> tuple[tuple[str, re.Pattern[str]], ...]:
    def text(path: tuple[str, ...]) -> str:
        return ref_path_text(path).replace(".[]", "[i]")
    tails = {("http_config", "follow_redirects")}
    for s in CREDENTIAL_REF_SITES:
        tails |= {s.path, *s.destinations, *s.proxies}
    out = []
    for slot in ("receiver", "overrides[i].receiver", "routes[i].receiver",
                 "_routing_enforced.receiver"):
        for tail in sorted(tails):
            t = f"{slot}.{text(tail)}"
            rx = re.escape(t).replace(r"\[i\]", r"\[[0-9]+\]").replace(r"\*", r"[^.\[\]]+")
            out.append((t, re.compile(rx)))
    return tuple(out)


_FIELDS = _field_templates()


def _vocab_kind(kind: object) -> str:
    return next((k for k in sorted(_KINDS) if k == kind), OTHER)


def _vocab_field(field: object) -> Optional[str]:
    if field is None:
        return None
    return next((t for t, rx in _FIELDS if isinstance(field, str) and rx.fullmatch(field)),
                OTHER)


def _vocab_tenant(tenant: object) -> Optional[str]:
    return tenant if isinstance(tenant, str) and is_valid_tenant_id(tenant) else None


def _judge_tree(root: str, policy: Any) -> Optional[tuple[set, list]]:
    """(tenants with routing, warnings) of the generator over *root*, or
    None when it refuses the tree. Its own output is not shown."""
    import generate_alertmanager_routes as gar
    sink = io.StringIO()
    try:
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            tree = gar.load_tenant_tree(root)
            _r, _v, warnings = gar.generate_routes(
                tree.routing_configs, allowed_domains=policy.allowed_domains,
                enforced_routing=tree.enforced_routing, tenants=tree.dedup_configs,
                secrets=policy.secret_binding())
    except SystemExit:
        return None
    finally:
        _lib_parse_cache.forget(root)
    return set(tree.routing_configs), warnings


def _still_refused(config_dir: str, rewritten: dict[str, str], policy: Any) -> list[dict]:
    """What the rewrite leaves refused, judged by the generator on a copy of
    the tree with *rewritten* in place, under *policy*: its Secret-reference
    findings, a tenant with routing before the rewrite and none after, a
    tree it refuses after and not before. Each row in the closed vocabulary
    (R9): never the generator's line, which may quote a written value."""
    before = _judge_tree(config_dir, policy)
    with tempfile.TemporaryDirectory(prefix="da-migrate-credentials-") as tmp:
        copy_root = os.path.join(tmp, "conf.d")
        shutil.copytree(config_dir, copy_root, symlinks=True)
        for rel, text in rewritten.items():
            target = os.path.join(copy_root, rel)
            with open(target, "w", encoding="utf-8", newline="") as fh:
                fh.write(text)
            os.chmod(target, 0o600)
        after = _judge_tree(copy_root, policy)
    if after is None:
        return [] if before is None else [{"kind": TREE_REFUSED, "tenant": None, "field": None}]
    tenants_after, warnings = after
    out: list[dict] = []
    for t in sorted((before[0] if before else set()) - tenants_after):
        out.append({"kind": TENANT_DROPPED, "tenant": _vocab_tenant(t), "field": None})
    for w in warnings:
        kind = getattr(w, "kind", "")
        if isinstance(kind, str) and kind.startswith("secret_ref_"):
            row = {"kind": _vocab_kind(kind), "tenant": _vocab_tenant(getattr(w, "tenant", None)),
                   "field": _vocab_field(getattr(w, "field", None))}
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
    """Write *text* to *path*: created 0600, never through a symlink
    (O_NOFOLLOW)."""
    _lib_io.write_operator_output_or_die(path, text, flag="--manifests-out", mode=0o600,
                                         nofollow=True)


def _manifest_plan(raws: list[RawReceiver], items: list[Item]
                   ) -> dict[str, list[tuple[str, str]]]:
    receivers = {(r.file, r.path): r.receiver for r in raws}
    by_tenant: dict[str, list[tuple[str, str]]] = {}
    for it in items:
        if it.entry is not None:
            value = ref_value_at(receivers[(it.file, it.receiver_path)],
                                 it.path[len(it.receiver_path):])
            by_tenant.setdefault(it.tenant, []).append((it.entry, value))
    return by_tenant


def _write_manifests(out: str, by_tenant: dict, paths: dict[str, str]) -> list[dict]:
    try:
        os.makedirs(out, mode=0o700, exist_ok=True)
    except OSError as exc:
        _die(f"ERROR: cannot create --manifests-out {_lib_io.safe_label(out)}: "
             f"{exc.strerror}; nothing was written")
    written = []
    for tenant in sorted(by_tenant):
        name = _object_name(tenant)
        _write_manifest(paths[tenant], _manifest_text(name, sorted(by_tenant[tenant])))
        written.append({"name": name, "tenant": tenant, "path": paths[tenant],
                        "entries": sorted(e for e, _v in by_tenant[tenant])})
    return written


def _write_confd(config_dir: str, entries: list[dict], manifests: list[dict]) -> None:
    """The rewrite through `da-guard write`, all or nothing. On failure, rc 2
    naming what is already on disk: the manifests written, and whether
    conf.d is unchanged or changed in part."""
    shown = ", ".join(_lib_io.safe_label(m["path"]) for m in manifests) or "(none)"
    try:
        _lib_confedit.write_all(config_dir, entries)
    except _lib_confedit.PartialWrite as exc:
        left = [*exc.written, *exc.maybe_written]
        _die(f"ERROR: the conf.d write stopped part-way ({_refusal_text(exc.cause)}): "
             f"changed or maybe changed: {', '.join(left) or '(none)'}, not rolled back. "
             f"The Secret manifests were written: {shown}")
    except _EDIT_ERRORS as exc:
        _die(f"ERROR: `da-guard write` did not take the rewrite ({_refusal_text(exc)}): "
             f"conf.d was not changed. The Secret manifests were written: {shown}")


# ── the report ──────────────────────────────────────────────────────────

def _row(it: Item, plan: Plan) -> dict:
    return {"file": it.file, "line": plan.lines.get((it.file, it.path)),
            "line_is_field": (it.file, it.path) in plan.exact,
            "field": it.field, "location": ref_path_text(it.path),
            "tenant": it.tenant, "category": it.category}


def _print_text(doc: dict) -> None:
    def say(text: str) -> None:
        for line in text.split("\n"):
            print(_WATCH.line(line))

    def where(row: dict) -> str:
        line = f":{row['line']}" if row["line"] is not None else ""
        held = "" if row["line_is_field"] or row["line"] is None else " (in the list there)"
        return f"{row['file']}{line} {row['location'] or ''}{held}"

    if doc["diff"]:
        say("== Rewrite (credential values read <redacted:N>) ==")
        say(doc["diff"].rstrip("\n"))
        say("(applied)" if doc["written"] else "(not applied: add --write)")
    if doc["manifests"]:
        say("== Kubernetes Secrets ==")
        for m in doc["manifests"]:
            dest = m["path"] or "not written"
            say(f"  {m['name']} ({', '.join(m['entries'])}) -> {dest}")
    if doc["decisions"]:
        say("== Left for a person ==")
        for row in doc["decisions"]:
            say(f"  {where(row)} [{row['tenant'] or 'platform'}] {REASONS[row['reason']]}")
    if doc["rotate"]:
        say("== Rotate (written out in the repository; "
            "docs/internal/secret-leak-remediation-sop.md) ==")
        for row in doc["rotate"]:
            say(f"  {where(row)} [{row['tenant'] or 'platform'}] {row['category']}")
    if doc["blocked_after_rewrite"]:
        say("== Still refused after the rewrite (not done) ==")
        for row in doc["blocked_after_rewrite"]:
            say(f"  {row['kind']} [{row['tenant'] or 'platform'}] {row['field'] or ''}")
    if doc["unread"]:
        say("== Not read (no plane reads these files; what they write is not judged) ==")
        for row in doc["unread"]:
            say(f"  {row['file']} ({row['reason']})")
    for note in doc["notes"]:
        say(f"NOTE: {note}")
    if not doc["rotate"] and not doc["unread"]:
        say("No credential is written out.")


def _run(argv: Optional[list[str]]) -> Any:
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
    _WATCH.load(found.receivers)
    if found.unreadable:
        _die("ERROR: these files cannot be read, so the credentials they write cannot be "
             "judged: " + "; ".join(f"{_lib_io.safe_label(f)} ({_lib_io.safe_label(why)})"
                                    for f, why in found.unreadable))
    items = _judge(found.receivers)
    plan = (_plan(args.config_dir, reader, items, found.receivers) if items else
            Plan("", {}, {}, set(), [], [], []))
    items = plan.items
    blocked = (_still_refused(args.config_dir, plan.rewritten, policy)
               if plan.rewritten else [])
    by_tenant = _manifest_plan(found.receivers, items)
    manifests = [{"name": _object_name(t), "tenant": t, "path": None,
                  "entries": sorted(e for e, _v in by_tenant[t])} for t in sorted(by_tenant)]
    written = False
    if blocked and args.write:
        plan.notes.append("--write wrote nothing (no manifest, no conf.d file): the rewrite "
                          "would make the generator refuse what is listed under "
                          "blocked_after_rewrite; fix the policy or the receivers' "
                          "http_config first, then run again")
    elif args.manifests_out is not None and by_tenant:
        paths = {t: _manifest_path(args.manifests_out, t) for t in by_tenant}
        conf_files = sorted({os.path.join(args.config_dir, r.file) for r in found.receivers})
        _check_manifest_targets(args.manifests_out, sorted(paths.values()), args.config_dir,
                                conf_files)
        manifests = _write_manifests(args.manifests_out, by_tenant, paths)
        if args.write and plan.rewritten:
            entries = []
            for f in sorted(plan.rewritten):
                node = reader.node(os.path.join(args.config_dir, f))
                rewrite = {it.path: _ref_node(it.entry) for it in items
                           if it.file == f and it.entry is not None}
                ops, _failed = _ops(node, rewrite)   # the lists that failed are out of items
                entries.append(_lib_confedit.edit_entry(f, ops))
            _write_confd(args.config_dir, entries, manifests)
            written = True
    if blocked:
        plan.notes.append("the rewrite is not complete: the generator would still refuse "
                          "what is listed under blocked_after_rewrite")
    decisions = [{**_row(it, plan), "reason": it.reason} for it in items if it.entry is None]
    decisions += [{"file": f, "line": None, "line_is_field": False, "field": None,
                   "location": None, "tenant": None, "category": None,
                   "reason": "diff_withheld"} for f in plan.withheld]
    doc = {
        "schema": SCHEMA,
        "config_dir": args.config_dir,
        "written": written,
        "diff": plan.diff,
        "manifests": manifests,
        "decisions": decisions,
        "rotate": [_row(it, plan) for it in items],
        "blocked_after_rewrite": blocked,
        "unread": [{"file": f, "reason": why} for f, why in found.unread],
        "notes": plan.notes,
    }
    doc, hit = _WATCH.doc(doc)
    if hit:
        doc["notes"].append(f"some text of this report is replaced by {WITHHELD!r}")
    status = EXIT_VIOLATION if items or found.unread else EXIT_OK
    if args.json_output:
        return Report(doc, status)
    _print_text(doc)
    return status


@_lib_parse_cache.exit_on_parse_tree_error
def main(argv: Optional[list[str]] = None) -> Any:
    try:
        return _run(argv)
    except Exception as exc:
        # R1: an exception's text passes the output check too. One that would
        # show a watched value goes on as a RuntimeError with no text of it
        # (the shared crash line, rc 2); any other goes on as it is.
        if not isinstance(exc, ParseTreeError) and _WATCH.hits(f"{exc!s} {exc!r}"):
            raise RuntimeError(WITHHELD) from None
        raise


if __name__ == "__main__":
    _lib_exitcodes.run(main)
