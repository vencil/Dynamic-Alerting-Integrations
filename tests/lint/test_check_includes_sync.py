"""Tests for check_includes_sync.py — bilingual include snippet sync checker.

Merged from previous _extended split (PR test-refactor sweep): pure-function
checks (count_*, compare_pair, _create_en_stub) live alongside the main()
CLI orchestrator tests at the bottom of the file.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

import check_includes_sync as cis
from _lib_exitcodes import EXIT_CALLER_ERROR


# ---------------------------------------------------------------------------
# Structural metric functions
# ---------------------------------------------------------------------------
class TestCountCodeBlocks:
    def test_basic(self):
        md = "```python\nprint('hi')\n```\n\ntext\n\n```bash\nls\n```"
        assert cis.count_code_blocks(md) == 4  # opening + closing pairs

    def test_none(self):
        assert cis.count_code_blocks("no code blocks here") == 0


class TestCountTableRows:
    def test_basic(self):
        md = "| Col A | Col B |\n|---|---|\n| 1 | 2 |"
        assert cis.count_table_rows(md) == 3

    def test_none(self):
        assert cis.count_table_rows("no tables") == 0


class TestCountUrls:
    def test_basic(self):
        md = "See https://example.com and http://foo.bar/baz"
        urls = cis.count_urls(md)
        assert len(urls) == 2
        assert "https://example.com" in urls

    def test_none(self):
        assert cis.count_urls("no urls") == []


class TestExtractVersions:
    def test_semver(self):
        md = "Version v2.1.0 and 1.0.0"
        versions = cis.extract_versions(md)
        assert "v2.1.0" in versions
        assert "1.0.0" in versions

    def test_none(self):
        assert cis.extract_versions("no versions") == []


class TestCountListItems:
    def test_unordered(self):
        md = "- item 1\n- item 2\n- item 3"
        assert cis.count_list_items(md) == 3

    def test_ordered(self):
        md = "1. first\n2. second"
        assert cis.count_list_items(md) == 2

    def test_mixed(self):
        md = "- bullet\n1. numbered"
        assert cis.count_list_items(md) == 2

    def test_none(self):
        assert cis.count_list_items("no lists") == 0


# ---------------------------------------------------------------------------
# compare_pair
# ---------------------------------------------------------------------------
class TestComparePair:
    def test_in_sync(self, tmp_path):
        zh = tmp_path / "snippet.md"
        en = tmp_path / "snippet.en.md"
        content = "# Title\n\n- item 1\n- item 2\n"
        zh.write_text(content, encoding="utf-8")
        en.write_text(content, encoding="utf-8")
        issues = cis.compare_pair(zh, en)
        assert issues == []

    def test_missing_english(self, tmp_path):
        zh = tmp_path / "snippet.md"
        en = tmp_path / "snippet.en.md"
        zh.write_text("content", encoding="utf-8")
        # en doesn't exist
        issues = cis.compare_pair(zh, en)
        assert len(issues) == 1
        assert "missing" in issues[0].lower()

    def test_code_block_mismatch(self, tmp_path):
        zh = tmp_path / "snippet.md"
        en = tmp_path / "snippet.en.md"
        zh.write_text("```yaml\nkey: val\n```\n", encoding="utf-8")
        en.write_text("no code blocks\n", encoding="utf-8")
        issues = cis.compare_pair(zh, en)
        assert any("code blocks" in i for i in issues)

    def test_table_row_mismatch(self, tmp_path):
        zh = tmp_path / "snippet.md"
        en = tmp_path / "snippet.en.md"
        zh.write_text("| A | B |\n|---|---|\n| 1 | 2 |\n", encoding="utf-8")
        en.write_text("| A | B |\n|---|---|\n", encoding="utf-8")
        issues = cis.compare_pair(zh, en)
        assert any("table rows" in i for i in issues)

    def test_version_mismatch(self, tmp_path):
        zh = tmp_path / "snippet.md"
        en = tmp_path / "snippet.en.md"
        zh.write_text("Version v2.1.0\n", encoding="utf-8")
        en.write_text("Version v2.0.0\n", encoding="utf-8")
        issues = cis.compare_pair(zh, en)
        assert any("versions" in i for i in issues)

    def test_url_count_mismatch(self, tmp_path):
        zh = tmp_path / "snippet.md"
        en = tmp_path / "snippet.en.md"
        zh.write_text("See https://a.com and https://b.com\n", encoding="utf-8")
        en.write_text("See https://a.com\n", encoding="utf-8")
        issues = cis.compare_pair(zh, en)
        assert any("URLs" in i for i in issues)

    def test_list_item_mismatch(self, tmp_path):
        zh = tmp_path / "snippet.md"
        en = tmp_path / "snippet.en.md"
        zh.write_text("- a\n- b\n- c\n", encoding="utf-8")
        en.write_text("- a\n- b\n", encoding="utf-8")
        issues = cis.compare_pair(zh, en)
        assert any("list items" in i for i in issues)


# ---------------------------------------------------------------------------
# _create_en_stub
# ---------------------------------------------------------------------------
class TestCreateEnStub:
    def test_creates_stub_with_translations(self, tmp_path):
        zh = tmp_path / "snippet.md"
        en = tmp_path / "snippet.en.md"
        zh.write_text("# 規則包 合計\n\n| 運營模式 | 自監控 |\n", encoding="utf-8")
        cis._create_en_stub(zh, en)
        assert en.exists()
        content = en.read_text(encoding="utf-8")
        assert "Rule Pack" in content
        assert "Total" in content
        assert "operational mode" in content
        assert "self-monitoring" in content

    def test_preserves_non_chinese(self, tmp_path):
        zh = tmp_path / "snippet.md"
        en = tmp_path / "snippet.en.md"
        zh.write_text("```yaml\nkey: value\n```\n", encoding="utf-8")
        cis._create_en_stub(zh, en)
        content = en.read_text(encoding="utf-8")
        assert "key: value" in content


# ---------------------------------------------------------------------------
# main() CLI — orchestrator coverage (was test_check_includes_sync_extended.py)
# ---------------------------------------------------------------------------
class TestMainCLI:
    """check_includes_sync main() tests."""

    def _setup_includes(self, tmp_path, zh_content="# Title\n\n- item 1\n",
                        en_content=None, create_en=True):
        """Set up an includes directory with zh/en pairs."""
        includes = tmp_path / "docs" / "includes"
        includes.mkdir(parents=True)

        zh = includes / "snippet.md"
        zh.write_text(zh_content, encoding="utf-8")

        if create_en and en_content is not None:
            en = includes / "snippet.en.md"
            en.write_text(en_content, encoding="utf-8")

        return includes

    def test_all_in_sync(self, tmp_path, monkeypatch, capsys, cli_argv):
        """All pairs in sync should return 0."""
        includes = self._setup_includes(tmp_path,
                                        "# Title\n\n- item 1\n",
                                        "# Title\n\n- item 1\n")
        monkeypatch.setattr(cis, "INCLUDES_DIR", includes)
        cli_argv('check_includes_sync')
        result = cis.main()
        assert result == 0
        out = capsys.readouterr().out
        assert "in sync" in out

    def test_missing_english_check_mode(self, tmp_path, monkeypatch, capsys, cli_argv):
        """Missing English file in --check mode should return 1."""
        includes = self._setup_includes(tmp_path, create_en=False)
        monkeypatch.setattr(cis, "INCLUDES_DIR", includes)
        cli_argv('check_includes_sync', '--check')
        result = cis.main()
        assert result == 1

    def test_missing_english_no_check(self, tmp_path, monkeypatch, capsys, cli_argv):
        """Missing English file without --check returns 0."""
        includes = self._setup_includes(tmp_path, create_en=False)
        monkeypatch.setattr(cis, "INCLUDES_DIR", includes)
        cli_argv('check_includes_sync')
        result = cis.main()
        # Without --check, returns 0 even with issues
        # Actually, the code returns 1 if total_issues > 0 or missing_en > 0
        # when --check is set, and 0 otherwise
        assert result == 0

    def test_verbose_in_sync(self, tmp_path, monkeypatch, capsys, cli_argv):
        """--verbose shows OK for in-sync pairs."""
        includes = self._setup_includes(tmp_path,
                                        "# Title\n\n- item 1\n",
                                        "# Title\n\n- item 1\n")
        monkeypatch.setattr(cis, "INCLUDES_DIR", includes)
        cli_argv('check_includes_sync', '--verbose')
        result = cis.main()
        assert result == 0
        out = capsys.readouterr().out
        assert "in sync" in out.lower()

    def test_fix_creates_stubs(self, tmp_path, monkeypatch, capsys, cli_argv):
        """--fix creates missing English stubs."""
        includes = self._setup_includes(tmp_path,
                                        "# 規則包\n",
                                        create_en=False)
        monkeypatch.setattr(cis, "INCLUDES_DIR", includes)
        cli_argv('check_includes_sync', '--fix')
        result = cis.main()
        assert result == 0
        en_file = includes / "snippet.en.md"
        assert en_file.exists()
        content = en_file.read_text(encoding="utf-8")
        assert "Rule Pack" in content

    def test_structural_mismatch(self, tmp_path, monkeypatch, capsys, cli_argv):
        """Structural mismatch between zh and en."""
        includes = self._setup_includes(
            tmp_path,
            "```yaml\nkey: val\n```\n",
            "no code blocks\n")
        monkeypatch.setattr(cis, "INCLUDES_DIR", includes)
        cli_argv('check_includes_sync', '--check')
        result = cis.main()
        assert result == 1
        out = capsys.readouterr().out
        assert "code blocks" in out

    def test_no_includes_dir(self, tmp_path, monkeypatch, capsys, cli_argv):
        """Nonexistent includes dir is a caller-error (exit 2, #452)."""
        monkeypatch.setattr(cis, "INCLUDES_DIR",
                            tmp_path / "nonexistent")
        cli_argv('check_includes_sync')
        result = cis.main()
        assert result == EXIT_CALLER_ERROR

    def test_no_zh_files(self, tmp_path, monkeypatch, capsys, cli_argv):
        """No Chinese files returns 0."""
        includes = tmp_path / "docs" / "includes"
        includes.mkdir(parents=True)
        # Only en file, no zh
        (includes / "snippet.en.md").write_text("English only",
                                                 encoding="utf-8")
        monkeypatch.setattr(cis, "INCLUDES_DIR", includes)
        cli_argv('check_includes_sync')
        result = cis.main()
        assert result == 0



# ---------------------------------------------------------------------------
# #3981: reads refuse a non-regular include; --fix writes through the entry
# ---------------------------------------------------------------------------
def _includes(tmp_path):
    inc = tmp_path / "docs" / "includes"
    inc.mkdir(parents=True)
    return inc


@pytest.mark.parametrize("where", ["en, --check", "zh, --fix"])
def test_a_fifo_named_like_an_include_is_rc2_one_line(where, tmp_path, monkeypatch,
                                                      capsys, cli_argv):
    """A FIFO named like an include blocked the read forever — under --fix
    after the stub before it had been written. Now: rc 2, one line naming
    it, and (under --fix) no stub is written."""
    import signal
    if not hasattr(os, "mkfifo") or not hasattr(signal, "SIGALRM"):
        pytest.skip("no FIFO / SIGALRM on this platform")
    inc = _includes(tmp_path)
    (inc / "a.md").write_text("# 規則包\n", encoding="utf-8")
    if where.startswith("en"):
        fifo = inc / "a.en.md"
        argv = ["--check"]
    else:
        fifo = inc / "b.md"
        argv = ["--fix"]
    os.mkfifo(fifo)
    monkeypatch.setattr(cis, "INCLUDES_DIR", inc)
    cli_argv("check_includes_sync", *argv)

    def hung(signum, frame):
        raise AssertionError("blocked on the FIFO")
    prev = signal.signal(signal.SIGALRM, hung)
    signal.alarm(20)
    try:
        with pytest.raises(SystemExit) as exc:
            cis.main()
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, prev)
    err = capsys.readouterr().err
    assert exc.value.code == 2, err
    assert err.strip().splitlines() == [
        f"ERROR: cannot read {fifo}: not a regular file"
        " — internal path, this is a bug or an unreadable workspace"], err
    if argv == ["--fix"]:
        assert not (inc / "a.en.md").exists()


