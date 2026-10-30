"""#3425：repo 內每一次 `go build` 都經 `scripts/ops/go_build.sh`，建置旗標只有這一個來源。

兩半：

1. **呼叫點**（結構化掃描，不是整行字串比對）：`git ls-files` 裡的 Makefile／*.mk、
   shell（*.sh、*.bash、shebang 是 sh/bash 的無副檔名檔）、Dockerfile*、YAML、Python、Go，
   找「實際執行」的 `go build`——shell 指令以 shlex 斷詞、去註解後看命令位置的字；
   Dockerfile 只看 RUN；Makefile 看 recipe 與變數定義；YAML 解析後看每個字串值；Python
   以 AST 看開頭兩項是 `go`、`build` 的 list/tuple 字面值與交給 `os.system`／`subprocess.*`
   的字串；Go 看 `exec.Command(…)` 的引數。純註解、文件、給人看的訊息字串不算。
   `go_build.sh` 本身以外一律紅，豁免只有 `_EXEMPT` 列的、各附理由。
2. **產物**（行為）：測試 session 建出的 binary 量 `go version -m` 與 ELF section——
   `CGO_ENABLED=0`、`-trimpath`、沒有符號表與 DWARF（`-s -w`）。只看呼叫點的話，腳本本身
   被改壞（例如掉了 CGO_ENABLED=0）照樣綠；cgo 版 da-guard 在產生器的 RLIMIT_DATA 下量到的
   是 thread 數，理由與量測在腳本頂部。
"""
from __future__ import annotations

import ast
import json
import os
import re
import shlex
import shutil
import struct
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
SCRIPT = "scripts/ops/go_build.sh"

#: 不經 go_build.sh 的 `go build`，逐項附理由。新增前先確認真的拿不到腳本。
_EXEMPT = {
    # 旗標的單一來源本身。
    SCRIPT: "the one place the flags are written",
    # 本檔的必響對照（test_an_unstripped_build_is_caught）刻意不經腳本建一支。
    "tests/ops/test_go_build_ssot.py": "the must-fire control builds the unscripted shape",
}

# ── shell ────────────────────────────────────────────────────────────────────

_SEP_CHARS = "();<>|&\n"
#: 其後的字仍在命令位置。
_PREFIX_WORDS = {"then", "do", "else", "elif", "if", "while", "until", "!", "{", "time",
                 "exec", "env", "command", "nice", "nohup", "sudo", "xargs"}
_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_SHELLS = {"sh", "bash", "ash", "dash", "zsh"}


def _is_go(word: str) -> bool:
    return word == "go" or word.endswith("/go")


def _shell_tokens(text: str) -> list[str]:
    # 註解吃到行尾時連換行一起吃掉，換行加倍才留得下命令邊界。
    text = text.replace("\\\n", " ").replace("\n", "\n\n")
    lx = shlex.shlex(text, posix=True, punctuation_chars=_SEP_CHARS)
    lx.whitespace = " \t\r"
    lx.whitespace_split = True  # `GOOS=${X}` 是一個字，不是 `GOOS=` `$` `{` …
    return list(lx)


def shell_runs_go_build(text: str) -> bool:
    """*text*（shell 指令）是否在命令位置執行 `go build`，含 `sh -c '…'` 裡的。"""
    try:
        tokens = _shell_tokens(text)
    except ValueError:  # 不成對的引號：逐行斷，仍不成就以寬鬆的字面形狀判（寧紅勿漏）
        out = False
        for line in text.splitlines():
            try:
                out |= shell_runs_go_build_tokens(_shell_tokens(line))
            except ValueError:
                code = line.split(" #", 1)[0]
                out |= bool(re.search(r"(^|[;&|(]|\b(then|do|exec|env)\b)\s*"
                                      r"([A-Za-z_]\w*=\S*\s+)*(\S*/)?go\s+build\b", code.strip()))
        return out
    return shell_runs_go_build_tokens(tokens)


def shell_runs_go_build_tokens(tokens: list[str]) -> bool:
    at_cmd = True
    prev_cmd = None
    for i, tok in enumerate(tokens):
        if tok and all(c in _SEP_CHARS for c in tok):
            at_cmd, prev_cmd = True, None
            continue
        if at_cmd:
            if _ASSIGN.match(tok) or tok in _PREFIX_WORDS:
                continue
            if _is_go(tok) and i + 1 < len(tokens) and tokens[i + 1] == "build":
                return True
            at_cmd, prev_cmd = False, tok
            continue
        # `sh -c '<script>'`：腳本本身也是 shell。
        if prev_cmd in _SHELLS and tok == "-c" and i + 1 < len(tokens):
            if shell_runs_go_build(tokens[i + 1]):
                return True
    return False


