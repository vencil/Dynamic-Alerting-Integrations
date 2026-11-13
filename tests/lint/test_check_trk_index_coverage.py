"""被引用的 TRK ⊆ planning SSOT 索引 — 守衛測試 (#1627)。

⚠️ **票明寫要對照組**：只斷言「今天乾淨」的話，一個**掃描面歸零**的實作也會過。
所以每一格「綠」的斷言旁邊都有一格種了假引用必須轉紅的孿生格。

掃描面只剩 issue／PR 標題（``--title`` 單一標題、``--all-titles`` 全量）。舊的
commit 歷史掃描面已刪除，理由見工具 docstring；``test_commit_history_is_not_a_surface``
釘住它不會悄悄回來。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CHECKER = _REPO_ROOT / "scripts" / "tools" / "lint" / "check_trk_index_coverage.py"
_MAPPING_REL = Path("docs/internal/planning-id-mapping.md")

_HEADER = """### TRK-300+ — post-migration 新分配

| TRK | Issue | 主題 | Epic |
|---|---|---|---|
"""


def _env_without_token() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in ("GH_TOKEN", "GITHUB_TOKEN")}


def _run(repo: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_CHECKER), "--repo", str(repo), *extra],
        capture_output=True,
        text=True, encoding="utf-8", errors="replace",
        env=_env_without_token(),
        timeout=180,
    )


def _fixture(tmp_path: Path, table_rows: str) -> Path:
    (tmp_path / _MAPPING_REL).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / _MAPPING_REL).write_text(_HEADER + table_rows, encoding="utf-8")
    return tmp_path


def _json(repo: Path, *extra: str) -> dict:
    proc = _run(repo, "--json", *extra)
    assert proc.returncode in (0, 1), f"rc={proc.returncode}\n{proc.stderr}"
    return json.loads(proc.stdout)


def _missing(repo: Path, title: str) -> list[str]:
    return [m["trk"] for m in _json(repo, "--title", title)["missing"]]


# ---------------------------------------------------------------------------
# 生產樹
# ---------------------------------------------------------------------------
def test_repo_table_is_within_the_freeze() -> None:
    """本 repo 現況：表上沒有凍結之後才出現的列。"""
    proc = _run(_REPO_ROOT, "--ci")
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"


def test_repo_table_parse_is_not_vacuous() -> None:
    """⚠️ 反空轉下限：表上解析出的 TRK 數不得暴跌。

    ⛔ 本工具第一版**只讀表格第 1 欄**，於是漏掉 legacy 三段（`HA-N` / `REG-NNN`
    / `TD-NNN`，那三段的 TRK 在第 2 欄），跑出五個**假洞**。量測時（#1627 補列後）
    是 137 個；下限取 100，足以在只剩單一區段時立刻紅。
    """
    data = _json(_REPO_ROOT)
    assert data["defined"] >= 100, (
        f"表上只解析出 {data['defined']} 個 TRK（量測時 137）——"
        "比較像逐列拆欄壞了，而不是表真的變短。"
    )


def test_commit_history_is_not_a_surface(tmp_path: Path) -> None:
    """⛔ commit 歷史不是掃描面：git 裡有表上沒有的引用，只驗表時仍是綠。

    刪掉的理由（工具 docstring）：main 上的 squash 訊息會拆散 trailer block
    （`dev-rules.md` §P1／#1741），它看不到正在寫的 commit，而且違規一旦進了
    main 就無從修正。這一格擋的是「把 git log 掃描加回來」。
    """
    repo = _fixture(tmp_path, "| TRK-301 | #1 | x | — |\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, timeout=60)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q",
         "--allow-empty", "-m", "TRK-999: probe\n\nRefs: TRK-999"],
        cwd=repo, check=True, timeout=60,
    )
    data = _json(repo, "--ci")
    assert data["missing"] == [] and data["surfaces"] == [], data


# ---------------------------------------------------------------------------
# 述詞 + 對照組（以單一標題驅動）
# ---------------------------------------------------------------------------
def test_title_citing_a_defined_trk_is_green(tmp_path: Path) -> None:
    repo = _fixture(tmp_path, "| TRK-301 | #1 | x | — |\n")
    assert _missing(repo, "fix: 修 TRK-301 那件事") == []


def test_planted_reference_turns_it_red(tmp_path: Path) -> None:
    """⛔ 票明寫的對照組：種一個表上沒有的 TRK-999 引用，必須紅。"""
    repo = _fixture(tmp_path, "| TRK-301 | #1 | x | — |\n")
    assert _missing(repo, "chore: TRK-999 probe") == ["999"]
    assert _run(repo, "--ci", "--title", "chore: TRK-999 probe").returncode == 1


def test_trk_in_the_second_column_counts_as_defined(tmp_path: Path) -> None:
    """legacy 區段把 TRK 放在第 2 欄（`| TD-030 | TRK-230 | … |`）——那也算定義。

    這一格是第一版那五個假洞的直接控制項。
    """
    repo = _fixture(tmp_path, "| TD-030 | TRK-230 | x | — |\n")
    assert _missing(repo, "feat: TRK-230") == []


def test_trk_only_in_prose_does_not_count_as_defined(tmp_path: Path) -> None:
    """⛔ 散文裡提到某個 TRK 不等於它被登錄了——只認表格前兩欄的獨立儲存格。"""
    rows = "| TRK-301 | #1 | 與 TRK-777 有關，但 777 沒有自己的一列 | — |\n"
    repo = _fixture(tmp_path, rows)
    assert _missing(repo, "chore: TRK-777") == ["777"]


@pytest.mark.parametrize("token", ["TRK-9991", "xTRK-999", "TRK-99"])
def test_near_miss_tokens_are_not_matched(tmp_path: Path, token: str) -> None:
    r"""邊界錨定：`TRK-9991` / `xTRK-999` / `TRK-99` 都不得被讀成一次 TRK 引用。

    ⚠️ 近似 token 必須落在一個**表上沒有**的號碼上，才驗得到錨定：第一版用
    `TRK-4011` 對上表裡的 `TRK-401`，鬆掉錨定後它被讀成 `401` 而 `401` 在表上，
    兩種寫法都綠——mutation（拿掉 `(?<![\w-])…(?![\w-])`）實測沒有任何一格轉紅。
    """
    repo = _fixture(tmp_path, "| TRK-301 | #1 | x | — |\n")
    assert _missing(repo, f"chore: {token}") == [], token


# ---------------------------------------------------------------------------
# 「量不到」必須與「量了沒事」分開
# ---------------------------------------------------------------------------
def test_missing_mapping_file_is_rc2(tmp_path: Path) -> None:
    assert _run(tmp_path).returncode == 2


def test_empty_table_is_rc2_not_green(tmp_path: Path) -> None:
    """⛔ 表上解析出 0 個 TRK 一律 rc 2：解析壞掉與「表是空的」長得一樣。"""
    repo = _fixture(tmp_path, "")
    proc = _run(repo)
    assert proc.returncode == 2, proc.stdout
    assert "量不到" in proc.stderr


def test_all_titles_without_token_is_rc2(tmp_path: Path) -> None:
    """⛔ 要求了全量標題卻沒有 token，必須 rc 2 —— 不能當成掃過了。"""
    repo = _fixture(tmp_path, "| TRK-301 | #1 | x | — |\n")
    proc = _run(repo, "--all-titles")
    assert proc.returncode == 2, proc.stdout
    assert "量不到" in proc.stderr


def test_title_and_all_titles_are_exclusive(tmp_path: Path) -> None:
    """兩種標題模式同時給是呼叫端錯誤（argparse rc 2），不能默默只跑其中一個。"""
    repo = _fixture(tmp_path, "| TRK-301 | #1 | x | — |\n")
    assert _run(repo, "--all-titles", "--title", "x").returncode == 2


def test_owner_repo_slug_is_validated() -> None:
    """⛔ argv 給的 owner/repo 不得把請求帶去 api.github.com 以外的路徑。

    ⚠️ bandit 的 B310（`urllib.urlopen`）在 `-ll -ii` 下是硬性 gate。抑制它用的是
    inline `# nosec B310`，而那只有在**述詞真的成立**時才誠實 —— 這一格就是那個
    述詞的控制項：slug 收斂 + host 常數前綴斷言。
    """
    mod = _load_module()
    # ⛔ 每一個都必須在**發出請求之前**被擋下。⚠️ `.` / `..` 原本通過字元集檢查，
    # 那一格真的打到 api.github.com（收 403）才暴露出來——字元集不等於路徑安全。
    bad_slugs = ("evil.com/x?", "a/b", ".", "..", "...", "own er", "", "a\\b", "%2e%2e")
    for bad in bad_slugs:
        with pytest.raises(ValueError):
            mod.title_trks(bad, "repo", "token")
        with pytest.raises(ValueError):
            mod.title_trks("owner", bad, "token")


# ---------------------------------------------------------------------------
# #2106：titles 面翻頁上限
# ---------------------------------------------------------------------------
def _load_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("_trk_mod_2106", _CHECKER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeResp:
    def __init__(self, payload: list, next_url: str = "") -> None:
        self._body = json.dumps(payload).encode("utf-8")
        self.headers = {"Link": f'<{next_url}>; rel="next"'} if next_url else {}

    def read(self, *a):
        body, self._body = self._body, b""
        return body

    def __enter__(self):
        return self

    def __exit__(self, *a) -> None:
        return None


@pytest.mark.parametrize("pages_with_next, expect_rc", [
    (None, 2),  # 永遠還有 rel="next"：撞到上限，沒看過的標題不能當成掃過 ⇒ rc 2
    (2, 1),     # 對照：第 3 頁沒有 rel="next" ⇒ 正常掃完，TRK-777 不在表上 ⇒ rc 1
])
def test_titles_follow_link_next_and_cap_is_rc2(
        tmp_path: Path, monkeypatch, capsys, pages_with_next, expect_rc) -> None:
    """⛔ 分頁跟 `Link: rel="next"` 走；頁碼式分頁在大資料集會被拒（422，#2106）。

    ⚠️ 空頁**不是**終止條件：fake 的每一頁都非空，只有 Link 決定要不要翻下一頁。
    """
    mod = _load_module()
    repo = _fixture(tmp_path, "| TRK-301 | #1 | x | — |\n")
    seen: list[str] = []

    def fake_urlopen(req, timeout=0):
        seen.append(req.full_url)
        n = len(seen)
        more = pages_with_next is None or n <= pages_with_next
        nxt = f"https://api.github.com/repositories/1/issues?after=c{n}" if more else ""
        return _FakeResp([{"number": n, "title": "TRK-777: x"}], nxt)

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("GH_TOKEN", "t")
    rc = mod.main(["--repo", str(repo), "--all-titles", "--ci"])
    assert rc == expect_rc, capsys.readouterr()
    if expect_rc == 2:
        assert len(seen) == mod._MAX_PAGES
    else:
        # 第 2、3 頁的 URL 必須是上一頁 Link 給的那一個，不是自己拼的 page=N
        assert seen[1:] == ["https://api.github.com/repositories/1/issues?after=c1",
                            "https://api.github.com/repositories/1/issues?after=c2"]


@pytest.mark.parametrize("title, expect_rc", [
    ("feat: TRK-393 照舊手配的新號", 1),  # 表上沒有的三位數新號 ⇒ 紅
    ("fix: 修 TRK-301 那件事", 0),         # 對照：引用表上既有的號 ⇒ 綠
])
def test_single_title_mode_needs_no_token(tmp_path: Path, title: str, expect_rc: int) -> None:
    """workflow 對 issue／PR 事件只驗觸發它的那一個標題（event payload），不打 API。"""
    repo = _fixture(tmp_path, "| TRK-301 | #1 | x | — |\n")
    proc = _run(repo, "--ci", "--title", title, "--title-ref", "#9999")
    assert proc.returncode == expect_rc, proc.stdout + proc.stderr


@pytest.mark.parametrize("rows, expect_rc", [
    ("| TRK-392 | #1 | x | — |\n| TRK-393 | #2 | x | — |\n", 1),   # 凍結之後又加一列 ⇒ 紅
    ("| TRK-392 | #1 | x | — |\n| TRK-150 | #2 | x | — |\n", 1),   # 補 legacy 區段的洞 ⇒ 紅
    ("| TRK-392 | #1 | x | — |\n| TRK-2150 | #2 | x | — |\n", 1),  # 四位數的列 ⇒ 紅
    ("| TRK-392 | #1 | x | — |\n| TRK-101 | #2 | x | — |\n", 0),   # 對照：凍結時就有的號 ⇒ 綠
])
def test_rows_past_the_freeze_are_red(tmp_path: Path, rows: str, expect_rc: int) -> None:
    """⛔ 表凍結在 TRK-392 要有機制：照舊取號的人會順手補列，讓自己的引用「在表上」。"""
    repo = _fixture(tmp_path, rows)
    assert _run(repo, "--ci").returncode == expect_rc


@pytest.mark.parametrize("owner, expect_rc", [
    (".", 2),  # 不合法的 slug：呼叫端錯誤＝量不到，不是「有違規」
])
def test_titles_scan_errors_are_rc2_not_violations(
        tmp_path: Path, monkeypatch, capsys, owner: str, expect_rc: int) -> None:
    """⛔ titles 面丟出的 ValueError（slug、Link host、壞 JSON）一律 rc 2。"""
    mod = _load_module()
    repo = _fixture(tmp_path, "| TRK-301 | #1 | x | — |\n")
    monkeypatch.setenv("GH_TOKEN", "t")
    rc = mod.main(["--repo", str(repo), "--all-titles", "--ci", "--owner", owner])
    assert rc == expect_rc, capsys.readouterr()
    assert "量不到" in capsys.readouterr().err
