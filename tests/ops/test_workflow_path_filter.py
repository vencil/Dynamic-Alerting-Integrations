"""scripts/ops/workflow_path_filter.py + scripts/ops/path_trace/sitecustomize.py (#3756).

A workflow's `on.pull_request.paths` is generated from what its jobs were
measured to load, and the job's last step fails when it loads something the
list does not cover. Each test below is one way that chain could go quiet:

  * the tracer missing a load (a cached import, a module rolled back out of
    `sys.modules`, a child process);
  * the runtime check passing with an entry removed from the block;
  * `generate --check` passing with the block and the trace out of step;
  * a pattern that matches nothing, or one the matcher cannot read, passing —
    and the second being reported as the first.

Everything but the last section runs on a synthetic repository in `tmp_path`
(file names start with `z` so no scanner mistakes one for a real repo path);
the last section asks the same questions of the real tree.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
OPS = ROOT / "scripts" / "ops"
TRACER_DIR = OPS / "path_trace"
SCRIPT = OPS / "workflow_path_filter.py"

sys.path.append(str(OPS))
import paths_filter_glob  # noqa: E402
import workflow_path_filter as wpf  # noqa: E402

from _lib_exitcodes import CallerError  # noqa: E402  (scripts/tools, on sys.path via wpf)

WORKFLOW = "zflow.yaml"
HEAD = """\
name: zflow   # a comment that must survive
on:
  pull_request:
    paths:
      # hand-written: the data the job scans
      - "zdata/**"
      # BEGIN GENERATED path-trace (do not edit)
      # END GENERATED path-trace
  workflow_dispatch:
"""
JOBS = """\
jobs:
  zjob:
    runs-on: ubuntu-24.04
    steps:
      - run: python3 scripts/ops/workflow_path_filter.py env --trace-dir "$RUNNER_TEMP/t" >> "$GITHUB_ENV"
      - run: python3 ztool/zmain.py
      - if: ${{ !cancelled() }}
        run: >-
          python3 scripts/ops/workflow_path_filter.py assert
          --workflow zflow.yaml --job zjob --trace-dir "$RUNNER_TEMP/t"
