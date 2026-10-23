#!/usr/bin/env python3
"""Measure one generator-container judgement against ADR-036 §3's budget (#3206).

    python3 scripts/tools/dx/measure_judgement_budget.py --runs 3
    python3 scripts/tools/dx/measure_judgement_budget.py --tree <conf.d> --work-dir <dir>

With ``--work-dir`` (a directory that does not exist yet) the directory is
kept: every run's job directory (its ``report.json``, stdout, stderr) and
``result.json``, the whole result.

Needs helm, amtool and da-guard (``make da-guard-build``) on this machine.

WHAT IS MEASURED
================
"One complete judgement" is everything the generator container runs for one
write when the per-file parse cache is gone, in one judgement process:

1. the base tree read from git objects and written to a job directory, the
   write applied (``_gsvc_child.read_tree`` / ``materialise`` /
   ``apply_writes`` — the container's own functions, so ``git ls-tree`` and
   ``git cat-file --batch`` run as they do there);
2. the route generator, ``generate_alertmanager_routes.main`` with
   ``--validate --strict --findings-json`` (the argv ``_gsvc_child`` builds),
   with NO parse cache installed: it reads conf.d through one
   ``da-guard parse-tree`` tree-mode run, and its ``--validate`` runs
   ``amtool check-config`` on the generated config;
3. ``da-guard served-values --tenant <id> ...`` for the tenants the write
   affects (ADR-038 decision 5), its output read and decoded by the
   judgement process.

Two writes are measured, and the conditions are judged on the worse:

* a tenant file: one numeric threshold of the tenant file declaring the most
  tenants raised by one; affected tenants are those it declares before and
  after the write;
* a defaults file: one numeric value of the root defaults carrier (else the
  carrier above the most tenant files) raised by one; affected tenants are
  every tenant declared below its directory.

The tree is a generated ``synthetic-v2`` fixture (``generate_tenant_fixture.py``,
the bench harness's realistic shape) unless ``--tree`` names one. A given tree
is committed whole: ``git add -A -f`` with no excludes file, so a file a
``.gitignore`` or a global excludes file would hide is measured too.

NOT REPLICATED
==============
* The judgement process is a fresh interpreter, not a fork of the
  long-running service with the modules already loaded: its interpreter start
  and imports are inside the measured wall time (an upper bound), and its
  memory does not include an inherited parse cache (there is none to inherit
  on the cache-less path).
* Done as ``_gsvc_child.run_child`` does: its own session (``setsid``), the
  job directory as working directory, stdin from ``/dev/null``, stdout and
  stderr to files, ``RLIMIT_DATA`` and ``RLIMIT_CORE`` 0, ``GOTRACEBACK=crash``
  for every program it starts. Not done: ``_close_inherited`` (the process is
  started with ``close_fds``, so it holds nothing to close) and the result
  pipe with its cache frames (the cache-less path has no cache to send).
* No deployment policy file: the chart's render baseline passes none
  (``generator.policy.existingConfigMap`` unset), so ``--policy`` is not given.
* CPU: the container's limit is a cgroup CPU quota; here the judgement is
  pinned to as many CPUs as that limit (``sched_setaffinity``, what
  ``taskset -c`` does). Not the same scheduler behaviour.

WHERE THE LIMITS COME FROM
==========================
None is written here. ``helm template`` renders the tenant-api chart; the
generator container's ``args`` are parsed with generator-service's own
argument parser, so ``--memory-limit-bytes`` and ``--max-judgement-seconds``
are what the container would run with (its chart flag, else the program
default); the CPU count is the container's rendered CPU limit. Change any of
them and the measurement follows. A value the render does not give is exit 2.

The pass rule is ADR-036 §3's: every process's peak memory at most
``MEMORY_FRACTION`` of the judgement process's limit, and the judgement's wall
time at most ``TIME_FRACTION`` of the judgement deadline.

A run under a limit that the limit ends — a MemoryError, any program or the
judgement process itself ended by a signal (every Go program runs with
``GOTRACEBACK=crash``, so its out-of-memory is SIGABRT, not exit 2) — is a
measured result: the memory condition does not hold. A run is complete only
when the generator gave a verdict (exit 0 or 1), amtool and the tree-mode
parse-tree ran, and served-values gave a usable document. Anything else is
not a measurement (exit 2), never a pass.

HOW PEAK MEMORY IS MEASURED (and its bias)
==========================================
* The judgement process: ``VmHWM`` of ``/proc/self/status`` at its end — the
  high-water RSS of its own address space since its exec. Not ``ru_maxrss``:
  Linux carries the high-water mark of the process it was forked from (the
  orchestrator) across exec into ``ru_maxrss``. ``ru_maxrss`` is kept in the
  report for comparison. A judgement process that dies has no figure.
* Every program it starts: the ``ru_maxrss`` of its ``wait4``, taken by a
  small launcher (``_LAUNCHER``) that forks and execs the program and reaps it
  (the launcher is what the judgement process starts; started straight from
  the judgement process, every program would read at least the judgement
  process's RSS, for the same reason as above). Each program's figure has the
  launcher's RSS (about 8 MiB) as its floor; its wall time is the launcher's
  fork to reap. The program dies with the launcher (``PR_SET_PDEATHSIG``), so
  a caller's timeout that kills the launcher leaves no program behind.

RSS is NOT what ``RLIMIT_DATA`` counts: RSS includes file-backed pages (the
binary's text, shared libraries) that ``RLIMIT_DATA`` does not count, and
leaves out private writable memory that is mapped but never touched, which
``RLIMIT_DATA`` does count (the Go runtime maps heap ahead of use). So RSS
can sit well below the limit while the limit is what ends the program. Two
more runs per write bound it from ``RLIMIT_DATA``'s own side:

* the fraction run: one judgement with ``RLIMIT_DATA`` at ``MEMORY_FRACTION``
  of the limit. Completing with the same verdict is the memory condition in
  ``RLIMIT_DATA``'s accounting;
* the diagnostic run: one judgement with no ``RLIMIT_DATA``, for every
  program's figures when a run under the limit was cut short. It is not part
  of the pass rule, except as the time basis when no run under the limit
  completed.

A judgement process that outlives ``_HANG_GUARD_S`` (a hang, not a slow
judgement) is ended with its whole process group, and the run is not a
measurement. Its process group is also ended when this tool is interrupted.

Exit codes: 0 measured, both conditions hold; 1 measured, a condition does not
hold; 2 not measured (no helm / amtool / da-guard / tree, a limit the render
does not give, a judgement that did not complete for a reason other than
memory).
"""
import os
import sys
import time

