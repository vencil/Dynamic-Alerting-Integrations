#!/usr/bin/env python3
"""test_sast.py — 集中式 SAST (Static Application Security Testing) 合規掃描。

規則集的 SSOT 是 `docs/internal/dev-rules.md` §5（編號 1-7），本檔的分節標號與它
一致。⛔ 第 4 條（yaml.load）**不在本檔**——#1643 起由 bandit B506 強制，理由寫
在下面第 4 節。其餘六條在本檔各有一個 Test* class。

涵蓋範圍: scripts/tools/ 全部 Python 檔案；⚠️ BOM 那一條例外，它掃全部 tracked
`.py`（理由見下方 `_tracked_py` 上方的註解）。
"""

import ast
import os
import re
import subprocess

import pytest

# ── 掃描範圍 ──────────────────────────────────────────────────────────

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TOOLS_DIR = os.path.join(REPO_ROOT, "scripts", "tools")

# 遞迴收集所有 .py 檔案
_PY_FILES = []
for dirpath, _dirs, filenames in os.walk(TOOLS_DIR):
    for fn in filenames:
        if fn.endswith(".py"):
            _PY_FILES.append(os.path.join(dirpath, fn))

# 確保至少找到預期數量的檔案（防止路徑錯誤導致空掃描）
assert len(_PY_FILES) >= 40, (
    f"預期至少 40 個 Python 檔案，實際找到 {len(_PY_FILES)}"
)


# ── BOM 檢查的語料：全部 tracked `.py`，與上面的 `_PY_FILES` 分開 ──────────
# ⛔ 分開是刻意的。`_PY_FILES` 是其餘六條規則的掃描面（`scripts/tools/`），動它
# 等於一次改掉六條規則的範圍。而 BOM 的傷害面比那大得多：本規則訊息點名的
# `subprocess-timeout-audit` 是 **FATAL** pre-commit hook，它的 `files:` 是
# `^(scripts|components/da-tools|tests)/.*\.py$` ── 實測 606 個檔，其中 365 個
# 落在 `scripts/tools/` 之外。只掃 `_PY_FILES` 等於守住它自己指的那道閘門的四成。
# ⚠️ 用 tracked 檔而不是 `os.walk`：untracked / gitignored 的檔不該讓 CI 紅。
# ⚠️ 成本量過才擴的：讀 616 個檔的前 3 bytes 是 **0.05 秒**。
def _tracked_py():
    out = subprocess.run(
        ["git", "ls-files", "-z", "*.py"], cwd=REPO_ROOT, capture_output=True,
        text=True, stdin=subprocess.DEVNULL, check=True, timeout=120).stdout
    return sorted(os.path.join(REPO_ROOT, p) for p in out.split("\0") if p)


_TRACKED_PY = _tracked_py()

# ⛔ 下限寫成字面量、不取自 `_TRACKED_PY` 自己：從被保護的東西推導出來的下限，
# 會跟著它一起縮到零而不出聲。
assert len(_TRACKED_PY) >= 400, (
    f"`git ls-files '*.py'` 只回了 {len(_TRACKED_PY)} 個檔，不像這個 repo 的清單；"
    "掃描面被截斷時，下面的『沒有檔案帶 BOM』會自己同意自己。"
)


def _read_source(path):
    """讀取並回傳檔案原始碼，剝掉一個前導 BOM。

    ⛔ 剝除是必要的，而且它不是在放行 BOM——放行的是**這支檔案原本的行為**。
    `open(encoding="utf-8")` 會把 BOM 當成 U+FEFF 留在字串裡，而 `ast.parse`
    對它拋 SyntaxError，於是下面每一條規則都走進 `pytest.skip`。實測（同一份
    位元組只差開頭 3 bytes，且該檔 `python <file>` **rc=0 跑得起來**）：

        plain  1 failed / 1694 passed      ← 蓄意違規被抓到
        BOM    1689 passed / 6 skipped     ← rc=0，六條規則全部靜默

    六條裡有兩條是 `governance-security.md` 標 Critical 的（`shell=True`、
    `eval/exec/pickle`）。⛔ `yaml.load` **不在本檔**（#1643 起由 bandit B506
    強制，見下面規則 4 的區塊）。⚠️ CI 那行沒有 `-rs`，所以
    FAILED→SKIPPED 只反映在一個數字上，沒有人會看見。
    ⚠️ 而 skip 訊息說「語法錯誤」——那句話對 BOM 檔是**假的**，它照樣編得過。
    ⛔ 剝掉之後 BOM 本身仍然要被擋，那是下面 `test_source_has_no_bom` 的事：
    這裡負責「看得見」，那裡負責「不准有」。兩件事分開，缺一個就是靜默。
    ⚠️ 剝掉**恰好一個**，與直譯器一致：`compile(bytes)` 對一個 BOM 是 OK、
    對兩個是 SyntaxError，所以兩個 BOM 的檔仍應該走到下面的 skip。
    """
    with open(path, encoding="utf-8") as f:
        return f.read().removeprefix("\ufeff")


