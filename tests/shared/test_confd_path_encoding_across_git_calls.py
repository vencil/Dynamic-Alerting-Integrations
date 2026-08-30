#!/usr/bin/env python3
"""#1634 — a carrier's NAME must survive every git call `--git-diff` makes.

`backtest_threshold.py --git-diff` is the mode CI runs. It asks git five
separate questions, and a conf.d filename travels through all of them:

    extract_changes_from_git_diff   git diff --unified=0      decode
    changed_conf_files              git diff --name-only      decode
    _carrier_at_head1               git ls-tree               decode
    _flat_keys_at_head1  fast path  git show HEAD~1:<path>    ENCODE
    _flat_keys_at_head1  fallback   git show HEAD~1:<path>    ENCODE

⛔ WHY THE END-TO-END ARM IS NOT OPTIONAL HERE.

The previous attempt at this axis fixed `_carrier_at_head1` alone and
shipped a test that called that helper directly. The test was green and
the tool bought nothing: the two callers upstream dropped the carrier
before the helper was ever reached. A direct-call test on a helper that
nobody can reach is indistinguishable from a fix.

There is now a second reason, and it points the other way. With those
upstream sites fixed, a tenant id can for the first time carry a
surrogate (from `surrogateescape`), and it then flows into a PromQL query
string, where `urlencode` raises `UnicodeEncodeError` — *after* the
Prometheus reachability check, so `--skip-if-unavailable` does not
contain it. That crash lives past `main()`; no in-process call of the
parsing helpers can see it. So the tests below run the real CLI, against
a real git repo, with a fake Prometheus.

⛔ THE FIXTURE PINS `core.quotepath` ON PURPOSE.

`core.quotepath` defaults to true, which is what makes git escape a
non-ASCII path — the whole defect. Many developers set it to false in
`~/.gitconfig`, and a fixture that inherits that setting tests a
configuration the customer does not have, going green on the machine of
whoever set it. Every repo built here pins it to the DEFAULT explicitly.

⛔ WHAT IS DELIBERATELY NOT FIXED (and is therefore asserted as audible).

With quoting off, git still quotes a path holding a newline, a double
quote or a backslash, and pads a space-bearing path in a unified diff
header with a tab. Those carriers are still dropped. The assertions below
require that each is NAMED on stderr, because "the tenant quietly
disappeared from the report" is the symptom this whole family is about —
an unfixable case that says so is a different thing from a silent one.
"""
from __future__ import annotations

import http.server
import json
import os
import pathlib
import re
import socketserver
import subprocess
import sys
import threading

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
TOOLS_DIR = REPO / "scripts" / "tools"
TOOL = TOOLS_DIR / "ops" / "backtest_threshold.py"

_BEFORE = b"tenants:\n  %s:\n    cpu_usage: 80\n    mem_usage: 70\n"
_AFTER_REMOVED = b"tenants:\n  %s:\n    mem_usage: 70\n"

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
            path = root_b + b"/conf.d/" + rel
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as handle:
                handle.write(body_template % tenant)

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
        cwd=str(cwd), capture_output=True, text=True, timeout=120)


def _analyzed(stdout: str):
    """The `Changes analyzed: N/M` denominator, or None if no report."""
    for line in (stdout or "").splitlines():
        if "Changes analyzed:" in line:
            return line.split(":", 1)[1].strip()
    return None


def test_non_ascii_carrier_removal_reaches_the_report(tmp_path, prom_url):
    """⛔ The defect as the customer meets it: a tenant named in a
    non-ASCII script has a threshold REMOVED — an alert switched off — and
    the tool answers "No threshold changes found."

    Both arms are byte-identical apart from the carrier's name.
    """
    arms = {}
    for arm, carrier in (("ascii", ASCII_CARRIER),
                         ("non_ascii", NON_ASCII_CARRIER)):
        repo = _repo_with_removal(tmp_path / arm, [(carrier, b"acme")])
        result = _run_cli(repo, prom_url)
        arms[arm] = (result.returncode, _analyzed(result.stdout))

    # ⛔ VACUITY GUARD FIRST. A run that finds nothing in BOTH arms would
    # "agree" and prove nothing — that is exactly how an earlier round of
    # this family read 0 vs 0 as parity.
    assert arms["ascii"] == (0, "0/1"), (
        f"the control arm found no change, so this fixture cannot "
        f"discriminate: {arms['ascii']}")
    assert arms["non_ascii"] == arms["ascii"], (
        f"the non-ASCII carrier's removal did not reach the report: "
        f"{arms['non_ascii']} != {arms['ascii']}")


