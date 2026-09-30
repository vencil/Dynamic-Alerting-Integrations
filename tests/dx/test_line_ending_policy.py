#!/usr/bin/env python3
"""test_line_ending_policy.py — regen / doc 工具的行尾政策守衛。

為什麼存在
==========
`bump_docs.py --sync-counts` 只改一個數字，卻在 Windows host 上把整個
`CLAUDE.md` 改寫成 CRLF（實測 120 CRLF / 0 bare LF）。根因是
`Path.write_text(..., encoding="utf-8")` 沒有 pin `newline=` — Python 的
text layer 會把寫出的每個 `\\n` 轉成 `os.linesep`。

這個 bug 能長期存活，是因為三層遮蔽同時生效：

1. **讀取端**走 universal-newline，CRLF 被正規化回 `\\n`
   ⇒ 工具自己的 `--check` 比對不出差異。
2. **`.gitattributes`**（`* text=auto eol=lf`）在 commit 時正規化
   ⇒ staged diff 仍然只有正確的那一行，PR 看起來完全正常。
3. **CI 跑 Linux**，`os.linesep == "\\n"` ⇒ 產物本來就是 LF。

壞掉的只有工作目錄的 bytes：它與樹裡其他檔案不一致，並讓之後每次 `git diff`
都噴 "CRLF will be replaced by LF" 警告（實測會出現）。

⚠️ 早期版本還宣稱這會「打壞斷言檔案被還原成 byte-identical 的 mutation
harness」——**那個危害在本 repo 不存在**：`tests/shared/_mutation_pilot.py`
讀寫兩端都用 `newline=""`，還原是逐位元組的。已刪除該理由，不要再寫回去。
真正的危害是上面那兩條，加上兩個**實測會壞**的具體站點（寫進 commit
object 的 message、以及 git hook 的 shebang 行）。

⛔ 第 3 點決定了閘門的形狀
==========================
CPython 的 CRLF 轉換寫死在 `TextIOWrapper` 的**編譯期 `#ifdef MS_WINDOWS`**，
不是執行期查 `os.linesep`。實測：在 Windows 上 monkeypatch
`os.linesep = "\\n"` 之後，`write_text` **照樣**寫出 `b"x\\r\\ny\\r\\n"`。

⇒ 在 Linux CI 上，**有 bug 的版本不可能產生 CRLF**。任何「跑一次 sync path、
斷言輸出 bytes 沒有 `\\r\\n`」的行為測試，在 CI 裡都是**恆綠**的。真正的閘門
只能是 static AST 規則。

閘門在哪（#1366 / TRK-358 起）
=============================
static 規則**本身**住在 pre-commit hook `open-encoding-audit`
（`scripts/tools/lint/check_open_encoding.py` 的 line-ending 規則，
`--strict-line-ending` ⇒ commit 當下擋）。偵測邏輯只有那一份；本檔不再
自帶第二份判定器。本檔留下的是 hook 給不了的東西：

* `TestGuardDetectsKnownViolations` — 正反例樣本對 **hook 的實作**跑，證明
  偵測器活著。⛔ 不可刪：沒有它，偵測器可以整個回傳空 list 而全綠。
* `TestHookScope` — 讀 `.pre-commit-config.yaml` 的 hook `entry`，釘住
  「範圍不得被無聲收窄、行尾規則必須是 FATAL」；並解析 `ci.yml`，釘住 CI
  Lint job 逐名跑 `pre-commit run open-encoding-audit --all-files`。那一行是
  這條規則全樹掃描在 CI 的**唯一**執行點（#2539 起；之前是本檔自帶的一支
  全樹掃描測試，已移除，不要加回第二份）。
* `TestSharedWriteHelpersPinLF` — 共用 helper 內部帶 ignore marker，static
  規則對它是盲的；這層直接驗 bytes。
* `TestSyncCountsEmitsLF` — 端到端行為測試，**只在 Windows host 上具鑑別力**。

⚠️ 如果你打算精簡這個檔案：拿掉 `TestHookScope` 的 CI 接線斷言、只留行為
測試，Lint job 那一行就能被無聲刪掉，等於在 CI 裡留下一個永遠不會紅的守衛。
要動之前請先讀懂上面這段。
"""
from __future__ import annotations

