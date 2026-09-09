#!/usr/bin/env python3
"""test_check_bilingual_content.py — check_bilingual_content.py 測試。

驗證:
  1. count_cjk_ratio() — CJK 字元比例計算
  2. scan_en_docs() — .en.md CJK 偵測
  3. scan_zh_docs() — zh 文件低 CJK 偵測
  4. run_all_checks() — 全管線
  5. 輸出格式 (text/JSON)
  6. CLI (main)
"""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts" / "tools" / "lint"))
import check_bilingual_content as cbc  # noqa: E402


# ---------------------------------------------------------------------------
# TestCountCjkRatio
# ---------------------------------------------------------------------------


class TestCountCjkRatio:
    """count_cjk_ratio() CJK 字元比例計算。"""

    def test_empty_string(self):
        """空字串回傳 0.0。"""
        assert cbc.count_cjk_ratio("") == 0.0

    def test_whitespace_only(self):
        """純空白回傳 0.0。"""
        assert cbc.count_cjk_ratio("   \n\t  ") == 0.0

    def test_pure_english(self):
        """純英文回傳 0.0。"""
        assert cbc.count_cjk_ratio("Hello World") == 0.0

    def test_pure_chinese(self):
        """純中文回傳 1.0。"""
        assert cbc.count_cjk_ratio("你好世界") == 1.0

    def test_mixed_content(self):
        """混合內容回傳正確比例。"""
        # "你好 Hello" → 2 CJK out of 7 non-ws chars = 0.2857...
        ratio = cbc.count_cjk_ratio("你好Hello")
        assert 0.25 < ratio < 0.35

    def test_markdown_with_cjk(self):
        """Markdown 內容中的 CJK 正確計算。"""
        text = "## Section Title 章節標題\n\nThis is content 這是內容。"
        ratio = cbc.count_cjk_ratio(text)
        assert 0.0 < ratio < 1.0


# ---------------------------------------------------------------------------
# TestScanEnDocs
# ---------------------------------------------------------------------------


class TestScanEnDocs:
    """scan_en_docs() .en.md CJK 偵測。"""

    def test_clean_en_doc(self, tmp_path):
        """純英文 .en.md 無 findings。"""
        en_doc = tmp_path / "test.en.md"
        en_doc.write_text("# English Title\n\nEnglish content here.",
                          encoding="utf-8")
        findings = cbc.scan_en_docs(tmp_path, threshold=0.2)
        assert len(findings) == 0

    def test_cjk_in_en_doc(self, tmp_path):
        """含大量 CJK 的 .en.md 觸發 warning。"""
        en_doc = tmp_path / "test.en.md"
        en_doc.write_text("# 中文標題\n\n這是中文內容而非英文。",
                          encoding="utf-8")
        with patch.object(cbc, "PROJECT_ROOT", tmp_path):
            findings = cbc.scan_en_docs(tmp_path, threshold=0.2)
        assert len(findings) == 1
        assert findings[0][0] == "warning"

    def test_threshold_sensitivity(self, tmp_path):
        """threshold 降低時偵測到更多 findings。"""
        en_doc = tmp_path / "test.en.md"
        # ~15% CJK: "AB你CD" → 1/5 = 20%
        en_doc.write_text("ABCDEFGHIJ你好",
                          encoding="utf-8")
        with patch.object(cbc, "PROJECT_ROOT", tmp_path):
            low = cbc.scan_en_docs(tmp_path, threshold=0.05)
            high = cbc.scan_en_docs(tmp_path, threshold=0.50)
        assert len(low) >= len(high)

    def test_nested_en_doc(self, tmp_path):
        """子目錄中的 .en.md 也被掃描。"""
        sub = tmp_path / "subdir"
        sub.mkdir()
        en_doc = sub / "nested.en.md"
        en_doc.write_text("# 完全中文文件\n\n所有內容都是中文。",
                          encoding="utf-8")
        with patch.object(cbc, "PROJECT_ROOT", tmp_path):
            findings = cbc.scan_en_docs(tmp_path, threshold=0.2)
        assert len(findings) == 1


# ---------------------------------------------------------------------------
# TestScanZhDocs
# ---------------------------------------------------------------------------


