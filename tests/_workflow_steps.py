"""Running a workflow `run:` step the way GitHub does, and asking bash what a
`$?` in it can ever read (#3946).

Two things live here, shared by the tests that execute workflow steps:

  * `step_shell_argv` / `run_step` — the WHOLE `run:` text, in a file, under
    the flags GitHub gives that step's shell. ⛔ Never a slice of the script
    and never `bash -c`: a step with no `shell:` runs as `bash -e {0}`, so a
    bare non-zero exit ends it on the spot, and a test that starts after that
    line (or runs without `-e`) cannot see it (#3944, #3946).
  * `probe_status_reads` — for every command in a script that mentions `$?`,
    whether it can ever be reached with a non-zero `$?`. The answer comes from
    bash executing the script, not from matching its text: see that function.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIR = REPO_ROOT / ".github/workflows"

# What GitHub documents for `jobs.<id>.steps[*].shell`. Only these two are
# modelled; any other value is refused by `step_shell_argv`, never guessed.
_SHELL_FLAGS = {
    None: ["-e"],
    "bash": ["--noprofile", "--norc", "-eo", "pipefail"],
}


class UnmodelledShell(Exception):
    """A `shell:` whose flags this module does not know."""


def bash_path() -> str:
    """bash, or a failure — never a skip on CI: "could not look" must not read
    as "looked and found nothing"."""
    import pytest
    bash = shutil.which("bash")
    if bash is None:
        if os.environ.get("CI"):
            pytest.fail("bash is not on PATH: no workflow step can be executed")
        pytest.skip("needs bash")
    return bash


def step_shell_argv(shell: str | None) -> list[str]:
    """The argv GitHub runs a `run:` step with, from the step's EFFECTIVE
    `shell:` (see `effective_shell`). No `shell:` → `bash -e {0}`;
    `shell: bash` → `bash --noprofile --norc -eo pipefail {0}`. Either way
    errexit is ON, and `set -uo pipefail` in the script does not turn it off."""
    if shell not in _SHELL_FLAGS:
        raise UnmodelledShell(f"unmodelled `shell: {shell}` — teach "
                              "tests/_workflow_steps.py its flags")
    return [bash_path(), *_SHELL_FLAGS[shell]]


def effective_shell(doc: dict, job: dict, step: dict) -> str | None:
    """`shell:` on the step, else the job's `defaults.run.shell`, else the
    workflow's — GitHub's precedence."""
    for holder in (step, (job.get("defaults") or {}).get("run") or {},
                   (doc.get("defaults") or {}).get("run") or {}):
        if holder.get("shell") is not None:
            return holder["shell"]
    return None


@dataclass(frozen=True)
class RunStep:
    file: str
    job: str
    index: int
    name: str
    shell: str | None
    run: str

    @property
    def label(self) -> str:
        return f"{self.file} · {self.job} · #{self.index} {self.name}"


def run_steps(workflow_dir: Path = WORKFLOW_DIR) -> tuple[list[RunStep], list[str]]:
    """Every `run:` step of every workflow, and the files that could not be
    read as one — returned, not skipped: the caller must fail on them."""
    steps: list[RunStep] = []
    unread: list[str] = []
    for path in sorted([*workflow_dir.glob("*.yml"), *workflow_dir.glob("*.yaml")]):
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
            if not isinstance(doc, dict) or not isinstance(doc.get("jobs"), dict):
                raise TypeError("no `jobs:` mapping")
            for job_id, job in doc["jobs"].items():
                for index, step in enumerate(job.get("steps") or []):
                    if "run" not in step:
                        continue
                    if not isinstance(step["run"], str):
                        raise TypeError(f"{job_id} step #{index}: `run:` is not a string")
                    steps.append(RunStep(path.name, str(job_id), index,
                                         str(step.get("name", "")),
                                         effective_shell(doc, job, step), step["run"]))
        except (yaml.YAMLError, ValueError, AttributeError, TypeError) as err:
            unread.append(f"{path.name}: {type(err).__name__}: {str(err).splitlines()[0]}")
    return steps, unread


def run_step(run: str, shell: str | None, tmp_path: Path, env: dict,
             stand_ins: dict[str, str] | None = None, cwd: Path | None = None):
    """Run a step's entire `run:` text under its shell's flags. `stand_ins`:
    command name → the body of a `/bin/sh` script put first on PATH."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    for name, body in (stand_ins or {}).items():
        fake = bindir / name
        fake.write_text("#!/bin/sh\n" + body, encoding="utf-8")
        fake.chmod(0o755)
    script = tmp_path / "step.sh"
    script.write_text(run, encoding="utf-8")
    return subprocess.run(
        [*step_shell_argv(shell), str(script)], capture_output=True, text=True,
        encoding="utf-8", timeout=30, check=False, cwd=cwd or tmp_path,
        env={"PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}", **env})


