"""Harness for ``test_generator_service.py`` (#2901): a git repo of conf.d
trees, stub executables, the service started as a real process on a Unix
socket, and a minimal HTTP/1.1 client.

Tenant ids are fixture names (CLAUDE.md #9).
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

REPO = Path(__file__).resolve().parents[2]
TOOLS = REPO / "scripts" / "tools"
OPS = TOOLS / "ops"
GAR = OPS / "generate_alertmanager_routes.py"
GIT = shutil.which("git") or "git"

ACCEPTING_AMTOOL = "#!/bin/sh\nexit 0\n"

#: ⛔ The time limit of every judgement a test makes unless the test is ABOUT
#: a time limit. A test whose verdict merely has to arrive must not race a
#: wall-clock limit it is not testing: CI runs this file under coverage
#: tracing and on loaded hosts, where a judgement that takes a second here
#: takes many (#3015 lost two runs that way: a memory test answered
#: ``deadline``, an E1 count off by a timing). Ten minutes is past pytest's
#: own per-test --timeout (300 s), so this limit is never what ends a test.
#: Only a test of a deadline passes its own ``--max-judgement-seconds`` /
#: ``deadline_ms``, and asserts only what that limit guarantees.
LONG_JUDGEMENT_SECONDS = 600.0
#: ``deadline_ms`` that leaves LONG_JUDGEMENT_SECONDS after the service's
#: default --deadline-reserve-seconds (5).
LONG_DEADLINE_MS = int((LONG_JUDGEMENT_SECONDS + 5.0) * 1000)


def run_git(repo: Path, *args: str, stdin: Optional[bytes] = None) -> str:
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com")
    return subprocess.run([GIT, "-C", str(repo), *args], input=stdin, env=env,
                          capture_output=True, check=True, timeout=120).stdout.decode().strip()


def init_repo(path: Path, fmt: str = "sha1") -> Path:
    path.mkdir(parents=True)
    run_git(path, "init", "-q", f"--object-format={fmt}")
    run_git(path, "config", "gc.auto", "0")
    return path


def commit_tree(repo: Path, files: dict[str, str], subdir: str = "conf.d") -> str:
    """Replace ``<subdir>`` with *files* and commit; returns the commit id."""
    d = repo / subdir
    if d.exists():
        shutil.rmtree(d)
    for rel, body in files.items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "--allow-empty", "-m", "tree")
    return run_git(repo, "rev-parse", "HEAD")


def write_tree(root: Path, files: dict[str, str]) -> Path:
    for rel, body in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    return root


def stub(bin_dir: Path, name: str, script: str) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    p = bin_dir / name
    p.write_text(script, encoding="utf-8")
    p.chmod(0o755)
    return p


def b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def short_dir() -> Path:
    """A directory whose socket path fits AF_UNIX's ~108 bytes."""
    return Path(tempfile.mkdtemp(prefix="gsvc", dir="/tmp"))


def provide_da_guard(bin_dir: Path) -> None:
    """Put the test session's da-guard (``$DA_GUARD_BINARY``, set for every
    test by tests/conftest.py) on *bin_dir* as ``da-guard``: the generator
    reads conf.d through ``da-guard parse-tree`` (ADR-036 step 4), and the
    service finds it on $PATH as in the image."""
    src = os.environ.get("DA_GUARD_BINARY", "").strip()
    if not src:
        raise RuntimeError("DA_GUARD_BINARY is not set: the harness has no da-guard to provide")
    bin_dir.mkdir(parents=True, exist_ok=True)
    dst = bin_dir / "da-guard"
    if not dst.exists():
        shutil.copy2(src, dst)  # a copy: no symlink capability needed


