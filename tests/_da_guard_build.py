"""測試自建 da-guard 時的 `go build` 環境（#3174）——Python 側只在這裡定義。

測試用的 da-guard 建成與出貨版同形：`CGO_ENABLED=0`（da-tools 映像的
`components/da-tools/app/build.sh`、`release.yaml` 都這樣建；Makefile 的對應物是
`DA_GUARD_GO_ENV`）。

⛔ 不是風格選擇：cgo 版的 Go runtime 以 pthread 開每條 OS thread，每條的 stack 是
RLIMIT_STACK 大小（通常 8 MiB）的可寫私有 mapping，全數計入 RLIMIT_DATA；產生器容器的
判定正是在 RLIMIT_DATA（預設 192 MiB）下跑 da-guard。量測：同一棵樹 cgo 版峰值 VmData
隨 thread 數（6–8 條）與 RLIMIT_STACK 落在 112–137 MiB（RLIMIT_STACK 16 MiB 時 152 MiB
起跳），非 cgo 版不論 GOMAXPROCS 都在 71–76 MiB。用 cgo 版測，量到的是本機 thread
排程與 stack 上限，不是判定的記憶體。守衛：tests/ops/test_da_guard_build_shape.py。
"""
from __future__ import annotations

import os

#: 疊加在 os.environ 上的 `go build` 環境。
GO_BUILD_ENV = {"CGO_ENABLED": "0"}


def go_build_env() -> dict:
    """`subprocess.run(..., env=go_build_env())` 用：本行程環境加上 GO_BUILD_ENV。"""
    return {**os.environ, **GO_BUILD_ENV}