"""


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True,
                   stdin=subprocess.DEVNULL, timeout=120)


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch) -> Path:
    """A git repository with one traced workflow and a tool that loads:
    zmain.py → zlib/zdirect.py (import), zlib/zrolled.py (imported, then
    removed from sys.modules), zdata/zconfig.txt and zlib/zopened.txt (opened),
    and — only on a branch the job never takes — zlib/zlazy.py, which imports
    zlib/zdeep.py."""
    repo = tmp_path / "zrepo"
    files = {
        f".github/workflows/{WORKFLOW}": HEAD + JOBS,
        "ztool/zmain.py": (
            "import os, sys\n"
            "HERE = os.path.dirname(os.path.abspath(__file__))\n"
            "sys.path.insert(0, os.path.join(HERE, os.pardir, 'zlib'))\n"
            "import zdirect\n"
            "import zrolled\n"
            "del sys.modules['zrolled']\n"
            "open(os.path.join(HERE, os.pardir, 'zdata', 'zconfig.txt')).read()\n"
            "open(os.path.join(HERE, os.pardir, 'zlib', 'zopened.txt')).read()\n"
            "if len(sys.argv) > 1:\n"
            "    import zlazy\n"),
        "zlib/zdirect.py": "VALUE = 1\n",
        "zlib/zrolled.py": "VALUE = 2\n",
        "zlib/zlazy.py": "def f():\n    import zdeep\n",
        "zlib/zdeep.py": "VALUE = 3\n",
        "zlib/zunrelated.py": "VALUE = 4\n",
        "zlib/zopened.txt": "x\n",
        "zdata/zconfig.txt": "y\n",
        ".gitignore": "__pycache__/\n",
    }
    for rel, text in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    monkeypatch.setattr(wpf, "NOT_YET_TRACED", {})
    return repo


def _run_traced(repo: Path, trace_dir: Path, *argv: str, times: int = 1) -> None:
    env = dict(os.environ, PYTHONPATH=str(TRACER_DIR), DA_PATH_TRACE_DIR=str(trace_dir))
    env.pop("PYTHONDONTWRITEBYTECODE", None)
    for _ in range(times):
        subprocess.run([sys.executable, "ztool/zmain.py", *argv], cwd=repo, env=env,
                       check=True, stdin=subprocess.DEVNULL, timeout=120)


def _main(repo: Path, *argv: str) -> int:
    return wpf.main(["--root", str(repo), *argv])


def _record_and_generate(repo: Path, trace_dir: Path) -> None:
    _run_traced(repo, trace_dir)
    assert _main(repo, "record", "--workflow", WORKFLOW, "--job", "zjob",
                 "--trace-dir", str(trace_dir)) == 0
    _git(repo, "add", "-A")
    assert _main(repo, "generate") == 0


def _block(repo: Path) -> list[str]:
    text = (repo / ".github/workflows" / WORKFLOW).read_text(encoding="utf-8")
    return wpf.split_block(text, WORKFLOW)[1]


# ── the tracer ──────────────────────────────────────────────────────────────

def test_tracer_records_imports_rollbacks_and_opens_from_a_cached_run(
        repo: Path, tmp_path: Path) -> None:
    """⛔ Run TWICE: the second run imports from `__pycache__`, where the source
    is never opened — a tracer listening to `open` alone records the first run
    and misses every later one. And `zrolled` is gone from `sys.modules` by
    exit, so a tracer that only dumps `sys.modules` never sees it."""
    first, second = tmp_path / "t1", tmp_path / "t2"
    _run_traced(repo, first)
    _run_traced(repo, second)
    tracked = frozenset(wpf.tracked_files(repo))
    for trace_dir in (first, second):
        trace = wpf.read_trace_dir(repo, trace_dir, tracked)
        assert {"ztool/zmain.py", "zlib/zdirect.py", "zlib/zrolled.py"} <= trace["code"]
        assert {"zdata/zconfig.txt", "zlib/zopened.txt"} <= trace["opened"]
        assert "zlib/zlazy.py" not in trace["code"] | trace["opened"]
        assert {"zlib", "ztool"} <= trace["import_dirs"]


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
    env = dict(os.environ, PYTHONPATH=str(TRACER_DIR), DA_PATH_TRACE_DIR=str(trace_dir))
    subprocess.run(
        [sys.executable, "-c",
         "import subprocess, sys; subprocess.run([sys.executable, 'ztool/zmain.py'], check=True)"],
        cwd=repo, env=env, check=True, stdin=subprocess.DEVNULL, timeout=120)
    trace = wpf.read_trace_dir(repo, trace_dir, frozenset(wpf.tracked_files(repo)))
    assert "zlib/zdirect.py" in trace["code"]


def test_env_prints_the_two_lines_a_job_exports(repo: Path, tmp_path: Path, capsys,
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
    workflow = repo / ".github/workflows" / WORKFLOW
    before = workflow.read_bytes()
    _record_and_generate(repo, tmp_path / "t")
    # zdata/zconfig.txt is NOT here: the hand-written `zdata/**` covers it.
    # zlazy.py was never executed by the job: it is reached through the literal
    # `import zlazy` in zmain.py, and zdeep.py through the import inside
    # zlazy's function.
    assert _block(repo) == sorted(_block(repo))
    assert set(_block(repo)) == {
        f".github/path-traces/{WORKFLOW}.json", f".github/workflows/{WORKFLOW}",
        "zlib/zdirect.py", "zlib/zopened.txt", "zlib/zrolled.py", "ztool/zmain.py",
        "zlib/zdeep.py", "zlib/zlazy.py"}
    assert "zlib/zunrelated.py" not in _block(repo)
    # Byte-for-byte outside the block: strip the generated lines and compare.
    after = workflow.read_bytes().decode("utf-8")
    stripped = "".join(
        line for line in after.splitlines(keepends=True)
        if not (line.startswith('      - "') and line.strip()[3:-1] in _block(repo)))
    assert stripped.encode("utf-8") == before
    assert _main(repo, "generate", "--check") == 0
    assert _main(repo, "generate") == 0  # idempotent
    assert workflow.read_bytes().decode("utf-8") == after


def test_generate_keeps_crlf_line_endings(repo: Path, tmp_path: Path) -> None:
    workflow = repo / ".github/workflows" / WORKFLOW
    workflow.write_bytes(workflow.read_bytes().replace(b"\n", b"\r\n"))
    _record_and_generate(repo, tmp_path / "t")
    data = workflow.read_bytes()
    assert b"\r\n" in data and b"\n" not in data.replace(b"\r\n", b"")


def test_check_is_red_when_the_block_and_the_trace_disagree(
        repo: Path, tmp_path: Path, capsys) -> None:
    _record_and_generate(repo, tmp_path / "t")
    trace_file = repo / ".github/path-traces" / f"{WORKFLOW}.json"
    doc = json.loads(trace_file.read_text(encoding="utf-8"))

    # The trace gains a file the block does not have.
    doc["jobs"]["zjob"]["files"].append("zlib/zunrelated.py")
    trace_file.write_text(json.dumps(doc), encoding="utf-8")
    capsys.readouterr()
    assert _main(repo, "generate", "--check") == 1
    err = capsys.readouterr().err
    assert "DRIFT" in err and "missing: zlib/zunrelated.py" in err

    # The block holds an entry nothing derives.
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


# ── the runtime check ───────────────────────────────────────────────────────

def test_assert_names_a_loaded_file_taken_out_of_the_block(
        repo: Path, tmp_path: Path, capsys) -> None:
    trace_dir = tmp_path / "t"
    _record_and_generate(repo, trace_dir)
    args = ("assert", "--workflow", WORKFLOW, "--job", "zjob", "--trace-dir", str(trace_dir))
    capsys.readouterr()
    assert _main(repo, *args) == 0

    workflow = repo / ".github/workflows" / WORKFLOW
    text = workflow.read_text(encoding="utf-8")
    line = '      - "zlib/zrolled.py"\n'
    assert text.count(line) == 1
    workflow.write_text(text.replace(line, ""), encoding="utf-8")
    out = tmp_path / "out.json"
    assert _main(repo, *args, "--out", str(out)) == 1
    err = capsys.readouterr().err
    assert "UNCOVERED  zlib/zrolled.py" in err
    assert err.count("UNCOVERED") == 1, err
    # …and the artifact is the trace file's own shape, ready to be committed.
    recorded = json.loads(out.read_text(encoding="utf-8"))["jobs"]["zjob"]
    assert "zlib/zrolled.py" in recorded["files"]
    # `--check` reds on the same edit, before CI is ever reached.
    assert _main(repo, "generate", "--check") == 1


def test_assert_sees_a_load_the_recorded_trace_never_had(
        repo: Path, tmp_path: Path, capsys) -> None:
    """The convergence argument: a new load starts from a listed file, the
    workflow runs, and the check names what the list lacks. Here the hand-
    written list is narrowed instead, so a file that WAS covered is not."""
    trace_dir = tmp_path / "t"
    _record_and_generate(repo, trace_dir)
    workflow = repo / ".github/workflows" / WORKFLOW
    text = workflow.read_text(encoding="utf-8")
    workflow.write_text(text.replace('      - "zdata/**"\n', '      - "zother/**"\n'),
                        encoding="utf-8")
    capsys.readouterr()
    assert _main(repo, "assert", "--workflow", WORKFLOW, "--job", "zjob",
                 "--trace-dir", str(trace_dir)) == 1
    assert "UNCOVERED  zdata/zconfig.txt" in capsys.readouterr().err


def test_assert_refuses_a_trace_that_measured_nothing(repo: Path, tmp_path: Path) -> None:
    _record_and_generate(repo, tmp_path / "t")
    args = ("assert", "--workflow", WORKFLOW, "--job", "zjob", "--trace-dir")
    with pytest.raises(CallerError, match="no Python trace"):
        _main(repo, *args, str(tmp_path / "zmissing"))
    # A trace taken in ANOTHER checkout holds no file of this one.
    empty = tmp_path / "zempty"
    empty.mkdir()
    (empty / "py-1-00.log").write_text("exec\t/zelsewhere/zmain.py\n", encoding="utf-8")
    with pytest.raises(CallerError, match="no first-party file"):
        _main(repo, *args, str(empty))
    with pytest.raises(CallerError, match="no job `znope`"):
        _main(repo, "assert", "--workflow", WORKFLOW, "--job", "znope",
              "--trace-dir", str(tmp_path / "t"))


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
    args = ("assert", "--workflow", WORKFLOW, "--job", "zjob", "--trace-dir", str(trace_dir))
    capsys.readouterr()
    assert _main(repo, *args) == 0
    (repo / "zapp/znew").mkdir()
    (repo / "zapp/znew/znew.go").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "-A")
    with (bindir / "zlisting").open("a", encoding="utf-8") as fh:
        fh.write(f"{repo}/zapp/znew\t{repo}/zapp/go.mod\t\n")
    assert _main(repo, *args) == 1
    assert "UNCOVERED  zapp/znew/znew.go  (go build zapp ./cmd/ztool)" in capsys.readouterr().err


def test_go_build_without_the_trace_dir_writes_no_log(repo: Path, tmp_path: Path) -> None:
    bindir = _fake_go(tmp_path, repo)
    env = {k: v for k, v in os.environ.items() if k != "DA_PATH_TRACE_DIR"}
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    subprocess.run(["sh", str(OPS / "go_build.sh"), "zapp", "./cmd/ztool",
                    str(tmp_path / "zout")], cwd=repo, env=env, check=True,
                   stdin=subprocess.DEVNULL, timeout=120)
    assert not list(tmp_path.glob("**/go-build.log"))


# ── dead and unmodelled patterns ────────────────────────────────────────────

def _add_pattern(repo: Path, pattern: str) -> None:
    workflow = repo / ".github/workflows" / WORKFLOW
    text = workflow.read_text(encoding="utf-8")
    anchor = '      - "zdata/**"\n'
    assert text.count(anchor) == 1
    workflow.write_text(text.replace(anchor, anchor + f'      - "{pattern}"\n'),
                        encoding="utf-8")


def test_a_pattern_matching_no_tracked_file_is_dead(
        repo: Path, tmp_path: Path, capsys) -> None:
    _record_and_generate(repo, tmp_path / "t")
    _add_pattern(repo, "zlib/zmoved_away.py")
    capsys.readouterr()
    assert _main(repo, "generate", "--check") == 1
    err = capsys.readouterr().err
    assert "DEAD" in err and "'zlib/zmoved_away.py'" in err
    assert "UNMODELLED" not in err


@pytest.mark.parametrize("pattern", [
    "zlib/z?irect.py", "zlib/{zdirect,zlazy}.py", "zlib/[!a]direct.py", "zlib**/x.py"])
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


def test_dorny_filters_and_push_paths_are_checked_too(repo: Path, capsys) -> None:
    (repo / ".github/workflows/zother.yaml").write_text(
        "on:\n  push:\n    paths:\n      - 'zdata/**'\n      - 'zgone/**'\n"
        "jobs:\n  zdetect:\n    runs-on: x\n    steps:\n"
        "      - uses: dorny/paths-filter@v3\n        with:\n          filters: |\n"
        "            zdocs:\n              - 'zlib/**'\n"
        "              - added|modified: 'zmissing/*.md'\n"
        "              - 'zlib/z+.py'\n",
        encoding="utf-8")
    _git(repo, "add", "-A")
    assert _main(repo, "generate", "--check") == 1
    err = capsys.readouterr().err
    assert "zother.yaml on.push.paths: 'zgone/**'" in err
    assert "zother.yaml filter `zdocs`: 'zmissing/*.md'" in err
    assert "UNMODELLED  zother.yaml filter `zdocs`: 'zlib/z+.py'" in err
    assert err.count("DEAD") == 2 and err.count("UNMODELLED") == 1, err


def test_an_unparseable_workflow_is_no_answer(repo: Path) -> None:
    (repo / ".github/workflows/zbroken.yaml").write_text("on: [pull_request\n",
                                                         encoding="utf-8")
    with pytest.raises(CallerError, match="zbroken.yaml does not parse"):
        _main(repo, "generate", "--check")


# ── which workflows are traced ──────────────────────────────────────────────

def test_a_path_gated_workflow_is_traced_or_named(repo: Path, tmp_path: Path,
                                                  capsys, monkeypatch) -> None:
    _record_and_generate(repo, tmp_path / "t")
    (repo / ".github/workflows/zgated.yaml").write_text(
        "on:\n  pull_request:\n    paths:\n      - 'zdata/**'\njobs: {}\n", encoding="utf-8")
    _git(repo, "add", "-A")
    capsys.readouterr()
    assert _main(repo, "generate", "--check") == 1
    assert "UNTRACED    zgated.yaml" in capsys.readouterr().err

    monkeypatch.setattr(wpf, "NOT_YET_TRACED", {"zgated.yaml": "why"})
    assert _main(repo, "generate", "--check") == 0

    # Both stale directions: the named workflow is traced now, or is gone.
    monkeypatch.setattr(wpf, "NOT_YET_TRACED",
                        {"zgated.yaml": "why", WORKFLOW: "why", "zgone.yaml": "why"})
    capsys.readouterr()
    assert _main(repo, "generate", "--check") == 1
    err = capsys.readouterr().err
    assert f"NOT_YET_TRACED names {WORKFLOW}, which carries the generated block" in err
    assert "NOT_YET_TRACED names zgone.yaml, which has no path gate" in err
    assert "zgated.yaml" not in err


@pytest.mark.parametrize("old, new, message", [
    ("      - run: python3 scripts/ops/workflow_path_filter.py env "
     '--trace-dir "$RUNNER_TEMP/t" >> "$GITHUB_ENV"\n', "",
     "never runs `workflow_path_filter.py env`"),
    ("--workflow zflow.yaml --job zjob", "--workflow zflow.yaml --job zelse",
     "has no step running"),
    ("      - if: ${{ !cancelled() }}\n", "      - if: ${{ success() }}\n",
     "must carry `if: ${{ !cancelled() }}`"),
    ("      - if: ${{ !cancelled() }}\n",
     "      - if: ${{ !cancelled() }}\n        continue-on-error: true\n",
     "sets `continue-on-error`"),
])
def test_a_traced_job_must_set_the_tracer_up_and_end_on_assert(
        repo: Path, tmp_path: Path, capsys, old: str, new: str, message: str) -> None:
    _record_and_generate(repo, tmp_path / "t")
    workflow = repo / ".github/workflows" / WORKFLOW
    text = workflow.read_text(encoding="utf-8")
    assert text.count(old) == 1, old
    workflow.write_text(text.replace(old, new), encoding="utf-8")
    capsys.readouterr()
    assert _main(repo, "generate", "--check") == 1
    assert message in capsys.readouterr().err


def test_a_job_without_a_recorded_trace_is_a_finding(repo: Path, capsys) -> None:
    assert _main(repo, "generate", "--check") == 1
    assert "job `zjob` has no recorded trace" in capsys.readouterr().err


def test_markers_outside_the_pull_request_list_are_no_answer(repo: Path) -> None:
    workflow = repo / ".github/workflows" / WORKFLOW
    text = workflow.read_text(encoding="utf-8")
    moved = text.replace("      # END GENERATED path-trace\n", "").replace(
        "jobs:\n", "      # END GENERATED path-trace\njobs:\n")
    workflow.write_text(moved.replace(
        "  workflow_dispatch:\n", "  push:\n    paths:\n      - \"zlib/**\"\n"), encoding="utf-8")
    with pytest.raises(CallerError, match="not a quoted list entry|must sit inside"):
        _main(repo, "generate", "--check")


# ── the real tree ───────────────────────────────────────────────────────────

def test_the_real_workflows_are_in_sync(capsys) -> None:
    """What the `workflow-path-filter-check` hook runs. Here as well because
    the Python Tests legs run on every PR and the hook's own job may not."""
    rc = wpf.main(["generate", "--check"])
    assert rc == 0, capsys.readouterr().err


def test_the_motivating_file_is_in_the_generated_block() -> None:
    """Vacuity tripwire. Every assertion above about the real tree is negative
    ("nothing to fix"); a generator that derived nothing would pass them."""
    name = "image-ref-resolve.yaml"
    text = (ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")
    block = wpf.split_block(text, name)[1]
    for expected in ("scripts/tools/_lib_input.py",                   # the #3756 file
                     "components/da-tools/app/VERSION",                # opened, not imported
                     "components/threshold-exporter/app/third_party/yaml.v3/*.go",
                     "components/threshold-exporter/app/go.sum"):
        assert expected in block, expected
    paths = wpf.pull_request_paths(wpf.load_workflow(
        ROOT / ".github" / "workflows" / name), name)
    assert paths_filter_glob.on_paths_covers(paths, "scripts/tools/_lib_input.py")


def test_not_yet_traced_reasons_are_written_down() -> None:
    for name, why in wpf.NOT_YET_TRACED.items():
        assert why.strip() and len(why) > 20, name
