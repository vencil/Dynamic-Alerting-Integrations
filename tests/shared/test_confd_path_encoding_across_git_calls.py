#!/usr/bin/env python3
"""#1634 / #3111 — a carrier's NAME must survive every git call `--git-diff` makes.

`backtest_threshold.py --git-diff` is the mode CI runs. Since #3111 it no
longer parses `git diff` text: it writes HEAD~1's `conf.d/` out as a tree
(`git ls-tree -r -z` + `git cat-file --batch`, names kept as BYTES) and lets
the exporter read that tree and the working tree's (`da-guard served-values`),
exactly as `--config-dir` does. The tenant is the `tenants:` key, never the
file name, so a name is only a path.

⛔ WHY THE END-TO-END ARM IS NOT OPTIONAL HERE.

The defect family is "the tenant quietly disappeared from the report". A
direct call of the tree writer cannot see what the report says, so the tests
below run the real CLI, against a real git repo, with a fake Prometheus.

⛔ THE FIXTURE PINS `core.quotepath` ON PURPOSE.

`core.quotepath` defaults to true, which is what made git escape a non-ASCII
path in the diff text the old parser read. Every repo built here pins the
DEFAULT explicitly rather than inherit the operator's `~/.gitconfig`.

What changed with #3111: the four name shapes the diff parser dropped (a
name holding a newline, a quote or a backslash, which git quotes even with
quoting off, and a space, which a diff header pads with a tab) and a name
that is not valid UTF-8 (which cannot be a PromQL label value, and was the
tenant id) now reach the report like any other.
"""
from __future__ import annotations

import http.server
import json
import os
import pathlib
import socketserver
import subprocess
import sys
import threading

import pytest
from _platform_fs import require_file_name  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[2]
TOOLS_DIR = REPO / "scripts" / "tools"
TOOL = TOOLS_DIR / "ops" / "backtest_threshold.py"

_BEFORE = b"tenants:\n  %s:\n    cpu_usage: 80\n    mem_usage: 70\n"
_AFTER_REMOVED = b"tenants:\n  %s:\n    mem_usage: 70\n"
_DEFAULTS = b"defaults:\n  cpu_usage: 90\n  mem_usage: 90\n"
# The removed override falls back to the default: 80 -> 90 on /metrics.
_REMOVAL_ROW = "acme/cpu_usage: 80 -> 90"

# The three carriers this axis is about, as raw BYTES — the point of the
# ticket is names that `str` cannot round-trip, so the fixture may not go
# through `str` on its way to the filesystem.
ASCII_CARRIER = b"db-a.yaml"
NON_ASCII_CARRIER = "İSTANBUL.YAML".encode("utf-8")   # valid UTF-8, non-ASCII
INVALID_UTF8_CARRIER = b"legacy-\xff.yaml"            # not valid UTF-8 at all


class _FakePrometheus(http.server.BaseHTTPRequestHandler):
    """Answers every GET with an empty but well-formed Prometheus result.

    The tool checks `/api/v1/status/buildinfo` before extracting changes,
    so without this the run exits at the availability gate and never
    reaches the code under test.
    """

    def do_GET(self):                                     # noqa: N802
        body = json.dumps({"status": "success",
                           "data": {"result": [], "version": "2.0.0"}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_a):                           # noqa: D102
        pass


@pytest.fixture(scope="module")
def prom_url():
    """A local fake Prometheus, torn down with the module."""
    httpd = socketserver.TCPServer(("127.0.0.1", 0), _FakePrometheus)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def _import_backtest():
    """Import the tool in-process, the way the neighbouring gate file does.

    Only one test needs this — see its comment for why the CLI cannot answer
    the question it asks.
    """
    import importlib  # noqa: PLC0415
    for extra in (str(TOOLS_DIR / "ops"), str(TOOLS_DIR)):
        if extra not in sys.path:
            sys.path.insert(0, extra)
    return importlib.import_module("backtest_threshold")


def _git(root: pathlib.Path, *args: str):
    return subprocess.run(("git",) + args, cwd=str(root),
                          capture_output=True, timeout=60)


def _repo_with_removal(root: pathlib.Path, carriers) -> pathlib.Path:
    """A git repo where each carrier loses `cpu_usage` between HEAD~1 and HEAD.

    `carriers` is a sequence of (relative bytes path, tenant-id bytes).
    Paths are joined as BYTES so a name that is not valid UTF-8 reaches the
    filesystem unchanged.
    """
    (root / "conf.d").mkdir(parents=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.invalid")
    _git(root, "config", "user.name", "t")
    # ⛔ Pin the DEFAULT rather than inherit the operator's ~/.gitconfig:
    # with `core.quotepath=false` set globally this fixture would describe
    # a configuration the customer does not have.
    _git(root, "config", "core.quotepath", "true")
    root_b = os.fsencode(str(root))

    def write(body_template):
        for rel, tenant in carriers:
            require_file_name(rel.rsplit(b"/", 1)[-1])
            path = root_b + b"/conf.d/" + rel
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as handle:
                handle.write(body_template % tenant)

    with open(root_b + b"/conf.d/_defaults.yaml", "wb") as handle:
        handle.write(_DEFAULTS)
    write(_BEFORE)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "before")
    write(_AFTER_REMOVED)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "after")
    return root


