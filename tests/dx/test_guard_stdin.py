"""The session-guards read stdin as UTF-8, whatever the locale (#3598).

A Claude Code hook gets its payload as UTF-8 JSON on stdin. `sys.stdin.read()`
decodes with the locale's encoding — cp950 on a zh-TW Windows host — so a
command or path in Chinese failed to decode: `preflight_bash`'s `sed -i`
guard crashed to the hook protocol's non-blocking 1 instead of blocking, and
the guards that swallow a bad payload (`paths_map`, `skill_usage`,
`stop_evidence`, `session-init`) silently did nothing.

`PYTHONIOENCODING=cp950` stands in for the cp950 host (not measured on a real
Windows host); in-process, a cp950 text stream over UTF-8 bytes does.
"""
from __future__ import annotations

import ast
import importlib.util
import io
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
GUARDS = REPO_ROOT / "scripts" / "session-guards"
READER = "_guardlib.py"

# PYTHONUNBUFFERED off and the locale's encoding pinned to cp950: the host.
_CP950 = dict({k: v for k, v in os.environ.items() if k != "PYTHONUNBUFFERED"},
              PYTHONIOENCODING="cp950")
_UTF8 = dict(_CP950, PYTHONIOENCODING="utf-8")
_ENCODINGS = {"cp950": _CP950, "utf-8": _UTF8}


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), GUARDS / name)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run(guard: str, stdin: bytes, env: dict[str, str], *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(GUARDS / guard), *args], input=stdin,
                          capture_output=True, timeout=60, env=env)


def _payload(**fields) -> bytes:
    return json.dumps(dict(hook_event_name="PreToolUse", session_id=f"s-{uuid.uuid4().hex}",
                           **fields), ensure_ascii=False).encode("utf-8")


# ── the gate: one reader ─────────────────────────────────────────────────

_STDIN_NAMES = ("stdin", "__stdin__")
_FD_OPENERS = {("os", "read"), ("os", "fdopen"), ("io", "open"), (None, "open")}


def _is_fd0_open(node: ast.Call) -> bool:
    func = node.func
    key = ((func.value.id if isinstance(func.value, ast.Name) else "?", func.attr)
           if isinstance(func, ast.Attribute) else
           (None, func.id) if isinstance(func, ast.Name) else None)
    return (key in _FD_OPENERS and bool(node.args)
            and isinstance(node.args[0], ast.Constant) and node.args[0].value == 0)


def _readers_in(tree: ast.AST) -> list[int]:
    """Lines that reach stdin: `sys.stdin` (under any alias of `sys`),
    `from sys import stdin`, `getattr(sys, "stdin")`, `input()`, an open or
    read of fd 0, and `fileinput`."""
    sys_names = {"sys"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            sys_names |= {a.asname for a in node.names if a.name == "sys" and a.asname}
    lines = []
    for node in ast.walk(tree):
        hit = False
        if isinstance(node, ast.Attribute) and node.attr in _STDIN_NAMES:
            hit = isinstance(node.value, ast.Name) and node.value.id in sys_names
        elif isinstance(node, ast.ImportFrom):
            hit = (node.module == "sys" and any(a.name in _STDIN_NAMES for a in node.names)) \
                or node.module == "fileinput"
        elif isinstance(node, ast.Import):
            hit = any(a.name == "fileinput" for a in node.names)
        elif isinstance(node, ast.Call):
            f = node.func
            hit = (isinstance(f, ast.Name) and f.id == "input") or _is_fd0_open(node) or (
                isinstance(f, ast.Name) and f.id == "getattr" and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant) and node.args[1].value in _STDIN_NAMES)
        if hit:
            lines.append(node.lineno)
    return sorted(set(lines))


def _stdin_readers() -> list[str]:
    found = []
    for path in sorted(GUARDS.glob("*.py")):
        if path.name == READER:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        found += [f"{path.name}:{line}" for line in _readers_in(tree)]
    return found


def test_only_guardlib_reads_stdin():
    """⛔ A guard reaches stdin through `_guardlib` only: `sys.stdin` anywhere
    else in scripts/session-guards/ decodes with the locale (#3598)."""
    assert _stdin_readers() == [], (
        "read stdin with _guardlib.read_payload() / read_stdin_text(), "
        "not sys.stdin (#3598)")


@pytest.mark.parametrize("source", [
    "import sys\nraw = sys.stdin.read()\n",
    "import sys\nraw = sys.__stdin__.read()\n",
    "import sys as s\nraw = s.stdin.read()\n",
    "import sys\nfrom sys import stdin\n",
    "import sys\nraw = getattr(sys, 'stdin').read()\n",
    "import sys\nraw = input()\n",
    "import sys\nraw = open(0).read()\n",
    "import os\nraw = os.read(0, 10)\n",
    "import os\nraw = os.fdopen(0).read()\n",
    "import sys\nimport fileinput\n",
])
def test_the_gate_sees_a_reader(tmp_path, monkeypatch, source):
    """The gate's own control: every way to reach stdin it knows is named."""
    (tmp_path / "rogue.py").write_text(source, encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "GUARDS", tmp_path)
    assert _stdin_readers() == ["rogue.py:2"]


# ── the guards, on a cp950 host ──────────────────────────────────────────

_SED = "se" + "d -i s/a/b/ /workspaces/vibe-k8s-lab/中文.txt"  # split: the live guard reads this file's writes


