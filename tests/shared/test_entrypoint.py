#!/usr/bin/env python3
"""Tests for da-tools entrypoint.py — CLI dispatcher logic.

pytest style：使用 plain assert + capsys fixture。

涵蓋:
  1. COMMAND_MAP 完整性 vs build.sh / release-tools.yaml
  2. 分派層不注入 --prometheus；每個 --prometheus 由 add_prometheus_arg 宣告後備（#3528）
  3. print_usage() / --version 輸出
  4. run_tool() 錯誤處理
  5. main() subcommand dispatch
  6. help 輸出格式驗證
  7. COMMAND_MAP ↔ help text 一致性
"""

import os
import re
import sys

import pytest

import entrypoint  # noqa: E402  (path set by conftest.py)
import _lib_exitcodes  # noqa: E402  (scripts/tools on sys.path via conftest)
from _lint_helpers import parse_build_sh_tools, BUILD_EXEMPT
from _platform_fs import symlink_or_skip  # noqa: E402

DA_TOOLS_DIR = os.path.join(
    os.path.dirname(__file__), os.pardir, os.pardir,
    'components', "da-tools", "app",
)


# ── Command Map Consistency ────────────────────────────────────────


class TestCommandMapConsistency:
    """COMMAND_MAP 必須涵蓋 build.sh / CI 中列出的所有工具。"""

    def test_command_map_covers_build_tools(self):
        """build.sh TOOL_FILES 中每個非豁免的 .py 都有對應 COMMAND_MAP 項目。"""
        build_tools = parse_build_sh_tools()
        assert build_tools, "無法解析 build.sh TOOL_FILES（檔案不存在或格式異常）"
        mapped_scripts = set(entrypoint.COMMAND_MAP.values())
        build_py = {f for f in build_tools if f.endswith(".py")} - BUILD_EXEMPT
        missing = build_py - mapped_scripts
        assert missing == set(), f"build.sh 有但 COMMAND_MAP 沒有: {missing}"

    def test_command_map_scripts_in_build_sh(self):
        """COMMAND_MAP 每個 .py value 都必須在 build.sh TOOL_FILES 中。

        防止新增 COMMAND_MAP 條目但忘記加入 build.sh 導致 Docker image crash。
        """
        build_tools = parse_build_sh_tools()
        assert build_tools, "無法解析 build.sh TOOL_FILES"
        cm_scripts = set(entrypoint.COMMAND_MAP.values())
        missing = cm_scripts - build_tools
        assert missing == set(), (
            f"COMMAND_MAP 指向的腳本不在 build.sh TOOL_FILES 中: {missing}。"
            f" Docker image 中對應命令會 crash。"
        )

    def test_command_map_values_are_py_files(self):
        """COMMAND_MAP 所有 value 都以 .py 結尾。"""
        for cmd, script in entrypoint.COMMAND_MAP.items():
            assert script.endswith(".py"), f"'{cmd}' 映射到非 .py: {script}"

    def test_command_map_keys_are_kebab_case(self):
        """所有 command 名稱使用 kebab-case 格式。"""
        for cmd in entrypoint.COMMAND_MAP:
            assert re.match(r'^[a-z][a-z0-9-]*$', cmd), \
                f"'{cmd}' 不是 kebab-case"

    def test_no_duplicate_scripts(self):
        """不同 command 不映射到同一個 script。"""
        scripts = list(entrypoint.COMMAND_MAP.values())
        assert len(scripts) == len(set(scripts)), \
            "COMMAND_MAP 有重複的 script 映射"


# ── $PROMETHEUS_URL is the tool's, not the dispatcher's (#3528) ──────


def _captured_parsers():
    import importlib.util
    script = os.path.join(os.path.dirname(__file__), os.pardir, os.pardir,
                          "scripts", "tools", "lint", "check_cli_contract.py")
    spec = importlib.util.spec_from_file_location("check_cli_contract_for_entrypoint", script)
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    captured, _unscoreable, blind, faults = gate.introspect_parsers()
    assert not faults and not blind, (faults, blind)
    return captured


class TestDispatcherInjectsNothing:
    """The dispatcher hands a tool its argv as typed (#3528). It used to append
    `--prometheus $PROMETHEUS_URL`, which argparse then judged: an unusable env
    stopped every run, paths that never query Prometheus included. The env is
    now read by the tool (`_lib_io.prometheus_url`) where it queries."""

    @pytest.mark.parametrize("command, argv", [
        ("threshold-recommend", ["--config-dir", "conf.d", "--dry-run"]),
        ("diagnose", ["db-a"]),
        ("discover-mappings", ["--job", "mysql"]),
        ("discover-mappings", ["--endpoint", "http://exporter:9104/metrics"]),
        ("alert-quality", ["--json"]),
    ])
    @pytest.mark.parametrize("env", ["http://env:9090", "ftp://bad"])
    def test_argv_reaches_the_tool_untouched(self, monkeypatch, command, argv, env):
        monkeypatch.setenv("PROMETHEUS_URL", env)
        seen = {}

        def fake_run_tool(script, args):
            seen["script"], seen["args"] = script, list(args)
        monkeypatch.setattr(entrypoint, "run_tool", fake_run_tool)
        monkeypatch.setattr(sys, "argv", ["da-tools", command, *argv])
        entrypoint.main()
        assert seen == {"script": entrypoint.COMMAND_MAP[command], "args": argv}

    def test_the_dispatcher_has_no_injection_left(self):
        assert not hasattr(entrypoint, "inject_prometheus_env")
        assert not hasattr(entrypoint, "PROMETHEUS_COMMANDS")


