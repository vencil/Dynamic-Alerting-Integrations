"""被引用的 TRK ⊆ planning SSOT 索引 — 守衛測試 (#1627)。

⚠️ **票明寫要對照組**：只斷言「今天乾淨」的話，一個**掃描面歸零**的實作也會過。
所以每一格「綠」的斷言旁邊都有一格種了假引用必須轉紅的孿生格。

⛔ 兩個掃描面不可互相取代，這是實測不是設計理由。⚠️ **而且 commits 面比一開始
以為的弱得多**：掃描面收窄成 subject + trailer 之後（見下），在本 repo 的 shallow
clone 上把六列拿掉重量，**commits 面 0/6、回綠**，六個全部只由 titles 面看得到。
先前記的「commits 面 4 個」是**掃整篇 commit body** 的數字，那個掃法已因誤紅
（它擋下了本功能自己的落地 commit——訊息裡當例子寫的 `TRK-999`／`TRK-401` 被讀成
引用）而改掉。⇒ 只跑 commits 面的 pre-commit hook 是弱後備，不是偵測器。
"""
from __future__ import annotations

import json
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


def _run(repo: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_CHECKER), "--repo", str(repo), *extra],
        capture_output=True,
        text=True,
        timeout=180,
    )


def _fixture(tmp_path: Path, table_rows: str, commit_bodies: list[str]) -> Path:
    (tmp_path / _MAPPING_REL).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / _MAPPING_REL).write_text(_HEADER + table_rows, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, timeout=60)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True, timeout=60)
    for i, body in enumerate(commit_bodies):
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-q", "--allow-empty", "-m", body],
            cwd=tmp_path, check=True, timeout=60,
        )
    return tmp_path


def _json(repo: Path) -> dict:
    proc = _run(repo, "--json")
    assert proc.returncode in (0, 1), f"rc={proc.returncode}\n{proc.stderr}"
    return json.loads(proc.stdout)


# ---------------------------------------------------------------------------
# 生產樹
# ---------------------------------------------------------------------------
def test_repo_index_covers_every_referenced_trk() -> None:
    """本 repo 現況：commit 面引用的 TRK 都要在表上。"""
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


# ---------------------------------------------------------------------------
# 述詞 + 對照組
# ---------------------------------------------------------------------------
def test_clean_fixture_is_green(tmp_path: Path) -> None:
    repo = _fixture(tmp_path, "| TRK-401 | #1 | x | — |\n", ["feat: a\n\nRefs: TRK-401"])
    assert _json(repo)["missing"] == []


def test_planted_reference_turns_it_red(tmp_path: Path) -> None:
    """⛔ 票明寫的對照組：種一個表上沒有的 TRK-999 引用，必須紅。"""
    repo = _fixture(tmp_path, "| TRK-401 | #1 | x | — |\n",
                    ["feat: a\n\nRefs: TRK-401", "chore: probe\n\nRefs: TRK-999"])
    data = _json(repo)
    assert [m["trk"] for m in data["missing"]] == ["999"], data
    assert _run(repo, "--ci").returncode == 1


def test_trk_in_the_second_column_counts_as_defined(tmp_path: Path) -> None:
    """legacy 區段把 TRK 放在第 2 欄（`| TD-030 | TRK-230 | … |`）——那也算定義。

    這一格是第一版那五個假洞的直接控制項。
    """
    repo = _fixture(tmp_path, "| TD-030 | TRK-230 | x | — |\n", ["feat: a\n\nRefs: TRK-230"])
    assert _json(repo)["missing"] == []


def test_trk_only_in_prose_does_not_count_as_defined(tmp_path: Path) -> None:
    """⛔ 散文裡提到某個 TRK 不等於它被登錄了——只認表格前兩欄的獨立儲存格。"""
    rows = "| TRK-401 | #1 | 與 TRK-777 有關，但 777 沒有自己的一列 | — |\n"
    repo = _fixture(tmp_path, rows, ["chore: x\n\nRefs: TRK-777"])
    assert [m["trk"] for m in _json(repo)["missing"]] == ["777"]


@pytest.mark.parametrize("token", ["TRK-9991", "xTRK-999", "TRK-99"])
def test_near_miss_tokens_are_not_matched(tmp_path: Path, token: str) -> None:
    """邊界錨定：`TRK-9991` / `xTRK-999` / `TRK-99` 都不得被讀成一次 TRK 引用。

    ⚠️ **這一格原本是空過的，在此更正**：第一版用 `TRK-4011` 對上表裡的
    `TRK-401`——鬆掉錨定後它被讀成 `401`，而 `401` **在表上**，於是
    `missing == []` 在正確與錯誤兩種寫法下都成立。mutation（把
    `(?<![\w-])…(?![\w-])` 拿掉）實測**沒有讓任何一格轉紅**。
    近似 token 必須落在一個**表上沒有**的號碼上，才驗得到錨定。
    """
    repo = _fixture(tmp_path, "| TRK-401 | #1 | x | — |\n", [f"chore: x\n\nRefs: {token}"])
    assert _json(repo)["missing"] == [], token


