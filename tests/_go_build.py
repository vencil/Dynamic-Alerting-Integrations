"""測試自建 Go binary 的唯一入口（#3425）：組出呼叫 `scripts/ops/go_build.sh` 的 argv。

建置旗標（`CGO_ENABLED=0`、`-trimpath`、`-ldflags "-s -w"`、`-buildvcs=false`）只寫在
那支腳本裡，映像、release、CI、Makefile 與測試都經它建，測的 binary 才與出貨的同形。
⛔ `CGO_ENABLED=0` 不是風格選擇：cgo 版每條 OS thread 的 pthread stack 計入
RLIMIT_DATA，產生器在 RLIMIT_DATA 下判定時量到的是 thread 數而不是判定的記憶體——
量測與每個旗標的理由都在腳本頂部。守衛：tests/ops/test_go_build_ssot.py（量測試
session 建出的 binary 確實是那個形狀；另掃描常見寫法的直接 `go build` 呼叫點）。
"""
from __future__ import annotations

import os
import shutil

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 建置旗標的單一來源。
GO_BUILD_SCRIPT = os.path.join(REPO_ROOT, "scripts", "ops", "go_build.sh")


def go_build_argv(module_dir: str, package: str, output: str, *,
                  ldflags_x: "tuple[str, ...] | list[str]" = ()) -> list[str]:
    """`subprocess.run(go_build_argv(...))` 用：以 go_build.sh 建 *module_dir* 裡的 *package*
    到 *output*。*ldflags_x* 是 `importpath.name=value`，各成一個 `-X`。

    以 `sh` 執行腳本：不依賴檔案的執行位（掛載路徑、Windows checkout 可能沒有）。
    """
    sh = shutil.which("sh") or "/bin/sh"
    argv = [sh, GO_BUILD_SCRIPT]
    for x in ldflags_x:
        argv += ["-X", x]
    return argv + [str(module_dir), str(package), str(output)]
