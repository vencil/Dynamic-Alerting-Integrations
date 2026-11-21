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
  * `finish` marking a measurement whose canaries do not hold, or leaving the
    repository's `core.trustctime` other than it found it;
  * git reading the whole tree after a reset, and that being read as the job's;
  * the tool's own imports reported as the job's — or hiding the job's;
  * a missing state, an unsettable access time or a differing filesystem
    ending in anything but exit 2.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from _platform_fs import symlink_or_skip  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
OPS = ROOT / "scripts" / "ops"
SCRIPT = OPS / "atime_trace.py"

sys.path.append(str(OPS))
import atime_trace as at  # noqa: E402

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


pytestmark = pytest.mark.skipif(
    os.name == "nt",
    reason="the access-time method is defined for the Linux CI runner; on "
           "Windows there is no O_NOATIME and no relatime to measure against")


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


def test_a_state_without_the_trustctime_record_cannot_be_finished(armed):
    repo, state, _ = armed
    doc = json.loads(state.read_text(encoding="utf-8"))
    del doc["trustctime"]
    state.write_text(json.dumps(doc), encoding="utf-8")
    proc = _tool(repo, "finish", "--state", str(state))
    assert proc.returncode == 2
    assert "did not set core.trustctime" in proc.stderr


def test_reset_keeps_the_modification_times_and_a_clean_tree(armed):
    repo, state, _ = armed
    before = {name: os.lstat(repo / name).st_mtime_ns for name in FILES}
    state.unlink()  # abandon the fixture's measurement; reset refuses to run over it
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
    _reset(repo, state)
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

    (tmp_path / "zcanary").mkdir()
    (tmp_path / "zcanary" / "zkeep").write_text("theirs\n", encoding="utf-8")
    monkeypatch.setattr(at.os, "lstat", lstat)
    with pytest.raises(at._lib_exitcodes.CallerError, match="another filesystem"):
        at.main(["--root", str(repo), "reset", "--state", str(tmp_path / "s.json"),
                 "--canary-dir", str(tmp_path / "zcanary")])


# ── finish ──────────────────────────────────────────────────────────────────

def _state(state: Path) -> dict:
    return json.loads(state.read_text(encoding="utf-8"))


def _local_trustctime(repo: Path) -> str | None:
    """The repository's LOCAL value, None when unset (`git config` exits 1)."""
    proc = subprocess.run(["git", "config", "--local", "--get", "core.trustctime"],
                          cwd=repo, capture_output=True, text=True, encoding="utf-8",
                          timeout=120, env=_ENV)
    assert proc.returncode in (0, 1), proc.stderr
    return proc.stdout.strip() if proc.returncode == 0 else None


def _reset(repo: Path, state: Path) -> None:
    """A fresh measurement. An unfinished one in *state* is abandoned the
    documented way — the file is deleted — because `reset` refuses to run
    over it."""
    state.unlink(missing_ok=True)
    proc = _tool(repo, "reset", "--state", str(state))
    assert proc.returncode == 0, proc.stderr


def test_finish_exercises_the_canaries_collects_and_marks_the_state(tmp_path):
    """The job has no canary step of its own: `finish` reads and lists them,
    then judges them. `_measurable` is not used here — nothing but the tool
    touches the canaries, so a `finish` that only CHECKED would exit 2."""
    repo = _make_repo(tmp_path / "zrepo")
    state = tmp_path / "state.json"
    _reset(repo, state)
    assert _state(state)["finished"] is None
    _cat(repo / "zd/zb.txt")
    proc = _tool(repo, "finish", "--state", str(state))
    if proc.returncode != 0 and "CANARY-FAILED" in proc.stderr:
        pytest.fail("COULD NOT MEASURE: this filesystem does not record reads; "
                    f"not a pass and not a skip.\n{proc.stderr}")
    assert proc.returncode == 0, proc.stderr
    assert "CANARY-OK" in proc.stdout
    doc = _state(state)
    assert doc["finished"] == {"canary": "ok", "segments": 1}
    assert [(s["label"], s["files"]) for s in doc["segments"]] == [("end", ["zd/zb.txt"])]


@pytest.mark.parametrize("spoil, problem", [
    ("touched", "`must-not-read` was recorded as read"),
    ("inner", "`listed-dir/inner` was recorded as read"),
    ("removed", "is missing"),
])
def test_finish_is_exit_2_and_leaves_the_state_unmarked_when_a_canary_fails(
        tmp_path, spoil, problem):
    repo = _make_repo(tmp_path / "zrepo")
    state = tmp_path / "state.json"
    _measurable(repo, state)
    _reset(repo, state)
    canary = Path(_state(state)["canary"]["dir"])
    if spoil == "touched":
        _cat(canary / at.CANARY_UNTOUCHED)
    elif spoil == "inner":
        _cat(canary / at.CANARY_INNER)
    else:
        shutil.rmtree(canary)
    _cat(repo / "za.txt")
    proc = _tool(repo, "finish", "--state", str(state))
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert problem in proc.stderr
    doc = _state(state)
    # The segment is there and the mark is not: a reader of the state file
    # cannot take the one for a measurement without the other.
    assert doc["finished"] is None
    assert doc["segments"][-1]["files"] == ["za.txt"]
    # …and the repository still got its setting back.
    assert _local_trustctime(repo) is None