class TestEveryPrometheusFlagDeclaresItsFallback:
    """With nothing injected, a `--prometheus` the reader may leave out (the
    help and README §6.1 say $PROMETHEUS_URL stands in) must be one the tool
    itself falls back for: made by `_lib_io.add_prometheus_arg`, which marks
    the action (`env_fallback`) — read off the real parsers, not a list."""

    #: `--prometheus` flags whose absence means "do not ask Prometheus", not
    #: "use $PROMETHEUS_URL" — they never had the dispatcher's fallback and
    #: must not gain one (it would turn a skipped check into a query).
    NO_FALLBACK = {
        "operator-check": "optional target-health check; absent = skipped",
        "runtime-audit": "one of two sources with --runtime-json; absent = no live source",
    }

    def test_every_prometheus_flag_is_made_by_add_prometheus_arg(self):
        import argparse
        found, undeclared = [], []

        def walk(command, parser):
            for action in parser._actions:
                if isinstance(action, argparse._SubParsersAction):
                    for name, sub in action.choices.items():
                        walk(f"{command} {name}", sub)
                elif "--prometheus" in action.option_strings:
                    found.append(command)
                    if getattr(action, "env_fallback", None) != "PROMETHEUS_URL" \
                            or action.required:
                        undeclared.append(command)
        for command, model in sorted(_captured_parsers().items()):
            walk(command, model.parser)
        assert len(found) >= 15, f"only {len(found)} --prometheus flags seen; the walk is blind"
        assert sorted(undeclared) == sorted(self.NO_FALLBACK), (
            "--prometheus not made by add_prometheus_arg (no $PROMETHEUS_URL fallback "
            f"declared, or required), beyond NO_FALLBACK: "
            f"{sorted(set(undeclared) - set(self.NO_FALLBACK))}; listed but now declared: "
            f"{sorted(set(self.NO_FALLBACK) - set(undeclared))}")

    def test_discover_mappings_keeps_endpoint_and_prometheus_exclusive(self):
        """issue 1513's group stays: one source or the other. Neither given is
        allowed now — Prometheus at $PROMETHEUS_URL, else localhost."""
        parser = _captured_parsers()["discover-mappings"].parser
        groups = [g for g in parser._mutually_exclusive_groups
                  if {"--endpoint", "--prometheus"} <= {o for a in g._group_actions
                                                        for o in a.option_strings}]
        assert len(groups) == 1 and not groups[0].required, groups


# ── Version Display ────────────────────────────────────────────────


class TestVersionDisplay:
    """--version 讀取 VERSION 檔案。"""

    def test_version_file_exists(self):
        """VERSION 檔案必須存在於 da-tools app 目錄。"""
        version_path = os.path.join(DA_TOOLS_DIR, "VERSION")
        assert os.path.isfile(version_path), f"VERSION 檔案不存在: {version_path}"

    def test_version_is_semver(self):
        """VERSION 內容必須是合法 semver 字串。"""
        version_path = os.path.join(DA_TOOLS_DIR, "VERSION")
        with open(version_path, encoding="utf-8") as f:
            ver = f.read().strip()
        assert re.match(r'^[0-9]+\.[0-9]+\.[0-9]+$', ver), \
            f"VERSION '{ver}' 不是合法 semver"

    def test_print_version_with_file(self, capsys, tmp_path):
        """_print_version(tools_dir) 讀該目錄 VERSION 並印 'da-tools <ver>' + exit 0。"""
        (tmp_path / "VERSION").write_text("9.9.9\n", encoding="utf-8")
        with pytest.raises(SystemExit) as exc_info:
            entrypoint._print_version(str(tmp_path))
        assert exc_info.value.code == 0
        assert capsys.readouterr().out.strip() == "da-tools 9.9.9"

    def test_print_version_dev_fallback(self, capsys, tmp_path):
        """VERSION 缺檔時 _print_version 印 'da-tools (dev)' + exit 0。"""
        with pytest.raises(SystemExit) as exc_info:
            entrypoint._print_version(str(tmp_path))
        assert exc_info.value.code == 0
        assert capsys.readouterr().out.strip() == "da-tools (dev)"


