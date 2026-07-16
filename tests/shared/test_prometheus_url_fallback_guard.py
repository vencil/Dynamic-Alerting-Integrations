"""防回潮 gate：$PROMETHEUS_URL env fallback 一律走 `_lib_io.add_prometheus_arg`。

Rationale（da-tools ROI r3 W1）：
`os.environ.get("PROMETHEUS_URL", default)` 對「set-but-empty」的環境變數
會回**空字串**（key 存在就不套 default）——一個 ConfigMap 缺鍵解析成空字串
的部署會把空 URL 直接塞進 HTTP 請求。canonical 形狀是 `_lib_io.py` 的
`or` 鏈（`default or os.environ.get(...) or "http://localhost:9090"`，空字串
視同未設），與 `entrypoint.inject_prometheus_env` 的 `if prom_url:` 語意
對齊。#1111 W4 已收斂 10 支工具；W1 收掉最後兩支殘留
（threshold_recommend / threshold_govern），本 gate 防止再長回來。

Pattern 精準度：只抓「帶 default 的錯誤形狀」`os.environ.get("PROMETHEUS_URL",`
（後接逗號）。合法的 `or` 鏈寫法（如 alert_correlate.py /
blind_spot_discovery.py 的 `... or os.environ.get("PROMETHEUS_URL") or ...`）
不帶逗號，不會誤殺。`_lib_io.py` 是 canonical 實作本身，排除在掃描外。
"""
from __future__ import annotations

import re
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parents[2] / "scripts" / "tools"

# get("PROMETHEUS_URL" 後面跟逗號 = 帶 default 的錯誤形狀。
# 全文掃描（\s 含換行）：跨行寫法 `os.environ.get(\n "PROMETHEUS_URL", ...)`
# 一樣命中，不因 formatter 拆行而假綠（W1 盲審 LOW-3）。
_BAD_SHAPE = re.compile(r"""os\.environ\s*\.\s*get\s*\(\s*["']PROMETHEUS_URL["']\s*,""")


def test_no_prometheus_env_get_with_default_outside_lib_io():
    """scripts/tools/**/*.py（_lib_io.py 之外）禁止 get("PROMETHEUS_URL", default)。"""
    assert TOOLS_DIR.is_dir(), f"tools dir not found: {TOOLS_DIR}"
    offenders: list[str] = []
    for py in sorted(TOOLS_DIR.rglob("*.py")):
        if py.name == "_lib_io.py":
            continue
        text = py.read_text(encoding="utf-8")
        for m in _BAD_SHAPE.finditer(text):
            lineno = text.count("\n", 0, m.start()) + 1
            line = text.splitlines()[lineno - 1]
            offenders.append(
                f"{py.relative_to(TOOLS_DIR).as_posix()}:{lineno}: {line.strip()}")
    assert offenders == [], (
        "os.environ.get(\"PROMETHEUS_URL\", default) 對 set-but-empty 回空字串；"
        "請改用 _lib_io.add_prometheus_arg（或至少 `or` 鏈）。違規：\n"
        + "\n".join(offenders)
    )