#: The judgement process (`--_judge`, started by this tool) records every
#: program it starts. Only there: the recorder must be in place before
#: anything imports subprocess (its poll captures os.waitpid at import time),
#: and an importer of this module (a test) must not get a patched os.
_JUDGING = __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "--_judge"

_STARTED: "dict[int, tuple[list[str], float, str | None]]" = {}
_REAPED: "list[dict]" = []


def _waitpid_with_rusage(pid, options):
    rpid, status, ru = os.wait4(pid, options)
    if rpid > 0:
        argv, t0, side = _STARTED.pop(rpid, (None, None, None))
        if argv is not None:
            rec = {"argv": argv, "wall_s": time.monotonic() - t0,
                   "maxrss_kib": ru.ru_maxrss, "exit": os.waitstatus_to_exitcode(status),
                   "launched": False}
            if side is not None:
                try:
                    with open(side, encoding="ascii") as fh:
                        kib, wall, st = fh.read().split()
                    rec.update(maxrss_kib=int(kib), wall_s=float(wall),
                               exit=os.waitstatus_to_exitcode(int(st)), launched=True)
                except (OSError, ValueError):
                    pass  # the launcher left no figures: the reap's own stand
            _REAPED.append(rec)
    return rpid, status


if _JUDGING:
    if "subprocess" in sys.modules:  # pragma: no cover - interpreter start never imports it
        raise SystemExit("measure_judgement_budget: subprocess was imported before the "
                         "wait4 recorder; per-program memory would not be recorded")
    os.waitpid = _waitpid_with_rusage

import argparse  # noqa: E402
import base64  # noqa: E402
import json  # noqa: E402
import math  # noqa: E402
import re  # noqa: E402
import resource  # noqa: E402
import shutil  # noqa: E402
import signal  # noqa: E402
import statistics  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402
from pathlib import Path  # noqa: E402

#: In the judgement process: where each started program's own figures go.
#: Set by `judge`; None elsewhere.
_LAUNCH_DIR: "str | None" = None
_LAUNCHED = [0]

# The launcher (see the module docstring): fork, PR_SET_PDEATHSIG(SIGKILL) in
# the child so the program dies with the launcher, exec the program with every
# descriptor and the environment as given, reap it with wait4, write
# "maxrss_kib wall_s status" to the side file, end the way the program ended
# (same exit code, or the same signal).
_LAUNCHER = r"""
import os, sys, time, signal
side, argv = sys.argv[1], sys.argv[2:]
t0 = time.monotonic()
pid = os.fork()
if pid == 0:
    try:
        import ctypes
        ctypes.CDLL(None).prctl(1, signal.SIGKILL)
        if os.getppid() == 1:
            os._exit(137)
        os.execvp(argv[0], argv)
    except BaseException:
        os._exit(127)
_, status, ru = os.wait4(pid, 0)
fd = os.open(side, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
os.write(fd, ("%d %.6f %d\n" % (ru.ru_maxrss, time.monotonic() - t0, status)).encode())
os.close(fd)
if os.WIFSIGNALED(status):
    sig = os.WTERMSIG(status)
    signal.signal(sig, signal.SIG_DFL)
    os.kill(os.getpid(), sig)
os._exit(os.waitstatus_to_exitcode(status) if os.WIFEXITED(status) else 1)
"""

_orig_popen_init = subprocess.Popen.__init__


def _popen_init(self, args, *a, **k):
    t0 = time.monotonic()
    argv = ([os.fsdecode(x) for x in args] if not isinstance(args, (str, bytes))
            else [os.fsdecode(args)])
    side = None
    if _LAUNCH_DIR is not None and not isinstance(args, (str, bytes)) and not k.get("shell"):
        _LAUNCHED[0] += 1
        side = os.path.join(_LAUNCH_DIR, f"{_LAUNCHED[0]:04d}")
        args = [sys.executable, "-I", "-S", "-c", _LAUNCHER, side, *argv]
    _orig_popen_init(self, args, *a, **k)
    _STARTED[self.pid] = (argv, t0, side)


if _JUDGING:
    subprocess.Popen.__init__ = _popen_init

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_TOOLS = os.path.join(_THIS_DIR, "..")
_REPO = os.path.abspath(os.path.join(_TOOLS, "..", ".."))
sys.path.insert(0, _TOOLS)
sys.path.insert(0, os.path.join(_TOOLS, "ops"))
from _lib_exitcodes import EXIT_CALLER_ERROR, EXIT_OK, EXIT_VIOLATION  # noqa: E402
from _lib_io import safe_label  # noqa: E402