def _run_cli(cwd: pathlib.Path, prom: str, *extra: str):
    return subprocess.run(
        [sys.executable, str(TOOL), "--git-diff", "--prometheus", prom, *extra],
        cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)


def _analyzed(stdout: str):
    """The `Changes analyzed: N/M` denominator, or None if no report."""
    for line in (stdout or "").splitlines():
        if "Changes analyzed:" in line:
            return line.split(":", 1)[1].strip()
    return None


_SHAPES = [
    pytest.param(ASCII_CARRIER, id="ascii"),
    pytest.param(NON_ASCII_CARRIER, id="non_ascii"),
    pytest.param(INVALID_UTF8_CARRIER, id="invalid_utf8"),
    pytest.param(b"has\nnewline.yaml", id="newline"),
    pytest.param(b'has"quote.yaml', id="quote"),
    pytest.param(b"has\\backslash.yaml", id="backslash"),
    pytest.param(b"has space.yaml", id="space"),
    pytest.param(b"sub/db-x.yaml", id="nested"),
]


@pytest.mark.usefixtures("da_guard_env")
@pytest.mark.parametrize("carrier", _SHAPES)
def test_a_removal_reaches_the_report_whatever_the_carrier_is_named(
        tmp_path, prom_url, carrier):
    """⛔ The defect as the customer meets it: a threshold REMOVED — an alert
    falling back to the default — answered with "No threshold changes
    found." because of the carrier's NAME. #1634 fixed the ASCII, non-ASCII
    and nested shapes and named the rest as dropped; #3111 reads the two
    trees instead of the diff text, so every shape is reported, under the
    tenant its `tenants:` key names.
    """
    repo = _repo_with_removal(tmp_path / "r", [(carrier, b"acme")])
    result = _run_cli(repo, prom_url)
    assert result.returncode == 0, result.stderr
    assert "Traceback" not in result.stderr, result.stderr
    assert _analyzed(result.stdout) == "0/1", (result.stdout, result.stderr)
    assert _REMOVAL_ROW in result.stdout, (result.stdout, result.stderr)
    assert "dropped" not in result.stderr, result.stderr


@pytest.mark.usefixtures("da_guard_env")
def test_a_head1_body_that_is_not_utf8_is_named_rc2(tmp_path, prom_url):
    """A HEAD~1 carrier the exporter cannot decode is a baseline tree it does
    not read: rc 2, the file named, no traceback — not a partial diff, the
    `--config-dir` rule (#2119). Before #3111 the removal on it was silently
    dropped (rc 0) while a clean neighbour's was reported."""
    repo = tmp_path / "badbody"
    (repo / "conf.d").mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "core.quotepath", "true")
    (repo / "conf.d" / "_defaults.yaml").write_bytes(_DEFAULTS)
    dirty = repo / "conf.d" / "db-a.yaml"
    dirty.write_bytes(b"# legacy \xff marker\n" + (_BEFORE % b"acme"))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "before")
    dirty.write_bytes(b"# clean marker\n" + (_AFTER_REMOVED % b"acme"))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "after")

    result = _run_cli(repo, prom_url, "--json")
    assert "Traceback" not in result.stderr, result.stderr
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert json.loads(result.stdout)["reason"] == "served_values_unavailable"
    assert "db-a.yaml" in result.stderr