def test_fix_writes_the_stub_with_the_checkout_mode(tmp_path, monkeypatch, capsys, cli_argv):
    inc = _includes(tmp_path)
    (inc / "a.md").write_text("# 規則包\n", encoding="utf-8")
    monkeypatch.setattr(cis, "INCLUDES_DIR", inc)
    cli_argv("check_includes_sync", "--fix")
    assert cis.main() == 0
    assert "  🔧 Created a.en.md from a.md\n" in capsys.readouterr().out
    stub = inc / "a.en.md"
    assert stub.read_text(encoding="utf-8") == "# Rule Pack\n"
    if os.name != "nt":
        assert stub.stat().st_mode & 0o777 == 0o644


def _as_another_user(shape, argv):
    """Run the tool on a fresh includes tree under /var/tmp (tmp_path is 0700:
    uid 65534 could not reach it), as a user *shape(inc, privileged)* denies.
    Root can read and write anything, so under root the tool runs as uid
    65534 on root-owned files (``privileged`` True); otherwise *shape* takes
    the permission bits away from the running user. Returns (proc, inc)."""
    if not hasattr(os, "geteuid"):
        pytest.skip("needs POSIX uids and file modes")
    prefix: list[str] = []
    if os.geteuid() == 0:
        if shutil.which("setpriv") is None:
            pytest.skip("root, and no setpriv to drop to another uid")
        prefix = ["setpriv", "--reuid=65534", "--regid=65534", "--clear-groups"]
    d = Path(tempfile.mkdtemp(dir="/var/tmp"))
    _OTHER_USER_DIRS.append(d)
    inc = d / "includes"
    inc.mkdir()
    (inc / "a.md").write_text("# 規則包\n", encoding="utf-8")
    d.chmod(0o755)
    inc.chmod(0o755)
    shape(inc, bool(prefix))
    driver = (
        "import sys; from pathlib import Path\n"
        f"sys.path[:0] = [{str(Path(cis.__file__).parent)!r}, {str(Path(cis.__file__).parent.parent)!r}]\n"
        "import check_includes_sync as m, _lib_exitcodes\n"
        f"m.INCLUDES_DIR = Path({str(inc)!r})\n"
        f"sys.argv = ['check_includes_sync.py', *{argv!r}]\n"
        "_lib_exitcodes.run(m.main)\n")
    env = {k: v for k, v in os.environ.items()
           if k not in ("PYTHONUNBUFFERED", "DA_TEST_CRASH_EXIT_CODE")}
    proc = subprocess.run([*prefix, sys.executable, "-c", driver], capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=120,
                          env=env, cwd=str(d))
    return proc, inc