# ── Makefile ─────────────────────────────────────────────────────────────────

_MAKE_VAR = re.compile(r"^(?:export\s+)?[A-Za-z_][A-Za-z0-9_.-]*\s*(?::{1,3}|\+|\?|!)?=\s*(.*)$")


def make_runs_go_build(text: str) -> bool:
    """recipe 行（tab 開頭，去掉 @-+ 前綴）與變數定義的右邊當 shell 看。"""
    chunks, buf = [], ""
    for line in text.splitlines():
        cur = buf + line
        if cur.endswith("\\"):
            buf = cur[:-1] + " "
            continue
        buf = ""
        if cur.startswith("\t"):
            chunks.append(cur.lstrip("\t").lstrip("@-+ "))
        elif not cur.lstrip().startswith("#"):
            m = _MAKE_VAR.match(cur.strip())
            if m:
                chunks.append(m.group(1))
    return any(shell_runs_go_build(c) for c in chunks)


# ── Dockerfile ───────────────────────────────────────────────────────────────

def dockerfile_runs_go_build(text: str) -> bool:
    instructions, buf = [], ""
    for raw in text.splitlines():
        s = raw.strip()
        if s.startswith("#"):
            continue
        if s.endswith("\\"):
            buf += s[:-1] + " "
            continue
        instructions.append(buf + s)
        buf = ""
    if buf:
        instructions.append(buf)
    for ins in instructions:
        word, _, rest = ins.partition(" ")
        if word.upper() != "RUN":
            continue
        rest = rest.strip()
        while rest.startswith("--"):  # RUN --mount=… --network=…
            rest = rest.partition(" ")[2].strip()
        if rest.startswith("["):
            try:
                argv = json.loads(rest)
            except ValueError:
                argv = None
            if isinstance(argv, list):
                if len(argv) >= 2 and _is_go(str(argv[0])) and argv[1] == "build":
                    return True
                if len(argv) >= 3 and Path(str(argv[0])).name in _SHELLS and argv[1] == "-c":
                    if shell_runs_go_build(str(argv[2])):
                        return True
                continue
        if shell_runs_go_build(rest):
            return True
    return False


# ── YAML ─────────────────────────────────────────────────────────────────────