import inspect
import re
import shlex
import sys
from pathlib import Path

import pytest
import yaml

import bump_docs  # noqa: E402  (sys.path wired by tests/conftest.py)
import check_open_encoding as hook  # noqa: E402  (scripts/tools/lint on sys.path)

REPO_ROOT = Path(bump_docs.__file__).resolve().parent.parent.parent.parent
HOOK_ID = "open-encoding-audit"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

# The shared workflow loader and `run:` line splitter. ⛔ Imported, not
# reimplemented: the splitter already knows that a `#` comment is not continued
# by a trailing backslash, which a second copy would have to relearn.
sys.path.insert(0, str(REPO_ROOT / "tests" / "ops"))
from test_ci_path_filter_coverage import (  # noqa: E402
    _load_workflow,
    _logical_shell_lines,
)

# ⛔ 獨立於 hook `entry` 的硬編清單，用來釘住「範圍不得被無聲收窄」。
# 必須寫死：拿 entry 自己去檢查自己，等於範圍縮小、斷言也跟著縮小。
# `scripts/dx` 明確列入 —— `tests/dx/test_generate_adr_index.py` 記載 PR #477
# 的 CRLF 回歸就出在那兩支 generator，是這條規則的起源現場。
#
# 範圍＝**所有會出貨或在生產跑的 Python**：`scripts/`（工具與 hook）、
# `components/`（打包進映像的 CLI）、`helm/`（init-container 內跑的腳本）。
# ⛔ 刻意**不含 `tests/`**（#1363 時實測 1348 個 site）。理由是它們寫的是 tmp
# fixture、不是出貨產物，行尾不影響任何消費者。這是刻意的邊界、不是漏掉。
REQUIRED_SUBTREES = (
    "scripts/dx",
    "scripts/ops",
    "scripts/session-guards",
    "scripts/tools",
    "scripts/tools/dx",
    "scripts/tools/lint",
    "scripts/tools/ops",
    "components",
    "helm",
)

IGNORE_MARKER = hook.LINE_ENDING_IGNORE_MARKER


def _violations_in(path: Path) -> list[tuple[int, str]]:
    """The hook's own line-ending scanner — the one implementation."""
    return hook.scan_line_endings(path)


def _hook_config() -> dict:
    with open(REPO_ROOT / ".pre-commit-config.yaml", encoding="utf-8") as fh:
        config = yaml.safe_load(fh)
    for repo in config["repos"]:
        for h in repo.get("hooks", []):
            if h["id"] == HOOK_ID:
                return h
    raise AssertionError(f"hook {HOOK_ID!r} is gone from .pre-commit-config.yaml")


def _entry_line_ending_roots(entry: str) -> list[Path]:
    argv = shlex.split(entry)
    return [REPO_ROOT / argv[i + 1] for i, a in enumerate(argv[:-1])
            if a == "--line-ending-root"]


def _governed_files() -> list[Path]:
    return hook.collect_files(_entry_line_ending_roots(_hook_config()["entry"]))


