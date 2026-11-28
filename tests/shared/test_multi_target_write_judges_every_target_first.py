"""#4250: a tool that rewrites several tracked files must judge every one of
them before it writes the first. `check_repo_artifact` only refuses what is
no file (a FIFO, a directory, a standard stream); the write also fails on a
file this process may not write, or a new file in a directory it may not
create one in — and by then the earlier targets were rewritten.

`check_writable_artifact` adds those two, with the same `os.access(W_OK)`
the atomic write refuses on; `write_repo_artifacts` judges a whole batch
with it before the first write, and names what it already wrote when a
write still fails (a full disk: only the write sees it).

The unprivileged cases run as uid 65534 in a child (root passes every
`os.access`); the rest fake `os.access` so they run as anyone.
"""
from __future__ import annotations

import importlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
TOOLS = REPO / "scripts" / "tools"
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(REPO / "scripts" / "dx"))

import _lib_output  # noqa: E402
from _lib_output import (  # noqa: E402
    OutputBatchStopped, OutputWriteError, check_writable_artifact, write_repo_artifacts,
)


def _deny_write(monkeypatch, *denied: Path) -> None:
    """`os.access(p, W_OK)` false for *denied* (as a non-root run would see a
    0444 file or a root-owned 0755 directory)."""
    real = os.access
    names = {os.path.realpath(p) for p in denied}

    def access(path, mode, *a, **k):
        if mode & os.W_OK and os.path.realpath(path) in names:
            return False
        return real(path, mode, *a, **k)
    monkeypatch.setattr(_lib_output.os, "access", access)


# --- check_writable_artifact ------------------------------------------------

def test_a_writable_file_and_a_new_file_in_a_writable_directory_pass(tmp_path):
    (tmp_path / "there.txt").write_text("x", encoding="utf-8")
    assert check_writable_artifact(tmp_path / "there.txt") == str(tmp_path / "there.txt")
    assert check_writable_artifact(tmp_path / "new.txt") == str(tmp_path / "new.txt")


def test_a_read_only_file_is_refused_by_name(tmp_path, monkeypatch):
    target = tmp_path / "ro.txt"
    target.write_text("x", encoding="utf-8")
    _deny_write(monkeypatch, target)
    with pytest.raises(OutputWriteError) as info:
        check_writable_artifact(target, flag="--out")
    assert info.value.path == str(target)
    assert "not writable" in str(info.value) and "--out" in str(info.value)


def test_a_new_file_in_a_directory_that_takes_none_is_refused(tmp_path, monkeypatch):
    """Critic on #4250: `os.access(W_OK)` cannot be asked of a file not there
    yet — its directory is what the write will need."""
    (tmp_path / "ro").mkdir()
    _deny_write(monkeypatch, tmp_path / "ro")
    with pytest.raises(OutputWriteError) as info:
        check_writable_artifact(tmp_path / "ro" / "new.txt")
    assert "takes no new file" in str(info.value)


def test_a_directory_still_to_be_made_is_judged_by_the_one_it_is_made_in(tmp_path, monkeypatch):
    """A tool that `ensure_dir`s before writing (`make_dirs`): the missing
    directories are not refused for being missing — the nearest existing one
    decides. Without `make_dirs` nothing makes them, so the write would fail
    (blind review): refused."""
    target = tmp_path / "a" / "b" / "new.txt"
    assert check_writable_artifact(target, make_dirs=True)
    with pytest.raises(OutputWriteError) as info:
        check_writable_artifact(target)
    assert "does not exist" in str(info.value)
    _deny_write(monkeypatch, tmp_path)
    with pytest.raises(OutputWriteError):
        check_writable_artifact(target, make_dirs=True)


def test_a_dangling_link_into_a_missing_directory_is_refused_even_with_make_dirs(tmp_path):
    """Blind review: `ensure_dir` makes the link's own directory, not the
    one it points into; the write would end in ENOENT."""
    from _platform_fs import symlink_or_skip
    link = tmp_path / "link.txt"
    symlink_or_skip(tmp_path / "nowhere" / "real.txt", link)
    with pytest.raises(OutputWriteError) as info:
        check_writable_artifact(link, make_dirs=True)
    assert info.value.path == str(link)