def _starts_with_bom(head: bytes) -> bool:
    """Pure, so the control above can hand it bytes that never touch the disk.

    ⛔ Inline over real files this predicate cannot fail — every file in the repo
    is clean — so nothing would tell "the check is here" from "the check was
    deleted". Split out for exactly that reason.
    """
    return head[:3] == b"\xef\xbb\xbf"


def _short_path(path):
    """回傳相對於 repo root 的短路徑（永遠用 forward-slash）。

    Windows 上 os.path.relpath 會回傳 backslash 路徑，但 CHMOD_EXEMPT 的
    key 一律是 forward-slash（POSIX 慣例）。為了讓 `short in CHMOD_EXEMPT`
    在所有 OS 上一致，這裡顯式 normalize 到 forward-slash。
    """
    return os.path.relpath(path, REPO_ROOT).replace(os.sep, "/")


# ============================================================
# 1. open() 必須帶 encoding
# ============================================================

# 允許的 encoding 值
_ALLOWED_ENCODINGS = {"utf-8", "utf-8-sig"}

# 排除模式：以 "rb" / "wb" 開啟的二進位模式不需要 encoding
_BINARY_MODE_RE = re.compile(r'["\'][rwax]+b["\']')


class TestOpenEncoding:
    """SAST 規則 1：open() 的 encoding，以及原始碼不得帶 BOM。

    ⚠️ 兩者掃描面不同：encoding 掃 `_PY_FILES`（`scripts/tools/`），BOM 掃
    `_TRACKED_PY`（全部 tracked `.py`）。理由見 `_tracked_py` 上方。
    """

    def test_the_reader_strips_a_bom_so_the_other_rules_can_see_the_file(self, tmp_path):
        """⛔ CONTROL for `_read_source`, and it has to be synthetic.

        剝除的效果在這棵樹上**不可觀察**：BOM 檔一個都沒有，而下面那條規則正是
        要讓它永遠沒有。實測拿掉剝除 ⇒ 2305 passed rc=0，什麼都不會響。
        ⇒ 唯一能釘住它的是自己造一個帶 BOM 的檔餵給 reader。少了這一格，
        「剝除」這一層是純粹的裝飾——而它的靜默失效會讓六條規則重新變瞎。
        ⚠️ 兩個方向都釘：BOM 要被剝掉，而檔案其餘內容一個位元組都不能動。
        """
        body = "import os\nprint(os.name)\n"
        f = tmp_path / "bom_sample.py"
        f.write_bytes(b"\xef\xbb\xbf" + body.encode("utf-8"))
        got = _read_source(str(f))
        assert got == body, repr(got)
        assert not got.startswith("\ufeff"), "the BOM survived the read"
        # 對照：沒有 BOM 的檔必須原封不動
        g = tmp_path / "plain.py"
        g.write_bytes(body.encode("utf-8"))
        assert _read_source(str(g)) == body

    def test_the_bom_predicate_actually_rejects_a_bom(self):
        """⛔ POSITIVE CONTROL for the scan below. 沒有它，那條掃描是空的。

        `test_source_has_no_bom` 斷言的是「這批檔案都乾淨」，而**任何**讓它
        少看一點的改動都會自動滿足它：實測把它的判定式換成 `head is not None`
        ⇒ 1929 passed rc=0；把 `_read_source` 的剝除拿掉 ⇒ 1929 passed rc=0。
        兩格都存活，因為沒有任何東西拿一個「已知是壞的」輸入餵過那個判定。
        ⇒ 這裡用合成位元組直接餵判定式，兩個方向都釘：該拒的要拒、該收的要收
        （後者防它朝「見人就咬」漂移，例如改成 `head is not None`）。
        """
        assert _starts_with_bom(b"\xef\xbb\xbf" + b"x = 1")
        assert _starts_with_bom(b"\xef\xbb\xbf")
        assert not _starts_with_bom(b"x = 1")
        assert not _starts_with_bom(b"")
        assert not _starts_with_bom(b"# -*- coding: utf-8 -*-")
        # ⚠️ 一個 BOM 的前綴片段不算 BOM——別讓判定式退化成「開頭是 0xEF」。
        assert not _starts_with_bom(b"\xef\xbb")

    @pytest.mark.parametrize("py_file", _TRACKED_PY, ids=_short_path)
    def test_source_has_no_bom(self, py_file):
        """原始碼不得以 UTF-8 BOM 開頭。

        ⛔ 這條規則本來就寫在 `docs/internal/dev-rules.md` 的 SAST 第 1 條
        （「encoding 檢查（強制 UTF-8 without BOM）」），只是一直沒有實作。
        ⚠️ 補上它之前，對「**別的**檔案帶 BOM」唯一會有反應的是一支關於折行
        路徑引用的守衛，而它是以**裸 traceback** 反應的（#1632）。那句話需要
        這個限定詞：幾支模組會 `ast.parse` 自己，所以它們對**自己**帶 BOM 也
        會紅——盲審實測 `tests/lint/test_e2e_spec_lint.py` 就是一例。
        ⚠️ Windows 主機加上 PowerShell 的 `Out-File` / `>` 預設就寫 BOM，
        所以這不是理論風險；`CLAUDE.md` 自己記著這一條。

        ⛔ 為什麼獨立成一條、而不是靠 parse 失敗來擋：帶 BOM 的檔**跑得起來**
        （`compile(bytes)` 會剝掉一個前導 U+FEFF，實測 `python <file>` rc=0），
        所以它不會在任何執行路徑上出聲；而上面 `_read_source` 現在會剝掉它，
        正是為了讓其餘規則看得見那個檔。少了這一條，BOM 就完全沒有人管。
        ⭐ 這條有一條真正回到綠的路，一句話說得完：把檔案存成不帶 BOM 的
        UTF-8。那是它與被它取代的那個裸 traceback 最大的差別。
        """
        with open(py_file, "rb") as handle:
            head = handle.read(3)
        assert not _starts_with_bom(head), (
            f"{_short_path(py_file)} 以 UTF-8 BOM 開頭。這個檔案跑得起來"
            "（直譯器編譯 bytes 時會剝掉一個前導 U+FEFF），所以不會有任何"
            "執行期症狀；但本模組其餘規則、`check_open_encoding` 與 "
            "`check_subprocess_timeout` 讀到的是帶 U+FEFF 的字串，實測會"
            "**靜默跳過**這個檔（#1632）。⚠️ 本模組其餘規則已不在此列——"
            "同一顆 commit 的 `_read_source` 會剝掉它，所以它們現在看得見"
            "這個檔。修法：把它存成不帶 BOM 的 UTF-8。"
        )

    @pytest.mark.parametrize("py_file", _PY_FILES, ids=_short_path)
    def test_open_has_encoding(self, py_file):
        """每個 open() 呼叫（非二進位模式）必須包含 encoding 參數。"""
        source = _read_source(py_file)
        try:
            tree = ast.parse(source, filename=py_file)
        except SyntaxError:
            pytest.skip(f"語法錯誤，跳過: {_short_path(py_file)}")
            return

        violations = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            # 偵測 open(...) 呼叫
            func = node.func
            is_open = False
            if isinstance(func, ast.Name) and func.id == "open":
                is_open = True
            elif isinstance(func, ast.Attribute) and func.attr == "open":
                is_open = True

            if not is_open:
                continue

            # 檢查是否為二進位模式
            line = source.splitlines()[node.lineno - 1] if node.lineno <= len(source.splitlines()) else ""
            if _BINARY_MODE_RE.search(line):
                continue

            # 檢查 mode 參數（第二個位置參數或 keyword）
            mode_val = None
            if len(node.args) >= 2:
                mode_arg = node.args[1]
                if isinstance(mode_arg, ast.Constant):
                    mode_val = mode_arg.value
            for kw in node.keywords:
                if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                    mode_val = kw.value.value

            if mode_val and "b" in str(mode_val):
                continue  # 二進位模式，不需要 encoding

            # 檢查 encoding 參數
            has_encoding = any(kw.arg == "encoding" for kw in node.keywords)
            if not has_encoding:
                violations.append(
                    f"L{node.lineno}: open() 缺少 encoding 參數"
                )

        assert not violations, (
            f"{_short_path(py_file)} 有 {len(violations)} 個 open() 缺少 encoding:\n"
            + "\n".join(f"  {v}" for v in violations)
        )


