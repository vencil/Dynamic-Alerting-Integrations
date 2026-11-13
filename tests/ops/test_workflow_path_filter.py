"""scripts/ops/workflow_path_filter.py + scripts/ops/path_trace/sitecustomize.py (#3756).

A workflow's `on.pull_request.paths` is generated from what its jobs were
measured to load, and the job's last step fails when it loads something the
list does not cover. Each test below is one way that chain could go quiet:

  * the tracer missing a load (a cached import, a module rolled back out of
    `sys.modules`, a child process, the same relative name in two directories,
    an event it could not record);
  * the runtime check passing with an entry removed from the region;
  * `generate --check` passing with the region and the trace out of step, or
    with a job that only LOOKS wired;
  * a pattern that matches nothing, or one the matcher cannot read, passing —
    and the second being reported as the first.

Everything but the last section runs on a synthetic repository in `tmp_path`
(file names start with `z` so no scanner mistakes one for a real repo path);
the last section asks the same questions of the real tree.
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
TRACER_DIR = OPS / "path_trace"
SCRIPT = OPS / "workflow_path_filter.py"

sys.path.append(str(OPS))
import paths_filter_glob  # noqa: E402
import workflow_path_filter as wpf  # noqa: E402

from _lib_exitcodes import CallerError  # noqa: E402  (scripts/tools, on sys.path via wpf)

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
import os, sys, types
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, os.pardir)
sys.path.insert(0, os.path.join(ROOT, 'zlib'))
sys.path.insert(0, ROOT)
import zdirect
import zrolled
del sys.modules['zrolled']
import zpkg.zmod
open(os.path.join(ROOT, 'zdata', 'zconfig.txt')).read()
open(os.path.join(ROOT, 'zlib', 'zopened.txt')).read()
os.close(os.open(os.path.join(ROOT, 'zlib', 'zosopen.txt'), os.O_RDONLY))
os.close(os.open(os.path.join(ROOT, 'zlib', 'zwritten.txt'), os.O_WRONLY))
fake = types.ModuleType('zfake')
fake.__file__ = os.path.join(ROOT, 'zlib', 'zonlyinmodules.txt')
sys.modules['zfake'] = fake
if len(sys.argv) > 1:
    import zlazy
"""
FILES = {
    f".github/workflows/{WORKFLOW}": TEXT,
    "ztool/zmain.py": MAIN,
    "zlib/zdirect.py": "VALUE = 1\n",
    "zlib/zrolled.py": "VALUE = 2\n",
    "zlib/zlazy.py": "def f():\n    import zdeep\n",
    "zlib/zdeep.py": "VALUE = 3\n",
    "zlib/zunrelated.py": "VALUE = 4\n",
    "zlib/zopened.txt": "x\n",
    "zlib/zosopen.txt": "x\n",
    "zlib/zwritten.txt": "x\n",
    "zlib/zonlyinmodules.txt": "x\n",
    "zpkg/__init__.py": "",
    "zpkg/zmod.py": "def f():\n    from . import zsibling\n",
    "zpkg/zsibling.py": "VALUE = 5\n",
    "zdata/zconfig.txt": "y\n",
    ".gitignore": "__pycache__/\n",
}
# What `generate` must derive for the synthetic job, and why each is there.
EXPECTED_BLOCK = {
    f".github/path-traces/{WORKFLOW}.json", f".github/workflows/{WORKFLOW}",
    "ztool/zmain.py", "zlib/zdirect.py",
    "zlib/zrolled.py",            # removed from sys.modules before exit
    "zlib/zopened.txt",           # open()
    "zlib/zosopen.txt",           # os.open(O_RDONLY)
    "zlib/zonlyinmodules.txt",    # only ever visible in sys.modules at exit
    "zpkg/__init__.py", "zpkg/zmod.py",
    "zpkg/zsibling.py",           # literal RELATIVE import inside a function
    "zlib/zlazy.py",              # literal import on a branch the job never took
    "zlib/zdeep.py",              # …and the import inside zlazy's function
}


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True,
                   stdin=subprocess.DEVNULL, timeout=120)


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch) -> Path:
    """A git repository with one traced workflow and a tool whose loads cover
    every way a file can reach the trace (see EXPECTED_BLOCK)."""
    repo = tmp_path / "zrepo"
    for rel, text in FILES.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    # The real ledgers name real workflows; this tree has its own.
    monkeypatch.setattr(wpf, "NOT_YET_TRACED", {})
    monkeypatch.setattr(wpf, "BOUNDARY", {})
    monkeypatch.setattr(wpf, "RESERVED_PATTERNS", {})
    monkeypatch.setattr(wpf, "MACHINERY", ())
    monkeypatch.delenv("DA_PATH_TRACE_DIR", raising=False)
    return repo