_OTHER_USER_DIRS: list[Path] = []


@pytest.fixture(autouse=True)
def _remove_the_other_users_dirs():
    yield
    while _OTHER_USER_DIRS:
        d = _OTHER_USER_DIRS.pop()
        for p in (d, d / "includes"):
            if p.exists():
                p.chmod(0o755)
        shutil.rmtree(d, ignore_errors=True)


def test_a_stub_that_cannot_be_written_is_rc2_one_line():
    """The includes directory takes no new file: rc 2, one line naming the
    stub, where a bare write ended in the crash handler (`unexpected internal
    error (PermissionError)`)."""
    def shape(inc, privileged):
        if not privileged:
            inc.chmod(0o555)
    proc, inc = _as_another_user(shape, ["--fix"])
    stub = inc / "a.en.md"
    assert proc.returncode == 2, proc.stderr
    assert proc.stderr.strip().splitlines() == [
        f"ERROR: cannot write {stub}: Permission denied (errno 13)"
        " — internal output path, this is a bug or an unwritable workspace"], proc.stderr
    assert not stub.exists()


def test_an_include_that_cannot_be_read_is_rc2_one_line():
    """An include the user may not read (EACCES), where the read ended in
    the crash handler (`unexpected internal error (PermissionError)`)."""
    def shape(inc, privileged):
        (inc / "a.en.md").write_text("# Rule Pack\n", encoding="utf-8")
        (inc / "a.md").chmod(0o600 if privileged else 0o000)
    proc, inc = _as_another_user(shape, ["--check"])
    assert proc.returncode == 2, proc.stderr
    assert proc.stderr.strip().splitlines() == [
        f"ERROR: cannot read {inc / 'a.md'}: Permission denied (errno 13)"
        " — internal path, this is a bug or an unreadable workspace"], proc.stderr


