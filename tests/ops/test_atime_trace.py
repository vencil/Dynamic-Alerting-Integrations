"""scripts/ops/atime_trace.py (#3756): which tracked files a stretch of a job read.

Every test runs the tool on a small git repository in `tmp_path` and does real
reads there — the tool's answer IS the filesystem's, so nothing is mocked except
where a failure cannot be produced on demand (an access time that cannot be
set, a filesystem that answers differently).

⛔ A filesystem that does not record reads makes every test here meaningless,
in both directions. `_measurable` asks the tool's own canary first and FAILS —
it does not skip — when the answer is no: "could not measure" must never read
as "measured, nothing wrong".

Each test is one way the measurement could go quiet:
  * a read not named, or a read named twice / never again (re-arming);
  * a listing reported as a read, a replaced file reported as read or unread;
  * the canaries passing when one of the three does not hold;
  * the tool's own imports reported as the job's — or hiding the job's;
  * `compare` passing when the access times missed a file the tracer saw;
  * a missing state, an unsettable access time or a differing filesystem
    ending in anything but exit 2.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
OPS = ROOT / "scripts" / "ops"
SCRIPT = OPS / "atime_trace.py"

sys.path.append(str(OPS))
import atime_trace as at  # noqa: E402

_ENV = {k: v for k, v in os.environ.items()
        if k not in ("PYTHONUNBUFFERED", "PYTHONPATH", "DA_PATH_TRACE_DIR",
                     "DA_TEST_CRASH_EXIT_CODE")}
FILES = ("za.txt", "zd/zb.txt", "zd/zsub/zc.txt", "ze/zf.txt")


def _git(repo: Path, *argv: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
         "-c", "commit.gpgsign=false", *argv],
        cwd=repo, check=True, capture_output=True, timeout=120, env=_ENV)


def _make_repo(repo: Path, files: tuple[str, ...] = FILES) -> Path:
    repo.mkdir()
    for name in files:
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(f"content of {name}\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


def _tool(repo: Path, *argv: str, script: Path = SCRIPT,
          flags: tuple[str, ...] = ()) -> subprocess.CompletedProcess:
    root = () if script != SCRIPT else ("--root", str(repo))
    return subprocess.run(
        [sys.executable, *flags, str(script), *root, *argv], capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=120, env=_ENV, cwd=repo)


def _cat(path: Path) -> None:
    subprocess.run(["cat", str(path)], stdout=subprocess.DEVNULL, check=True, timeout=60)


def _ls(path: Path) -> None:
    subprocess.run(["ls", str(path)], stdout=subprocess.DEVNULL, check=True, timeout=60)


def _measurable(repo: Path, state: Path) -> Path:
    """reset + exercise the canaries + `canary`; the canary directory. FAILS
    when this filesystem does not record reads."""
    proc = _tool(repo, "reset", "--state", str(state))
    assert proc.returncode == 0, proc.stderr
    canary = Path(json.loads(state.read_text(encoding="utf-8"))["canary"]["dir"])
    _cat(canary / at.CANARY_READ)
    _ls(canary / at.CANARY_DIR)
    proc = _tool(repo, "canary", "--state", str(state))
    if proc.returncode != 0:
        pytest.fail(
            "COULD NOT MEASURE: the filesystem holding pytest's tmp_path does "
            "not record reads the way atime_trace.py needs, so none of these "
            "tests can tell a working tool from a broken one. This is not a "
            f"pass and not a skip.\n{proc.stderr}")
    return canary


@pytest.fixture()
def armed(tmp_path):
    repo = _make_repo(tmp_path / "zrepo")
    state = tmp_path / "state.json"
    canary = _measurable(repo, state)
    return repo, state, canary


def _collect(repo: Path, state: Path, label: str = "zseg") -> tuple[dict, str]:
    proc = _tool(repo, "collect", "--state", str(state), "--label", label)
    assert proc.returncode == 0, proc.stderr
    return json.loads(state.read_text(encoding="utf-8"))["segments"][-1], proc.stdout


# ── collect ─────────────────────────────────────────────────────────────────

def test_a_read_is_named_and_only_that_read(armed):
    repo, state, _ = armed
    _cat(repo / "zd/zb.txt")
    segment, out = _collect(repo, state)
    assert segment["files"] == ["zd/zb.txt"]
    assert segment["dirs"] == [] and segment["replaced"] == [] and segment["gone"] == []
    assert "READ      zd/zb.txt" in out


def test_nothing_read_is_an_empty_segment(armed):
    repo, state, _ = armed
    segment, out = _collect(repo, state)
    assert segment["files"] == [] and segment["dirs"] == []
    assert "0 tracked file(s) read, 0 directory(ies) listed" in out


def test_a_file_is_reported_once_and_again_when_read_again(armed):
    """Re-arming: under relatime a second read of a file whose access time was
    left where the first read put it is NOT recorded. `collect` has to set it
    back, or every later segment misses the file."""
    repo, state, _ = armed
    _cat(repo / "za.txt")
    assert _collect(repo, state, "one")[0]["files"] == ["za.txt"]
    assert _collect(repo, state, "two")[0]["files"] == []
    _cat(repo / "za.txt")
    assert _collect(repo, state, "three")[0]["files"] == ["za.txt"]


def test_a_listing_is_listed_not_read(armed):
    repo, state, _ = armed
    os.listdir(repo / "zd")
    segment, out = _collect(repo, state)
    assert segment["files"] == []
    assert segment["dirs"] == ["zd"]
    assert "LISTED    zd" in out


def test_a_read_does_not_list_its_directory(armed):
    repo, state, _ = armed
    _cat(repo / "zd/zsub/zc.txt")
    assert _collect(repo, state)[0]["dirs"] == []


def test_a_replaced_file_is_replaced_not_read_and_not_silent(armed):
    """A rename over the path is a new inode: whether the old content was read
    first is unknown, so it is neither READ nor absent from the report."""
    repo, state, _ = armed
    (repo / "za.new").write_text("new\n", encoding="utf-8")
    os.replace(repo / "za.new", repo / "za.txt")
    segment, out = _collect(repo, state)
    assert segment["files"] == []
    assert segment["replaced"] == ["za.txt"]
    assert "REPLACED  za.txt" in out
    # …and it is armed again: a read of the new file is seen.
    _cat(repo / "za.txt")
    assert _collect(repo, state)[0]["files"] == ["za.txt"]


def test_a_deleted_file_is_gone(armed):
    repo, state, _ = armed
    (repo / "ze/zf.txt").unlink()
    segment, _ = _collect(repo, state)
    assert segment["gone"] == ["ze/zf.txt"]
    assert _collect(repo, state)[0]["gone"] == []


def test_reset_keeps_the_modification_times_and_a_clean_tree(armed):
    repo, state, _ = armed
    before = {name: os.lstat(repo / name).st_mtime_ns for name in FILES}
    proc = _tool(repo, "reset", "--state", str(state))
    assert proc.returncode == 0, proc.stderr
    assert {name: os.lstat(repo / name).st_mtime_ns for name in FILES} == before
    assert all(os.lstat(repo / name).st_atime_ns == at.PAST_NS for name in FILES)
    status = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True,
                            text=True, encoding="utf-8", timeout=120, check=True, env=_ENV)
    assert status.stdout == ""


def test_collect_writes_the_list(armed, tmp_path):
    repo, state, _ = armed
    _cat(repo / "za.txt")
    _cat(repo / "ze/zf.txt")
    out = tmp_path / "list.txt"
    proc = _tool(repo, "collect", "--state", str(state), "--label", "z", "--out", str(out))
    assert proc.returncode == 0, proc.stderr
    assert out.read_text(encoding="utf-8") == "za.txt\nze/zf.txt\n"


# ── canary ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("read, listed, touched, inner, problems", [
    (False, False, False, False, ("`must-read` was not recorded", "`listed-dir` was not recorded")),
    (True, False, False, False, ("`listed-dir` was not recorded",)),
    (False, True, False, False, ("`must-read` was not recorded",)),
    (True, True, True, False, ("`must-not-read` was recorded as read",)),
    (True, True, False, True, ("`listed-dir/inner` was recorded as read",)),
])
def test_canary_is_exit_2_naming_what_does_not_hold(tmp_path, read, listed, touched,
                                                   inner, problems):
    repo = _make_repo(tmp_path / "zrepo")
    state = tmp_path / "state.json"
    _measurable(repo, state)  # the all-three-hold case is rc 0 here, or the test fails
    assert _tool(repo, "reset", "--state", str(state)).returncode == 0
    canary = Path(json.loads(state.read_text(encoding="utf-8"))["canary"]["dir"])
    if read:
        _cat(canary / at.CANARY_READ)
    if listed:
        _ls(canary / at.CANARY_DIR)
    if touched:
        _cat(canary / at.CANARY_UNTOUCHED)
    if inner:
        _cat(canary / at.CANARY_INNER)
    proc = _tool(repo, "canary", "--state", str(state))
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "CANARY-FAILED" in proc.stderr
    assert "CANARY-OK" not in proc.stdout
    for text in problems:
        assert text in proc.stderr
    assert proc.stderr.count("\n  - ") == len(problems), proc.stderr


def test_canary_ok_says_so_and_survives_collects(armed):
    """The canaries are not re-armed by `collect`: the check at the end of a
    job still sees the read made at its start."""
    repo, state, _ = armed
    _collect(repo, state)
    _collect(repo, state)
    proc = _tool(repo, "canary", "--state", str(state))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("CANARY-OK")


def test_a_removed_canary_is_exit_2(armed):
    repo, state, canary = armed
    shutil.rmtree(canary)
    proc = _tool(repo, "canary", "--state", str(state))
    assert proc.returncode == 2
    assert "is missing" in proc.stderr


def test_canaries_on_another_filesystem_are_refused(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path / "zrepo")
    real = os.lstat

    def lstat(path, *a, **kw):
        st = real(path, *a, **kw)
        if str(path).endswith("zcanary"):
            return os.stat_result(tuple(st[:2]) + (st.st_dev + 1,) + tuple(st[3:]))
        return st

    monkeypatch.setattr(at.os, "lstat", lstat)
    with pytest.raises(at._lib_exitcodes.CallerError, match="another filesystem"):
        at.main(["--root", str(repo), "reset", "--state", str(tmp_path / "s.json"),
                 "--canary-dir", str(tmp_path / "zcanary")])


# ── no answer is exit 2 ─────────────────────────────────────────────────────

@pytest.mark.parametrize("command", [
    ("collect", "--label", "z"), ("canary",), ("cost",),
    ("compare", "--tracer-json", "znone.json")])
def test_no_state_file_is_exit_2(tmp_path, command):
    repo = _make_repo(tmp_path / "zrepo")
    proc = _tool(repo, command[0], "--state", str(tmp_path / "znone.json"), *command[1:])
    assert proc.returncode == 2, proc.stdout
    assert "run `reset` first" in proc.stderr


def test_a_foreign_state_file_is_exit_2(tmp_path):
    repo = _make_repo(tmp_path / "zrepo")
    state = tmp_path / "state.json"
    state.write_text('{"version": 0}', encoding="utf-8")
    proc = _tool(repo, "canary", "--state", str(state))
    assert proc.returncode == 2
    assert "is not a version-1 state file" in proc.stderr


def test_an_access_time_that_cannot_be_set_is_exit_2_not_a_smaller_list(tmp_path, monkeypatch):
    """A path that cannot be reset keeps whatever access time it had: a read of
    it is invisible. That is no measurement, not a measurement of the rest."""
    repo = _make_repo(tmp_path / "zrepo")
    real = at._set_past

    def refuse(path):
        if path.endswith("zb.txt"):
            raise PermissionError(1, "Operation not permitted")
        real(path)

    monkeypatch.setattr(at, "_set_past", refuse)
    state = tmp_path / "state.json"
    with pytest.raises(at._lib_exitcodes.CallerError, match=r"zd/zb\.txt: Operation not permitted"):
        at.main(["--root", str(repo), "reset", "--state", str(state)])
    assert not state.exists()


def test_reset_outside_a_repository_is_exit_2(tmp_path):
    (tmp_path / "zplain").mkdir()
    proc = _tool(tmp_path / "zplain", "reset", "--state", str(tmp_path / "s.json"))
    assert proc.returncode == 2, proc.stdout
    assert "git" in proc.stderr


def test_cost_without_a_segment_is_exit_2(armed):
    repo, state, _ = armed
    proc = _tool(repo, "cost", "--state", str(state))
    assert proc.returncode == 2
    _collect(repo, state, "zone")
    proc = _tool(repo, "cost", "--state", str(state))
    assert proc.returncode == 0, proc.stderr
    assert "COST: segment [zone]: 0 read" in proc.stdout
    assert f"COST: {len(FILES)} tracked file(s), 4 directory(ies)" in proc.stdout


# ── the tool's own reads ────────────────────────────────────────────────────

def _repo_with_the_tool(tmp_path: Path) -> tuple[Path, Path]:
    """A repository that TRACKS a copy of the tool and of what it imports, the
    way the real one does."""
    repo = tmp_path / "zrepo"
    (repo / "scripts/ops").mkdir(parents=True)
    (repo / "scripts/tools").mkdir(parents=True)
    shutil.copy(SCRIPT, repo / "scripts/ops/atime_trace.py")
    for name in ("_lib_exitcodes.py", "_lib_compat.py"):
        shutil.copy(ROOT / "scripts/tools" / name, repo / "scripts/tools" / name)
    (repo / "zdata.txt").write_text("z\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    return repo, repo / "scripts/ops/atime_trace.py"


def test_the_tools_own_imports_are_neither_reported_nor_hidden(tmp_path):
    """The tool imports scripts/tools/_lib_exitcodes.py from the tree it
    measures. Two ways that goes wrong: every run reports the file as read by
    the job; or the tool leaves a .pyc, after which the job's own import loads
    the cache and the .py is never opened."""
    repo, script = _repo_with_the_tool(tmp_path)
    state = tmp_path / "state.json"
    isolated = ("-I",)
    assert _tool(repo, "reset", "--state", str(state), script=script,
                 flags=isolated).returncode == 0
    for label in ("zone", "ztwo"):
        proc = _tool(repo, "collect", "--state", str(state), "--label", label,
                     script=script, flags=isolated)
        assert proc.returncode == 0, proc.stderr
        assert "0 tracked file(s) read, 0 directory(ies) listed" in proc.stdout, proc.stdout
        assert "(not measured: scripts/ops/atime_trace.py" in proc.stdout
    assert not list(repo.rglob("*.pyc")), "the tool left bytecode in the measured tree"

    # The job imports the same module: seen, and seen as the job's.
    subprocess.run(
        [sys.executable, "-B", "-c", "import sys; sys.path.insert(0, 'scripts/tools'); "
                                     "import _lib_exitcodes"],
        cwd=repo, check=True, capture_output=True, timeout=120, env=_ENV)
    proc = _tool(repo, "collect", "--state", str(state), "--label", "zjob",
                 script=script, flags=isolated)
    segment = json.loads(state.read_text(encoding="utf-8"))["segments"][-1]
    assert segment["files"] == ["scripts/tools/_lib_compat.py", "scripts/tools/_lib_exitcodes.py"]
    assert "scripts/tools" in segment["dirs"]


def test_without_isolated_mode_the_script_directory_is_named_as_not_measured(tmp_path):
    repo, script = _repo_with_the_tool(tmp_path)
    state = tmp_path / "state.json"
    assert _tool(repo, "reset", "--state", str(state), script=script).returncode == 0
    proc = _tool(repo, "collect", "--state", str(state), "--label", "z", script=script)
    assert proc.returncode == 0, proc.stderr
    assert "0 tracked file(s) read, 0 directory(ies) listed" in proc.stdout, proc.stdout
    assert "(not measured: the directory scripts/ops" in proc.stdout


# ── compare ─────────────────────────────────────────────────────────────────

def _trace(path: Path, files: list[str], sources: list[str]) -> Path:
    path.write_text(json.dumps({"workflow": "z.yaml", "jobs": {"zjob": {
        "files": files, "import_dirs": [],
        "go": [{"module_dir": "zd", "package": "./zsub", "sources": sources}]}}}),
        encoding="utf-8")
    return path


def test_compare_is_exit_1_naming_what_the_access_times_missed(armed, tmp_path):
    repo, state, _ = armed
    _cat(repo / "za.txt")
    _cat(repo / "ze/zf.txt")
    _collect(repo, state, "one")
    _cat(repo / "zd/zsub/zc.txt")
    _collect(repo, state, "two")
    trace = _trace(tmp_path / "t.json", ["za.txt", "zd/zb.txt"], ["zd/zsub/*.txt", "zd/*.txt"])
    proc = _tool(repo, "compare", "--state", str(state), "--tracer-json", str(trace))
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "COMPARE: TRACER-ONLY (1)" in proc.stderr
    assert "  TRACER-ONLY  zd/zb.txt" in proc.stderr
    assert "TRACER-ONLY  za.txt" not in proc.stderr
    assert "3 tracked file(s) read over 2 segment(s)" in proc.stdout
    assert "tracer (Python): 2 first-party file(s); both saw 1" in proc.stdout
    # `zd/*.txt` stays inside one segment: zd/zb.txt, not zd/zsub/zc.txt twice.
    assert "tracer (go list): 2 tracked file(s) under 2 source pattern(s); both saw 1" in proc.stdout
    assert "  GO-LIST-ONLY  zd/zb.txt" in proc.stdout
    assert "  ATIME-ONLY  ze/zf.txt" in proc.stdout


def test_compare_is_exit_0_when_every_traced_file_was_seen(armed, tmp_path):
    repo, state, _ = armed
    _cat(repo / "za.txt")
    _collect(repo, state)
    trace = _trace(tmp_path / "t.json", ["za.txt"], [])
    proc = _tool(repo, "compare", "--state", str(state), "--tracer-json", str(trace))
    assert proc.returncode == 0, proc.stderr
    assert "COMPARE: TRACER-ONLY (0)" in proc.stdout


@pytest.mark.parametrize("content, message", [
    ("not json", "is not a trace written by"),
    ('{"jobs": {"z": {"files": [], "go": []}}}', "names no file"),
])
def test_compare_with_nothing_to_compare_against_is_exit_2(armed, tmp_path, content, message):
    repo, state, _ = armed
    _collect(repo, state)
    trace = tmp_path / "t.json"
    trace.write_text(content, encoding="utf-8")
    proc = _tool(repo, "compare", "--state", str(state), "--tracer-json", str(trace))
    assert proc.returncode == 2, proc.stdout
    assert message in proc.stderr


def test_compare_before_any_collect_is_exit_2(armed, tmp_path):
    repo, state, _ = armed
    trace = _trace(tmp_path / "t.json", ["za.txt"], [])
    proc = _tool(repo, "compare", "--state", str(state), "--tracer-json", str(trace))
    assert proc.returncode == 2
    assert "no segment was collected" in proc.stderr


# ── semantics ───────────────────────────────────────────────────────────────

def test_every_declared_cell_is_measured_and_only_those(tmp_path):
    got = at.measure_semantics(str(tmp_path))
    assert sorted(got) == sorted(cell for cell, *_ in at._CELLS)
    assert len({cell for cell, *_ in at._CELLS}) == len(at._CELLS)


def _reference() -> dict[str, bool]:
    return {cell: expected for cell, _text, expected, _rests in at._CELLS}


def test_semantics_matching_the_reference_is_exit_0(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(at, "measure_semantics", lambda work: _reference())
    assert at.main(["semantics", "--dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "SEMANTICS-OK" in out and "DIFFERS" not in out
    assert not list(tmp_path.iterdir()), "the scratch directory was left behind"


def test_a_differing_basis_cell_is_exit_2_by_name(tmp_path, monkeypatch, capsys):
    """noatime: nothing moves. The cells the method rests on differ."""
    monkeypatch.setattr(at, "measure_semantics",
                        lambda work: {cell: False for cell in _reference()})
    with pytest.raises(at._lib_exitcodes.CallerError) as caught:
        at.main(["semantics", "--dir", str(tmp_path)])
    message = str(caught.value)
    assert "SEMANTICS-BASIS-DIFFERS" in message
    for cell in ("reset-read", "cat", "listdir:dir", "ls:dir"):
        assert cell in message
    assert "o-noatime" not in message  # False is the reference there
    assert "SEMANTICS-OK" not in capsys.readouterr().out


def test_a_differing_context_cell_is_named_and_exit_0(tmp_path, monkeypatch, capsys):
    """strictatime: a second read without a reset IS recorded. The method
    always resets, so it does not rest on that cell."""
    got = _reference()
    got["reset-second-read"] = True
    monkeypatch.setattr(at, "measure_semantics", lambda work: got)
    assert at.main(["semantics", "--dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "SEMANTICS-CONTEXT-DIFFERS: reset-second-read" in out
    assert "1 context cell(s) differ" in out


def test_the_cells_the_tests_rely_on_hold_here(armed):
    """The real run, on the filesystem these tests use: the basis cells match
    the reference. `armed` has already failed loudly if reads are not recorded
    at all; this names WHICH cell, if the filesystem is merely different."""
    repo, _state, _ = armed
    proc = _tool(repo, "semantics")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "SEMANTICS-OK" in proc.stdout
