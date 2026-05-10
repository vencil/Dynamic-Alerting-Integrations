#!/usr/bin/env python3
"""Add t.Parallel() to top-level Go test functions in safe packages.

Reads each file, finds `func TestXxx(t *testing.T) {` lines that are NOT
followed by an existing `t.Parallel()` call within 5 lines, and inserts
`\tt.Parallel()` as the first statement of the body.

Safety:
- Only operates on files passed on the command line; caller picks safe pkgs
- Skips functions that already have t.Parallel() in their body
- Skips functions that look risky: contains os.Setenv / os.Chdir /
  os.Getenv (process-global state) anywhere in the body
- Idempotent: re-running is a no-op
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# Match `func TestXxx(t *testing.T) {` at start of line.
TEST_FUNC_RE = re.compile(r"^func (Test\w+)\(t \*testing\.T\) \{$")

# Risky patterns: tests using these touch process-global state and CANNOT
# safely run in parallel even within a package.
#
# History:
#   - The first 4 entries (os.Setenv etc.) were the original RISKY guard.
#   - "Metrics.requestsTotal" / "Metrics.errorsTotal" were added after CI
#     run #25602108441 caught a race in TestMetricsMiddleware_CountsRequests
#     ("requestsTotal = 3, want 2") — the 3 TestMetricsMiddleware_* tests
#     read a package-level Prometheus singleton via a before/after delta,
#     which collides under t.Parallel(). The substring match catches all
#     three call sites because they share the "Metrics." prefix.
RISKY = (
    "os.Setenv",
    "os.Unsetenv",
    "os.Chdir",
    "t.Setenv",
    "Metrics.",
    # slog.SetDefault swaps the package-level slog default logger.
    # Tests installing custom slog handler over a captured bytes.Buffer
    # race against any other parallel test that calls production code
    # which logs via slog.Warn / slog.Info — production writes into the
    # captured buffer, racing the test's bytes.Buffer access. Caught
    # by CI run #25605708926 in TestSlogRequestLogger_EmitsStructuredLine
    # → TestPutTenant_DirectMode race; PR #350 tenant-api sweep's missed
    # RISKY case (codified by PR #357).
    "slog.SetDefault",
    # `withIsolatedMetrics(t)` in components/threshold-exporter/app/
    # used to implement a global-swap pattern (saves+sets+restores a
    # package-level `configMetrics` singleton via setConfigMetrics()).
    # Two parallel tests calling it would race on the global, with the
    # second test's `fresh` registry receiving observations from the
    # first test's reload. Caught by PR #356 (config_* sweep) before
    # landing. The helper itself was removed in PR #4a (replaced by
    # freshMetrics + ConfigManager.SetMetrics field injection); kept
    # in RISKY as a tripwire so a future re-introduction surfaces here.
    "withIsolatedMetrics",
    "setConfigMetrics",
    # log.SetOutput / log.SetFlags swap the package-level stdlib logger.
    # Tests that capture log output via `log.SetOutput(&buf)` race on the
    # logger destination if multiple parallel tests do it — and worse, any
    # other parallel test that calls log.Printf will write into whichever
    # buffer happened to be set last, corrupting captured output and
    # tripping the race detector on the &buf bytes.Buffer. Caught by
    # PR #356 when -race surfaced bytes.Buffer races in
    # TestMixedMode_DuplicateAcrossModes_RejectedAtLoad in the Linux
    # dev container.
    #
    # The exporter's package main test files no longer use log.SetOutput
    # (#4b PR-F replaced the 4 capture sites with mgr.SetLogger(testLogger)
    # injection through the ConfigManager.logger seam from #4b PR-D).
    # Kept in RISKY as a tripwire so a future re-introduction surfaces here.
    # config_bench_test.go's silenceLog(b) helper still calls log.SetOutput
    # but it's a *testing.B benchmark scope (single-threaded by design)
    # and would not trigger this lint anyway.
    "log.SetOutput",
    "log.SetFlags",
)


def find_function_body(lines: list[str], start_idx: int) -> tuple[int, int]:
    """Given index of `func ... {` line, return (body_start, body_end_exclusive).

    body_start is `start_idx + 1`; body_end is index of the closing `}`
    line of the function (matched at depth 0).
    """
    depth = 1  # opened by the `{` on the func line
    for i in range(start_idx + 1, len(lines)):
        # Crude but works for well-formatted Go: count braces in code,
        # ignoring those inside strings/comments. Goimports-style files
        # rarely have unbalanced braces in line-internal strings; this
        # heuristic is good enough for Test* func bodies.
        line = lines[i]
        # Strip line-comment portion first to avoid counting `// {`.
        code = line.split("//", 1)[0]
        depth += code.count("{") - code.count("}")
        if depth == 0:
            return start_idx + 1, i
    return start_idx + 1, len(lines)


def process_file(path: Path) -> int:
    """Returns count of injections."""
    text = path.read_text(encoding="utf-8")
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    injected = 0
    while i < len(lines):
        line = lines[i]
        m = TEST_FUNC_RE.match(line)
        if not m:
            out.append(line)
            i += 1
            continue
        # Find function body
        body_start, body_end = find_function_body(lines, i)
        body = "\n".join(lines[body_start:body_end])
        if "t.Parallel()" in body:
            out.append(line)
            i += 1
            continue
        if any(r in body for r in RISKY):
            out.append(line)
            i += 1
            continue
        # Inject t.Parallel() as first statement of body.
        out.append(line)
        out.append("\tt.Parallel()")
        injected += 1
        i += 1
    if injected:
        # Preserve trailing newline (avoids the sed-i truncation problem
        # explicitly: we always end with the same EOF state).
        new_text = "\n".join(out)
        if text.endswith("\n") and not new_text.endswith("\n"):
            new_text += "\n"
        path.write_text(new_text, encoding="utf-8")
    return injected


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: add_t_parallel.py <file_or_dir>...", file=sys.stderr)
        return 2
    total = 0
    for arg in sys.argv[1:]:
        p = Path(arg)
        targets = sorted(p.rglob("*_test.go")) if p.is_dir() else [p]
        for f in targets:
            n = process_file(f)
            if n:
                print(f"{f}: +{n} t.Parallel()")
                total += n
    print(f"Total: {total} injections")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
