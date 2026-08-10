#!/usr/bin/env python3
"""check_devcontainer_dep_parity.py — dev container must declare every Python dep CI installs.

Lint class & scope (lint-policy.md §3)
--------------------------------------
Repo-invariant, whole-surface (not diff-scoped): two declaration sites that
must not drift from each other. Pure static parse — reads
`.devcontainer/devcontainer.json` and `.github/workflows/ci.yml`, builds no
container and runs no pip.

Why this exists (#1254)
-----------------------
CLAUDE.md makes the dev container the MAIN PATH for running tests
(`make dc-test`). But its `postCreateCommand` declared a SMALLER package set
than the CI job that runs the same suite, and the missing packages do not
fail — the affected tests `pytest.importorskip`/skip themselves. Measured on
the same commit, same three modules:

    dev container : 133 passed, 42 skipped
    Windows host  : 161 passed, 14 skipped

i.e. 28 tests silently did not run in the main-path environment, with no
signal. `croniter` and `promql-parser` were never declared; `hypothesis` was
never declared either (an ad-hoc install happened to be present, at a version
that had already drifted from the pin). Worse, `pytest-benchmark` WAS declared
and yet absent — proof that the declaration and the live container can diverge
with nothing noticing.

This is the same "silent skip = toothless" failure mode the repo already
fails CLOSED on elsewhere (`VIBE_REQUIRE_MTAIL=1`, `WAVEFORM_PROMTOOL_REQUIRE=1`,
the vmalert-tool provisioning step). This gate applies that discipline to the
dev container's declared Python dependencies.

What it checks
--------------
CI's package set MUST be a SUBSET of the dev container's declared set:

    {packages pip-installed by the ci.yml job that runs the full pytest suite}
        ⊆ {packages pip-installed by devcontainer.json postCreateCommand}

Extra packages in the dev container (mkdocs-*, pytest-benchmark, …) are fine —
the container legitimately does more than CI.

HONEST BOUNDARY — what this gate does NOT do
--------------------------------------------
It verifies the DECLARATION only. It cannot tell you whether an ALREADY-BUILT
container actually has those packages: an existing container predates any edit
to `postCreateCommand`, and nothing re-runs provisioning on an existing
container. After this gate goes green you may still need to rebuild the
container (or `pip install` by hand) for the packages to exist. Verify a live
container with:

    docker exec vibe-dev-container bash -lc 'python -m pip show <pkg>'

Exit codes (dev-rules #13)
--------------------------
  0  parity holds (CI set ⊆ devcontainer set)
  1  drift: CI installs something the dev container never declares
  2  caller/config error (file missing, unparseable, or the ci.yml job that
     runs the suite could not be located) — fail-closed, NOT a pass
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
from pathlib import Path

import yaml

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))  # Repo subdir layout
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402
from _lib_compat import try_utf8_stdout  # noqa: E402
from _lib_validation import i18n_text  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DEVCONTAINER = REPO_ROOT / ".devcontainer" / "devcontainer.json"
DEFAULT_CI = REPO_ROOT / ".github" / "workflows" / "ci.yml"

# The ci.yml job we demand parity with: the one that runs the FULL pytest
# suite. Identified by a step whose `run` invokes pytest over `tests/` with
# coverage — matching on the job NAME would break the moment someone renames
# it, and matching the aggregation gate ("Python Tests (3.13)") would be wrong
# because that job runs no tests at all.
_FULL_SUITE_RUN_RE = re.compile(r"\bpytest\s+tests/.*--cov\b", re.DOTALL)

# pip flags that take a value we must not mistake for a package name.
_FLAGS_WITH_VALUE = {"-c", "--constraint", "-r", "--requirement", "--index-url",
                     "-i", "--extra-index-url", "--find-links", "-f",
                     "--target", "-t", "--python-version", "--platform"}


class ParityError(Exception):
    """Configuration/parse failure — always fail-closed (exit 2)."""


def normalize(pkg: str) -> str:
    """Normalize a pip requirement to a comparable distribution name.

    pip treats names case-insensitively and `-`/`_`/`.` as equivalent
    (PEP 503), so `PyYAML`, `pyyaml` and `py_yaml` are one package. Version
    specifiers and extras are stripped: we compare WHICH packages are
    declared, not which versions (versions are pinned centrally by
    `requirements/ci-constraints.txt`).
    """
    name = re.split(r"[<>=!~\[;]", pkg, maxsplit=1)[0].strip()
    return re.sub(r"[-_.]+", "-", name).lower()


def packages_from_pip_command(command: str) -> set[str]:
    """Extract package names from every `pip install` in a shell command.

    Handles `&&`-chained commands, `pip`/`pip3`/`python -m pip`, and skips
    flags (plus the values of flags that take one, so `-c constraints.txt`
    never yields a bogus "constraints.txt" package).
    """
    try:
        tokens = shlex.split(command)
    except ValueError as exc:  # unbalanced quotes
        raise ParityError(f"cannot tokenize command: {exc}") from exc

    packages: set[str] = set()
    i = 0
    while i < len(tokens):
        # Find `pip install` / `pip3 install` / `python -m pip install`
        is_pip = tokens[i] in ("pip", "pip3") or (
            tokens[i] in ("python", "python3")
            and tokens[i + 1:i + 3] == ["-m", "pip"]
        )
        if not (is_pip and "install" in tokens[i:i + 4]):
            i += 1
            continue
        i = tokens.index("install", i) + 1
        while i < len(tokens):
            tok = tokens[i]
            if tok in ("&&", "||", ";", "|"):
                break
            if tok in _FLAGS_WITH_VALUE:
                i += 2
                continue
            if tok.startswith("-"):
                i += 1
                continue
            packages.add(normalize(tok))
            i += 1
    return packages


def _strip_jsonc_comments(text: str) -> str:
    """Drop `//` line comments so devcontainer.json (JSONC) parses as JSON.

    Only strips `//` that starts a line (after optional whitespace). A naive
    strip would corrupt URLs like `https://...` inside string values.
    """
    return re.sub(r"^[ \t]*//.*$", "", text, flags=re.MULTILINE)


def devcontainer_packages(path: Path) -> set[str]:
    """Packages declared by devcontainer.json's postCreateCommand."""
    if not path.is_file():
        raise ParityError(f"devcontainer.json not found: {path}")
    try:
        data = json.loads(_strip_jsonc_comments(path.read_text(encoding="utf-8")))
    except json.JSONDecodeError as exc:
        raise ParityError(f"cannot parse {path.name}: {exc}") from exc

    command = data.get("postCreateCommand")
    if command is None:
        raise ParityError(f"{path.name} has no postCreateCommand")
    if isinstance(command, list):
        command = " ".join(command)
    if not isinstance(command, str):
        raise ParityError(
            f"{path.name} postCreateCommand is {type(command).__name__}, "
            "expected string or list"
        )
    return packages_from_pip_command(command)