# ⛔ 反例樣本 —— 這一組的存在本身就是閘門的一部分。
#
# 第一版沒有這組樣本，後果是：把偵測器第一行改成 `return []`（當時是本檔的
# `_violations_in()`，#1366 起是 hook 的 `scan_line_endings()`），
# 250 個 case **全部維持綠**。因為受管的樹是乾淨的，parametrize 那層只走綠
# 方向，從來沒有任何一個已知違規證明偵測器還活著——偵測器可以整個被掏空而
# 沒有任何測試察覺。反例與正例必須成對。
#
# 每一條的 bytes 都在 Windows host 上實測過會產生 CRLF。
VIOLATING_SAMPLES = {
    "builtin_open_w": 'open(p, "w", encoding="utf-8").write(s)',
    "builtin_open_a": 'open(p, "a", encoding="utf-8").write(s)',
    "builtin_open_rplus": 'open(p, "r+", encoding="utf-8").write(s)',
    "path_open_w": 'p.open("w", encoding="utf-8").write(s)',
    "path_open_a": 'p.open("a", encoding="utf-8").write(s)',
    "io_open_w": 'io.open(p, "w", encoding="utf-8").write(s)',
    "os_fdopen_w": 'os.fdopen(fd, "w", encoding="utf-8").write(s)',
    "named_tmp_positional": 'tempfile.NamedTemporaryFile("w", suffix=".msg")',
    "named_tmp_kwarg": 'tempfile.NamedTemporaryFile(mode="w", suffix=".json")',
    "gzip_open_wt": 'gzip.open(p, "wt", encoding="utf-8").write(s)',
    "write_text_bare": 'p.write_text(s, encoding="utf-8")',
    "write_text_none": 'p.write_text(s, encoding="utf-8", newline=None)',
    "open_newline_none": 'open(p, "w", encoding="utf-8", newline=None)',
    # 這兩條是「看起來合規、實際照樣 CRLF」——最容易被寫出來的那種
    "newline_os_linesep": 'open(p, "w", encoding="utf-8", newline=os.linesep)',
    "write_text_os_linesep": 'p.write_text(s, encoding="utf-8", newline=os.linesep)',
    "newline_variable": 'open(p, "w", encoding="utf-8", newline=nl)',
    # mode 靜態不可知，但 encoding= 證明這是文字串流 ⇒ fail closed
    "dynamic_mode": 'open(p, mode_var, encoding="utf-8")',
    "atomic_wrapper_optout": 'atomic_write_text(p, s, newline=None)',
    # 沒有 mode 參數的文字 handle 建構子，newline 預設就是 None
    "text_io_wrapper": 'io.TextIOWrapper(buf, encoding="utf-8").write(s)',
}

COMPLIANT_SAMPLES = {
    "open_lf": 'open(p, "w", encoding="utf-8", newline="\\n").write(s)',
    "write_text_lf": 'p.write_text(s, encoding="utf-8", newline="\\n")',
    "csv_empty_newline": 'open(p, "w", encoding="utf-8", newline="").write(s)',
    "path_open_lf": 'p.open("w", encoding="utf-8", newline="\\n").write(s)',
    "fdopen_lf": 'os.fdopen(fd, "w", encoding="utf-8", newline="\\n").write(s)',
    "named_tmp_lf": 'tempfile.NamedTemporaryFile(mode="w", newline="\\n")',
    # 讀取與二進位不該被碰
    "read_only": 'open(p, "r", encoding="utf-8").read()',
    "read_default": 'open(p, encoding="utf-8").read()',
    "path_open_read": 'p.open(encoding="utf-8").read()',
    "binary_write": 'open(p, "wb").write(b)',
    "named_tmp_binary_default": 'tempfile.NamedTemporaryFile(suffix=".bin")',
    # wrapper 自己 pin 了 LF，呼叫端不必再傳
    "atomic_wrapper_default": 'atomic_write_text(p, s)',
    # 這些 `.open()` 不是文字檔 handle，連 newline= 參數都沒有 —— 守衛若對
    # 它們報錯，失敗訊息叫人加的那個 kwarg 會直接 TypeError。
    "os_open_fd": 'os.open(p, os.O_WRONLY | os.O_CREAT, 0o600)',
    "tarfile_write": 'tarfile.open(archive, "w:gz")',
    "shelve_open": 'shelve.open(dbpath, "c")',
    # mode 動態但沒有 encoding= ⇒ 沒有證據顯示這是文字串流，不猜
    "dynamic_mode_no_encoding": 'open(path, mode_var)',
}