# ── _t i18n helper ─────────────────────────────────────────────────


class TestI18nHelper:
    """_t() 依偵測語言挑選 zh/en 訊息變體。

    _t 在 body 內讀 module-level _LANG（非 default-arg 綁定），所以
    monkeypatch entrypoint._LANG 可測 zh 路徑；monkeypatch 自動還原，
    不會洩漏到其他測試的 en-default 假設。
    """

    def test_returns_en_by_default(self, monkeypatch):
        """_LANG='en' 時回傳英文變體。"""
        monkeypatch.setattr(entrypoint, "_LANG", "en")
        assert entrypoint._t("中文", "english") == "english"

    def test_returns_zh_when_lang_zh(self, monkeypatch):
        """_LANG='zh' 時回傳中文變體。"""
        monkeypatch.setattr(entrypoint, "_LANG", "zh")
        assert entrypoint._t("中文", "english") == "中文"


# ── _configure_std_utf8 ────────────────────────────────────────────


class TestConfigureStdUtf8:
    """_configure_std_utf8() best-effort：缺 reconfigure 不爆，有則呼叫 utf-8。"""

    def test_no_raise_when_stream_lacks_reconfigure(self, monkeypatch):
        """stream 無 reconfigure 屬性時靜默 no-op（pytest capture 情境）。"""
        class _Plain:
            pass
        monkeypatch.setattr(sys, "stdout", _Plain())
        monkeypatch.setattr(sys, "stderr", _Plain())
        entrypoint._configure_std_utf8()  # 不應 raise

    def test_reconfigures_both_streams_when_available(self, monkeypatch):
        """有 reconfigure 時對 stdout+stderr 各以 utf-8/replace 呼叫一次。"""
        calls = []

        class _Fake:
            def reconfigure(self, **kw):
                calls.append(kw)

        monkeypatch.setattr(sys, "stdout", _Fake())
        monkeypatch.setattr(sys, "stderr", _Fake())
        entrypoint._configure_std_utf8()
        assert calls == [
            {"encoding": "utf-8", "errors": "replace"},
            {"encoding": "utf-8", "errors": "replace"},
        ]

    def test_swallows_reconfigure_errors(self, monkeypatch):
        """reconfigure 拋 OSError 時被吞掉（best-effort，不致命）。"""
        class _Boom:
            def reconfigure(self, **kw):
                raise OSError("nope")

        monkeypatch.setattr(sys, "stdout", _Boom())
        monkeypatch.setattr(sys, "stderr", _Boom())
        entrypoint._configure_std_utf8()  # 不應 raise

    def test_swallows_reconfigure_attribute_error(self, monkeypatch):
        """reconfigure 拋 AttributeError 時被吞掉（補滿 (AttributeError, OSError) tuple）。"""
        class _AttrBoom:
            def reconfigure(self, **kw):
                raise AttributeError("unexpected")

        monkeypatch.setattr(sys, "stdout", _AttrBoom())
        monkeypatch.setattr(sys, "stderr", _AttrBoom())
        entrypoint._configure_std_utf8()  # 不應 raise


# ── run_tool error handling ────────────────────────────────────────


class TestRunToolErrors:
    """run_tool() 在 script 不存在時正確退出。"""

    def test_missing_script_exits(self):
        """不存在的 script 應 sys.exit(2)——呼叫端錯誤，不是「有發現」的 1（#1406）。"""
        with pytest.raises(SystemExit) as exc_info:
            entrypoint.run_tool("nonexistent_tool_xyz.py", [])
        assert exc_info.value.code == 2
        assert exc_info.value.code == entrypoint.EXIT_CALLER_ERROR

    def test_missing_script_lists_searched_paths(self, capsys):
        """錯誤訊息應列出所有掃過的 paths（local-dev fallback transparency）。"""
        with pytest.raises(SystemExit):
            entrypoint.run_tool("nonexistent_tool_xyz.py", [])
        err = capsys.readouterr().err
        # Primary path (Docker image / TOOLS_DIR) always searched first
        assert "components" in err or "Searched paths" in err or "已搜尋" in err
        assert "nonexistent_tool_xyz.py" in err


# ── run_tool exit-code pass-through ────────────────────────────────


