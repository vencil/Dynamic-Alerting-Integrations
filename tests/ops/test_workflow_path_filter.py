"""scripts/ops/workflow_path_filter.py, on measurements by scripts/ops/atime_trace.py (#3756).

A workflow's `on.pull_request.paths` is generated from what its jobs were
measured to read — by access time, so whichever process did the reading — and
the job's last step fails when it reads something the list does not cover.
Each test below is one way that chain could go quiet:

  * a state file that measured nothing, or was never finished, read as a list;
  * the runtime check passing with an entry removed from the region;
  * `generate --check` passing with the region and the trace out of step, or
    with a job that only LOOKS wired;
  * a pattern that matches nothing, or one the matcher cannot read, passing —
    and the second being reported as the first.

Everything but the last section runs on a synthetic repository in `tmp_path`
(file names start with `z` so no scanner mistakes one for a real repo path),
and every trace there comes from a REAL measurement: `atime_trace.py reset`,
the job's command, `atime_trace.py finish`. ⛔ A filesystem that does not
record reads fails these tests (`_measure`) — it does not skip them: "could
not measure" must never read as "measured, nothing wrong". The last section
asks the same questions of the real tree.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
OPS = ROOT / "scripts" / "ops"
SCRIPT = OPS / "workflow_path_filter.py"
ATIME = OPS / "atime_trace.py"

sys.path.append(str(OPS))
import paths_filter_glob  # noqa: E402
import workflow_path_filter as wpf  # noqa: E402

from _lib_exitcodes import CallerError  # noqa: E402  (scripts/tools, on sys.path via wpf)

# The job's processes, and the tools': no bytecode (a `.pyc` written by one
# measurement would hide the `.py` from the next — the boundary the tool's
# docstring names), and none of the variables a pytest session may carry.
_ENV = {k: v for k, v in os.environ.items()
        if k not in ("PYTHONUNBUFFERED", "PYTHONPATH", "DA_TEST_CRASH_EXIT_CODE",
                     # a git hook running pytest exports these; git in a
                     # fixture repository must not follow them out of it
                     "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
                     "GIT_OBJECT_DIRECTORY", "GIT_NAMESPACE", "GIT_PREFIX")}


@pytest.fixture(autouse=True)
def _no_inherited_git_location(monkeypatch):
    """The in-process calls read os.environ, not _ENV."""
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
                 "GIT_OBJECT_DIRECTORY", "GIT_NAMESPACE", "GIT_PREFIX"):
        monkeypatch.delenv(name, raising=False)


_ENV["PYTHONDONTWRITEBYTECODE"] = "1"

WORKFLOW = "zflow.yaml"
TEXT = """\
name: zflow   # a comment that must survive
on:
  pull_request:
    paths:
      # hand-written: the data the job scans
      - "zdata/**"
      # BEGIN GENERATED path-trace (do not edit)
      # END GENERATED path-trace
  workflow_dispatch:
jobs:
  zjob:
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@v6
      # BEGIN GENERATED path-trace-env job=zjob
      # END GENERATED path-trace-env
      - run: python3 ztool/zmain.py
      # BEGIN GENERATED path-trace-assert job=zjob
      # END GENERATED path-trace-assert
"""
MAIN = """\
import os, subprocess, sys
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, os.pardir)
for d in ('zlib', 'zdata', ''):
    sys.path.insert(0, os.path.join(ROOT, d))
import zdirect
import zplug
import zpkg.zmod
open(os.path.join(ROOT, 'zdata', 'zconfig.txt')).read()
open(os.path.join(ROOT, 'zlib', 'zopened.txt')).read()
os.close(os.open(os.path.join(ROOT, 'zlib', 'zunread.txt'), os.O_RDONLY))
for name in ('zlib/zcat.txt', 'zapp/zpkg/zsrc.go'):
    subprocess.run(['cat', os.path.join(ROOT, name)], stdout=subprocess.DEVNULL, check=True)
if len(sys.argv) > 1:
    import zlazy
"""
FILES = {
    f".github/workflows/{WORKFLOW}": TEXT,
    "ztool/zmain.py": MAIN,
    "zlib/zdirect.py": "VALUE = 1\n",
    "zlib/zlazy.py": "def f():\n    import zdeep\n",
    "zlib/zdeep.py": "VALUE = 3\n",
    "zlib/zhidden.py": "VALUE = 6\n",
    "zlib/zunrelated.py": "VALUE = 4\n",
    "zlib/zopened.txt": "x\n",
    "zlib/zunread.txt": "x\n",
    "zlib/zcat.txt": "x\n",
    "zpkg/__init__.py": "VALUE = 0\n",
    "zpkg/zmod.py": "def f():\n    from . import zsibling\n",
    "zpkg/zsibling.py": "VALUE = 5\n",
    "zdata/zconfig.txt": "y\n",
    "zdata/zplug.py": "def f():\n    import zhidden\n",
    "zapp/zpkg/zsrc.go": "package zpkg\n",
    "zapp/zpkg/zother_test.go": "package zpkg\n",
    "zapp/zpkg/znotes.md": "x\n",
    "zapp/zelse/zelse.go": "package zelse\n",
    ".gitignore": "__pycache__/\n",
}
# What `generate` must derive for the synthetic job, and why each is there.
EXPECTED_BLOCK = {
    f".github/path-traces/{WORKFLOW}.json", f".github/workflows/{WORKFLOW}",
    "ztool/zmain.py", "zlib/zdirect.py",
    "zlib/zopened.txt",           # open().read()
    "zlib/zcat.txt",              # read by a CHILD that is not Python
    "zpkg/__init__.py", "zpkg/zmod.py",
    "zpkg/zsibling.py",           # literal RELATIVE import inside a function
    "zlib/zlazy.py",              # literal import on a branch the job never took
    "zlib/zdeep.py",              # …and the import inside zlazy's function
    "zlib/zhidden.py",            # literal import inside a hand-covered `.py`
    "zapp/zpkg/*.go",             # a `.go` file read → its package directory
}
# What the measured job read. `zlib/zunread.txt` is not in it: opened and
# closed, never read.
EXPECTED_READ = {
    "ztool/zmain.py", "zlib/zdirect.py", "zdata/zplug.py", "zpkg/__init__.py",
    "zpkg/zmod.py", "zdata/zconfig.txt", "zlib/zopened.txt", "zlib/zcat.txt",
    "zapp/zpkg/zsrc.go",
}


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True,
                   stdin=subprocess.DEVNULL, timeout=120, env=_ENV)


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch) -> Path:
    """A git repository with one traced workflow and a tool whose reads cover
    every way a file can reach the list (see EXPECTED_BLOCK)."""
    repo = tmp_path / "zrepo"
    for rel, text in FILES.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    # The real ledgers name real workflows; this tree has its own.
    monkeypatch.setattr(wpf, "ADVISORY_PATH_GATES", {})
    monkeypatch.setattr(wpf, "READ_ASSERTION_ORDER", ())
    monkeypatch.setattr(wpf, "RESERVED_PATTERNS", {})
    monkeypatch.setattr(wpf, "MACHINERY", ())
    return repo.resolve()


pytestmark = pytest.mark.skipif(
    os.name == "nt",
    reason="the access-time measurement these tests run for real is defined "
           "for the Linux CI runner")


def _atime(repo: Path, *argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-I", str(ATIME), "--root", str(repo), *argv], cwd=repo,
        env=_ENV, capture_output=True, text=True, encoding="utf-8", errors="replace",
        stdin=subprocess.DEVNULL, timeout=120)


def _measure(repo: Path, state: Path, *argv: str, script: str = "ztool/zmain.py") -> None:
    """reset → the job's command → finish, for real. FAILS when the filesystem
    under pytest's tmp_path does not record reads."""
    done = _atime(repo, "reset", "--state", str(state))
    assert done.returncode == 0, done.stderr
    subprocess.run([sys.executable, script, *argv], cwd=repo, env=_ENV, check=True,
                   stdin=subprocess.DEVNULL, timeout=120)
    done = _atime(repo, "finish", "--state", str(state))
    if done.returncode != 0 and "CANARY-FAILED" in done.stderr:
        pytest.fail(
            "COULD NOT MEASURE: the filesystem holding pytest's tmp_path does "
            "not record reads the way atime_trace.py needs, so none of these "
            "tests can tell a working tool from a broken one. This is not a "
            f"pass and not a skip.\n{done.stderr}")
    assert done.returncode == 0, done.stderr


def _main(repo: Path, *argv: str) -> int:
    return wpf.main(["--root", str(repo), *argv])


def _job_args(state: Path) -> tuple[str, ...]:
    return ("--workflow", WORKFLOW, "--job", "zjob", "--state", str(state))


def _record_and_generate(repo: Path, state: Path) -> None:
    _measure(repo, state)
    assert _main(repo, "record", *_job_args(state)) == 0
    _git(repo, "add", "-A")
    assert _main(repo, "generate") == 0


def _workflow(repo: Path) -> Path:
    return repo / ".github/workflows" / WORKFLOW


def _trace_file(repo: Path) -> Path:
    return repo / ".github/path-traces" / f"{WORKFLOW}.json"


def _block(repo: Path) -> list[str]:
    return wpf.Traced(repo, _workflow(repo), wpf.tracked_files(repo)).block


def _edit(repo: Path, old: str, new: str) -> None:
    text = _workflow(repo).read_text(encoding="utf-8")
    assert text.count(old) == 1, old
    _workflow(repo).write_text(text.replace(old, new), encoding="utf-8")


def _read(repo: Path, state: Path) -> set[str]:
    return wpf.read_state(repo, state, frozenset(wpf.tracked_files(repo)))


# ── the measurement, as this tool reads it ──────────────────────────────────

def test_the_read_set_is_what_the_job_read_whichever_process_read_it(
        repo: Path, tmp_path: Path) -> None:
    """Measured TWICE over the same tree: `finish` puts nothing back that the
    next `reset` does not set again, so a second measurement is the first.
    `zlib/zcat.txt` and the `.go` file were read by `cat`, not by Python."""
    for state in (tmp_path / "s1.json", tmp_path / "s2.json"):
        _measure(repo, state)
        assert _read(repo, state) == EXPECTED_READ