def test_a_writable_file_in_a_directory_that_takes_no_new_file_passes(tmp_path, monkeypatch):
    """Such a file is written in place on purpose (`_lib_atomic_write`): the
    directory only matters for a file that is not there."""
    target = tmp_path / "there.txt"
    target.write_text("x", encoding="utf-8")
    _deny_write(monkeypatch, tmp_path)
    assert check_writable_artifact(target)


# --- write_repo_artifacts ----------------------------------------------------

def test_a_read_only_later_target_leaves_the_earlier_ones_untouched(tmp_path, monkeypatch):
    first, second = tmp_path / "first.txt", tmp_path / "second.txt"
    first.write_text("OLD\n", encoding="utf-8")
    second.write_text("OLD\n", encoding="utf-8")
    _deny_write(monkeypatch, second)
    with pytest.raises(OutputWriteError) as info:
        write_repo_artifacts([(first, "NEW\n"), (second, "NEW\n")])
    assert not isinstance(info.value, OutputBatchStopped)
    assert info.value.path == str(second)
    assert first.read_text(encoding="utf-8") == "OLD\n"


def test_a_write_that_fails_after_the_check_names_what_it_already_wrote(tmp_path, monkeypatch):
    """What only the write meets (here a faked full disk on the second file):
    the first stays written — not rolled back — and the line says so."""
    first, second = tmp_path / "first.txt", tmp_path / "second.txt"
    real = _lib_output.write_repo_artifact

    def full_on_second(path, content, **k):
        if os.fspath(path) == str(second):
            raise OutputWriteError(path, OSError(28, "No space left on device"))
        return real(path, content, **k)
    monkeypatch.setattr(_lib_output, "write_repo_artifact", full_on_second)
    with pytest.raises(OutputBatchStopped) as info:
        write_repo_artifacts([(first, "NEW\n"), (second, "NEW\n")])
    assert info.value.path == str(second) and info.value.errno == 28
    assert info.value.written == [str(first)]
    assert f"already written, not rolled back: {first}" in str(info.value)
    assert first.read_text(encoding="utf-8") == "NEW\n"


def test_a_full_disk_keeps_its_own_line_when_the_batch_stops(tmp_path, monkeypatch):
    """Blind review: the atomic write's OutputNoSpaceError says "free space
    and re-run"; the batch error is that line plus what was written — not a
    rebuilt "this is a bug" line."""
    import _lib_atomic_write
    first, second = tmp_path / "first.txt", tmp_path / "second.txt"
    real = _lib_output.write_repo_artifact

    def full_on_second(path, content, **k):
        if os.fspath(path) == str(second):
            raise _lib_atomic_write.OutputNoSpaceError(
                path, OSError(28, "No space left on device"), flag=None, previous_kept=True)
        return real(path, content, **k)
    monkeypatch.setattr(_lib_output, "write_repo_artifact", full_on_second)
    with pytest.raises(OutputBatchStopped) as info:
        write_repo_artifacts([(first, "NEW\n"), (second, "NEW\n")])
    line = str(info.value)
    assert "free space and re-run" in line and "this is a bug" not in line, line
    assert line.endswith(f"already written, not rolled back: {first}"), line
    assert info.value.previous_kept is True
    assert isinstance(info.value.failed, _lib_atomic_write.OutputNoSpaceError)


def test_the_batch_returns_what_it_wrote(tmp_path):
    a, b = tmp_path / "a.txt", tmp_path / "sub" / "b.sh"
    assert write_repo_artifacts([(a, "A\n"), (b, "B\n", 0o755)], make_dirs=True) == [str(a), str(b)]
    assert a.read_text(encoding="utf-8") == "A\n" and b.read_text(encoding="utf-8") == "B\n"


