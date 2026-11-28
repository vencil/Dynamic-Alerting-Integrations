#!/usr/bin/env python3
"""check_retired_keys_not_taught.py — a retired threshold key is not taught anywhere (#1200).

A key listed under ``retired_keys:`` in ``rule-packs/threshold-registry.yaml``
is refused in every layer of a tenant config (the shared key verdict: unread / error /
retired). A doc, sample, script or portal file that still shows it as a key
to write teaches a config the platform rejects. This guard reads the retired
keys from the registry — it holds no list of keys of its own — and fails on
any tracked file that names one as a word.

Lint class (lint-policy §2): (a) bounded enumeration. The set of bad words is
the registry's ``retired_keys`` plus each one's derived ``<key>_critical``
(retired with its base, as the key verdict judges it). Scope: the full tree,
every run.

What is scanned: every file ``git ls-files`` lists, except (``exempt_reason``)

* the registry itself, ``CHANGELOG*.md`` and ``changelog.d/`` — their job is
  to talk about retirements;
* tests — a directory named ``tests`` / ``test`` / ``testdata`` /
  ``__tests__`` anywhere on the path, or a test file by name (``test_*.py``,
  ``*_test.py``, ``*_test.go``, ``*.test.*``, ``*.spec.*``);
* symlinks, files with a NUL byte, and tracked paths that are not a regular
  file in the work tree (counted in the summary, never a finding).

Text generated from the registry is recognised by rendering it again, not by
a list of paths: a ``--regen`` block (``_registry_lib.surface_specs``), the
generated RULE_PACKS module or the generated Go table of ``retired_keys`` that
is byte-equal to a fresh render is skipped,
and in a ``.json`` file a whole JSON string that is verbatim a string value
of the registry's live sections (a key's ``desc`` projected into
``platform-data.json`` and the portal fallback) is skipped. A stale or
edited copy is not recognised, so it is scanned.

Word boundary: a key matches where the character before and the character
after are not ``[A-Za-z0-9_]``. So ``tenant:alert_threshold:<key>``,
``<key>:``, ```<key>``` and ``<key>.`` are mentions; ``<key>_total``,
``<key>s``, ``x<key>`` and a longer key or raw metric name that merely
contains the letters are not. Matching is case-sensitive and literal. NOT
seen: the key re-spelled (an alert name in CamelCase, ``metric="…"`` label
values of ``user_threshold``, a key split across lines or string pieces),
and a retired key that is also the whole name of something else would be
flagged there too.

Escape, at the spot: a line that explains a retirement carries
``retired-key-mention: <key>[, <key>…]`` (in whatever comment syntax the
file has; ``<!-- … -->`` in Markdown). It covers that line and the next one,
and only for the keys it names. A marker that names a key which is not
retired, or covers no mention of a key it names, is an error (a stale
exemption), and so is a marker that names no key at all (nothing but the end
of the line or a comment closer after the colon; ``<key>`` written as a
placeholder in prose about the marker is not one). There is no central
allowlist.

Fail-closed (exit 2, never 0): the registry cannot be read; ``retired_keys``
is missing, is not a mapping, or does not hold as many entries as the file's
text shows under ``retired_keys:``; no file was scanned.

Exit codes (_lib_exitcodes): 0 clean (or findings without ``--ci``) /
1 findings with ``--ci`` / 2 the scan could not be made.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))
sys.path.insert(0, os.path.join(_THIS_DIR, "..", "ops"))
from _lib_compat import require_git_project_root, try_utf8_stdout  # noqa: E402  (#2852)
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402
from _lib_validation import i18n_text  # noqa: E402
import _registry_lib as registry_lib  # noqa: E402

_GENERATOR_ROOT = os.path.abspath(os.path.join(_THIS_DIR, "..", "..", ".."))
REGISTRY_REL = "rule-packs/threshold-registry.yaml"
RETIRED_SECTION = "retired_keys"

#: The escape marker (see the module docstring). A hyphenated word on
#: purpose: the registry gate's header-prose scan reads `snake_case:` as a
#: threshold-key claim.
MARKER = "retired-key-mention:"
_MARKER_RE = re.compile(
    re.escape(MARKER) + r"[ \t]*([A-Za-z0-9_]+(?:[ \t]*,[ \t]*[A-Za-z0-9_]+)*)")

_WORD = "A-Za-z0-9_"

_CHANGELOG_RE = re.compile(r"^(?:CHANGELOG[^/]*\.md|changelog\.d/.*)$")
_TEST_DIRS = frozenset({"tests", "test", "testdata", "__tests__"})
_TEST_FILE_RE = re.compile(
    r"^(?:test_.*\.py|.*_test\.py|.*_test\.go|.*\.(?:test|spec)\.[A-Za-z]+)$")
#: A marker with no key: only the end of the line or a comment closer follows.
_EMPTY_MARKER_RE = re.compile(re.escape(MARKER) + r"[ \t]*(?:$|-->|\*/)")


class MeasureError(Exception):
    """The scan could not be made — exit 2, never 0."""


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    key: str
    reason: str


@dataclass
class Result:
    keys: tuple = ()
    findings: list = field(default_factory=list)
    files_scanned: int = 0
    marker_exempted: int = 0
    generated_skipped: int = 0
    exempt_files: dict = field(default_factory=dict)
    #: files scanned per top-level directory ("." for the root's own files)
    scanned_roots: dict = field(default_factory=dict)


def exempt_reason(rel: str) -> Optional[str]:
    """Why the tracked path *rel* is not scanned, or None when it is."""
    if rel == REGISTRY_REL:
        return "registry"
    if _CHANGELOG_RE.match(rel):
        return "changelog"
    parts = rel.split("/")
    if _TEST_DIRS.intersection(parts[:-1]) or _TEST_FILE_RE.match(parts[-1]):
        return "test"
    return None


def _section_entries_in_text(text: str) -> Optional[int]:
    """How many entries the registry's TEXT shows under ``retired_keys:``.

    None when the text has no top-level ``retired_keys:`` line, or has one
    whose inline value cannot be counted (a flow mapping with entries). An
    inline ``retired_keys: {}`` is 0. Read from the text, apart from the YAML
    loader, so a section the loader turned into nothing is noticed."""
    lines = text.split("\n")
    for i, line in enumerate(lines):
        m = re.match(rf"^{RETIRED_SECTION}:(.*)$", line)
        if not m:
            continue
        rest = m.group(1).split("#", 1)[0].strip()
        if rest:
            return 0 if rest in ("{}", "null", "~") else None
        count = 0
        for nxt in lines[i + 1:]:
            if not nxt.strip() or nxt.lstrip().startswith("#"):
                continue
            if not nxt.startswith(" "):
                break
            if re.match(r"^  [^\s#-]", nxt):
                count += 1
        return count
    return None


def load_retired_keys(registry_path: str) -> dict:
    """The registry's ``retired_keys`` mapping, or MeasureError."""
    try:
        with open(registry_path, encoding="utf-8") as fh:
            text = fh.read()
        doc = registry_lib.load_registry(registry_path)
    except Exception as exc:  # noqa: BLE001 — unreadable in any way is exit 2
        raise MeasureError(f"cannot read the threshold registry {registry_path}: "
                           f"{' '.join(str(exc).split())}") from exc
    if not isinstance(doc, dict) or RETIRED_SECTION not in doc:
        raise MeasureError(
            f"{registry_path} has no `{RETIRED_SECTION}:` section, so no key can "
            "be judged retired (write `retired_keys: {}` for none)")
    retired = doc[RETIRED_SECTION]
    in_text = _section_entries_in_text(text)
    if not isinstance(retired, dict):
        raise MeasureError(
            f"{registry_path}: `{RETIRED_SECTION}:` is not a mapping "
            f"({type(retired).__name__})")
    if in_text is None or in_text != len(retired):
        raise MeasureError(
            f"{registry_path}: `{RETIRED_SECTION}:` reads as {len(retired)} "
            f"entr{'y' if len(retired) == 1 else 'ies'} but the file's text shows "
            f"{'no section it can count' if in_text is None else in_text}")
    return {"doc": doc, "retired": retired}