def _traced_env(trace_dir: Path) -> dict[str, str]:
    env = dict(os.environ, PYTHONPATH=str(TRACER_DIR), DA_PATH_TRACE_DIR=str(trace_dir))
    env.pop("PYTHONDONTWRITEBYTECODE", None)
    return env


def _run_traced(repo: Path, trace_dir: Path, *argv: str,
                script: str = "ztool/zmain.py") -> None:
    subprocess.run([sys.executable, script, *argv], cwd=repo, env=_traced_env(trace_dir),
                   check=True, stdin=subprocess.DEVNULL, timeout=120)


def _main(repo: Path, *argv: str) -> int:
    return wpf.main(["--root", str(repo), *argv])


def _job_args(trace_dir: Path) -> tuple[str, ...]:
    return ("--workflow", WORKFLOW, "--job", "zjob", "--trace-dir", str(trace_dir))


def _record_and_generate(repo: Path, trace_dir: Path) -> None:
    _run_traced(repo, trace_dir)
    assert _main(repo, "record", *_job_args(trace_dir)) == 0
    _git(repo, "add", "-A")
    assert _main(repo, "generate") == 0


def _workflow(repo: Path) -> Path:
    return repo / ".github/workflows" / WORKFLOW


def _block(repo: Path) -> list[str]:
    return wpf.Traced(repo, _workflow(repo), wpf.tracked_files(repo)).block


def _edit(repo: Path, old: str, new: str) -> None:
    text = _workflow(repo).read_text(encoding="utf-8")
    assert text.count(old) == 1, old
    _workflow(repo).write_text(text.replace(old, new), encoding="utf-8")


def _trace(repo: Path, trace_dir: Path) -> dict:
    return wpf.read_trace_dir(repo, trace_dir, frozenset(wpf.tracked_files(repo)))


# ── the tracer ──────────────────────────────────────────────────────────────

def test_tracer_records_every_kind_of_load_from_a_cached_run(
        repo: Path, tmp_path: Path) -> None:
    """⛔ Run TWICE: the second run imports from `__pycache__`, where the source
    is never opened — a tracer listening to `open` alone records the first run
    and misses every later one. `zrolled` is gone from `sys.modules` by exit,
    so a tracer that only dumps `sys.modules` never sees it; `zfake` is ONLY
    in `sys.modules`, so a tracer without the dump never sees that one."""
    for trace_dir in (tmp_path / "t1", tmp_path / "t2"):
        _run_traced(repo, trace_dir)
        trace = _trace(repo, trace_dir)
        assert {"ztool/zmain.py", "zlib/zdirect.py", "zlib/zrolled.py",
                "zpkg/zmod.py", "zlib/zonlyinmodules.txt"} <= trace["code"]
        assert {"zdata/zconfig.txt", "zlib/zopened.txt",
                "zlib/zosopen.txt"} <= trace["opened"]
        loaded = trace["code"] | trace["opened"]
        assert "zlib/zlazy.py" not in loaded
        # Opened write-only: not a read, so not a dependency.
        assert "zlib/zwritten.txt" not in loaded
        assert {"zlib", "ztool"} <= trace["import_dirs"]