def test_an_include_that_is_not_utf8_is_rc2_one_line(tmp_path, monkeypatch, capsys, cli_argv):
    """A UnicodeDecodeError is a ValueError, not an OSError: it went to the
    crash handler (`unexpected internal error (UnicodeDecodeError)`)."""
    inc = _includes(tmp_path)
    (inc / "a.md").write_bytes(b"\xff\xfe# x\n")
    (inc / "a.en.md").write_text("# x\n", encoding="utf-8")
    monkeypatch.setattr(cis, "INCLUDES_DIR", inc)
    cli_argv("check_includes_sync", "--check")
    with pytest.raises(SystemExit) as exc:
        cis.main()
    err = capsys.readouterr().err
    assert exc.value.code == 2, err
    assert err.strip().splitlines() == [
        f"ERROR: cannot read {inc / 'a.md'}: not UTF-8 (invalid start byte)"
        " — internal path, this is a bug or an unreadable workspace"], err


@pytest.mark.skipif(not os.path.exists("/dev/null") or os.name == "nt", reason="POSIX /dev/null")
def test_an_include_that_is_a_device_is_refused(tmp_path, monkeypatch, capsys, cli_argv):
    """Declared (#3981): every non-regular include is refused, a link to
    /dev/null included — it used to read as an empty file (rc 0)."""
    from _platform_fs import symlink_or_skip
    inc = _includes(tmp_path)
    symlink_or_skip("/dev/null", inc / "a.md")
    (inc / "a.en.md").write_text("", encoding="utf-8")
    monkeypatch.setattr(cis, "INCLUDES_DIR", inc)
    cli_argv("check_includes_sync")
    with pytest.raises(SystemExit) as exc:
        cis.main()
    assert exc.value.code == 2
    assert "not a regular file" in capsys.readouterr().err


def test_fix_names_an_include_it_cannot_read(tmp_path, monkeypatch, capsys, cli_argv):
    """A zh include that is a symlink to nothing: the stub's source cannot be
    read. rc 2, one line naming it, and the stub before it is not written —
    where the read ended in the crash handler (`unexpected internal error`)."""
    from _platform_fs import symlink_or_skip
    inc = _includes(tmp_path)
    (inc / "a.md").write_text("# 規則包\n", encoding="utf-8")
    dangling = inc / "b.md"
    symlink_or_skip(inc / "nowhere.md", dangling)
    monkeypatch.setattr(cis, "INCLUDES_DIR", inc)
    cli_argv("check_includes_sync", "--fix")
    with pytest.raises(SystemExit) as exc:
        cis.main()
    err = capsys.readouterr().err
    assert exc.value.code == 2, err
    assert err.strip().splitlines() == [
        f"ERROR: cannot read {dangling}: No such file or directory (errno 2)"
        " — internal path, this is a bug or an unreadable workspace"], err
    assert not (inc / "a.en.md").exists()