def test_nested_carrier_removal_is_resolved_at_head1(tmp_path, prom_url):
    """`git diff -- conf.d/` is recursive; `git ls-tree` without `-r` is not.

    A tenant parsed from a nested carrier could therefore never be resolved
    back to its HEAD~1 path, and the REMOVAL direction was dropped — while
    the ADD direction accepted the same carrier. One file, two answers.
    """
    repo = _repo_with_removal(tmp_path / "nested",
                              [(b"sub/db-x.yaml", b"acme")])
    result = _run_cli(repo, prom_url)
    assert result.returncode == 0, result.stderr
    assert _analyzed(result.stdout) == "0/1", (
        f"the nested carrier's removal was dropped; stderr={result.stderr!r}")


def test_invalid_utf8_carrier_is_dropped_but_says_so(tmp_path, prom_url):
    """A name that is not valid UTF-8 cannot become a PromQL label value.

    ⛔ This test exists because fixing the sites above made this case
    REACHABLE: before, the carrier was dropped upstream and the run ended
    quietly; after, its tenant id reached `urlencode` and the tool died
    with `UnicodeEncodeError` — past the availability gate, so
    `--skip-if-unavailable` could not contain it, and with rc=1
    indistinguishable from a real violation.

    The carrier is still dropped. What must not happen is that it is
    dropped silently, or that the drop takes the process down with it.
    """
    repo = _repo_with_removal(tmp_path / "badbyte",
                              [(INVALID_UTF8_CARRIER, b"acme")])
    result = _run_cli(repo, prom_url)
    assert result.returncode == 0, (
        f"the run must not die on an unusable name: {result.stderr!r}")
    assert "Traceback" not in result.stderr, (
        f"an unhandled exception escaped: {result.stderr!r}")
    assert "not valid UTF-8" in result.stderr, (
        f"the dropped carrier was not named: {result.stderr!r}")


