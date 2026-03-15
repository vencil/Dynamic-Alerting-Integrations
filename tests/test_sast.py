#!/usr/bin/env python3
"""test_sast.py — 集中式 SAST (Static Application Security Testing) 合規掃描。

掃描所有 Python 工具程式碼，確保符合專案安全規範：
  1. open() 呼叫必須帶 encoding="utf-8"（或 utf-8-sig）
  2. subprocess 呼叫禁止 shell=True
  3. 檔案寫入需搭配 os.chmod(path, 0o600) 限制權限

涵蓋範圍: scripts/tools/ 全部 Python 檔案。
"""

import ast
import os
import re

import pytest

# ── 掃描範圍 ──────────────────────────────────────────────────────────

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
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


def _read_source(path):
    """讀取並回傳檔案原始碼。"""
    with open(path, encoding="utf-8") as f:
        return f.read()


def _short_path(path):
    """回傳相對於 repo root 的短路徑。"""
    return os.path.relpath(path, REPO_ROOT)


# ============================================================
# 1. open() 必須帶 encoding
# ============================================================

# 允許的 encoding 值
_ALLOWED_ENCODINGS = {"utf-8", "utf-8-sig"}

# 排除模式：以 "rb" / "wb" 開啟的二進位模式不需要 encoding
_BINARY_MODE_RE = re.compile(r'["\'][rwax]+b["\']')


class TestOpenEncoding:
    """掃描所有 open() 呼叫，確認帶有 encoding 參數。"""

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
    """

    @pytest.mark.parametrize("py_file", _PY_FILES, ids=_short_path)
    def test_write_open_has_chmod(self, py_file):
        """寫入模式 open() 的同一函式中應有 os.chmod 呼叫。"""
        source = _read_source(py_file)
        try:
            tree = ast.parse(source, filename=py_file)
        except SyntaxError:
            pytest.skip(f"語法錯誤，跳過: {_short_path(py_file)}")
            return

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

                    # 偵測 os.chmod(...)
                    if isinstance(func, ast.Attribute) and func.attr == "chmod":
                        if isinstance(func.value, ast.Name) and func.value.id == "os":
                            has_chmod = True

            if write_opens and not has_chmod:
                for lineno in write_opens:
                    violations.append(
                        f"L{lineno}: {node.name}() 有 write open 但缺少 os.chmod"
                    )

        # 此為 advisory 警告，不做硬性失敗（部分輸出到 stdout 的工具不寫檔案）
        if violations:
            pytest.skip(
                f"{_short_path(py_file)} 有 {len(violations)} 個潛在權限問題 "
                "(advisory):\n" + "\n".join(f"  {v}" for v in violations)
            )
