"""`_lib_kubectl` — the one place kubectl is started (`kubectl_run`, #3539)
and the one cluster reader on top of it (#3207, #3210).

A fake `kubectl` on PATH replays what kubectl 1.37.1 did against a real
kube-apiserver (measured 2026-10-24): an empty answer is rc 0 with an empty
List; a forbidden list is rc 1 **with an empty List still on stdout**; an
unreachable server is rc 1 with client-go log lines before kubectl's own
message; an unknown kind is rc 1.
"""
from __future__ import annotations

import json
import os
import stat
import signal
import subprocess
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "tools"))
import _lib_engine  # noqa: E402
import _lib_kubectl as lk  # noqa: E402
from _lib_exitcodes import CallerError  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import _kubectl_fake  # noqa: E402

_EMPTY = json.dumps({"apiVersion": "v1", "kind": "List", "items": []})


def _fake_kubectl(tmp_path, monkeypatch, rc, stdout="", stderr=""):
    """Put a `kubectl` on PATH that records its argv and answers as given."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (tmp_path / "out").write_text(stdout, encoding="utf-8")
    (tmp_path / "err").write_text(stderr, encoding="utf-8")
    script = bindir / "kubectl"
    script.write_text(
        f"#!{sys.executable}\n"
        "import pathlib, sys\n"
        f"d = pathlib.Path({str(tmp_path)!r})\n"
        "(d / 'argv').write_text('\\n'.join(sys.argv[1:]) + '\\n', encoding='utf-8')\n"
        "sys.stdout.write((d / 'out').read_text(encoding='utf-8'))\n"
        "sys.stderr.write((d / 'err').read_text(encoding='utf-8'))\n"
        f"sys.exit({rc})\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", str(bindir))
    return lambda: (tmp_path / "argv").read_text(encoding="utf-8").split("\n")[:-1]


def test_an_answer_is_its_items_and_the_argv_is_built_from_the_arguments(tmp_path, monkeypatch):
    argv = _fake_kubectl(tmp_path, monkeypatch, 0, json.dumps({"items": [{"metadata": {"name": "a"}}]}))
    got = lk.kubectl_list("secret", namespace="ns", selector="a=b", field_selector="metadata.name=a",
                          kubeconfig="/kc")
    assert got == [{"metadata": {"name": "a"}}]
    assert argv() == ["--kubeconfig", "/kc", "get", "secret", "-n", "ns", "-l", "a=b",
                      "--field-selector", "metadata.name=a", "-o", "json"]


def test_an_empty_answer_is_an_empty_list(tmp_path, monkeypatch):
    _fake_kubectl(tmp_path, monkeypatch, 0, _EMPTY)
    assert lk.kubectl_list("secret", namespace="ns", field_selector="metadata.name=x") == []


@pytest.mark.parametrize("stdout, stderr, why", [
    # forbidden: rc 1, and kubectl still prints an empty List on stdout
    (_EMPTY, 'Error from server (Forbidden): secrets "x" is forbidden: User "nobody" cannot list',
     "Forbidden"),
    # unreachable: client-go logs first; kubectl's own message is the last line
    ("", 'E1024 06:02:54 memcache.go:381] "Couldn\'t get current server API group list"\n'
         "The connection to the server 127.0.0.1:6999 was refused - did you specify the right host or port?",
     "The connection to the server"),
    ("", 'error: the server doesn\'t have a resource type "alertmanagerconfig"', "resource type"),
    ("", "", "no message on stderr"),
], ids=["forbidden-with-empty-list", "unreachable", "unknown-kind", "silent"])
def test_a_non_zero_exit_is_no_answer_whatever_stdout_says(tmp_path, monkeypatch, stdout, stderr, why):
    _fake_kubectl(tmp_path, monkeypatch, 1, stdout, stderr)
    with pytest.raises(lk.KubectlError) as ei:
        lk.kubectl_list("secret", namespace="ns")
    assert "(exit 1)" in str(ei.value) and why in str(ei.value)
    assert "memcache" not in str(ei.value)


@pytest.mark.parametrize("stdout", ["not json", "[]", '{"items": {}}', '{"items": [1]}', "{}"],
                         ids=["not-json", "a-list", "items-not-a-list", "item-not-an-object", "no-items"])
def test_rc_0_with_output_that_is_not_a_list_of_objects_is_no_answer(tmp_path, monkeypatch, stdout):
    _fake_kubectl(tmp_path, monkeypatch, 0, stdout)
    with pytest.raises(lk.KubectlError):
        lk.kubectl_list("secret")


def test_kubectl_not_on_path(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(lk.KubectlError, match="kubectl is not available"):
        lk.kubectl_list("secret")


_CANNOT_START = [
    # no execute permission: EACCES — the lookup names the file it may not run
    (0o644, "#!/bin/sh\necho {}\n", "is on PATH but is not executable"),
    # executable, but no #! and not a binary the kernel knows: ENOEXEC
    (0o755, "\x00\x01not a program\n", "Exec format error"),
    # on PATH, but its #! interpreter does not exist: ENOENT, not "not on PATH"
    (0o755, "#!/nonexistent/interpreter\n", "No such file or directory.*interpreter"),
]


def _unstartable_kubectl(tmp_path, monkeypatch, mode, body):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    exe = bindir / "kubectl"
    exe.write_text(body, encoding="utf-8")
    exe.chmod(mode)
    monkeypatch.setenv("PATH", str(bindir))
    return exe


@pytest.mark.parametrize("mode, body, why", _CANNOT_START,
                         ids=["eacces", "enoexec", "no-interpreter"])
def test_a_kubectl_that_cannot_be_started_is_no_answer(tmp_path, monkeypatch, mode, body, why):
    """#3534: found on PATH but not started is a named caller error (rc 2,
    reason kubectl_error), not a crash (internal_error)."""
    exe = _unstartable_kubectl(tmp_path, monkeypatch, mode, body)
    with pytest.raises(lk.KubectlError, match=f"cannot run kubectl: .*{why}") as ei:
        lk.kubectl_list("secret")
    assert str(exe) in str(ei.value)      # which file, not only that one failed


@pytest.mark.parametrize("verb, args, stdin_text", [
    ("apply", ["-f", "-"], "kind: ConfigMap\n"),
    ("patch", ["configmap", "c", "--type", "merge", "-p", "{}"], None),
    ("label", ["configmap", "c", "a=b"], None),
    ("delete", ["job", "j"], None),
    ("create", ["configmap", "c", "--dry-run=client", "-o", "yaml"], None),
], ids=["apply", "patch", "label", "delete", "create"])
@pytest.mark.parametrize("mode, body, why", _CANNOT_START,
                         ids=["eacces", "enoexec", "no-interpreter"])
def test_a_write_that_cannot_be_started_is_the_same_named_error(
        tmp_path, monkeypatch, mode, body, why, verb, args, stdin_text):
    """#3539: writes are started in the same place as reads — a kubectl that
    cannot be started is KubectlError for every verb, never an OSError the
    caller has to remember to catch."""
    _unstartable_kubectl(tmp_path, monkeypatch, mode, body)
    with pytest.raises(lk.KubectlError, match=f"`kubectl {verb}`: cannot run kubectl: .*{why}"):
        lk.kubectl_run(verb, args, timeout=5, stdin_text=stdin_text)


def test_a_failed_start_is_named_by_any_os_error(monkeypatch):
    """Every OSError raised while starting kubectl is no answer, whichever
    errno the kernel picks (a directory on PATH that cannot be entered,
    ENOMEM at fork) — not only the ones the tests above can stage."""
    def boom(*a, **kw):
        raise OSError(12, "Cannot allocate memory")
    _kubectl_fake.install(monkeypatch, boom)
    with pytest.raises(lk.KubectlError, match="cannot run kubectl: .*Cannot allocate memory"):
        lk.kubectl_list("secret")
    with pytest.raises(lk.KubectlError, match="cannot run kubectl: .*Cannot allocate memory"):
        lk.kubectl_run("apply", ["-f", "-"], timeout=5, stdin_text="x")


def test_no_answer_within_the_timeout(monkeypatch):
    def boom(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="kubectl", timeout=3)
    _kubectl_fake.install(monkeypatch, boom)
    with pytest.raises(lk.KubectlError, match="kubectl did not finish within 3s"):
        lk.kubectl_list("secret", timeout=3)


def test_a_write_out_of_time_says_only_that_it_did_not_finish(monkeypatch):
    """⛔ The whole message, pinned: a write that ran out of time may have
    reached the cluster, so the words may say it did not finish and nothing
    about what was or was not sent, applied or run."""
    def boom(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="kubectl", timeout=3)
    _kubectl_fake.install(monkeypatch, boom)
    for verb in ("apply", "patch", "label", "delete"):
        with pytest.raises(lk.KubectlError) as ei:
            lk.kubectl_run(verb, ["x"], timeout=3, stdin_text="m")
        assert str(ei.value) == f"`kubectl {verb}`: kubectl did not finish within 3s (rc 124)"


# ── kubectl_run: the one launcher, reads and writes (#3539) ─────────────────

def test_run_returns_kubectls_own_exit_status_for_the_caller_to_judge(tmp_path, monkeypatch):
    """A non-zero exit is an answer of kubectl's, not a failure to run it:
    `create --dry-run=client` refusing a key name is a finding on the
    caller's files, a write the cluster refused is the caller's to report."""
    argv = _fake_kubectl(tmp_path, monkeypatch, 1, "partial\n", "error: refused\n")
    proc = lk.kubectl_run("label", ["configmap", "c", "a=b"], timeout=5,
                          kubeconfig="/kc", context="ctx")
    assert (proc.returncode, proc.stdout, proc.stderr) == (1, "partial\n", "error: refused\n")
    assert argv() == ["--kubeconfig", "/kc", "--context", "ctx", "label", "configmap", "c", "a=b"]


#: Run in a child whose stdin is a real pipe: under pytest this process's
#: own fd 0 is the capture's, and "kubectl got nothing on stdin" would hold
#: whichever stdin kubectl was given.
_STDIN_PROBE = """
import json, sys
sys.path.insert(0, {tools!r})
import _lib_kubectl as lk
call = sys.argv[1]
if call == "run":
    out = lk.kubectl_run("apply", ["-f", "-"], timeout=20).stdout