class TestGuardDetectsKnownViolations:
    """反例層：證明偵測器活著——對 hook 的實作跑。

    ⛔ 不要刪掉這個 class。沒有它，偵測器可以整個回傳空 list 而整份測試檔
    仍然全綠（#1363 實測 250/250 pass）——那是「守衛存在但不會跑」的完全體。
    """

    @pytest.mark.parametrize("name", sorted(VIOLATING_SAMPLES))
    def test_violating_shape_is_flagged(self, name, tmp_path):
        sample = tmp_path / f"v_{name}.py"
        sample.write_text(VIOLATING_SAMPLES[name] + "\n",
                          encoding="utf-8", newline="\n")
        found = _violations_in(sample)
        assert found, (
            f"guard did NOT flag a known-bad shape: {VIOLATING_SAMPLES[name]}\n"
            f"這個形狀在 Windows 上會寫出 CRLF，守衛必須看得見它。"
        )

    @pytest.mark.parametrize("name", sorted(COMPLIANT_SAMPLES))
    def test_compliant_shape_is_not_flagged(self, name, tmp_path):
        sample = tmp_path / f"c_{name}.py"
        sample.write_text(COMPLIANT_SAMPLES[name] + "\n",
                          encoding="utf-8", newline="\n")
        found = _violations_in(sample)
        assert not found, (
            f"guard raised a FALSE POSITIVE on: {COMPLIANT_SAMPLES[name]}\n"
            f"got: {found}"
        )

    def test_ignore_marker_suppresses(self, tmp_path):
        """逃生門必須真的有效，且只關掉行尾規則自己。"""
        sample = tmp_path / "ignored.py"
        sample.write_text(
            f'open(p, "w", encoding="utf-8")  # {IGNORE_MARKER}\n',
            encoding="utf-8", newline="\n")
        assert not _violations_in(sample)
        # 沒有 marker 的同一行必須仍然被抓 —— 證明上面那條是 marker 生效，
        # 不是這個形狀本來就不會被抓（否則這個測試是空跑）。
        bare = tmp_path / "bare.py"
        bare.write_text('open(p, "w", encoding="utf-8")\n',
                        encoding="utf-8", newline="\n")
        assert _violations_in(bare)
        # 兩個 marker 互不代位：encoding 的 marker 不會放行行尾規則。
        other = tmp_path / "other_marker.py"
        other.write_text(
            f'open(p, "w", encoding="utf-8")  # {hook.IGNORE_MARKER}\n',
            encoding="utf-8", newline="\n")
        assert _violations_in(other)

    def test_call_missing_both_keywords_is_reported_once(self, tmp_path, capsys,
                                                         monkeypatch):
        """同一呼叫同時缺 encoding= 與 newline=：印一行、兩條規則各自計數。"""
        sample = tmp_path / "both.py"
        sample.write_text('open(p, "w").write(s)\n', encoding="utf-8", newline="\n")
        monkeypatch.setattr("sys.argv", [
            "check_open_encoding.py", "--ci", "--strict-line-ending",
            "--subprocess-baseline", str(tmp_path / "none.json"), str(sample)])
        rc = hook.main()
        out, err = capsys.readouterr()
        flagged = [ln for ln in out.splitlines() if "both.py:1:" in ln]
        assert rc == 1
        assert len(flagged) == 1, out
        assert "encoding=" in flagged[0] and "newline=" in flagged[0], flagged
        assert "1 encoding violations" in err and "1 line-ending violations" in err


