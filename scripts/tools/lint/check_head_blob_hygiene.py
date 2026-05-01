#!/usr/bin/env python3
"""check_head_blob_hygiene.py — Inspect committed HEAD blobs for corruption.

Why this exists
---------------
``fix_file_hygiene.py`` runs as an ``pre-commit`` hook and strips NUL bytes
and ensures trailing newlines, but **only on files being staged**. If a
corrupted blob gets into HEAD — via ``--no-verify``, pre-hook bypass, or a
past commit made before the hook existed — nothing retroactively fixes it.

The ``architecture-quiz.jsx`` regression that motivated this harness-hardening
PR is exactly that failure mode:

- Working copy was clean.
- ``git cat-file blob HEAD:docs/interactive/tools/architecture-quiz.jsx``
  had 12 trailing NUL bytes.
- Babel loaded the blob (via ``fetch``) at runtime, saw ``\\x00``, and crashed.
- ``file-hygiene`` never fired because the file was never restaged.

This linter scans **every blob referenced by HEAD** for three defects:

1. **NUL bytes** (``\\x00``) anywhere in text blobs. Binary files are
   allowlisted by extension / detected path prefix.
2. **Missing EOF newline** on source / config / docs files.
3. **Truncated YAML/JSON** — last line looks like a key-without-value or
   file ends mid-token (heuristic, YAML/JSON only).

Rule 3 catches the ``mkdocs.yml`` case where sed truncation left
``- Changelog: CHANGELOG.m`` instead of ``CHANGELOG.md\\n``.

Usage
-----
::

    python3 scripts/tools/lint/check_head_blob_hygiene.py          # report
    python3 scripts/tools/lint/check_head_blob_hygiene.py --ci      # exit 1
    python3 scripts/tools/lint/check_head_blob_hygiene.py --fix     # auto-clean (rewrites working copy; still need to commit)

Exit codes
----------
- ``0`` — clean (or report-only)
- ``1`` — violations found under ``--ci``
- ``2`` — cannot run (not a git repo, git not available)
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]

# Extensions treated as text (EOF-newline + NUL scan).
# NUL scan still runs on any non-binary blob — this set drives the EOF check.
_TEXT_EXT = {
    ".md", ".txt", ".yaml", ".yml", ".json", ".toml",
    ".py", ".jsx", ".js", ".ts", ".tsx", ".css", ".scss",
    ".html", ".xml", ".sh", ".bash", ".mk", ".go", ".rs",
    ".ini", ".cfg", ".conf", ".env", ".tf", ".dockerfile",
}

# Binary extensions — skip entirely.
_BINARY_EXT = {
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg",
    ".pdf", ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".mp3", ".mp4", ".wav", ".webm", ".ogg",
    ".exe", ".dll", ".so", ".dylib",
    ".class", ".jar", ".pyc",
}

# YAML/JSON extensions for truncation heuristic.
_STRUCTURED_EXT = {".yaml", ".yml", ".json"}


@dataclass
class BlobViolation:
    """A single defect detected in a HEAD-committed blob."""

    path: str
    rule: str
    detail: str


def _run_git(args: list[str]) -> subprocess.CompletedProcess[bytes]:
    """Run a git command from PROJECT_ROOT, capturing raw bytes."""
    return subprocess.run(
        ["git", *args],
        cwd=PROJECT_ROOT,
        capture_output=True,
        check=False,
        timeout=60,
    )


def _list_tracked_files() -> list[str]:
    """Return POSIX-style relative paths for every tracked blob.

    Uses ``git ls-files -z`` which reads from the index (the version that
    reflects both HEAD and any staged changes). This matters during
    pre-commit runs: the scan must see the about-to-be-committed content
    so a fix-commit can pass its own hook.
    """
    result = _run_git(["ls-files", "-z"])
    if result.returncode != 0:
        return []
    raw = result.stdout
    if not raw:
        return []
    return [p.decode("utf-8", errors="replace") for p in raw.split(b"\x00") if p]


def _cat_blob(path: str) -> bytes | None:
    """Return raw bytes of the index version of <path>, or None if unavailable.

    ``git cat-file blob :<path>`` reads the INDEX copy:
    - Staged files → the new content about to be committed
    - Unstaged files → identical to HEAD

    Kept as a one-shot fallback for ``--fix`` mode. The main scan path uses
    :func:`_batch_cat_blobs` which is ~50x faster for large repos because
    it fires a single ``git cat-file --batch`` subprocess rather than one
    subprocess per file.
    """
    result = _run_git(["cat-file", "blob", f":{path}"])
    if result.returncode != 0:
        return None
    return result.stdout


def _batch_cat_blobs(paths: list[str]) -> dict[str, bytes]:
    """Return raw bytes for every path in one git subprocess.

    Uses ``git cat-file --batch --buffer`` with the index-prefix (``:path``)
    so staged files return their about-to-be-committed bytes (matching the
    single-path :func:`_cat_blob` semantics).

    Output format per request:
        <sha> blob <size>\\n
        <size bytes of content>\\n
    Missing paths yield:
        <input> missing\\n

    Empty-result dict on any unrecoverable git error — the caller falls
    back to the per-path ``_cat_blob`` loop and skips paths we cannot read.

    Implementation note (PR-2c bug fix): the previous Popen-based
    write-then-read-loop pattern deadlocked above ~150 paths on Windows
    because ``--buffer`` makes git accumulate ALL stdout in memory until
    stdin EOF, then flush it all at once. With pipe buffer ~64KB and
    ~2MB+ of total output for a typical repo, git blocked on stdout
    write while we were still reading from a different path's body —
    classic single-thread Popen pipe deadlock. ``proc.communicate()``
    sidesteps it by using internal threads to drain stdout/stderr
    while writing stdin. Since we already buffer all output for the
    parse loop anyway, the memory profile is identical; the only
    semantic change is that everything is read into memory in one go
    rather than streamed, which is fine here (repo is ~1k files, total
    cat-file output bounded by tracked-bytes <<= GB).
    """
    if not paths:
        return {}

    request = "".join(f":{p}\n" for p in paths).encode("utf-8")
    try:
        proc = subprocess.Popen(
            ["git", "cat-file", "--batch", "--buffer"],
            cwd=PROJECT_ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        # ``communicate`` handles stdin write + stdout/stderr drain on
        # threads, avoiding the deadlock that plain write-then-read
        # would hit once total output exceeds the pipe buffer.
        out, _err = proc.communicate(input=request, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:
            pass
        return {}

    if proc.returncode != 0:
        return {}

    # Parse the batched output buffer in-memory. Same protocol as before:
    #   <sha> blob <size>\n<size bytes>\n   (or `<input> missing\n`)
    result: dict[str, bytes] = {}
    pos = 0
    n = len(out)

    for path in paths:
        # Read header line up to '\n'.
        nl = out.find(b"\n", pos)
        if nl < 0:
            break
        header = out[pos:nl]
        pos = nl + 1
        header_text = header.decode("utf-8", errors="replace")

        parts = header_text.split(" ")
        if len(parts) >= 2 and parts[1] in {"missing", "ambiguous"}:
            # Path not in the index (e.g., submodule entry) — skip silently.
            continue
        if len(parts) != 3 or parts[1] != "blob":
            # Unexpected header; abandon batch to avoid desync.
            break

        try:
            size = int(parts[2])
        except ValueError:
            break

        if pos + size > n:
            break
        result[path] = out[pos:pos + size]
        pos += size
        # Consume the trailing '\n' between blobs (if present).
        if pos < n and out[pos:pos + 1] == b"\n":
            pos += 1

    return result


def _is_skippable(path: str) -> bool:
    """True if we should not scan this path at all."""
    p = Path(path)
    ext = p.suffix.lower()
    if ext in _BINARY_EXT:
        return True
    # Skip LFS pointers, vendored assets, third-party bundles
    for skip_prefix in ("vendor/", "node_modules/", "dist/", "build/", ".git/"):
        if path.startswith(skip_prefix):
            return True
    return False


def _is_structured_truncated(blob: bytes, path: str) -> str | None:
    """Heuristic: detect truncated YAML / JSON blobs.

    Returns a human-readable reason string, or None if the blob looks fine.
    """
    ext = Path(path).suffix.lower()
    if ext not in _STRUCTURED_EXT:
        return None

    try:
        text = blob.decode("utf-8", errors="replace")
    except (UnicodeDecodeError, OSError):
        return None

    if not text:
        return None

    last_line = text.rstrip("\n").splitlines()[-1] if text.strip() else ""

    # JSON: must end with } or ] (optionally + whitespace)
    if ext == ".json":
        stripped = text.rstrip()
        if stripped and stripped[-1] not in "}]":
            return f"JSON blob does not end with closing bracket (last char: {stripped[-1]!r})"
        return None

    # YAML: the last non-empty content line must not look like a key-with-no-value
    # and must not be mid-token (e.g. dangling ``- Changelog: CHANGELOG.m``).
    # Simple rule: if the last line ends in a letter/digit without a trailing
    # newline, AND the line looks like an incomplete path (ends in . or has
    # a bare extension stub), flag it.
    if last_line:
        # Dangling key "foo:" is legal YAML (null value), skip.
        # Dangling path/file ref like "foo.m" or "foo: bar.m" is suspicious.
        if last_line.rstrip().endswith((".m", ".y", ".j", ".p", ".t", ".h", ".c", ".s")):
            # Single-character extension fragment is suspicious regardless
            # of position in the value — catches "CHANGELOG.m" type truncation.
            tail = last_line.rstrip().rsplit(".", 1)[-1]
            if len(tail) <= 2 and tail.isalpha():
                return (
                    f"YAML blob's last line looks truncated (ends with '.{tail}'): "
                    f"{last_line.strip()!r}"
                )

    return None


def scan_blob(path: str, blob: bytes, strict: bool = False) -> list[BlobViolation]:
    """Return any hygiene violations for a single HEAD blob.

    Rule severity:
      - NUL / TRUNC → always fatal (zero tolerance)
      - EOF        → fatal only under strict mode (pre-existing drift exists)
    """
    issues: list[BlobViolation] = []
    ext = Path(path).suffix.lower()

    # Rule 1: NUL bytes (ALWAYS fatal — catches the architecture-quiz.jsx case)
    if b"\x00" in blob:
        positions = []
        idx = 0
        while len(positions) < 3:
            j = blob.find(b"\x00", idx)
            if j < 0:
                break
            positions.append(j)
            idx = j + 1
        count = blob.count(b"\x00")
        issues.append(
            BlobViolation(
                path=path,
                rule="NUL",
                detail=f"{count} NUL byte(s) in HEAD blob (first at offset {positions[0]})",
            )
        )

    # Rule 3: structured-file truncation (ALWAYS fatal — catches mkdocs.yml case)
    trunc_reason = _is_structured_truncated(blob, path)
    if trunc_reason:
        issues.append(
            BlobViolation(
                path=path,
                rule="TRUNC",
                detail=trunc_reason,
            )
        )

    # Rule 2: Missing EOF newline — strict mode only until pre-existing drift is cleaned
    if strict and ext in _TEXT_EXT and blob and not blob.endswith(b"\n"):
        last_bytes = blob[-20:].decode("utf-8", errors="replace").replace("\n", "\\n")
        issues.append(
            BlobViolation(
                path=path,
                rule="EOF",
                detail=f"missing trailing newline (ends with …{last_bytes!r})",
            )
        )

    return issues


def _fix_working_copy(path: str, original: bytes) -> tuple[bool, str]:
    """Strip NUL bytes + ensure EOF newline in the working-copy file.

    Returns ``(fixed, note)``. Does NOT touch binary files and does not
    stage/commit anything — the user still has to commit the fix.
    """
    full = PROJECT_ROOT / path
    if not full.exists():
        return False, "working copy missing"
    try:
        current = full.read_bytes()
    except OSError as exc:
        return False, f"read failed: {exc}"

    fixed = current.replace(b"\x00", b"")
    ext = Path(path).suffix.lower()
    if ext in _TEXT_EXT and fixed and not fixed.endswith(b"\n"):
        fixed = fixed + b"\n"

    if fixed == current:
        # Working copy is already clean; defect only exists in HEAD blob.
        return False, "working copy already clean — commit needed to replace HEAD blob"

    try:
        full.write_bytes(fixed)
    except OSError as exc:
        return False, f"write failed: {exc}"

    return True, "fixed working copy (re-stage and commit)"


def main() -> int:
    """CLI entry point: scan HEAD blobs for hygiene defects."""
    parser = argparse.ArgumentParser(
        description="Scan committed HEAD blobs for NUL bytes, missing EOF newlines, "
        "and truncated YAML/JSON."
    )
    parser.add_argument("--ci", action="store_true", help="Exit 1 on violations")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Also fail on missing EOF newlines (CI mode; pre-commit stays lenient)",
    )
    parser.add_argument(
        "--fix",
        action="store_true",
        help="Attempt to fix working copy (does not commit)",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Show per-file progress"
    )
    args = parser.parse_args()

    # Verify git is available and we're in a repo
    probe = _run_git(["rev-parse", "--git-dir"])
    if probe.returncode != 0:
        print("⚠ not a git repo — skipping HEAD blob scan")
        return 2

    paths = _list_tracked_files()
    if not paths:
        print("⚠ git index has no tracked files")
        return 0

    violations: list[BlobViolation] = []
    scanned = 0
    skipped = 0

    # Filter out skippable paths once, then batch-read everything that
    # remains in a single ``git cat-file --batch`` subprocess. This is
    # ~50x faster than one subprocess per file on a 700-blob repo.
    scan_paths = [p for p in paths if not _is_skippable(p)]
    skipped += len(paths) - len(scan_paths)

    # PR #165 (S#74 follow-up): localize-the-hang milestones. The hook
    # has historically gone silent during long runs, making "is it
    # stuck or just slow" indistinguishable. Three observation points
    # below let the operator pin down the failure mode:
    #
    #   - "Reading N HEAD blobs..."           → entered _batch_cat_blobs
    #   - "✓ batch read complete (took Xs)"   → batch returned cleanly
    #   - "scan I/N" every 100 in --verbose   → mid-scan progress
    #   - final "✓ N HEAD blob(s) clean"      → all done
    #
    # Hang patterns now diagnosable:
    #   (a) "Reading..." but never "✓ batch read complete" → hang
    #       inside _batch_cat_blobs (the PR #164 deadlock class — now
    #       guarded by communicate(timeout=60), but logs make it
    #       diagnosable if a new variant shows up)
    #   (b) "✓ batch read complete" but verbose progress stalls →
    #       hang inside scan_blob for some pathological input
    #   (c) silent past "Reading..." for >60s in non-verbose mode →
    #       run with --verbose to localize
    print(f"Reading {len(scan_paths)} HEAD blob(s)...", flush=True)
    t_batch_start = time.time()
    blobs = _batch_cat_blobs(scan_paths)
    t_batch_elapsed = time.time() - t_batch_start
    print(
        f"✓ batch read complete: {len(blobs)} blob(s) loaded "
        f"in {t_batch_elapsed:.1f}s",
        flush=True,
    )

    PROGRESS_EVERY = 100
    for i, path in enumerate(scan_paths):
        blob = blobs.get(path)
        if blob is None:
            # Missing from the batch — fall back to the per-path reader
            # (submodules, missing index entries, etc.)
            blob = _cat_blob(path)
        if blob is None:
            skipped += 1
            continue

        scanned += 1
        if args.verbose:
            print(f"  scan {path} ({len(blob)}B)")
        elif scanned > 0 and scanned % PROGRESS_EVERY == 0:
            # Default-mode progress: every 100 blobs without --verbose,
            # so a stuck scan in non-verbose mode still shows it's alive.
            print(f"  ...scanned {scanned}/{len(scan_paths)}", flush=True)

        violations.extend(scan_blob(path, blob, strict=args.strict))

    if not violations:
        print(f"✓ {scanned} HEAD blob(s) clean ({skipped} skipped)")
        return 0

    by_file: dict[str, list[BlobViolation]] = {}
    for v in violations:
        by_file.setdefault(v.path, []).append(v)

    print(f"✗ {len(violations)} defect(s) in {len(by_file)} HEAD blob(s):\n")
    for path, items in sorted(by_file.items()):
        print(f"  {path}")
        for v in items:
            print(f"    [{v.rule}] {v.detail}")
        if args.fix:
            original = _cat_blob(path) or b""
            fixed, note = _fix_working_copy(path, original)
            marker = "✓" if fixed else "…"
            print(f"    {marker} {note}")
        print()

    print(
        "Defects in HEAD blobs are NOT fixed by the staged-file hygiene hook. "
        "Re-stage the affected files and commit to replace the corrupted blob.\n"
        "Root cause is usually 'sed -i' on a file missing its trailing newline — "
        "dev-rules #11 forbids that pattern."
    )

    return 1 if args.ci else 0


if __name__ == "__main__":
    sys.exit(main())