def test_an_unusable_carrier_is_named_once_not_once_per_metric(
        tmp_path, prom_url):
    """⛔ The drop is decided per CHANGE, so one carrier losing two metrics
    would say the same thing twice without the once-per-run dedup.

    This file's neighbour gate already settled the principle — a warning
    repeated per row trains the operator to skip it, and then the report's
    reason for existing goes with it. The dedup was written for that, but
    the mutation battery found it had no guard: deleting it left every gate
    file green, because every other fixture here drops exactly one metric.
    """
    repo = tmp_path / "twice"
    (repo / "conf.d").mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "core.quotepath", "true")
    path = os.fsencode(str(repo)) + b"/conf.d/" + INVALID_UTF8_CARRIER
    # Two thresholds removed and NOTHING added: an added key is a change too,
    # so an `after` body that introduces one makes the count 3 rather than 2.
    # ⚠️ The first version of this fixture wrote `other_key: 1` here and the
    # comment claimed "two changes"; blind review measured 3. The number was
    # asserted, not counted — the very habit the rest of this change exists to
    # correct. The count is now pinned below rather than described.
    with open(path, "wb") as handle:
        handle.write(_BEFORE % b"acme")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "before")
    with open(path, "wb") as handle:
        handle.write(b"tenants:\n  acme:\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "after")

    # ⛔ THE DISCRIMINATION GUARD, and it deliberately does NOT go through the
    # warning count. The warning count is the thing the dedup suppresses, so
    # using it to prove the fixture can discriminate would be proving the
    # premise with the conclusion: if this tree ever yields ONE change,
    # `said == 1` passes with the dedup and without it, and the test decays
    # into a vacuous one silently. Count the changes at the source instead.
    diff = subprocess.run(
        ("git", "diff", "HEAD~1", "--unified=0", "--", "conf.d/"),
        cwd=str(repo), capture_output=True, timeout=60).stdout
    changed = [ln for ln in diff.splitlines()
               if re.match(rb"^[-+]\s+\w+:\s+.+$", ln)]
    assert len(changed) >= 2, (
        f"this tree yields {len(changed)} change line(s); with fewer than two "
        f"the dedup cannot be observed at all: {changed!r}")

    result = _run_cli(repo, prom_url)
    assert result.returncode == 0, result.stderr
    said = result.stderr.count("not valid UTF-8")
    assert said == 1, (
        f"the same carrier was named {said} times — once per metric rather "
        f"than once per run: {result.stderr!r}")


@pytest.mark.parametrize("carrier,needle", [
    pytest.param(b"has\nnewline.yaml", "git quoted it", id="newline"),
    pytest.param(b'has"quote.yaml', "git quoted it", id="quote"),
    pytest.param(b"has\\backslash.yaml", "git quoted it", id="backslash"),
    pytest.param(b"has space.yaml", "padded a space", id="space"),
])
def test_a_name_this_parser_cannot_read_is_named_not_swallowed(
        tmp_path, prom_url, carrier, needle):
    """⛔ The four shapes #1634 deliberately does NOT fix.

    With `core.quotepath=false` git still quotes a name holding a newline,
    a quote or a backslash, and pads a space-bearing name in the unified
    diff header with a tab. Un-escaping those is a parser of its own, and
    for a NEWLINE it cannot work here at all — the `+++` header is split
    across two lines, so a line-oriented scan has already lost it.

    They stay dropped. They may not stay silent: this asserts the drop is
    announced, which is the difference between a known gap and the family's
    original charge.
    """
    repo = _repo_with_removal(tmp_path / "weird", [(carrier, b"acme")])
    result = _run_cli(repo, prom_url)
    assert result.returncode == 0, result.stderr
    assert "Traceback" not in result.stderr, result.stderr
    assert needle in result.stderr, (
        f"a carrier this tool cannot parse vanished quietly: "
        f"{result.stderr!r}")


@pytest.mark.parametrize("dirty_rel", [
    pytest.param(b"db-a.yaml", id="fast_path"),
    pytest.param(b"sub/db-a.yaml", id="fallback"),
])
def test_a_head1_body_that_is_not_utf8_does_not_take_the_run_down(
        tmp_path, prom_url, dirty_rel):
    """⛔ The two `git show` calls read file CONTENT, not a path — and this
    case is REACHABLE only because the sites above were fixed.

    Before, a carrier whose body held an invalid byte was dropped upstream
    (or its tenant never resolved), so `git show` was never reached with
    one. Now a removal routes straight into `_flat_keys_at_head1`, and with
    `text=True` that call raises `UnicodeDecodeError` from inside
    `subprocess.run` — which is NOT in the tool's
    `except (TimeoutExpired, FileNotFoundError)`.

    ⛔ This test exists because the mutation battery found the gap: putting
    `text=True` back on either `git show` left every other gate file green.
    A fix whose only evidence is the fixer's own reasoning is not covered.

    The carrier NAME here is deliberately plain ASCII, so nothing but the
    body encoding can be what this pins.

    ⛔ THE BAD BYTE IS IN HEAD~1 ONLY, and that placement is the test.
    The first version of this fixture put it in BOTH commits, so the WORKING
    TREE held it too — and `load_conf_files` reads the working tree through
    `load_yaml_file`, which opens in text mode and dies there first. Measured:
    that arm raises the identical `UnicodeDecodeError` on base and on this
    tree, from `_lib_io.py`, i.e. it is a PRE-EXISTING defect on a different
    axis (a conf.d body that is not UTF-8 at all) and it masked the call this
    test is for — the same "the upstream dies before the thing under test is
    reached" shape this file's header describes. That axis is not fixed here.

    With the byte only in HEAD~1 the arms separate cleanly (measured):

        base  rc=1  UnicodeDecodeError
        here  rc=0  no traceback

    The removal on the dirty carrier is still DROPPED — `yaml.safe_load` on
    bytes answers `ReaderError`, a `YAMLError`, which the pre-existing handler
    turns into an empty key set. Crash became the pre-existing silent skip,
    which is exactly what this change claims and no more.
    """
    repo = tmp_path / "badbody"
    (repo / "conf.d").mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "core.quotepath", "true")
    # ⛔ BOTH `git show` call sites, and they need different fixtures. The
    # fast path spells the carrier from the tenant id; only when that spelling
    # MISSES does the fallback (resolve the real path, then show it) run. A
    # top-level carrier never reaches the fallback, so pinning the fast path
    # alone leaves the second call unguarded — measured: with only the
    # top-level arm, putting `text=True` back on the fallback left this file
    # green. The nested arm makes the fast-path spelling
    # (`conf.d/db-a.yaml`) miss, so the fallback runs.
    dirty = repo / "conf.d" / os.fsdecode(dirty_rel)
    dirty.parent.mkdir(parents=True, exist_ok=True)
    # ⛔ VACUITY GUARD, and it has to be a whole second carrier: with only the
    # dirty one present, a run that classified NO removal at all would also
    # produce "no traceback, rc=0" and the test would pass without ever
    # reaching `git show`. The clean neighbour proves the removal machinery
    # ran, and that only the dirty carrier was dropped.
    clean = repo / "conf.d" / "db-b.yaml"
    dirty.write_bytes(b"# legacy \xff marker\n" + (_BEFORE % b"acme"))
    clean.write_bytes(_BEFORE % b"other")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "before")
    dirty.write_bytes(b"# clean marker\n" + (_AFTER_REMOVED % b"acme"))
    clean.write_bytes(_AFTER_REMOVED % b"other")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "after")

    result = _run_cli(repo, prom_url)
    assert "Traceback" not in result.stderr, (
        f"an undecodable HEAD~1 body took the run down: {result.stderr!r}")
    assert result.returncode == 0, result.stderr
    assert _analyzed(result.stdout) == "0/1", (
        f"expected exactly the clean carrier's removal to survive; the "
        f"machinery may not have run at all: {result.stdout!r}")
    assert "db-b/cpu_usage" in result.stdout, (
        f"the clean neighbour's removal is missing, so this fixture is not "
        f"discriminating: {result.stdout!r}")
    assert "db-a/cpu_usage" not in result.stdout, (
        f"the undecodable HEAD~1 body was parsed anyway: {result.stdout!r}")


