"""The judgement process of ``generator_service`` (#2901, ADR-036 §3).

``run_child`` is called in the process the long-running service has just
forked, and never returns. In order:

1. ``os.setsid()`` — its own process group, so the service can end it and
   everything it started (amtool) with one ``killpg``; then ``RLIMIT_DATA``.
2. Take the conf.d files of the base commit from git objects — ``git ls-tree
   -r -z`` and one ``git cat-file --batch`` — never from a working tree and
   never with ``git archive`` (it applies ``.gitattributes`` export rules).
   A failed read is retried once.
3. Write them under the job directory — the regular files (mode 100644 /
   100755) only. Every other entry (a symlink, a gitlink, any other mode)
   is not written: it is handed to the generator as not_regular_file
   (#2981, ADR-036 §3), unless it is hidden (no reader reads those) or the
   request writes or deletes that path; its parent directories are made,
   so it still occupies its path. Then apply the request's writes, in
   order.
4. Run the generator in this process — ``generate_alertmanager_routes.main``
   with ``--validate --strict --findings-json`` — with the service's parse
   cache installed (``_lib_parse_cache``), so an unchanged file is not read
   through ``da-guard parse-tree`` again (ADR-036 step 4: the generator reads
   conf.d from parse-tree; da-guard is on the image's $PATH).
5. Report over one pipe (``_gsvc_proto`` frames): the content hashes of the
   base tree's files as soon as the tree is read, each new parse-cache entry the moment its
   parse finishes, and the result last — so a process the deadline kills
   still hands back every parse it completed. Then ``os._exit``.

A warming run (``generator_service``'s pre-warm) is the same, with no writes
and its result ignored: it exists only to fill the cache.

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
import time
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
import _grar_parse  # noqa: E402
import _lib_confd  # noqa: E402
import _lib_key_verdict  # noqa: E402
import _lib_tenant_values  # noqa: E402
import _lib_parse_cache  # noqa: E402
import _lib_parse_tree  # noqa: E402
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
    #: #2901 E2: a warming run — the generator's conf.d read only
    #: (``load_tenant_tree``, where every parse of the judgement happens),
    #: no writes, no verdict, no amtool: it exists to fill the cache.
    warm: bool = False
    #: The judgement deadline (``time.monotonic()``, which the service's
    #: event loop clock is; the fork shares it): where the service ends this
    #: process. 0: none known — the key verdict is not started.
    deadline_at: float = 0.0


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
    gitlink. ``config_subdir`` arrives normalised by the service's start
    (generator_service._normalize_config_subdir): ``""`` is the root."""
    sub = job.config_subdir
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


REGULAR_MODES = ("100644", "100755")


def materialise(root: str, tree: list[tuple[str, str, str, bytes]]) -> dict[str, str]:
    """Write the regular files of *tree* below *root* (created when the tree
    has entries); return the other entries as {path: mode}.

    #2981 (ADR-036 §3): read from git objects, a symlink is its link text
    and a gitlink is nothing — neither is the file a filesystem reader
    would follow to. Writing the link back as a symlink made the verdict
    depend on what the text happened to point at (or parse as), so every
    entry that is not a regular file is judged not_regular_file instead
    (_grar_parse.set_git_irregular_entries). Such an entry is still a tree
    entry at its path: its parent directories are made, so a write of one
    of them meets a directory there and is refused as for a regular file
    (apply_writes). A hidden one (a path component Go classes `hidden`) is
    left out without a word: every reader skips it, whatever its shape.
    Which component is hidden is Go's answer (`classify_names`, one names-mode
    run for every component of every such entry — #3206, no name rule here);
    raises `_lib_parse_tree.ParseTreeError` when da-guard cannot answer."""
    if tree:
        os.makedirs(root, mode=0o700, exist_ok=True)
    others: list[tuple[str, str]] = []
    for mode, _oid, rel, content in tree:
        if path_problem(rel) is not None:
            raise GitReadError(f"tree entry with an unusable path: {rel!r}")
        if mode in REGULAR_MODES:
            _write_file(root, rel, content, GitReadError)
        else:
            others.append((rel, mode))
    parts = sorted({part for rel, _m in others for part in rel.split("/")})
    classes = _lib_parse_tree.classify_names(parts) if parts else {}
    irregular: dict[str, str] = {}
    for rel, mode in others:
        if not any(classes[part].cls == "hidden" for part in rel.split("/")):
            _parent_dir(root, rel, GitReadError)
            irregular[rel] = mode
    return irregular


