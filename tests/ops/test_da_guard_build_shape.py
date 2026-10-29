"""#3174：repo 內自建的 da-guard 必須與出貨版同形（`CGO_ENABLED=0`）。

cgo 版每條 OS thread 的 pthread stack（RLIMIT_STACK 大小）計入 RLIMIT_DATA，產生器
判定的記憶體預算因此量到 thread 數而不是判定本身——tests/_da_guard_build.py 有量測。
這支守衛讓「有人把 build 改回預設（cgo）」直接轉紅，而不是變回 CI 上的偶發 RLIMIT_DATA。
"""
from __future__ import annotations

import os
import shutil
import subprocess

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _build_settings(binary: str) -> dict[str, str]:
    """`go version -m <binary>` 的 `build` 列，key → value。"""
    go = shutil.which("go")
    if go is None:
        pytest.fail("`go` is not on PATH: the build settings of da-guard cannot be read")
    proc = subprocess.run([go, "version", "-m", binary], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", check=False, timeout=60)
    assert proc.returncode == 0, proc.stderr
    settings = {}
    for line in proc.stdout.splitlines():
        fields = line.strip().split("\t")
        if len(fields) == 2 and fields[0] == "build" and "=" in fields[1]:
            key, _, value = fields[1].partition("=")
            settings[key] = value
    # 一列都沒解析到＝量不到，不是「沒有 cgo」。
    assert settings, f"no build settings in `go version -m` output:\n{proc.stdout}"
    return settings


def test_the_test_session_da_guard_is_built_without_cgo(da_guard_binary):
    settings = _build_settings(da_guard_binary)
    assert settings.get("CGO_ENABLED") == "0", (da_guard_binary, settings)


def test_make_da_guard_build_builds_without_cgo():
    make = shutil.which("make")
    if make is None:
        pytest.fail("`make` is not on PATH: the da-guard-build recipe cannot be read")
    env = {k: v for k, v in os.environ.items() if k != "DA_GUARD_BINARY"}
    proc = subprocess.run([make, "-n", "-s", "-C", REPO_ROOT, "da-guard-build"],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", check=False, timeout=60, env=env)
    assert proc.returncode == 0, proc.stderr
    builds = [ln for ln in proc.stdout.splitlines() if "go build" in ln and "./cmd/da-guard" in ln]
    assert len(builds) == 1, proc.stdout
    before = builds[0].split("go build", 1)[0].split()
    assert "CGO_ENABLED=0" in before, builds[0]


# #3206：產生器的判定在 RLIMIT_DATA 下也跑 da-read 與 da-crdecode（讀出貨的平台規則
# ConfigMap），同一個理由：cgo 版在 CI 上以 `runtime/cgo: pthread_create failed` 中止，
# 判定回 rc 2。測試建的、`make` 建的，都要與出貨同形。
def test_the_test_session_consumer_binaries_are_built_without_cgo(
        da_read_binary, da_crdecode_binary):
    for binary in (da_read_binary, da_crdecode_binary):
        assert _build_settings(binary).get("CGO_ENABLED") == "0", binary


def test_the_session_wide_consumer_binaries_are_built_without_cgo():
    """`_provide_session_consumer_binaries` 設進環境變數的那兩支（子行程用的就是它們）。"""
    for env in ("VIBE_TEST_DA_READ_BINARY", "VIBE_TEST_DA_CRDECODE_BINARY"):
        binary = os.environ.get(env, "")
        assert binary and os.path.isfile(binary), f"${env} names no binary: {binary!r}"
        assert _build_settings(binary).get("CGO_ENABLED") == "0", binary


@pytest.mark.parametrize("target,package", [
    ("da-read-build", "components/da-read"),
    ("da-crdecode-build", "./cmd/da-crdecode"),
])
def test_make_consumer_builds_build_without_cgo(target, package):
    make = shutil.which("make")
    if make is None:
        pytest.fail(f"`make` is not on PATH: the {target} recipe cannot be read")
    proc = subprocess.run([make, "-n", "-s", "-C", REPO_ROOT, target],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", check=False, timeout=60)
    assert proc.returncode == 0, proc.stderr
    builds = [ln for ln in proc.stdout.splitlines() if "go build" in ln and package in ln]
    assert len(builds) == 1, proc.stdout
    assert "CGO_ENABLED=0" in builds[0].split("go build", 1)[0].split(), builds[0]
