#!/usr/bin/env python3
"""test_dispatch_contract.py — dispatcher shim 佈線契約（parametrized over 3 shims）。

取代原本三份近乎逐行鏡像的 per-shim 測試檔：

  tests/ops/test_guard_dispatch.py     (16 cases)
  tests/ops/test_batchpr_dispatch.py   (22 cases)
  tests/ops/test_parser_dispatch.py    (22 cases)

三個 shim（guard_dispatch / batchpr_dispatch / parser_dispatch）自 v2.8.0 PR-2
起都是「純 config + 委派給 _lib_godispatch.GoBinaryDispatcher」的薄殼；
library 本身的契約由 tests/shared/test_lib_godispatch.py 用假 dispatcher 釘住。
本檔釘的是 **每個 shim 的實際佈線**（subcommand 集合、binary 名 / flag / env var、
pass_subcommand 方向、usage 文案），對每個 shim 跑同一組契約 case：

  - help / usage（no-args、-h/--help/help；每個 subcommand 都要出現在 usage）
  - subcommand allowlist（合法轉發、未知 → exit 2 + 列出可用清單）
  - binary 解析順序（--flag space/= 形式 > $ENV > $PATH；空值 / 裸 flag fall-through）
  - missing-binary 友善錯誤（explicit 路徑 echo、resolution order、install hints）
  - argv passthrough 完整性（順序保留、binary flag 剝除、subcommand 依
    pass_subcommand 轉發或剝除）
  - exit code passthrough
  - subprocess FileNotFoundError / OSError → exit 2

subprocess.run 均被 mock，測試不需要真的 Go binary。
"""
from __future__ import annotations

import re
import stat
import subprocess
from dataclasses import dataclass
from typing import Callable

import pytest

# conftest.py 已把 scripts/tools 與 scripts/tools/ops 放進 sys.path。
import batchpr_dispatch
import guard_dispatch
import parser_dispatch


# ---------------------------------------------------------------------------
# Per-shim 佈線規格
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ShimSpec:
    """一個 dispatcher shim 的佈線事實（測試以行為驗證，不直接讀 _DISPATCHER）。"""
    id: str
    main: Callable[[list], int]
    cli_alias: str
    binary_name: str
    binary_flag: str
    env_var: str
    subcommands: tuple            # 全部合法 subcommand
    pass_subcommand: bool         # True: 轉發 subcommand；False: 剝除（guard pattern）
    sample_flags: tuple           # 泛用測試附帶的無害 flags
    help_regexes: tuple           # usage 輸出必須命中的 regex（防 substring 假陽性）
    # 每項: (subcommand, forwarded_flags) — passthrough 精確順序測試用。
    passthrough_samples: tuple

    def __str__(self):  # pytest ids
        return self.id


SPECS = [
    ShimSpec(
        id="guard",
        main=guard_dispatch.main,
        cli_alias="guard",
        binary_name="da-guard",
        binary_flag="--da-guard-binary",
        env_var="DA_GUARD_BINARY",
        subcommands=("defaults-impact",),
        pass_subcommand=False,
        sample_flags=("--config-dir", "/tmp/conf.d"),
        help_regexes=(r"guard", r"defaults-impact"),
        passthrough_samples=(
            (
                "defaults-impact",
                (
                    "--config-dir", "/conf.d",
                    "--scope", "/conf.d/db",
                    "--required-fields", "cpu,mem",
                    "--cardinality-limit", "500",
                    "--format", "json",
                    "--warn-as-error",
                ),
            ),
        ),
    ),
    ShimSpec(
        id="batchpr",
        main=batchpr_dispatch.main,
        cli_alias="batch-pr",
        binary_name="da-batchpr",
        binary_flag="--da-batchpr-binary",
        env_var="DA_BATCHPR_BINARY",
        subcommands=("apply", "refresh", "refresh-source"),
        pass_subcommand=True,
        sample_flags=("--workdir", "/tmp/repo"),
        # "refresh" 是 "refresh-source" 的 substring — 用行首 + 後接空白鎖定
        # standalone refresh 那一行，避免 false-positive（沿自原 batchpr 測試）。
        help_regexes=(r"batch-pr", r"apply", r"(?m)^\s*refresh\s{2,}", r"refresh-source"),
        passthrough_samples=(
            (
                "refresh-source",
                (
                    "--input", "in.json",
                    "--patches-dir", "./patches/",
                    "--workdir", "./repo",
                    "--report", "out.md",
                    "--dry-run",
                ),
            ),
            (
                "apply",
                (
                    "--plan", "plan.json",
                    "--emit-dir", "./emit/",
                    "--repo", "vencil/customer",
                    "--workdir", "./customer-repo",
                    "--branch-prefix", "import/",
                    "--inter-call-delay-ms", "500",
                ),
            ),
        ),
    ),
    ShimSpec(
        id="parser",
        main=parser_dispatch.main,
        cli_alias="parser",
        binary_name="da-parser",
        binary_flag="--da-parser-binary",
        env_var="DA_PARSER_BINARY",
        subcommands=("import", "allowlist"),
        pass_subcommand=True,
        sample_flags=("--input", "rules.yaml"),
        help_regexes=(r"parser", r"import", r"allowlist"),
        passthrough_samples=(
            (
                "import",
                (
                    "--input", "rules.yaml",
                    "--fail-on-non-portable",
                    "--generated-by", "ci-job-99",
                ),
            ),
            ("allowlist", ("--format", "json")),
        ),
    ),
]

