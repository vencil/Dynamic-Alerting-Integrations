"""da-guard exit 4 叫人跑的產生器指令，照抄就能跑（#3420）。

da-guard 在樹上有路由／domain policy 時回 exit 4，報告與 stderr 叫使用者跑
`da-tools generate-routes ...`。#3420 前那行少了產生器必填的 `--config-dir`，照抄
rc 2。這裡不比字串：取 da-guard binary 真的印出的指令，`shlex.split` 後去掉
`da-tools generate-routes`，交給產生器自己的 argparse parser
（`generate_alertmanager_routes._build_parser`）解析——不得 SystemExit，且解析出的
`config_dir` 就是傳給 da-guard 的那個值（路徑含空白、引號與開頭的 `-`，驗 shell
quoting）。help 裡的同一指令（佔位字 `<dir>`）也照同一 parser 解析。

da-guard 由 conftest 的 session fixture 以 `go build` 建出；建不起來就 fail、不 skip。
"""
from __future__ import annotations

import re
import shlex
import subprocess
from pathlib import Path

import pytest

import entrypoint  # noqa: E402  (path set by conftest.py)
import generate_alertmanager_routes as gar

_PREFIX = ["da-tools", "generate-routes"]
# A Markdown code span holding the command: the fence is any run of backticks.
_SPAN = re.compile(r"(`+)(da-tools generate-routes .*?)\1")
# The help quotes it in single quotes, one per mention.
_HELP = re.compile(r"'(da-tools generate-routes [^']*)'")

_TREE = {
    "_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
    # `_routing` is what only the route generator judges: exit 4.
    "tx.yaml": ('tenants:\n  tx:\n    mysql_connections: "50"\n'
                '    _routing:\n      receiver: {type: webhook, url: "https://t.example/h"}\n'),
}


def _parse_as_generator(command: str):
    """The generator's own parse of `command` minus the da-tools prefix."""
    argv = shlex.split(command)
    assert argv[:2] == _PREFIX, f"not a generate-routes command: {command!r}"
    try:
        return gar._build_parser().parse_args(argv[2:])
    except SystemExit as exc:
        pytest.fail(f"generate-routes refuses the command da-guard prints (rc {exc.code}): {command!r}")


def test_da_tools_runs_this_parser_for_generate_routes():
    """The contract below parses with this script's parser: it is the one
    `da-tools generate-routes` dispatches to."""
    assert entrypoint.COMMAND_MAP["generate-routes"] == "generate_alertmanager_routes.py"
    assert Path(gar.__file__).name == "generate_alertmanager_routes.py"


@pytest.mark.parametrize("name, absolute", [
    ("my conf.d", False),       # relative, as the caller typed it, with a space
    ("it's conf.d", True),      # absolute, with a single quote
    ("-x conf.d", False),       # a value argparse would take for an option
    ("a`b $HOME", False),       # a backtick (Markdown fence) and shell expansion
])
def test_exit4_hint_parses_as_the_generator_reads_it(da_guard_binary, tmp_path, name, absolute):
    conf_d = tmp_path / name
    conf_d.mkdir()
    for rel, body in _TREE.items():
        (conf_d / rel).write_text(body, encoding="utf-8")
    config_dir = str(conf_d) if absolute else name

    proc = subprocess.run([da_guard_binary, "--config-dir", config_dir], cwd=tmp_path,
                          capture_output=True, text=True, encoding="utf-8", check=False, timeout=120)
    assert proc.returncode == 4, f"want exit 4 (undetermined), got {proc.returncode}:\n{proc.stderr}"

    from_stderr = [m.group(2) for m in _SPAN.finditer(proc.stderr)]
    from_report = [m.group(2) for m in _SPAN.finditer(proc.stdout)]
    assert len(from_stderr) == 1 and from_report == from_stderr, (
        f"want one command, the same in stderr and the report:\nstderr: {proc.stderr}\nreport: {proc.stdout}")

    args = _parse_as_generator(from_stderr[0])
    assert args.config_dir == config_dir
    assert args.validate and args.strict


def test_help_names_the_command_with_config_dir(da_guard_binary):
    proc = subprocess.run([da_guard_binary, "-h"], capture_output=True, text=True,
                          encoding="utf-8", check=False, timeout=120)
    assert proc.returncode == 0, proc.stderr
    commands = _HELP.findall(proc.stderr)
    assert len(commands) == 2, f"want the command at --required-fields and at exit 4:\n{proc.stderr}"
    for command in commands:
        args = _parse_as_generator(command)
        assert args.config_dir == "<dir>" and args.validate and args.strict