# ============================================================
# 2. subprocess 禁止 shell=True
# ============================================================

class TestNoShellTrue:
    """掃描 subprocess 呼叫，禁止 shell=True。"""

    @pytest.mark.parametrize("py_file", _PY_FILES, ids=_short_path)
    def test_no_shell_true(self, py_file):
        """subprocess.run/call/Popen 呼叫不得使用 shell=True。"""
        source = _read_source(py_file)
        try:
            tree = ast.parse(source, filename=py_file)
        except SyntaxError:
            pytest.skip(f"語法錯誤，跳過: {_short_path(py_file)}")
            return

        _SUBPROCESS_FUNCS = {"run", "call", "check_call", "check_output", "Popen"}
        violations = []

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func

            # 偵測 subprocess.xxx(...) 呼叫
            is_subprocess = False
            if isinstance(func, ast.Attribute) and func.attr in _SUBPROCESS_FUNCS:
                if isinstance(func.value, ast.Name) and func.value.id == "subprocess":
                    is_subprocess = True

            if not is_subprocess:
                continue

            # 檢查 shell=True
            for kw in node.keywords:
                if kw.arg == "shell":
                    if isinstance(kw.value, ast.Constant) and kw.value.value is True:
                        violations.append(
                            f"L{node.lineno}: subprocess.{func.attr}() 使用 shell=True"
                        )

        assert not violations, (
            f"{_short_path(py_file)} 有 {len(violations)} 個 subprocess shell=True:\n"
            + "\n".join(f"  {v}" for v in violations)
        )