def test_tracer_resolves_a_relative_path_where_the_event_happened(
        repo: Path, tmp_path: Path) -> None:
    """`open("zsame.txt")`, `os.chdir`, `open("zsame.txt")` are two files. A
    tracer that de-duplicates the string it was handed records the first."""
    for rel in ("zdata/zsame.txt", "zlib/zsame.txt", "zdata/zsame.py", "zlib/zsame.py"):
        (repo / rel).write_text("x = 1\n", encoding="utf-8")
    (repo / "ztool/zcwd.py").write_text(
        "import os\n"
        "for d in ('zdata', os.path.join(os.pardir, 'zlib')):\n"
        "    os.chdir(d)\n"
        "    open('zsame.txt').read()\n"
        "    exec(compile(open('zsame.py').read(), 'zsame.py', 'exec'))\n",
        encoding="utf-8")
    _git(repo, "add", "-A")
    _run_traced(repo, tmp_path / "t", script="ztool/zcwd.py")
    trace = _trace(repo, tmp_path / "t")
    assert {"zdata/zsame.txt", "zlib/zsame.txt"} <= trace["opened"]
    assert {"zdata/zsame.py", "zlib/zsame.py"} <= trace["code"]


def test_an_event_the_tracer_cannot_record_never_reaches_the_program_but_fails_assert(
        repo: Path, tmp_path: Path) -> None:
    """A program may raise the audit events itself, with any arguments. The
    hook must not turn that into an exception inside the traced program — and
    must not drop it silently either: the trace is then incomplete, which
    `assert` reports as no answer, never as a pass."""
    (repo / "ztool/zaudit.py").write_text(
        "import sys\n"
        "sys.audit('open', 'zdata/zconfig.txt')\n"       # one argument, not three
        "sys.audit('open', 'zlib/zopened.txt', 7, 0)\n"  # a mode that is not text
        "print('survived')\n",
        encoding="utf-8")
    (repo / "ztool/zbroken.py").write_text(
        "import sys\nsys.audit('open')\nsys.audit('exec')\nprint('survived')\n",
        encoding="utf-8")
    _git(repo, "add", "-A")
    _record_and_generate(repo, tmp_path / "base")

    tolerant = tmp_path / "t1"
    _run_traced(repo, tolerant, script="ztool/zaudit.py")
    trace = _trace(repo, tolerant)  # no error line: both were recorded
    assert {"zdata/zconfig.txt", "zlib/zopened.txt"} <= trace["opened"]

    broken = tmp_path / "t2"
    _run_traced(repo, broken, script="ztool/zbroken.py")  # check=True: it exited 0
    with pytest.raises(CallerError, match="incomplete.*open event.*IndexError"):
        _main(repo, "assert", *_job_args(broken))


def test_tracer_is_inert_without_the_trace_dir(repo: Path, tmp_path: Path) -> None:
    env = dict(os.environ, PYTHONPATH=str(TRACER_DIR))
    env.pop("DA_PATH_TRACE_DIR", None)
    subprocess.run([sys.executable, "ztool/zmain.py"], cwd=repo, env=env, check=True,
                   stdin=subprocess.DEVNULL, timeout=120)
    assert not list(tmp_path.glob("**/py-*.log"))


def test_tracer_still_runs_the_sitecustomize_it_shadows(tmp_path: Path) -> None:
    """A Python build may ship its own `sitecustomize` (some distributions and
    container images do). Putting the tracer first on `PYTHONPATH` must not
    switch that one off for every interpreter of the job."""
    other = tmp_path / "zsite"
    other.mkdir()
    (other / "sitecustomize.py").write_text(
        "import os\nos.environ['ZSHADOWED_RAN'] = 'yes'\n", encoding="utf-8")
    for trace_dir in (None, tmp_path / "t"):
        env = dict(os.environ, PYTHONPATH=f"{TRACER_DIR}{os.pathsep}{other}")
        env.pop("DA_PATH_TRACE_DIR", None)
        if trace_dir:
            env["DA_PATH_TRACE_DIR"] = str(trace_dir)
        done = subprocess.run(
            [sys.executable, "-c", "import os; print(os.environ.get('ZSHADOWED_RAN'))"],
            env=env, capture_output=True, text=True, encoding="utf-8", check=True,
            stdin=subprocess.DEVNULL, timeout=120)
        assert done.stdout.strip() == "yes", done.stderr