# 展平的 (spec, subcommand) 與 (spec, sub, flags) 參數組。
SPEC_SUB_PAIRS = [(s, sub) for s in SPECS for sub in s.subcommands]
PASSTHROUGH_CASES = [
    (s, sub, flags) for s in SPECS for (sub, flags) in s.passthrough_samples
]


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _en_lang_and_clean_env(monkeypatch):
    """固定英文訊息（斷言決定性；語言選擇本身由 test_lib_godispatch 覆蓋），
    並清掉所有 shim 的 binary env var，避免宿主環境洩漏進解析順序。"""
    monkeypatch.setenv("DA_LANG", "en")
    for s in SPECS:
        monkeypatch.delenv(s.env_var, raising=False)


def _make_fake_binary(tmp_path, name):
    """建立一個掛上執行位元的假 binary 檔。subprocess.run 都被 patch，
    內容無所謂 —— _resolve_binary 只檢查檔案存在。"""
    p = tmp_path / name
    p.write_text("#!/bin/sh\nexit 0\n")
    p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(p)


def _run_ok(main, argv):
    """以 mock subprocess.run（rc=0）呼叫 shim main，回傳 (rc, forwarded_cmd)。"""
    from unittest import mock
    with mock.patch("subprocess.run") as run:
        run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
        rc = main(list(argv))
    return rc, (run.call_args[0][0] if run.called else None)


# ---------------------------------------------------------------------------
# help / usage
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("spec", SPECS, ids=str)
def test_help_no_args_returns_zero(spec, capsys):
    rc = spec.main([])
    captured = capsys.readouterr()
    assert rc == 0
    for pattern in spec.help_regexes:
        assert re.search(pattern, captured.out), (
            f"usage for {spec.id} missing pattern {pattern!r}"
        )


@pytest.mark.parametrize("spec", SPECS, ids=str)
@pytest.mark.parametrize("flag", ["-h", "--help", "help"])
def test_help_explicit_flag_returns_zero(spec, flag, capsys):
    rc = spec.main([flag])
    captured = capsys.readouterr()
    assert rc == 0, f"flag {flag} returned {rc}"
    assert spec.subcommands[0] in captured.out


# ---------------------------------------------------------------------------
# subcommand allowlist
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "spec,sub", SPEC_SUB_PAIRS, ids=[f"{s.id}-{sub}" for s, sub in SPEC_SUB_PAIRS]
)
def test_known_subcommand_accepted(spec, sub, tmp_path):
    """每個合法 subcommand 都乾淨轉發；subcommand 依 pass_subcommand 出現/剝除。"""
    fake = _make_fake_binary(tmp_path, spec.binary_name)
    rc, cmd = _run_ok(
        spec.main, [sub, spec.binary_flag, fake, *spec.sample_flags]
    )
    assert rc == 0
    assert cmd[0] == fake
    if spec.pass_subcommand:
        assert cmd[1] == sub
    else:
        assert sub not in cmd


@pytest.mark.parametrize("spec", SPECS, ids=str)
def test_unknown_subcommand_returns_two(spec, capsys):
    rc = spec.main(["frobnicate"])
    captured = capsys.readouterr()
    assert rc == 2
    assert "unknown" in captured.err.lower()
    assert spec.cli_alias in captured.err
    for sub in spec.subcommands:  # 錯誤訊息須列出可用 subcommands
        assert sub in captured.err


