#!/usr/bin/env python3
"""gofmt -s -l over the staged .go files; any unformatted file fails the commit (#2583).

Why this exists
---------------
Every Go module's ``.golangci.yml`` enables the ``gofmt`` formatter, but that
only runs in CI (``validate.yaml`` Go Lint). Before this hook no pre-commit
hook ran gofmt, so an unformatted ``.go`` passed the local commit and
``make pr-preflight-quick`` and was first caught by CI or a reviewer (#2577:
``cmd/da-guard/effective_test.go``).

What it does
------------
pre-commit hands it the staged files matching ``\\.go$`` — any module, so
all of the repo's go.mod modules are covered without listing them. It runs
``gofmt -s -l`` on them and fails, listing the file names, when gofmt reports
any. It never rewrites a file (dev-rules: no in-place rewrite on mounted
paths); fix with ``gofmt -s -w <file>`` yourself.

⛔ ``-s`` is not optional. golangci-lint's gofmt formatter defaults to
``simplify: true`` and no module's ``.golangci.yml`` turns it off, so CI is
``gofmt -s``. Measured on golangci-lint v2.12.2 with ``var X = []T{T{A: 1}}``:
plain ``gofmt -l`` lists nothing, CI Go Lint reports "File is not properly
formatted (gofmt)", and the same config with ``simplify: false`` passes.
Without ``-s`` this hook would be green on a file CI rejects.

``--`` is passed both in the hook entry (to this script's argparse) and to
gofmt, so a staged file whose name starts with ``-`` is a path, not a flag:
without it gofmt answers ``flag provided but not defined: -x.go`` (rc 2). With no ``.go`` file staged
pre-commit does not call it at all ("(no files to check)Skipped").

Which gofmt
-----------
``gofmt`` on PATH, else ``$(go env GOROOT)/bin/gofmt``. gofmt ships with the
Go toolchain, whose SSOT is each module's ``go.mod`` (the dev container's go
feature is aligned to it; see docs/internal/toolchain-versions.md). This hook
does not pin a version itself: the CI gofmt is the one built into
golangci-lint, and gofmt's output has been stable across recent Go releases.
The gofmt path and ``go version`` are printed on every failure so a version
mismatch is visible.

Missing toolchain: fail-closed
------------------------------
No gofmt and no go on PATH ⇒ rc 2 ("cannot measure"), never rc 0. A pass
that measured nothing would read as "formatted". The explicit opt-out is
pre-commit's own ``SKIP=go-fmt git commit ...``, which pre-commit reports as
``Skipped`` (not ``Passed``); CI Go Lint still checks the file. Same contract
as the other engine-wrapping hooks (``check_iac_vibe_rules.py``,
``check_pint.py --ci``): a missing engine in pre-commit is an error.

Exit codes: 0 every given file is gofmt-clean / 1 gofmt lists at least one
file / 2 cannot measure — no gofmt, a given path does not exist, or gofmt
itself errored (e.g. a syntax error it cannot parse). On a syntax error gofmt
still lists the OTHER unformatted files on stdout; those are printed too, so
one broken file does not hide the rest, but the rc stays 2.

Usage:
    python3 scripts/tools/lint/check_go_fmt.py -- path/a.go path/b.go
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Sequence

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))
try:
    from _lib_compat import try_utf8_stdout  # noqa: E402
except Exception:  # pragma: no cover
    def try_utf8_stdout() -> None:  # type: ignore
        pass
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402

HOOK_ID = "go-fmt"
_TIMEOUT = 120


class MeasureError(Exception):
    """gofmt could not be run — maps to rc 2, never to rc 0.

    ``listed`` carries any files gofmt did list before failing on another.
    """

    def __init__(self, msg: str, listed: Optional[List[str]] = None) -> None:
        super().__init__(msg)
        self.listed = listed or []


def locate_gofmt() -> Optional[str]:
    """gofmt on PATH, else the one inside ``go env GOROOT``; None if neither."""
    found = shutil.which("gofmt")
    if found:
        return found
    go = shutil.which("go")
    if not go:
        return None
    try:
        proc = subprocess.run(  # nosec B603 — fixed argv, no shell
            [go, "env", "GOROOT"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    goroot = proc.stdout.strip()
    if proc.returncode != 0 or not goroot:
        return None
    exe = "gofmt.exe" if os.name == "nt" else "gofmt"
    candidate = Path(goroot) / "bin" / exe
    return str(candidate) if candidate.is_file() else None


def _go_version() -> str:
    go = shutil.which("go")
    if not go:
        return "go not on PATH"
    try:
        proc = subprocess.run(  # nosec B603 — fixed argv, no shell
            [go, "version"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"go version failed: {exc}"
    return (proc.stdout or proc.stderr).strip()


GOFMT_ARGS = ("-s", "-l", "--")


def unformatted(gofmt: str, files: Sequence[str]) -> List[str]:
    """Return the files ``gofmt -s -l`` lists. Raises MeasureError if it cannot run."""
    missing = [f for f in files if not Path(f).is_file()]
    if missing:
        raise MeasureError(f"no such file: {', '.join(missing)}")
    try:
        proc = subprocess.run(  # nosec B603 — fixed argv, no shell
            [gofmt, *GOFMT_ARGS, *files], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise MeasureError(f"{gofmt} failed to run: {exc}") from exc
    listed = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
    if proc.returncode != 0:
        # A file gofmt cannot parse is not listed by -l; it only shows up as
        # a non-zero rc + stderr. Treat that as "not measured", not as clean,
        # but keep what it did list for the other files.
        raise MeasureError(
            f"{gofmt} {' '.join(GOFMT_ARGS[:2])} exited {proc.returncode}: "
            f"{proc.stderr.strip() or '(no stderr)'}",
            listed=listed,
        )
    return listed


def main(argv: Optional[List[str]] = None) -> int:
    try_utf8_stdout()
    parser = argparse.ArgumentParser(
        description="Fail when gofmt -s -l lists any of the given .go files (#2583)")
    parser.add_argument("files", nargs="*", help=".go files (pre-commit passes the staged ones)")
    args = parser.parse_args(argv)

    if not args.files:
        print(f"[{HOOK_ID}] no files given; nothing measured", file=sys.stderr)
        return EXIT_OK

    gofmt = locate_gofmt()
    if gofmt is None:
        print(
            f"[{HOOK_ID}] ⛔ cannot measure: neither `gofmt` nor `go` is on PATH, "
            f"so {len(args.files)} staged .go file(s) were NOT format-checked.\n"
            "  Install the Go toolchain (version: the module's go.mod; the dev "
            "container ships it), or, to commit without this check, run\n"
            f"    SKIP={HOOK_ID} git commit ...\n"
            "  pre-commit then reports the hook as Skipped, and CI Go Lint "
            "(gofmt formatter) still checks the file.",
            file=sys.stderr,
        )
        return EXIT_CALLER_ERROR

    try:
        bad = unformatted(gofmt, args.files)
    except MeasureError as exc:
        print(f"[{HOOK_ID}] ⛔ cannot measure: {exc}", file=sys.stderr)
        if exc.listed:
            _report(exc.listed, gofmt)
        return EXIT_CALLER_ERROR

    if not bad:
        return EXIT_OK
    _report(bad, gofmt)
    return EXIT_VIOLATION


def _report(bad: Sequence[str], gofmt: str) -> None:
    print(f"[{HOOK_ID}] {len(bad)} file(s) not gofmt -s formatted:")
    for f in bad:
        print(f"  ❌ {f}")
    print(
        "  Fix with `gofmt -s -w <file>` and re-stage. This hook never rewrites files.\n"
        f"  gofmt: {gofmt} ({_go_version()})",
        file=sys.stderr,
    )


if __name__ == "__main__":
    sys.exit(main())