def test_the_record_keeps_python_files_and_drops_other_hand_covered_ones(
        repo: Path, tmp_path: Path) -> None:
    """`zdata/**` is hand-written. `zdata/zconfig.txt` is left out of the
    trace (a job that reads a whole tree would make every file added there a
    change to it); `zdata/zplug.py` is NOT — the import written in it
    (`zhidden`, never executed) is found from the trace, and only from there."""
    state = tmp_path / "s.json"
    _record_and_generate(repo, state)
    job = json.loads(_trace_file(repo).read_text(encoding="utf-8"))["jobs"]["zjob"]
    assert sorted(job) == ["files", "import_dirs"]
    assert job["files"] == sorted(EXPECTED_READ - {"zdata/zconfig.txt"})
    assert job["import_dirs"] == ["zdata", "zlib", "zpkg", "ztool"]
    block = _block(repo)
    assert "zlib/zhidden.py" in block
    # In the trace, and not in the block: the hand-written pattern has it.
    assert "zdata/zplug.py" not in block and "zdata/zconfig.txt" not in block


def test_a_go_file_read_is_listed_as_its_package_directory(
        repo: Path, tmp_path: Path) -> None:
    """One `.go` file was read. The entry is `<dir>/*.go`: the `_test.go`
    beside it is covered (wider than the read — the safe direction), the
    notes file there and the package next door are not."""
    _record_and_generate(repo, tmp_path / "s.json")
    block = _block(repo)
    assert "zapp/zpkg/*.go" in block and "zapp/zpkg/zsrc.go" not in block
    assert not any(e.startswith("zapp/zelse") or e.endswith("znotes.md") for e in block)
    traced = wpf.Traced(repo, _workflow(repo), wpf.tracked_files(repo))
    covered = {f: paths_filter_glob.on_paths_covers(traced.paths_now(), f)
               for f in ("zapp/zpkg/zsrc.go", "zapp/zpkg/zother_test.go",
                         "zapp/zpkg/znotes.md", "zapp/zelse/zelse.go")}
    assert covered == {"zapp/zpkg/zsrc.go": True, "zapp/zpkg/zother_test.go": True,
                       "zapp/zpkg/znotes.md": False, "zapp/zelse/zelse.go": False}
    assert wpf.go_package_glob("zroot.go") == "*.go"


# ── generate / --check ──────────────────────────────────────────────────────

def test_generate_writes_the_union_and_touches_nothing_else(
        repo: Path, tmp_path: Path) -> None:
    before = _workflow(repo).read_bytes()
    _record_and_generate(repo, tmp_path / "t")
    block = _block(repo)
    # zdata/zconfig.txt is NOT there: the hand-written `zdata/**` covers it.
    assert set(block) == EXPECTED_BLOCK
    assert block == sorted(block)
    # Byte-for-byte outside the regions: drop what is between the markers.
    after = _workflow(repo).read_bytes().decode("utf-8")
    lines = after.splitlines(keepends=True)
    inside = {i for r in wpf.find_regions(lines, WORKFLOW)
              for i in range(r.begin + 1, r.end)}
    assert "".join(l for i, l in enumerate(lines) if i not in inside).encode() == before
    # …and the two steps are in the job, parsed.
    steps = yaml.safe_load(after)["jobs"]["zjob"]["steps"]
    assert [s.get("name") for s in steps] == [
        None, wpf.ENV_STEP_NAME, None, wpf.ASSERT_STEP_NAME]
    assert steps[1]["run"] == wpf.ENV_STEP_RUN and " reset --state " in steps[1]["run"]
    assert " finish --state " in steps[3]["run"]
    assert " && python3 scripts/ops/workflow_path_filter.py assert " in steps[3]["run"]
    assert "--workflow zflow.yaml --job zjob " in steps[3]["run"]
    assert _main(repo, "generate", "--check") == 0
    assert _main(repo, "generate") == 0  # idempotent
    assert _workflow(repo).read_bytes().decode("utf-8") == after


def test_generate_keeps_crlf_line_endings(repo: Path, tmp_path: Path) -> None:
    _workflow(repo).write_bytes(_workflow(repo).read_bytes().replace(b"\n", b"\r\n"))
    _record_and_generate(repo, tmp_path / "t")
    data = _workflow(repo).read_bytes()
    assert b"\r\n" in data and b"\n" not in data.replace(b"\r\n", b"")


def test_check_is_red_when_the_region_and_the_trace_disagree(
        repo: Path, tmp_path: Path, capsys) -> None:
    _record_and_generate(repo, tmp_path / "t")
    trace_file = _trace_file(repo)
    doc = json.loads(trace_file.read_text(encoding="utf-8"))

    # The trace gains a file the region does not have.
    doc["jobs"]["zjob"]["files"].append("zlib/zunrelated.py")
    trace_file.write_text(json.dumps(doc), encoding="utf-8")
    capsys.readouterr()
    assert _main(repo, "generate", "--check") == 1
    err = capsys.readouterr().err
    assert "DRIFT" in err and "missing: zlib/zunrelated.py" in err

    # The region holds an entry nothing derives.
    doc["jobs"]["zjob"]["files"].remove("zlib/zunrelated.py")
    doc["jobs"]["zjob"]["files"].remove("zlib/zopened.txt")
    trace_file.write_text(json.dumps(doc), encoding="utf-8")
    assert _main(repo, "generate", "--check") == 1
    assert "not derived: zlib/zopened.txt" in capsys.readouterr().err


def test_check_is_red_when_a_listed_file_gains_a_literal_import(
        repo: Path, tmp_path: Path, capsys) -> None:
    """The closure is recomputed from the sources, so the commit that writes
    the import is the commit `--check` stops."""
    _record_and_generate(repo, tmp_path / "t")
    (repo / "zlib/zdirect.py").write_text(
        "def f():\n    from zunrelated import VALUE\n", encoding="utf-8")
    capsys.readouterr()
    assert _main(repo, "generate", "--check") == 1
    assert "missing: zlib/zunrelated.py" in capsys.readouterr().err


def test_generate_prunes_a_trace_entry_whose_file_is_gone(
        repo: Path, tmp_path: Path, capsys) -> None:
    _record_and_generate(repo, tmp_path / "t")
    _git(repo, "rm", "-q", "-f", "zlib/zopened.txt")
    assert _main(repo, "generate", "--check") == 1
    assert "no longer a tracked file" in capsys.readouterr().err
    assert _main(repo, "generate") == 0
    assert "zlib/zopened.txt" not in _block(repo)
    assert _main(repo, "generate", "--check") == 0


OLD_FORMAT = {"workflow": WORKFLOW, "jobs": {"zjob": {
    "files": ["ztool/zmain.py"], "import_dirs": ["zlib"],
    "go": [{"module_dir": "zapp", "package": "./zpkg", "sources": ["zapp/zpkg/*.go"]}]}}}


@pytest.mark.parametrize("broken, why", [
    ("{not json", "JSONDecodeError"),
    ('{"jobs": {"zjob": {"files": "x", "import_dirs": []}}}', "`files` is not a list"),
    ('{"jobs": {"zjob": {"files": [1], "import_dirs": []}}}', "`files` is not a list of strings"),
    ('{"jobs": {"zjob": {"files": []}}}', "KeyError"),
    ('{"workflow": 1}', "KeyError"),
    # The format the retired Python tracer wrote: its `files` were what Python
    # loaded, not what the job read. Refused, with what to do about it.
    (json.dumps(OLD_FORMAT), "a job holds `go`"),
])
def test_a_malformed_or_old_format_trace_file_is_no_answer_not_an_empty_trace(
        repo: Path, tmp_path: Path, broken: str, why: str) -> None:
    """Read as empty, a broken trace file would have `generate` rewrite the
    region down to the workflow's own two entries and exit 0."""
    state = tmp_path / "s.json"
    _record_and_generate(repo, state)
    before = _workflow(repo).read_bytes()
    _trace_file(repo).write_text(broken, encoding="utf-8")
    for argv in (("generate",), ("generate", "--check"),
                 ("assert", *_job_args(state)), ("record", *_job_args(state))):
        with pytest.raises(CallerError, match="is not a trace file") as caught:
            _main(repo, *argv)
        assert why in str(caught.value)
        assert "again from an atime measurement" in str(caught.value)
        assert "Delete it" in str(caught.value)
    assert _workflow(repo).read_bytes() == before
    assert _trace_file(repo).read_text(encoding="utf-8") == broken


def test_a_later_exclusion_of_a_generated_entry_is_a_finding(
        repo: Path, tmp_path: Path, capsys) -> None:
    """`!zlib/**` AFTER the region wins under GitHub's last-match rule: the
    entries are in the list and start nothing."""
    _record_and_generate(repo, tmp_path / "t")
    _edit(repo, "      # END GENERATED path-trace\n",
          '      # END GENERATED path-trace\n      - "!zlib/**"\n')
    capsys.readouterr()
    assert _main(repo, "generate", "--check") == 1
    err = capsys.readouterr().err
    assert "EXCLUDED    zflow.yaml: zlib/zdirect.py" in err


def test_this_tool_is_listed_through_machinery(repo: Path, tmp_path: Path,
                                               monkeypatch) -> None:
    """Neither tool is in a job's measurement (atime_trace.py leaves its own
    source out; `assert` runs after `finish`), so both are listed by name,
    with whatever they import."""
    for rel, text in (("zops/ztool.py", "import zhelper\n"), ("zops/zhelper.py", "")):
        (repo / rel).parent.mkdir(exist_ok=True)
        (repo / rel).write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    monkeypatch.setattr(wpf, "MACHINERY", ("zops/ztool.py",))
    monkeypatch.setattr(wpf, "MACHINERY_IMPORT_DIRS", ("zops",))
    _record_and_generate(repo, tmp_path / "t")
    assert {"zops/ztool.py", "zops/zhelper.py"} <= set(_block(repo))


def test_the_real_machinery_is_this_tool_and_the_one_that_measures() -> None:
    assert wpf.MACHINERY == ("scripts/ops/workflow_path_filter.py",
                             "scripts/ops/atime_trace.py")
    assert all((ROOT / f).is_file() for f in wpf.MACHINERY)