# ============================================================
# 3. 檔案寫入需有適當權限設定
# ============================================================

class TestFileWritePermissions:
    """掃描寫入模式的 open() 呼叫，確認同函式內有 os.chmod。

    注意：此檢查為啟發式（heuristic），採用寬鬆策略——
    只要同一函式體內有 os.chmod 呼叫即視為合規。

    v2.7.0 Harness Audit: 將原本的 advisory skip 改為 explicit exemption。
    每個豁免必須有文件化原因，新增工具若觸發此規則將硬性失敗。
    """

    # ── 豁免清單（每個項目必須有原因）──────────────────────────
    # key = 相對於 repo root 的路徑
    # value = 豁免原因
    CHMOD_EXEMPT = {
        "scripts/tools/lint/fix_file_hygiene.py": (
            "pre-commit auto-fixer：以 open(path, 'wb') 寫回同一檔案，"
            "應保留原始權限而非強制 0o600（會改變既有檔案的 permission bits）"
        ),
        "scripts/tools/ops/generate_rule_pack_split.py": (
            "寫出 validation-report.json 至使用者指定的 output 目錄，"
            "為 DX 報告檔非敏感資料，沿用目錄預設 umask 即可"
        ),
        "scripts/tools/dx/generate_tenant_fixture.py": (
            "產生測試用 conf.d/ fixture YAML 至使用者指定的 --output 目錄，"
            "為開發者 DX 工具非敏感資料，沿用目錄預設 umask 即可"
        ),
        "scripts/tools/dx/run_chaos_soak.py": (
            "v2.8.0 readiness chaos soak harness：寫 metrics-timeseries.csv / "
            "summary.txt / run-config.json 至使用者指定的 --output-dir，"
            "為 DX 觀測非敏感資料，沿用目錄預設 umask 即可"
        ),
    }

    @pytest.mark.parametrize("py_file", _PY_FILES, ids=_short_path)
    def test_write_open_has_chmod(self, py_file):
        """寫入模式 open() 的同一函式中應有 os.chmod 呼叫。"""
        source = _read_source(py_file)
        try:
            tree = ast.parse(source, filename=py_file)
        except SyntaxError:
            pytest.skip(f"語法錯誤，跳過: {_short_path(py_file)}")
            return

        short = _short_path(py_file)

        # 收集每個函式中的寫入 open() 和 chmod 呼叫
        violations = []

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue

            write_opens = []
            has_chmod = False

            for child in ast.walk(node):
                if isinstance(child, ast.Call):
                    func = child.func
                    # 偵測 open(..., "w"...)
                    is_write_open = False
                    if isinstance(func, ast.Name) and func.id == "open":
                        is_write_open = True
                    elif isinstance(func, ast.Attribute) and func.attr == "open":
                        is_write_open = True

                    if is_write_open:
                        mode_val = None
                        if len(child.args) >= 2:
                            arg = child.args[1]
                            if isinstance(arg, ast.Constant):
                                mode_val = arg.value
                        for kw in child.keywords:
                            if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                                mode_val = kw.value.value
                        if mode_val and "w" in str(mode_val):
                            write_opens.append(child.lineno)

                    # 偵測 os.chmod(...) 或 pathlib 等價形式 Path(p).chmod(...)
                    # 兩者語意完全等價（都呼叫 os.chmod 底層），SAST 只在意
                    # 「write open 後有對檔案套權限」的 intent，不限定 spelling。
                    if isinstance(func, ast.Attribute) and func.attr == "chmod":
                        receiver = func.value
                        # os.chmod(p, mode)
                        if isinstance(receiver, ast.Name) and receiver.id == "os":
                            has_chmod = True
                        # Path(p).chmod(mode) — pathlib idiom
                        elif isinstance(receiver, ast.Call):
                            inner = receiver.func
                            if isinstance(inner, ast.Name) and inner.id == "Path":
                                has_chmod = True

            if write_opens and not has_chmod:
                for lineno in write_opens:
                    violations.append(
                        f"L{lineno}: {node.name}() 有 write open 但缺少 os.chmod"
                    )

        if violations:
            if short in self.CHMOD_EXEMPT:
                # 已審查的豁免：pass 而非 skip，避免虛假的 skip count 膨脹
                return
            # 未豁免的新工具：硬性失敗，強制補 chmod 或加入 CHMOD_EXEMPT
            pytest.fail(
                f"{short} 有 {len(violations)} 個潛在權限問題:\n"
                + "\n".join(f"  {v}" for v in violations)
                + "\n\n如確認不需 os.chmod，請將此檔加入 "
                "TestFileWritePermissions.CHMOD_EXEMPT 並註明原因。"
            )

    def test_chmod_exempt_files_exist(self):
        """確保 CHMOD_EXEMPT 中的檔案仍然存在（dead exemption 偵測）。"""
        for rel_path, reason in self.CHMOD_EXEMPT.items():
            full_path = os.path.join(REPO_ROOT, rel_path)
            assert os.path.isfile(full_path), (
                f"CHMOD_EXEMPT 中的 {rel_path} 不存在，"
                "請移除此豁免項目"
            )
            assert len(reason) > 10, (
                f"CHMOD_EXEMPT[{rel_path}] 的原因太短，"
                "請提供有意義的豁免說明"
            )


