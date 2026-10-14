"""The judgement process of ``generator_service`` (#2901, ADR-036 §3).

``run_child`` is called in the process the long-running service has just
forked, and never returns. In order:

1. ``os.setsid()`` — its own process group, so the service can end it and
   everything it started (amtool) with one ``killpg``; then ``RLIMIT_DATA``.
2. Take the conf.d files of the base commit from git objects — ``git ls-tree
   -r -z`` and one ``git cat-file --batch`` — never from a working tree and
   never with ``git archive`` (it applies ``.gitattributes`` export rules).
   A failed read is retried once.
3. Write them under the job directory (by tree entry mode: a file, a symlink
   with its target text as stored, an empty directory for a gitlink), then
   apply the request's writes.
4. Run the generator in this process — ``generate_alertmanager_routes.main``
   with ``--validate --strict --findings-json`` — with the service's parse
   cache installed (``_lib_parse_cache``), so unchanged files are not parsed
   again.
5. Send the result and the new cache entries to the service over a pipe
   (``_gsvc_proto`` frames), then ``os._exit``.

Its exit status is how the service tells "no complete result" apart: it
exits 0 only after the end frame is written.
"""
from __future__ import annotations

import hashlib
import json
import os
import resource
import signal
import stat
import subprocess
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))  # Repo subdir layout

# ⛔ Imported here, at the service's start: the judgement process is a fork,
# so it finds every generator module already loaded (ADR-036 §3).
import generate_alertmanager_routes as gar  # noqa: E402
import _lib_parse_cache  # noqa: E402
from _gsvc_proto import (  # noqa: E402
    STDERR_TAIL_BYTES, ValidateRequest, entry_frame, frame, path_problem,
)

#: Tests only: called with a stage name (``"before_result"``,
#: ``"after_result"``, ``"after_end"``) in the judgement process. None in
#: service use.
TEST_HOOK: Optional[Callable[[str], None]] = None


_GIT_TIMEOUT_SECONDS = 600


class GitReadError(Exception):
    """Reading the base tree from git failed."""


class BadWrite(Exception):
    """A write cannot be applied to the base tree (HTTP 400)."""


@dataclass
class ChildJob:
    request: ValidateRequest
    job_dir: str
    repo: str
    config_subdir: str
    git: str
    object_format: str
    memory_limit: int
    policy_copy: Optional[str]
    policy_sha256: Optional[str]
    store_entries: Mapping
    max_transfer: int


# ── git ──────────────────────────────────────────────────────────────────

def _git(job: ChildJob, *args: str, stdin=None) -> bytes:
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0")
    # The judgement deadline ends this (killpg on the whole group) long
    # before the timeout; the timeout only bounds a git that outlives it.
    proc = subprocess.run([job.git, "-C", job.repo, *args], stdin=stdin,
                          capture_output=True, env=env, check=False,
                          timeout=_GIT_TIMEOUT_SECONDS)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip()
        raise GitReadError(f"git {args[0]} exited {proc.returncode}: {err}")
    return proc.stdout


def read_tree(job: ChildJob) -> list[tuple[str, str, str, bytes]]:
    """``(mode, blob id, path below conf.d, content)`` for every entry of
    the config subdirectory in the base commit; content is empty for a
    gitlink."""
    sub = job.config_subdir.strip("/")
    out = _git(job, "ls-tree", "-r", "-z", job.request.base_commit, "--",
               sub or ".")
    entries: list[tuple[str, str, str]] = []
    for rec in out.split(b"\0"):
        if not rec:
            continue
        meta, _tab, raw_path = rec.partition(b"\t")
        try:
            mode, _type, oid = meta.decode("ascii").split(" ")
            path = raw_path.decode("utf-8")
        except ValueError as exc:
            raise GitReadError(f"unreadable ls-tree record: {rec[:200]!r}") from exc
        if sub:
            if not path.startswith(sub + "/"):
                continue
            path = path[len(sub) + 1:]
        entries.append((mode, oid, path))
    blobs = [oid for mode, oid, _p in entries if mode != "160000"]
    contents: dict[str, bytes] = {}
    if blobs:
        ids_file = os.path.join(job.job_dir, "blob-ids")
        with open(ids_file, "wb") as fh:
            fh.write(b"".join(oid.encode("ascii") + b"\n" for oid in blobs))
        os.chmod(ids_file, 0o600)
        with open(ids_file, "rb") as fh:
            data = _git(job, "cat-file", "--batch", stdin=fh)
        pos = 0
        for oid in blobs:
            nl = data.find(b"\n", pos)
            if nl < 0:
                raise GitReadError("cat-file output ended early")
            head = data[pos:nl].decode("ascii", "replace").split(" ")
            if len(head) != 3 or head[0] != oid or head[1] != "blob":
                raise GitReadError(f"cat-file: unexpected header {head!r}")
            size = int(head[2])
            body = data[nl + 1:nl + 1 + size]
            if len(body) != size or data[nl + 1 + size:nl + 2 + size] != b"\n":
                raise GitReadError("cat-file output ended early")
            contents[oid] = body
            pos = nl + 2 + size
    return [(mode, oid, path, contents.get(oid, b""))
            for mode, oid, path in entries]