# ── where the regions sit ───────────────────────────────────────────────────

def test_an_empty_region_outside_the_pull_request_list_is_refused_unwritten(
        repo: Path, tmp_path: Path) -> None:
    """The state a workflow is in the first time `generate` runs: nothing
    between the markers yet. Under `push.paths` the entries would be written
    there, the pull_request list would stay as it was, and the run would
    exit 0."""
    _measure(repo, tmp_path / "t")
    _edit(repo,
          "      # BEGIN GENERATED path-trace (do not edit)\n"
          "      # END GENERATED path-trace\n  workflow_dispatch:\n",
          "  push:\n    paths:\n      - \"zlib/**\"\n"
          "      # BEGIN GENERATED path-trace (do not edit)\n"
          "      # END GENERATED path-trace\n")
    before = _workflow(repo).read_bytes()
    for argv in (("generate",), ("generate", "--check"),
                 ("record", *_job_args(tmp_path / "t"))):
        with pytest.raises(CallerError, match="do not sit inside the `on.pull_request.paths`"):
            _main(repo, *argv)
    assert _workflow(repo).read_bytes() == before
    assert not (repo / ".github/path-traces").exists()


@pytest.mark.parametrize("old, new, message", [
    # Indented so the entries would land outside the list: not parseable.
    ("      # BEGIN GENERATED path-trace (do not edit)\n",
     "  # BEGIN GENERATED path-trace (do not edit)\n", "does not parse|do not sit inside"),
    # The assert region moved under `on:`.
    ("      # BEGIN GENERATED path-trace-assert job=zjob\n"
     "      # END GENERATED path-trace-assert\n", "", "exactly one"),
    ("path-trace-env job=zjob", "path-trace-env job=znope", "does not define"),
    ("      # END GENERATED path-trace-env\n", "", "before the previous region's END"),
])
def test_misplaced_or_unpaired_markers_are_no_answer_and_write_nothing(
        repo: Path, old: str, new: str, message: str) -> None:
    _edit(repo, old, new)
    before = _workflow(repo).read_bytes()
    with pytest.raises(CallerError, match=message):
        _main(repo, "generate")
    assert _workflow(repo).read_bytes() == before


def test_a_job_region_outside_its_job_is_refused(repo: Path) -> None:
    text = _workflow(repo).read_text(encoding="utf-8")
    region = ("      # BEGIN GENERATED path-trace-assert job=zjob\n"
              "      # END GENERATED path-trace-assert\n")
    assert text.count(region) == 1
    _workflow(repo).write_text(
        text.replace(region, "") + "  zsecond:\n    runs-on: x\n    steps:\n" + region,
        encoding="utf-8")
    with pytest.raises(CallerError, match="exactly one|not inside job"):
        _main(repo, "generate")


# ── is the job wired: the ways it only looks so ─────────────────────────────

CHECKOUT = "      - uses: actions/checkout@v6\n"
ENV_STEP = (f"      - name: {wpf.ENV_STEP_NAME}\n"
            f"        run: {wpf.ENV_STEP_RUN}\n")
ENV_REGION = ("      # BEGIN GENERATED path-trace-env job=zjob\n" + ENV_STEP
              + "      # END GENERATED path-trace-env\n")
TOOL_STEP = "      - run: python3 ztool/zmain.py\n"
ASSERT_RUN = f"        run: {wpf.assert_step_run(WORKFLOW, 'zjob')}\n"
FINISH = 'python3 -I scripts/ops/atime_trace.py finish --state "$RUNNER_TEMP/atime-trace.json" && '
RESET_ONCE = "generated reset step exactly once, unchanged (found 0)"
ASSERT_ONCE = "generated assert step exactly once, unchanged (found 0)"


@pytest.mark.parametrize("old, new, message", [
    # ── the checkout is the first step ──
    (CHECKOUT, "      - run: echo zearly\n" + CHECKOUT,
     "its first step must be `uses: actions/checkout@…`"),
    (CHECKOUT, "      - uses: actions/setup-python@v6\n" + CHECKOUT,
     "its first step must be `uses: actions/checkout@…`"),
    # ── the reset step is the second, byte-identical ──
    # moved after the step that does the reading
    (ENV_REGION + TOOL_STEP, TOOL_STEP + ENV_REGION,
     "the generated reset step is step 3; it must be the second step"),
    # a `uses:` step slipped in between: what the action reads is not measured
    (CHECKOUT, CHECKOUT + "      - uses: actions/setup-python@v6\n",
     "the generated reset step is step 3; it must be the second step"),
    # its text edited: another state file, the `-I` dropped, another canary dir
    ('reset --state "$RUNNER_TEMP/atime-trace.json"', 'reset --state "$RUNNER_TEMP/zother.json"',
     RESET_ONCE),
    ("run: python3 -I scripts/ops/atime_trace.py reset", "run: python3 scripts/ops/atime_trace.py reset",
     RESET_ONCE),
    (' --canary-dir "$GITHUB_WORKSPACE/.git/atime-canary"\n', "\n", RESET_ONCE),
    # a key added to it: a failure of the reset would not stop the job
    (ENV_STEP, ENV_STEP + "        continue-on-error: true\n", RESET_ONCE),
    # a second copy, by hand, later in the job: the first measurement is void
    (TOOL_STEP, TOOL_STEP + ENV_STEP,
     "generated reset step exactly once, unchanged (found 2)"),
    # ── the assert step is the last `run:`, byte-identical ──
    # its failure is swallowed by the shell
    (ASSERT_RUN, ASSERT_RUN.rstrip("\n") + " || true\n", ASSERT_ONCE),
    # the command survives only in a shell comment
    (ASSERT_RUN, "        run: |\n          # " + ASSERT_RUN.strip()[5:] + "\n          true\n",
     ASSERT_ONCE),
    # it reads another state file than the one `finish` marked
    ('--state "$RUNNER_TEMP/atime-trace.json" --out', '--state "$RUNNER_TEMP/zother.json" --out',
     ASSERT_ONCE),
    # `finish` dropped: nothing ends the measurement or checks the canaries
    (FINISH, "", ASSERT_ONCE),
    # `&&` loosened: `assert` would run after a failed `finish`
    (FINISH, FINISH[:-len(" && ")] + "; ", ASSERT_ONCE),
    ("        if: ${{ !cancelled() }}\n", "        if: ${{ success() }}\n", ASSERT_ONCE),
    ("      # END GENERATED path-trace-assert\n",
     "      # END GENERATED path-trace-assert\n      - run: python3 ztool/zmain.py late\n",
     "step 5 runs a command after the assert step"),
    # a `uses:` step after it reads unmeasured (and the assert moved ABOVE
    # one is the same job text: that step's reads leave the next recording)
    ("      # END GENERATED path-trace-assert\n",
     "      # END GENERATED path-trace-assert\n      - uses: zorg/zscan@v1\n",
     "step 5 comes after the assert step"),
    # the shell the assert step runs under is not the runner's
    ("    runs-on: ubuntu-24.04\n",
     "    runs-on: ubuntu-24.04\n    defaults:\n      run:\n        shell: bash -c \"bash {0} || true\"\n",
     "the job sets `defaults.run.shell`"),
    ("jobs:\n", "defaults:\n  run:\n    shell: bash -c \"bash {0} || true\"\njobs:\n",
     "the workflow sets `defaults.run.shell`"),
    # ── nothing in the job makes a failure not count ──
    ("    runs-on: ubuntu-24.04\n", "    runs-on: ubuntu-24.04\n    continue-on-error: true\n",
     "job `zjob` sets `continue-on-error`"),
    ("    runs-on: ubuntu-24.04\n", "    runs-on: ubuntu-24.04\n    container: zimage\n",
     "job `zjob` runs in a `container:`"),
    # ── no other step touches the git setting the measurement holds ──
    (TOOL_STEP, "      - run: git config core.trustctime true && python3 ztool/zmain.py\n",
     "step 3's `run:` mentions `core.trustctime`"),
    (TOOL_STEP, "      - run: git -c Core.TrustCtime=true status; python3 ztool/zmain.py\n",
     "step 3's `run:` mentions `core.trustctime`"),
    # …whichever step it is in (review: every row above put it in step 3).
    (TOOL_STEP, TOOL_STEP + "      - run: git config core.trustctime true\n",
     "step 4's `run:` mentions `core.trustctime`"),
    # A string comparison, stated as one: a mention in a comment is refused too.
    (TOOL_STEP, "      - run: |\n          # leaves core.trustctime alone\n"
                "          python3 ztool/zmain.py\n",
     "step 3's `run:` mentions `core.trustctime`"),
])
def test_a_job_that_only_looks_wired_is_a_finding(
        repo: Path, tmp_path: Path, capsys, old: str, new: str, message: str) -> None:
    _record_and_generate(repo, tmp_path / "t")
    assert _main(repo, "generate", "--check") == 0
    _edit(repo, old, new)
    capsys.readouterr()
    assert _main(repo, "generate", "--check") == 1
    err = capsys.readouterr().err
    assert message in err, err
    assert "WIRING" in err


def test_a_wired_job_has_no_wiring_finding_and_each_rule_reports_once(
        repo: Path, tmp_path: Path) -> None:
    """The control for the table above (a rule that fired on a correct job
    would make every row pass for the wrong reason), and one mis-wiring is
    one finding, not a cascade."""
    _record_and_generate(repo, tmp_path / "t")
    traced = wpf.Traced(repo, _workflow(repo), wpf.tracked_files(repo))
    assert traced.wiring_errors() == []
    # An artifact upload after the assert step is the one thing allowed there,
    # and `defaults.run` without a `shell` changes nothing the rule is about.
    allowed = yaml.safe_load(_workflow(repo).read_text(encoding="utf-8"))
    allowed["jobs"]["zjob"]["steps"].append({"uses": "actions/upload-artifact@v7"})
    allowed["jobs"]["zjob"]["defaults"] = {"run": {"working-directory": "."}}
    assert traced.wiring_errors(allowed) == []
    doc = yaml.safe_load(_workflow(repo).read_text(encoding="utf-8"))
    doc["jobs"]["zjob"]["steps"][2]["run"] += "  # core.trustctime"
    assert len(traced.wiring_errors(doc)) == 1


