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
真正的危害是上面那兩條，加上下面兩個**實測會壞**的具體站點（寫進 commit
object 的 message、以及 git hook 的 shebang 行）。

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
import re
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
#
# 範圍＝**所有會出貨或在生產跑的 Python**：`scripts/`（工具與 hook）、
# `components/`（打包進映像的 CLI）、`helm/`（init-container 內跑的腳本）。
# ⛔ 刻意**不含 `tests/`**（實測 1348 個 site）。理由是它們寫的是 tmp fixture、
# 不是出貨產物，行尾不影響任何消費者；把它們拉進來只會製造一次性的大掃除與
# 之後每支新測試的摩擦。這是刻意的邊界、不是漏掉——真要納入請先評估那 1348 個。
GOVERNED_PATHS = [
    REPO_ROOT / "scripts",
    REPO_ROOT / "components",
    REPO_ROOT / "helm",
]

# ⛔ 獨立於 `GOVERNED_PATHS` 的硬編清單，用來釘住「範圍不得被無聲收窄」。
# 必須寫死：拿 GOVERNED_PATHS 自己去檢查自己，等於變數縮小、斷言也跟著縮小。
# `scripts/dx` 明確列入 —— `tests/dx/test_generate_adr_index.py` 記載 PR #477
# 的 CRLF 回歸就出在那兩支 generator，是這條規則的起源現場。
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

# 與 check_open_encoding.py 的 `# open-encoding: ignore` 同風格。
IGNORE_MARKER = "line-ending: ignore"

# ⛔ 這張表刻意**只列 wrapper**，不列「開檔的語法形狀」。
# 第一版守衛把規則寫成「`open` 或 `write_text` 這兩個名字」，結果是列舉而非推導：
# `Path.open("w")` / `os.fdopen(fd,"w")` / `NamedTemporaryFile("w")` / `io.open`
# / `gzip.open(...,"wt")` 全部從旁邊走過去（實測 22 種形狀中 20 種漏掉），樹裡
# 當時就有 6 個真實未 pin 的站點。現在改成：**先判斷這個呼叫會不會交出一個
# 可寫的文字 handle**，再要求它表態；名字只用來決定「預設 mode 是什麼」。
#
# `atomic_write_text` 自己 pin 了 LF（預設 `newline="\n"`，另有專門測試釘住），
# 呼叫端不必再傳；但**明確傳 `newline=None`** 會把平台預設要回來
# （`generate_tool_map.py` 正是這樣中招），所以那個形狀仍要擋。
LF_PINNING_WRAPPERS = {"atomic_write_text"}

# 會交出檔案 handle 的呼叫 → 該呼叫在「沒有指定 mode」時的預設 mode。
# 依此判斷是否為可寫文字模式；`b` 一律視為二進位（無換行轉換）。
HANDLE_FACTORIES = {
    "open": "r",          # builtin open / Path.open / io.open / gzip.open / codecs.open
    "fdopen": "r",        # os.fdopen
    "NamedTemporaryFile": "w+b",
    "TemporaryFile": "w+b",
    "SpooledTemporaryFile": "w+b",
}

# 直接產生文字 handle、**沒有 mode 參數**的建構子：一律需要表態。
# `io.TextIOWrapper(buf, encoding=...)` 的 `newline` 預設就是 `None`，也就是
# 這支守衛存在的理由本身；它在本 repo 已是既有詞彙（`pr_preflight.py`、
# `diag_pr_ci.py` 都用它包 `sys.stdout.buffer`）。
TEXT_WRAPPER_FACTORIES = {"TextIOWrapper"}

# `.open()` 這個名字被太多不相干的 API 借用。以下模組的 `.open()` **不是**
# 文字檔 handle（回傳 fd／binary stream／DB handle／根本不是檔案），把它們
# 排除，否則守衛會對連 `newline=` 參數都沒有的呼叫報錯，而失敗訊息還會叫人
# 去傳那個參數（照做直接 TypeError）。
NON_TEXT_OPEN_MODULES = {
    "os",          # os.open → int fd
    "tarfile", "zipfile", "shelve", "dbm", "sqlite3",
    "webbrowser",  # 根本不是檔案
    "socket",
}

# 位置引數中「第幾個開始才可能是 mode」：
#   open(file, mode) / io.open(file, mode) / os.fdopen(fd, mode) → 1
#   Path.open(mode) / NamedTemporaryFile(mode)                   → 0
# ⛔ 這個偏移是必要的，不是潔癖：若從 0 開始掃，`open("r", "w")`（檔名剛好
# 叫 `r`）會把**檔名**當成 mode、判為唯讀而放行——在一支處處 fail-closed 的
# 守衛裡開一個 fail-open 的洞。
_MODULE_STYLE_OPEN = {"io", "gzip", "codecs", "bz2", "lzma", "fileinput"}

# 檔案 mode 字串的形狀。
_MODE_RE = re.compile(r"^[rwxa]\+?[btU]*\+?$")


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


_UNRESOLVED_MODE = object()