# ── what can a `$?` read? ───────────────────────────────────────────────────

# Sourced through BASH_ENV, so it is in place before the script's first line.
# A DEBUG trap sees every simple command before it runs:
#   * a command that would touch the world (anything that is not an
#     assignment, a function of the script, or a builtin that only changes
#     shell state) is SKIPPED, which bash counts as success;
#   * the command named by __INJECT is replaced by a function that returns 1 —
#     so the failure happens at that exact place in the script and bash itself
#     decides what errexit does with it;
#   * a command whose text has `$?` outside single quotes is logged with the
#     `$?` it is about to read.
# `exit` is skipped too, so what follows an early `exit` is still walked.
_PRELUDE = r"""
set -T
command_not_found_handle() { [[ -n ${__FAIL_CNF-} ]] && return 127; return 0; }
__scan() {  # __W: the command word of $1 ('' = assignments only); __R: reads $?
  local s=$1 i n c depth q
  __R=0
  n=${#s}; q=
  for ((i = 0; i < n; i++)); do
    c=${s:i:1}
    if [[ $q == "'" ]]; then [[ $c == "'" ]] && q=; continue; fi
    case $c in
      \\) ((i++)) ;;
      "'") [[ $q ]] || q="'" ;;
      '"') if [[ $q ]]; then q=; else q='"'; fi ;;
      '$') [[ ${s:i+1:1} == '?' ]] && __R=1 ;;
    esac
  done
  while [[ $s =~ ^[A-Za-z_][A-Za-z0-9_]*(\[[^]]*\])?\+?= ]]; do
    s=${s:${#BASH_REMATCH[0]}}; n=${#s}; depth=0; q=
    for ((i = 0; i < n; i++)); do
      c=${s:i:1}
      if [[ $q == "'" ]]; then [[ $c == "'" ]] && q=; continue; fi
      case $c in
        \\) ((i++)) ;;
        "'") [[ $q ]] || q="'" ;;
        '"') if [[ $q ]]; then q=; else q='"'; fi ;;
        '('|'{') ((depth++)) ;;
        ')'|'}') ((depth > 0)) && ((depth--)) ;;
        ' ') [[ -z $q ]] && ((depth == 0)) && break ;;
      esac
    done
    s=${s:i}
    while [[ $s == ' '* ]]; do s=${s# }; done
  done
  __W=${s%% *}
}
__dbg() {  # returns 0 to run the command, 1 to skip it
  local st=$1 ln=$2 cmd=${BASH_COMMAND//[$'\n\t']/ } __W __R key word native=0 canfail=1
  builtin set +u
  __FAIL_CNF=
  shopt -q extdebug || shopt -s extdebug
  [[ ${FUNCNAME[2]-} == command_not_found_handle ]] && return 0
  [[ ${FUNCNAME[2]-} == "${__INJECTED-}" && -n ${__INJECTED-} ]] && return 0
  __N=$((${__N-0} + 1))
  if ((__N > __MAX)); then
    builtin printf 'CUT\n' >> "$__LOG"
    builtin exit 97
  fi
  __scan "$cmd"
  key="$ln:$cmd"
  [[ $__W == trap ]] && __R=0
  # native: 1 = runs, always · 2 = runs, and may be skipped to explore ("pass")
  # · 3 = a plain `name=…`: may be skipped, or given a stand-in value ("fill")
  # so a loop or a test over the output of a skipped command is entered.
  #
  # canfail: whether making it fail is something that can happen. Not for
  # what cannot return non-zero in practice (`local x=$(cmd)` is 0 whatever
  # cmd does — failing `local` itself would call a masked read live), and not
  # for a function of the script (its commands are failed one by one instead).
  if [[ -z $__W ]]; then
    native=2; canfail=0
    [[ $cmd =~ ^[A-Za-z_][A-Za-z0-9_]*= ]] && native=3
  else
    case $cmd in
      '[[ '*|'(('*) native=2; canfail=0 ;;
      'for '*|'case '*|'select '*) native=1; canfail=0 ;;
    esac
    case $__W in
      '['|test|let) native=2 ;;
      export|local|declare|typeset|readonly) native=2; canfail=0 ;;
      read|mapfile|readarray) native=1 ;;
      set|shopt|unset|shift|return|break|continue|:|true|false|trap|getopts)
        native=1; canfail=0 ;;
    esac
    if ((native == 0)) && declare -F -- "$__W" > /dev/null 2>&1; then native=1; canfail=0; fi
  fi
  builtin printf 'CMD\t%s\t%s\t%s\t%s\t%s\n' "$st" "$__R" "$native" "$canfail" "$key" >> "$__LOG"
  if ((canfail)) && [[ $'\n'"${__INJECT-}"$'\n' == *$'\n'"fail:$key"$'\n'* ]]; then
    word=$__W
    # Expanded only when that cannot run anything: the DEBUG trap does not
    # fire inside itself, so a `$(…)` here would really execute.
    if [[ ! $word =~ ^[A-Za-z0-9_.+-]+$|^\[$ && $word != *'$('* && $word != *'`'* \
          && $word != *'<('* && $word != *'>('* ]]; then
      builtin eval "word=$__W" 2> /dev/null || word=$__W
    fi
    if [[ $word =~ ^[A-Za-z0-9_.+-]+$|^\[$ ]]; then
      builtin printf 'INJECTED\t%s\n' "$key" >> "$__LOG"
      __INJECTED=$word
      builtin eval "function $word { builtin unset -f $word; builtin return 1; }"
      return 0
    elif [[ -z $word ]]; then      # the name expands to nothing: the handler fails it
      builtin printf 'INJECTED\t%s\n' "$key" >> "$__LOG"
      __FAIL_CNF=1
      return 0
    elif [[ $word == */* && ! -e $word ]]; then   # a path that is not there: bash fails it
      builtin printf 'INJECTED\t%s\n' "$key" >> "$__LOG"
      return 0
    fi
    builtin printf 'UNINJECTABLE\t%s\n' "$key" >> "$__LOG"
  fi
  if [[ $'\n'"${__INJECT-}"$'\n' == *$'\n'"pass:$key"$'\n'* ]]; then
    return 1
  fi
  if [[ $'\n'"${__INJECT-}"$'\n' == *$'\n'"fill:$key"$'\n'* ]]; then
    builtin eval "${cmd%%=*}=__filled__"
    return 1
  fi
  ((native > 0)) && return 0
  return 1
}
__dbgskip() { __dbg "$@" && return 1; return 0; }
trap '! __dbgskip "$?" "$LINENO"' DEBUG
"""