# ---------------------------------------------------------------------------
# 「量不到」必須與「量了沒事」分開
# ---------------------------------------------------------------------------
def test_missing_mapping_file_is_rc2(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, timeout=60)
    assert _run(tmp_path).returncode == 2


def test_empty_table_is_rc2_not_green(tmp_path: Path) -> None:
    """⛔ 表上解析出 0 個 TRK 一律 rc 2：解析壞掉與「表是空的」長得一樣。"""
    repo = _fixture(tmp_path, "", ["chore: x"])
    proc = _run(repo)
    assert proc.returncode == 2, proc.stdout
    assert "量不到" in proc.stderr


def test_titles_surface_without_token_is_rc2(tmp_path: Path, monkeypatch) -> None:
    """⛔ 要求了 titles 面卻沒有 token，必須 rc 2 —— 不能當成掃過了。"""
    repo = _fixture(tmp_path, "| TRK-401 | #1 | x | — |\n", ["chore: x"])
    env = {k: v for k, v in __import__("os").environ.items()
           if k not in ("GH_TOKEN", "GITHUB_TOKEN")}
    proc = subprocess.run(
        [sys.executable, str(_CHECKER), "--repo", str(repo), "--surface", "titles"],
        capture_output=True, text=True, env=env, timeout=120,
    )
    assert proc.returncode == 2, proc.stdout
    assert "量不到" in proc.stderr


def test_shallow_clone_is_reported_as_partial(tmp_path: Path) -> None:
    """shallow clone 下「沒找到」不等於「沒有」，報告必須說出來。"""
    repo = _fixture(tmp_path, "| TRK-401 | #1 | x | — |\n", ["chore: x"])
    (repo / ".git" / "shallow").write_text("deadbeef\n", encoding="utf-8")
    data = _json(repo)
    assert data["partial"] is True
    assert "PARTIAL" in _run(repo).stdout


# ---------------------------------------------------------------------------
# 掃描面：subject + trailer，不是整篇 body
# ---------------------------------------------------------------------------
def test_trk_mentioned_only_in_commit_body_prose_is_not_a_reference(tmp_path: Path) -> None:
    """⛔ commit 散文裡當例子提到的號碼**不是**一次引用。

    ⚠️ 這一格是本功能自己燒出來的：第一版掃整篇 commit body，於是它的落地 commit
    ——訊息裡寫著「dogfood 種一個 TRK-999 引用」——被判成一個洞，**pre-commit 擋下
    了那次 commit**。票寫的掃描面是「commit trailer」，不是整篇散文。
    """
    body = ("chore: 說明用的 commit\n\n"
            "這段散文提到 TRK-999 與 TRK-888 只是舉例，不是引用。\n\n"
            "Refs: TRK-401\n")
    repo = _fixture(tmp_path, "| TRK-401 | #1 | x | — |\n", [body])
    assert _json(repo)["missing"] == []


def test_trk_in_a_trailer_is_a_reference(tmp_path: Path) -> None:
    """同一顆 commit，號碼改放 trailer 就必須算引用。"""
    body = "chore: x\n\n散文完全不提號碼。\n\nRefs: TRK-777\n"
    repo = _fixture(tmp_path, "| TRK-401 | #1 | x | — |\n", [body])
    assert [m["trk"] for m in _json(repo)["missing"]] == ["777"]


def test_trk_in_the_subject_line_is_a_reference(tmp_path: Path) -> None:
    """subject 行等同 issue/PR 標題那一面，也算引用。"""
    repo = _fixture(tmp_path, "| TRK-401 | #1 | x | — |\n", ["TRK-555: 做了某件事"])
    assert [m["trk"] for m in _json(repo)["missing"]] == ["555"]


@pytest.mark.parametrize("key", ["Refs", "Resolves", "Closes", "Fixes"])
def test_every_declared_trailer_key_is_scanned(tmp_path: Path, key: str) -> None:
    """宣告的 trailer key 每一個都要真的被掃到——否則就是列了沒接。"""
    repo = _fixture(tmp_path, "| TRK-401 | #1 | x | — |\n",
                    [f"chore: x\n\n{key}: TRK-777\n"])
    assert [m["trk"] for m in _json(repo)["missing"]] == ["777"], key
