"""audit_rules_drift ``--out`` 的路徑顯示（#2343）。

修正前，報表寫完之後才用 ``out.relative_to(REPO_ROOT)`` 組訊息：``--out``
指到 repo 外、或給相對路徑（argparse 不 resolve）時丟 ``ValueError``，
rc 1 帶 traceback，而檔案其實已經寫出去了。

這裡經由真正的 ``main`` 驅動，斷言 rc 0、檔案存在、訊息印出的路徑。
#3982（owner 2026-11-18）：``--out`` 是 operator 輸出——原地寫、0600、
``-`` 是 stdout，所以收據行改印在 stderr。
「repo 內」的對照組把 ``REPO_ROOT`` 指到暫存假根，不寫進真 repo 的
``docs/internal/audit-reports/``。
"""
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_OPS = REPO_ROOT / "scripts" / "ops"
if str(_OPS) not in sys.path:
    sys.path.insert(0, str(_OPS))

import audit_rules_drift as ard  # noqa: E402


@pytest.fixture
def memory_dir(tmp_path: Path) -> Path:
    # 顯式 --memory-dir 指到不存在的目錄會先 rc 2 退出，所以建一個空的。
    d = tmp_path / "nomem"
    d.mkdir()
    return d


def _wrote_line(out: str) -> str:
    lines = [ln for ln in out.splitlines() if ln.startswith("wrote drift report: ")]
    assert len(lines) == 1, out
    return lines[0].removeprefix("wrote drift report: ")


def test_out_outside_repo_absolute(tmp_path, memory_dir, capsys):
    target = tmp_path / "outside" / "report.md"
    rc = ard.main(["--memory-dir", str(memory_dir), "--out", str(target)])
    assert rc == 0
    assert target.is_file()
    assert _wrote_line(capsys.readouterr().err) == str(target.resolve())


def test_out_relative_outside_repo(tmp_path, memory_dir, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rc = ard.main(["--memory-dir", str(memory_dir), "--out", "rel-report.md"])
    assert rc == 0
    assert (tmp_path / "rel-report.md").is_file()
    assert _wrote_line(capsys.readouterr().err) == str(
        (tmp_path / "rel-report.md").resolve()
    )


@pytest.fixture
def fake_root(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "fakeroot"
    root.mkdir()
    monkeypatch.setattr(ard, "REPO_ROOT", root.resolve())
    return root


def test_out_relative_inside_repo_prints_relative(
    fake_root, memory_dir, capsys, monkeypatch
):
    # 相對路徑即使落在 repo 內，修正前也會 ValueError（沒 resolve）。
    (fake_root / "sub").mkdir()
    monkeypatch.chdir(fake_root / "sub")
    rc = ard.main(["--memory-dir", str(memory_dir), "--out", "rel.md"])
    assert rc == 0
    assert (fake_root / "sub" / "rel.md").is_file()
    assert _wrote_line(capsys.readouterr().err) == str(Path("sub") / "rel.md")


def test_out_absolute_inside_repo_prints_relative(fake_root, memory_dir, capsys):
    # 對照組：修正前就是 rc 0、印相對路徑，修正後不得改變。
    target = fake_root / "reports" / "in.md"
    rc = ard.main(["--memory-dir", str(memory_dir), "--out", str(target)])
    assert rc == 0
    assert target.is_file()
    assert _wrote_line(capsys.readouterr().err) == str(Path("reports") / "in.md")


# ── #3982: an operator output ─────────────────────────────────────────────
def test_out_dash_is_the_report_on_stdout_alone(tmp_path, memory_dir, capsys, monkeypatch):
    """`--out -` used to write a file called `-`. The receipt is on stderr,
    so stdout is the report and nothing else — the same text `--stdout`
    prints."""
    monkeypatch.chdir(tmp_path)
    assert ard.main(["--memory-dir", str(memory_dir), "--out", "-"]) == 0
    got = capsys.readouterr()
    assert not (tmp_path / "-").exists()
    assert "wrote drift report" not in got.out and "wrote drift report" not in got.err
    assert ard.main(["--memory-dir", str(memory_dir), "--stdout"]) == 0
    assert capsys.readouterr().out == got.out


@pytest.mark.skipif(os.name != "posix", reason="POSIX modes")
@pytest.mark.parametrize("existing", [None, 0o644])
def test_the_report_is_written_in_place_and_ends_0600(tmp_path, memory_dir, existing):
    target = tmp_path / "r.md"
    ino = None
    if existing is not None:
        target.write_text("old\n", encoding="utf-8")
        target.chmod(existing)
        ino = target.stat().st_ino
    assert ard.main(["--memory-dir", str(memory_dir), "--out", str(target)]) == 0
    st = target.stat()
    assert stat.S_IMODE(st.st_mode) == 0o600
    assert target.read_text(encoding="utf-8").startswith("#")
    if ino is not None:
        assert st.st_ino == ino, "replaced, not written in place"


def test_an_empty_out_is_refused_at_parse_time(memory_dir, capsys):
    with pytest.raises(SystemExit) as ei:
        ard.main(["--memory-dir", str(memory_dir), "--out", ""])
    assert ei.value.code == 2
    assert "argument --out: an empty value names no file" in capsys.readouterr().err