class TestScanZhDocs:
    """scan_zh_docs() zh 文件低 CJK 偵測。"""

    def test_normal_zh_doc(self, tmp_path):
        """正常中文文件無 findings。"""
        zh_doc = tmp_path / "test.md"
        zh_doc.write_text("# 中文標題\n\n" + "這是一段很長的中文內容。" * 20,
                          encoding="utf-8")
        with patch.object(cbc, "PROJECT_ROOT", tmp_path):
            findings = cbc.scan_zh_docs(tmp_path)
        assert len(findings) == 0

    def test_en_doc_skipped(self, tmp_path):
        """scan_zh_docs 不掃描 .en.md。"""
        en_doc = tmp_path / "test.en.md"
        en_doc.write_text("Pure English content " * 30,
                          encoding="utf-8")
        with patch.object(cbc, "PROJECT_ROOT", tmp_path):
            findings = cbc.scan_zh_docs(tmp_path)
        assert len(findings) == 0

    def test_short_file_skipped(self, tmp_path):
        """短文件 (<200 chars) 被跳過。"""
        zh_doc = tmp_path / "short.md"
        zh_doc.write_text("Short English only.",
                          encoding="utf-8")
        with patch.object(cbc, "PROJECT_ROOT", tmp_path):
            findings = cbc.scan_zh_docs(tmp_path)
        assert len(findings) == 0

    def test_includes_dir_skipped(self, tmp_path):
        """includes 目錄被跳過。"""
        inc = tmp_path / "includes"
        inc.mkdir()
        zh_doc = inc / "generated.md"
        zh_doc.write_text("Pure English auto-generated content " * 20,
                          encoding="utf-8")
        with patch.object(cbc, "PROJECT_ROOT", tmp_path):
            findings = cbc.scan_zh_docs(tmp_path)
        assert len(findings) == 0


# ---------------------------------------------------------------------------
# #1810 — includes 只對 docs_dir 之下的相對段判定
# ---------------------------------------------------------------------------

# >200 字元、只有一個 CJK 字：0 < ratio < DEFAULT_ZH_MIN_THRESHOLD → info finding。
_UNTRANSLATED = "This is all English content without any CJK characters. " * 10 + "中"


def _docs_tree(root: Path) -> Path:
    """docs/ 下一個未翻譯的 zh 文件（>200 字元、幾乎純英文）。"""
    docs = root / "docs"
    (docs / "guide").mkdir(parents=True)
    (docs / "guide" / "setup.zh.md").write_text(_UNTRANSLATED, encoding="utf-8")
    return docs


def _zh_findings(root: Path) -> set:
    with patch.object(cbc, "PROJECT_ROOT", root):
        findings = cbc.scan_zh_docs(root / "docs")
    return {path for _sev, _msg, path, _ratio in findings}


class TestIncludesIsJudgedBelowDocsDir:
    """#1810：checkout 的祖先目錄叫 ``includes`` 時整棵 docs/ 不得被跳過。

    舊述詞看絕對路徑的每一段，祖先撞名就全數跳過、印 passed。正向格把同
    一棵樹放在撞名父目錄與中性父目錄下比對結果；負向格釘住 docs/ 之內
    的 ``includes/`` 仍被跳過。
    """

    def test_same_tree_under_includes_and_under_plain_finds_the_same_files(
            self, tmp_path):
        clash = tmp_path / "includes" / "repo"
        plain = tmp_path / "plain" / "repo"
        _docs_tree(clash)
        _docs_tree(plain)
        under_clash = _zh_findings(clash)
        under_plain = _zh_findings(plain)
        assert under_clash == under_plain
        assert under_clash == {str(Path("docs/guide/setup.zh.md"))}

    def test_includes_inside_docs_dir_is_still_skipped(self, tmp_path):
        root = tmp_path / "includes" / "repo"
        docs = _docs_tree(root)
        (docs / "includes").mkdir()
        (docs / "includes" / "generated.zh.md").write_text(
            _UNTRANSLATED, encoding="utf-8")
        assert _zh_findings(root) == {str(Path("docs/guide/setup.zh.md"))}