_EXPR = re.compile(r"\$\{\{.*?\}\}", re.DOTALL)


class NoConfinement(Exception):
    """This machine cannot confine the probe's writes, so it does not run."""


# linux/landlock.h
_FS_WRITE_FILE, _FS_REFER, _FS_TRUNCATE = 1 << 1, 1 << 13, 1 << 14
_FS_CHANGES = sum(1 << bit for bit in (1, 4, 5, 6, 7, 8, 9, 10, 11, 12))  # write, remove, make
_SYS_CREATE_RULESET, _SYS_ADD_RULE, _SYS_RESTRICT_SELF = 444, 445, 446


def _confinement():
    """A function that, called in the child before bash starts, takes away
    the right to change any file outside the given paths (Landlock; Linux
    5.13+, no privilege needed, inherited by every process bash starts).

    ⛔ Why the probe refuses to run without it: bash performs a command's
    redirections even when the command itself is replaced, and a script on a
    machine that is not a runner has empty `$RUNNER_TEMP`-like variables — so
    `> "$RUNNER_TEMP/x"` is `> /x`, and `> /etc/…` in a step would truncate
    the real file. Measured while writing this: nine empty files in `/`."""
    import ctypes
    import sys
    if not sys.platform.startswith("linux"):
        raise NoConfinement(f"no Landlock on {sys.platform}")
    libc = ctypes.CDLL(None, use_errno=True)
    abi = libc.syscall(_SYS_CREATE_RULESET, None, ctypes.c_size_t(0), ctypes.c_uint32(1))
    if abi < 1:
        raise NoConfinement(f"Landlock is not available: {os.strerror(ctypes.get_errno())}")
    handled = _FS_CHANGES | (_FS_REFER if abi >= 2 else 0) | (_FS_TRUNCATE if abi >= 3 else 0)

    class PathBeneath(ctypes.Structure):
        _pack_ = 1
        _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]

    def confine(writable: list[Path]) -> None:
        def check(result: int, what: str) -> int:
            if result < 0:
                raise OSError(ctypes.get_errno(), f"landlock {what}")
            return result
        attr = ctypes.c_uint64(handled)
        ruleset = check(libc.syscall(_SYS_CREATE_RULESET, ctypes.byref(attr),
                                     ctypes.c_size_t(8), ctypes.c_uint32(0)), "create_ruleset")
        for path in [*writable, Path(os.devnull)]:
            fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
            # Only the rights that apply to a file may be granted on a file.
            access = handled if path.is_dir() else handled & (_FS_WRITE_FILE | _FS_TRUNCATE)
            rule = PathBeneath(access, fd)
            check(libc.syscall(_SYS_ADD_RULE, ctypes.c_int(ruleset), ctypes.c_int(1),
                               ctypes.byref(rule), ctypes.c_uint32(0)), "add_rule")
            os.close(fd)
        check(libc.prctl(38, 1, 0, 0, 0), "no_new_privs")   # PR_SET_NO_NEW_PRIVS
        check(libc.syscall(_SYS_RESTRICT_SELF, ctypes.c_int(ruleset), ctypes.c_uint32(0)),
              "restrict_self")
        os.close(ruleset)
    return confine