# ---------------------------------------------------------------------------
# binary 解析：explicit flag
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("spec", SPECS, ids=str)
def test_explicit_flag_wins_over_env_and_path(spec, tmp_path, monkeypatch):
    """--<flag> 永遠壓過 $ENV 與 $PATH（兩者都灌毒確認未被採用）。"""
    fake = _make_fake_binary(tmp_path, spec.binary_name)
    monkeypatch.setenv(spec.env_var, "/some/wrong/path")
    monkeypatch.setattr("shutil.which", lambda _: "/some/other/wrong/path")
    rc, cmd = _run_ok(
        spec.main, [spec.subcommands[0], spec.binary_flag, fake]
    )
    assert rc == 0
    assert cmd[0] == fake
    # flag 本體須在轉發前剝除。
    assert spec.binary_flag not in cmd


@pytest.mark.parametrize("spec", SPECS, ids=str)
def test_explicit_flag_equals_form_supported(spec, tmp_path):
    """--<flag>=<path> 單參數形式；flag 殘留不得出現在轉發 argv。"""
    fake = _make_fake_binary(tmp_path, spec.binary_name)
    rc, cmd = _run_ok(
        spec.main, [spec.subcommands[0], f"{spec.binary_flag}={fake}"]
    )
    assert rc == 0
    assert cmd[0] == fake
    assert spec.binary_flag not in cmd
    assert not any(a.startswith(f"{spec.binary_flag}=") for a in cmd)


@pytest.mark.parametrize("spec", SPECS, ids=str)
def test_explicit_empty_value_falls_through_to_env(spec, tmp_path, monkeypatch):
    """`--<flag>=`（空值）視同未指定，fall through 到 $ENV / $PATH。
    契約 pin：_resolve_binary 的 `if explicit:` 對空字串為 falsy —— 若未 pin，
    改成把空路徑轉發給 subprocess 的 regression 會無聲破壞。"""
    fake = _make_fake_binary(tmp_path, spec.binary_name)
    monkeypatch.setenv(spec.env_var, fake)
    rc, cmd = _run_ok(spec.main, [spec.subcommands[0], f"{spec.binary_flag}="])
    assert rc == 0
    assert cmd[0] == fake


@pytest.mark.parametrize("spec", SPECS, ids=str)
def test_explicit_trailing_bare_flag_falls_through(spec, tmp_path, monkeypatch):
    """結尾裸 `--<flag>`（無值）須被丟棄後走 $ENV / $PATH，且絕不可轉發
    裸 flag（會讓 Go binary flag-parse error）。"""
    fake = _make_fake_binary(tmp_path, spec.binary_name)
    monkeypatch.setenv(spec.env_var, fake)
    rc, cmd = _run_ok(
        spec.main,
        [spec.subcommands[0], *spec.sample_flags, spec.binary_flag],
    )
    assert rc == 0
    assert cmd[0] == fake
    assert spec.binary_flag not in cmd


@pytest.mark.parametrize("spec", SPECS, ids=str)
def test_explicit_missing_binary_returns_two(spec, tmp_path, capsys):
    nonexistent = str(tmp_path / "does-not-exist")
    rc = spec.main([spec.subcommands[0], spec.binary_flag, nonexistent])
    captured = capsys.readouterr()
    assert rc == 2
    assert f"{spec.binary_name} binary not found" in captured.err
    # 使用者提供的路徑須原樣 echo 以利診斷（lib 保證 raw、非 repr()）。
    assert nonexistent in captured.err


# ---------------------------------------------------------------------------
# binary 解析：$ENV / $PATH
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("spec", SPECS, ids=str)
def test_env_var_used_when_no_explicit_flag(spec, tmp_path, monkeypatch):
    fake = _make_fake_binary(tmp_path, spec.binary_name)
    monkeypatch.setenv(spec.env_var, fake)
    monkeypatch.setattr("shutil.which", lambda _: None)
    rc, cmd = _run_ok(spec.main, [spec.subcommands[0]])
    assert rc == 0
    assert cmd[0] == fake


