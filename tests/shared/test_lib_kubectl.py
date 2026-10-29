"""`_lib_kubectl.kubectl_list` — the one cluster reader (#3207, #3210).

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
import _lib_kubectl as lk  # noqa: E402
from _lib_exitcodes import CallerError  # noqa: E402

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
    with pytest.raises(lk.KubectlError, match="kubectl is not on PATH"):
        lk.kubectl_list("secret")


def test_no_answer_within_the_timeout(monkeypatch):
    def boom(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="kubectl", timeout=3)
    monkeypatch.setattr(lk.subprocess, "run", boom)
    with pytest.raises(lk.KubectlError, match="no answer within 3s"):
        lk.kubectl_list("secret", timeout=3)


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