def test_the_generated_step_text_holds_no_mention_of_the_setting_it_guards() -> None:
    """The `core.trustctime` rule skips the two generated steps by position;
    were the setting named in their text, a hand-made copy of one could not
    be told from a step that sets it."""
    assert wpf.TRUSTCTIME not in wpf.ENV_STEP_RUN
    assert wpf.TRUSTCTIME not in wpf.assert_step_run(WORKFLOW, "zjob")


def test_a_job_without_a_recorded_trace_is_a_finding(repo: Path, capsys) -> None:
    assert _main(repo, "generate", "--check") == 1
    assert "job `zjob` has no recorded trace" in capsys.readouterr().err


# ── the runtime check ───────────────────────────────────────────────────────

def test_assert_names_a_read_file_taken_out_of_the_region(
        repo: Path, tmp_path: Path, capsys) -> None:
    state = tmp_path / "t"
    _record_and_generate(repo, state)
    capsys.readouterr()
    assert _main(repo, "assert", *_job_args(state)) == 0
    assert f"{len(EXPECTED_READ)} tracked file(s) read" in capsys.readouterr().out

    _edit(repo, '      - "zlib/zcat.txt"\n', "")
    out = tmp_path / "out.json"
    assert _main(repo, "assert", *_job_args(state), "--out", str(out)) == 1
    err = capsys.readouterr().err
    assert "UNCOVERED  zlib/zcat.txt" in err
    assert err.count("UNCOVERED") == 1, err
    assert "atime_trace.py reset" in err and "BOUNDARY" not in err and "tracer" not in err
    # `--check` reds on the same edit, before CI is ever reached.
    assert _main(repo, "generate", "--check") == 1


def _extra_tracked(repo: Path, *names: str) -> None:
    for name in names:
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{name}\n", encoding="utf-8")
    _git(repo, "add", "-A")


def test_a_file_the_job_replaced_or_deleted_counts_as_read(
        repo: Path, tmp_path: Path, capsys) -> None:
    """`sed -i` reads the file it replaces; a file deleted after it was read
    has no access time left to show it. atime_trace.py reports the two as
    REPLACED / GONE because it cannot know — and "cannot know" used to come
    out here as "Every one is covered" (an adversarial review ran both)."""
    state = tmp_path / "t"
    _record_and_generate(repo, state)
    _extra_tracked(repo, "zextra/zreplaced.txt", "zextra/zgone.txt", "zextra/zidle.txt")
    assert _atime(repo, "reset", "--state", str(state)).returncode == 0
    fresh = repo / "zextra/znew"
    fresh.write_text("rewritten\n", encoding="utf-8")
    os.replace(fresh, repo / "zextra/zreplaced.txt")
    (repo / "zextra/zgone.txt").unlink()
    assert _atime(repo, "finish", "--state", str(state)).returncode == 0
    capsys.readouterr()
    assert _main(repo, "assert", *_job_args(state)) == 1
    err = capsys.readouterr().err
    assert "UNCOVERED  zextra/zreplaced.txt" in err and "UNCOVERED  zextra/zgone.txt" in err
    assert "zidle" not in err and err.count("UNCOVERED") == 2, err


def test_a_file_read_and_then_removed_from_the_index_still_counts(
        repo: Path, tmp_path: Path, capsys) -> None:
    """`git rm` after the read: the file is tracked no more when `assert`
    runs, and was filtered out with everything else untracked."""
    state = tmp_path / "t"
    _record_and_generate(repo, state)
    _extra_tracked(repo, "zextra/zrm.txt")
    assert _atime(repo, "reset", "--state", str(state)).returncode == 0
    (repo / "zextra/zrm.txt").read_bytes()
    _git(repo, "rm", "-q", "-f", "zextra/zrm.txt")
    assert _atime(repo, "finish", "--state", str(state)).returncode == 0
    capsys.readouterr()
    assert _main(repo, "assert", *_job_args(state)) == 1
    assert "UNCOVERED  zextra/zrm.txt" in capsys.readouterr().err


@pytest.mark.parametrize("bad", [{"zlib/zcat.txt": True}, "zlib/zcat.txt", [1]])
def test_a_segment_field_that_is_not_a_list_of_paths_is_no_answer(
        repo: Path, tmp_path: Path, bad) -> None:
    """A mapping iterates as its keys: `{"covered.txt": true}` used to read
    as one covered file."""
    state = tmp_path / "t"
    _record_and_generate(repo, state)
    doc = json.loads(state.read_text(encoding="utf-8"))
    doc["segments"][-1]["files"] = bad
    state.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(CallerError, match="is not a state file"):
        _main(repo, "assert", *_job_args(state))


def test_a_listed_python_file_missing_from_the_tree_does_not_stop_generate(
        repo: Path, tmp_path: Path) -> None:
    """A job that reads a `.py` file and deletes it leaves a trace naming a
    file with no text to parse; the path is still a requirement."""
    _record_and_generate(repo, tmp_path / "t")
    (repo / "zlib/zdirect.py").unlink()
    assert _main(repo, "generate", "--check") in (0, 1)


def test_the_import_lookup_always_includes_the_checkout_root(
        repo: Path, tmp_path: Path) -> None:
    """`python -m pkg.main` resolves `import pkg.lazy` from the root, whatever
    directories the job read files in."""
    for name, text in (("zpkg/__init__.py", ""), ("zpkg/zmain.py", "import zpkg.zlazy\n"),
                       ("zpkg/zlazy.py", "")):
        (repo / name).parent.mkdir(exist_ok=True)
        (repo / name).write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    tracked = frozenset(wpf.tracked_files(repo))
    trace = {"jobs": {"zjob": {"files": ["zpkg/zmain.py"], "import_dirs": ["zpkg"]}}}
    assert "zpkg/zlazy.py" in wpf.required_entries(repo, WORKFLOW, trace, tracked)


def test_a_go_file_is_recorded_even_when_a_hand_written_pattern_covers_it() -> None:
    """Its package glob has to survive for the siblings the job did not read."""
    record = wpf.job_record({"zgo/za.go", "zdata/zx.txt"}, ["zgo/za.go", "zdata/**"])
    assert record["files"] == ["zgo/za.go"] and record["import_dirs"] == []


def _out_file(path: Path, files: list[str], dirs: list[str], *, workflow: str = WORKFLOW,
              job: str = "zjob", leg: str | None = "0/1") -> Path:
    doc = {"workflow": workflow, "jobs": {job: {"files": files, "import_dirs": dirs}}}
    if leg is not None:
        doc["leg"] = leg
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def _merge(repo: Path, *outs: Path, job: str = "zjob") -> int:
    return _main(repo, "merge", "--workflow", WORKFLOW, "--job", job, *map(str, outs))


def _recorded(repo: Path) -> dict:
    return json.loads(_trace_file(repo).read_text(encoding="utf-8"))["jobs"]


def test_merge_replaces_a_jobs_record_with_the_union_of_its_legs(
        repo: Path, tmp_path: Path) -> None:
    """A matrix job's legs each read their own part and each write an
    `--out` file; the job's entry is all of them together. It REPLACES the
    entry, so a re-recording still shrinks the list, and it leaves every
    other job's entry as it was."""
    _record_and_generate(repo, tmp_path / "t")
    doc = json.loads(_trace_file(repo).read_text(encoding="utf-8"))
    doc["jobs"]["zneighbour"] = {"files": ["zdata/zx.txt"], "import_dirs": []}
    _trace_file(repo).write_text(json.dumps(doc), encoding="utf-8")
    a = _out_file(tmp_path / "a.json", ["zlib/zcat.txt", "ztool/zmain.py"], ["ztool"],
                  leg="0/2")
    b = _out_file(tmp_path / "b.json", ["zlib/zdirect.py"], ["zlib"], leg="1/2")
    assert _merge(repo, a, b) == 0
    want = {"files": ["zlib/zcat.txt", "zlib/zdirect.py", "ztool/zmain.py"],
            "import_dirs": ["zlib", "ztool"]}
    assert _recorded(repo) == {"zjob": want, "zneighbour": doc["jobs"]["zneighbour"]}
    # The order of the legs is not part of the answer.
    assert _merge(repo, b, a) == 0
    assert _recorded(repo)["zjob"] == want
    # A later measurement that read less: the entry shrinks.
    one = _out_file(tmp_path / "one.json", ["zlib/zdirect.py"], ["zlib"])
    assert _merge(repo, one) == 0
    assert _recorded(repo)["zjob"] == {"files": ["zlib/zdirect.py"], "import_dirs": ["zlib"]}


def _raw(text: str):
    return lambda p: (p.write_text(text, encoding="utf-8"), p)[1]