@pytest.mark.parametrize("spec", SPECS, ids=str)
def test_env_var_missing_file_falls_through_to_error(spec, monkeypatch, capsys):
    """env var 指向不存在路徑 + $PATH 落空 → exit 2；訊息走 generic
    resolution-order 版（env 路徑視為 soft fallback，不特別點名）。"""
    monkeypatch.setenv(spec.env_var, f"/missing/{spec.binary_name}")
    monkeypatch.setattr("shutil.which", lambda _: None)
    rc = spec.main([spec.subcommands[0]])
    captured = capsys.readouterr()
    assert rc == 2
    assert f"{spec.binary_name} binary not found" in captured.err
    assert "Resolution order" in captured.err


@pytest.mark.parametrize("spec", SPECS, ids=str)
def test_path_lookup_used_when_no_flag_no_env(spec, tmp_path, monkeypatch):
    fake = _make_fake_binary(tmp_path, spec.binary_name)
    monkeypatch.setattr(
        "shutil.which", lambda name: fake if name == spec.binary_name else None
    )
    rc, cmd = _run_ok(spec.main, [spec.subcommands[-1]])
    assert rc == 0
    assert cmd[0] == fake


@pytest.mark.parametrize("spec", SPECS, ids=str)
def test_path_lookup_misses_returns_two_with_install_hints(spec, monkeypatch, capsys):
    monkeypatch.setattr("shutil.which", lambda _: None)
    rc = spec.main([spec.subcommands[0]])
    captured = capsys.readouterr()
    assert rc == 2
    assert f"{spec.binary_name} binary not found" in captured.err
    # 三層解析順序 + 友善安裝指引都要出現。
    assert spec.binary_flag in captured.err
    assert spec.env_var in captured.err
    assert "Install options" in captured.err
    assert "github.com/vencil/Dynamic-Alerting-Integrations/releases" in captured.err
    assert "go build" in captured.err


# ---------------------------------------------------------------------------
# argv passthrough 完整性
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "spec,sub,flags",
    PASSTHROUGH_CASES,
    ids=[f"{s.id}-{sub}" for s, sub, _ in PASSTHROUGH_CASES],
)
def test_argv_passthrough_exact_order(spec, sub, flags, tmp_path):
    """轉發 argv 逐位精確：binary 先、subcommand 依 pass_subcommand、
    其餘 flags 原順序、binary flag 剝除。"""
    fake = _make_fake_binary(tmp_path, spec.binary_name)
    rc, cmd = _run_ok(spec.main, [sub, spec.binary_flag, fake, *flags])
    assert rc == 0
    expected = [fake] + ([sub] if spec.pass_subcommand else []) + list(flags)
    assert cmd == expected
    if not spec.pass_subcommand:
        assert sub not in cmd  # guard pattern：subcommand 是 Python 側組織層


# ---------------------------------------------------------------------------
# exit code passthrough
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("spec", SPECS, ids=str)
@pytest.mark.parametrize("rc_in", [0, 1, 2, 3])
def test_exit_code_passes_through(spec, rc_in, tmp_path):
    """Go binary 回什麼，dispatcher 就回什麼。"""
    from unittest import mock
    fake = _make_fake_binary(tmp_path, spec.binary_name)
    with mock.patch("subprocess.run") as run:
        run.return_value = subprocess.CompletedProcess(args=[], returncode=rc_in)
        rc = spec.main([spec.subcommands[0], spec.binary_flag, fake])
    assert rc == rc_in


# ---------------------------------------------------------------------------
# subprocess 例外
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("spec", SPECS, ids=str)
def test_subprocess_filenotfounderror_returns_two(spec, tmp_path, capsys):
    """Race：binary 在 resolve 與 exec 之間消失 → 視同 missing。"""
    from unittest import mock
    fake = _make_fake_binary(tmp_path, spec.binary_name)
    with mock.patch("subprocess.run", side_effect=FileNotFoundError):
        rc = spec.main([spec.subcommands[0], spec.binary_flag, fake])
    captured = capsys.readouterr()
    assert rc == 2
    assert "not found" in captured.err.lower()


@pytest.mark.parametrize("spec", SPECS, ids=str)
def test_subprocess_oserror_returns_two(spec, tmp_path, capsys):
    from unittest import mock
    fake = _make_fake_binary(tmp_path, spec.binary_name)
    with mock.patch("subprocess.run", side_effect=OSError("permission denied")):
        rc = spec.main([spec.subcommands[0], spec.binary_flag, fake])
    captured = capsys.readouterr()
    assert rc == 2
    assert "failed to execute" in captured.err.lower()