def _mode_arg_start(call: ast.Call, name: str) -> int:
    """First positional index that could hold a mode string.

    `open(file, mode)` / `io.open(file, mode)` / `os.fdopen(fd, mode)` put the
    file/fd first; `Path.open(mode)` / `NamedTemporaryFile(mode)` do not.
    """
    if name == "fdopen":
        return 1
    if name == "open":
        fn = call.func
        if isinstance(fn, ast.Name):
            return 1  # builtin open(file, mode)
        if isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name) \
                and fn.value.id in _MODULE_STYLE_OPEN:
            return 1  # io.open(file, mode) etc.
        return 0      # Path.open(mode)
    return 0          # tempfile factories take mode first


def _mode_of(call: ast.Call, default: str, name: str):
    """Resolve the file mode of a handle-producing call.

    Returns the mode string, or `_UNRESOLVED_MODE` when a mode is supplied but
    is not a static literal (e.g. `open(p, mode_var)`).

    ⛔ 「有給 mode 但看不懂」與「根本沒給 mode」必須是**不同**的回傳值。
    最早的版本把兩者都回 `None` 然後一律放行 ⇒ `open(p, mode_var)` 靜默通過。
    現在前者 fail-closed，後者才套用預設。
    """
    kw = _get_kwarg(call, "mode")
    if kw is not None:
        if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
            return kw.value.value
        return _UNRESOLVED_MODE

    start = _mode_arg_start(call, name)
    saw_non_literal = False
    for arg in call.args[start:]:
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            if _MODE_RE.match(arg.value):
                return arg.value
        elif not isinstance(arg, ast.Constant):
            saw_non_literal = True
    if saw_non_literal:
        return _UNRESOLVED_MODE
    return default


def _newline_verdict(kw):
    """Classify a `newline=` keyword. Returns None if acceptable, else a reason.

    ⛔ 「有傳 newline=」不等於「pin 了行尾」。`newline=os.linesep` 會原封不動
    把原始 bug 寫回去，卻長得完全合規（實測會產生 CRLF）——那正是想修這個
    bug 的人最可能寫出的東西。所以只接受**字串字面值**；任何運算式（變數、
    `os.linesep`、屬性存取）都要求作者改成字面值或明示 ignore。
    """
    if kw is None:
        return "no newline= (platform default → CRLF on Windows)"
    if isinstance(kw.value, ast.Constant):
        if kw.value.value is None:
            return "newline=None (explicit platform default)"
        if isinstance(kw.value.value, str):
            return None  # "\n" / "" / anything the author literally chose
    return (
        f"newline={ast.unparse(kw.value)} is not a string literal — "
        f"os.linesep / a variable can still be CRLF"
    )


def _violations_in(path: Path) -> list[tuple[int, str]]:
    """Return [(lineno, reason)] for text-mode writes that don't pin newline.

    規則由**操作**推導，不是列舉語法：凡是會交出「可寫的文字 handle」的呼叫，
    或直接寫出文字的呼叫，都必須明確表態 `newline=` 且值為字串字面值。值本身
    交給作者決定（csv 的 `newline=""` 合法通過），強制的是「必須表態」。
    """
    # Mirror check_open_encoding.py's robustness: a non-UTF-8 or unparseable
    # .py landing under a governed tree should not turn the whole guard into an
    # ERROR whose traceback points at the guard instead of the offending file.
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return []
    lines = source.splitlines()
    out: list[tuple[int, str]] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        fn = node.func
        name = fn.attr if isinstance(fn, ast.Attribute) else (
            fn.id if isinstance(fn, ast.Name) else None)
        if name is None:
            continue
        newline_kw = _get_kwarg(node, "newline")
        reason = None

        # `<module>.open()` on a module whose open() is not a text file handle
        # (os → int fd, tarfile/zipfile → binary, shelve/dbm → DB, …). These
        # do not even accept newline=, so flagging them would produce advice
        # that raises TypeError if followed.
        # ⚠️ Scoped to the name `open` on purpose: `os.open` is excluded but
        # `os.fdopen` is a genuine text-handle factory and must stay in scope.
        # (An earlier version excluded the whole `os` module and silently lost
        # os.fdopen — the positive-control samples caught it.)
        if name == "open" and isinstance(fn, ast.Attribute) \
                and isinstance(fn.value, ast.Name) \
                and fn.value.id in NON_TEXT_OPEN_MODULES:
            continue

        if name == "write_text":
            # Path.write_text(...) — always text, always writing.
            verdict = _newline_verdict(newline_kw)
            if verdict:
                reason = f"write_text(): {verdict}"

        elif name in TEXT_WRAPPER_FACTORIES:
            # No mode argument at all; it is a text handle by construction.
            verdict = _newline_verdict(newline_kw)
            if verdict:
                reason = f"{name}(): {verdict}"

        elif name in HANDLE_FACTORIES:
            mode = _mode_of(node, HANDLE_FACTORIES[name], name)
            if mode is _UNRESOLVED_MODE:
                # ⛔ fail closed ONLY with positive evidence that this is a text
                # stream — i.e. an encoding= kwarg. Without it we cannot tell a
                # dynamic-mode text open from `os.open(p, flags, 0o600)` or a
                # binary API, and guessing produces false positives on calls
                # that have no newline= parameter to pin.
                # (`encoding=` itself is separately enforced by
                # scripts/tools/lint/check_open_encoding.py, so the pair covers
                # the dynamic-mode case between them.)
                if _get_kwarg(node, "encoding") is None:
                    continue
                reason = (
                    f"{name}(): mode is not a literal, so it cannot be proven "
                    f"read-only or binary — pin newline= or mark ignore"
                )
            elif "b" in mode:
                continue  # binary — no newline translation happens
            elif not any(c in mode for c in "wax+"):
                continue  # read-only text ("r"); nothing is written
            else:
                verdict = _newline_verdict(newline_kw)
                if verdict:
                    reason = f"{name}(..., {mode!r}): {verdict}"

        elif name in LF_PINNING_WRAPPERS:
            # Helper pins LF internally; only an explicit opt-out is a problem.
            if newline_kw is not None and isinstance(newline_kw.value, ast.Constant) \
                    and newline_kw.value.value is None:
                reason = (
                    f"{name}(newline=None) — opts back out of the helper's "
                    f'newline="\\n" default'
                )

        if reason is None:
            continue
        if 1 <= node.lineno <= len(lines) and IGNORE_MARKER in lines[node.lineno - 1]:
            continue
        out.append((node.lineno, reason))

    return out