@pytest.mark.parametrize("make, message", [
    (lambda p: _out_file(p, [], [], workflow="zother.yaml", leg="1/2"),
     "it is a trace of `zother.yaml`"),
    (_raw('{"leg": "1/2", "jobs": {"zjob": {"files": [], "import_dirs": []}}}'),
     "it is a trace of `None`"),
    (lambda p: _out_file(p, [], [], job="zelse", leg="1/2"), "KeyError"),
    (_raw('{"workflow": "zflow.yaml", "leg": "1/2", "jobs": {"zjob": {"files": "a", '
          '"import_dirs": []}}}'), "`files` is not a list of strings"),
    (_raw('{"workflow": "zflow.yaml", "leg": "1/2", "jobs": {"zjob": {"files": [], '
          '"import_dirs": [], "go": []}}}'), "pre-atime"),
    (_raw("{not json"), "JSONDecodeError"),
    (lambda p: p, "FileNotFoundError"),
    # which leg it is: absent (a file from before legs were written), not a
    # leg at all, or the leg another file already is
    (lambda p: _out_file(p, [], [], leg=None), "leg None is not"),
    (lambda p: _out_file(p, [], [], leg="2/2"), "leg '2/2' is not"),
    (lambda p: _out_file(p, [], [], leg="/"), "leg '/' is not"),
    (lambda p: _out_file(p, [], [], leg="0/2"), "it is leg 0, and so is"),
    # names `assert --out` cannot have written: it writes tracked files only
    (lambda p: _out_file(p, ["zlib/../zlib/zcat.txt"], [], leg="1/2"),
     "not tracked in this checkout"),
    (lambda p: _out_file(p, ["/etc/passwd"], [], leg="1/2"), "not tracked in this checkout"),
    (lambda p: _out_file(p, ["zlib/zgone.txt"], [], leg="1/2"), "zlib/zgone.txt, not tracked"),
    (lambda p: _out_file(p, [], [".."], leg="1/2"), "not tracked in this checkout"),
    (lambda p: _out_file(p, [], ["zlib/"], leg="1/2"), "not tracked in this checkout"),
])
@pytest.mark.parametrize("bad_first", [False, True])
def test_merge_refuses_a_file_that_is_not_this_jobs_measurement_and_writes_nothing(
        repo: Path, tmp_path: Path, make, message: str, bad_first: bool) -> None:
    """One bad leg among good ones, wherever it stands on the command line:
    nothing is written — a union with a hole in it would look like a complete
    record."""
    _record_and_generate(repo, tmp_path / "t")
    before = _trace_file(repo).read_bytes()
    good = _out_file(tmp_path / "good.json", ["zlib/zcat.txt"], [], leg="0/2")
    bad = make(tmp_path / "bad.json")
    if bad_first and "and so is" in message:
        pytest.skip("the second file of a pair is the duplicate, whichever it is")
    with pytest.raises(CallerError, match=message):
        _merge(repo, *((bad, good) if bad_first else (good, bad)))
    assert _trace_file(repo).read_bytes() == before
    with pytest.raises(CallerError, match="has no job `znone`"):
        _merge(repo, good, job="znone")


@pytest.mark.parametrize("legs, message", [
    (["0/3", "1/3"], r"got leg\(s\) 0, 1 of 3"),          # one left out
    (["1/2"], r"got leg\(s\) 1 of 2"),                   # one leg's file alone
    (["0/2", "1/3"], r"got leg\(s\) 0, 1 of 2 or 3"),     # two different runs
])
def test_merge_takes_nothing_less_than_every_leg(
        repo: Path, tmp_path: Path, legs: list[str], message: str) -> None:
    """A left-out leg whose reads hand-written patterns cover never fails its
    own `assert`; what its `.py` files brought in by import would just go."""
    _record_and_generate(repo, tmp_path / "t")
    before = _trace_file(repo).read_bytes()
    outs = [_out_file(tmp_path / f"{i}.json", ["zlib/zcat.txt"], [], leg=leg)
            for i, leg in enumerate(legs)]
    with pytest.raises(CallerError, match=message):
        _merge(repo, *outs)
    assert _trace_file(repo).read_bytes() == before


def test_record_refuses_a_job_with_a_strategy(repo: Path, tmp_path: Path) -> None:
    """One state file is one leg; as the job's entry it would drop the rest."""
    state = tmp_path / "t"
    _record_and_generate(repo, state)
    before = _trace_file(repo).read_bytes()
    _edit(repo, "    runs-on: ubuntu-24.04\n",
          "    runs-on: ubuntu-24.04\n    strategy:\n      matrix:\n        zleg: [a, b]\n")
    with pytest.raises(CallerError, match="has a `strategy:`"):
        _main(repo, "record", *_job_args(state))
    assert _trace_file(repo).read_bytes() == before


def test_assert_on_a_matrix_leg_points_at_merge_not_at_its_own_file(
        repo: Path, tmp_path: Path, capsys) -> None:
    state = tmp_path / "t"
    _record_and_generate(repo, state)
    _edit(repo, '      - "zlib/zcat.txt"\n', "")
    out = tmp_path / "out.json"
    capsys.readouterr()
    assert _main(repo, "assert", *_job_args(state), "--out", str(out), "--leg", "1/3") == 1
    err = capsys.readouterr().err
    assert "leg 1/3 of a matrix job" in err and "the union of all 3" in err
    assert "merge --workflow zflow.yaml --job zjob <the 3 files>" in err
    assert "`record`" not in err
    assert json.loads(out.read_text(encoding="utf-8"))["leg"] == "1/3"
    # …and a job with no matrix is told its one file is the whole answer.
    assert _main(repo, "assert", *_job_args(state)) == 1
    err = capsys.readouterr().err
    assert "matrix" not in err and "<that file>" in err
    for leg in ("3/3", "/", "", "a/2", "0/0"):
        with pytest.raises(CallerError, match="--leg"):
            _main(repo, "assert", *_job_args(state), "--out", str(out), "--leg", leg)
        assert json.loads(out.read_text(encoding="utf-8"))["leg"] == "1/3", leg


def test_the_read_set_is_the_union_of_every_segment(
        repo: Path, tmp_path: Path, capsys) -> None:
    """A read collected in an earlier segment is the job's as much as one in
    the last (review: every other state here holds a single segment)."""
    state = tmp_path / "t"
    _record_and_generate(repo, state)
    _extra_tracked(repo, "zextra/zearly.txt", "zextra/zlate.txt")
    assert _atime(repo, "reset", "--state", str(state)).returncode == 0
    (repo / "zextra/zearly.txt").read_bytes()
    assert _atime(repo, "collect", "--state", str(state), "--label", "zfirst").returncode == 0
    (repo / "zextra/zlate.txt").read_bytes()
    assert _atime(repo, "finish", "--state", str(state)).returncode == 0
    capsys.readouterr()
    assert _main(repo, "assert", *_job_args(state)) == 1
    err = capsys.readouterr().err
    assert "UNCOVERED  zextra/zearly.txt" in err and "UNCOVERED  zextra/zlate.txt" in err


def test_a_measurement_naming_nearly_the_whole_tree_is_no_answer(
        repo: Path, tmp_path: Path, capsys, monkeypatch) -> None:
    """What one `git status` with `core.trustctime` in effect as true leaves
    behind (`git -c`, a `GIT_CONFIG_*` variable — spellings the wiring check
    cannot see). Thousands of UNCOVERED lines and "record them" would be the
    wrong reading: the measurement is polluted, which is exit 2."""
    state = tmp_path / "t"
    _record_and_generate(repo, state)
    assert _atime(repo, "reset", "--state", str(state)).returncode == 0
    tracked = wpf.tracked_files(repo)
    for name in tracked:
        (repo / name).read_bytes()
    assert _atime(repo, "finish", "--state", str(state)).returncode == 0
    monkeypatch.setattr(wpf, "WHOLE_TREE_FLOOR", len(tracked))
    with pytest.raises(CallerError, match="tracked files as read"):
        _main(repo, "assert", *_job_args(state))
    # Below the floor the same state is an ordinary answer (a small tree,
    # all of it read, is not that signature).
    monkeypatch.setattr(wpf, "WHOLE_TREE_FLOOR", len(tracked) + 1)
    assert _main(repo, "assert", *_job_args(state)) in (0, 1)


def test_assert_sees_a_read_the_recorded_trace_never_had(
        repo: Path, tmp_path: Path, capsys) -> None:
    """The convergence argument: a new read starts from a listed file, the
    workflow runs, and the check names what the list lacks. Two ways here: the
    job takes a branch the recorded run did not and reads through a path
    nothing static names; and the hand-written list is narrowed, so a file
    that WAS covered is not."""
    _record_and_generate(repo, tmp_path / "t")
    main = repo / "ztool/zmain.py"
    main.write_text(main.read_text(encoding="utf-8")
                    + "    open(os.path.join(ROOT, 'zlib', 'zunrelated.py')).read()\n",
                    encoding="utf-8")
    state = tmp_path / "t2"
    _measure(repo, state, "zbranch")
    capsys.readouterr()
    assert _main(repo, "assert", *_job_args(state)) == 1
    err = capsys.readouterr().err
    # zlazy.py was read on that branch too — and is covered (literal import).
    assert "UNCOVERED  zlib/zunrelated.py" in err and err.count("UNCOVERED") == 1, err

    _edit(repo, '      - "zdata/**"\n', '      - "zother/**"\n')
    assert _main(repo, "assert", *_job_args(tmp_path / "t")) == 1
    err = capsys.readouterr().err
    assert "UNCOVERED  zdata/zconfig.txt" in err and "UNCOVERED  zdata/zplug.py" in err


def _doctored(state: Path, target: Path, change) -> Path:
    doc = json.loads(state.read_text(encoding="utf-8"))
    change(doc)
    target.write_text(json.dumps(doc), encoding="utf-8")
    return target


def _drop_segments(doc: dict) -> None:
    doc["segments"] = []


def _drop_finished(doc: dict) -> None:
    doc["finished"] = None


def _remove_finished(doc: dict) -> None:
    del doc["finished"]


def _failed_canary(doc: dict) -> None:
    doc["finished"] = {"canary": "failed"}


def _empty_reads(doc: dict) -> None:
    for segment in doc["segments"]:
        segment["files"] = []


def _untracked_reads(doc: dict) -> None:
    for segment in doc["segments"]:
        segment["files"] = ["zelsewhere/zmain.py"]


def _other_checkout(doc: dict) -> None:
    doc["root"] = "/zelsewhere/zrepo"


def _no_root(doc: dict) -> None:
    del doc["root"]


@pytest.mark.parametrize("change, message", [
    (_drop_segments, "holds no segment"),
    (_drop_finished, "is not marked finished"),
    (_remove_finished, "is not marked finished"),
    (_failed_canary, "is not marked finished"),
    (_empty_reads, "names no tracked file as read over 1 segment"),
    (_untracked_reads, "names no tracked file as read over 1 segment"),
    (_other_checkout, "measured /zelsewhere/zrepo, not this checkout"),
    (_no_root, "is not a state file `atime_trace.py` wrote"),
])
def test_a_state_that_is_not_a_finished_measurement_is_no_answer(
        repo: Path, tmp_path: Path, change, message: str) -> None:
    """Every one of these, read leniently, is a job that "read nothing
    uncovered": exit 0 on a run that measured nothing."""
    state = tmp_path / "t"
    _record_and_generate(repo, state)
    recorded = _trace_file(repo).read_bytes()
    bad = _doctored(state, tmp_path / "zbad.json", change)
    for command in ("assert", "record"):
        with pytest.raises(CallerError, match=message):
            _main(repo, command, *_job_args(bad))
    assert _trace_file(repo).read_bytes() == recorded