def apply_writes(root: str, request: ValidateRequest,
                 irregular: "dict[str, str] | None" = None) -> list[dict]:
    """Apply the request's writes, in order; the ``validated.files`` records.

    *irregular* (materialise's answer, updated in place) holds the entries
    that occupy a path but are not on disk. Each write sees the tree the
    writes before it left: one below such an entry is refused, as one below
    a file is, and one that writes or deletes the entry's own path takes it
    out (tenant-api writes a regular file there, or leaves nothing). Only
    after a delete may a later write go below it: once written, the path is
    a regular file, and a write below it is refused as above. A write of a directory that holds one meets that directory
    (materialise made it) and is refused like any other."""
    if irregular is None:
        irregular = {}
    files: list[dict] = []
    if request.writes:
        os.makedirs(root, mode=0o700, exist_ok=True)
    for w in request.writes:
        parts = w.path.split("/")
        for i in range(1, len(parts)):
            if "/".join(parts[:i]) in irregular:
                raise BadWrite(f"{w.path}: {parts[i - 1]!r} in its path is not a directory "
                               f"in the base tree")
        irregular.pop(w.path, None)
        if w.delete:
            parent = _parent_dir(root, w.path, BadWrite)
            target = os.path.join(parent, w.path.split("/")[-1])
            if os.path.isdir(target) and not os.path.islink(target):
                raise BadWrite(f"{w.path}: is a directory in the base tree")
            if os.path.lexists(target):
                os.unlink(target)
            files.append({"path": w.path, "deleted": True})
        else:
            content = w.content
            _write_file(root, w.path, content, BadWrite)
            files.append({"path": w.path,
                          "sha256": hashlib.sha256(content).hexdigest()})
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


class _Stream:
    """The pipe to the long-running process: entries go out as they are
    made, within the per-judgement transfer cap (past it they are dropped —
    the verdict does not depend on them)."""

    def __init__(self, fd: int, cap: int) -> None:
        self.fd, self.cap, self.used = fd, cap, 0

    def send(self, data: bytes) -> None:
        _send(self.fd, data)

    def entry(self, key: tuple[str, str], value: bytes) -> None:
        piece = entry_frame(key, value)
        if self.used + len(piece) > self.cap:
            return
        self.used += len(piece)
        _send(self.fd, piece)