def test_z_does_not_unquote_a_unified_diff_header(tmp_path):
    """⛔ Pins the measurement that decides `quotepath_off` vs `-z`.

    `-z` suppresses quoting for `--name-only` and `ls-tree`. It does
    NOTHING to the `+++` header of a unified diff. A future reader
    "simplifying" `extract_changes_from_git_diff` onto `-z` for consistency
    with its two neighbours would silently restore the defect, and no
    behavioural test above would explain why — this one states the reason
    in git's own output.
    """
    repo = _repo_with_removal(tmp_path / "zflag",
                              [(NON_ASCII_CARRIER, b"acme")])

    def headers(*args):
        out = subprocess.run(("git",) + args, cwd=str(repo),
                             capture_output=True, timeout=60).stdout
        return [ln for ln in out.splitlines() if ln.startswith(b"+++")]

    plain = headers("diff", "HEAD~1", "--unified=0", "--", "conf.d/")
    with_z = headers("diff", "-z", "HEAD~1", "--unified=0", "--", "conf.d/")
    unquoted = headers("-c", "core.quotepath=false", "diff", "HEAD~1",
                       "--unified=0", "--", "conf.d/")

    assert plain and plain == with_z, (
        f"`-z` changed the unified diff header, so the reason this file "
        f"uses `core.quotepath=false` no longer holds: {plain} vs {with_z}")
    assert unquoted != plain, (
        f"`core.quotepath=false` did not unquote the header, so this "
        f"fixture is not reproducing the defect: {unquoted}")
    assert any(NON_ASCII_CARRIER in h for h in unquoted), (
        f"the raw carrier bytes are not in the unquoted header: {unquoted}")