class TestHookScope:
    """hook `entry` 是範圍的 SSOT；這裡釘住它不會被無聲收窄或降級成 warn-only，
    以及 CI 真的會跑它（Lint job 那一行是全樹掃描在 CI 的唯一執行點，#2539）。"""

    def test_line_ending_rule_is_fatal_in_the_hook(self):
        argv = shlex.split(_hook_config()["entry"])
        assert "--strict-line-ending" in argv, (
            f"{HOOK_ID} no longer passes --strict-line-ending: the line-ending "
            f"rule would only warn at commit time (#1366)."
        )

    def test_required_subtrees_are_governed(self):
        """守衛自身的活體檢查——逐一斷言，不用總數門檻。

        ⛔ 不要換回單一的總數門檻：曾經寫 `> 150` 而實際值是 250，把範圍收窄成
        `scripts/tools` 會剩 231 個檔、照樣通過，被靜靜丟掉的正好是
        `scripts/ops` 與 `scripts/session-guards`——也就是「CRLF 會真的弄壞
        東西」的例子（寫 git ref、改 hook shebang）所在的兩棵子樹。

        同時驗 hook 的 `files:`：entry 掃得到、但 `files:` 不選的樹，改那裡的
        檔 commit 時 hook 根本不會觸發。
        """
        cfg = _hook_config()
        covered = {f.resolve() for f in _governed_files()}
        files_re = re.compile(cfg["files"])
        for sub in REQUIRED_SUBTREES:
            subtree = REPO_ROOT / sub
            assert subtree.is_dir(), f"expected subtree missing: {subtree}"
            in_subtree = [f for f in hook.collect_files([subtree])
                          if f.resolve() in covered]
            assert in_subtree, (
                f"{sub} contributes 0 governed files — the hook's "
                f"--line-ending-root scope was narrowed and this subtree fell "
                f"out silently. If that is intended, delete it from "
                f"REQUIRED_SUBTREES in the same commit so the removal is reviewable."
            )
            rel = in_subtree[0].resolve().relative_to(REPO_ROOT).as_posix()
            assert files_re.search(rel), (
                f"{HOOK_ID}'s files: filter does not select {rel} — a commit "
                f"touching {sub} would never run the hook."
            )

    def test_ci_lint_job_runs_the_hook_over_the_whole_tree(self):
        """CI 的執行點：Lint job 逐名跑 `pre-commit run open-encoding-audit --all-files`。

        全樹掃描的**唯一** CI 執行點是那一行（#2539）——本檔不再自帶一份全樹
        掃描。Lint job 逐名跑 hook，不在名單上的 hook 在 CI 等於不存在，所以
        這裡釘的是「那一行在、而且真的會讓 job 紅」。

        ⛔ 解析 YAML，不 grep：`ci.yml` 的註解裡本來就會提到 hook 名稱。
        `run:` 逐邏輯行比對（`_logical_shell_lines` 丟掉註解行、接續行併行），
        且整行必須**恰好**是那條指令——`|| true`、`; true` 之類吞 rc 的寫法
        因此比對不到。step 與 job 都不得帶 `if:` / `continue-on-error`，step
        不得改 `shell:`、script 裡不得 `set +e`（預設 shell 是 `bash -e`，
        一行失敗整個 step 就失敗）。

        ⚠️ 量不到的形狀：包進 shell 函式或 `if` 區塊再呼叫、`trap`、在更早的
        step 改寫 `pre-commit` 本身。這些不是吞 rc 的慣用寫法，未建模。
        """
        workflow = _load_workflow(CI_WORKFLOW)
        assert "lint" in workflow.get("jobs", {}), (
            f"job 'lint' is gone from {CI_WORKFLOW.name}; if it was renamed, "
            f"update this pin — do not drop it.")
        job = workflow["jobs"]["lint"]
        for key in ("if", "continue-on-error"):
            assert key not in job, (
                f"{CI_WORKFLOW.name}::lint has job-level `{key}:` — the "
                f"{HOOK_ID} line would no longer be an unconditional gate.")
        for scope in (workflow, job):
            shell = ((scope.get("defaults") or {}).get("run") or {}).get("shell")
            assert shell is None, (
                f"`defaults.run.shell: {shell}` overrides GitHub's `bash -e`; "
                f"a failing {HOOK_ID} line might no longer fail its step.")

        wanted = ["pre-commit", "run", HOOK_ID, "--all-files"]
        hits = []
        for step in job.get("steps") or []:
            lines = _logical_shell_lines(str(step.get("run") or ""))
            if any(line.split() == wanted for line in lines):
                hits.append((step, lines))
        assert hits, (
            f"{CI_WORKFLOW.name}::lint no longer runs `{' '.join(wanted)}` as "
            f"a bare, uncommented line. That line is the ONLY CI entry point "
            f"for the encoding= / newline= rules (#2539); without it they "
            f"only run where a contributor's local hook happens to.")
        for step, lines in hits:
            label = step.get("name") or "<unnamed step>"
            for key in ("if", "continue-on-error", "shell"):
                assert key not in step, (
                    f"step {label!r} running {HOOK_ID} sets `{key}:` — it can "
                    f"be skipped or its failure swallowed.")
            disarmed = [ln for ln in lines
                        if ln.split()[:1] == ["set"]
                        and any(t.startswith("+") and "e" in t
                                for t in ln.split()[1:])]
            assert not disarmed, (
                f"step {label!r} turns off errexit ({disarmed}); a failing "
                f"{HOOK_ID} line would no longer fail the step.")