class TestRunToolExitPassthrough:
    """run_tool() 原封不動透傳工具自己的結束碼（#1406 只改分派層，不改工具）。

    工具以 exec_module 在同一行程內當 __main__ 執行，其 sys.exit(N) 以
    SystemExit(N) 直接竄出 run_tool——沒有任何重映射。用假工具把 0 / 1 / 2
    各自鎖住，避免日後在 run_tool 加 try/except 把 EXIT_VIOLATION 或
    EXIT_CALLER_ERROR 折疊掉。run_tool 只攔 Exception（#3090 的最後防線），
    SystemExit 與 KeyboardInterrupt 一律原樣竄出。
    """

    @staticmethod
    def _fake_tool(tmp_path, monkeypatch, body):
        script = tmp_path / "fake_tool_xyz.py"
        script.write_text(body, encoding="utf-8")
        monkeypatch.setattr(entrypoint, "_resolve_script_path",
                            lambda name: (str(script), [str(script)]))
        # run_tool 會改寫 sys.argv；經 monkeypatch 走一遍讓它自動還原
        monkeypatch.setattr(sys, "argv", ["da-tools"])
        return script

    @pytest.mark.parametrize("code", [0, 1, 2])
    def test_sys_exit_code_passes_through(self, tmp_path, monkeypatch, code):
        """工具 sys.exit(0/1/2) → run_tool 竄出同碼的 SystemExit。"""
        self._fake_tool(tmp_path, monkeypatch, f"import sys\nsys.exit({code})\n")
        with pytest.raises(SystemExit) as exc_info:
            entrypoint.run_tool("fake_tool_xyz.py", ["--flag"])
        assert exc_info.value.code == code

    @pytest.mark.real_crash_exit_code
    def test_uncaught_exception_becomes_caller_error(self, tmp_path, monkeypatch, capsys):
        """工具拋出未捕捉例外 → run_tool 是最後防線：印 Traceback 與一行
        ERROR，以 EXIT_CALLER_ERROR (2) 結束（#3090；之前原樣竄出、直譯器
        預設 rc 1，與「有發現」撞號）。"""
        monkeypatch.setattr(_lib_exitcodes, "_crash_exit_code",
                            _lib_exitcodes.EXIT_CALLER_ERROR)
        self._fake_tool(tmp_path, monkeypatch, "raise RuntimeError('boom')\n")
        with pytest.raises(SystemExit) as exc_info:
            entrypoint.run_tool("fake_tool_xyz.py", [])
        assert exc_info.value.code == 2
        err = capsys.readouterr().err
        assert "Traceback" in err
        assert err.rstrip().splitlines()[-1] == \
            "ERROR: unexpected internal error (RuntimeError): boom"

    @pytest.mark.real_crash_exit_code
    def test_tool_crash_exit_code_is_honoured(self, tmp_path, monkeypatch, capsys):
        """工具自己 set_crash_exit_code(1) → run_tool 用它，不蓋成 2（機制測試；
        tenant_verify 在 #3393 前是這樣用的，現在沒有工具呼叫它）。"""
        monkeypatch.setattr(_lib_exitcodes, "_crash_exit_code",
                            _lib_exitcodes.EXIT_CALLER_ERROR)
        self._fake_tool(tmp_path, monkeypatch,
                        "import _lib_exitcodes\n"
                        "_lib_exitcodes.set_crash_exit_code(1)\n"
                        "raise RuntimeError('boom')\n")
        with pytest.raises(SystemExit) as exc_info:
            entrypoint.run_tool("fake_tool_xyz.py", [])
        assert exc_info.value.code == 1
        assert "ERROR: unexpected internal error (RuntimeError): boom" in \
            capsys.readouterr().err

    def test_keyboard_interrupt_propagates_unwrapped(self, tmp_path, monkeypatch):
        """Ctrl-C 不是內部錯誤：原樣竄出，交給直譯器自己的處理。"""
        self._fake_tool(tmp_path, monkeypatch, "raise KeyboardInterrupt\n")
        with pytest.raises(KeyboardInterrupt):
            entrypoint.run_tool("fake_tool_xyz.py", [])

    def test_fallback_crash_line_matches_the_ssot(self):
        """entrypoint 不能 import _lib_*，所以 crash 那一行在這裡有一份鏡像；
        兩份必須逐字相同（含多行訊息壓成一行、無訊息、超長截斷）。"""
        class Unprintable(Exception):
            def __str__(self):
                raise RuntimeError("no")

        for exc in (RuntimeError("boom"), KeyError("k"), ValueError(),
                    OSError("a\nb\tc"), RuntimeError("x" * 2000), Unprintable()):
            assert entrypoint._crash_message(exc) == _lib_exitcodes.crash_message(exc)

    def test_the_fd_filling_is_the_ssots_byte_for_byte(self):
        """#3558：entrypoint 不能 import _lib_*，`sanitize_std_fds`（fd 0–2
        關閉者以唯讀 /dev/null 補齊）在這裡有一份鏡像；兩份原始碼必須逐字相同——
        不是「行為相近」：任一份改了另一份沒跟，這裡就紅。"""
        import inspect
        mine = inspect.getsource(entrypoint.sanitize_std_fds)
        assert mine == inspect.getsource(_lib_exitcodes.sanitize_std_fds)
        assert "os.O_RDONLY" in mine  # anti-vacuity: the function, not an empty stub

    def test_main_fills_the_fds_before_anything_else(self):
        """`main()` 的第一個呼叫就是補 fd：在它之前沒有任何輸出或開檔。"""
        import ast
        import inspect
        body = ast.parse(inspect.getsource(entrypoint.main)).body[0].body
        first = body[0].value
        assert isinstance(first, ast.Call) and first.func.id == "sanitize_std_fds"

    @pytest.mark.parametrize("raw", [
        None, "70", "3", "255", "070", "2", "1", "0", "256", "-5", "+70", " 70",
        "", "abc", "７０"])
    def test_test_crash_code_rule_matches_the_ssot(self, monkeypatch, raw):
        """DA_TEST_CRASH_EXIT_CODE（#3092）的解析規則在這裡也有一份鏡像；
        兩份對同一個值必須給出同一個結果。"""
        assert entrypoint.TEST_CRASH_EXIT_CODE_ENV == _lib_exitcodes.TEST_CRASH_EXIT_CODE_ENV
        if raw is None:
            monkeypatch.delenv(entrypoint.TEST_CRASH_EXIT_CODE_ENV, raising=False)
        else:
            monkeypatch.setenv(entrypoint.TEST_CRASH_EXIT_CODE_ENV, raw)
        assert entrypoint._crash_exit_code_from_env() == \
            _lib_exitcodes.crash_exit_code_from_env()

    def test_normal_return_is_exit_zero(self, tmp_path, monkeypatch):
        """工具正常返回（不呼叫 sys.exit）→ run_tool 正常返回 → 直譯器 rc 0。"""
        self._fake_tool(tmp_path, monkeypatch, "X = 1\n")
        assert entrypoint.run_tool("fake_tool_xyz.py", []) is None