def test_ls_tree_lists_nothing_from_a_subdirectory_without_full_tree(tmp_path):
    """⛔ Pins why `materialize_head1_confd` passes `--full-tree`.

    `git ls-tree <rev>:<dir>` still limits its listing to the cwd's own path
    inside the tree it is given, so run from a subdirectory of the repo (this
    repo's CI runs from `components/threshold-exporter/config`) it lists
    nothing, and HEAD~1 would read as an empty conf.d/ — every tenant
    "newly enabled".
    """
    repo = tmp_path / "coords"
    inner = repo / "components" / "x" / "config"
    (inner / "conf.d").mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    (inner / "conf.d" / "db-a.yaml").write_bytes(_BEFORE % b"acme")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "only")

    def listing(*flags):
        return subprocess.run(("git", "ls-tree", "-r", "-z", *flags, "HEAD:./conf.d"),
                              cwd=str(inner), capture_output=True, timeout=60)
    plain, full = listing(), listing("--full-tree")
    assert plain.returncode == 0 and plain.stdout == b"", (
        f"`ls-tree` from a subdirectory now lists the tree, so `--full-tree` "
        f"may no longer be load-bearing: {plain.stdout!r}")
    assert full.stdout.endswith(b"\tdb-a.yaml\0"), full.stdout


def test_the_dedup_is_process_wide_and_the_reset_clears_it(capsys):
    """`_warn_dropped_carrier` names a carrier once per run; the reset clears it.

    Since #3111 no change source hands the tool a tenant id that cannot be a
    PromQL label value (the ids are the exporter's `tenants:` keys), so the
    end-to-end arm that used to drive this is gone; the gate stays in front
    of the query, and this drives its dedup in-process.

    ⛔ It also exists because `reset_dropped_carrier_warnings_for_test` had
    ZERO callers — a test-only setter nobody calls is dead code by this
    repo's own self-review rule, and its neighbour
    (`reset_unlistable_warnings_for_test`) does have one. The coverage bot
    is what surfaced it: every branch this file guards runs in a SUBPROCESS,
    and coverage cannot see into one, so the whole group reads as uncovered
    no matter how thoroughly the CLI exercises it.
    """
    bt = _import_backtest()
    bt.reset_dropped_carrier_warnings_for_test()
    try:
        bt._warn_dropped_carrier("carrier-x", "a reason")
        bt._warn_dropped_carrier("carrier-x", "a reason")
        said_twice = capsys.readouterr().err
        assert said_twice.count("carrier-x") == 1, (
            f"the second call was not suppressed: {said_twice!r}")

        bt.reset_dropped_carrier_warnings_for_test()
        bt._warn_dropped_carrier("carrier-x", "a reason")
        after_reset = capsys.readouterr().err
        assert after_reset.count("carrier-x") == 1, (
            f"the reset did not clear the dedup, so a test cannot drive this "
            f"branch twice: {after_reset!r}")
    finally:
        # ⚠️ Idempotent reset, not save-and-restore: this set is process
        # global, and leaving an entry behind would silence a warning a later
        # test is asserting on.
        bt.reset_dropped_carrier_warnings_for_test()


def test_both_change_sources_answer_an_unusable_name_without_a_traceback(
        tmp_path, prom_url, da_guard_env):
    """⛔ The anti-#1911 assertion: one carrier, two change sources, one answer.

    Both read the tenant from the file's `tenants:` mapping through the
    exporter (#2119, #3111), so a carrier whose name is not valid UTF-8 is
    reported the same way by both, under its tenant `acme`. Before #3111
    `--git-diff` took the tenant from the NAME and dropped it.
    """
    repo = _repo_with_removal(tmp_path / "src_git",
                              [(INVALID_UTF8_CARRIER, b"acme")])
    from_git = _run_cli(repo, prom_url)

    cur, base = tmp_path / "cur", tmp_path / "base"
    for directory, body in ((cur, _AFTER_REMOVED), (base, _BEFORE)):
        directory.mkdir(parents=True)
        (directory / "_defaults.yaml").write_text(
            "defaults:\n  cpu_usage: 90\n  mem_usage: 90\n", encoding="utf-8")
        with open(os.fsencode(str(directory)) + b"/" + INVALID_UTF8_CARRIER,
                  "wb") as handle:
            handle.write(body % b"acme")
    from_dirs = subprocess.run(
        [sys.executable, str(TOOL), "--config-dir", str(cur),
         "--baseline", str(base), "--prometheus", prom_url],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)

    for label, result in (("--git-diff", from_git), ("--config-dir", from_dirs)):
        assert result.returncode == 0, f"[{label}] rc={result.returncode} {result.stderr!r}"
        assert "Traceback" not in result.stderr, f"[{label}] {result.stderr!r}"
        assert _REMOVAL_ROW in result.stdout, f"[{label}] {result.stdout!r} {result.stderr!r}"