MAX_COMMANDS = 3000   # per run of a script: a loop that never ends is cut here
MAX_RUNS = 600        # per script


@dataclass
class Site:
    """One command that mentions `$?` (outside single quotes)."""
    line: int
    command: str
    statuses: set[int] = field(default_factory=set)       # every `$?` it was reached with
    after: set[str] = field(default_factory=set)          # the commands logged just before it
    after_failed: set[str] = field(default_factory=set)   # …of those, the ones made to fail

    @property
    def verdict(self) -> str:
        """`live`: reached at least once with a non-zero `$?`.
        `dead`: reached, the command before it was made to fail, and still it
        only ever read 0 — that failure ends the script (errexit) or is
        masked before the read.
        `undetermined`: reached with 0 only, and no command before it could be
        made to fail — nothing is concluded."""
        if self.statuses - {0}:
            return "live"
        return "dead" if self.after_failed else "undetermined"


@dataclass
class Probe:
    sites: list[Site]
    runs: int
    cut: bool          # some run hit MAX_COMMANDS, or MAX_RUNS was reached
    unseen: list[int]  # script lines with `$?` no logged command accounts for


def _status_lines(run: str) -> list[int]:
    """1-based lines of the script that hold `$?` and are not comment lines."""
    return [n for n, line in enumerate(run.splitlines(), 1)
            if "$?" in line and not line.lstrip().startswith("#")]