@pytest.mark.parametrize("before", [None, "true", "false"])
def test_reset_sets_trustctime_false_and_finish_puts_back_what_was_there(tmp_path, before):
    repo = _make_repo(tmp_path / "zrepo")
    if before is not None:
        _git(repo, "config", "--local", "core.trustctime", before)
    state = tmp_path / "state.json"
    _reset(repo, state)
    assert _local_trustctime(repo) == "false"
    assert _state(state)["trustctime"] == {
        "previous": before, "restored": False, "root": str(repo.resolve())}
    # The state file is LOST before any finish (an interrupted local run).
    # The next reset finds its own `false` in the config; what is put back is
    # still what the first one found, because that is written in the config
    # too. (Review: the tool used to "restore" the `false` it had written.)
    state.unlink()
    _reset(repo, state)
    assert _state(state)["trustctime"]["previous"] == before
    proc = _tool(repo, "finish", "--state", str(state))
    assert proc.returncode == 0, proc.stderr
    assert _local_trustctime(repo) == before
    assert _state(state)["trustctime"]["restored"] is True
    marker = subprocess.run(["git", "config", "--local", "--get", at.TRUSTCTIME_WAS],
                            cwd=repo, capture_output=True, text=True, encoding="utf-8",
                            timeout=60, env=_ENV)
    assert (marker.returncode, marker.stdout) == (1, ""), "the marker outlived finish"
    # A second finish does not "restore" again over whatever is there now.
    _git(repo, "config", "--local", "core.trustctime", "true")
    assert _tool(repo, "finish", "--state", str(state)).returncode == 0
    assert _local_trustctime(repo) == "true"


def test_a_reset_over_an_unfinished_measurement_is_exit_2_and_discards_nothing(armed):
    """A second `reset` in the middle of a job — spelled any way, so the
    wiring check cannot see it — would drop every read collected so far and
    the job would be asserted on the rest."""
    repo, state, _canary = armed
    _cat(repo / "za.txt")
    _collect(repo, state, "zfirst")
    before = state.read_bytes()
    proc = _tool(repo, "reset", "--state", str(state))
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "was not finished" in proc.stderr and "Traceback" not in proc.stderr
    assert state.read_bytes() == before
    # Finished, the same state file may be reset over.
    assert _tool(repo, "finish", "--state", str(state)).returncode == 0
    assert _tool(repo, "reset", "--state", str(state)).returncode == 0


def test_collect_after_finish_is_exit_2_and_adds_no_segment(armed):
    """`finish` put the git setting back: a segment collected after it would
    not measure the job, and the state would still read as finished."""
    repo, state, _canary = armed
    assert _tool(repo, "finish", "--state", str(state)).returncode == 0
    before = state.read_bytes()
    proc = _tool(repo, "collect", "--state", str(state), "--label", "zlate")
    assert proc.returncode == 2 and "is marked finished" in proc.stderr
    assert state.read_bytes() == before


def test_a_finish_that_found_the_setting_changed_cannot_be_retried_into_a_pass(armed):
    """The first `finish` restores the setting and exits 2. A second one used
    to skip the check (already restored) and mark the same segments finished."""
    repo, state, _canary = armed
    _git(repo, "config", "--local", "core.trustctime", "true")
    assert _tool(repo, "finish", "--state", str(state)).returncode == 2
    proc = _tool(repo, "finish", "--state", str(state))
    assert proc.returncode == 2 and "was changed during the" in proc.stderr
    assert _state(state)["finished"] is None


def test_a_path_that_could_not_be_rearmed_keeps_the_state_from_finishing(
        armed, monkeypatch, capsys):
    """Its stamp is now the access time AFTER the read, so a later read may
    not move it. The failed `collect` is exit 2 on its own; what this holds
    is that a `finish` after it cannot mark the state finished."""
    repo, state, _canary = armed
    _cat(repo / "za.txt")
    real = at._set_past

    def refuse(path):
        if path.endswith("za.txt"):
            raise PermissionError(1, "Operation not permitted", path)
        real(path)

    monkeypatch.setattr(at, "_set_past", refuse)
    with pytest.raises(at._lib_exitcodes.CallerError, match="could not be re-armed"):
        at.main(["--root", str(repo), "collect", "--state", str(state), "--label", "z"])
    monkeypatch.setattr(at, "_set_past", real)
    assert _state(state)["rearm_failed"]
    proc = _tool(repo, "finish", "--state", str(state))
    assert proc.returncode == 2 and "could not be re-armed" in proc.stderr
    assert _state(state)["finished"] is None