def judged_keys(retired: dict) -> dict:
    """{word: retired entry it is judged by}: each key and its ``_critical``."""
    suffix = registry_lib.CRITICAL_SUFFIX
    out = {}
    for key, entry in retired.items():
        out[key] = (key, entry or {})
    for key, entry in retired.items():
        if not key.endswith(suffix):
            out.setdefault(key + suffix, (key, entry or {}))
    return out


def key_regex(words: Iterable[str]) -> "re.Pattern[str]":
    """One pattern for *words* as whole words; it matches nothing for none."""
    alt = "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))
    if not alt:
        return re.compile(r"(?!)")
    return re.compile(rf"(?<![{_WORD}])(?:{alt})(?![{_WORD}])")


def _blank(text: str, start: int, end: int) -> str:
    """*text* with [start, end) replaced by spaces, newlines kept."""
    middle = "".join(c if c == "\n" else " " for c in text[start:end])
    return text[:start] + middle + text[end:]


def _strings(node) -> Iterable[str]:
    """Every string VALUE under *node* (a mapping's keys are not values)."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for v in node.values():
            yield from _strings(v)
    elif isinstance(node, (list, tuple)):
        for v in node:
            yield from _strings(v)


class Generated:
    """Text the registry's own generators write, found by rendering it."""

    def __init__(self, doc: dict, pattern: "re.Pattern[str]"):
        # The generators name their targets by absolute path in THIS repo;
        # the scanned tree is looked up by the same relative path.
        repo_root = _GENERATOR_ROOT
        self._blocks: dict = {}
        try:
            for spec in registry_lib.surface_specs(doc):
                rel = os.path.relpath(spec["path"], repo_root).replace(os.sep, "/")
                self._blocks.setdefault(rel, []).append(spec["block"])
            module_rel = os.path.relpath(
                registry_lib.RULE_PACKS_MODULE_PATH, repo_root).replace(os.sep, "/")
            self._blocks.setdefault(module_rel, []).append(
                registry_lib.render_rule_packs_module(doc))
            go_rel = os.path.relpath(
                registry_lib.RETIRED_KEYS_GO_PATH, repo_root).replace(os.sep, "/")
            self._blocks.setdefault(go_rel, []).append(
                registry_lib.render_retired_keys_go(doc))
        except Exception as exc:  # noqa: BLE001 — a registry that cannot render
            raise MeasureError(
                "cannot render the registry's generated surfaces: "
                f"{' '.join(str(exc).split())}") from exc
        # What a generator can project: the string values of the live
        # sections. The retirement sections themselves are projected nowhere
        # a reader learns keys from, so a bare retired key is never "text".
        projected = {k: v for k, v in doc.items()
                     if k not in (RETIRED_SECTION, "deprecated_aliases")}
        self._json_strings = sorted(
            {s for s in _strings(projected) if pattern.search(s)}, key=len, reverse=True)

    def mask(self, rel: str, text: str) -> str:
        """*text* with the registry-generated parts blanked (same line count)."""
        for block in self._blocks.get(rel, ()):
            at = text.find(block)
            if at >= 0:
                text = _blank(text, at, at + len(block))
        if rel.lower().endswith(".json"):
            for s in self._json_strings:
                for ascii_only in (False, True):
                    # the whole JSON string token, quotes included: a longer
                    # string that merely contains the registry's is not it
                    needle = json.dumps(s, ensure_ascii=ascii_only)
                    at = text.find(needle)
                    while at >= 0:
                        if at == 0 or text[at - 1] != "\\":
                            text = _blank(text, at, at + len(needle))
                        at = text.find(needle, at + len(needle))
        return text