def _judge(job: ChildJob, stream: _Stream) -> dict:
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
        irregular = materialise(tree_dir, tree)
    except (GitReadError, OSError) as exc:
        print(f"generator-service: base tree not written: {exc}", file=sys.stderr)
        return {"outcome": "git_read"}
    except _lib_parse_tree.ParseTreeError as exc:
        # Not a git read: da-guard could not say which path components are
        # hidden (names mode). Its own outcome, so the reply does not blame git.
        msg = " ".join(str(exc).split())
        print(f"generator-service: da-guard could not classify the base tree's "
              f"path names: {msg}", file=sys.stderr)
        return {"outcome": "da_guard", "message": msg}
    # #2901 E2: the base tree's files, as soon as they are known — the
    # long-running process pins them even if this process is killed. By the
    # cache's own key (SHA-256 of the path below conf.d and the content,
    # ADR-036 step 4), not the git blob id.
    stream.send(frame(b"B", json.dumps(sorted(
        {_lib_parse_cache.content_key(p, c) for mode, _o, p, c in tree
         if mode != "160000"})).encode("utf-8")))
    written = {w.path for w in job.request.writes}
    base = {p: c for mode, _o, p, c in tree if p in written and mode in ("100644", "100755")}
    try:
        files = apply_writes(tree_dir, job.request, irregular)
    except BadWrite as exc:
        return {"outcome": "bad_request", "message": str(exc)}

    if job.warm:
        return _warm(job, tree_dir, stream)

    findings_path = os.path.join(job.job_dir, "findings.json")
    argv = ["--config-dir", tree_dir, "--validate", "--strict",
            "--findings-json", findings_path]
    if job.policy_copy is not None:
        argv += ["--policy", job.policy_copy]
    # #2901 E1: every new entry goes down the pipe the moment it is made.
    cache = _lib_parse_cache.ParseCache(job.store_entries,
                                        on_store=stream.entry)
    _lib_parse_cache.install(cache)
    _grar_parse.set_git_irregular_entries(irregular)
    rc, memory = 1, False
    try:
        # ADR-038 §5: not the CLI's key verdict — this process judges the
        # keys itself below (key_verdict()), after a pass and within this
        # judgement's deadline; the CLI's would judge every write a second time.
        gar.main(argv, key_verdict=False)
        rc = 0
    except SystemExit as exc:
        rc = _exit_code_of(exc)
    except MemoryError:
        memory = True
    except Exception:  # noqa: BLE001 — the CLI would die with its crash code, too
        traceback.print_exc()
        from _lib_exitcodes import effective_crash_exit_code  # loaded with gar (#3090)
        rc = effective_crash_exit_code()
    except BaseException:  # noqa: BLE001 — e.g. KeyboardInterrupt: not a crash code
        traceback.print_exc()
        rc = 1
    finally:
        _lib_parse_cache.install(None)
        _grar_parse.set_git_irregular_entries(None)
    sys.stdout.flush()
    sys.stderr.flush()
    common = {
        "validated": {"base_commit": job.request.base_commit, "files": files,
                      "policy_sha256": job.policy_sha256},
        "cache": {"parses": cache.parses, "hits": len(cache.hits),
                  "stored": len(cache.new), "unreadable": cache.rejected_entries},
        "hit_keys": [list(k) for k in cache.hits],
    }
    if memory or cache.memory_error:
        return {"outcome": "memory", **common}
    try:
        with open(findings_path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return {"outcome": "incomplete", **common}
    if not isinstance(doc, dict) or doc.get("exit_code") != rc:
        return {"outcome": "incomplete", **common}
    out = {"outcome": "ran", "rc": rc, "findings": doc, **common}
    if rc == 0:
        # A write the generator refuses is refused whatever the verdict
        # says; one it passes gets the verdict (ADR-038 §5).
        out["key_verdict"] = key_verdict(job, tree_dir, base, cache)
        out["hit_keys"] = [list(k) for k in cache.hits]  # the verdict's hits keep their entries too
    return out


# ── the key verdict (ADR-038 decision 5) ─────────────────────────────────
#
# After a write the generator passes, the shared verdict on the keys of the
# tenants the write touches (_lib_key_verdict.judge_tree, `--tenant`), in
# this process, under its RLIMIT_DATA, within the same judgement deadline as
# the generator: it has no deadline of its own and does not yield to a
# waiting write. A verdict that does not fit — out of time or memory,
# da-guard failing, an undecidable tree — is reported as not completed, with
# the reason. The counterfactual of `disable` (`--disable-effects`) is
# computed for the requested tenants only (da-guard's subset). Its cost grows
# with the tree (measured: about 1.2 s at 1000 tenants, 3 s at 2500, 6 s at
# 5000 on one CPU, #3206).

#: The verdict stops this long before the judgement deadline, so the result
#: is still sent in time — with room for a C call (a JSON decode of
#: da-guard's output, a wait for the killed da-guard) that holds off the
#: alarm until it returns.
VERDICT_RESERVE_SECONDS = 2.0
#: Less time than this left for the verdict: it is not started.
VERDICT_MIN_SECONDS = 0.5
#: Findings below error severity sent at most (the rest counted in
#: ``truncated``). Error findings are never dropped: the write is refused on
#: them (ADR-038 §5), so a refusal must list every one.
VERDICT_MAX_FINDINGS = 1000


class _VerdictDeadline(BaseException):
    """The judgement deadline, less the reserve, ran out (SIGALRM). A
    BaseException, so no ``except Exception`` on the way swallows it."""


def _on_alarm(_signum, _frame) -> None:
    raise _VerdictDeadline()


def written_scope(job: ChildJob, tree_dir: str, base: "dict[str, bytes]",
                  reader: "_lib_parse_cache.ConfdReader") -> "tuple[list[str], list[str]]":
    """``(platform paths, tenants)`` of the write (ADR-038 §5).

    *tenants*: those the written files declare, before and after the write
    (a tenant file's ``tenants:``, and a root platform file's — its
    per-tenant platform defaults), read through the installed cache, where
    the generator's run (after) and the long-running process (before) left
    them.

    *platform paths*: the written files whose keys reach tenants they do
    not name: a defaults file at any depth, or a root platform file with
    ``profiles:`` before or after (the verdict reads profiles from those
    files too, ``_lib_key_verdict._root_profiles``). What a path is, is Go's
    classification of the tree after the write (*reader*'s listing); a
    deleted path, gone from it, by its name alone. A file no reader selects
    (a hidden one, a ``_`` file below the root that is not a defaults file)
    declares nothing."""
    listing = _lib_confd.config_listing(tree_dir, reader=reader)
    after = {e.path: e for e in listing.config_files()} if listing is not None else {}
    deleted = sorted({w.path.split("/")[-1] for w in job.request.writes if w.delete})
    named = _lib_parse_tree.classify_names(deleted) if deleted else {}
    cache = _lib_parse_cache.active()
    platform: list[str] = []
    tenants: set[str] = set()
    for w in job.request.writes:
        if any(part.startswith(".") for part in w.path.split("/")):
            continue  # hidden: no reader walks into it
        if w.delete:
            e = named.get(w.path.split("/")[-1])
            cls, root_platform = (e.cls if e else None), (e is not None and e.reserved and "/" not in w.path)
        else:
            e = after.get(w.path)
            cls, root_platform = (e.cls if e else None), (e is not None and e.root_platform_file)
        if cls == "defaults":
            platform.append(w.path)
            continue
        if cls != "tenant" and not root_platform:
            continue  # declares nothing a reader serves
        docs = []
        try:
            before = base.get(w.path)
            if before is not None:
                key = (_lib_parse_cache.content_key(w.path, before), _lib_parse_cache.VALUES.name)
                doc = cache.cached(key) if cache is not None else _lib_parse_cache.MISS
                if doc is _lib_parse_cache.MISS:
                    doc = _lib_parse_cache.read_text_as(tree_dir, w.path, before)
                docs.append(doc)
            if not w.delete:
                docs.append(reader.read(os.path.join(tree_dir, w.path)))
        except (_lib_parse_cache.NotSelected, _lib_parse_cache.FileUnreadable):
            continue  # declares nothing the exporter serves
        if root_platform and any(isinstance(d, dict) and d.get("profiles") for d in docs):
            platform.append(w.path)
            continue
        for doc in docs:
            block = doc.get("tenants") if isinstance(doc, dict) else None
            if isinstance(block, dict):
                tenants.update(str(t) for t in block)
    return platform, sorted(tenants)


def key_verdict(job: ChildJob, tree_dir: str, base: "dict[str, bytes]",
                cache: "_lib_parse_cache.ParseCache") -> dict:
    """The verdict's report: ``status`` complete or not_completed (with
    ``reason`` no_time / deadline / memory / error / undecidable /
    platform_file and ``message``), ``tenants`` judged, ``seconds``, and
    when complete the findings."""
    started = time.monotonic()
    try:
        return _key_verdict(job, tree_dir, base, cache, started)
    except _VerdictDeadline:
        # The alarm came after the verdict's own handlers, on its way out.
        signal.setitimer(signal.ITIMER_REAL, 0)
        return {"status": "not_completed", "reason": "deadline", "tenants": 0,
                "seconds": round(time.monotonic() - started, 3), "message": _OUT_OF_TIME}


_OUT_OF_TIME = "the judgement deadline ran out"


def kept_findings(findings: list) -> "tuple[list, int]":
    """``(sent, truncated)``: every error finding, then the others up to
    VERDICT_MAX_FINDINGS in all; ``truncated`` counts the others dropped."""
    errors = [f for f in findings if f.severity == _lib_key_verdict.ERROR]
    others = [f for f in findings if f.severity != _lib_key_verdict.ERROR]
    room = max(0, VERDICT_MAX_FINDINGS - len(errors))
    return errors + others[:room], max(0, len(others) - room)


def _key_verdict(job: ChildJob, tree_dir: str, base: "dict[str, bytes]",
                 cache: "_lib_parse_cache.ParseCache", started: float) -> dict:
    budget = job.deadline_at - VERDICT_RESERVE_SECONDS - started if job.deadline_at else 0.0

    def report(status: str, **fields) -> dict:
        return {"status": status, "seconds": round(time.monotonic() - started, 3), **fields}
    if budget < VERDICT_MIN_SECONDS:
        return report("not_completed", reason="no_time", tenants=0,
                      message=f"{max(budget, 0.0):.1f}s of the judgement deadline left for it")
    previous = signal.signal(signal.SIGALRM, _on_alarm)
    outer = signal.setitimer(signal.ITIMER_REAL, budget)  # a timer someone set before (a test runner's)
    # The verdict's parses are not streamed: a signal ending it in the middle
    # of writing an entry would leave half a frame on the pipe, and the
    # result after it unreadable. They stay in this process.
    on_store, cache.on_store = cache.on_store, None
    _lib_parse_cache.install(cache)
    tenants: list[str] = []
    try:
        # One reader for the whole verdict: its tree-mode document is read once.
        reader = _lib_parse_cache.ConfdReader(tree_dir)
        platform, tenants = written_scope(job, tree_dir, base, reader)
        if platform:
            # Its keys reach tenants it does not name: the whole tree's
            # verdict (the PR's CI) judges it, not this one.
            return report("not_completed", reason="platform_file", tenants=0,
                          message=f"{platform[0]} is a platform file: its keys reach tenants "
                                  "it does not name, which only a whole-tree verdict judges")
        if not tenants:
            return report("complete", tenants=0, findings=[], truncated=0)
        verdict = _lib_key_verdict.judge_tree(
            tree_dir, tenants=tenants, timeout=budget, reader=reader)
        if verdict.undecidable:
            # ADR-038 §7: no root platform file, so a tenant's threshold key
            # cannot be told from a misspelt one; a retry changes nothing.
            return report("not_completed", reason="undecidable", tenants=len(tenants),
                          message="no root platform file was read: the tenants' threshold "
                                  "keys cannot be judged")
        kept, truncated = kept_findings(verdict.findings)
        findings = [{"severity": f.severity, "kind": f.kind, "key": f.key, "file": f.file,
                     "tenants": list(f.tenants), "reason": f.reason} for f in kept]
        return report("complete", tenants=len(tenants), findings=findings, truncated=truncated)
    except _VerdictDeadline:
        return report("not_completed", reason="deadline", tenants=len(tenants),
                      message=_OUT_OF_TIME)
    except MemoryError as exc:
        return report("not_completed", reason="memory", tenants=len(tenants),
                      message=" ".join(str(exc).split())[:300] or "MemoryError")
    except _lib_tenant_values.DaGuardError as exc:
        # A da-guard ended by a signal under RLIMIT_DATA (GOTRACEBACK=crash:
        # the Go runtime's own out-of-memory too) is its memory limit.
        reason = "memory" if (exc.returncode or 0) < 0 else "error"
        return report("not_completed", reason=reason, tenants=len(tenants),
                      message=" ".join(str(exc).split())[:300])
    except BaseException as exc:  # noqa: BLE001 — reported as not completed, with its reason
        return report("not_completed", reason="error", tenants=len(tenants),
                      message=f"{type(exc).__name__}: {' '.join(str(exc).split())[:300]}")
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
        if outer[0] > 0:
            signal.setitimer(signal.ITIMER_REAL,
                             max(outer[0] - (time.monotonic() - started), 0.001), outer[1])
        _lib_parse_cache.install(None)
        cache.on_store = on_store


def _close_inherited(keep: int) -> None:
    limit = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
    top = limit if 0 < limit < (1 << 20) else 65536
    os.closerange(3, keep)
    os.closerange(keep + 1, top)


def _warm(job: ChildJob, tree_dir: str, stream: "_Stream") -> dict:
    cache = _lib_parse_cache.ParseCache(job.store_entries,
                                        on_store=stream.entry)
    _lib_parse_cache.install(cache)
    try:
        gar.load_tenant_tree(tree_dir, strict_policies=True)
    except SystemExit:
        pass  # an unusable tree: whatever was parsed has been sent
    finally:
        _lib_parse_cache.install(None)
    outcome = "memory" if cache.memory_error else "warmed"
    return {"outcome": outcome,
            "cache": {"parses": cache.parses, "hits": len(cache.hits),
                      "stored": len(cache.new), "unreadable": cache.rejected_entries}}


def run_child(job: ChildJob, result_fd: int) -> None:
    """The forked judgement process: judge, report on *result_fd*, exit."""
    code = 70
    try:
        os.setsid()
        resource.setrlimit(resource.RLIMIT_DATA, (job.memory_limit, job.memory_limit))
        # Every da-guard this judgement starts inherits that limit; one ended
        # by a signal is this judgement's memory (#2906), and it leaves no
        # core file behind.
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        _lib_parse_tree.run_under_memory_limit()
        # ⛔ Close every inherited descriptor but stdio and the result pipe:
        # a fork copies the service's open client connections and its
        # listening socket, and a judgement (or warming run) holding a
        # client's socket keeps that client waiting for EOF after the
        # service has answered and closed its side.
        _close_inherited(result_fd)
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGCHLD):
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
        stream = _Stream(result_fd, job.max_transfer)
        result = _judge(job, stream)
        sys.stdout.flush()
        sys.stderr.flush()
        result["stderr_tail"] = _tail(os.path.join(job.job_dir, "stderr"))
        if TEST_HOOK is not None:
            TEST_HOOK("before_result")
        _send(result_fd, frame(b"R", json.dumps(result).encode("utf-8")))
        if TEST_HOOK is not None:
            TEST_HOOK("after_result")
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