def test_ls_tree_is_cwd_relative_while_diff_is_repo_root_relative(tmp_path):
    """⛔ Pins why `_carrier_at_head1` returns `./` + the FULL entry.

    The two commands answer in different coordinate systems, and
    `git show <rev>:<path>` reads the prefix to decide which one it is
    given. Dropping the `./`, or rebuilding the path from its basename,
    each breaks a different case — and both look harmless in review.
    """
    # ⛔ The repo root must be the OUTER directory and `conf.d` must sit
    # under a subtree — that asymmetry IS the thing under test, and this
    # repo's own layout (`components/threshold-exporter/config/conf.d`) is
    # exactly that shape, which is also how CI invokes the tool. The first
    # version of this fixture ran `git init` in the inner directory, so the
    # repo root and the cwd coincided, `HEAD~1:conf.d/...` resolved, and the
    # test failed for the right reason: it was not building the case it
    # describes.
    repo = tmp_path / "coords"
    inner = repo / "components" / "x" / "config"
    (inner / "conf.d").mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "core.quotepath", "true")
    carrier = inner / "conf.d" / "db-a.yaml"
    carrier.write_bytes(_BEFORE % b"acme")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "before")
    carrier.write_bytes(_AFTER_REMOVED % b"acme")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "after")

    diff_out = subprocess.run(
        ("git", "diff", "-z", "--name-only", "HEAD~1", "--", "conf.d/"),
        cwd=str(inner), capture_output=True, timeout=60).stdout
    tree_out = subprocess.run(
        ("git", "ls-tree", "-r", "-z", "--name-only", "HEAD~1", "./conf.d/"),
        cwd=str(inner), capture_output=True, timeout=60).stdout

    tree_entry = [b for b in tree_out.split(b"\0") if b][0]
    assert tree_entry.startswith(b"conf.d/"), (
        f"`ls-tree` stopped answering cwd-relative: {tree_entry!r}")

    ok = subprocess.run(("git", "show", b"HEAD~1:./" + tree_entry),
                        cwd=str(inner), capture_output=True, timeout=60)
    no_prefix = subprocess.run(("git", "show", b"HEAD~1:" + tree_entry),
                               cwd=str(inner), capture_output=True, timeout=60)
    assert ok.returncode == 0, (
        f"`./` + the ls-tree entry stopped resolving: {ok.stderr!r}")
    assert no_prefix.returncode != 0, (
        "the `./` prefix became optional, so the comment explaining it is "
        "now wrong — check before deleting either")
    assert diff_out, "the diff arm produced nothing, so this fixture is vacuous"


def test_both_change_sources_drop_an_unusable_name_the_same_way(
        tmp_path, prom_url):
    """⛔ The anti-#1339 assertion: one carrier, one answer, two enumerators.

    `--git-diff` and `--config-dir` reach tenant ids by different routes
    (git, and `os.listdir`). Guarding only the first would leave them
    disagreeing about the same file — which is the shape this entire family
    exists to remove, and which this chain has already produced by fixing
    one path of a pair.
    """
    repo = _repo_with_removal(tmp_path / "src_git",
                              [(INVALID_UTF8_CARRIER, b"acme")])
    from_git = _run_cli(repo, prom_url)

    cur, base = tmp_path / "cur", tmp_path / "base"
    for directory, body in ((cur, _AFTER_REMOVED), (base, _BEFORE)):
        directory.mkdir(parents=True)
        with open(os.fsencode(str(directory)) + b"/" + INVALID_UTF8_CARRIER,
                  "wb") as handle:
            handle.write(body % b"acme")
    from_dirs = subprocess.run(
        [sys.executable, str(TOOL), "--config-dir", str(cur),
         "--baseline", str(base), "--prometheus", prom_url],
        capture_output=True, text=True, timeout=120)

    for label, result in (("--git-diff", from_git), ("--config-dir", from_dirs)):
        assert result.returncode == 0, f"[{label}] rc={result.returncode} {result.stderr!r}"
        assert "Traceback" not in result.stderr, f"[{label}] {result.stderr!r}"
        assert "not valid UTF-8" in result.stderr, (
            f"[{label}] the carrier was dropped without being named: "
            f"{result.stderr!r}")