def probe_status_reads(run: str, shell: str | None, tmp_path: Path) -> Probe:
    """Execute `run` under its shell's flags with every outside effect
    removed — once as written, then once for every command with that command
    made to fail in place (and, for a test or an assignment, skipped or given
    a stand-in value), each on top of the run that first reached it — and
    report what every `$?` read.

    What this can and cannot see is stated in
    tests/ops/test_workflow_status_reads.py, next to the cases that pin it."""
    argv = step_shell_argv(shell)
    try:
        confine = _confinement()
    except NoConfinement as err:
        # Never a pass, and on CI never a skip: "could not look" must not
        # read as "looked and found nothing".
        import pytest
        if os.environ.get("CI"):
            pytest.fail(f"the `$?` probe cannot run here: {err}")
        pytest.skip(f"the `$?` probe cannot run here: {err}")
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "prelude.sh").write_text(_PRELUDE, encoding="utf-8")
    (tmp_path / "step.sh").write_text(_EXPR.sub("__GHA_EXPR__", run), encoding="utf-8")
    # The only place the script may write. It is also where the variables a
    # runner provides point, so `>> "$GITHUB_OUTPUT"` works as it does there.
    box = tmp_path / "box"
    for sub in ("work", "temp", "home"):
        (box / sub).mkdir(parents=True, exist_ok=True)
    log = tmp_path / "log"
    env = {"PATH": str(tmp_path / "no-such-directory"), "HOME": str(box / "home"),
           "TMPDIR": str(box / "temp"), "RUNNER_TEMP": str(box / "temp"),
           "GITHUB_WORKSPACE": str(box / "work"),
           **{f"GITHUB_{name}": str(box / name.lower())
              for name in ("OUTPUT", "STEP_SUMMARY", "ENV", "PATH", "STATE")},
           "BASH_ENV": str(tmp_path / "prelude.sh"), "__LOG": str(log),
           "__MAX": str(MAX_COMMANDS)}
    sites: dict[str, Site] = {}
    failed: set[str] = set()
    cut = False

    def once(inject: tuple[str, ...]) -> dict[str, tuple[str, bool]]:
        """One execution. Returns the commands it logged → how each ran."""
        nonlocal cut
        log.write_text("", encoding="utf-8")
        try:
            subprocess.run(
                [*argv, str(tmp_path / "step.sh")], stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60,
                check=False, cwd=box / "work", env={**env, "__INJECT": "\n".join(inject)},
                preexec_fn=lambda: confine([box, log]))
        except subprocess.TimeoutExpired:
            cut = True
        seen: dict[str, tuple[str, bool]] = {}
        previous: str | None = None
        for row in log.read_text(encoding="utf-8", errors="replace").splitlines():
            kind, _, rest = row.partition("\t")
            if kind == "CUT":
                cut = True
            elif kind == "INJECTED":
                failed.add(rest)
            elif kind == "CMD":
                status, reads, native, canfail, key = rest.split("\t", 4)
                seen.setdefault(key, (native, canfail == "1"))
                if reads == "1":
                    line, _, command = key.partition(":")
                    site = sites.setdefault(key, Site(int(line), command))
                    site.statuses.add(int(status))
                    if previous is not None:
                        site.after.add(previous)
                previous = key
        return seen

    # Each command gets its variants ONCE, on top of the injections of the run
    # that first reached it — so the number of runs grows with the number of
    # commands, not with the number of paths.
    known: set[str] = set()
    queue: list[tuple[str, ...]] = [()]
    runs = 0
    while queue:
        if runs >= MAX_RUNS:
            cut = True
            break
        inject = queue.pop(0)
        runs += 1
        for key, (native, canfail) in once(inject).items():
            if key in known:
                continue
            known.add(key)
            if canfail:
                queue.append((*inject, f"fail:{key}"))
            if native in ("2", "3"):
                queue.append((*inject, f"pass:{key}"))
            if native == "3":
                queue.append((*inject, f"fill:{key}"))
    for site in sites.values():
        site.after_failed = site.after & failed
    ordered = sorted(sites.values(), key=lambda s: (s.line, s.command))
    seen_lines = {s.line for s in ordered}
    return Probe(ordered, runs, cut, [n for n in _status_lines(run) if n not in seen_lines])