def test_a_missing_or_unreadable_state_and_an_unknown_job_are_no_answer(
        repo: Path, tmp_path: Path) -> None:
    state = tmp_path / "t"
    _record_and_generate(repo, state)
    with pytest.raises(CallerError, match="does not exist: nothing was measured"):
        _main(repo, "assert", *_job_args(tmp_path / "zmissing"))
    (tmp_path / "zjunk").write_text("{not json", encoding="utf-8")
    with pytest.raises(CallerError, match="cannot be read"):
        _main(repo, "assert", *_job_args(tmp_path / "zjunk"))
    with pytest.raises(CallerError, match="no job `znope`"):
        _main(repo, "assert", "--workflow", WORKFLOW, "--job", "znope",
              "--state", str(state))


def test_a_measurement_nobody_finished_is_refused_for_real(
        repo: Path, tmp_path: Path) -> None:
    """Not a doctored file: `reset`, the job, `collect` — and no `finish`."""
    _record_and_generate(repo, tmp_path / "t")
    state = tmp_path / "zunfinished"
    assert _atime(repo, "reset", "--state", str(state)).returncode == 0
    subprocess.run([sys.executable, "ztool/zmain.py"], cwd=repo, env=_ENV, check=True,
                   stdin=subprocess.DEVNULL, timeout=120)
    assert _atime(repo, "collect", "--state", str(state), "--label", "z").returncode == 0
    with pytest.raises(CallerError, match="is not marked finished"):
        _main(repo, "assert", *_job_args(state))
    _git(repo, "config", "--local", "--unset", "core.trustctime")


def test_record_is_reproducible_and_equals_what_assert_writes(
        repo: Path, tmp_path: Path) -> None:
    """The artifact a red run points at (`assert --out`) is byte for byte what
    `record` commits, and reading a state file does not change it."""
    state = tmp_path / "t"
    _record_and_generate(repo, state)
    trace_file = _trace_file(repo)
    first = trace_file.read_bytes()
    out = tmp_path / "out.json"
    for argv in (["record", *_job_args(state)],
                 ["assert", *_job_args(state), "--out", str(out)],
                 ["record", *_job_args(state)]):
        done = subprocess.run(
            [sys.executable, str(SCRIPT), "--root", str(repo), *argv], env=_ENV,
            capture_output=True, text=True, encoding="utf-8",
            stdin=subprocess.DEVNULL, timeout=120)
        # The subprocess has the real MACHINERY and ledgers; `record` and
        # `assert` read neither.
        assert done.returncode == 0, done.stderr
        assert trace_file.read_bytes() == first, argv
    # …plus which leg wrote it; `merge` of that one file is `record`.
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc.pop("leg") == "0/1"
    assert doc == json.loads(first)
    trace_file.write_text("{}", encoding="utf-8")
    trace_file.unlink()
    assert _main(repo, "merge", "--workflow", WORKFLOW, "--job", "zjob", str(out)) == 0
    assert trace_file.read_bytes() == first


def test_cli_exits_2_with_one_error_line_when_it_cannot_measure(tmp_path: Path) -> None:
    done = subprocess.run(
        [sys.executable, str(SCRIPT), "assert", "--workflow", "image-ref-resolve.yaml",
         "--job", "resolve", "--state", str(tmp_path / "zmissing")],
        capture_output=True, text=True, encoding="utf-8", stdin=subprocess.DEVNULL,
        timeout=120, env=_ENV)
    assert done.returncode == 2, done.stderr
    assert done.stderr.startswith("ERROR: the atime state file"), done.stderr
    assert done.stdout == ""


@pytest.mark.parametrize("gone", ["env", "--trace-dir"])
def test_the_tracer_era_command_line_is_gone(tmp_path: Path, gone: str) -> None:
    argv = (["env", "--trace-dir", str(tmp_path)] if gone == "env" else
            ["assert", "--workflow", WORKFLOW, "--job", "zjob", "--trace-dir", str(tmp_path)])
    done = subprocess.run([sys.executable, str(SCRIPT), *argv], capture_output=True,
                          text=True, encoding="utf-8", stdin=subprocess.DEVNULL,
                          timeout=120, env=_ENV)
    assert done.returncode == 2 and "usage:" in done.stderr, done.stderr


# ── the two generated steps, run as the shell lines they are ────────────────

def _repo_with_the_tools(tmp_path: Path, monkeypatch) -> Path:
    """The synthetic repository, TRACKING copies of the two tools and what
    they import at the paths the generated steps name."""
    repo = tmp_path / "zreal"
    for rel, text in FILES.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_bytes(text.encode("utf-8"))
    for rel in ("scripts/ops/atime_trace.py", "scripts/ops/workflow_path_filter.py",
                "scripts/ops/paths_filter_glob.py", "scripts/tools/_lib_exitcodes.py",
                "scripts/tools/_lib_compat.py"):
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / rel, repo / rel)
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    monkeypatch.setattr(wpf, "ADVISORY_PATH_GATES", {})
    monkeypatch.setattr(wpf, "READ_ASSERTION_ORDER", ())
    monkeypatch.setattr(wpf, "RESERVED_PATTERNS", {})
    return repo.resolve()


def _step(repo: Path, runner_temp: Path, script: str) -> subprocess.CompletedProcess:
    """A `run:` the way a runner starts it: `bash -eo pipefail`, in the
    workspace, the `${{ strategy.* }}` expression already expanded (a job
    with no matrix is run 0 of 1)."""
    assert script.count(wpf.LEG_EXPR) <= 1
    script = script.replace(wpf.LEG_EXPR, "0/1")
    env = dict(_ENV, RUNNER_TEMP=str(runner_temp), GITHUB_WORKSPACE=str(repo))
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("no bash on PATH: a workflow `run:` step cannot be started here")
    return subprocess.run(
        [bash, "--noprofile", "--norc", "-eo", "pipefail", "-c", script], cwd=repo,
        env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
        stdin=subprocess.DEVNULL, timeout=120)


def test_the_generated_steps_measure_and_assert_end_to_end(
        tmp_path: Path, monkeypatch, capsys) -> None:
    """Nothing here calls a function of either tool: the reset step's text,
    the job's command, the assert step's text, each through bash. The copies
    in the repository are the ones that run, with the real MACHINERY."""
    repo = _repo_with_the_tools(tmp_path, monkeypatch)
    temp = tmp_path / "zrunner"
    temp.mkdir()
    reset, check = wpf.ENV_STEP_RUN, wpf.assert_step_run(WORKFLOW, "zjob")
    job = "python3 ztool/zmain.py"

    # First run of a workflow being wired: no trace yet, so the region holds
    # nothing the job read — and the step says so, by name, with exit 1.
    assert _main(repo, "generate") == 1          # WIRING: no recorded trace
    assert "has no recorded trace" in capsys.readouterr().err
    _git(repo, "add", "-A")
    for script in (reset, job):
        done = _step(repo, temp, script)
        assert done.returncode == 0, done.stderr
    done = _step(repo, temp, check)
    if "CANARY-FAILED" in done.stderr:
        pytest.fail(f"COULD NOT MEASURE — not a pass and not a skip.\n{done.stderr}")
    assert done.returncode == 1, done.stdout + done.stderr
    assert "CANARY-OK" in done.stdout
    assert "UNCOVERED  zlib/zcat.txt" in done.stderr
    # The tools' own files are not "read by the job": listed through MACHINERY.
    assert "UNCOVERED  scripts/" not in done.stderr

    # The `--out` artifact of that red run is the trace to commit.
    _trace_file(repo).parent.mkdir(parents=True)
    shutil.copy(temp / "path-trace.json", _trace_file(repo))
    _git(repo, "add", "-A")
    assert _main(repo, "generate") == 0
    _git(repo, "add", "-A")
    assert _main(repo, "generate", "--check") == 0
    block = set(_block(repo))
    assert EXPECTED_BLOCK | {"scripts/ops/atime_trace.py", "scripts/ops/paths_filter_glob.py",
                             "scripts/ops/workflow_path_filter.py",
                             "scripts/tools/_lib_compat.py",
                             "scripts/tools/_lib_exitcodes.py"} == block

    # Second run: green.
    for script in (reset, job, check):
        done = _step(repo, temp, script)
        assert done.returncode == 0, (script, done.stdout, done.stderr)
    assert "Every one is covered" in done.stdout and "(by access time)" in done.stdout

    # Third run: the canaries do not hold. `finish` exits 2, `assert` does not
    # run (no verdict line, the artifact of the run before is not rewritten),
    # and the step is the failure.
    for script in (reset, job):
        assert _step(repo, temp, script).returncode == 0
    artifact = temp / "path-trace.json"
    artifact.unlink()
    shutil.rmtree(repo / ".git" / "atime-canary")
    done = _step(repo, temp, check)
    assert done.returncode == 2, done.stdout + done.stderr
    assert "is missing" in done.stderr
    assert "(by access time)" not in done.stdout and "UNCOVERED" not in done.stderr
    assert not artifact.exists()


# ── dead, reserved and unmodelled patterns ──────────────────────────────────

def _add_pattern(repo: Path, pattern: str) -> None:
    anchor = '      - "zdata/**"\n'
    _edit(repo, anchor, anchor + f'      - "{pattern}"\n')


def test_a_pattern_matching_no_tracked_file_is_dead(
        repo: Path, tmp_path: Path, capsys) -> None:
    _record_and_generate(repo, tmp_path / "t")
    _add_pattern(repo, "zlib/zmoved_away.py")
    capsys.readouterr()
    assert _main(repo, "generate", "--check") == 1
    err = capsys.readouterr().err
    assert "DEAD" in err and "'zlib/zmoved_away.py'" in err
    assert "UNMODELLED" not in err