# ---------------------------------------------------------------------------
# TestRunAllChecks
# ---------------------------------------------------------------------------


class TestRunAllChecks:
    """run_all_checks() 全管線。"""

    def test_empty_dir(self, tmp_path):
        """空目錄無 findings。"""
        findings = cbc.run_all_checks(docs_dir=tmp_path)
        assert len(findings) == 0

    def test_combined_findings(self, tmp_path):
        """同時偵測 en 和 zh 問題。"""
        en_doc = tmp_path / "bad.en.md"
        en_doc.write_text("# 完全中文\n\n這不應該是英文文件。" * 10,
                          encoding="utf-8")
        zh_doc = tmp_path / "untranslated.md"
        zh_doc.write_text("This is all English content without any CJK "
                          "characters whatsoever. " * 15,
                          encoding="utf-8")
        with patch.object(cbc, "PROJECT_ROOT", tmp_path):
            findings = cbc.run_all_checks(docs_dir=tmp_path)
        # At least the en doc warning
        assert any(s == "warning" for s, *_ in findings)


# ---------------------------------------------------------------------------
# TestOutputFormatting
# ---------------------------------------------------------------------------


class TestOutputFormatting:
    """輸出格式測試。"""

    def test_text_all_pass(self):
        """無 findings 的 text 報告。"""
        text = cbc.format_text_report([])
        assert "All bilingual content checks passed" in text

    def test_text_with_warnings(self):
        """有 warnings 的 text 報告。"""
        findings = [
            ("warning", "test.en.md: 50% CJK", "test.en.md", 0.5),
        ]
        text = cbc.format_text_report(findings)
        assert "1 warning" in text
        assert "50% CJK" in text

    def test_json_pass(self):
        """無 findings 的 JSON 報告。"""
        output = cbc.format_json_report([])
        data = json.loads(output)
        assert data["status"] == "pass"
        assert data["warning_count"] == 0

    def test_json_with_findings(self):
        """有 findings 的 JSON 報告。"""
        findings = [
            ("warning", "bad.en.md: 60% CJK", "bad.en.md", 0.6),
            ("info", "maybe.md: 2% CJK", "maybe.md", 0.02),
        ]
        output = cbc.format_json_report(findings)
        data = json.loads(output)
        assert data["status"] == "warn"
        assert data["warning_count"] == 1
        assert data["info_count"] == 1
        assert len(data["findings"]) == 2


# ---------------------------------------------------------------------------
# TestCLI
# ---------------------------------------------------------------------------


def _one_unpaired_doc(root: Path) -> None:
    """非空母體、零 finding：一個沒有 .en.md / .zh.md 配對的 plain.md。"""
    (root / "plain.md").write_text("# plain\n\n單獨一份、不配對。\n", encoding="utf-8")


class TestCLI:
    """CLI main() 測試。"""

    def test_main_no_findings(self, tmp_path, monkeypatch, capsys, cli_argv):
        """無 findings 時正常退出。"""
        _one_unpaired_doc(tmp_path)
        cli_argv("check_bilingual_content")
        monkeypatch.setattr(cbc, "DOCS_DIR", tmp_path)
        monkeypatch.setattr(cbc, "PROJECT_ROOT", tmp_path)
        cbc.main()
        out = capsys.readouterr().out
        assert "passed" in out

    def test_main_json_flag(self, tmp_path, monkeypatch, capsys, cli_argv):
        """--json 輸出 JSON。"""
        _one_unpaired_doc(tmp_path)
        cli_argv("check_bilingual_content", "--json")
        monkeypatch.setattr(cbc, "DOCS_DIR", tmp_path)
        monkeypatch.setattr(cbc, "PROJECT_ROOT", tmp_path)
        cbc.main()
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["status"] == "pass"

    def test_main_ci_exits_on_warnings(self, tmp_path, monkeypatch, capsys, cli_argv):
        """--ci 有 warnings 時 exit 1。"""
        en_doc = tmp_path / "bad.en.md"
        en_doc.write_text("# 完全中文\n\n全部中文內容。" * 10,
                          encoding="utf-8")
        cli_argv("check_bilingual_content", "--ci")
        monkeypatch.setattr(cbc, "DOCS_DIR", tmp_path)
        monkeypatch.setattr(cbc, "PROJECT_ROOT", tmp_path)
        with pytest.raises(SystemExit) as exc_info:
            cbc.main()
        assert exc_info.value.code == 1

    def test_main_threshold_flag(self, tmp_path, monkeypatch, capsys, cli_argv):
        """--threshold 參數生效。"""
        _one_unpaired_doc(tmp_path)
        cli_argv("check_bilingual_content", "--threshold", "0.99")
        monkeypatch.setattr(cbc, "DOCS_DIR", tmp_path)
        monkeypatch.setattr(cbc, "PROJECT_ROOT", tmp_path)
        cbc.main()
        out = capsys.readouterr().out
        assert "passed" in out