def scan_text(rel: str, text: str, judged: dict,
              pattern: "re.Pattern[str]") -> "tuple[list, int]":
    """(findings, mentions exempted by a marker) for one file's text."""
    findings: list = []
    lines = text.split("\n")
    stripped: list = []
    markers: list = []  # (lineno, key)
    for lineno, line in enumerate(lines, 1):
        for m in _MARKER_RE.finditer(line):
            for key in re.split(r"[ \t]*,[ \t]*", m.group(1)):
                markers.append((lineno, key))
        if _EMPTY_MARKER_RE.search(line):
            findings.append(Finding(
                rel, lineno, "",
                f"stale exemption: `{MARKER}` names no key; name the retired "
                "key it covers or remove the marker"))
        stripped.append(_MARKER_RE.sub(lambda m: " " * len(m.group(0)), line))
    mentions: dict = {}  # lineno -> [key]
    for lineno, line in enumerate(stripped, 1):
        for m in pattern.finditer(line):
            mentions.setdefault(lineno, []).append(m.group(0))
    covered: set = set()  # (lineno, key)
    for lineno, key in markers:
        if key not in judged:
            findings.append(Finding(
                rel, lineno, key,
                f"stale exemption: `{MARKER} {key}` names a key that is not in "
                f"the registry's {RETIRED_SECTION}; remove the marker"))
            continue
        hit = [(n, key) for n in (lineno, lineno + 1) if key in mentions.get(n, ())]
        if not hit:
            findings.append(Finding(
                rel, lineno, key,
                f"stale exemption: `{MARKER} {key}` covers no mention of {key} "
                "on its line or the next; remove the marker"))
        covered.update(hit)
    exempted = 0
    for lineno, keys in sorted(mentions.items()):
        for key in keys:
            if (lineno, key) in covered:
                exempted += 1
                continue
            base, entry = judged[key]
            repl = entry.get("replaced_by")
            if repl and key != base:
                repl += registry_lib.CRITICAL_SUFFIX
            how = f"replaced by {repl}" if repl else "no replacement"
            findings.append(Finding(
                rel, lineno, key,
                f"retired threshold key ({how}; #{entry.get('tracking')}): show a "
                f"live key, or if this line explains the retirement mark it "
                f"`{MARKER} {key}`"))
    return findings, exempted