#: ADR-036 §3's pass rule (the owner's decision rule, not a limit): peak
#: memory ≤ 75 % of the judgement limit, wall time ≤ half the deadline.
MEMORY_FRACTION = 0.75
TIME_FRACTION = 0.5

_CHART = os.path.join(_REPO, "helm", "tenant-api")
#: The chart refuses to render without writeBack.mode and a git conf.d; the
#: tests' render baseline sets the smallest acknowledged values (no generator
#: setting among them, no policy file).
_RENDER_BASELINE = os.path.join(_REPO, "tests", "helm", "tenant_api_render_baseline.yaml")
_FIXTURE_TOOL = os.path.join(_THIS_DIR, "generate_tenant_fixture.py")
_GENERATOR_SERVICE = os.path.join(_TOOLS, "ops", "generator_service.py")
_SELF_LABEL = "judgement process (python)"
#: The generator's verdicts: 0 pass, 1 findings that block. Any other exit
#: is no verdict.
_VERDICTS = (0, 1)

#: Only a guard against a hang (helm, git, da-guard, a judgement process),
#: never a limit of the measurement: a judgement is timed, not stopped, at the
#: judgement deadline.
_HANG_GUARD_S = 1800

EXIT_HOLD, EXIT_NOT_HOLD, EXIT_NOT_MEASURED = EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR
MiB = 1024 * 1024


class NotMeasured(Exception):
    """The measurement cannot be made; the message says why (exit 2)."""


def _write_private(path: str, text: str) -> None:
    """A scratch file of this measurement (0o600, inside its work dir)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        data = text.encode("utf-8")
        while data:
            data = data[os.write(fd, data):]
    finally:
        os.close(fd)


# ── limits ───────────────────────────────────────────────────────────────

def _cpu_quantity(q: str) -> float:
    q = str(q).strip()
    if q.endswith("m"):
        return int(q[:-1]) / 1000.0
    return float(q)


def _source_line(path: str, name: str) -> str:
    with open(path, encoding="utf-8") as fh:
        for i, line in enumerate(fh, 1):
            if re.match(rf"{re.escape(name)}\s*=", line):
                return f"{os.path.relpath(path, _REPO)}:{i}"
    return os.path.relpath(path, _REPO)


def read_limits(chart: str, values: "list[str]") -> dict:
    """The generator container's judgement limits, as the chart renders them."""
    import yaml  # rendered Kubernetes manifests, not conf.d
    if shutil.which("helm") is None:
        raise NotMeasured("helm is not on PATH: the limits are read from the rendered chart")
    cmd = ["helm", "template", "m", chart]
    for v in values:
        cmd += ["-f", v]
    proc = subprocess.run(cmd, capture_output=True, check=False, timeout=_HANG_GUARD_S)
    if proc.returncode != 0:
        raise NotMeasured(f"helm template exited {proc.returncode}: "
                          f"{proc.stderr.decode('utf-8', 'replace').strip()}")
    container = None
    for doc in yaml.safe_load_all(proc.stdout):
        if not isinstance(doc, dict) or doc.get("kind") != "Deployment":
            continue
        for c in doc["spec"]["template"]["spec"].get("containers", []):
            if (c.get("args") or [None])[0] == "generator-service":
                container = c
    if container is None:
        raise NotMeasured("the rendered chart has no container running generator-service")
    import generator_service
    args = generator_service._build_parser().parse_args(container["args"][1:])
    given = {a.split("=", 1)[0] for a in container["args"][1:]}

    def origin(flag: str, default_name: str) -> str:
        if flag in given:
            return f"chart arg {flag} ({os.path.relpath(chart, _REPO)}, rendered)"
        return (f"generator-service default {default_name} "
                f"({_source_line(_GENERATOR_SERVICE, default_name)}); the chart passes no {flag}")

    cpu = ((container.get("resources") or {}).get("limits") or {}).get("cpu")
    if cpu is None:
        raise NotMeasured("the generator container renders no resources.limits.cpu")
    return {
        "memory_limit_bytes": args.memory_limit_bytes,
        "memory_limit_from": origin("--memory-limit-bytes", "DEFAULT_MEMORY_LIMIT_BYTES"),
        "deadline_seconds": args.max_judgement_seconds,
        "deadline_from": origin("--max-judgement-seconds", "DEFAULT_MAX_JUDGEMENT_SECONDS"),
        "policy": args.policy,
        "cpu_limit": str(cpu),
        "cpus": max(1, math.ceil(_cpu_quantity(cpu))),
        "cpu_from": f"{os.path.relpath(chart, _REPO)}, rendered: generator resources.limits.cpu",
    }


# ── the tree and the writes ──────────────────────────────────────────────

def _git(repo: str, *args: str) -> str:
    env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="measure",
               GIT_AUTHOR_EMAIL="measure@invalid", GIT_COMMITTER_NAME="measure",
               GIT_COMMITTER_EMAIL="measure@invalid")
    proc = subprocess.run(["git", "-C", repo, "-c", "commit.gpgsign=false",
                           "-c", f"core.excludesFile={os.devnull}", *args],
                          capture_output=True, env=env, check=False, timeout=_HANG_GUARD_S)
    if proc.returncode != 0:
        raise NotMeasured(f"git {args[0]}: {proc.stderr.decode('utf-8', 'replace').strip()}")
    return proc.stdout.decode("utf-8").strip()