def test_tracer_follows_a_child_interpreter(repo: Path, tmp_path: Path) -> None:
    """A job step is rarely the interpreter that does the loading: `make`, a
    shell script or the tool itself starts another one."""
    trace_dir = tmp_path / "t"
    subprocess.run(
        [sys.executable, "-c",
         "import subprocess, sys; subprocess.run([sys.executable, 'ztool/zmain.py'], check=True)"],
        cwd=repo, env=_traced_env(trace_dir), check=True, stdin=subprocess.DEVNULL,
        timeout=120)
    assert "zlib/zdirect.py" in _trace(repo, trace_dir)["code"]


def test_env_prints_the_two_lines_a_job_exports(tmp_path: Path, capsys,
                                                monkeypatch) -> None:
    monkeypatch.setenv("PYTHONPATH", "/zalready")
    assert wpf.main(["env", "--trace-dir", str(tmp_path / "t")]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines == [
        f"PYTHONPATH={TRACER_DIR.as_posix()}{os.pathsep}/zalready",
        f"DA_PATH_TRACE_DIR={(tmp_path / 't').resolve().as_posix()}"]


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
    assert steps[1]["run"].endswith('>> "$GITHUB_ENV"')
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
    trace_file = repo / ".github/path-traces" / f"{WORKFLOW}.json"
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


def test_a_malformed_trace_file_is_no_answer_not_an_empty_trace(
        repo: Path, tmp_path: Path) -> None:
    """Read as empty, a broken trace file would have `generate` rewrite the
    region down to the workflow's own two entries and exit 0."""
    _record_and_generate(repo, tmp_path / "t")
    trace_file = repo / ".github/path-traces" / f"{WORKFLOW}.json"
    before = _workflow(repo).read_bytes()
    for broken in ("{not json", '{"jobs": {"zjob": {"files": "x"}}}', '{"workflow": 1}',
                   '{"jobs": {"zjob": {"files": [], "import_dirs": [], "go": [{}]}}}'):
        trace_file.write_text(broken, encoding="utf-8")
        for argv in (("generate",), ("generate", "--check"),
                     ("assert", *_job_args(tmp_path / "t"))):
            with pytest.raises(CallerError, match="is not a trace file"):
                _main(repo, *argv)
    assert _workflow(repo).read_bytes() == before


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


def test_boundary_entries_are_written_and_checked(repo: Path, tmp_path: Path, capsys,
                                                  monkeypatch) -> None:
    (repo / "Zmakefile").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "-A")
    monkeypatch.setattr(wpf, "BOUNDARY", {WORKFLOW: {"Zmakefile": "zmake reads it"}})
    _record_and_generate(repo, tmp_path / "t")
    assert "Zmakefile" in _block(repo)

    monkeypatch.setattr(wpf, "BOUNDARY", {
        WORKFLOW: {"Zmakefile": "zmake reads it", "Zgone": "zwhy"},
        "zother.yaml": {"Zmakefile": "zwhy"}})
    capsys.readouterr()
    assert _main(repo, "generate", "--check") == 1
    err = capsys.readouterr().err
    assert "DEAD        BOUNDARY zflow.yaml: 'Zgone'" in err
    assert "STALE       BOUNDARY names zother.yaml" in err


def test_this_tool_is_listed_through_machinery(repo: Path, tmp_path: Path,
                                               monkeypatch) -> None:
    """`assert` and `record` drop their own trace, so the tool is listed by
    name, with whatever it imports."""
    for rel, text in (("zops/ztool.py", "import zhelper\n"), ("zops/zhelper.py", "")):
        (repo / rel).parent.mkdir(exist_ok=True)
        (repo / rel).write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    monkeypatch.setattr(wpf, "MACHINERY", ("zops/ztool.py",))
    monkeypatch.setattr(wpf, "MACHINERY_IMPORT_DIRS", ("zops",))
    _record_and_generate(repo, tmp_path / "t")
    assert {"zops/ztool.py", "zops/zhelper.py"} <= set(_block(repo))


# ── where the regions sit ───────────────────────────────────────────────────

def test_an_empty_region_outside_the_pull_request_list_is_refused_unwritten(
        repo: Path, tmp_path: Path) -> None:
    """The state a workflow is in the first time `generate` runs: nothing
    between the markers yet. Under `push.paths` the entries would be written
    there, the pull_request list would stay as it was, and the run would
    exit 0."""
    _run_traced(repo, tmp_path / "t")
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