# ── _resolve_script_path: local-dev fallback ──────────────────────


class TestResolveScriptPathFallback:
    """_resolve_script_path() Docker-image path + local-dev source fallback。

    Docker image: build.sh flattens scripts into TOOLS_DIR → primary lookup hits.
    Local dev: scripts at scripts/tools/{ops,dx,}/ → fallback walks repo root.
    """

    def test_resolves_existing_source_script(self):
        """check-alert exists at scripts/tools/ops/check_alert.py — fallback finds it."""
        path, searched = entrypoint._resolve_script_path("check_alert.py")
        assert path is not None, f"failed to resolve check_alert.py; searched={searched}"
        assert path.endswith(os.path.join("scripts", "tools", "ops", "check_alert.py")) or \
               path.endswith(os.path.join("components", "da-tools", "app", "check_alert.py"))

    def test_resolves_three_new_v280_tools(self):
        """v2.8.0 PR #422/#424/#431 new tools live at scripts/tools/ops/."""
        for script in ("state_reconcile.py", "rule_pack_diff.py",
                       "silencer_drift_check.py"):
            path, _ = entrypoint._resolve_script_path(script)
            assert path is not None, f"failed to resolve {script}"

    def test_returns_none_for_missing(self):
        """Truly missing script returns None + non-empty searched list."""
        path, searched = entrypoint._resolve_script_path("definitely_missing_xyz.py")
        assert path is None
        assert len(searched) >= 1  # at least TOOLS_DIR was tried

    def test_searched_includes_tools_dir_first(self):
        """First search path is always TOOLS_DIR (Docker image layout primary)."""
        _, searched = entrypoint._resolve_script_path("definitely_missing_xyz.py")
        assert searched[0].startswith(entrypoint.TOOLS_DIR)

    def test_find_repo_root_is_three_levels_above_the_app_dir(self, tmp_path):
        """<root>/components/da-tools/app → <root>, with or without a .git (#2852)."""
        app = tmp_path / "components" / "da-tools" / "app"
        app.mkdir(parents=True)
        assert entrypoint._find_repo_root(str(app)) == str(tmp_path)

    def test_find_repo_root_ignores_a_git_off_that_structure(self, tmp_path):
        """A .git above a path that is not components/da-tools/app → None.

        The old rule walked up to it; #2852 measured that running an outer
        project's tool when this tree was vendored.
        """
        (tmp_path / ".git").write_text("gitdir: /elsewhere\n", encoding="utf-8")
        nested = tmp_path / "a" / "b" / "c"
        nested.mkdir(parents=True)
        assert entrypoint._find_repo_root(str(nested)) is None


# ── print_usage ────────────────────────────────────────────────────


class TestPrintUsage:
    """print_usage() 輸出驗證。"""

    def test_usage_exits_zero(self):
        """print_usage 應 sys.exit(0)。"""
        with pytest.raises(SystemExit) as exc_info:
            entrypoint.print_usage()
        assert exc_info.value.code == 0


# ── main() routing ─────────────────────────────────────────────────