def _strings(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for v in node.values():
            yield from _strings(v)
    elif isinstance(node, list):
        for v in node:
            yield from _strings(v)


def yaml_runs_go_build(text: str) -> bool:
    """每個字串值都當 shell 看（`run:`、`entry:`、`command:` 都在其中）。解析不了的
    檔（Helm 樣板等）以整份原文當 shell 看：量不到時寧紅勿漏。"""
    try:
        docs = list(yaml.safe_load_all(text))
    except yaml.YAMLError:
        return shell_runs_go_build(text)
    return any(shell_runs_go_build(s) for d in docs for s in _strings(d))


# ── Python ───────────────────────────────────────────────────────────────────

_PY_SHELL_CALLS = {"system", "popen", "getoutput", "getstatusoutput",
                   "run", "call", "check_call", "check_output", "Popen"}


def _py_word(node) -> "str | None":
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return node.id
    return None


def python_runs_go_build(text: str) -> bool:
    """開頭兩項是 `go`（字面值或名為 go 的變數）、`build` 的 list/tuple 字面值；
    或交給 os.system／subprocess.* 的字串字面值裡的 shell 指令。"""
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if isinstance(node, (ast.List, ast.Tuple)) and len(node.elts) >= 2:
            first, second = _py_word(node.elts[0]), _py_word(node.elts[1])
            if first is not None and _is_go(first) and second == "build" \
                    and isinstance(node.elts[1], ast.Constant):
                return True
        if isinstance(node, ast.Call) and node.args:
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
            arg = node.args[0]
            if name in _PY_SHELL_CALLS and isinstance(arg, ast.Constant) \
                    and isinstance(arg.value, str) and shell_runs_go_build(arg.value):
                return True
    return False


# ── Go ───────────────────────────────────────────────────────────────────────

_GO_TOKEN = re.compile(r'//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\\n])*"|`[^`]*`|[A-Za-z_][\w.]*|\S',
                       re.S)


def _go_args(tokens: list[str], start: int) -> list[str]:
    """`(` 之後到對應 `)` 的頂層引數，各以 token 串接（字串字面值保留引號）。"""
    args, cur, depth = [], [], 0
    for tok in tokens[start:]:
        if tok in "([{":
            depth += 1
        elif tok in ")]}":
            if depth == 0:
                break
            depth -= 1
        if tok == "," and depth == 0:
            args.append(" ".join(cur))
            cur = []
            continue
        cur.append(tok)
    if cur:
        args.append(" ".join(cur))
    return args


def _go_str(arg: str) -> "str | None":
    """引數恰是一個字串字面值時的值；其他（運算式、串接、變數）回 None。"""
    m = re.fullmatch(r'"(?:\\.|[^"\\\n])*"|`[^`]*`', arg)
    if m is None:
        return None
    if arg[0] == "`":
        return arg[1:-1]
    try:
        return json.loads(arg)
    except ValueError:  # Go 才有的跳脫（\x、\a…）：值不重要，只要不是 go/build
        return arg[1:-1]


def go_runs_go_build(text: str) -> bool:
    """`exec.Command(…)`／`exec.CommandContext(ctx, …)` 的引數：第一個是 "go"（或以 /go
    結尾的字面值、名字含 go 的變數）、第二個是 "build"；或 `sh -c "<script>"`。"""
    tokens = [t for t in _GO_TOKEN.findall(text) if not t.startswith(("//", "/*"))]
    for i, tok in enumerate(tokens):
        if tok not in ("exec.Command", "exec.CommandContext") or tokens[i + 1:i + 2] != ["("]:
            continue
        args = _go_args(tokens, i + 2)
        if tok == "exec.CommandContext":
            args = args[1:]
        if len(args) < 2:
            continue
        first, second = _go_str(args[0]), _go_str(args[1])
        if second == "build":
            if first is not None and _is_go(first):
                return True
            if first is None and "go" in args[0].lower():
                return True
        if first is not None and Path(first).name in _SHELLS and second == "-c" and len(args) > 2:
            script = _go_str(args[2])
            if script is not None and shell_runs_go_build(script):
                return True
    return False


# ── 檔案分類 ─────────────────────────────────────────────────────────────────

def classify(rel: str, head: str) -> "str | None":
    name = rel.rsplit("/", 1)[-1]
    if name == "Makefile" or name.endswith(".mk"):
        return "make"
    if name.endswith(".dockerignore"):  # BuildKit's per-Dockerfile ignore file
        return None
    if name.startswith("Dockerfile") or name.endswith(".Dockerfile"):
        return "docker"
    suffix = os.path.splitext(name)[1]
    if suffix in (".sh", ".bash"):
        return "shell"
    if suffix in (".yml", ".yaml"):
        return "yaml"
    if suffix == ".py":
        return "python"
    if suffix == ".go":
        return "go"
    if not suffix and re.match(r"#!\S*(/|\s)(ba|da|a)?sh\b", head):
        return "shell"
    return None


_DETECTORS = {
    "make": make_runs_go_build, "docker": dockerfile_runs_go_build,
    "shell": shell_runs_go_build, "yaml": yaml_runs_go_build,
    "python": python_runs_go_build, "go": go_runs_go_build,
}


def _tracked_files() -> list[str]:
    # 還沒 add 的新檔也算（--others --exclude-standard）：本機寫完就該紅，不等 commit。
    proc = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                          cwd=REPO, capture_output=True,
                          check=True, timeout=120)
    return [p for p in proc.stdout.decode("utf-8").split("\0") if p]


def _scan() -> "tuple[list[str], dict[str, int]]":
    hits, counted = [], {k: 0 for k in _DETECTORS}
    for rel in _tracked_files():
        path = REPO / rel
        if not path.is_file() or path.is_symlink():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        kind = classify(rel, text[:64])
        if kind is None:
            continue
        counted[kind] += 1
        if _DETECTORS[kind](text):
            hits.append(rel)
    return hits, counted


def test_every_go_build_goes_through_the_script():
    hits, counted = _scan()
    # 每類都真的讀到了檔，量到的「沒有」才有意義。
    assert all(counted.values()), counted
    stray = sorted(set(hits) - set(_EXEMPT))
    assert not stray, (
        "these files run `go build` directly; build through scripts/ops/go_build.sh "
        "(tests/_go_build.py for Python), so the flags stay in one place (#3425):\n"
        + "".join(f"  {p}\n" for p in stray))
    # 豁免名單不留死項：每一項都得真的有 `go build`。
    dead = sorted(set(_EXEMPT) - set(hits))
    assert not dead, f"_EXEMPT lists files that no longer run `go build`: {dead}"