def ci_packages(path: Path) -> set[str]:
    """Packages installed by the ci.yml job that runs the full pytest suite."""
    if not path.is_file():
        raise ParityError(f"ci.yml not found: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ParityError(f"cannot parse {path.name}: {exc}") from exc

    jobs = (data or {}).get("jobs")
    if not isinstance(jobs, dict):
        raise ParityError(f"{path.name} has no `jobs:` mapping")

    for job_id, job in jobs.items():
        steps = (job or {}).get("steps") or []
        runs = [s.get("run", "") for s in steps if isinstance(s, dict)]
        if not any(_FULL_SUITE_RUN_RE.search(r or "") for r in runs):
            continue
        packages: set[str] = set()
        for run in runs:
            packages |= packages_from_pip_command(run or "")
        if not packages:
            raise ParityError(
                f"job {job_id!r} runs the full suite but installs no pip "
                "packages — the parse is wrong, refusing to pass vacuously"
            )
        return packages

    raise ParityError(
        f"no job in {path.name} runs the full pytest suite "
        f"(looked for a `run:` matching {_FULL_SUITE_RUN_RE.pattern!r}). "
        "If the command changed, update this gate — do not delete it."
    )


def main(argv: list[str] | None = None) -> int:
    try_utf8_stdout()
    parser = argparse.ArgumentParser(
        description=i18n_text(
            "斷言 dev container 宣告安裝的 Python 套件集，涵蓋 CI 跑全套測試"
            "那個 job 所安裝的套件——缺的套件會讓測試靜默跳過而非失敗。",
            "Assert the dev container declares every Python package the CI "
            "job that runs the full test suite installs — a missing package "
            "makes tests skip silently instead of failing.",
        ),
    )
    parser.add_argument(
        "--devcontainer", default=str(DEFAULT_DEVCONTAINER),
        help=i18n_text("devcontainer.json 路徑", "Path to devcontainer.json"),
    )
    parser.add_argument(
        "--ci", dest="ci_workflow", default=str(DEFAULT_CI),
        help=i18n_text("CI workflow YAML 路徑", "Path to the CI workflow YAML"),
    )
    parser.add_argument(
        "--json", action="store_true",
        help=i18n_text("以 JSON 輸出結果", "Emit the result as JSON"),
    )
    args = parser.parse_args(argv)

    try:
        declared = devcontainer_packages(Path(args.devcontainer))
        required = ci_packages(Path(args.ci_workflow))
    except ParityError as exc:
        if args.json:
            print(json.dumps({"status": "error", "error": str(exc)},
                             ensure_ascii=False))
        else:
            print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_CALLER_ERROR

    missing = sorted(required - declared)

    if args.json:
        print(json.dumps(
            {
                "status": "drift" if missing else "ok",
                "missing": missing,
                "declared": sorted(declared),
                "required": sorted(required),
            },
            ensure_ascii=False,
        ))
        return EXIT_VIOLATION if missing else EXIT_OK

    if missing:
        print(
            f"devcontainer dep parity FAILED: {len(missing)} package(s) "
            f"installed by CI but never declared in devcontainer.json:",
            file=sys.stderr,
        )
        for pkg in missing:
            print(f"  - {pkg}", file=sys.stderr)
        print(
            "\nWhy this matters: the dev container is the main path for "
            "`make dc-test`, and a missing package does NOT fail the suite — "
            "the affected tests skip themselves, so the container reports a "
            "GREENER result than CI.\n"
            "Fix: add them to `postCreateCommand` in "
            ".devcontainer/devcontainer.json, keeping "
            "`-c requirements/ci-constraints.txt`.\n"
            "Note: this gate checks the DECLARATION only — an already-built "
            "container still needs a rebuild for the change to take effect.",
            file=sys.stderr,
        )
        return EXIT_VIOLATION

    print(
        f"devcontainer dep parity OK: all {len(required)} CI package(s) "
        f"are declared ({len(declared)} declared in total)."
    )
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