def test_a_reserved_pattern_is_excused_and_the_ledger_is_checked_both_ways(
        repo: Path, tmp_path: Path, capsys, monkeypatch) -> None:
    _record_and_generate(repo, tmp_path / "t")
    _add_pattern(repo, "zlib/*.zyml")
    key = (WORKFLOW, "on.pull_request.paths", "zlib/*.zyml")
    monkeypatch.setattr(wpf, "RESERVED_PATTERNS", {key: "the family declares it"})
    assert _main(repo, "generate", "--check") == 0

    # Listed, but a file matches now: the excuse is over.
    (repo / "zlib/zx.zyml").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "-A")
    capsys.readouterr()
    assert _main(repo, "generate", "--check") == 1
    assert "STALE       RESERVED_PATTERNS names zflow.yaml on.pull_request.paths: 'zlib/*.zyml'" \
        in capsys.readouterr().err

    # Listed, but the pattern is not in that list (any more).
    monkeypatch.setattr(wpf, "RESERVED_PATTERNS", {
        (WORKFLOW, "on.push.paths", "zlib/*.zyml"): "why"})
    assert _main(repo, "generate", "--check") == 1
    assert "which is not a pattern of that list" in capsys.readouterr().err


@pytest.mark.parametrize("pattern", [
    "zlib/z?irect.py", "zlib/{zdirect,zlazy}.py", "zlib/[!a]direct.py", "zlib**/x.py",
    "zlib/[z-a]*.py"])
def test_a_pattern_the_matcher_cannot_read_is_refused_not_called_dead(
        repo: Path, tmp_path: Path, capsys, pattern: str) -> None:
    """⛔ The two must not merge: "dead" tells the reader to delete the entry,
    and every pattern here may well be starting the workflow today."""
    _record_and_generate(repo, tmp_path / "t")
    _add_pattern(repo, pattern)
    capsys.readouterr()
    assert _main(repo, "generate", "--check") == 1
    err = capsys.readouterr().err
    assert "UNMODELLED" in err and repr(pattern) in err
    assert "DEAD" not in err


def test_a_bad_character_class_is_named_not_raised() -> None:
    assert "not valid" in paths_filter_glob.unmodelled("ztests/[z-a]*.ts")
    assert paths_filter_glob.covers("ztests/[z-a]*.ts", "ztests/zx.ts") is False
    assert paths_filter_glob.match_segments(["[z-a]*.ts"], ["zx.ts"]) is False


def test_push_paths_are_checked_too(repo: Path, capsys) -> None:
    (repo / ".github/workflows/zother.yaml").write_text(
        "on:\n  push:\n    paths:\n      - 'zdata/**'\n      - 'zgone/**'\n"
        "      - 'zlib/z+.py'\n"
        "jobs:\n  zjob:\n    runs-on: x\n    steps:\n      - run: 'true'\n",
        encoding="utf-8")
    _git(repo, "add", "-A")
    assert _main(repo, "generate", "--check") == 1
    err = capsys.readouterr().err
    assert "zother.yaml on.push.paths: 'zgone/**'" in err
    assert "UNMODELLED  zother.yaml on.push.paths: 'zlib/z+.py'" in err
    assert err.count("DEAD ") == 1 and err.count("UNMODELLED") == 1, err


def test_a_dorny_job_gate_is_refused_not_left_unchecked(repo: Path, capsys,
                                                        monkeypatch) -> None:
    """#3756 retired the last job-level dorny gates and, with them, this
    tool's reading of a dorny `filters:` block. A new one must not become a
    path gate nothing looks at: it is reported, and no ledger excuses it."""
    (repo / ".github/workflows/zdorny.yaml").write_text(
        "on: [pull_request]\njobs:\n  zdetect:\n    runs-on: x\n    steps:\n"
        "      - uses: dorny/paths-filter@v3\n        with:\n          filters: |\n"
        "            zdocs:\n              - 'zmissing/*.md'\n",
        encoding="utf-8")
    # Control: the action named in a `run:` string is not a step that uses it.
    (repo / ".github/workflows/ztalk.yaml").write_text(
        "on: [pull_request]\njobs:\n  zjob:\n    runs-on: x\n    steps:\n"
        "      - run: echo dorny/paths-filter\n", encoding="utf-8")
    _git(repo, "add", "-A")
    for ledger in ({}, {"zdorny.yaml": (wpf.DIFF_DRIVEN, "zwhy")}):
        monkeypatch.setattr(wpf, "ADVISORY_PATH_GATES", ledger)
        capsys.readouterr()
        assert _main(repo, "generate", "--check") == 1
        err = capsys.readouterr().err
        assert "UNMODELLED  zdorny.yaml: a job uses dorny/paths-filter" in err
        assert "ztalk.yaml" not in err


@pytest.mark.parametrize("uses", [
    "Dorny/Paths-Filter@v3",
    "DORNY/PATHS-FILTER@0e4a8c6effa4802afeda77dc8d303f8176d7dfad",
    "tj-actions/changed-files@v45",
    "docker://ghcr.io/dorny/paths-filter:v3",
])
def test_a_path_filter_action_is_refused_in_every_spelling(
        repo: Path, capsys, uses: str) -> None:
    """GitHub resolves `owner/repo` case-insensitively, so `Dorny/Paths-Filter`
    is the same action; a case-sensitive match reported it as nothing (rc 0).
    `tj-actions/changed-files` is on the list because the required-check
    contract test refuses it — one list, read by both."""
    (repo / ".github/workflows/zfilter.yaml").write_text(
        "on: [pull_request]\njobs:\n  zdetect:\n    runs-on: x\n    steps:\n"
        f"      - uses: {uses}\n", encoding="utf-8")
    _git(repo, "add", "-A")
    assert _main(repo, "generate", "--check") == 1
    err = capsys.readouterr().err
    assert f"UNMODELLED  zfilter.yaml: a job uses {uses} (zdetect)" in err, err


def test_path_filter_steps_reads_steps_not_mentions() -> None:
    workflow = {"jobs": {
        "zq-a": {"steps": [{"name": "zq-f", "uses": "Dorny/Paths-Filter@v3"},
                           {"run": "echo dorny/paths-filter"},
                           {"uses": "actions/checkout@v6"}]},
        "zq-b": {"uses": "./.github/workflows/dorny/paths-filter.yaml"},
        "zq-c": {"steps": [{"uses": "tj-actions/changed-files@v45"}]}}}
    assert wpf.path_filter_steps(workflow) == [
        ("zq-a", "zq-f", "Dorny/Paths-Filter@v3"),
        ("zq-c", 0, "tj-actions/changed-files@v45")]
    assert wpf.path_filter_steps({}) == []
    assert wpf.path_filter_steps({"jobs": None}) == []


def test_an_unparseable_workflow_is_no_answer(repo: Path) -> None:
    (repo / ".github/workflows/zbroken.yaml").write_text("on: [pull_request\n",
                                                         encoding="utf-8")
    with pytest.raises(CallerError, match="zbroken.yaml does not parse"):
        _main(repo, "generate", "--check")


# ── which workflows are traced ──────────────────────────────────────────────

GATED = {
    "zgated.yaml": "on:\n  pull_request:\n    paths:\n      - 'zdata/**'\njobs: {}\n",
}


@pytest.mark.parametrize("name", sorted(GATED))
def test_a_path_gated_workflow_is_traced_or_named(repo: Path, tmp_path: Path, capsys,
                                                  monkeypatch, name: str) -> None:
    _record_and_generate(repo, tmp_path / "t")
    (repo / ".github/workflows" / name).write_text(GATED[name], encoding="utf-8")
    # Control: a workflow with no path gate needs no entry.
    (repo / ".github/workflows/zfree.yaml").write_text(
        "on: [pull_request]\njobs: {}\n", encoding="utf-8")
    _git(repo, "add", "-A")
    capsys.readouterr()
    assert _main(repo, "generate", "--check") == 1
    err = capsys.readouterr().err
    assert f"UNTRACED    {name}" in err and "zfree.yaml" not in err

    monkeypatch.setattr(wpf, "ADVISORY_PATH_GATES", {name: (wpf.DIFF_DRIVEN, "why")})
    assert _main(repo, "generate", "--check") == 0

    # Both stale directions: the named workflow is traced now, or is gone.
    why = (wpf.DIFF_DRIVEN, "why")
    monkeypatch.setattr(wpf, "ADVISORY_PATH_GATES",
                        {name: why, WORKFLOW: why, "zgone.yaml": why, "zfree.yaml": why})
    capsys.readouterr()
    assert _main(repo, "generate", "--check") == 1
    err = capsys.readouterr().err
    assert f"ADVISORY_PATH_GATES names {WORKFLOW}, which carries the generated regions" in err
    assert "ADVISORY_PATH_GATES names zgone.yaml, which has no path gate" in err
    assert "ADVISORY_PATH_GATES names zfree.yaml, which has no path gate" in err
    assert name not in err

    # #3756 phase 2 — the kind. An entry that is not a (kind, why) pair with
    # a known kind is a finding, whatever else is right about it: the old
    # free-text shape, a kind nobody defined, a pair of the wrong length.
    for bad in ("the old free-text reason", ("cheap-verification", "why"),
                (wpf.DIFF_DRIVEN,), (wpf.DIFF_DRIVEN, "why", "extra"),
                [wpf.DIFF_DRIVEN, "why"], (wpf.DIFF_DRIVEN, ""),
                (wpf.DIFF_DRIVEN, "  "), (wpf.DIFF_DRIVEN, None)):
        monkeypatch.setattr(wpf, "ADVISORY_PATH_GATES", {name: bad})
        capsys.readouterr()
        assert _main(repo, "generate", "--check") == 1, bad
        err = capsys.readouterr().err
        assert f"UNCLASSIFIED ADVISORY_PATH_GATES[{name!r}]" in err, (bad, err)

    # The read-assertion queue and the entries awaiting it are one set, both
    # ways, and the queue names nothing twice.
    waits = (wpf.AWAITS_READ_ASSERTION, "why")
    monkeypatch.setattr(wpf, "ADVISORY_PATH_GATES", {name: waits})
    capsys.readouterr()
    assert _main(repo, "generate", "--check") == 1
    assert (f"UNORDERED   {name} is an awaits-read-assertion entry that "
            "READ_ASSERTION_ORDER does not place") in capsys.readouterr().err
    monkeypatch.setattr(wpf, "READ_ASSERTION_ORDER", (name,))
    assert _main(repo, "generate", "--check") == 0
    monkeypatch.setattr(wpf, "ADVISORY_PATH_GATES", {name: why})
    capsys.readouterr()
    assert _main(repo, "generate", "--check") == 1
    assert (f"UNORDERED   {name} is in READ_ASSERTION_ORDER without an "
            "awaits-read-assertion entry") in capsys.readouterr().err
    monkeypatch.setattr(wpf, "ADVISORY_PATH_GATES", {name: waits})
    monkeypatch.setattr(wpf, "READ_ASSERTION_ORDER", (name, name))
    capsys.readouterr()
    assert _main(repo, "generate", "--check") == 1
    assert "READ_ASSERTION_ORDER names a workflow twice" in capsys.readouterr().err


