#!/usr/bin/env python3
"""generate_nav.py 的最小行為守衛。

⛔ 這支檔案**不是** #1884 要的那組測試。#1884 要的是 nav 比對結果、front matter
解析與 section 分類的內容驗證；這裡只釘住兩件在刪 `--update` 那一役實際量過的事：

  1. `--update` 已不存在。它曾經被宣告、被 `--help` 與三處散文宣傳，卻從來沒有實作
     （`args.update` 從未被讀取）——跑它與不帶旗標完全同義，是一個會說謊的介面。
  2. `--check` 的兩個方向。⚠️ 沒有這一半，第 1 格會是平凡為真：一支整個壞掉、對任何
     argv 都回 rc 2 的實作，照樣能讓「`--update` 被拒絕」通過。

  3. `--check` 對 `extra`（nav 列了、檔案不在）回 rc 0。⚠️ 這一格是**現況存證，不是
     規格**：這一役的 docstring 把這個行為寫進散文，寫下而不守就是下一句會腐爛的散文；
     而 #1884 認定它是缺口。⇒ 釘住是為了「有人改動它時會有東西喊」。讀到它紅不要當成
     回歸，去看 #1884 是不是把 `extra` 改成觸發 rc 1 了。
"""

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL = REPO_ROOT / "scripts" / "tools" / "dx" / "generate_nav.py"

EXIT_OK = 0
EXIT_VIOLATION = 1
EXIT_CALLER_ERROR = 2


def _repo(tmp_path: Path, *, nav_lists_the_doc: bool) -> Path:
    """合成一個最小 repo root：一份帶 front matter 的文件 + 一份 mkdocs.yml。"""
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "orphan.md").write_text(
        '---\ntitle: "Orphan"\ntags: [dx]\nlang: zh\n---\n# Orphan\n', encoding="utf-8")
    nav = "  - Orphan: docs/orphan.md\n" if nav_lists_the_doc else "  - Home: index.md\n"
    (tmp_path / "mkdocs.yml").write_text(f"site_name: t\nnav:\n{nav}", encoding="utf-8")
    return tmp_path


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(TOOL), "--repo-root", str(repo), *args],
        capture_output=True, text=True, timeout=60,
    )


@pytest.mark.parametrize("listed, expected", [
    (False, EXIT_VIOLATION),   # 文件在、nav 沒列 ⇒ 違規
    (True, EXIT_OK),           # nav 列了 ⇒ 乾淨
])
def test_check_reports_both_directions(tmp_path, listed, expected):
    """`--check` 兩個方向都要動，否則「永遠回 0」與「永遠回 1」都能過。"""
    repo = _repo(tmp_path, nav_lists_the_doc=listed)
    proc = _run(repo, "--check")
    assert proc.returncode == expected, (
        f"--check 對 nav_lists_the_doc={listed} 回 rc={proc.returncode}，"
        f"期待 {expected}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )


def test_the_dead_update_flag_is_gone(tmp_path):
    """⛔ `--update` 必須被拒絕，而不是被靜默忽略。

    刪除前的實測：帶 `--update` 跑完 rc 0，而 mkdocs.yml 的 sha256 前後相同——它什麼
    也沒做，卻讓呼叫者以為 nav 已經被寫回。⇒ 這一格守的是「不留說謊的介面」，不是
    「永遠不准有 --update」：真要實作寫回，連同這一格一起改。
    """
    repo = _repo(tmp_path, nav_lists_the_doc=True)
    proc = _run(repo, "--update")
    assert proc.returncode == EXIT_CALLER_ERROR, (
        f"--update 應被 argparse 拒絕（rc {EXIT_CALLER_ERROR}），實得 rc={proc.returncode}。"
        f"若它又被加回來，請確認 args.update 真的有被讀取。\nstderr:\n{proc.stderr}"
    )
    assert "--update" in proc.stderr, "argparse 的拒絕訊息應指名這個未知旗標"


def test_check_does_not_fail_on_extra_entries_today(tmp_path):
    """現況存證：nav 列了、檔案不在（`extra`）時 `--check` 仍回 rc 0，只把它印出來。

    ⚠️ 不是在主張「應該如此」。實測（`--repo-root` 合成 repo，nav 列 real.md + ghost.md）：
    rc 0，stdout 有 `In nav but not found: 1`。沒有這一格，第一格的 `missing` 斷言
    即使在「對任何差異都回 rc 1」的實作下也照樣全綠。缺口本身記在 #1884。
    """
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "real.md").write_text(
        '---\ntitle: "Real"\ntags: [dx]\nlang: zh\n---\n# Real\n', encoding="utf-8")
    # real.md 有被列 ⇒ 沒有 missing；ghost.md 被列但檔案不存在 ⇒ 只有 extra
    (tmp_path / "mkdocs.yml").write_text(
        "site_name: t\nnav:\n  - Real: docs/real.md\n  - Ghost: docs/ghost.md\n",
        encoding="utf-8")
    proc = _run(tmp_path, "--check")
    assert proc.returncode == EXIT_OK, (
        f"`extra` 不該觸發 rc 1（現況）。實得 rc={proc.returncode}。若 #1884 已把 extra "
        f"改成違規，請改這一格而不是刪它。\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    assert "ghost.md" in proc.stdout, (
        "rc 0 還不夠：它必須真的看到那筆 extra 並印出來，否則一支完全忽略 nav 的實作也會綠。"
        f"\nstdout:\n{proc.stdout}"
    )
