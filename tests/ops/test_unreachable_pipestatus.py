"""``set -e`` + ``pipefail`` 之下不可達的 ``PIPESTATUS`` 讀取 — 類別級守衛 (TRK-381 / #1771)。

這個形狀在本 repo 出現過兩次（TRK-376 / #1734 的 workflow、TRK-381 / #1771 的
``bench_wrapper.sh``），兩次都是「守衛看起來存在、實際上從來沒有守過」。修掉第二
個實例本身不會擋住第三個，所以這裡放的是**述詞**而不是那兩處的字面比對。

⛔ 本檔不比對歷史 bytes。守衛對兩個**真實歷史實例**的偵測力已在 PR 中以實測記錄
（pre-fix ``bench_wrapper.sh`` sha256 ``c563c21f…`` → 命中 line 104；pre-#1734
``bench-probe-write-latency.yaml`` sha256 ``2e106c13…`` → 命中 line 131）。把那些
bytes 複製進測試會多出一個會漂的載體，而 CI 的 shallow clone 也不保證拿得到。

⚠️ **反空轉下限**：``test_population_is_not_vacuous`` 釘住母體不得為空也不得暴跌。
沒有它，一個掃不到任何檔案的壞掉 glob 會讓其餘每一條斷言**空過**。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CHECKER = _REPO_ROOT / "scripts" / "tools" / "lint" / "check_unreachable_pipestatus.py"


def _run(repo: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_CHECKER), "--repo", str(repo), *extra],
        capture_output=True,
        text=True,
        timeout=120,
    )


def _findings(repo: Path) -> dict:
    proc = _run(repo, "--json")
    assert proc.returncode in (0, 1), f"checker failed to run: rc={proc.returncode}\n{proc.stderr}"
    return json.loads(proc.stdout)


def _git_fixture(tmp_path: Path, files: dict[str, str]) -> Path:
    """建一個最小 git repo，因為母體是用 ``git ls-files`` 枚舉的。"""
    for name, body in files.items():
        fp = tmp_path / name
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(body, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, timeout=60)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True, timeout=60)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "fixture"],
        cwd=tmp_path,
        check=True,
        timeout=60,
    )
    return tmp_path


# ---------------------------------------------------------------------------
# 生產樹
# ---------------------------------------------------------------------------
def test_repo_has_no_unreachable_pipestatus_reads() -> None:
    """整個 repo 現況必須乾淨。"""
    proc = _run(_REPO_ROOT, "--ci")
    assert proc.returncode == 0, (
        "發現 set -e + pipefail 之下不可達的 PIPESTATUS 讀取：\n"
        f"{proc.stdout}\n{proc.stderr}"
    )


def test_population_is_not_vacuous() -> None:
    """⚠️ 反空轉下限：母體不得為空、也不得暴跌到不合理的低點。

    量測時（TRK-381）母體是 264 個 shell 單元（64 scripts / 200 workflow run
    blocks）。下限取 100 —— 遠低於現況、又足以在 glob 壞掉時立刻紅。
    """
    data = _findings(_REPO_ROOT)
    assert data["units"] >= 100, (
        f"母體只剩 {data['units']} 個 shell 單元（量測時 264）。"
        "這比較像枚舉壞了，而不是檔案真的變少 —— 工具失能長得就像零命中。"
    )


def test_empty_population_is_loud_not_green(tmp_path: Path) -> None:
    """⛔ 空母體必須 rc 2（量不到），絕不能回 0（量了沒事）。"""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, timeout=60)
    proc = _run(tmp_path)
    assert proc.returncode == 2, f"空母體回了 rc={proc.returncode}，應為 2\n{proc.stdout}"
    assert "量不到" in proc.stderr


def test_non_git_dir_is_loud_not_green(tmp_path: Path) -> None:
    """不是 git repo 也必須是 rc 2。"""
    proc = _run(tmp_path)
    assert proc.returncode == 2


# ---------------------------------------------------------------------------
# 述詞：命中什麼、以及**不**命中什麼
# ---------------------------------------------------------------------------
_VIOLATING = """#!/usr/bin/env bash
set -euo pipefail
foo | bar | baz
RC="${PIPESTATUS[0]}"
[ "$RC" -eq 0 ] || { echo "never printed"; exit 1; }
"""


def test_detects_the_violating_shape(tmp_path: Path) -> None:
    repo = _git_fixture(tmp_path, {"violating.sh": _VIOLATING})
    data = _findings(repo)
    assert len(data["findings"]) == 1, data
    assert data["findings"][0]["line"] == 4


def test_ci_flag_returns_1_on_violation(tmp_path: Path) -> None:
    repo = _git_fixture(tmp_path, {"violating.sh": _VIOLATING})
    assert _run(repo, "--ci").returncode == 1


@pytest.mark.parametrize(
    "name,body",
    [
        # errexit 被關掉 ⇒ 讀得到（run_hooks_sandbox.sh:101 的真實形狀）
        (
            "set_plus_e.sh",
            '#!/usr/bin/env bash\nset -euo pipefail\nset +e\nfoo | bar | tee log\n'
            'RC="${PIPESTATUS[0]}"\nset -e\n[ "$RC" -eq 0 ] || exit 1\n',
        ),
        # 沒有 pipefail ⇒ 管線 rc 是最後一個指令的，守衛會醒（而且是有用的後備）
        (
            "no_pipefail.sh",
            '#!/usr/bin/env bash\nset -eu\nfoo | bar\nRC="${PIPESTATUS[0]}"\n'
            '[ "$RC" -eq 0 ] || exit 1\n',
        ),
        # 管線在 if 條件裡 ⇒ errexit 不觸發
        (
            "if_guarded.sh",
            '#!/usr/bin/env bash\nset -euo pipefail\nif ! foo | bar; then\n'
            '  RC="${PIPESTATUS[0]}"\n  echo "rc=$RC"\nfi\n',
        ),
        # 只出現在註解裡（本票修好之後 bench_wrapper.sh 的形狀）
        (
            "comment_only.sh",
            '#!/usr/bin/env bash\nset -euo pipefail\n'
            '# 不要在這裡讀 ${PIPESTATUS[0]} — 它不可達\nfoo | bar\n',
        ),
        # 前一個語句不是管線 ⇒ 述詞保守，不判違規
        (
            "not_a_pipeline.sh",
            '#!/usr/bin/env bash\nset -euo pipefail\nfoo\nRC="${PIPESTATUS[0]}"\n',
        ),
    ],
)
def test_legal_shapes_stay_green(tmp_path: Path, name: str, body: str) -> None:
    """⚠️ 誤紅是守衛被刪掉的原因（D-05e）—— 這些合法寫法必須維持綠。"""
    repo = _git_fixture(tmp_path, {name: body})
    data = _findings(repo)
    assert data["findings"] == [], f"{name} 被誤判為違規：{data['findings']}"


def test_workflow_run_block_default_shell_has_no_pipefail(tmp_path: Path) -> None:
    """GitHub 預設 shell 是 ``bash -e {0}`` —— errexit 開、pipefail **關**。

    所以預設 ``run:`` 區塊裡的 PIPESTATUS 讀取是合法的（weekly-fuzz.yaml 的形狀）。
    """
    wf = (
        "on: push\njobs:\n  j:\n    runs-on: ubuntu-latest\n    steps:\n"
        "      - name: s\n        run: |\n          foo | bar\n"
        '          rc="${PIPESTATUS[0]}"\n          echo "$rc"\n'
    )
    repo = _git_fixture(tmp_path, {".github/workflows/w.yml": wf})
    assert _findings(repo)["findings"] == []


def test_workflow_run_block_with_shell_bash_gets_pipefail(tmp_path: Path) -> None:
    """寫了 ``shell: bash`` 就是 ``bash -eo pipefail`` ⇒ 同一段變成不可達。"""
    wf = (
        "on: push\njobs:\n  j:\n    runs-on: ubuntu-latest\n    steps:\n"
        "      - name: s\n        shell: bash\n        run: |\n          foo | bar\n"
        '          rc="${PIPESTATUS[0]}"\n          echo "$rc"\n'
    )
    repo = _git_fixture(tmp_path, {".github/workflows/w.yml": wf})
    findings = _findings(repo)["findings"]
    assert len(findings) == 1, findings