class TestMainRouting:
    """main() subcommand dispatch 測試。"""

    def test_unknown_command_exits(self, monkeypatch, cli_argv):
        """未知 command 應 sys.exit(2)——呼叫端錯誤（#1406）。

        之前是 1，與各子命令「有發現」的 EXIT_VIOLATION 撞碼：打錯子命令、或
        image 的移動 tag 尚未收錄該子命令（CHANGELOG 記錄的週跑 threshold-govern
        CronJob），在消費端看起來與「工具跑完並找到東西」同形，且 stdout 為空。
        """
        cli_argv("da-tools", "nonexistent-xyz")
        with pytest.raises(SystemExit) as exc_info:
            entrypoint.main()
        assert exc_info.value.code == 2
        assert exc_info.value.code == entrypoint.EXIT_CALLER_ERROR

    def test_unknown_command_stdout_empty(self, capsys, cli_argv):
        """未知 command 的訊息全部走 stderr，stdout 為空（不污染 --json 消費端）。"""
        cli_argv("da-tools", "nonexistent-xyz")
        with pytest.raises(SystemExit):
            entrypoint.main()
        out, err = capsys.readouterr()
        assert out == ""
        assert err.strip()

    def test_missing_script_in_image_exits_two(self, capsys, monkeypatch, cli_argv):
        """已註冊子命令但 image 缺對應腳本 → rc 2 + stderr 點名腳本檔（#1406）。

        模擬 build.sh TOOL_FILES 漏打包：COMMAND_MAP 有、_resolve_script_path
        找不到（連 local-dev fallback 也沒有）。
        """
        monkeypatch.setitem(entrypoint.COMMAND_MAP, "ghost-cmd", "ghost_tool_xyz.py")
        monkeypatch.setattr(
            entrypoint, "_resolve_script_path",
            lambda name: (None, [os.path.join(entrypoint.TOOLS_DIR, name)]))
        cli_argv("da-tools", "ghost-cmd", "--whatever")
        with pytest.raises(SystemExit) as exc_info:
            entrypoint.main()
        assert exc_info.value.code == 2
        out, err = capsys.readouterr()
        assert out == ""
        assert "ghost_tool_xyz.py" in err

    @pytest.mark.parametrize("code", [0, 1, 2])
    def test_dispatched_tool_exit_passes_through(self, tmp_path, monkeypatch,
                                                 cli_argv, code):
        """main() 端到端：子命令自己的 0 / 1 / 2 原封透傳，分派層不重映射。"""
        script = tmp_path / "fake_tool_xyz.py"
        script.write_text(f"import sys\nsys.exit({code})\n", encoding="utf-8")
        monkeypatch.setitem(entrypoint.COMMAND_MAP, "fake-cmd", "fake_tool_xyz.py")
        monkeypatch.setattr(entrypoint, "_resolve_script_path",
                            lambda name: (str(script), []))
        cli_argv("da-tools", "fake-cmd")
        with pytest.raises(SystemExit) as exc_info:
            entrypoint.main()
        assert exc_info.value.code == code

    def test_unknown_command_stderr(self, capsys, cli_argv):
        """未知 command 的 stderr 含命令名 + 錯誤前綴。

        守護 _t 折疊後的 unknown-command block（原本只測 exit code，
        en stderr 文字未被 pin）。OR-form 兼容 zh/en（測試環境預設 en）。
        """
        cli_argv("da-tools", "nonexistent-xyz")
        with pytest.raises(SystemExit):
            entrypoint.main()
        err = capsys.readouterr().err
        assert "nonexistent-xyz" in err
        assert "Unknown command" in err or "未知命令" in err

    def test_help_exits_zero(self, monkeypatch, cli_argv):
        """--help 應 sys.exit(0)。"""
        cli_argv("da-tools", "--help")
        with pytest.raises(SystemExit) as exc_info:
            entrypoint.main()
        assert exc_info.value.code == 0

    def test_no_args_exits_zero(self, monkeypatch, cli_argv):
        """無參數時顯示 usage 並 sys.exit(0)。"""
        cli_argv("da-tools")
        with pytest.raises(SystemExit) as exc_info:
            entrypoint.main()
        assert exc_info.value.code == 0

    def test_version_flag_exits_zero(self, monkeypatch, cli_argv):
        """--version 應 sys.exit(0)。"""
        cli_argv("da-tools", "--version")
        with pytest.raises(SystemExit) as exc_info:
            entrypoint.main()
        assert exc_info.value.code == 0


# ── Help text consistency ────────────────────────────────────────