# ---------------------------------------------------------------------------
# #1810 — 空母體是 caller error，不是 passed
# ---------------------------------------------------------------------------


class TestEmptyPopulationIsACallerError:
    """#1810：docs 下沒有任何 markdown、或 docs 不是目錄 ⇒ rc 2。

    修前空 tmp_path 印 "All bilingual content checks passed" 且 rc 0，
    與「每份文件都乾淨」無法區分。
    """

    def _point(self, monkeypatch, docs: Path) -> None:
        monkeypatch.setattr(cbc, "DOCS_DIR", docs)
        monkeypatch.setattr(cbc, "PROJECT_ROOT", docs.parent)

    def test_no_markdown_is_rc_2_and_the_reason_is_the_last_stdout_line(
            self, tmp_path, monkeypatch, capsys, cli_argv):
        """下限被拿掉、或理由不再到 stdout 最後一行（validate_all 只引用
        stdout 最後一行）時轉紅。"""
        docs = tmp_path / "docs"
        (docs / "images").mkdir(parents=True)
        (docs / "images" / "logo.png").write_bytes(b"\x89PNG")
        self._point(monkeypatch, docs)
        cli_argv("check_bilingual_content", "--ci")
        with pytest.raises(SystemExit) as exc_info:
            cbc.main()
        assert exc_info.value.code == cbc.EXIT_CALLER_ERROR
        captured = capsys.readouterr()
        assert "no markdown files found" in captured.err
        assert "no markdown files found" in captured.out.strip().splitlines()[-1]
        assert "passed" not in captured.out

    def test_json_mode_refuses_with_one_json_document(
            self, tmp_path, monkeypatch, capsys, cli_argv):
        """--json 的 stdout 在每條終止路徑都必須是單一 JSON 文件（dev-rules
        §13）；拒絕改印散文到 stdout 時轉紅。"""
        docs = tmp_path / "docs"
        docs.mkdir()
        self._point(monkeypatch, docs)
        cli_argv("check_bilingual_content", "--json")
        with pytest.raises(SystemExit) as exc_info:
            cbc.main()
        assert exc_info.value.code == cbc.EXIT_CALLER_ERROR
        captured = capsys.readouterr()
        doc = json.loads(captured.out)
        assert doc["status"] == "caller_error"
        assert "no markdown files found" in doc["reason"]
        assert doc["findings"] == []
        assert doc["warning_count"] == 0 and doc["info_count"] == 0
        assert "no markdown files found" in captured.err

    @pytest.mark.parametrize("shape", ["missing", "file"])
    def test_a_docs_path_that_is_not_a_directory_is_named_as_such(
            self, tmp_path, monkeypatch, capsys, cli_argv, shape):
        """docs/ 不存在或是一般檔案時，理由要指名「not a directory」，不能
        說成「no markdown files found」把 operator 送去錯的修法。"""
        docs = tmp_path / "docs"
        if shape == "file":
            docs.write_text("not a dir\n", encoding="utf-8")
        self._point(monkeypatch, docs)
        cli_argv("check_bilingual_content")
        with pytest.raises(SystemExit) as exc_info:
            cbc.main()
        assert exc_info.value.code == cbc.EXIT_CALLER_ERROR
        err = capsys.readouterr().err
        assert "not a directory" in err
        assert "no markdown files found" not in err