# ── the tree on disk ─────────────────────────────────────────────────────

def _parent_dir(root: str, rel: str, err: type) -> str:
    """The directory *rel* goes into, created below *root* without passing
    through a symlink or a file (a tree entry or a write must not land
    outside the job directory)."""
    parts = rel.split("/")
    cur = root
    for part in parts[:-1]:
        cur = os.path.join(cur, part)
        try:
            st = os.lstat(cur)
        except FileNotFoundError:
            os.mkdir(cur, 0o700)
            continue
        if not stat.S_ISDIR(st.st_mode):
            raise err(f"{rel}: {part!r} in its path is not a directory in the base tree")
    return cur


def _write_file(root: str, rel: str, content: bytes, err: type) -> None:
    parent = _parent_dir(root, rel, err)
    target = os.path.join(parent, rel.split("/")[-1])
    try:
        st = os.lstat(target)
    except FileNotFoundError:
        st = None
    if st is not None:
        if stat.S_ISDIR(st.st_mode):
            raise err(f"{rel}: is a directory in the base tree")
        os.unlink(target)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(content)


def materialise(root: str, tree: list[tuple[str, str, str, bytes]]) -> None:
    """Write *tree* below *root* (created when the tree has entries)."""
    if tree:
        os.makedirs(root, mode=0o700, exist_ok=True)
    for mode, _oid, rel, content in tree:
        if path_problem(rel) is not None:
            raise GitReadError(f"tree entry with an unusable path: {rel!r}")
        if mode == "160000":
            parent = _parent_dir(root, rel, GitReadError)
            os.mkdir(os.path.join(parent, rel.split("/")[-1]), 0o700)
        elif mode == "120000":
            parent = _parent_dir(root, rel, GitReadError)
            os.symlink(os.fsdecode(content), os.path.join(parent, rel.split("/")[-1]))
        elif mode.startswith("100"):
            _write_file(root, rel, content, GitReadError)
        else:
            raise GitReadError(f"tree entry {rel!r} has unknown mode {mode}")


def apply_writes(root: str, request: ValidateRequest) -> list[dict]:
    """Apply the request's writes; the ``validated.files`` records."""
    files: list[dict] = []
    if request.writes:
        os.makedirs(root, mode=0o700, exist_ok=True)
    for w in request.writes:
        if w.delete:
            parent = _parent_dir(root, w.path, BadWrite)
            target = os.path.join(parent, w.path.split("/")[-1])
            if os.path.isdir(target) and not os.path.islink(target):
                raise BadWrite(f"{w.path}: is a directory in the base tree")
            if os.path.lexists(target):
                os.unlink(target)
            files.append({"path": w.path, "deleted": True})
        else:
            _write_file(root, w.path, w.content, BadWrite)
            files.append({"path": w.path,
                          "sha256": hashlib.sha256(w.content).hexdigest()})
    return files


# ── the judgement process ────────────────────────────────────────────────

def _tail(path: str) -> str:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - STDERR_TAIL_BYTES))
            return fh.read().decode("utf-8", "replace")
    except OSError:
        return ""


def _exit_code_of(exc: SystemExit) -> int:
    if exc.code is None:
        return 0
    if isinstance(exc.code, int):
        return exc.code
    return 1