elif call == "run-text":
    out = lk.kubectl_run("apply", ["-f", "-"], timeout=20, stdin_text="the manifest").stdout
elif call == "run-closed":
    out = lk.kubectl_run("apply", ["-f", "-"], timeout=20, inherit_stdin=False).stdout
elif call == "list":
    lk.kubectl_list("pods", timeout=20); out = open({seen!r}).read()
elif call == "list-inherit":
    lk.kubectl_list("pods", timeout=20, inherit_stdin=True); out = open({seen!r}).read()
elif call == "get":
    lk.kubectl_get("pod", "p", timeout=20); out = open({seen!r}).read()
elif call == "logs":
    out = lk.kubectl_logs("ns", "pod/p", timeout=20)
print(json.dumps(out))
"""


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell")
@pytest.mark.parametrize("call, kubectl_read", [
    # a write with no manifest keeps the tool's stdin: an exec credential
    # plugin that prompts on the terminal can be answered (as before #3539)
    ("run", "typed at the terminal"),
    ("run-text", "the manifest"),
    ("run-closed", ""),
    # the readers gave kubectl /dev/null before #3539 and still do …
    ("list", ""),
    ("get", ""),
    ("logs", ""),
    # … unless asked (drift-detect, which started kubectl itself before)
    ("list-inherit", "typed at the terminal"),
])
def test_which_stdin_kubectl_is_given(tmp_path, call, kubectl_read):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    seen = tmp_path / "seen"
    exe = bindir / "kubectl"
    # `apply` echoes its stdin; `get` records it and answers an empty list
    exe.write_text("#!/bin/sh\n"
                   f"if [ \"$1\" = get ]; then cat > {seen}; echo '{{\"items\": []}}'; "
                   "else cat; fi\n", encoding="utf-8")
    exe.chmod(0o755)
    tools = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "tools")
    probe = _STDIN_PROBE.format(tools=os.path.abspath(tools), seen=str(seen))
    env = dict(os.environ, PATH=f"{bindir}{os.pathsep}{os.environ['PATH']}")
    r = subprocess.run([sys.executable, "-c", probe, call], input="typed at the terminal",
                       capture_output=True, text=True, encoding="utf-8", env=env, timeout=120)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == kubectl_read


@pytest.mark.skipif(os.name == "nt", reason="POSIX signals")
def test_a_failed_read_carries_kubectls_whole_stderr(tmp_path, monkeypatch):
    """The message names kubectl's last stderr line; the lines before it
    (client-go's reason for an unreachable cluster) are on the error for a
    caller that prints them (drift-detect)."""
    stderr = "E1114 memcache.go:265] couldn't get current server API group list\nerror: refused\n"
    _fake_kubectl(tmp_path, monkeypatch, 1, "", stderr)
    with pytest.raises(lk.KubectlError) as ei:
        lk.kubectl_list("pods")
    assert ei.value.stderr == stderr and "memcache" not in str(ei.value)


def test_a_control_sequence_on_the_last_stderr_line_does_not_reach_the_message(tmp_path,
                                                                             monkeypatch):
    """CodeRabbit on #3539: the last stderr line goes into the error message;
    `failed\\x1b[2J` there cleared the operator's screen. The message shows
    `?`; `error.stderr` keeps kubectl's own bytes for a caller that wants them."""
    stderr = "failed\x1b[2J\x07"
    _fake_kubectl(tmp_path, monkeypatch, 1, "", stderr)
    with pytest.raises(lk.KubectlError) as ei:
        lk.kubectl_list("secret", namespace="ns")
    assert "failed?[2J?" in str(ei.value)
    assert "\x1b" not in str(ei.value) and "\x07" not in str(ei.value)
    assert ei.value.stderr == stderr


def test_printable_stderr_keeps_the_lines_and_drops_the_controls():
    assert lk.printable_stderr("a\x1b[2Jb\r\nc\td\x00\n") == "a?[2Jb\nc\td?"
    long = "x" * 5000
    shown = lk.printable_stderr(long, limit=100)
    assert shown.endswith("x" * 100) and "4900 earlier characters not shown" in shown


def test_a_kubectl_killed_by_a_signal_gave_no_exit_status(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    exe = bindir / "kubectl"
    exe.write_text("#!/bin/sh\nkill -KILL $$\n", encoding="utf-8")
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    with pytest.raises(lk.KubectlError) as ei:
        lk.kubectl_run("apply", ["-f", "-"], timeout=10, stdin_text="x")
    # ⛔ not "did not run": the apply may have been sent before the signal
    assert str(ei.value) == "`kubectl apply`: kubectl apply was killed by signal 9"


def test_a_verb_without_a_registered_proof_is_a_programming_error(tmp_path, monkeypatch):
    _fake_kubectl(tmp_path, monkeypatch, 0)
    assert ("kubectl", "exec") not in _lib_engine.PROOFS
    with pytest.raises(KeyError):
        lk.kubectl_run("exec", ["pod", "--", "sh"], timeout=5)


def test_list_passes_the_context(tmp_path, monkeypatch):
    argv = _fake_kubectl(tmp_path, monkeypatch, 0, _EMPTY)
    assert lk.kubectl_list("prometheusrules", namespace="ns", kubeconfig="/kc", context="ctx") == []
    assert argv() == ["--kubeconfig", "/kc", "--context", "ctx", "get", "prometheusrules",
                      "-n", "ns", "-o", "json"]


def test_it_is_a_named_caller_error():
    assert issubclass(lk.KubectlError, CallerError) and lk.KubectlError("x").reason == "kubectl_error"


# ── kubectl_get: one object by name, `get` only ─────────────────────────────

def test_get_returns_the_object_and_builds_its_argv(tmp_path, monkeypatch):
    argv = _fake_kubectl(tmp_path, monkeypatch, 0, json.dumps({"kind": "Secret", "metadata": {"name": "s"}}))
    assert lk.kubectl_get("secret", "s", namespace="ns", kubeconfig="/kc") == {
        "kind": "Secret", "metadata": {"name": "s"}}
    assert argv() == ["--kubeconfig", "/kc", "get", "secret", "s", "-n", "ns",
                      "--ignore-not-found", "-o", "json"]


def test_get_of_an_absent_object_is_none(tmp_path, monkeypatch):
    """Measured: `--ignore-not-found -o json` on an absent object is rc 0
    with empty stdout."""
    _fake_kubectl(tmp_path, monkeypatch, 0, "")
    assert lk.kubectl_get("crd", "nope.example.com") is None


@pytest.mark.parametrize("rc, stdout, stderr, why", [
    (1, "", 'Error from server (Forbidden): secrets "s" is forbidden', "Forbidden"),
    (1, "", "The connection to the server 127.0.0.1:6999 was refused", "was refused"),
    (0, "not json", "", "not JSON"),
    (0, "[]", "", "not an object"),
], ids=["forbidden", "unreachable", "not-json", "not-an-object"])
def test_get_without_an_answer_raises(tmp_path, monkeypatch, rc, stdout, stderr, why):
    _fake_kubectl(tmp_path, monkeypatch, rc, stdout, stderr)
    with pytest.raises(lk.KubectlError, match=why):
        lk.kubectl_get("secret", "s", namespace="ns")


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell and background job")
def test_an_answer_is_read_when_kubectl_exits_not_when_its_children_do(tmp_path, monkeypatch):
    """#3349 review: kubectl answers and exits, leaving a child (an exec
    credential plugin's helper) that still holds its stdout and stderr. The
    answer is read when kubectl exits — read through a pipe, it waited for
    that child, and a 10s budget reported an answered read as "no answer"."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    pidfile = tmp_path / "child.pid"
    exe = bindir / "kubectl"
    exe.write_text("#!/bin/sh\n"
                   "echo '{\"items\": []}'\n"
                   "sleep 30 &\n"
                   f"echo $! > {pidfile}\n"
                   "exit 0\n", encoding="utf-8")
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    start = time.monotonic()
    try:
        assert lk.kubectl_list("pods", timeout=10) == []
        assert time.monotonic() - start < 5
    finally:
        try:
            os.kill(int(pidfile.read_text().strip()), signal.SIGKILL)
        except (OSError, ValueError):
            pass