@pytest.mark.parametrize("encoding", sorted(_ENCODINGS))
def test_preflight_blocks_sed_i_on_a_chinese_path(encoding):
    proc = _run("preflight_bash.py", _payload(tool_name="Bash", tool_input={"command": _SED}),
                _ENCODINGS[encoding])
    err = proc.stderr.decode("utf-8", "replace")
    assert proc.returncode == 2, err  # was 1 under cp950: a crash, which never blocks
    assert "BLOCKED" in err, err


@pytest.mark.parametrize("encoding", sorted(_ENCODINGS))
def test_paths_map_injects_for_a_payload_in_chinese(encoding, tmp_path):
    target = REPO_ROOT / "scripts" / "tools" / "_lib_exitcodes.py"
    env = dict(_ENCODINGS[encoding], XDG_CACHE_HOME=str(tmp_path), TMPDIR=str(tmp_path))
    proc = _run("paths_map.py", _payload(tool_name="Edit", cwd=str(REPO_ROOT), tool_input={
        "file_path": str(target), "old_string": "中文", "new_string": "x"}), env)
    assert proc.returncode == 0, proc.stderr
    assert b"_lib_exitcodes.py" in proc.stdout, proc.stderr  # was empty under cp950


@pytest.mark.parametrize("encoding", sorted(_ENCODINGS))
def test_stop_evidence_reads_a_chinese_message(encoding):
    proc = _run("stop_evidence.py", "說明一下設計取捨。\n".encode("utf-8"),
                _ENCODINGS[encoding], "--message-stdin")
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")  # was 1: a crash
    assert json.loads(proc.stdout)["ok"] is True


@pytest.mark.parametrize("stdin", ["<&-", "< /dev/null", "<<< ' '"])
def test_stop_evidence_names_a_missing_message(stdin):
    """No message was read (closed, empty, blank): a named 1, never `"ok":
    true` for a message nobody measured. `<&-` reads as empty input once
    #3602 binds a closed stdin, so the two must answer alike."""
    import shutil
    bash = shutil.which("bash") or pytest.skip("bash is not on PATH")
    proc = subprocess.run([bash, "-c", f'exec "$0" "$1" --message-stdin {stdin}', sys.executable,
                           str(GUARDS / "stop_evidence.py")], capture_output=True, timeout=60,
                          env=_UTF8)
    err = proc.stderr.decode("utf-8", "replace")
    assert proc.returncode == 1, err
    assert "--message-stdin: no message on stdin" in err
    assert proc.stdout == b""


def test_preflight_blocks_a_payload_with_a_bom():
    data = b"\xef\xbb\xbf" + _payload(tool_name="Bash", tool_input={"command": _SED})
    proc = _run("preflight_bash.py", data, _CP950)
    assert proc.returncode == 2, proc.stderr.decode("utf-8", "replace")


def test_stop_evidence_names_a_message_that_is_not_utf8():
    proc = _run("stop_evidence.py", "中文".encode("cp950"), _UTF8, "--message-stdin")
    err = proc.stderr.decode("utf-8", "replace")
    assert proc.returncode == 1, err  # the hook protocol's caller error: never blocks
    assert "--message-stdin: stdin is not UTF-8" in err and "Traceback" not in err, err


def _cp950_stdin(data: bytes) -> io.TextIOWrapper:
    """A stdin as a cp950 host gives one: text decoded as cp950, bytes beneath."""
    return io.TextIOWrapper(io.BytesIO(data), encoding="cp950")


def test_session_init_reads_the_session_id_on_a_cp950_host(monkeypatch):
    mod = _load("session-init.py")
    monkeypatch.setattr(sys, "stdin", _cp950_stdin(
        json.dumps({"session_id": "會話-1", "tool_name": "Bash",
                    "tool_input": {"command": "ls 中文"}}, ensure_ascii=False).encode("utf-8")))
    assert mod._payload_session_id() == "會話-1"  # was None: the marker fell back


# ── _guardlib.read_payload ───────────────────────────────────────────────

@pytest.fixture()
def gl():
    sys.path.insert(0, str(GUARDS))
    try:
        yield _load("_guardlib.py")
    finally:
        sys.path.remove(str(GUARDS))


def test_read_payload_decodes_utf8_under_a_cp950_stdin(gl, monkeypatch):
    monkeypatch.setattr(sys, "stdin", _cp950_stdin('{"a": "中文"}'.encode("utf-8")))
    assert gl.read_payload() == {"a": "中文"}


@pytest.mark.parametrize("data, why", [
    ("中文".encode("cp950"), "UnicodeDecodeError"),
    (b"not json", "JSONDecodeError"),
    (b"[1, 2]", "a JSON list, not an object"),
])
def test_read_payload_warns_and_allows_what_it_cannot_read(gl, monkeypatch, data, why):
    said: list[str] = []
    monkeypatch.setattr(sys, "stdin", _cp950_stdin(data))
    assert gl.read_payload(said.append) is None
    assert len(said) == 1 and why in said[0], said


@pytest.mark.parametrize("data", [b"", b"  \n"])
def test_read_payload_is_silent_for_no_payload(gl, monkeypatch, data):
    said: list[str] = []
    monkeypatch.setattr(sys, "stdin", _cp950_stdin(data))
    assert gl.read_payload(said.append) is None
    assert said == []


def test_read_payload_without_stdin_is_none(gl, monkeypatch):
    monkeypatch.setattr(sys, "stdin", None)
    assert gl.read_payload() is None