class TestSharedWriteHelpersPinLF:
    """共用 helper 的預設值本身就是契約 —— 呼叫端全靠它。"""

    def test_atomic_write_text_defaults_to_lf(self):
        from _atomic_write import atomic_write_text

        default = inspect.signature(atomic_write_text).parameters["newline"].default
        assert default == "\n", (
            "atomic_write_text's newline default is load-bearing: "
            "generate_tool_map.py --safe relies on it rather than passing "
            "newline= itself."
        )

    def test_atomic_write_text_emits_lf_without_being_asked(self, tmp_path):
        """呼叫端不傳 newline 時，落盤 bytes 必須是 LF。

        ⛔ 這支不能被上面那支簽章測試取代。`_atomic_write.py` 內部那行帶著
        `# line-ending: ignore`（`newline=` 是它自己的參數、無法寫成字面值），
        所以 static guard 對它是盲的。實測：把那行改成 `newline=None` 之後，
        守衛乾淨、簽章測試照樣綠、`tests/dx/test_atomic_write.py` 在 Linux 上
        也全綠 —— 約 49 個 caller 依賴的那個 pin 完全沒有跨平台覆蓋。
        這支就是補那個洞：不傳 newline，直接驗 bytes。
        （Windows 上具鑑別力；Linux 上是 smoke test。）
        """
        from _atomic_write import atomic_write_text

        target = tmp_path / "sample.md"
        atomic_write_text(target, "alpha\nbeta\ngamma\n")
        assert b"\r\n" not in target.read_bytes()

    def test_write_text_secure_emits_lf(self, tmp_path):
        """行為測試 —— 在 Windows 上具鑑別力，Linux 上是 smoke test。"""
        from _lib_io import write_text_secure

        target = tmp_path / "sample.md"
        write_text_secure(str(target), "alpha\nbeta\ngamma\n")
        assert b"\r\n" not in target.read_bytes()


