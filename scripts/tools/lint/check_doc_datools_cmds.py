#!/usr/bin/env python3
"""check_doc_datools_cmds.py — documented `da-tools` binary-wrapper subcommands
must be valid.

Static guard for the #141 Track A / F3 class: the try-local README showed
``da-tools ... guard /conf.d``, but the shipped CLI takes ``guard
defaults-impact --config-dir ...`` — a stale subcommand that only surfaced
when a human ran it. No check covered command validity, so it shipped.

**Scope decision.** A broader check (validate every ``da-tools <command>``
against the full CLI command tree) was prototyped and rejected: scenario docs
use illustrative / aspirational pseudo-commands even inside code blocks
(``da-tools describe-tenant``, ``list-tenants``, ``upgrade-check`` per issue
#405), giving ~88 false positives — the same noise that sank a broad
path-existence lint. So this is scoped to the three **binary-wrapper**
commands (``guard`` / ``parser`` / ``batch-pr``), whose subcommand sets are a
small, stable, real contract — and which is exactly where F3 lived.

Only fenced code blocks are scanned (prose mentions and inline-code
suggestions are not runnable). Lines with a ``<placeholder>`` or an inline
``datools-cmd-ignore`` are skipped; ``guard --help`` / ``-h`` is allowed.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, NamedTuple, Set

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))  # Repo subdir layout
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]
OPS_DIR = REPO_ROOT / "scripts" / "tools" / "ops"
DOCS_DIR = REPO_ROOT / "docs"

# Binary-wrapper command -> valid subcommands. Source of truth: the
# `Subcommands:` block of each dispatcher in scripts/tools/ops/*_dispatch.py
# (mirrors the Go binary). Kept as a literal for robustness; the self-test
# `test_subcommand_map_matches_dispatchers` greps the dispatchers so this drifts
# loudly if a subcommand is added/removed.
WRAPPER_SUBCOMMANDS: Dict[str, Set[str]] = {
    "guard": {"defaults-impact"},
    "parser": {"import", "allowlist"},
    "batch-pr": {"apply", "refresh", "refresh-source"},
}

# `da-tools` (binary) or `da-tools:vX.Y.Z` (image), then the wrapper command +
# whatever token follows (the candidate subcommand).
_DATOOLS_RE = re.compile(
    r"da-tools(?::v[0-9.]+)?\s+(guard|parser|batch-pr)(?:\s+([^\s\\]+))?")

_PLACEHOLDER_CHARS = "<>${}"
INLINE_IGNORE = "datools-cmd-ignore"


class Issue(NamedTuple):
    check: str
    file: str
    line: int
    message: str

    def to_dict(self) -> dict:
        return self._asdict()


def _doc_files(docs_dir: Path) -> List[Path]:
    return [f for f in sorted(docs_dir.rglob("*.md"))
            if "/internal/archive/" not in f.as_posix()]


def check_datools_subcommands(doc_files: List[Path],
                              sub_map: Dict[str, Set[str]],
                              repo_root: Path) -> List[Issue]:
    issues: List[Issue] = []
    for f in doc_files:
        try:
            lines = f.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        rel = str(f.relative_to(repo_root)).replace("\\", "/")
        in_code = False
        for i, line in enumerate(lines, 1):
            if line.lstrip().startswith("```"):
                in_code = not in_code
                continue
            # Only fenced code blocks hold real invocations; prose / inline-code
            # mentions are illustrative and must not be flagged.
            if not in_code:
                continue
            if INLINE_IGNORE in line or any(c in line for c in _PLACEHOLDER_CHARS):
                continue
            for m in _DATOOLS_RE.finditer(line):
                wrapper, nxt = m.group(1), m.group(2)
                valid = sub_map.get(wrapper, set())
                # A bare `--flag` (e.g. --help) is a valid invocation.
                if nxt is None or nxt.startswith("-"):
                    continue
                if nxt not in valid:
                    issues.append(Issue(
                        "datools-bad-subcommand", rel, i,
                        f"da-tools {wrapper} '{nxt}' is not a subcommand "
                        f"(valid: {', '.join(sorted(valid))})"))
    return issues


# Customer-facing markdown that lives OUTSIDE docs/. Enumerated by FILE, not
# by spelling: these are the entry points a customer actually lands on, and
# each already carries `docker run` examples. A new one has to be added here —
# accepted, because the alternative (scan every .md in the repo) pulls in
# internal notes and archived reports whose examples are deliberately stale.
_EXTRA_DOC_FILES = (
    "components/da-tools/README.md",
    "components/da-tools/app/QUICKSTART.md",
    "try-local/README.md",
)

_DOCKER_RUN_RE = re.compile(r"\bdocker\s+run\b")
_DATOOLS_IMAGE_RE = re.compile(r"da[-_]tools", re.IGNORECASE)


def _mounts(flat: str) -> List[str]:
    """Every `-v` / `--volume` spec in *flat*, quoting-normalised.

    ⛔ Strip quotes and trailing punctuation BEFORE reading the option field.
    A naive regex reads `"$(pwd)/conf.d:/conf.d:ro"` as writable because the
    closing quote lands in the option group — i.e. it over-reports, which for
    a guard means誤紅 on examples that are already correct.
    """
    toks = flat.split()
    out: List[str] = []
    for i, t in enumerate(toks):
        if t in ("-v", "--volume") and i + 1 < len(toks):
            out.append(toks[i + 1].strip("\"'").rstrip("\\,;)]\"'"))
    return out


def _is_writable(spec: str) -> bool:
    parts = spec.split(":")
    opts = parts[-1].split(",") if len(parts) >= 3 else []
    return "ro" not in opts


def check_writable_mount_has_user(doc_files: List[Path],
                                  repo_root: Path) -> List[Issue]:
    """A da-tools `docker run` with a WRITABLE mount must pass `--user`.

    The shipped image ends ``USER nonroot:nonroot`` (uid 10001,
    ``components/da-tools/app/Dockerfile``) while the directory a customer
    mounts is their own checkout (typically uid 1000). Any subcommand that
    writes then dies on a bare ``PermissionError`` traceback with zero files
    produced — measured inside a single Linux container: uid 10001 fails,
    uid 1000 succeeds (#1495).

    ⛔ The predicate is the MOUNT, not the subcommand. "Which subcommands
    write" is a list that has to be maintained against every tool's argparse
    (and the same tool writes or not depending on its flags — ``generate-routes
    --validate`` exits before it touches ``-o``). A writable mount is the
    example's own declaration of intent, so the invariant reads: if you
    declared you may write there, pass the uid that can. An example that never
    writes should say so with ``:ro`` and is then out of scope by construction.

    Placeholder-bearing lines are skipped by the same rule the subcommand check
    uses: a synopsis like ``<command> [flags]`` cannot be judged, and guessing
    would put誤紅 on documentation that is deliberately generic.
    """
    issues: List[Issue] = []
    for f in doc_files:
        try:
            lines = f.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        rel = str(f.relative_to(repo_root)).replace("\\", "/")
        in_code = False
        i = 0
        while i < len(lines):
            line = lines[i]
            if line.lstrip().startswith("```"):
                in_code = not in_code
                i += 1
                continue
            if not in_code or not _DOCKER_RUN_RE.search(line):
                i += 1
                continue
            start = i
            buf = [line]
            while (buf[-1].rstrip().endswith("\\")
                   and i + 1 < len(lines)
                   and not lines[i + 1].lstrip().startswith("```")):
                i += 1
                buf.append(lines[i])
            blk = "\n".join(buf)
            i += 1
            flat = " ".join(blk.split())
            if not _DATOOLS_IMAGE_RE.search(flat):
                continue
            # ⛔ NOT `_PLACEHOLDER_CHARS`. That set is `<>${}`, and `$` appears
            # in essentially every real example (`-v $(pwd)/conf.d:...`), so
            # reusing it here skipped 97 of 122 blocks — including all fifteen
            # this rule was written to hold. Measured before shipping, which is
            # the only reason it was noticed: the check was GREEN either way.
            #
            # The two checks need different skips because they judge different
            # things. A `<command>` placeholder makes the SUBCOMMAND
            # unjudgeable; it says nothing about the MOUNT, and `$(pwd)` is a
            # real shell expression rather than a placeholder at all. So skip
            # only when the mount spec itself is a placeholder.
            if INLINE_IGNORE in blk:
                continue
            if "--user" in flat:
                continue
            specs = _mounts(flat)
            # `<host>:/path` is a placeholder; `${{ github.workspace }}/x:/y`
            # is an UNEXPANDED CI template whose spaces defeat token splitting
            # (it parses as the fragment `${{`). Both are "not a mount spec
            # yet", so both are out of scope rather than reported — reporting a
            # fragment would be a誤紅 whose only cheap fix is deleting the
            # example.
            if any(("<" in s or ">" in s or "{{" in s) for s in specs):
                continue
            writable = [m for m in specs if _is_writable(m)]
            if writable:
                issues.append(Issue(
                    "datools-writable-mount-without-user", rel, start + 1,
                    f"writable mount(s) {writable} but no --user; the image "
                    f"runs as uid 10001 and a customer's directory does not, "
                    f"so anything this writes fails with PermissionError. "
                    f"Add `--user $(id -u):$(id -g)`, or mark the mount `:ro` "
                    f"if it is only read (#1495)."))
    return issues


def run(repo_root: Path = REPO_ROOT) -> List[Issue]:
    docs = _doc_files(repo_root / "docs")
    extra = [repo_root / rel for rel in _EXTRA_DOC_FILES]
    return (check_datools_subcommands(docs, WRAPPER_SUBCOMMANDS, repo_root)
            + check_writable_mount_has_user(docs + [f for f in extra
                                                    if f.is_file()], repo_root))


def main() -> int:
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ci", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    issues = run()
    if args.json:
        print(json.dumps({"issues": [i.to_dict() for i in issues],
                          "count": len(issues)}, ensure_ascii=False, indent=2))
    elif issues:
        for it in issues:
            print(f"  ❌ [{it.check}] {it.file}:{it.line} — {it.message}",
                  file=sys.stderr)
        print(f"\n❌ {len(issues)} da-tools subcommand issue(s)", file=sys.stderr)
    else:
        print("✅ documented da-tools wrapper subcommands are valid")
    return EXIT_VIOLATION if (issues and args.ci) else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