def prepare_repo(tree: "str | None", work: str, count: int, seed: int, da_guard: str) -> str:
    """A git repository whose root is the conf.d (the chart's --repo=/conf.d,
    --config-subdir=): the given tree copied and committed whole, or a
    generated synthetic-v2 fixture of *count* tenants."""
    repo = os.path.join(work, "repo")
    if tree is not None:
        shutil.copytree(tree, repo, symlinks=True, ignore=shutil.ignore_patterns(".git"))
    else:
        proc = subprocess.run(
            [sys.executable, _FIXTURE_TOOL, "--layout", "synthetic-v2", "--count", str(count),
             "--with-defaults", "--seed", str(seed), "--output", repo],
            capture_output=True, env=dict(os.environ, DA_GUARD_BINARY=da_guard), check=False,
            timeout=_HANG_GUARD_S)
        if proc.returncode != 0:
            raise NotMeasured("generate_tenant_fixture.py exited "
                              f"{proc.returncode}: {proc.stderr.decode('utf-8', 'replace')[-2000:]}")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A", "-f")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "base")
    return repo


_VALUE_LINE = re.compile(rb'^(\s+[A-Za-z][A-Za-z0-9_]*: )("?)(\d+)((?:\.\d+)?"?)\s*$', re.M)


def _bump_value(repo: str, rel: str) -> bytes:
    """*rel*'s bytes with its first numeric value line raised by one."""
    old = Path(repo, rel).read_bytes()
    m = _VALUE_LINE.search(old)
    if m is None:
        raise NotMeasured(f"{rel}: no numeric value line to change")
    return old[:m.start(3)] + str(int(m.group(3)) + 1).encode() + old[m.end(3):]


def _in_dir(rel: str, d: str) -> bool:
    return d == "." or rel.startswith(d.rstrip("/") + "/")


def choose_writes(repo: str, da_guard: str) -> "list[dict]":
    """The two writes (module docstring): a tenant file, a defaults file.
    Tenants are read through parse-tree, as every conf.d reader is."""
    import _lib_parse_cache
    reader = _lib_parse_cache.ConfdReader(repo, binary=da_guard)
    declared, _unknown = _lib_parse_cache.declared_tenants(reader)
    by_file: "dict[str, set[str]]" = {}
    for tenant, files in declared.items():
        for f in files:
            by_file.setdefault(f, set()).add(tenant)
    if not by_file:
        raise NotMeasured("the tree declares no tenant")
    writes = []

    rel = sorted(by_file, key=lambda f: (-len(by_file[f]), f))[0]
    new = _bump_value(repo, rel)
    after = _lib_parse_cache.read_text_as(repo, rel, new, binary=da_guard)
    after_tenants = set((after.get("tenants") or {}).keys()) if isinstance(after, dict) else set()
    writes.append({"kind": "tenant file", "path": rel,
                   "content_b64": base64.b64encode(new).decode("ascii"),
                   "tenants": sorted(by_file[rel] | after_tenants)})

    carriers = dict(reader.tree().defaults_carriers or {})
    if carriers:
        def below(d: str) -> "set[str]":
            return {t for f, ts in by_file.items() if _in_dir(f, d) for t in ts}
        d = "." if "." in carriers else sorted(carriers, key=lambda x: (-len(below(x)), x))[0]
        crel = carriers[d]
        cnew = _bump_value(repo, crel)
        writes.append({"kind": "defaults file", "path": crel,
                       "content_b64": base64.b64encode(cnew).decode("ascii"),
                       "tenants": sorted(below(d))})
    for w in writes:
        w["tenant_count_in_tree"] = len(declared)
    return writes


# ── one judgement (the judgement process) ────────────────────────────────

def _label(argv: "list[str]") -> str:
    name = os.path.basename(argv[0])
    rest = argv[1:]
    if name == "git":
        while rest and rest[0] in ("-C", "-c"):
            rest = rest[2:]
        return f"git {rest[0]}" if rest else "git"
    if rest and not rest[0].startswith("-"):
        if rest[0] == "parse-tree":
            mode = ("list" if "--list-only" in rest else "file" if "--path" in rest
                    else "names" if "--names" in rest else "tree")
            return f"{name} parse-tree ({mode})"
        return f"{name} {rest[0]}"
    return name


def _vm_hwm_kib() -> "int | None":
    try:
        for line in Path("/proc/self/status").read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("VmHWM:"):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return None