class Service:
    """The service as a real process; ``stop()`` (or the context manager)
    ends it."""

    def __init__(self, tmp: Path, repo: Path, *, bin_dir: Path,
                 args: tuple[str, ...] = (), prelude: str = "",
                 env: Optional[dict] = None, socket_path: Optional[Path] = None,
                 subdir: str = "conf.d", wait: bool = True) -> None:
        self.tmp = tmp
        tmp.mkdir(parents=True, exist_ok=True)
        self.sock_dir = short_dir()
        self.socket = socket_path or (self.sock_dir / "g.sock")
        self.work = tmp / "work"
        launcher = tmp / "launcher.py"
        launcher.write_text(
            "import os, sys\n"
            f"sys.path[:0] = [{str(OPS)!r}, {str(TOOLS)!r}]\n"
            + prelude +
            "\nimport generator_service as gs\n"
            "sys.exit(gs.main(sys.argv[1:]))\n", encoding="utf-8")
        provide_da_guard(bin_dir)
        run_env = dict(os.environ)
        run_env["PATH"] = str(bin_dir)
        run_env.pop("DA_GUARD_BINARY", None)
        run_env.update(env or {})
        self.log = tmp / "service.log"
        self.proc = subprocess.Popen(
            [sys.executable, "-s", str(launcher), "--socket", str(self.socket),
             "--repo", str(repo), "--config-subdir", subdir,
             "--work-dir", str(self.work), "--git", GIT,
             # start-up warming off unless a test asks for it (#2901 E2)
             *(() if any(a.startswith("--warm-ref") for a in args) else ("--warm-ref=",)),
             # no judgement limit in play unless a test asks for one (above)
             *(() if any(a.startswith("--max-judgement-seconds") for a in args)
               else ("--max-judgement-seconds", str(LONG_JUDGEMENT_SECONDS))),
             *args],
            env=run_env, stdout=subprocess.DEVNULL,
            stderr=open(self.log, "wb"))  # noqa: SIM115 — closed with the process
        if wait:
            self.wait_ready()

    def wait_ready(self, timeout: float = 60.0) -> None:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.proc.poll() is not None:
                raise RuntimeError(f"service exited {self.proc.returncode}: "
                                   + self.log.read_text(encoding="utf-8", errors="replace"))
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                probe.connect(str(self.socket))
                return
            except OSError:
                pass  # not bound yet (or an old socket file still there)
            finally:
                probe.close()
            time.sleep(0.05)
        raise RuntimeError("service did not start")

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        shutil.rmtree(self.sock_dir, ignore_errors=True)

    def __enter__(self) -> "Service":
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    # ── client ──────────────────────────────────────────────────────────

    def raw(self, data: bytes, timeout: float = 60.0) -> tuple[int, dict, bytes]:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(str(self.socket))
        try:
            s.sendall(data)
            out = b""
            while True:
                chunk = s.recv(1 << 16)
                if not chunk:
                    break
                out += chunk
        finally:
            s.close()
        head, _, body = out.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        status = int(lines[0].split(" ")[1])
        headers = {}
        for line in lines[1:]:
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()
        return status, headers, body

    def call(self, method: str, path: str, doc: object = None,
             timeout: float = 60.0) -> tuple[int, object]:
        body = b"" if doc is None else json.dumps(doc).encode("utf-8")
        head = f"{method} {path} HTTP/1.1\r\nHost: g\r\n"
        if method == "POST":
            head += f"Content-Length: {len(body)}\r\n"
        status, headers, data = self.raw(head.encode() + b"\r\n" + body, timeout)
        if headers.get("content-type", "").startswith("application/json"):
            return status, json.loads(data)
        return status, data.decode("utf-8")

    def validate(self, commit: str, writes: list[dict] | None = None,
                 deadline_ms: int = LONG_DEADLINE_MS,
                 timeout: float = LONG_JUDGEMENT_SECONDS + 60) -> tuple[int, dict]:
        return self.call("POST", "/v1/validate",
                         {"base_commit": commit, "writes": writes or [],
                          "deadline_ms": deadline_ms}, timeout)

    def metrics(self) -> dict[str, float]:
        _status, text = self.call("GET", "/metrics")
        out = {}
        for line in text.splitlines():
            if line and not line.startswith("#"):
                name, _, value = line.rpartition(" ")
                out[name] = float(value)
        return out


def run_cli(confdir: Path, bin_dir: Path, findings: Path,
            policy: Optional[str] = None) -> tuple[int, dict]:
    """``generate-routes --validate --strict`` on *confdir*, as the CLI."""
    provide_da_guard(bin_dir)
    env = {k: v for k, v in os.environ.items() if k != "PATH"}
    env["PATH"] = str(bin_dir)
    argv = [sys.executable, "-s", str(GAR), "--config-dir", str(confdir),
            "--validate", "--strict", "--findings-json", str(findings)]
    if policy:
        argv += ["--policy", policy]
    proc = subprocess.run(argv, env=env, capture_output=True, timeout=180)
    return proc.returncode, json.loads(findings.read_text(encoding="utf-8"))


def alive(pid: int) -> bool:
    """True while the process exists — a zombie included: one nobody has
    reaped is still a leak (#2901 R3)."""
    return os.path.exists(f"/proc/{pid}")


def zombies_of(pid: int) -> list[int]:
    """The zombie children of *pid*."""
    out = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat", encoding="utf-8") as fh:
                fields = fh.read().rsplit(")", 1)[1].split()
        except OSError:
            continue
        if fields[0] == "Z" and int(fields[1]) == pid:
            out.append(int(entry))
    return out