def _send(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        n = os.write(fd, view)
        view = view[n:]


def _judge(job: ChildJob) -> dict:
    tree_dir = os.path.join(job.job_dir, "conf.d")
    tree = None
    for attempt in (1, 2):
        try:
            tree = read_tree(job)
            break
        except (GitReadError, OSError, subprocess.SubprocessError) as exc:
            print(f"generator-service: git read attempt {attempt} failed: {exc}",
                  file=sys.stderr)
    if tree is None:
        return {"outcome": "git_read"}
    try:
        materialise(tree_dir, tree)
    except (GitReadError, OSError) as exc:
        print(f"generator-service: base tree not written: {exc}", file=sys.stderr)
        return {"outcome": "git_read"}
    base_blobs = sorted({oid for mode, oid, _p, _c in tree if mode != "160000"})
    try:
        files = apply_writes(tree_dir, job.request)
    except BadWrite as exc:
        return {"outcome": "bad_request", "message": str(exc)}

    findings_path = os.path.join(job.job_dir, "findings.json")
    argv = ["--config-dir", tree_dir, "--validate", "--strict",
            "--findings-json", findings_path]
    if job.policy_copy is not None:
        argv += ["--policy", job.policy_copy]
    cache = _lib_parse_cache.ParseCache(job.store_entries, job.object_format)
    _lib_parse_cache.install(cache)
    rc, memory = 1, False
    try:
        gar.main(argv)
        rc = 0
    except SystemExit as exc:
        rc = _exit_code_of(exc)
    except MemoryError:
        memory = True
    except BaseException:  # noqa: BLE001 — the CLI would die with rc 1, too
        traceback.print_exc()
        rc = 1
    finally:
        _lib_parse_cache.install(None)
    sys.stdout.flush()
    sys.stderr.flush()
    common = {
        "base_blobs": base_blobs,
        "validated": {"base_commit": job.request.base_commit, "files": files,
                      "policy_sha256": job.policy_sha256},
        "cache": {"parses": cache.parses, "hits": len(cache.hits),
                  "stored": len(cache.new), "unreadable": cache.rejected_entries},
        "hit_keys": [list(k) for k in cache.hits],
    }
    if memory or cache.memory_error:
        return {"outcome": "memory", **common, "_cache": cache}
    try:
        with open(findings_path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return {"outcome": "incomplete", **common}
    if not isinstance(doc, dict) or doc.get("exit_code") != rc:
        return {"outcome": "incomplete", **common}
    return {"outcome": "ran", "rc": rc, "findings": doc, **common,
            "_cache": cache}


def run_child(job: ChildJob, result_fd: int) -> None:
    """The forked judgement process: judge, report on *result_fd*, exit."""
    code = 70
    try:
        os.setsid()
        resource.setrlimit(resource.RLIMIT_DATA, (job.memory_limit, job.memory_limit))
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, signal.SIG_DFL)
        try:
            signal.set_wakeup_fd(-1)
        except ValueError:
            pass
        os.chdir(job.job_dir)
        null = os.open(os.devnull, os.O_RDONLY)
        os.dup2(null, 0)
        for fd, name in ((1, "stdout"), (2, "stderr")):
            out = os.open(os.path.join(job.job_dir, name),
                          os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            os.dup2(out, fd)
            os.close(out)
        result = _judge(job)
        cache = result.pop("_cache", None)
        sys.stdout.flush()
        sys.stderr.flush()
        result["stderr_tail"] = _tail(os.path.join(job.job_dir, "stderr"))
        if TEST_HOOK is not None:
            TEST_HOOK("before_result")
        _send(result_fd, frame(b"R", json.dumps(result).encode("utf-8")))
        if TEST_HOOK is not None:
            TEST_HOOK("after_result")
        if cache is not None:
            used = 0
            for key, value in cache.new.items():
                piece = entry_frame(key, value)
                if used + len(piece) > job.max_transfer:
                    continue  # past the cap: dropped, the verdict is unaffected
                used += len(piece)
                _send(result_fd, piece)
        _send(result_fd, frame(b"Z", b""))
        if TEST_HOOK is not None:
            TEST_HOOK("after_end")
        code = 0
    except BaseException:  # noqa: BLE001 — no complete result: the exit says so
        try:
            traceback.print_exc()
            sys.stderr.flush()
        except BaseException:  # noqa: BLE001
            pass
    finally:
        os._exit(code)