# ── is the job wired: seven ways it only looks so, and four more ────────────

ENV_REGION = ("      # BEGIN GENERATED path-trace-env job=zjob\n"
              f"      - name: {wpf.ENV_STEP_NAME}\n"
              f"        run: {wpf.ENV_STEP_RUN}\n"
              "      # END GENERATED path-trace-env\n")
TOOL_STEP = "      - run: python3 ztool/zmain.py\n"
ASSERT_RUN = f"        run: {wpf.assert_step_run(WORKFLOW, 'zjob')}\n"


@pytest.mark.parametrize("old, new, message", [
    # 1. the assert's failure is swallowed by the shell
    (ASSERT_RUN, ASSERT_RUN.rstrip("\n") + " || true\n",
     "generated assert step exactly once"),
    # 2. …or by the job
    ("    runs-on: ubuntu-24.04\n", "    runs-on: ubuntu-24.04\n    continue-on-error: true\n",
     "job `zjob` sets `continue-on-error`"),
    # 3. the env step, byte-identical, moved after the step that does the loading
    (ENV_REGION + TOOL_STEP, TOOL_STEP + ENV_REGION,
     "runs a command before the env step"),
    # 4. the env lines are printed, not exported
    (' >> "$GITHUB_ENV"\n', "\n", "generated env step exactly once"),
    # 5. the assert command survives only in a shell comment
    (ASSERT_RUN, "        run: |\n          # " + ASSERT_RUN.strip()[5:] + "\n          true\n",
     "generated assert step exactly once"),
    # 6. a later step resets what the tracer lives on
    (TOOL_STEP, "      - run: PYTHONPATH= python3 ztool/zmain.py\n",
     "assigns, unsets or clears"),
    # 7. the assert reads another directory
    ('--trace-dir "$RUNNER_TEMP/path-trace" --out', '--trace-dir "$RUNNER_TEMP/zother" --out',
     "generated assert step exactly once"),
    # …and four more of the same family.
    (TOOL_STEP, "      - run: python3 ztool/zmain.py\n        env:\n          PYTHONPATH: zx\n",
     "step 3 sets PYTHONPATH / DA_PATH_TRACE_DIR in its `env:`"),
    (TOOL_STEP, "      - run: unset FOO DA_PATH_TRACE_DIR; python3 ztool/zmain.py\n",
     "assigns, unsets or clears"),
    (TOOL_STEP, "      - run: env -i python3 ztool/zmain.py\n", "assigns, unsets or clears"),
    ("      # END GENERATED path-trace-assert\n",
     "      # END GENERATED path-trace-assert\n      - run: python3 ztool/zmain.py late\n",
     "runs a command after the assert step"),
    ("        if: ${{ !cancelled() }}\n", "        if: ${{ success() }}\n",
     "generated assert step exactly once"),
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


def test_a_job_without_a_recorded_trace_is_a_finding(repo: Path, capsys) -> None:
    assert _main(repo, "generate", "--check") == 1
    assert "job `zjob` has no recorded trace" in capsys.readouterr().err


# ── the runtime check ───────────────────────────────────────────────────────

def test_assert_names_a_loaded_file_taken_out_of_the_region(
        repo: Path, tmp_path: Path, capsys) -> None:
    trace_dir = tmp_path / "t"
    _record_and_generate(repo, trace_dir)
    capsys.readouterr()
    assert _main(repo, "assert", *_job_args(trace_dir)) == 0

    _edit(repo, '      - "zlib/zrolled.py"\n', "")
    out = tmp_path / "out.json"
    assert _main(repo, "assert", *_job_args(trace_dir), "--out", str(out)) == 1
    err = capsys.readouterr().err
    assert "UNCOVERED  zlib/zrolled.py" in err
    assert err.count("UNCOVERED") == 1, err
    # `--check` reds on the same edit, before CI is ever reached.
    assert _main(repo, "generate", "--check") == 1


def test_assert_sees_a_load_the_recorded_trace_never_had(
        repo: Path, tmp_path: Path, capsys) -> None:
    """The convergence argument: a new load starts from a listed file, the
    workflow runs, and the check names what the list lacks. Here the hand-
    written list is narrowed instead, so a file that WAS covered is not."""
    trace_dir = tmp_path / "t"
    _record_and_generate(repo, trace_dir)
    _edit(repo, '      - "zdata/**"\n', '      - "zother/**"\n')
    capsys.readouterr()
    assert _main(repo, "assert", *_job_args(trace_dir)) == 1
    assert "UNCOVERED  zdata/zconfig.txt" in capsys.readouterr().err


def test_assert_refuses_a_trace_that_measured_nothing(repo: Path, tmp_path: Path) -> None:
    _record_and_generate(repo, tmp_path / "t")
    with pytest.raises(CallerError, match="no Python trace"):
        _main(repo, "assert", *_job_args(tmp_path / "zmissing"))
    # A trace taken in ANOTHER checkout holds no file of this one.
    empty = tmp_path / "zempty"
    empty.mkdir()
    (empty / "py-1-00.log").write_text("exec\t/zelsewhere/zmain.py\n", encoding="utf-8")
    with pytest.raises(CallerError, match="no first-party file"):
        _main(repo, "assert", *_job_args(empty))
    with pytest.raises(CallerError, match="no job `znope`"):
        _main(repo, "assert", "--workflow", WORKFLOW, "--job", "znope",
              "--trace-dir", str(tmp_path / "t"))


def test_record_is_reproducible_and_equals_what_assert_writes(
        repo: Path, tmp_path: Path) -> None:
    """In CI `assert` runs INSIDE the traced job, so the tracer logs the tool's
    own reads (the workflow, the trace file) next to the job's. Left in, a
    second `record` differs from the first by exactly those, and the artifact
    the failure message points at is not what `record` writes."""
    trace_dir = tmp_path / "t"
    _record_and_generate(repo, trace_dir)
    trace_file = repo / ".github/path-traces" / f"{WORKFLOW}.json"
    first = trace_file.read_bytes()
    env = _traced_env(trace_dir)
    out = tmp_path / "out.json"
    for argv in (["record", *_job_args(trace_dir)],
                 ["assert", *_job_args(trace_dir), "--out", str(out)],
                 ["record", *_job_args(trace_dir)]):
        done = subprocess.run(
            [sys.executable, str(SCRIPT), "--root", str(repo), *argv], env=env,
            capture_output=True, text=True, encoding="utf-8",
            stdin=subprocess.DEVNULL, timeout=120)
        assert done.returncode == 0, done.stderr
        assert trace_file.read_bytes() == first, argv
    assert out.read_bytes() == first


def test_cli_exits_2_with_one_error_line_when_it_cannot_measure(tmp_path: Path) -> None:
    done = subprocess.run(
        [sys.executable, str(SCRIPT), "assert", "--workflow", "image-ref-resolve.yaml",
         "--job", "resolve", "--trace-dir", str(tmp_path / "zmissing")],
        capture_output=True, text=True, encoding="utf-8", stdin=subprocess.DEVNULL,
        timeout=120,
        env={k: v for k, v in os.environ.items() if k != "DA_PATH_TRACE_DIR"})
    assert done.returncode == 2, done.stderr
    assert done.stderr.startswith("ERROR: no Python trace"), done.stderr
    assert done.stdout == ""


# ── Go builds ───────────────────────────────────────────────────────────────

def _fake_go(tmp_path: Path, repo: Path) -> Path:
    """A `go` that answers `go list -deps` with two in-repo packages (one
    embedding a file), one in the module cache, and accepts `go build`."""
    bindir = tmp_path / "zbin"
    bindir.mkdir()
    app = repo / "zapp"
    lines = [f"{app}/cmd/ztool\t{app}/go.mod\t",
             f"{app}/zpkg\t{app}/go.mod\tzembed.txt\x1f",
             "/zmodcache/zdep@v1\t/zmodcache/zdep@v1/go.mod\t"]
    listing = bindir / "zlisting"
    listing.write_text("\n".join(lines) + "\n", encoding="utf-8")
    go = bindir / "go"
    go.write_text(f'#!/bin/sh\nif [ "$1" = list ]; then cat "{listing}"; fi\nexit 0\n',
                  encoding="utf-8")
    go.chmod(0o755)
    for rel in ("zapp/go.mod", "zapp/go.sum", "zapp/cmd/ztool/main.go",
                "zapp/zpkg/zpkg.go", "zapp/zpkg/zembed.txt", "zapp/zpkg/znotes.md",
                "scripts/ops/go_build.sh"):
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text("x\n", encoding="utf-8")
    _git(repo, "add", "-A")
    return bindir


def test_go_build_is_recorded_and_its_sources_required(
        repo: Path, tmp_path: Path, monkeypatch, capsys) -> None:
    bindir = _fake_go(tmp_path, repo)
    trace_dir = tmp_path / "t"
    env = dict(os.environ, PATH=f"{bindir}{os.pathsep}{os.environ['PATH']}",
               DA_PATH_TRACE_DIR=str(trace_dir))
    subprocess.run(["sh", str(OPS / "go_build.sh"), "zapp", "./cmd/ztool",
                    str(tmp_path / "zout")], cwd=repo, env=env, check=True,
                   stdin=subprocess.DEVNULL, timeout=120)
    assert (trace_dir / "go-build.log").read_text(encoding="utf-8") == (
        f"{repo.resolve()}/zapp\t./cmd/ztool\n")

    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    _record_and_generate(repo, trace_dir)
    block = set(_block(repo))
    assert {"zapp/cmd/ztool/*.go", "zapp/zpkg/*.go", "zapp/zpkg/zembed.txt",
            "zapp/go.mod", "zapp/go.sum", "scripts/ops/go_build.sh"} <= block
    assert "zapp/zpkg/znotes.md" not in block
    assert not any("zmodcache" in e for e in block)

    # The runtime check asks `go list` again, so a package the build has since
    # grown is named even though the committed trace predates it.
    capsys.readouterr()
    assert _main(repo, "assert", *_job_args(trace_dir)) == 0
    (repo / "zapp/znew").mkdir()
    (repo / "zapp/znew/znew.go").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "-A")
    with (bindir / "zlisting").open("a", encoding="utf-8") as fh:
        fh.write(f"{repo}/zapp/znew\t{repo}/zapp/go.mod\t\n")
    assert _main(repo, "assert", *_job_args(trace_dir)) == 1
    assert "UNCOVERED  zapp/znew/znew.go  (go build zapp ./cmd/ztool)" in capsys.readouterr().err


def test_go_build_without_the_trace_dir_writes_no_log(repo: Path, tmp_path: Path) -> None:
    bindir = _fake_go(tmp_path, repo)
    env = {k: v for k, v in os.environ.items() if k != "DA_PATH_TRACE_DIR"}
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    subprocess.run(["sh", str(OPS / "go_build.sh"), "zapp", "./cmd/ztool",
                    str(tmp_path / "zout")], cwd=repo, env=env, check=True,
                   stdin=subprocess.DEVNULL, timeout=120)
    assert not list(tmp_path.glob("**/go-build.log"))


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


def test_dorny_filters_and_push_paths_are_checked_too(repo: Path, capsys,
                                                      monkeypatch) -> None:
    (repo / ".github/workflows/zother.yaml").write_text(
        "on:\n  push:\n    paths:\n      - 'zdata/**'\n      - 'zgone/**'\n"
        "jobs:\n  zdetect:\n    runs-on: x\n    steps:\n"
        "      - uses: dorny/paths-filter@v3\n        with:\n          filters: |\n"
        "            zdocs:\n              - 'zlib/**'\n"
        "              - added|modified: 'zmissing/*.md'\n"
        "              - 'zlib/z+.py'\n",
        encoding="utf-8")
    _git(repo, "add", "-A")
    monkeypatch.setattr(wpf, "NOT_YET_TRACED", {"zother.yaml": "zwhy"})
    assert _main(repo, "generate", "--check") == 1
    err = capsys.readouterr().err
    assert "zother.yaml on.push.paths: 'zgone/**'" in err
    assert "zother.yaml filter `zdocs`: 'zmissing/*.md'" in err
    assert "UNMODELLED  zother.yaml filter `zdocs`: 'zlib/z+.py'" in err
    assert err.count("DEAD ") == 2 and err.count("UNMODELLED") == 1, err


def test_an_unparseable_workflow_is_no_answer(repo: Path) -> None:
    (repo / ".github/workflows/zbroken.yaml").write_text("on: [pull_request\n",
                                                         encoding="utf-8")
    with pytest.raises(CallerError, match="zbroken.yaml does not parse"):
        _main(repo, "generate", "--check")


# ── which workflows are traced ──────────────────────────────────────────────

GATED = {
    "zgated.yaml": "on:\n  pull_request:\n    paths:\n      - 'zdata/**'\njobs: {}\n",
    # No `on.pull_request.paths` at all: the gate is a job's dorny step.
    "zdorny.yaml": ("on: [pull_request]\njobs:\n  zdetect:\n    runs-on: x\n    steps:\n"
                    "      - uses: dorny/paths-filter@v3\n        with:\n"
                    "          filters: |\n            zdocs:\n              - 'zlib/**'\n"),
}


@pytest.mark.parametrize("name", sorted(GATED))
def test_a_path_gated_workflow_is_traced_or_named(repo: Path, tmp_path: Path, capsys,
                                                  monkeypatch, name: str) -> None:
    _record_and_generate(repo, tmp_path / "t")
    (repo / ".github/workflows" / name).write_text(GATED[name], encoding="utf-8")
    # Control: a workflow with no path gate of either kind needs no entry.
    (repo / ".github/workflows/zfree.yaml").write_text(
        "on: [pull_request]\njobs: {}\n", encoding="utf-8")
    _git(repo, "add", "-A")
    capsys.readouterr()
    assert _main(repo, "generate", "--check") == 1
    err = capsys.readouterr().err
    assert f"UNTRACED    {name}" in err and "zfree.yaml" not in err

    monkeypatch.setattr(wpf, "NOT_YET_TRACED", {name: "why"})
    assert _main(repo, "generate", "--check") == 0

    # Both stale directions: the named workflow is traced now, or is gone.
    monkeypatch.setattr(wpf, "NOT_YET_TRACED",
                        {name: "why", WORKFLOW: "why", "zgone.yaml": "why",
                         "zfree.yaml": "why"})
    capsys.readouterr()
    assert _main(repo, "generate", "--check") == 1
    err = capsys.readouterr().err
    assert f"NOT_YET_TRACED names {WORKFLOW}, which carries the generated regions" in err
    assert "NOT_YET_TRACED names zgone.yaml, which has no path gate" in err
    assert "NOT_YET_TRACED names zfree.yaml, which has no path gate" in err
    assert name not in err


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
                     "requirements/ci-constraints.txt",                # pip's `-c`, traced
                     "Makefile", ".python-version",                    # BOUNDARY
                     "scripts/ops/workflow_path_filter.py",            # MACHINERY
                     "scripts/ops/paths_filter_glob.py",               # …and its import
                     "components/threshold-exporter/app/third_party/yaml.v3/*.go",
                     "components/threshold-exporter/app/go.sum"):
        assert expected in traced.block, expected
    assert paths_filter_glob.on_paths_covers(traced.paths_now(), "scripts/tools/_lib_input.py")
    assert traced.wiring_errors() == []


def test_every_real_ledger_entry_has_its_reason_written_down() -> None:
    reasons = (list(wpf.NOT_YET_TRACED.items())
               + [(k, why) for k, why in wpf.RESERVED_PATTERNS.items()]
               + [((wf, e), why) for wf, entries in wpf.BOUNDARY.items()
                  for e, why in entries.items()])
    assert reasons
    for key, why in reasons:
        assert why.strip() and len(why) > 20, key
    # The dorny-gated required-check workflows are listed, not out of scope.
    for name in ("ci.yml", "docs-ci.yaml", "validate.yaml"):
        assert "dorny" in wpf.NOT_YET_TRACED[name]


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
    """The matcher stands in for picomatch (dorny/paths-filter passes it
    `dot: true`). Held cell for cell: every positive pattern in every workflow
    against every tracked file.

    ⚠️ GitHub's own `on.<event>.paths` matcher is not picomatch and cannot be
    run here; the alphabet `unmodelled` admits is the part its documentation
    describes the same way. This test anchors the dorny half.

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
