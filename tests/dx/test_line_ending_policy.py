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

壞掉的只有工作目錄的 bytes：它與樹裡其他檔案不一致，打壞 byte-level 比對
（例如斷言「檔案被還原成 byte-identical」的 mutation harness），並讓之後每次
`git diff` 都噴 "CRLF will be replaced by LF" 警告。

⛔ 第 3 點決定了這個檔案為什麼分兩層
====================================
CPython 的 CRLF 轉換寫死在 `TextIOWrapper` 的**編譯期 `#ifdef MS_WINDOWS`**，
不是執行期查 `os.linesep`。實測：在 Windows 上 monkeypatch
`os.linesep = "\\n"` 之後，`write_text` **照樣**寫出 `b"x\\r\\ny\\r\\n"`。

⇒ 在 Linux CI 上，**有 bug 的版本不可能產生 CRLF**。任何「跑一次 sync path、
斷言輸出 bytes 沒有 `\\r\\n`」的行為測試，在 CI 裡都是**恆綠**的。

所以本檔分兩層，兩層的職責不同、不可互相取代：

* `TestWriteSitesPinNewline` — static AST guard。**跨平台都會紅**，
  這才是真正的閘門，也是唯一能在 CI 擋下復發的東西。
* `TestSyncCountsEmitsLF` — 端到端行為測試。跑真正的
  `apply_count_updates()` 寫檔路徑，但**只在 Windows host 上具鑑別力**。

⚠️ 如果你打算精簡這個檔案：拿掉 static guard、只留行為測試，等於在 CI 裡留下
一個永遠不會紅的守衛。要動之前請先讀懂上面這段。
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

import bump_docs  # noqa: E402  (sys.path wired by tests/conftest.py)

REPO_ROOT = Path(bump_docs.__file__).resolve().parent.parent.parent.parent

# ── 守衛涵蓋範圍 ────────────────────────────────────────────────────────
# 整棵 scripts/ ——「每個工具寫檔前都要對行尾表態」是可推導的邊界，比列舉
# 目錄好：新增子目錄會自動納入，不需要有人記得回來改這張表。
#
# 這裡不會誤傷**合法**需要非 LF 的用法：規則只要求「明確表態」，不要求值
# 一定是 LF。csv 模組要求的 `newline=""`（`run_chaos_soak.py`）與
# `_federation_revocation_reconciler.py` 讀取端明文標註 load-bearing 的
# `newline=""` 都照常通過。被擋的只有「沒表態」與「明確要平台預設
# （`newline=None`）」兩種。
GOVERNED_PATHS = [REPO_ROOT / "scripts"]

# 與 check_open_encoding.py 的 `# open-encoding: ignore` 同風格。
IGNORE_MARKER = "line-ending: ignore"

# 這兩個 wrapper 自己在內部 pin 了 LF，呼叫端不必再傳 newline=。
# 但**明確傳 newline=None** 會把平台預設轉換要回來（generate_tool_map.py 就是
# 這樣中招的），所以那個形狀仍要擋。
LF_PINNING_WRAPPERS = {"write_text_secure", "atomic_write_text"}


def _iter_py_files(paths: list[Path]) -> list[Path]:
    out: list[Path] = []
    for p in paths:
        if p.is_file() and p.suffix == ".py":
            out.append(p)
        elif p.is_dir():
            out.extend(
                f for f in sorted(p.rglob("*.py"))
                if "__pycache__" not in f.parts
            )
    return out


def _get_kwarg(call: ast.Call, name: str):
    for kw in call.keywords:
        if kw.arg == name:
            return kw
    return None


def _mode_of(call: ast.Call):
    """Return open()'s mode string if statically known, else None."""
    if len(call.args) >= 2 and isinstance(call.args[1], ast.Constant):
        if isinstance(call.args[1].value, str):
            return call.args[1].value
    kw = _get_kwarg(call, "mode")
    if kw is not None and isinstance(kw.value, ast.Constant):
        if isinstance(kw.value.value, str):
            return kw.value.value
    return None


def _is_explicit_none(kw) -> bool:
    return isinstance(kw.value, ast.Constant) and kw.value.value is None


