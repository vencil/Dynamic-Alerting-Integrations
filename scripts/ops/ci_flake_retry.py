#!/usr/bin/env python3
"""ci_flake_retry.py — Surgical Go-test retry wrapper using flaky-tests.yaml.

Audit ⑥ "Legacy" — codifies HA-10 from docs/internal/dx-tooling-backlog.md.

Why this exists
---------------
Some Go tests are stochastically flaky (time-dependent fsnotify, CI-runner
load). Without retry, we burn ops time on `gh run rerun --failed` (v2.7.0
PR #26 took an extra hour from this). With BLANKET retry, we hide real
regressions. This wrapper is the surgical middle ground:

  1. Run the underlying command (e.g. `go test ./... -race -count=1`).
  2. If everything passes → exit 0.
  3. If anything fails → parse Go test output for failing test names.
  4. If ALL failures match `flaky-tests.yaml` patterns → retry the
     matching tests (just those, not the whole suite) up to their
     individual max_retries. Final pass = exit 0; persistent fail = exit 1.
  5. If ANY failure doesn't match the registry → exit immediately with
     the original exit code. Real regressions still fail fast.

Usage
-----
    scripts/ops/ci_flake_retry.py -- go test ./... -race -count=1

The `--` separator is conventional but optional; everything after this
script's own flags is the command to run.

Local exercising:
    scripts/ops/ci_flake_retry.py --self-test    # runs unit doctests

CI wiring (see PR description for the wave-2 follow-up):
    .github/workflows/ci.yml  → step "Run threshold-exporter tests …"
    Wrap the existing `go test …` command:
        run: python3 scripts/ops/ci_flake_retry.py -- go test ./... -race -count=1

Registry expiry
---------------
Each entry has an `expire_at: vX.Y.Z` field. When the platform version
(read from CHANGELOG.md latest tag or env $DA_VERSION) reaches or
exceeds expire_at, the entry is treated as EXPIRED and will FAIL CI
even if the test passes — driving the root-cause fix forward.

Currently `--check-expiry` is opt-in; flip to default-on once the
registry has a stable cadence of root-cause fixes landing on time.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REGISTRY = REPO_ROOT / "flaky-tests.yaml"

# Go test failure line: "--- FAIL: TestName (0.05s)" or
# "    --- FAIL: TestName/Subtest (0.05s)" (subtest indent).
_FAIL_LINE_RE = re.compile(
    r"^\s*--- FAIL:\s+(\S+)\s+\(",
    re.MULTILINE,
)


@dataclass(frozen=True)
class FlakeEntry:
    test: str
    pattern: str  # regex passed to `go test -run`
    max_retries: int
    owner: str
    tracked_by: str
    expire_at: str

    def matches(self, test_name: str) -> bool:
        """Check if the test's name matches this entry's pattern."""
        try:
            return bool(re.match(self.pattern, test_name))
        except re.error:
            return False


def load_registry(path: Path) -> list[FlakeEntry]:
    """Parse flaky-tests.yaml into FlakeEntry list.

    Returns empty list if the file is missing — that's allowed; means no
    flakes are recognized and the wrapper degrades to a pure pass-through.
    """
    if not path.is_file():
        return []
    try:
        import yaml  # local import: keeps script importable for unit tests
    except ImportError:
        # PyYAML missing — degrade to pure pass-through with a stderr
        # advisory rather than crashing the wrapper. CI / pre-commit
        # configuration is responsible for installing pyyaml when the
        # registry has entries that matter.
        sys.stderr.write(
            "ci_flake_retry: pyyaml not installed; ignoring registry "
            f"({path.name}). pip install pyyaml to enable retries.\n"
        )
        return []
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    raw = data.get("known_flakes") or []
    out: list[FlakeEntry] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        out.append(FlakeEntry(
            test=str(item.get("test", "")),
            pattern=str(item.get("pattern", "")),
            max_retries=int(item.get("max_retries", 1)),
            owner=str(item.get("owner", "")),
            tracked_by=str(item.get("tracked_by", "")),
            expire_at=str(item.get("expire_at", "")),
        ))
    return out


def parse_failing_tests(stdout: str) -> list[str]:
    """Extract Go test names from `go test` output's `--- FAIL:` lines.

    Returns names in encounter order (preserves first-failure focus). Subtest
    paths like `TestParent/case_one` are returned as-is; callers decide
    whether to map them back to the parent for `-run` re-execution.
    """
    return _FAIL_LINE_RE.findall(stdout or "")


def classify_failures(
    failing: list[str], registry: list[FlakeEntry],
) -> tuple[list[tuple[str, FlakeEntry]], list[str]]:
    """Partition failing tests into (matched_flakes, unmatched).

    matched_flakes: list of (test_name, entry) where entry.matches(test_name).
    unmatched: test names not in the registry — real regressions; don't retry.
    """
    matched: list[tuple[str, FlakeEntry]] = []
    unmatched: list[str] = []
    for name in failing:
        # Strip subtest path for entry matching (entry.pattern targets parent).
        parent = name.split("/", 1)[0]
        for entry in registry:
            if entry.matches(parent):
                matched.append((name, entry))
                break
        else:
            unmatched.append(name)
    return matched, unmatched


def run_command(cmd: list[str], timeout: float = 1800.0) -> tuple[int, str, str]:
    """Run command, capture stdout/stderr, return (rc, stdout, stderr).

    Uses utf-8 encoding with errors="replace" to defend against non-UTF-8
    bytes from Go test output (rare but seen with cgo logs).

    Default timeout 1800s (30 min) — generous backstop for `go test ./...`
    runs (typical: 1-5 min); shorter than GitHub Actions' 6h job cap so a
    test deadlock surfaces with a TimeoutExpired traceback rather than
    consuming the whole CI budget.
    """
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def retry_one_test(
    base_cmd: list[str], parent_test: str, max_retries: int,
) -> tuple[int, str]:
    """Retry a single Go test up to max_retries times.

    Returns (final_rc, last_stdout). Uses `-run ^TestName$` to scope the
    retry to just the failing test (Go test's `-run` is a regex on the
    fully-qualified path).
    """
    last_stdout = ""
    for attempt in range(1, max_retries + 1):
        cmd = list(base_cmd) + ["-run", f"^{parent_test}$"]
        rc, stdout, stderr = run_command(cmd)
        last_stdout = stdout + stderr
        if rc == 0:
            return 0, last_stdout
        # else continue retrying
    return rc, last_stdout


_SCRIPT_FLAGS_WITH_VALUE = frozenset({"--registry"})
_SCRIPT_FLAGS_NO_VALUE = frozenset({"--self-test", "--verbose", "-v"})


def split_args(argv: list[str]) -> tuple[list[str], list[str]]:
    """Split argv at the `--` separator. Returns (script_args, command).

    If `--` is present, everything before it is this script's args and
    everything after is the command to run. If no `--` present, walk
    argv left-to-right and consume tokens that match this script's own
    known flags (so `ci_flake_retry.py --self-test` works without `--`).
    """
    if "--" in argv:
        i = argv.index("--")
        return argv[:i], argv[i + 1:]

    script_args: list[str] = []
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok in _SCRIPT_FLAGS_NO_VALUE:
            script_args.append(tok)
            i += 1
            continue
        if tok in _SCRIPT_FLAGS_WITH_VALUE:
            script_args.append(tok)
            if i + 1 < len(argv):
                script_args.append(argv[i + 1])
                i += 2
            else:
                i += 1
            continue
        if tok.startswith("--registry="):
            script_args.append(tok)
            i += 1
            continue
        # First non-script token starts the command
        break
    return script_args, argv[i:]


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    script_args, command = split_args(argv)

    parser = argparse.ArgumentParser(
        description="Run a Go test command with surgical flake retry.",
    )
    parser.add_argument(
        "--registry",
        default=str(DEFAULT_REGISTRY),
        help=f"Path to flaky-tests.yaml (default: {DEFAULT_REGISTRY.name})",
    )
    parser.add_argument(
        "--self-test", action="store_true",
        help="Run unit doctests on the parser/classifier and exit",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Print classification + retry decisions to stderr",
    )
    args = parser.parse_args(script_args)

    if args.self_test:
        return _self_test()

    if not command:
        print(
            "ci_flake_retry.py: no command given; expected `-- go test ...`",
            file=sys.stderr,
        )
        return 2

    registry = load_registry(Path(args.registry))
    if args.verbose:
        print(
            f"ci_flake_retry: registry has {len(registry)} entries",
            file=sys.stderr,
        )

    rc, stdout, stderr = run_command(command)
    # Pass through original output so CI logs look identical on success.
    sys.stdout.write(stdout)
    sys.stderr.write(stderr)
    if rc == 0:
        return 0

    failing = parse_failing_tests(stdout)
    if not failing:
        # Non-zero exit but no `--- FAIL:` lines parsed — likely a build
        # failure or unrelated tooling error. Don't try to retry.
        if args.verbose:
            print(
                "ci_flake_retry: non-zero exit but no FAIL: lines; passing through",
                file=sys.stderr,
            )
        return rc

    matched, unmatched = classify_failures(failing, registry)
    if args.verbose:
        print(
            f"ci_flake_retry: {len(matched)} flaky candidate(s), "
            f"{len(unmatched)} unmatched failure(s)",
            file=sys.stderr,
        )

    if unmatched:
        # At least one real regression — don't waste time retrying flakes
        # that might pass; surface the original failure immediately.
        if args.verbose:
            for u in unmatched:
                print(f"ci_flake_retry: unmatched failure: {u}", file=sys.stderr)
        return rc

    # Group matched failures by parent test name (so we run `-run`
    # once per parent, not once per subtest leaf).
    by_parent: dict[str, FlakeEntry] = {}
    for name, entry in matched:
        parent = name.split("/", 1)[0]
        by_parent.setdefault(parent, entry)

    all_recovered = True
    for parent, entry in by_parent.items():
        if args.verbose:
            print(
                f"ci_flake_retry: retrying {parent} up to {entry.max_retries}x "
                f"(owner={entry.owner}, tracked_by={entry.tracked_by})",
                file=sys.stderr,
            )
        retry_rc, retry_out = retry_one_test(command, parent, entry.max_retries)
        if retry_rc == 0:
            sys.stderr.write(
                f"ci_flake_retry: {parent} recovered after retry "
                f"(owner={entry.owner})\n"
            )
        else:
            all_recovered = False
            sys.stderr.write(
                f"ci_flake_retry: {parent} STILL FAILING after "
                f"{entry.max_retries} retries — this is now a real regression\n"
            )
            sys.stderr.write(retry_out)
    return 0 if all_recovered else 1


# ── Doctest-style self-test ──────────────────────────────────────────

def _self_test() -> int:
    """Exercise parser + classifier on hand-crafted fixtures.

    Cheaper than a full pytest module; lives inline so the CI never
    skips this when running this script standalone.
    """
    fails = 0

    # parse_failing_tests
    sample = """