# ============================================================
# 4. 禁止 yaml.load()（必須使用 yaml.safe_load）
#    ⛔ 這一條**不在本檔實作**——它由 bandit B506 強制，見
#    `.github/workflows/security-audit.yaml`（dev-rules §5 第 4 條逐字
#    指定的就是 B506）。本檔先前另有一份 AST 實作，已於 #1643 移除。
#
#    ⚠️ 這是本次撤除的代價，不是這裡本來就沒有守衛。逐格量測（被移除的那份
#    vs B506）留在該 commit 的訊息裡，不複製到這裡。⛔ 撤除唯一失去的真陽性
#    是「**關不掉**」——`# nosec` 對 B506 有效、對 AST 版無效。
#
#    ⛔ 兩者都看不到的，撤除後仍然看不到。⛔ **不要在這裡列舉形狀**——列舉會
#    漏（本輪盲審就是這樣打出來的）。可推導的那句話是：兩者認的都只是**名字
#    剛好叫 `yaml.load` 的那個呼叫**，所以別的入口一律全盲——`yaml.load_all` /
#    `unsafe_load` / `full_load`，以及自己建 loader 再 `.get_single_data()`。
#    唯一的反向例外是 `Loader=<SafeLoader 子類>`：兩者都**誤紅**。
#    本 repo 兩個活實例（今天都安全，但改壞了沒有人會看見）：
#      `check_admin_config_schema._StrictSafeLoader`（走 `load_all`）；
#      `validate_config._load_with_exporter_keys()`（自己建 loader）。後者的
#    安全性質由**行為級**測試釘住（`tests/ops/test_validate_config.py::
#    TestTenantIdParity::test_the_loader_cannot_construct_python_objects`，餵真的
#    `!!python/object/apply` payload）。⛔ 刪那支測試等於讓它完全無人看守。
#
#    ⛔ 不要在本檔重新長出第二份 AST 實作——同一個判定兩份實作正是 #1643。
# ============================================================