def test_a_canary_directory_holding_someone_elses_files_is_refused_untouched(tmp_path):
    """`--canary-dir` may name any directory; `reset` used to empty it first
    and check afterwards."""
    repo = _make_repo(tmp_path / "zrepo")
    theirs = repo / ".git" / "zkeep"
    theirs.mkdir()
    (theirs / "zmine.txt").write_text("not a canary\n", encoding="utf-8")
    proc = _tool(repo, "reset", "--state", str(tmp_path / "s.json"),
                 "--canary-dir", str(theirs))
    assert proc.returncode == 2 and "did not" in proc.stderr
    assert (theirs / "zmine.txt").read_text(encoding="utf-8") == "not a canary\n"
    assert _local_trustctime(repo) is None  # and the failed reset left no setting behind
    # Its own directory from an earlier run is re-created, not refused.
    state = tmp_path / "s2.json"
    _reset(repo, state)
    _reset(repo, state)


@pytest.mark.parametrize("shape", ["top-level file", "file in listed-dir",
                                   "inner replaced by a directory",
                                   "listed-dir linked elsewhere",
                                   "a canary name with other content"])
def test_a_marked_canary_directory_keeps_what_somebody_added_to_it(tmp_path, shape):
    """The mark says who made the directory, not who owns everything in it.
    Nothing is deleted recursively: the tool unlinks its own files by name and
    `rmdir`s, so whatever else is there — under any name, a directory where a
    canary file was included — survives and the reset is exit 2. (Review, three
    rounds: each "check the names, then rmtree" let one more shape through.)"""
    repo = _make_repo(tmp_path / "zrepo")
    state = tmp_path / "s.json"
    _reset(repo, state)
    canary = Path(_state(state)["canary"]["dir"])
    if shape == "top-level file":
        theirs = canary / "znotes.txt"
    elif shape == "file in listed-dir":
        theirs = canary / at.CANARY_DIR / "zmore.txt"
    elif shape == "inner replaced by a directory":
        (canary / at.CANARY_INNER).unlink()
        (canary / at.CANARY_INNER).mkdir()
        theirs = canary / at.CANARY_INNER / "zdeep.txt"
    elif shape == "listed-dir linked elsewhere":
        # The cleanup would follow the link and unlink `inner` out there.
        outside = tmp_path / "zoutside"
        outside.mkdir()
        (canary / at.CANARY_INNER).unlink()
        (canary / at.CANARY_DIR).rmdir()
        symlink_or_skip(outside, canary / at.CANARY_DIR, target_is_directory=True)
        theirs = outside / "inner"
    else:
        # A name proves nothing about who wrote the file.
        theirs = canary / at.CANARY_READ
    theirs.write_text("theirs\n", encoding="utf-8")
    kept = sorted(p.name for p in canary.iterdir())
    state.unlink()
    proc = _tool(repo, "reset", "--state", str(state))
    assert proc.returncode == 2 and "did not put there" in proc.stderr, proc.stderr
    assert "Traceback" not in proc.stderr
    assert theirs.read_text(encoding="utf-8") == "theirs\n"
    # Refused whole: the tool's own canaries are still there too.
    assert sorted(p.name for p in canary.iterdir()) == kept
    assert _local_trustctime(repo) is None  # the failed reset left no setting behind


def test_a_canary_directory_named_through_a_symlink_is_refused(tmp_path):
    """Checked on the path as given: resolved, it is the link's target, and
    an empty target would simply be filled with canaries."""
    repo = _make_repo(tmp_path / "zrepo")
    target = repo / ".git" / "ztarget"
    target.mkdir()
    link = repo / ".git" / "zlink"
    symlink_or_skip(target, link, target_is_directory=True)
    proc = _tool(repo, "reset", "--state", str(tmp_path / "s.json"), "--canary-dir", str(link))
    assert proc.returncode == 2 and "symbolic link" in proc.stderr, proc.stderr
    assert list(target.iterdir()) == [] and link.is_symlink()
    assert _local_trustctime(repo) is None