class TestSyncCountsEmitsLF:
    """端到端：跑真正的 --sync-counts 寫檔路徑。

    ⚠️ 只在 Windows host 上具鑑別力（見模組 docstring）。在 Linux CI 上這個
    測試恆綠 —— 它證明的是「路徑沒壞」，不是「CRLF 不會復發」。
    擋復發的是 hook `open-encoding-audit` 的 line-ending 規則（CI 上是 Lint
    job 的 `pre-commit run open-encoding-audit --all-files`，由
    `TestHookScope.test_ci_lint_job_runs_the_hook_over_the_whole_tree` 釘住）。
    """

    def _build_fixture_repo(self, root: Path) -> Path:
        # _count_python_tools() 需要真的看到 *.py 才會 > 0 並產生規則
        dx_dir = root / "scripts" / "tools" / "dx"
        dx_dir.mkdir(parents=True)
        for name in ("alpha.py", "beta.py", "gamma.py"):
            (dx_dir / name).write_text("# stub\n", encoding="utf-8", newline="\n")

        # 重現 owner 回報的那條規則：pre-commit hook breakdown
        (root / ".pre-commit-config.yaml").write_text(
            "repos:\n"
            "  - repo: local\n"
            "    hooks:\n"
            "      - id: auto-one\n"
            "      - id: manual-one\n"
            "        stages: [manual]\n"
            "      - id: push-one\n"
            "        stages: [pre-push]\n",
            encoding="utf-8",
            newline="\n",
        )

        claude_md = root / "CLAUDE.md"
        claude_md.write_text(
            "# CLAUDE.md\n"
            "\n"
            "## 工具\n"
            "\n"
            "| 目錄 | 說明 | 數量 |\n"
            "| --- | --- | --- |\n"
            "| `dx/` | DX 自動化（generate_*, bump_docs...） | 99 |\n"
            "\n"
            "## 品質閘門\n"
            "\n"
            "99 auto-run + 88 manual-stage + 77 pre-push hooks。\n"
            "\n"
            "結尾段落，用來確認整檔都被重寫。\n",
            encoding="utf-8",
            newline="\n",
        )
        return claude_md

    def test_sync_counts_does_not_rewrite_file_as_crlf(self, patch_repo_root):
        root = patch_repo_root(bump_docs)
        claude_md = self._build_fixture_repo(root)

        before = claude_md.read_bytes()
        assert b"\r\n" not in before, "fixture must start as pure LF"

        changes = bump_docs.apply_count_updates()

        # 沒有 UPDATE 就代表這個測試什麼都沒證明 —— 規則沒 fire、檔案沒被寫。
        updates = [c for c in changes if c[0] == "UPDATE"]
        assert updates, (
            f"no count rule fired, so the write path never ran: {changes}"
        )

        after = claude_md.read_bytes()
        assert after != before, "file should have been rewritten"
        crlf_count = after.count(b"\r\n")
        assert crlf_count == 0, (
            f"--sync-counts rewrote the whole file with CRLF "
            f"({crlf_count} occurrences) — the write path lost its "
            f'newline="\\n" pin'
        )

    # ⚠️ 這個名字的長度不是隨意的，但**原本寫在這裡的機制說明是錯的**，已於
    # #1364 讀 pinned 原始碼（trufflehog v3.95.3 `pkg/detectors/lob/lob.go`）
    # 更正。舊說法：「`(live|test)_\w{35}`，`test_` 後剛好 35 個 word char 就
    # 中，避開 35 即可」——兩半都不對。
    #
    # 真正的 pattern 是 `(?i:lob)(?:.|[\n\r]){0,40}?\b([a-zA-Z0-9_]{40})\b`：
    # **任何剛好 40 字元的識別字**，只要落在字母 `lob` 之後 40 字元內就中，而
    # `lob` 藏在 `global` / `blob` 這種日常字裡。`test_` 前綴完全不相干——舊
    # 例子只是 `test_`(5) + 35 恰好湊滿 40。⛔ 舊說法的推論還會反向誤導：照
    # 它寫的「此名為 40（安全）」去理解，下一個人會以為 40 沒事，但 40 正是
    # 觸發長度。這個方法名安全的真正理由是**完整識別字是 45 字元**
    # （`test_` + 40），落在 `\b…{40}\b` 之外。
    #
    # ⚠️ 此外 #1364 之後 Lob detector 已在 L2 被停用
    # （`TRUFFLEHOG_EXCLUDE_DETECTORS`），所以「會直接觸發 ROTATE FIRST 阻擋」
    # 這個前提對今天的閘門也不再成立；保留這段是為了記錄命名限制的由來。
    # ⛔ 仍然不要在註解裡把那個舊名字寫出來——字面值一旦重新出現，任何未停用
    # 該 detector 的掃描（例如上游或別的 repo）一樣會命中。
    def test_sync_counts_touches_only_the_count_lines(self, patch_repo_root):
        """順帶釘住：只改數字，不動其他行。

        CRLF 那個 bug 的傷害之所以隱形，正是因為「只有一行的 diff」與
        「整檔被重寫」在 git 眼裡長得一樣。這裡直接比對行內容。

        ⛔ 比較走 **read_bytes()**，不可改回 `read_text().splitlines()`。
        `read_text` 會做 universal-newline 正規化，把 `b"a\\r\\nb"` 讀成
        `"a\\nb"` —— 也就是把這個測試想觀察的那個維度整個洗掉：即使 bug 完整
        存在、整檔被改寫成 CRLF，逐行比對仍然只會看到 2 行不同。原本的寫法正是
        如此，它掛在名為 `...EmitsLF` 的 class 底下卻對 LF 毫無鑑別力。
        """
        root = patch_repo_root(bump_docs)
        claude_md = self._build_fixture_repo(root)
        before_lines = claude_md.read_bytes().split(b"\n")

        bump_docs.apply_count_updates()

        after_lines = claude_md.read_bytes().split(b"\n")
        assert len(before_lines) == len(after_lines)

        differing = [
            i for i, (a, b) in enumerate(zip(before_lines, after_lines)) if a != b
        ]
        # 一條規則改一行：hook breakdown 行。
        #
        # 曾經是 2（dx/ 表格列 + hook breakdown），但 CLAUDE.md 那張工具表在
        # repo 裡已不存在，對應的計數規則隨 #1407 D-5 一併刪除（死規則不留）。
        # fixture 的 `| dx/ | … | 99 |` 那列刻意留著當**反向對照**：它現在沒有
        # 任何規則會碰，所以它必須保持原樣——若哪天又有規則手滑改到它，這個
        # 數字會從 1 變 2，測試就會叫。
        assert len(differing) == 1, (
            f"expected exactly 1 changed line, got {len(differing)}: "
            f"{[(before_lines[i], after_lines[i]) for i in differing]}"
        )