# ============================================================
# 5. 禁止硬編碼機密（密碼、Token、API Key）
# ============================================================

# 排除清單：已知安全的 pattern（測試用假值、常量名稱等）
_CREDENTIAL_SAFE_VALUES = frozenset({
    "password",        # 變數命名或 placeholder
    "changeme",
    "xxx",
    "***",
    "REDACTED",
    "",
})

# 匹配 password = "...", token = "...", secret = "...", api_key = "..." 等
_CREDENTIAL_PATTERN = re.compile(
    r"""(?:password|passwd|token|secret|api_key|apikey|auth_token)"""
    r"""\s*=\s*["']([^"']{4,})["']""",
    re.IGNORECASE,
)

# 排除引用環境變數的 pattern
_ENV_REF_PATTERN = re.compile(
    r"""\$\{?\w+\}?|os\.environ|os\.getenv|valueFrom|secretKeyRef""",
    re.IGNORECASE,
)


class TestNoHardcodedCredentials:
    """掃描 Python 原始碼，禁止硬編碼機密值。"""

    @pytest.mark.parametrize("py_file", _PY_FILES, ids=_short_path)
    def test_no_hardcoded_credentials(self, py_file):
        """程式碼中不得出現硬編碼的密碼、Token 或 API Key。"""
        source = _read_source(py_file)
        violations = []

        for i, line in enumerate(source.splitlines(), 1):
            # 跳過註解行
            stripped = line.strip()
            if stripped.startswith("#"):
                continue

            for match in _CREDENTIAL_PATTERN.finditer(line):
                value = match.group(1).strip()

                # 排除已知安全值
                if value.lower() in _CREDENTIAL_SAFE_VALUES:
                    continue

                # 排除環境變數引用
                if _ENV_REF_PATTERN.search(value):
                    continue

                # 排除格式化字串佔位符
                if "{" in value and "}" in value:
                    continue

                # 排除 argparse help 文字（含空白的描述句）
                if " " in value and len(value.split()) > 3:
                    continue

                violations.append(
                    f"L{i}: 疑似硬編碼機密: {match.group(0)[:60]}..."
                )

        assert not violations, (
            f"{_short_path(py_file)} 有 {len(violations)} 個疑似硬編碼機密:\n"
            + "\n".join(f"  {v}" for v in violations)
        )


# ============================================================
# 6. 禁止危險函式（eval / exec / pickle / os.system / compile）
# ============================================================

# 危險的內建函式呼叫
_DANGEROUS_BUILTINS = {"eval", "exec", "compile"}