# ── 偵測器的必響／必不響（突變驗證） ─────────────────────────────────────────

@pytest.mark.parametrize("kind,text", [
    ("shell", "go build ./cmd/x\n"),
    ("shell", "cd app && CGO_ENABLED=0 GOOS=linux go build -o out .\n"),
    ("shell", "for a in b; do\n  (cd x && go build \\\n    -o y .)\ndone\n"),
    ("shell", "# a comment\nexec /usr/local/go/bin/go build .\n"),
    ("shell", "bash -c 'go build ./...'\n"),
    ("make", "x:\n\t@cd a && go build -o b .\n"),
    ("make", "x:\n\t@cd a && \\\n\t\tgo build -o b .\n"),
    ("make", "GOB := CGO_ENABLED=0 go build\n"),
    ("docker", "FROM a\nRUN CGO_ENABLED=0 go build \\\n    -o /x .\n"),
    ("docker", 'FROM a\nRUN ["go", "build", "-o", "/x", "."]\n'),
    ("docker", "FROM a\nRUN --mount=type=cache,target=/c go build .\n"),
    ("yaml", "jobs:\n  a:\n    steps:\n      - run: |\n          cd x\n          go build -o y .\n"),
    ("yaml", "repos:\n  - hooks:\n      - entry: bash -c 'cd x && go build .'\n"),
    ("python", 'subprocess.run(["go", "build", "-o", out, "."])\n'),
    ("python", 'subprocess.run([go, "build", "-o", out, "."])\n'),
    ("python", 'subprocess.run(("go", "build"))\n'),
    ("python", 'os.system("cd x && go build .")\n'),
    ("go", 'cmd := exec.Command("go", "build", "-o", out, ".")\n'),
    ("go", 'cmd := exec.CommandContext(ctx, "go", "build", ".")\n'),
    ("go", 'cmd := exec.Command(goBin, "build", ".")\n'),
    ("go", 'cmd := exec.Command("sh", "-c", "go build .")\n'),
])
def test_the_detectors_fire(kind, text):
    assert _DETECTORS[kind](text), text


@pytest.mark.parametrize("kind,text", [
    ("shell", "# go build ./cmd/x\n"),
    ("shell", 'echo "run go build to rebuild"\n'),
    ("shell", "echo go build\n"),
    ("shell", "sh scripts/ops/go_build.sh app ./cmd/x out\n"),
    ("shell", "docker build -t x .\n"),
    ("make", "# the go build cache\nx:\n\t@# a go build comment\n\t@echo hi\n"),
    ("make", "x:\n\t@$(GO_BUILD) app ./cmd/x out\n"),
    ("docker", "FROM a\n# RUN go build .\nRUN sh /opt/go_build.sh . ./cmd/x /x\n"),
    ("yaml", "a: the go build of da-guard\nb: [go, build]\n"),
    ("yaml", "steps:\n  - run: |\n      # go build here once\n      make da-guard-build\n"),
    ("python", '# subprocess.run(["go", "build"])\nmsg = "go build ./cmd/x"\n'),
    ("python", 'f"CGO_ENABLED=0 go build -o /usr/local/bin/{bn}"\n'),
    ("python", 'argv = ["retry", "--", "go", "build", "./..."]\n'),
    ("python", 'subprocess.run(go_build_argv(app, "./cmd/x", out))\n'),
    ("go", '// exec.Command("go", "build")\nx := "go build"\n'),
    ("go", 'exec.Command("go", "test", "./...")\n'),
    ("go", 'exec.Command("sh", filepath.Join(root, "scripts", "ops", "go_build.sh"), app)\n'),
    ("go", 'exec.Command("docker", "build", ".")\n'),
])
def test_the_detectors_stay_quiet(kind, text):
    assert not _DETECTORS[kind](text), text