def judge(spec: dict) -> dict:
    """Runs in the judgement process; returns its report (written as JSON)."""
    import _gsvc_child
    import _gsvc_proto
    import _grar_parse
    import _lib_parse_tree
    import generate_alertmanager_routes as gar
    global _LAUNCH_DIR
    t_ready = time.monotonic()
    _lib_parse_tree.run_under_memory_limit()
    # Every Go program of the judgement (amtool and served-values included,
    # not only the parse-tree runs _lib_parse_tree starts): an out-of-memory
    # is then a signal, never an exit 2 read as "no verdict" (module docstring).
    os.environ["GOTRACEBACK"] = "crash"
    job_dir = spec["job_dir"]
    _LAUNCH_DIR = os.path.join(job_dir, "launched")
    os.mkdir(_LAUNCH_DIR, 0o700)
    tree_dir = os.path.join(job_dir, "conf.d")
    w = spec["write"]
    request = _gsvc_proto.ValidateRequest(
        spec["base_commit"], (_gsvc_proto.Write(w["path"], w["content_b64"]),),
        int(spec["deadline_seconds"] * 1000))
    job = _gsvc_child.ChildJob(
        request=request, job_dir=job_dir, repo=spec["repo"], config_subdir="", git="git",
        object_format="sha1", memory_limit=spec["memory_limit"] or 0, policy_copy=None,
        policy_sha256=None, store_entries={}, max_transfer=0)
    stages = {}
    t = time.monotonic()
    tree = _gsvc_child.read_tree(job)
    irregular = _gsvc_child.materialise(tree_dir, tree)
    del tree
    _gsvc_child.apply_writes(tree_dir, request, irregular)
    stages["git read + write"] = time.monotonic() - t

    findings_path = os.path.join(job_dir, "findings.json")
    argv = ["--config-dir", tree_dir, "--validate", "--strict", "--findings-json", findings_path]
    _grar_parse.set_git_irregular_entries(irregular)
    t = time.monotonic()
    memory, rc = None, None
    try:
        gar.main(argv)
        rc = 0
    except SystemExit as exc:
        rc = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    except MemoryError as exc:
        memory = str(exc) or "MemoryError"
    finally:
        _grar_parse.set_git_irregular_entries(None)
    stages["generator (parse-tree, routes, amtool)"] = time.monotonic() - t
    sys.stdout.flush()
    findings = None
    try:
        with open(findings_path, encoding="utf-8") as fh:
            doc = json.load(fh)
        if isinstance(doc, dict):
            findings = {}
            for f in doc.get("findings", []):
                key = f"{f.get('kind')}/{f.get('severity')}"
                findings[key] = findings.get(key, 0) + 1
    except (OSError, ValueError):
        pass

    t = time.monotonic()
    cmd = [spec["da_guard"], "served-values", "--config-dir", tree_dir]
    for tenant in w["tenants"]:
        cmd += ["--tenant", tenant]
    sv_rc, sv_ok, absent = None, False, None
    try:
        sv = subprocess.run(cmd, capture_output=True, check=False, timeout=_HANG_GUARD_S)
        sv_rc = sv.returncode
        if sv.returncode == 0:
            sv_doc = json.loads(sv.stdout)
            absent = sv_doc.get("tenants_absent")
            sv_ok = sv_doc.get("schema") == "da-guard.served-values/v1" and not absent
        del sv
    except MemoryError as exc:
        memory = memory or f"served-values output: {exc}"
    except ValueError:
        pass
    stages["served-values --tenant"] = time.monotonic() - t
    return {
        "affinity": sorted(os.sched_getaffinity(0)),
        "rlimit_data": resource.getrlimit(resource.RLIMIT_DATA)[0],
        "gotraceback": os.environ.get("GOTRACEBACK"),
        "judge_s": time.monotonic() - t_ready,
        "stages": stages,
        "generator_rc": rc, "memory_error": memory, "findings": findings,
        "served_values_rc": sv_rc, "served_values_ok": sv_ok, "tenants_absent": absent,
        "self_hwm_kib": _vm_hwm_kib(),
        "self_maxrss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "children": [{**r, "label": _label(r["argv"])} for r in _REAPED],
    }