def test_git_is_not_led_to_another_repository_by_inherited_variables(tmp_path, monkeypatch):
    """A git hook exports GIT_DIR; the setting must land in --root's config."""
    repo = _make_repo(tmp_path / "zrepo")
    other = _make_repo(tmp_path / "zother")
    env = dict(_ENV, GIT_DIR=str(other / ".git"), GIT_WORK_TREE=str(other))
    proc = subprocess.run(
        [sys.executable, "-I", str(SCRIPT), "--root", str(repo), "reset",
         "--state", str(tmp_path / "s.json")], cwd=repo, env=env, capture_output=True,
        text=True, encoding="utf-8", stdin=subprocess.DEVNULL, timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert _local_trustctime(repo) == "false" and _local_trustctime(other) is None


def test_finish_twice_leaves_no_stale_mark_when_the_second_one_fails(armed):
    """`finish` clears the mark before it measures: a state that was finished
    once and then fails its canaries must not still read as finished."""
    repo, state, _canary = armed
    assert _tool(repo, "finish", "--state", str(state)).returncode == 0
    assert _state(state)["finished"]["canary"] == "ok"
    _cat(Path(_state(state)["canary"]["dir"]) / at.CANARY_UNTOUCHED)
    assert _tool(repo, "finish", "--state", str(state)).returncode == 2
    assert _state(state)["finished"] is None


def test_reset_right_after_the_tree_is_written_leaves_git_nothing_to_reread(tmp_path):
    """git re-reads a file written in the same second as the index whatever
    `core.trustctime` says. `reset` waits that second out and THEN refreshes
    the index, which is what clears those entries: without the wait (or the
    refresh) the job's first `git status` reads them all. Measured on a real
    checkout followed at once by `reset`: 455 to 1350 files."""
    count = 150
    names = tuple(f"zr/z{i:03}.txt" for i in range(count))
    repo = _make_repo(tmp_path / "zrepo", names)
    state = tmp_path / "state.json"
    _measurable(repo, state)
    assert _tool(repo, "finish", "--state", str(state)).returncode == 0
    # Rewrite the tree and the index inside one second, then reset at once.
    for name in names:
        (repo / name).write_text(f"rewritten {name}\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _reset(repo, state)
    time.sleep(1.2)
    subprocess.run(["git", "-c", "core.checkstat=default", "status"],
                   cwd=repo, check=True, capture_output=True, timeout=120, env=_ENV)
    assert _collect(repo, state, "zgit")[0]["files"] == []


@pytest.mark.parametrize("meddle", [("true",), ("--unset",)])
def test_finish_is_exit_2_when_trustctime_was_changed_during_the_measurement(
        tmp_path, meddle):
    """With any value but `false` the job's git commands compared change
    times: the segments may name the whole tree. Not a measurement — and the
    value `reset` found is still put back."""
    repo = _make_repo(tmp_path / "zrepo")
    _git(repo, "config", "--local", "core.trustctime", "yes")
    state = tmp_path / "state.json"
    _reset(repo, state)
    if meddle == ("--unset",):
        _git(repo, "config", "--local", "--unset", "core.trustctime")
    else:
        _git(repo, "config", "--local", "core.trustctime", *meddle)
    proc = _tool(repo, "finish", "--state", str(state))
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "core.trustctime was" in proc.stderr and "not the `false`" in proc.stderr
    assert _state(state)["finished"] is None
    assert _local_trustctime(repo) == "yes"


def test_git_rereads_the_whole_tree_after_a_reset_unless_trustctime_is_false(tmp_path):
    """Why `reset` touches the git config at all. Setting an access time moves
    the change time; with git's default the next `git status` reads the
    content of every tracked file, and the measurement names the whole tree.

    ⚠️ Two things this reproduction depends on, both measured (#3756):
      * git compares change times to the SECOND, and re-reads a file that is
        not older than the index whatever the setting — hence the sleeps;
      * a developer machine may set `core.trustctime=false` or
        `core.checkstat=minimal` globally, where the default never shows —
        hence `-c` on the git command line, which wins over every file.
    """
    count = 120
    names = tuple(f"zt/z{i:03}.txt" for i in range(count))
    repo = _make_repo(tmp_path / "zrepo", names)
    state = tmp_path / "state.json"
    _measurable(repo, state)

    def reads_after_status(*config: str) -> int:
        time.sleep(1.2)
        _reset(repo, state)
        time.sleep(1.2)
        subprocess.run(["git", "-c", "core.checkstat=default", *config, "status"],
                       cwd=repo, check=True, capture_output=True, timeout=120, env=_ENV)
        return len(_collect(repo, state, "zgit")[0]["files"])

    default = reads_after_status("-c", "core.trustctime=true")
    assert _local_trustctime(repo) == "false"  # the second run reads this one
    held = reads_after_status()
    assert default == count, (default, held)
    assert held == 0, (default, held)


# ── no answer is exit 2 ─────────────────────────────────────────────────────

@pytest.mark.parametrize("command", [
    ("collect", "--label", "z"), ("canary",), ("cost",), ("finish",)])
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
    # No state file names the value to put back, so `reset` put it back itself.
    assert _local_trustctime(repo) is None


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