@pytest.mark.parametrize("rel,head,want", [
    ("Makefile", "", "make"), ("ci/x.mk", "", "make"),
    ("a/Dockerfile", "", "docker"), ("a/Dockerfile.dev", "", "docker"),
    ("a/b.sh", "", "shell"), ("bin/tool", "#!/usr/bin/env bash\n", "shell"),
    ("bin/tool", "#!/bin/sh\n", "shell"), ("bin/tool", "#!/usr/bin/env python3\n", None),
    (".github/workflows/x.yml", "", "yaml"), ("a.py", "", "python"), ("a_test.go", "", "go"),
    ("README.md", "", None),
])
def test_classify(rel, head, want):
    assert classify(rel, head) == want


# ── 產物：測試 session 建出的 binary 是出貨的形狀 ─────────────────────────────

def _build_settings(binary: str) -> dict[str, str]:
    """`go version -m <binary>` 的 `build` 列，key → value。"""
    go = shutil.which("go")
    if go is None:
        pytest.fail("`go` is not on PATH: the build settings cannot be read")
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


def _elf_section_names(binary: str) -> list[str]:
    """ELF64 little-endian 的 section 名稱（linux/amd64、arm64 的 Go binary）。"""
    with open(binary, "rb") as f:
        data = f.read()
    assert data[:4] == b"\x7fELF" and data[4] == 2 and data[5] == 1, \
        f"{binary} is not an ELF64 LE binary: the strip check reads ELF sections"
    shoff, = struct.unpack_from("<Q", data, 0x28)
    shentsize, shnum, shstrndx = struct.unpack_from("<HHH", data, 0x3A)
    headers = [struct.unpack_from("<IIQQQQ", data, shoff + i * shentsize) for i in range(shnum)]
    str_off, str_size = headers[shstrndx][4], headers[shstrndx][5]
    table = data[str_off:str_off + str_size]
    names = [table[h[0]:table.index(b"\0", h[0])].decode() for h in headers]
    assert ".text" in names, names  # 解析對了的必響對照
    return names


def _assert_shipped_shape(binary: str) -> None:
    settings = _build_settings(binary)
    assert settings.get("CGO_ENABLED") == "0", (binary, settings)
    assert settings.get("-trimpath") == "true", (binary, settings)
    names = _elf_section_names(binary)
    unstripped = [n for n in names if n == ".symtab" or n.startswith((".debug", ".zdebug"))]
    assert not unstripped, f"{binary} keeps {unstripped}: not built with -ldflags '-s -w'"


def test_the_session_binaries_have_the_shipped_shape(
        da_guard_binary, da_read_binary, da_crdecode_binary):
    for binary in (da_guard_binary, da_read_binary, da_crdecode_binary):
        _assert_shipped_shape(binary)


def test_the_session_wide_binaries_have_the_shipped_shape():
    """`pytest_configure` 設進環境變數的那三支（子行程用的就是它們）。"""
    for env in ("VIBE_TEST_DA_GUARD_BINARY", "VIBE_TEST_DA_READ_BINARY",
                "VIBE_TEST_DA_CRDECODE_BINARY"):
        binary = os.environ.get(env, "")
        assert binary and os.path.isfile(binary), f"${env} names no binary: {binary!r}"
        _assert_shipped_shape(binary)


def test_an_unstripped_build_is_caught(tmp_path):
    """必響對照：同一個套件不經腳本、以 go 的預設旗標建，上面的判定要不成立。"""
    go = shutil.which("go")
    if go is None:
        pytest.fail("`go` is not on PATH")
    out = tmp_path / "da-crdecode"
    proc = subprocess.run([go, "build", "-buildvcs=false", "-o", str(out), "./cmd/da-crdecode"],
                          cwd=REPO / "components" / "threshold-exporter" / "app",
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=600)
    assert proc.returncode == 0, proc.stderr
    with pytest.raises(AssertionError):
        _assert_shipped_shape(str(out))


@pytest.mark.parametrize("target", [
    "da-guard-build", "da-read-build", "da-crdecode-build", "tenant-api-build"])
def test_make_targets_build_through_the_script(target):
    make = shutil.which("make")
    if make is None:
        pytest.fail(f"`make` is not on PATH: the {target} recipe cannot be read")
    env = {k: v for k, v in os.environ.items()
           if k not in ("DA_GUARD_BINARY", "DA_READ_BINARY", "DA_CRDECODE_BINARY")}
    proc = subprocess.run([make, "-n", "-s", "-C", str(REPO), target],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", check=False, timeout=60, env=env)
    assert proc.returncode == 0, proc.stderr
    builds = [ln for ln in proc.stdout.splitlines() if SCRIPT in ln]
    assert len(builds) == 1, proc.stdout
