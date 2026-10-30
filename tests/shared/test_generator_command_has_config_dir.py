"""照抄的 `da-tools generate-routes …` 指令一律帶 `--config-dir`（#3420）。

產生器的 `--config-dir` 是必填，少了它照抄就是 rc 2。#3420 前 da-guard 的 exit 4
提示、help 與十幾處文件都少了它，靠人 grep 才找到。這支掃 repo：凡是以
`da-tools generate-routes` 起頭、後面接旗標的指令（給人照抄的形狀），該指令必須含
`--config-dir`；`--help`／`-h` 例外。只寫 `generate-routes --validate --strict`
是把檢查當名詞，不在此列。

指令的範圍：在 Markdown 行內 code span 裡，就是那個 span；在 `.py`／`.go` 的字串
常數裡（code span 之外），到該行下一個 `"` 為止；其餘（fenced block、YAML、註解）
到行尾。同一行後面另外提到的旗標不算進前一條指令。

範圍：`docs/**/*.md`、`components/**/*.{md,go,py}`、`scripts/tools/**/*.py`、
`.github/**/*.{yml,yaml}`；排除 `docs/adr/**`（決策紀錄）、測試、生成物
（`components/tenant-api/docs/`）、`CHANGELOG.md` 與 `changelog.d/`（不在範圍內）。
da-guard 自己的指令只有一個來源（`generatorCommand`），由
`test_da_guard_generator_hint.py` 以產生器的 parser 驗。
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

_COMMAND = re.compile(r"da-tools\s+generate-routes(?=\s+-)")
_HELP = re.compile(r"(?<!\S)(--help|-h)(?!\S)")


def _in_scope(path: str) -> bool:
    if path.startswith("docs/adr/") or path.startswith("components/tenant-api/docs/"):
        return False
    name = path.rsplit("/", 1)[-1]
    if (path.startswith("tests/") or "/tests/" in path or name.endswith("_test.go")
            or name.startswith("test_") or name == "conftest.py"):
        return False
    if path.startswith("docs/"):
        return path.endswith(".md")
    if path.startswith("components/"):
        return path.endswith((".md", ".go", ".py"))
    if path.startswith("scripts/tools/"):
        return path.endswith(".py")
    if path.startswith(".github/"):
        return path.endswith((".yml", ".yaml"))
    return False


def missing_config_dir(path: str, text: str) -> list[str]:
    """`path:line: command` for each copyable generate-routes command in *text*
    without `--config-dir`."""
    in_string_file = path.endswith((".py", ".go"))
    out = []
    for n, line in enumerate(text.split("\n"), 1):
        for m in _COMMAND.finditer(line):
            if line[: m.start()].count("`") % 2 == 1:   # inside a code span
                end = line.find("`", m.end())
            elif in_string_file:                        # a string constant
                end = line.find('"', m.end())
            else:
                end = -1
            command = line[m.start(): end if end >= 0 else len(line)]
            if _HELP.search(command) or "--config-dir" in command:
                continue
            out.append(f"{path}:{n}: {command.strip()}")
    return out


def _tracked() -> list[str]:
    proc = subprocess.run(["git", "ls-files", "-z"], cwd=REPO_ROOT, capture_output=True,
                          check=True, timeout=60)
    return [p for p in proc.stdout.decode("utf-8").split("\0") if p]


def test_every_copyable_generate_routes_command_names_config_dir():
    files = [p for p in _tracked() if _in_scope(p)]
    assert len(files) > 100, f"scope matched only {len(files)} files: the walk is broken"
    misses, unreadable = [], []
    for p in files:
        try:
            text = (REPO_ROOT / p).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            unreadable.append(f"{p}: {exc}")
            continue
        misses += missing_config_dir(p, text)
    assert not unreadable, "files in scope that could not be read:\n" + "\n".join(unreadable)
    assert not misses, (
        "`da-tools generate-routes` with flags but no --config-dir (required: pasted as "
        "written it ends in rc 2, #3420):\n" + "\n".join(misses))


@pytest.mark.parametrize("path, line", [
    ("docs/x.md", "再跑 `da-tools generate-routes --validate --strict`。"),
    ("docs/x.md", "    da-tools generate-routes --validate --strict"),
    ("scripts/tools/x.py", '    "run `da-tools generate-routes --validate --strict` (3 > 1)\\n"'),
    ("scripts/tools/x.py", '    "run da-tools generate-routes --validate", "--config-dir"'),
    (".github/workflows/x.yml", "        run: da-tools generate-routes --strict --validate"),
])
def test_a_command_without_config_dir_is_named(path, line):
    assert missing_config_dir(path, line), line


@pytest.mark.parametrize("path, line", [
    # for-platform-engineers.md:274's shape: a complete command, then the
    # check named again further on the line.
    ("docs/x.md", "驗證：`da-tools generate-routes --config-dir conf.d/ --validate`（加 "
                  "`--strict` 轉 ERROR；`generate-routes --validate --strict` 與 …）"),
    ("docs/x.md", "交給 `generate-routes --validate --strict` 判"),          # the check as a noun
    ("docs/x.md", "`da-tools generate-routes --help`"),
    ("docs/x.md", "`da-tools generate-routes` 子命令"),                       # no flags
    ("docs/x.md", "    da-tools generate-routes \\"),                         # flags on the next line
    ("scripts/tools/x.py", '    "`da-tools generate-routes --config-dir "'),
])
def test_other_shapes_are_not_named(path, line):
    assert not missing_config_dir(path, line), line