# 危險的模組.函式呼叫 (module_name, func_name)
_DANGEROUS_MODULE_CALLS = {
    ("os", "system"),
    ("os", "popen"),
    ("pickle", "load"),
    ("pickle", "loads"),
    ("cPickle", "load"),
    ("cPickle", "loads"),
    ("marshal", "load"),
    ("marshal", "loads"),
}


class TestNoDangerousFunctions:
    """掃描危險函式呼叫：eval, exec, pickle.load, os.system 等。

    這些函式可能導致任意程式碼執行 (ACE)，在生產工具中禁止使用。
    """

    @pytest.mark.parametrize("py_file", _PY_FILES, ids=_short_path)
    def test_no_dangerous_functions(self, py_file):
        """程式碼中不得使用 eval/exec/pickle.load/os.system 等危險函式。"""
        source = _read_source(py_file)
        try:
            tree = ast.parse(source, filename=py_file)
        except SyntaxError:
            pytest.skip(f"語法錯誤，跳過: {_short_path(py_file)}")
            return

        violations = []

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func

            # 偵測 eval() / exec() / compile() 等危險內建函式
            if isinstance(func, ast.Name) and func.id in _DANGEROUS_BUILTINS:
                violations.append(
                    f"L{node.lineno}: {func.id}() — 禁止使用危險內建函式"
                )

            # 偵測 os.system() / pickle.load() 等危險模組函式
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                pair = (func.value.id, func.attr)
                if pair in _DANGEROUS_MODULE_CALLS:
                    violations.append(
                        f"L{node.lineno}: {func.value.id}.{func.attr}() — "
                        "禁止使用危險模組函式"
                    )

        assert not violations, (
            f"{_short_path(py_file)} 有 {len(violations)} 個危險函式呼叫:\n"
            + "\n".join(f"  {v}" for v in violations)
        )


# ============================================================
# 7. 錯誤訊息必須路由到 stderr
# ============================================================

class TestStderrRouting:
    """Rule 7: Error messages must route to stderr.

    print() calls with ERROR/Error prefix must include file=sys.stderr
    to ensure proper log routing in production.
    """

    @pytest.mark.parametrize("py_file", _PY_FILES, ids=_short_path)
    def test_error_prints_use_stderr(self, py_file):
        """確認所有 ERROR/Error 開頭的 print 使用 file=sys.stderr。"""
        source = _read_source(py_file)
        try:
            tree = ast.parse(source, filename=py_file)
        except SyntaxError:
            pytest.skip(f"語法錯誤，跳過: {_short_path(py_file)}")
            return

        violations = []

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func

            # 偵測 print(...) 呼叫
            if not (isinstance(func, ast.Name) and func.id == "print"):
                continue

            # 檢查第一個位置參數是否為 ERROR/Error 開頭的字串
            if not node.args:
                continue

            first_arg = node.args[0]
            starts_with_error = False

            # 檢查字面常數字串
            if isinstance(first_arg, ast.Constant) and isinstance(first_arg.value, str):
                if first_arg.value.startswith("ERROR") or first_arg.value.startswith("Error"):
                    starts_with_error = True

            # 檢查 f-string（JoinedStr）
            elif isinstance(first_arg, ast.JoinedStr):
                if first_arg.values and len(first_arg.values) > 0:
                    first_value = first_arg.values[0]
                    if isinstance(first_value, ast.Constant) and isinstance(first_value.value, str):
                        if first_value.value.startswith("ERROR") or first_value.value.startswith("Error"):
                            starts_with_error = True

            if not starts_with_error:
                continue

            # 檢查是否有 file=sys.stderr 參數
            has_stderr = False
            for kw in node.keywords:
                if kw.arg == "file":
                    # 檢查是否為 sys.stderr
                    if isinstance(kw.value, ast.Attribute):
                        if (isinstance(kw.value.value, ast.Name) and
                            kw.value.value.id == "sys" and
                            kw.value.attr == "stderr"):
                            has_stderr = True

            if not has_stderr:
                violations.append(
                    f"L{node.lineno}: print() with ERROR/Error prefix missing file=sys.stderr"
                )

        assert not violations, (
            f"{_short_path(py_file)} 有 {len(violations)} 個 print() 呼叫未路由到 stderr:\n"
            + "\n".join(f"  {v}" for v in violations)
        )