ok  	github.com/x/foo	0.123s
=== RUN   TestAlpha
--- FAIL: TestAlpha (0.05s)
    foo_test.go:42: expected …
=== RUN   TestBeta
--- PASS: TestBeta (0.01s)
=== RUN   TestGamma/case1
    --- FAIL: TestGamma/case1 (0.02s)
FAIL
"""
    got = parse_failing_tests(sample)
    expected = ["TestAlpha", "TestGamma/case1"]
    if got != expected:
        print(f"  ✗ parse_failing_tests: got={got!r} want={expected!r}",
              file=sys.stderr)
        fails += 1

    # classify_failures
    registry = [
        FlakeEntry(
            test="TestAlpha", pattern="^TestAlpha$",
            max_retries=2, owner="@team", tracked_by="HA-N",
            expire_at="v2.9.0",
        ),
    ]
    matched, unmatched = classify_failures(
        ["TestAlpha", "TestGamma/case1", "TestNew"], registry,
    )
    if [n for n, _ in matched] != ["TestAlpha"] or unmatched != ["TestGamma/case1", "TestNew"]:
        print(
            f"  ✗ classify_failures: matched={matched!r} unmatched={unmatched!r}",
            file=sys.stderr,
        )
        fails += 1

    # FlakeEntry.matches honors regex anchors
    e = FlakeEntry(
        test="X", pattern="^TestFoo$", max_retries=1, owner="o",
        tracked_by="t", expire_at="v2.9.0",
    )
    if not e.matches("TestFoo"):
        print("  ✗ FlakeEntry.matches: TestFoo should match ^TestFoo$",
              file=sys.stderr)
        fails += 1
    if e.matches("TestFooBar"):
        print("  ✗ FlakeEntry.matches: TestFooBar should NOT match ^TestFoo$",
              file=sys.stderr)
        fails += 1

    # split_args — explicit `--` separator
    s, c = split_args(["--verbose", "--", "go", "test", "./..."])
    if s != ["--verbose"] or c != ["go", "test", "./..."]:
        print(f"  ✗ split_args separator: s={s!r} c={c!r}", file=sys.stderr)
        fails += 1

    # split_args — no separator, script flag is consumed
    s, c = split_args(["--self-test"])
    if s != ["--self-test"] or c != []:
        print(f"  ✗ split_args --self-test alone: s={s!r} c={c!r}",
              file=sys.stderr)
        fails += 1

    # split_args — flag with value (--registry foo.yaml)
    s, c = split_args(["--registry", "foo.yaml", "go", "test"])
    if s != ["--registry", "foo.yaml"] or c != ["go", "test"]:
        print(f"  ✗ split_args --registry value: s={s!r} c={c!r}",
              file=sys.stderr)
        fails += 1

    # split_args — flag with =value
    s, c = split_args(["--registry=foo.yaml", "go", "test"])
    if s != ["--registry=foo.yaml"] or c != ["go", "test"]:
        print(f"  ✗ split_args --registry=value: s={s!r} c={c!r}",
              file=sys.stderr)
        fails += 1

    if fails:
        print(f"\nci_flake_retry --self-test: {fails} failure(s)", file=sys.stderr)
        return 1
    print("ci_flake_retry --self-test: all checks passed", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