class TestHelpTextConsistency:
    """help text 與 COMMAND_MAP 的一致性。"""

    # v2.4.0: 清空豁免清單。所有 COMMAND_MAP 命令都必須出現在 help text 中。
    # v2.3.0 曾豁免 validate-config，但該命令已在 help text 中。
    _HELP_EXEMPT: set = set()

    def test_all_commands_in_english_help(self):
        """COMMAND_MAP 所有 command（排除豁免項）出現在英文 help text 中。"""
        help_text = entrypoint._build_help_text("en")
        for cmd in entrypoint.COMMAND_MAP:
            if cmd in self._HELP_EXEMPT:
                continue
            assert cmd in help_text, f"'{cmd}' 不在英文 help text 中"

    def test_all_commands_in_chinese_help(self):
        """COMMAND_MAP 所有 command（排除豁免項）出現在中文 help text 中。"""
        help_text = entrypoint._build_help_text("zh")
        for cmd in entrypoint.COMMAND_MAP:
            if cmd in self._HELP_EXEMPT:
                continue
            assert cmd in help_text, f"'{cmd}' 不在中文 help text 中"

    def test_help_text_bilingual_available(self):
        """英文和中文 help text 均可產生且長度 > 100。"""
        en = entrypoint._build_help_text("en")
        zh = entrypoint._build_help_text("zh")
        assert len(en) > 100
        assert len(zh) > 100

    def test_help_text_contains_usage_pattern(self):
        """Help text 包含 usage 格式說明。"""
        for lang in ["en", "zh"]:
            help_text = entrypoint._build_help_text(lang)
            assert "da-tools" in help_text
            assert "<command>" in help_text


# ── CI Workflow Sync ───────────────────────────────────────────────


class TestCIWorkflowSync:
    """release-tools.yaml TOOLS 陣列必須匹配 build.sh TOOL_FILES。"""

    @staticmethod
    def _parse_tools_from_file(filepath, start_marker, end_marker):
        """從 script 中擷取 tool 檔名。"""
        tools = []
        with open(filepath, encoding="utf-8") as f:
            content = f.read()
        in_block = False
        for line in content.splitlines():
            stripped = line.strip()
            if start_marker in stripped:
                in_block = True
                continue
            if in_block and end_marker in stripped:
                break
            if in_block and stripped and not stripped.startswith("#"):
                name = stripped.strip("\"'(),")
                if name.endswith(".py") or name.endswith(".yaml"):
                    tools.append(name)
        return set(tools)

    def test_ci_matches_build_sh(self):
        """release-tools.yaml TOOLS 必須是 build.sh TOOL_FILES 的超集。"""
        repo_root = os.path.join(os.path.dirname(__file__), os.pardir, os.pardir)
        build_sh = os.path.join(repo_root, "components", "da-tools", "app", "build.sh")
        ci_yaml = os.path.join(repo_root, ".github", "workflows", "release-tools.yaml")

        if not os.path.isfile(build_sh) or not os.path.isfile(ci_yaml):
            pytest.skip("build.sh 或 release-tools.yaml 不存在")

        build_tools = self._parse_tools_from_file(build_sh, "TOOL_FILES=(", ")")
        ci_tools = self._parse_tools_from_file(ci_yaml, "TOOLS=(", ")")

        missing_in_ci = build_tools - ci_tools
        assert missing_in_ci == set(), \
            f"build.sh 有但 CI workflow 沒有: {missing_in_ci}"


# ── bump_docs tools rule coverage ──────────────────────────────────


class TestBumpDocsToolsRuleCoverage:
    """bump_docs.py tools rules 必須涵蓋 da-tools README 標題版號。"""

    def test_readme_title_rule_exists(self):
        """bump_docs tools rules 有規則匹配 da-tools README H1 標題版號。

        Behavioural check — keyed on the rule *pattern*, not its ``desc``
        string. The previous version asserted ``"version header" in desc``,
        which broke the moment the rule was (correctly) renamed to
        "da-tools README title version". A pattern-based check survives
        future desc edits and tests what actually matters: that the
        ``# da-tools (vX.Y.Z)`` title format is covered by some rule.
        """
        import re
        import bump_docs
        rules = bump_docs._build_rules()
        sample = "# da-tools (v9.9.9)"
        matched = [
            r for r in rules["tools"]
            if "pattern" in r and re.search(r["pattern"], sample, re.MULTILINE)
        ]
        assert len(matched) >= 1, (
            "bump_docs 沒有涵蓋 da-tools README 標題版號 "
            "(# da-tools (vX.Y.Z)) 的規則"
        )


def test_dispatcher_mirror_of_the_exit_code_ssot_does_not_drift():
    """entrypoint.py mirrors EXIT_CALLER_ERROR as a literal on purpose (it is
    zero-import from _lib_*); this is the one place the mirror is checked
    against the SSOT so a renumbering there cannot leave the dispatcher on
    the old value (blind review, #1406)."""
    assert entrypoint.EXIT_CALLER_ERROR == _lib_exitcodes.EXIT_CALLER_ERROR


# ── Invoked through the PATH symlink (issue 1444) ─────────────────