def tracked_files(root: Path) -> list:
    try:
        res = subprocess.run(["git", "ls-files", "-z"], cwd=str(root),
                             capture_output=True, timeout=120, check=True)
    except (OSError, subprocess.SubprocessError) as exc:
        raise MeasureError(f"`git ls-files` failed in {root}: {exc}") from exc
    return [p for p in res.stdout.decode("utf-8", "surrogateescape").split("\0") if p]


def run_check(root: Path, files: Optional[list] = None,
              registry_path: Optional[str] = None) -> Result:
    """Scan *files* (paths relative to *root*; default: git's tracked files)."""
    root = Path(root)
    loaded = load_retired_keys(registry_path or str(root / REGISTRY_REL))
    judged = judged_keys(loaded["retired"])
    result = Result(keys=tuple(loaded["retired"]))
    if files is None:
        files = tracked_files(root)
    # With no retired key nothing can be mentioned, but the walk is still held
    # to "it saw files", and every marker left in the tree is then stale.
    pattern = key_regex(judged)
    generated = Generated(loaded["doc"], pattern)
    for rel in files:
        rel = rel.replace(os.sep, "/")
        why = exempt_reason(rel)
        path = root / rel
        if why is None:
            if path.is_symlink():
                why = "symlink"
            elif not path.is_file():
                why = "not-a-file"
        if why is None:
            try:
                raw = path.read_bytes()
            except OSError as exc:
                raise MeasureError(f"cannot read {rel}: {exc}") from exc
            if b"\0" in raw:
                why = "binary"
        if why is not None:
            result.exempt_files[why] = result.exempt_files.get(why, 0) + 1
            continue
        result.files_scanned += 1
        top = rel.split("/", 1)[0] if "/" in rel else "."
        result.scanned_roots[top] = result.scanned_roots.get(top, 0) + 1
        text = raw.decode("utf-8", "replace")
        if not pattern.search(text) and MARKER not in text:
            continue
        masked = generated.mask(rel, text)
        before = sum(1 for _ in pattern.finditer(_MARKER_RE.sub("", text)))
        after = sum(1 for _ in pattern.finditer(_MARKER_RE.sub("", masked)))
        result.generated_skipped += before - after
        findings, exempted = scan_text(rel, masked, judged, pattern)
        result.findings.extend(findings)
        result.marker_exempted += exempted
    if result.files_scanned == 0:
        raise MeasureError(
            f"no file was scanned under {root} ({len(files)} listed): an empty "
            "scan proves nothing")
    return result


def main(argv: Optional[list] = None) -> int:
    try_utf8_stdout()
    ap = argparse.ArgumentParser(
        description=i18n_text(
            "已退役的閾值 key（registry 的 retired_keys）不得出現在文件、範例、"
            "腳本與 portal（#1200）",
            "A retired threshold key (the registry's retired_keys) must not be "
            "named in docs, samples, scripts or the portal (#1200)."))
    ap.add_argument("--ci", action="store_true",
                    help=i18n_text("有 finding 即 exit 1", "exit 1 on findings"))
    ap.add_argument("--repo-root", default=None,
                    help=i18n_text("要掃的 git work tree（預設：本檔所在的 repo）",
                                   "the git work tree to scan (default: this file's repo)"))
    args = ap.parse_args(argv)
    try:
        root = Path(args.repo_root) if args.repo_root else require_git_project_root(_THIS_DIR)
        result = run_check(root)
    except MeasureError as exc:
        print(f"ERROR: check_retired_keys_not_taught: {exc}", file=sys.stderr)
        return EXIT_CALLER_ERROR
    skipped = ", ".join(f"{n} {why}" for why, n in sorted(result.exempt_files.items()))
    summary = (f"{len(result.keys)} retired key(s) judged, "
               f"{result.files_scanned} file(s) scanned; "
               f"{result.marker_exempted} mention(s) exempted by a "
               f"`{MARKER}` marker, {result.generated_skipped} in "
               f"registry-generated text; not scanned: {skipped or 'none'}")
    if not result.findings:
        print(f"OK no retired threshold key is taught — {summary}.")
        return EXIT_OK
    print(f"❌ {len(result.findings)} finding(s): a retired threshold key is "
          "named outside the registry and the changelog —", file=sys.stderr)
    for f in result.findings:
        print(f"  {f.path}:{f.line}: {f.key or '(no key)'} — {f.reason}", file=sys.stderr)
    print(f"({summary}.)\nThe keys come from `{RETIRED_SECTION}:` in "
          f"{REGISTRY_REL}; see this script's docstring for the boundary and "
          "the marker.", file=sys.stderr)
    return EXIT_VIOLATION if args.ci else EXIT_OK


if __name__ == "__main__":
    import _lib_exitcodes
    _lib_exitcodes.run(main)
