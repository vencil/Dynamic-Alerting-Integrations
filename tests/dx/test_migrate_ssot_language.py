"""migrate_ssot_language: the `--execute` rewrites go through the artifact
entry (#3981)."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "tools", "dx"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "tools"))

import migrate_ssot_language as mig  # noqa: E402
from _lib_output import OutputWriteError  # noqa: E402

_STALE = "---\nlang: zh\ntitle: x\n---\nbody\n"


def _plan_for(target: Path) -> "mig.MigrationPlan":
    plan = mig.MigrationPlan()
    plan.add(mig.MigrationAction(target, target, "update_frontmatter", "lang"))
    return plan


def test_execute_rewrites_the_frontmatter_through_the_entry(tmp_path, monkeypatch, capsys):
    """Control: the same run with the entry left alone writes the file,
    with the mode a checkout gives."""
    doc = tmp_path / "a.md"
    doc.write_text(_STALE, encoding="utf-8")
    doc.chmod(0o640)
    monkeypatch.setattr(mig, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(mig, "build_migration_plan", lambda d: _plan_for(doc))
    monkeypatch.setattr(sys, "argv", ["migrate_ssot_language.py", "--execute"])
    with pytest.raises(SystemExit) as exc:
        mig.main()
    assert exc.value.code == 0, capsys.readouterr()
    assert doc.read_text(encoding="utf-8") == _STALE.replace("lang: zh", "lang: en")
    if os.name != "nt":
        assert doc.stat().st_mode & 0o777 == 0o644


def test_a_refused_target_is_rc2_one_line(tmp_path, monkeypatch, capsys):
    """A write the entry refuses ends at `main` as rc 2 and one line naming
    the file, where the bare `write_text` raised into the crash handler."""
    doc = tmp_path / "a.md"
    doc.write_text(_STALE, encoding="utf-8")

    def refuse(path, text, **kw):
        raise OutputWriteError(path, OSError(13, "Permission denied"))
    monkeypatch.setattr(mig, "write_repo_artifact", refuse)
    monkeypatch.setattr(mig, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(mig, "build_migration_plan", lambda d: _plan_for(doc))
    monkeypatch.setattr(sys, "argv", ["migrate_ssot_language.py", "--execute"])
    with pytest.raises(SystemExit) as exc:
        mig.main()
    err = capsys.readouterr().err
    assert exc.value.code == 2, err
    assert err.strip().splitlines() == [
        f"ERROR: cannot write {doc}: Permission denied (errno 13)"
        " — internal output path, this is a bug or an unwritable workspace"], err
    assert doc.read_text(encoding="utf-8") == _STALE


_STDOUT_DRIVER = """\
import sys
from pathlib import Path
sys.path[:0] = [sys.argv[1], sys.argv[2]]
import migrate_ssot_language as mig, _lib_exitcodes
root = Path(sys.argv[3])
src, dst = root / "b.md", root / "b.zh.md"
plan = mig.MigrationPlan()
plan.add(mig.MigrationAction(src, dst, "rename", "b.md -> b.zh.md"))
plan.add(mig.MigrationAction(dst, dst, "update_frontmatter", "lang"))
mig.REPO_ROOT = root
mig.build_migration_plan = lambda d: plan
sys.argv = ["migrate_ssot_language.py", "--execute"]
_lib_exitcodes.run(mig.main)
"""


def test_a_refused_file_is_judged_before_the_first_rename(tmp_path):
    """#3981: stdout appended onto a file the plan renames and then rewrites.
    The renames ran first and the rewrite was refused after them, leaving the
    tree half migrated; now it is refused before any rename: rc 2, one line,
    the file still under its old name with its frontmatter untouched."""
    src = tmp_path / "b.md"
    src.write_text(_STALE, encoding="utf-8")
    dx = Path(mig.__file__).resolve().parent
    with open(src, "a", encoding="utf-8") as out:
        proc = subprocess.run(
            [sys.executable, "-c", _STDOUT_DRIVER, str(dx), str(dx.parent), str(tmp_path)],
            stdout=out, stderr=subprocess.PIPE, text=True, encoding="utf-8", timeout=60)
    assert proc.returncode == 2, proc.stderr
    assert proc.stderr.strip().splitlines() == [
        f"ERROR: cannot write {src}: is this process's standard stream"
        " — internal output path, this is a bug or an unwritable workspace"], proc.stderr
    # The banner printed ahead of the plan lands in the file (it is stdout).
    assert src.read_text(encoding="utf-8").startswith(_STALE)
    assert not (tmp_path / "b.zh.md").exists()