def _violations_in(path: Path) -> list[tuple[int, str]]:
    """Return [(lineno, reason)] for text-mode writes that don't pin newline.

    可推導規則，不是列舉違規語法：任何**會寫出文字**的呼叫都必須明確表態
    `newline=`，而且不能是 `None`。值本身交給作者決定（csv 的 `newline=""`
    合法通過），強制的是「必須表態」。
    """
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    lines = source.splitlines()
    out: list[tuple[int, str]] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        fn = node.func
        newline_kw = _get_kwarg(node, "newline")
        reason = None

        if isinstance(fn, ast.Attribute) and fn.attr == "write_text":
            if newline_kw is None:
                reason = "Path.write_text() without newline="
            elif _is_explicit_none(newline_kw):
                reason = "Path.write_text(newline=None) — platform default"

        elif isinstance(fn, ast.Name) and fn.id == "open":
            mode = _mode_of(node)
            if mode is None or "b" in mode:
                continue  # read-default or binary — no text translation
            if not any(c in mode for c in "wax"):
                continue  # reading only
            if newline_kw is None:
                reason = f"open(..., {mode!r}) without newline="
            elif _is_explicit_none(newline_kw):
                reason = f"open(..., {mode!r}, newline=None) — platform default"

        elif isinstance(fn, ast.Name) and fn.id in LF_PINNING_WRAPPERS:
            if newline_kw is not None and _is_explicit_none(newline_kw):
                reason = (
                    f"{fn.id}(newline=None) — opts back out of the helper's "
                    f'newline="\\n" default'
                )

        if reason is None:
            continue
        if 1 <= node.lineno <= len(lines) and IGNORE_MARKER in lines[node.lineno - 1]:
            continue
        out.append((node.lineno, reason))

    return out


GOVERNED_FILES = _iter_py_files(GOVERNED_PATHS)


class TestWriteSitesPinNewline:
    """Static AST guard — 跨平台閘門（見模組 docstring 的 ⛔ 段）。"""

    def test_governed_paths_are_not_empty(self):
        """守衛自身的活體檢查。

        如果 scripts/ 被搬走或改名，`_iter_py_files` 會安靜地回空 list，
        底下每個 parametrize 都變成 0 個 case ⇒ 整層守衛無聲消失。
        """
        assert len(GOVERNED_FILES) > 150, (
            f"only {len(GOVERNED_FILES)} governed files found — "
            f"GOVERNED_PATHS is stale, the guard is scanning almost nothing"
        )

    @pytest.mark.parametrize(
        "py_file", GOVERNED_FILES, ids=lambda p: p.name,
    )
    def test_text_writes_pin_newline(self, py_file: Path):
        violations = _violations_in(py_file)
        if violations:
            rel = py_file.relative_to(REPO_ROOT).as_posix()
            detail = "\n".join(f"  {rel}:{ln}: {why}" for ln, why in violations)
            pytest.fail(
                f"text-mode write(s) without an explicit LF policy:\n{detail}\n\n"
                f'Fix: pass newline="\\n" (repo standard — .gitattributes pins '
                f"`* text=auto eol=lf`).\n"
                f"Without it the SAME generator emits LF on Linux/CI and CRLF "
                f"on a Windows host.\n"
                f'If a non-LF ending is genuinely required (csv wants '
                f'newline=""), pass it explicitly.\n'
                f"If the file must be CRLF (.bat/.cmd/.ps1 — the only paths "
                f".gitattributes marks eol=crlf), append "
                f"`# {IGNORE_MARKER}` on that line."
            )


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
    擋復發的是 TestWriteSitesPinNewline。
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

    # ⚠️ 這個名字的長度不是隨意的。`test_` 後面接**剛好 35 個** word char 會
    # 命中 trufflehog 的 Lob API-key 形狀（`(live|test)_\w{35}`），而該
    # detector 會把它回報成 VERIFIED，直接觸發 L2 secret-scan 的
    # 「ROTATE FIRST」阻擋。本測試的前一個名字正好是 35，實際被擋過一次。
    # 取名時避開 35 即可（此名為 40）。⛔ 也不要在註解裡把那個舊名字寫出來
    # ——字面值一旦重新出現，掃描器一樣會命中。
    def test_sync_counts_touches_only_the_count_lines(self, patch_repo_root):
        """順帶釘住：只改數字，不動其他行。

        CRLF 那個 bug 的傷害之所以隱形，正是因為「只有一行的 diff」與
        「整檔被重寫」在 git 眼裡長得一樣。這裡直接比對行內容。
        """
        root = patch_repo_root(bump_docs)
        claude_md = self._build_fixture_repo(root)
        before_lines = claude_md.read_text(encoding="utf-8").splitlines()

        bump_docs.apply_count_updates()

        after_lines = claude_md.read_text(encoding="utf-8").splitlines()
        assert len(before_lines) == len(after_lines)

        differing = [
            i for i, (a, b) in enumerate(zip(before_lines, after_lines)) if a != b
        ]
        # 兩條規則各改一行：dx/ 表格列 + hook breakdown 行
        assert len(differing) == 2, (
            f"expected exactly 2 changed lines, got {len(differing)}: "
            f"{[(before_lines[i], after_lines[i]) for i in differing]}"
        )