def _judge_main(spec_path: str) -> int:
    with open(spec_path, encoding="utf-8") as fh:
        spec = json.load(fh)
    os.chdir(spec["job_dir"])
    null = os.open(os.devnull, os.O_RDONLY)
    os.dup2(null, 0)
    os.close(null)
    for fd, name in ((1, "stdout"), (2, "stderr")):
        out = os.open(os.path.join(spec["job_dir"], name),
                      os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.dup2(out, fd)
        os.close(out)
    report = judge(spec)
    _write_private(spec["report"], json.dumps(report))
    return 0


def _end_group(pid: int) -> None:
    """End the judgement process's whole process group (setsid) and reap it."""
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        pass


def run_judgement(spec: dict, cpus: "list[int]", limit: "int | None") -> dict:
    """One judgement process pinned to *cpus*, under RLIMIT_DATA *limit*
    (None: none); its report plus the wall time seen from here."""
    os.makedirs(spec["job_dir"], mode=0o700)
    spec = dict(spec, memory_limit=limit, report=os.path.join(spec["job_dir"], "report.json"))
    spec_path = os.path.join(spec["job_dir"], "spec.json")
    _write_private(spec_path, json.dumps(spec))

    def pre() -> None:
        os.setsid()
        os.sched_setaffinity(0, cpus)
        if limit is not None:
            resource.setrlimit(resource.RLIMIT_DATA, (limit, limit))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

    env = dict(os.environ, DA_GUARD_BINARY=spec["da_guard"], PYTHONDONTWRITEBYTECODE="1")
    t0 = time.monotonic()
    proc = subprocess.Popen([sys.executable, os.path.abspath(__file__), "--_judge", spec_path],
                            preexec_fn=pre, env=env)
    hung = False
    try:
        while True:
            pid, status, _ru = os.wait4(proc.pid, os.WNOHANG)
            if pid:
                break
            if time.monotonic() - t0 > _HANG_GUARD_S:
                hung = True
                _end_group(proc.pid)
                status = None
                break
            time.sleep(0.05)
    except BaseException:
        _end_group(proc.pid)
        proc.returncode = -signal.SIGKILL
        raise
    wall = time.monotonic() - t0
    if status is not None:
        # Whatever it started that outlived it (none should) goes with it.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    proc.returncode = os.waitstatus_to_exitcode(status) if status is not None else -signal.SIGKILL
    report = None
    try:
        with open(spec["report"], encoding="utf-8") as fh:
            report = json.load(fh)
    except (OSError, ValueError):
        pass
    try:
        tail = Path(spec["job_dir"], "stderr").read_text("utf-8", "replace")[-3000:]
    except OSError:
        tail = ""
    run = {"limit": limit, "exit": proc.returncode, "hung": hung, "wall_s": wall,
           "report": report, "stderr_tail": tail}
    run["outcome"], run["why"] = outcome(run)
    return run


# ── classification and the result ────────────────────────────────────────

def outcome(run: dict) -> "tuple[str, str | None]":
    """("complete", None); ("memory", why) — the limit ended part of the
    judgement, a measured result; ("failed", why) — the same in a run with no
    limit, which says nothing about the limit; or ("incomplete", why), no
    measurement."""
    limited = run["limit"] is not None
    ended = "memory" if limited else "failed"
    rep = run["report"]
    if run.get("hung"):
        return "incomplete", f"judgement process still running after {_HANG_GUARD_S} s: ended"
    if rep is None:
        if run["exit"] is not None and run["exit"] < 0:
            return ended, f"judgement process ended by signal {-run['exit']}"
        if "MemoryError" in run["stderr_tail"]:
            return ended, "judgement process: MemoryError"
        return "incomplete", f"judgement process exited {run['exit']}: {run['stderr_tail'][-800:]}"
    signalled = [c for c in rep["children"] if c["exit"] < 0]
    if rep["memory_error"] or signalled:
        why = rep["memory_error"] or ""
        if signalled:
            why += "; " + ", ".join(f"{c['label']} ended by signal {-c['exit']}" for c in signalled)
        return ended, why.strip("; ")
    unmeasured = [c["label"] for c in rep["children"] if not c["launched"]]
    if unmeasured:
        return "incomplete", f"no own figures (launcher side file) for: {', '.join(unmeasured)}"
    labels = {c["label"] for c in rep["children"]}
    if not any(lb.endswith("parse-tree (tree)") for lb in labels):
        return "incomplete", "no parse-tree tree-mode run: this is not the cache-less path"
    if rep["generator_rc"] not in _VERDICTS:
        return "incomplete", f"the generator gave no verdict (exit {rep['generator_rc']})"
    if "amtool check-config" not in labels:
        return "incomplete", "amtool check-config did not run: the generator's --validate skipped it"
    if not rep["served_values_ok"]:
        return "incomplete", (f"served-values --tenant gave no usable document (exit "
                              f"{rep.get('served_values_rc')}, absent: {rep['tenants_absent']})")
    return "complete", None


def _programs(run: dict) -> "dict[str, dict]":
    """label → {peak_mib (max), wall_s (sum), n, signalled} for one judgement."""
    rep = run["report"]
    if rep is None:
        return {}
    own = rep.get("self_hwm_kib") or rep["self_maxrss_kib"]
    out = {_SELF_LABEL: {"peak_mib": own / 1024, "wall_s": run["wall_s"], "n": 1,
                         "signalled": False}}
    for c in rep["children"]:
        p = out.setdefault(c["label"], {"peak_mib": 0.0, "wall_s": 0.0, "n": 0, "signalled": False})
        p["peak_mib"] = max(p["peak_mib"], c["maxrss_kib"] / 1024)
        p["wall_s"] += c["wall_s"]
        p["n"] += 1
        p["signalled"] = p["signalled"] or c["exit"] < 0
    return out


def _machine() -> dict:
    model = ""
    try:
        for line in Path("/proc/cpuinfo").read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("model name"):
                model = line.split(":", 1)[1].strip()
                break
    except OSError:
        pass
    return {"nproc": os.cpu_count(), "cpu_model": model, "kernel": os.uname().release,
            "python": sys.version.split()[0]}


def _table(runs: "list[dict]") -> "list[dict]":
    per_run = [_programs(r) for r in runs]
    labels = sorted({k for pr in per_run for k in pr}, key=lambda k: (k != _SELF_LABEL, k))
    rows = []
    for k in labels:
        cells = [pr.get(k) for pr in per_run]
        present = [c for c in cells if c is not None]
        rows.append({"program": k, "per_run": cells,
                     "peak_mib_median": statistics.median(c["peak_mib"] for c in present),
                     "wall_s_median": statistics.median(c["wall_s"] for c in present)})
    return rows


def _brief(r: dict) -> dict:
    return {"rlimit_data": r["limit"], "outcome": r["outcome"], "why": r["why"],
            "wall_s": r["wall_s"], "programs": _programs(r),
            "stages": (r["report"] or {}).get("stages"),
            "generator_rc": (r["report"] or {}).get("generator_rc"),
            "self_maxrss_mib": ((r["report"] or {}).get("self_maxrss_kib") or 0) / 1024}


def summarise_write(write: dict, limits: dict, runs, frac, diag) -> dict:
    """One write's result: per-program table and both conditions."""
    limit = limits["memory_limit_bytes"]
    mem_cap = MEMORY_FRACTION * limit / MiB
    time_cap = TIME_FRACTION * limits["deadline_seconds"]
    table = _table(runs)
    peaks = [c["peak_mib"] for row in table for c in row["per_run"] if c is not None]
    worst_peak = max(peaks) if peaks else None
    cut = [r for r in runs if r["outcome"] == "memory"]
    complete = [r for r in runs if r["outcome"] == "complete"]
    verdicts = {r["report"]["generator_rc"] for r in complete}
    frac_ok = (frac["outcome"] == "complete"
               and (not verdicts or frac["report"]["generator_rc"] in verdicts))
    memory_holds = (not cut and frac_ok and worst_peak is not None and worst_peak <= mem_cap)
    if complete:
        timed, time_basis = [r["wall_s"] for r in complete], "complete runs under the limit"
    elif diag["outcome"] == "complete":
        timed, time_basis = [diag["wall_s"]], "the unlimited run (no run under the limit completed)"
    else:
        timed, time_basis = [], "nothing: no complete run"
    time_worst = max(timed) if timed else None
    return {
        "write": {k: write[k] for k in ("kind", "path", "tenants", "tenant_count_in_tree")},
        "generator_rc": next(iter(sorted(verdicts)), None),
        "findings": complete[0]["report"]["findings"] if complete else None,
        "runs": [_brief(r) for r in runs], "programs": table,
        "judgement_wall_s": [r["wall_s"] for r in runs],
        "worst_peak_mib": worst_peak,
        "memory_rss_margin_pct": (100 * (mem_cap - worst_peak) / mem_cap
                                  if worst_peak is not None else None),
        "runs_cut_by_limit": len(cut),
        "memory_holds": memory_holds,
        "time_basis": time_basis, "time_worst_s": time_worst,
        "time_holds": time_worst is not None and time_worst <= time_cap,
        "time_margin_pct": (100 * (time_cap - time_worst) / time_cap
                            if time_worst is not None else None),
        "fraction_run": _brief(frac), "unlimited_run": _brief(diag),
    }


def summarise(limits: dict, cpus, per_write: "list[dict]", seen: "dict | None") -> dict:
    limit = limits["memory_limit_bytes"]
    return {
        "machine": _machine(), "cpus_pinned": cpus, "limits": limits, "seen": seen,
        "pass_rule": {"memory_mib_max": MEMORY_FRACTION * limit / MiB,
                      "wall_s_max": TIME_FRACTION * limits["deadline_seconds"]},
        "writes": per_write,
        "memory_holds": all(w["memory_holds"] for w in per_write),
        "time_holds": all(w["time_holds"] for w in per_write),
    }


# ── the command ──────────────────────────────────────────────────────────

def main(argv: "list[str] | None" = None) -> int:
    p = argparse.ArgumentParser(
        description="Measure one complete generator-container judgement (git read, "
                    "parse-tree tree mode, generate-routes --validate --strict with amtool, "
                    "served-values --tenant), for a tenant-file and a defaults-file write, "
                    "against ADR-036 section 3's budget, with the limits read from the Helm "
                    "chart and generator-service's own parser.")
    p.add_argument("--tree", help="conf.d to measure (default: generate a synthetic-v2 fixture)")
    p.add_argument("--count", type=int, default=5000,
                   help="tenants of the generated fixture (default: %(default)s)")
    p.add_argument("--seed", type=int, default=42, help="fixture seed (default: %(default)s)")
    p.add_argument("--runs", type=int, default=3,
                   help="judgements per write under the full limit (default: %(default)s)")
    p.add_argument("--chart", default=_CHART, help="tenant-api chart (default: the repo's)")
    p.add_argument("--values", action="append", default=None,
                   help="values file for the render (repeatable; default: the tests' render baseline)")
    p.add_argument("--da-guard-binary", default=None,
                   help="da-guard to run (default: $DA_GUARD_BINARY, then PATH)")
    p.add_argument("--work-dir", default=None,
                   help="scratch directory to create and keep (must not exist; default: a new "
                        "temp dir, removed)")
    p.add_argument("--_judge", help=argparse.SUPPRESS)
    a = p.parse_args(argv)
    if a._judge:
        return _judge_main(a._judge)
    if a.runs < 1:
        p.error("--runs must be at least 1")
    try:
        return measure(a)
    except NotMeasured as exc:
        print(f"measure_judgement_budget: not measured: {exc}", file=sys.stderr)
        return EXIT_NOT_MEASURED


def _preflight(a: argparse.Namespace) -> str:
    """Everything checkable before anything is created; returns da-guard."""
    da_guard = a.da_guard_binary or os.environ.get("DA_GUARD_BINARY") or shutil.which("da-guard")
    if not da_guard or not os.access(da_guard, os.X_OK):
        raise NotMeasured("no da-guard (--da-guard-binary, $DA_GUARD_BINARY, PATH); "
                          "`make da-guard-build` builds .build/da-guard")
    if shutil.which("amtool") is None:
        raise NotMeasured("amtool is not on PATH: the judgement would not be complete "
                          "(the container answers VALIDATOR_UNAVAILABLE)")
    if a.tree is not None and not os.path.isdir(a.tree):
        raise NotMeasured(f"--tree {a.tree} is not a directory")
    if a.work_dir is not None and os.path.lexists(a.work_dir):
        raise NotMeasured(f"--work-dir {a.work_dir} already exists: name a new directory")
    return os.path.abspath(da_guard)


def measure(a: argparse.Namespace) -> int:
    da_guard = _preflight(a)
    limits = read_limits(a.chart, a.values if a.values is not None else [_RENDER_BASELINE])
    if limits.get("policy"):
        raise NotMeasured("the rendered chart passes --policy; this tool measures no policy file")
    allowed = sorted(os.sched_getaffinity(0))
    if len(allowed) < limits["cpus"]:
        raise NotMeasured(f"{limits['cpus']} CPUs needed, {len(allowed)} available")
    cpus = allowed[:limits["cpus"]]
    work = a.work_dir or tempfile.mkdtemp(prefix="judgement-budget-")
    os.makedirs(work, exist_ok=True)
    try:
        repo = prepare_repo(a.tree, work, a.count, a.seed, da_guard)
        writes = choose_writes(repo, da_guard)
        base = _git(repo, "rev-parse", "HEAD")
        limit = limits["memory_limit_bytes"]
        per_write, seen = [], None
        for wi, write in enumerate(writes, 1):
            spec = {"repo": repo, "base_commit": base, "write": write, "da_guard": da_guard,
                    "deadline_seconds": limits["deadline_seconds"]}

            def one(name: str, lim: "int | None") -> dict:
                r = run_judgement(dict(spec, job_dir=os.path.join(work, f"w{wi}-{name}")), cpus, lim)
                if r["outcome"] == "incomplete":
                    raise NotMeasured(f"{write['kind']} {name} is not a complete judgement: {r['why']}")
                return r

            runs = [one(f"run-{i + 1}", limit) for i in range(a.runs)]
            frac = one("fraction", int(MEMORY_FRACTION * limit))
            diag = one("unlimited", None)
            for r in runs:
                if seen is None and r["report"] is not None:
                    seen = {k: r["report"][k] for k in ("affinity", "rlimit_data", "gotraceback")}
            per_write.append(summarise_write(write, limits, runs, frac, diag))
        result = summarise(limits, cpus, per_write, seen)
        print_result(result)
        if a.work_dir is not None:
            _write_private(os.path.join(work, "result.json"),
                           json.dumps(result, indent=2, ensure_ascii=False) + "\n")
        return EXIT_HOLD if result["memory_holds"] and result["time_holds"] else EXIT_NOT_HOLD
    finally:
        if a.work_dir is None:
            shutil.rmtree(work, ignore_errors=True)


def _fmt(v, spec: str, none: str = "n/a") -> str:
    return none if v is None else format(v, spec)


def print_result(r: dict) -> None:
    m, lim = r["machine"], r["limits"]
    seen = r["seen"] or {}
    print(f"machine: nproc={m['nproc']} cpu={m['cpu_model']!r} kernel={m['kernel']} "
          f"python={m['python']}; judgement pinned to CPUs {r['cpus_pinned']} "
          f"(seen inside: {seen.get('affinity')}, GOTRACEBACK={seen.get('gotraceback')})")
    print(f"RLIMIT_DATA {lim['memory_limit_bytes']} B <- {lim['memory_limit_from']} "
          f"(seen inside: {seen.get('rlimit_data')})")
    print(f"deadline {lim['deadline_seconds']} s <- {lim['deadline_from']}")
    print(f"CPU limit {lim['cpu_limit']} <- {lim['cpu_from']}; policy file: none")
    for w in r["writes"]:
        wr = w["write"]
        ten = wr["tenants"]
        shown = ", ".join(safe_label(t) for t in ten[:3]) + (f", ... ({len(ten)})" if len(ten) > 3 else "")
        print()
        print(f"== write: {wr['kind']} {safe_label(wr['path'])} (tenants {shown}; "
              f"{wr['tenant_count_in_tree']} in the tree); generator exit {w['generator_rc']}, "
              f"findings {w['findings']}")
        n = len(w["runs"])
        print("peak RSS MiB / wall s".ljust(38) + "  ".join(f"run{i + 1}".rjust(15) for i in range(n))
              + "   " + "median".rjust(15))
        for row in w["programs"]:
            cells = []
            for c in row["per_run"]:
                if c is None:
                    cells.append("not run".rjust(15))
                else:
                    mark = "!" if c.get("signalled") else " "
                    cells.append(f"{c['peak_mib']:6.1f}/{c['wall_s']:6.2f}{mark}".rjust(15))
            print(f"{row['program'][:37].ljust(38)}" + "  ".join(cells)
                  + f"   {row['peak_mib_median']:6.1f}/{row['wall_s_median']:6.2f} ".rjust(18))
        print("(! = ended by a signal under the limit; judgement process = its VmHWM)")
        for i, run in enumerate(w["runs"], 1):
            print(f"run{i}: {run['outcome']}" + (f" - {run['why']}" if run["why"] else ""))
        print("judgement wall s (start to reap): "
              + ", ".join(f"{x:.2f}" for x in w["judgement_wall_s"]))
        print(f"memory: worst peak RSS {_fmt(w['worst_peak_mib'], '.1f')} MiB vs "
              f"{r['pass_rule']['memory_mib_max']:.1f} MiB (RSS margin "
              f"{_fmt(w['memory_rss_margin_pct'], '.1f')}%); runs cut by the limit: "
              f"{w['runs_cut_by_limit']}/{n}; fraction run: {w['fraction_run']['outcome']} -> "
              f"{'holds' if w['memory_holds'] else 'DOES NOT HOLD'}")
        print(f"time: worst {_fmt(w['time_worst_s'], '.2f')} s of {w['time_basis']} vs "
              f"{r['pass_rule']['wall_s_max']:.2f} s -> "
              f"{'holds' if w['time_holds'] else 'DOES NOT HOLD'} "
              f"(margin {_fmt(w['time_margin_pct'], '.1f')}%)")
        for key, title in (("fraction_run", "fraction run"), ("unlimited_run", "unlimited run (diagnostic)")):
            f = w[key]
            progs = ", ".join(f"{k} {v['peak_mib']:.1f}/{v['wall_s']:.2f}" for k, v in f["programs"].items())
            print(f"{title}, RLIMIT_DATA {f['rlimit_data']}: {f['outcome']}"
                  + (f" ({f['why']})" if f["why"] else "") + f", {f['wall_s']:.2f} s; {progs}")
    print()
    print(f"OVERALL (worst write): memory {'holds' if r['memory_holds'] else 'DOES NOT HOLD'}; "
          f"time {'holds' if r['time_holds'] else 'DOES NOT HOLD'}")


if __name__ == "__main__":
    sys.exit(main())