def test_dispatch_through_the_path_symlink_finds_the_tools(tmp_path):
    """`da-tools <cmd>` via the image's /usr/local/bin/da-tools symlink must work.

    The Dockerfile links /usr/local/bin/da-tools -> /opt/da-tools/entrypoint.py,
    and every GitLab job `da-tools init --ci gitlab` emits clears the image
    ENTRYPOINT (issue 1408) and calls `da-tools <cmd>` from the shell — i.e.
    THROUGH that link. Resolving the tools directory without following the
    link looks for the scripts next to the link and finds none, so every such
    job exits 2 before doing anything (measured on a real GitLab runner, and on
    the published v2.9.0 image). This mirrors the image layout: the dispatcher
    and a tool flat in one directory, the link elsewhere, cwd somewhere else.
    """
    import shutil
    import subprocess

    opt = tmp_path / "opt" / "da-tools"
    opt.mkdir(parents=True)
    shutil.copy(os.path.join(DA_TOOLS_DIR, "entrypoint.py"), opt / "entrypoint.py")
    script = entrypoint.COMMAND_MAP["validate-config"]
    (opt / script).write_text('print("STUB-RAN")\n', encoding="utf-8")
    bin_dir = tmp_path / "usr" / "local" / "bin"
    bin_dir.mkdir(parents=True)
    link = bin_dir / "da-tools"
    try:
        symlink_or_skip(opt / "entrypoint.py", link)
    except (OSError, NotImplementedError):
        pytest.skip("this filesystem cannot create symlinks")
    work = tmp_path / "builds"
    work.mkdir()
    p = subprocess.run(
        [sys.executable, str(link), "validate-config"], cwd=work,
        capture_output=True, encoding="utf-8", errors="replace", timeout=60,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert p.returncode == 0, p.stderr
    assert "STUB-RAN" in p.stdout, p.stdout + p.stderr


# ── Crash through the real dispatcher process (#3090) ────────────


def _image_layout(tmp_path, tool_body):
    """entrypoint.py and one tool flat in a directory, like the image — and,
    like a raw local-dev run, NO _lib_* on sys.path."""
    import shutil

    opt = tmp_path / "opt"
    opt.mkdir()
    shutil.copy(os.path.join(DA_TOOLS_DIR, "entrypoint.py"), opt / "entrypoint.py")
    script = entrypoint.COMMAND_MAP["validate-config"]
    (opt / script).write_text(tool_body, encoding="utf-8")
    return opt / "entrypoint.py"


def _run_dispatcher(entry, *args):
    import subprocess

    return subprocess.run(
        [sys.executable, str(entry), "validate-config", *args],
        cwd=entry.parent, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=60,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )


@pytest.mark.real_crash_exit_code
def test_dispatcher_turns_a_crash_of_a_tool_without_the_lib_into_rc_2(tmp_path):
    """The tool never imports _lib_exitcodes (so no excepthook is installed):
    only run_tool's own catch stands between the crash and rc 1."""
    entry = _image_layout(
        tmp_path, "print('partial')\nraise RuntimeError('boom')\n")
    p = _run_dispatcher(entry)
    assert p.returncode == 2, p.stderr
    assert "Traceback" in p.stderr
    assert p.stderr.rstrip().splitlines()[-1] == \
        "ERROR: unexpected internal error (RuntimeError): boom"
    assert p.stdout == "partial\n"


@pytest.mark.real_crash_exit_code
def test_dispatcher_honours_the_tools_crash_exit_code(tmp_path):
    """Tool loads _lib_exitcodes from the source tree and sets its own crash
    code; the dispatcher's catch hands the crash to it instead of using 2."""
    tools = os.path.abspath(os.path.join(
        os.path.dirname(__file__), os.pardir, os.pardir, "scripts", "tools"))
    entry = _image_layout(
        tmp_path,
        f"import sys\nsys.path.insert(0, {tools!r})\n"
        "import _lib_exitcodes\n_lib_exitcodes.set_crash_exit_code(1)\n"
        "raise RuntimeError('boom')\n")
    p = _run_dispatcher(entry)
    assert p.returncode == 1, p.stderr
    assert "ERROR: unexpected internal error (RuntimeError): boom" in p.stderr
    assert p.stderr.count("ERROR: unexpected internal error") == 1, p.stderr


def test_dispatcher_fallback_ends_at_the_test_crash_code(tmp_path):
    """#3092: the fallback (a tool without the lib) reads DA_TEST_CRASH_EXIT_CODE
    like _lib_exitcodes does — red when the mirror stops reading it."""
    entry = _image_layout(tmp_path, "raise RuntimeError('boom')\n")
    p = _run_dispatcher(entry)
    assert p.returncode == 70, p.stderr
    assert "ERROR: unexpected internal error (RuntimeError): boom" in p.stderr


@pytest.mark.parametrize("code", [0, 1, 2])
def test_dispatcher_passes_tool_system_exit_through(tmp_path, code):
    entry = _image_layout(tmp_path, f"import sys\nsys.exit({code})\n")
    p = _run_dispatcher(entry)
    assert p.returncode == code, p.stderr
    assert "ERROR: unexpected internal error" not in p.stderr