def test_the_batch_keeps_each_items_mode(tmp_path):
    """POSIX mode bits only (CodeRabbit on #4326): Windows `chmod` sets just
    the read-only flag."""
    from _platform_fs import require_posix_modes
    require_posix_modes()
    b = tmp_path / "b.sh"
    write_repo_artifacts([(b, "B\n", 0o755)])
    assert b.stat().st_mode & 0o777 == 0o755


def test_an_empty_path_in_the_batch_stops_it_before_the_first_write(tmp_path, monkeypatch):
    """CodeRabbit on #4326: `realpath("")` is the working directory, so an
    empty name passed as a new file there; the write then failed on it with
    the earlier target already rewritten."""
    monkeypatch.chdir(tmp_path)
    first = tmp_path / "first.txt"
    first.write_text("OLD\n", encoding="utf-8")
    with pytest.raises(OutputWriteError) as info:
        write_repo_artifacts([(first, "NEW\n"), ("", "NEW\n")])
    assert not isinstance(info.value, OutputBatchStopped)
    assert first.read_text(encoding="utf-8") == "OLD\n"


# --- the generators moved to the batch ---------------------------------------

def test_adr_index_renders_every_doc_before_it_writes_one(tmp_path, monkeypatch):
    """`generate_adr_index` used to check, read and write each doc in turn: a
    later doc without its sentinels left the earlier ones rewritten."""
    mod = importlib.import_module("generate_adr_index")
    good, bad = tmp_path / "good.md", tmp_path / "bad.md"
    good.write_text(f"x\n{mod.SENTINEL_START}\nold\n{mod.SENTINEL_END}\n", encoding="utf-8")
    bad.write_text("no sentinels here\n", encoding="utf-8")
    monkeypatch.setattr(mod, "DEFAULT_TARGETS", ((good, "adr/", "zh"), (bad, "adr/", "zh")))
    monkeypatch.setattr(mod, "discover_adrs", lambda adr_dir: [tmp_path / "0001.md"])
    monkeypatch.setattr(mod, "parse_adr", lambda p: None)
    monkeypatch.setattr(mod, "status_mismatches", lambda entries: [])
    monkeypatch.setattr(mod, "render_table", lambda entries, prefix="adr/", lang="zh": "NEW")
    monkeypatch.setattr(sys, "argv", ["generate_adr_index.py", "--write"])
    assert mod.main() == 2
    assert "old" in good.read_text(encoding="utf-8")


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() != 0,
                    reason="needs root to run the child as another uid")
def test_tenant_id_json_with_a_read_only_second_copy_rewrites_neither():
    """The reproduction of #4250, unprivileged and for real: two stale copies,
    the second 0444. Before: rc 2 with the first rewritten."""
    if shutil.which("setpriv") is None:
        pytest.skip("needs setpriv")
    mod = importlib.import_module("gen_tenant_id_json")
    work = Path(tempfile.mkdtemp(dir="/var/tmp"))
    try:
        os.chmod(work, 0o755)
        tree = work / "repo"
        for rel in ("scripts/tools", "docs/schemas"):
            shutil.copytree(REPO / rel, tree / rel, ignore=shutil.ignore_patterns("__pycache__"))
        copies = [tree / rel for rel in mod.OUT_RELS]
        for path in copies:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('"stale"\n', encoding="utf-8")
        os.chmod(copies[0], 0o666)  # writable by the child, in place
        os.chmod(copies[1], 0o444)
        env = {k: v for k, v in os.environ.items() if not k.startswith(("COV_CORE_", "COVERAGE_"))}
        proc = subprocess.run(
            ["setpriv", "--reuid=65534", "--regid=65534", "--clear-groups",
             sys.executable, "-B", str(tree / "scripts/tools/dx/gen_tenant_id_json.py")],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60, env=env, cwd=work)
        assert proc.returncode == 2, (proc.stdout, proc.stderr)
        assert "not writable" in proc.stderr, proc.stderr
        assert [p.read_text(encoding="utf-8") for p in copies] == ['"stale"\n', '"stale"\n']
    finally:
        shutil.rmtree(work, ignore_errors=True)