GOVERNED_FILES = _iter_py_files(GOVERNED_PATHS)


# ⛔ 反例樣本 —— 這一組的存在本身就是閘門的一部分。
#
# 第一版沒有這組樣本，後果是：把 `_violations_in()` 第一行改成 `return []`，
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
    """反例層：證明偵測器活著。

    ⛔ 不要刪掉這個 class。沒有它，`_violations_in()` 可以整個回傳空 list 而
    整份測試檔仍然全綠（實測 250/250 pass）——那是「守衛存在但不會跑」的
    完全體。
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
        """逃生門必須真的有效——它在 repo 內 0 次使用，沒有測試就等於沒驗過。"""
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


class TestWriteSitesPinNewline:
    """Static AST guard — 跨平台閘門（見模組 docstring 的 ⛔ 段）。"""

    def test_governed_paths_are_not_empty(self):
        """守衛自身的活體檢查。

        如果 scripts/ 被搬走或改名，`_iter_py_files` 會安靜地回空 list，
        底下每個 parametrize 都變成 0 個 case ⇒ 整層守衛無聲消失。

        ⛔ 不要把這個換回單一的總數門檻。原本寫的是 `> 150`，而實際值是 250：
        把 `GOVERNED_PATHS` 收窄成 `scripts/tools` 會剩 231 個檔、照樣通過，
        被靜靜丟掉的正好是 `scripts/ops` 與 `scripts/session-guards`——也就是
        本次唯二「CRLF 會真的弄壞東西」的例子（寫 git ref、改 hook shebang）
        所在的兩棵子樹。總數門檻對「範圍被收窄」是盲的，所以改成逐一斷言。
        """
        # ⛔ 不可寫成 `for p in GOVERNED_PATHS: assert ...`。那是對「要被釘住的
        # 那個變數」本身做斷言 —— 變數縮小，斷言也跟著縮小。實測把
        # GOVERNED_PATHS 改回只剩 `scripts/` 時，那種寫法 292 個測試全綠，而
        # 悄悄掉出去的正是本 PR 在 helm/ 修的那一站。所以清單寫死在這裡。
        covered = {f.resolve() for f in GOVERNED_FILES}
        for sub in REQUIRED_SUBTREES:
            subtree = REPO_ROOT / sub
            assert subtree.is_dir(), f"expected subtree missing: {subtree}"
            in_subtree = [f for f in _iter_py_files([subtree])
                          if f.resolve() in covered]
            assert in_subtree, (
                f"{sub} contributes 0 governed files — the guard's scope was "
                f"narrowed and this subtree fell out silently. If that is "
                f"intended, delete it from REQUIRED_SUBTREES in the same "
                f"commit so the removal is reviewable."
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
                f"Other explicit values are accepted — the rule requires a "
                f"stated policy, not LF specifically:\n"
                f'  • csv via the csv module      → newline=""\n'
                f'  • must be CRLF on every host  → newline="\\r\\n"\n'
                f"    (.bat/.cmd/.ps1 are the only paths .gitattributes marks "
                f"eol=crlf; pin CRLF explicitly — do NOT reach for the ignore\n"
                f"     marker, which restores the platform-dependent default "
                f"and so is LF on Linux.)\n"
                f"Only if the call genuinely has no newline= parameter, append "
                f"`# {IGNORE_MARKER}` on the call's own line."
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
        # 兩條規則各改一行：dx/ 表格列 + hook breakdown 行
        assert len(differing) == 2, (
            f"expected exactly 2 changed lines, got {len(differing)}: "
            f"{[(before_lines[i], after_lines[i]) for i in differing]}"
        )