# ── the real tree ───────────────────────────────────────────────────────────

def test_the_real_workflows_are_in_sync(capsys) -> None:
    """What the `workflow-path-filter-check` hook runs. Here as well because
    the Python Tests legs run on every PR and the hook's own job may not."""
    rc = wpf.main(["generate", "--check"])
    assert rc == 0, capsys.readouterr().err


def test_the_motivating_files_are_in_the_generated_region() -> None:
    """Vacuity tripwire. Every assertion above about the real tree is negative
    ("nothing to fix"); a generator that derived nothing would pass them."""
    name = "image-ref-resolve.yaml"
    path = ROOT / ".github" / "workflows" / name
    traced = wpf.Traced(ROOT, path, wpf.tracked_files(ROOT))
    for expected in ("scripts/tools/_lib_input.py",                   # the #3756 file
                     "components/da-tools/app/VERSION",                # opened, not imported
                     "requirements/ci-constraints.txt",                # pip's `-c`
                     "Makefile",                                       # read by make
                     ".python-version",                                # read by setup-python
                     # Read by every `make`: the Makefile's `CHART_VER :=
                     # $(shell grep … Chart.yaml)` runs when it is parsed. The
                     # Python tracer this replaced never saw it, and nobody
                     # had listed it by hand.
                     "helm/threshold-exporter/Chart.yaml",
                     "scripts/ops/go_build.sh",                        # read by sh
                     "scripts/ops/workflow_path_filter.py",            # MACHINERY
                     "scripts/ops/atime_trace.py",                     # MACHINERY
                     "scripts/ops/paths_filter_glob.py",               # …and its import
                     "components/threshold-exporter/app/third_party/yaml.v3/*.go",
                     "components/threshold-exporter/app/go.sum"):
        assert expected in traced.block, expected
    # Every one of those but MACHINERY and the closure is there because the
    # committed trace names it — measured, not listed by hand.
    job = traced.trace["jobs"]["resolve"]
    assert {"Makefile", ".python-version", "requirements/ci-constraints.txt",
            "helm/threshold-exporter/Chart.yaml", "scripts/ops/go_build.sh",
            "components/threshold-exporter/app/go.sum"} <= set(job["files"])
    assert not any("path_trace" in e for e in traced.block)
    assert paths_filter_glob.on_paths_covers(traced.paths_now(), "scripts/tools/_lib_input.py")
    assert traced.wiring_errors() == []


def test_every_real_ledger_entry_has_its_reason_written_down() -> None:
    reasons = ([(name, why) for name, (_kind, why) in wpf.ADVISORY_PATH_GATES.items()]
               + [(k, why) for k, why in wpf.RESERVED_PATTERNS.items()])
    assert reasons
    for key, why in reasons:
        assert why.strip() and len(why) > 20, key
    # The required-check workflows carry no path gate since #3756, so a
    # ledger entry for one would be an excuse for a gate that must not exist.
    for name in ("ci.yml", "docs-ci.yaml", "validate.yaml"):
        assert name not in wpf.ADVISORY_PATH_GATES


def test_the_cheap_verification_workflows_gate_on_nothing() -> None:
    """#3756, owner ruling 2026-11-14: a cheap verification job does not keep
    a list of what it reads — it runs on every PR. These three dropped their
    `paths` with the ledger's kinds; one that grows a list again is UNTRACED
    in `generate --check` (it has no entry), and this says which rule it broke.
    """
    for name in ("security-audit.yaml", "required-checks-drift.yml", "playwright.yml"):
        assert name not in wpf.ADVISORY_PATH_GATES
        doc = wpf.load_workflow(ROOT / ".github" / "workflows" / name)
        triggers = doc.get(True) or doc.get("on") or {}
        assert "pull_request" in triggers, name
        for event, cfg in triggers.items():
            assert not (isinstance(cfg, dict) and ("paths" in cfg or "paths-ignore" in cfg)), \
                f"{name}: on.{event} gates on paths"
        # "Every PR" has two more ways to stop being true without a `paths`
        # key (an adversarial review walked through both): the trigger
        # narrowed by branch or activity type, and the job switched off.
        pr = triggers["pull_request"] or {}
        assert set(pr) <= {"branches"} and "main" in pr.get("branches", ["main"]), \
            f"{name}: on.pull_request is narrowed: {pr!r}"
        for job_id, job in doc["jobs"].items():
            assert "if" not in job, f"{name}: job {job_id} runs conditionally"


def test_the_real_ledger_holds_both_kinds_and_the_ruled_members() -> None:
    """The classification the ruling states by name, so a re-labelling is a
    visible edit here and not a one-word change in the ledger."""
    assert wpf.ADVISORY_KINDS == (wpf.DIFF_DRIVEN, wpf.AWAITS_READ_ASSERTION)
    by_kind = {kind: sorted(n for n, (k, _w) in wpf.ADVISORY_PATH_GATES.items() if k == kind)
               for kind in wpf.ADVISORY_KINDS}
    # Empty since #3756 stage 3: component-docker-build.yaml, backtest.yaml
    # and forge-e2e-github.yaml each left this list when it was wired.
    assert by_kind[wpf.AWAITS_READ_ASSERTION] == []
    assert by_kind[wpf.DIFF_DRIVEN] == [
        "bench-gate-pr.yaml", "bench-probe-write-latency.yaml",
        "bench-workload-effect.yaml", "bench-workload-record.yaml",
        "config-diff-advisory.yml", "guard-defaults-impact.yml"]
    assert wpf.READ_ASSERTION_ORDER == ()


def _all_real_patterns() -> list[str]:
    patterns: set[str] = set()
    for path in sorted((ROOT / ".github" / "workflows").glob("*.y*ml")):
        for _label, rules in wpf.pattern_lists(wpf.load_workflow(path), path.name):
            for rule in rules:
                glob = wpf._filter_rule_glob(rule)
                if glob and not glob.startswith("!"):
                    patterns.add(glob)
    return sorted(patterns)


def test_the_matcher_agrees_with_picomatch_on_every_real_pattern_and_file() -> None:
    """The matcher is compared with picomatch (`dot: true`), cell for cell:
    every positive pattern in every workflow against every tracked file.

    ⚠️ GitHub's own `on.<event>.paths` matcher is not picomatch and cannot be
    run here; the alphabet `unmodelled` admits is the part its documentation
    describes the same way. picomatch was the matcher of the dorny filters
    this module also graded until #3756; it is kept as the one executable
    reference there is, so a matcher edit is still measured against something
    other than itself.

    Same contract as tests/lint/test_editor_schema_glob_parity.py: no node or
    no picomatch is a skip locally and a failure under VIBE_REQUIRE_PICOMATCH=1.
    """
    env_dir = os.environ.get("PICOMATCH_DIR")
    candidates = ([Path(env_dir)] if env_dir else []) + [
        ROOT / "tests" / "e2e" / "node_modules" / "picomatch",
        ROOT / "tools" / "portal" / "node_modules" / "picomatch"]
    pm_dir = next((c for c in candidates if (c / "package.json").is_file()), None)
    node = shutil.which("node")
    if pm_dir is None or node is None:
        msg = ("real picomatch not resolvable (set PICOMATCH_DIR or install "
               "tests/e2e node_modules)")
        if os.environ.get("VIBE_REQUIRE_PICOMATCH") == "1":
            pytest.fail(f"VIBE_REQUIRE_PICOMATCH=1 but {msg}")
        pytest.skip(msg + " — the matcher was not compared with picomatch")
    patterns = _all_real_patterns()
    files = list(wpf.tracked_files(ROOT))
    assert len(patterns) > 100 and len(files) > 1000, (len(patterns), len(files))
    refused = {p: paths_filter_glob.unmodelled(p) for p in patterns
               if paths_filter_glob.unmodelled(p)}
    assert not refused, refused
    script = (
        "const pm=require(process.argv[1]);"
        "const [pats,files]=JSON.parse(require('fs').readFileSync(0,'utf8'));"
        "console.log(JSON.stringify(pats.map(p=>{const m=pm(p,{dot:true});"
        "const hit=[];files.forEach((f,i)=>{if(m(f))hit.push(i)});return hit})));")
    done = subprocess.run(
        [node, "-e", script, str(pm_dir)], input=json.dumps([patterns, files]),
        capture_output=True, text=True, encoding="utf-8", check=True, timeout=300)
    real = json.loads(done.stdout)
    differing = {}
    for pattern, hits in zip(patterns, real):
        ours = [i for i, f in enumerate(files) if paths_filter_glob.covers(pattern, f)]
        if ours != hits:
            odd = sorted(set(ours) ^ set(hits))[:5]
            differing[pattern] = [(files[i], "picomatch" if i in set(hits) else "ours")
                                  for i in odd]
    assert not differing, differing
    # …and the three shapes where this module once disagreed with picomatch.
    done = subprocess.run(
        [node, "-e", script, str(pm_dir)],
        input=json.dumps([["*.zmd", "*zfoo", "za/*zx*", "za/*"],
                          [".zmd", "zfoo", "za/zx", "za"]]),
        capture_output=True, text=True, encoding="utf-8", check=True, timeout=300)
    # (`za/*` covers `za/zx`, index 2, and not the bare `za`, index 3.)
    assert json.loads(done.stdout) == [[0], [1], [2], [2]]
    assert paths_filter_glob.covers("*.zmd", ".zmd")
    assert paths_filter_glob.covers("*zfoo", "zfoo")
    assert paths_filter_glob.covers("za/*zx*", "za/zx")
    assert not paths_filter_glob.covers("za/*", "za")
