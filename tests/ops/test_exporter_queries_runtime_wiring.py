"""Every pack with a queries file is run against a real database by CI (#3833).

`check_exporter_queries.py` has two halves. The static half derives the names a
`rule-packs/exporters/<pack>.yaml` serves; whether its SQL runs at all is only
answered by `scripts/ops/exporter_queries_runtime.sh <pack>`, which starts the
database and scrapes query-exporter. That half is worth nothing for a pack no
CI step runs it for — and the DB2 pack was exactly that until this module: the
registry named its queries file, the static gate was green, and nothing ever
executed the SQL.

Pinned contracts
----------------
1. The packs the script can run (`--list`) are the registry packs that name a
   `queries:` file — no more, no fewer.
2. Each of those packs has a ci.yml step that invokes the script for it, and
   that step is the invocation and NOTHING else: its `run:` is one line of the
   form `bash scripts/ops/with_*.sh … bash scripts/ops/exporter_queries_runtime.sh
   <pack>`, the step has no key but `name` and `run`, the job has no `if:` /
   `continue-on-error` / `defaults`, and the job reports a name under
   `required:` in .github/required-checks.yaml. An allow-list on purpose: a
   list of forbidden shell spellings (`|| true`, `| tee`, `if …; then`, a
   path-gating `git diff … && exit 0`, `&`, a trap) is never complete.
3. The script creates the monitor user and gives it the grants the queries
   file's header documents, and does nothing else to the database: everything
   it executes in the database container is pinned, command by command. A
   header without a `#   GRANT …` line, or with one in another spelling, is
   "could not run" (exit 2), not "nothing needed".
4. The exporter runs THAT pack's queries file as the monitor user, and the
   script's verdict is the checker's: `--pack <pack> --compare-served`, exit
   code passed through.
5. Infrastructure failing — an image that cannot be pulled, a container that
   does not start, a database that never becomes ready, an exporter that is
   not listening — is exit 2 with a message naming it, never exit 1 (a finding).

3–5 run the real script with `docker` / `curl` / `sleep` and the checker's
python replaced by recorders: what is asserted is what the script hands them.

⚠️ What this does NOT cover: that the SQL runs on a real database — that is
the CI step itself. What the database grants a user without being asked (DB2's
PUBLIC can connect) is not seen either. The `with_*.sh` wrappers are trusted to
`exec` their arguments. And whether the job's name is ticked in branch
protection lives in GitHub's settings (required-checks-drift.yml compares).
"""
from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/ops/exporter_queries_runtime.sh"
CI = ROOT / ".github/workflows/ci.yml"
SSOT = ROOT / ".github/required-checks.yaml"
BASH = shutil.which("bash")

sys.path.insert(0, str(ROOT / "scripts/tools/lint"))
import check_exporter_queries as ceq  # noqa: E402


def _registry_packs() -> dict[str, Path]:
    return {pack: queries for pack, _rule_pack, queries in ceq.bindings()}


def _script_packs() -> list[str]:
    out = subprocess.run([BASH, str(SCRIPT), "--list"], capture_output=True,
                         text=True, encoding="utf-8", timeout=30, check=True)
    return out.stdout.split()


@pytest.mark.skipif(BASH is None, reason="bash is not on PATH")
def test_the_script_runs_exactly_the_registry_packs_with_a_queries_file() -> None:
    registry = _registry_packs()
    assert len(registry) >= 2, f"only {sorted(registry)} name a queries file — did the registry read change?"
    assert sorted(_script_packs()) == sorted(registry)


# ── 2. each pack has a CI step that is the invocation and nothing else ───

_SCRIPT_NAME = "exporter_queries_runtime.sh"
_INVOCATION = re.compile(
    r"(?:bash scripts/ops/with_[a-z_]+\.sh )*bash scripts/ops/exporter_queries_runtime\.sh ([a-z0-9_]+)")


def wiring_problems(ci: dict, required: set[str], packs: list[str]) -> list[str]:
    problems = []
    ran: dict[str, list[str]] = {p: [] for p in packs}
    if "defaults" in ci:
        problems.append("the workflow sets `defaults:` — a step's shell is no longer `bash -e`")
    for job_id, job in (ci.get("jobs") or {}).items():
        for step in job.get("steps") or []:
            run = step.get("run") or ""
            if _SCRIPT_NAME not in run:
                continue
            where = f"{job_id} / {step.get('name')!r}"
            match = _INVOCATION.fullmatch(run.strip())
            if not match:
                problems.append(f"{where}: `run:` mentions {_SCRIPT_NAME} but is not exactly one "
                                f"`bash scripts/ops/with_*.sh … bash scripts/ops/{_SCRIPT_NAME} <pack>` line")
                continue
            pack = match.group(1)
            if pack not in ran:
                problems.append(f"{where} runs the script for {pack!r}, which has no queries file")
                continue
            ran[pack].append(where)
            extra = sorted(set(step) - {"name", "run"})
            if extra:
                problems.append(f"{where} carries {extra} — only `name` and `run` are allowed")
            for key in ("if", "continue-on-error", "defaults"):
                if key in job:
                    problems.append(f"job {job_id} carries `{key}:`")
            if job.get("name") not in required:
                problems.append(f"job {job_id} ({job.get('name')!r}) is not under `required:` "
                                "in .github/required-checks.yaml")
    for pack, where in sorted(ran.items()):
        if not where:
            problems.append(f"no ci.yml step runs {_SCRIPT_NAME} {pack}")
    return problems


def _required_names() -> set[str]:
    return {e["name"] for e in yaml.safe_load(SSOT.read_text(encoding="utf-8"))["required"]}


def test_every_pack_with_a_queries_file_has_a_required_ci_step() -> None:
    ci = yaml.safe_load(CI.read_text(encoding="utf-8"))
    problems = wiring_problems(ci, _required_names(), sorted(_registry_packs()))
    assert not problems, "\n  ".join([""] + problems)


_CMD = "bash scripts/ops/exporter_queries_runtime.sh a"


def _ci(run: str = _CMD, step: dict | None = None, **job) -> dict:
    return {"jobs": {"j": {"name": "J", "steps": [{"name": "s", "run": run, **(step or {})}], **job}}}


@pytest.mark.parametrize("ci, packs, expected", [
    (_ci(), ["a"], 0),
    (_ci(f"bash scripts/ops/with_da_read.sh {_CMD}\n"), ["a"], 0),
    (_ci(), ["a", "b"], 1),
    (_ci("echo no check here"), ["a"], 1),
    (_ci("bash scripts/ops/exporter_queries_runtime.sh z"), ["a"], 2),
    (_ci(**{"if": "github.event_name == 'push'"}), ["a"], 1),
    (_ci(**{"continue-on-error": True}), ["a"], 1),
    (_ci(**{"defaults": {"run": {"shell": "bash +e {0}"}}}), ["a"], 1),
    (_ci(step={"if": "false"}), ["a"], 1),
    (_ci(step={"continue-on-error": True}), ["a"], 1),
    (_ci(step={"shell": "bash +e {0}"}), ["a"], 1),
    ({"jobs": {"j": {"name": "Other", "steps": [{"name": "s", "run": _CMD}]}}}, ["a"], 1),
    ({"defaults": {"run": {"shell": "sh"}}, **_ci()}, ["a"], 1),
] + [(_ci(run), ["a"], 2) for run in (
    f"# {_CMD}",
    f"{_CMD} || true",
    f"{_CMD} || exit 0",
    f"{_CMD} || echo ignored",
    f"{_CMD} &",
    f"{_CMD} | tee /tmp/log",
    f"{_CMD}; true",
    f"if ! {_CMD}\nthen echo ignored\nfi",
    f"git diff --quiet origin/main -- rule-packs/exporters && exit 0\n{_CMD}",
    f"exit\n{_CMD}",
    f"f() {{\n{_CMD}\n}}",
    f"cat <<EOF\n{_CMD}\nEOF",
    f"trap 'exit 0' EXIT\n{_CMD}",
    f"{_CMD}\necho done",
    f"pip install x\n{_CMD}",
    "bash scripts/ops/exporter_queries_runtime.sh",
    "bash scripts/ops/exporter_queries_runtime.sh a b",
    "bash scripts/ops/exporter_queries_runtime.sh $PACK",
    f"env X=1 {_CMD}",
)])
def test_the_wiring_rule_can_fail(ci, packs, expected) -> None:
    """Every shape the review of this module got past the first, deny-list
    version of the rule: each leaves the pack without a step (2 problems: the
    malformed step and the pack nobody runs)."""
    assert len(wiring_problems(ci, {"J"}, packs)) == expected


# ── 3–5. the script itself, with docker / curl / sleep / the checker recorded ──

_DOCKER_STUB = r"""#!/usr/bin/env bash
# Recorder: argv to $REC/argv; stdin of `exec -i`, files of `cp` and the
# exporter's databases.yaml to $REC. $FAIL names a subcommand (or, for `run`,
# `run-db` / `run-exporter`) that fails.
printf '%s\n' "$*" >> "$REC/argv"
sub=$1
if [[ "$sub" == run ]]; then
  [[ "$*" == *query-exporter* ]] && sub=run-exporter || sub=run-db
  for a in "$@"; do
    [[ "$a" == *databases.yaml:* ]] && cat "${a%%:*}" > "$REC/databases"
  done
fi
if [[ "${FAIL:-}" == "$sub" ]]; then echo "docker: simulated failure of $sub" >&2; exit 125; fi
# The exporter's log cannot be read (the daemon went away after the scrape).
if [[ "$1" == logs && -n "${FAIL_EXPORTER_LOGS:-}" && "$2" == qe-exp-* ]]; then exit 125; fi
case "$1" in
  logs)    if [[ -z "${NEVER_READY:-}" ]]; then
             echo "DATABASE IS READY TO USE / Setup has completed"
             # Keep writing after the marker, past a pipe buffer, and die of
             # SIGPIPE as docker does when the reader has gone: a reader that
             # stops at its match must not turn that into "not ready".
             set -o pipefail
             head -c 300000 /dev/zero | tr '\0' 'x' || exit 141
           fi ;;
  inspect) echo true ;;
  port)    echo "127.0.0.1:1" ;;
  cp)      cat "$2" >> "$REC/copied" ;;
  exec)    [[ "$2" == "-i" ]] && cat >> "$REC/stdin" ;;
esac
exit 0
"""

# Stands in for the python the script runs the checker with: `-c …` (counting
# the queries) goes to the real interpreter, the checker call is recorded.
_PYTHON_STUB = """#!/usr/bin/env bash
if [[ "$1" == "-c" ]]; then exec "$REAL_PYTHON" "$@"; fi
printf '%s\\n' "$*" >> "$REC/checker"
exit "${CHECKER_RC:-0}"
"""


@pytest.fixture
def stubbed(tmp_path):
    """Run the script with its collaborators stubbed; returns run(pack, **env)."""
    bin_dir, rec = tmp_path / "bin", tmp_path / "rec"
    bin_dir.mkdir()
    rec.mkdir()
    for name, body in (("docker", _DOCKER_STUB), ("pystub", _PYTHON_STUB),
                       ("curl", "#!/usr/bin/env bash\nexit 0\n"),
                       ("sleep", "#!/usr/bin/env bash\nexit 0\n")):
        path = bin_dir / name
        path.write_text(body, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)

    def run(pack: str, script: Path = SCRIPT, **extra):
        env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
               "REC": str(rec), "PYTHON": str(bin_dir / "pystub"),
               "REAL_PYTHON": sys.executable, **extra}
        proc = subprocess.run([BASH, str(script), pack], capture_output=True, text=True, encoding="utf-8",
                              timeout=120, env=env, cwd=tmp_path)

        def read(name: str) -> str:
            return (rec / name).read_text(encoding="utf-8") if (rec / name).exists() else ""
        return proc, read
    return run


def _header_grants(queries: Path) -> list[str]:
    return re.findall(r"(?m)^#   (GRANT [^;\n]*)$", queries.read_text(encoding="utf-8"))


def _normalised(text: str) -> str:
    """Recorded text with the run's own pid-suffixed names replaced."""
    return re.sub(r"qe-(db|exp|net)-\d+", r"qe-\1", text)


# Everything the script may execute in the database container, per pack: the
# header's grants (spliced in below) and this. A line more — a role, a group,
# an instance setting — is a privilege the header does not document.
_DB_SETUP = {
    "oracle": {
        "exec": ["exec -i qe-db sqlplus -s system/Ci_only_pw1@localhost/FREEPDB1"],
        "before": ["WHENEVER SQLERROR EXIT FAILURE",
                   'CREATE USER monitor IDENTIFIED BY "Ci_monitor_pw1";'],
        "grant": "{};",
        "after": ["exit"],
    },
    "db2": {
        "exec": ["exec qe-db bash -c useradd monitor && echo 'monitor:Ci_monitor_pw1' | chpasswd"
                 " && chmod 644 /tmp/ci_setup.sh",
                 "exec qe-db su - db2inst1 -c bash /tmp/ci_setup.sh"],
        "before": ["set -e", "db2 connect to CIDB"],
        "grant": 'db2 "{}"',
        "after": ['db2 "CREATE TABLESPACE CI_TS_MAX MANAGED BY AUTOMATIC STORAGE AUTORESIZE YES MAXSIZE 64 M"',
                  "db2 \"CREATE TABLESPACE CI_TS_FIX MANAGED BY DATABASE USING"
                  " (FILE '/database/data/ci_ts_fix.dat' 2000) AUTORESIZE NO\"",
                  'db2 "SELECT COUNT(*) FROM TABLE(MON_GET_DATABASE(-1))"',
                  'db2 "SELECT COUNT(*) FROM TABLE(MON_GET_TRANSACTION_LOG(-1))"',
                  'db2 "SELECT COUNT(*) FROM TABLE(MON_GET_TABLESPACE(NULL, -1))"'],
    },
}


@pytest.mark.skipif(BASH is None, reason="bash is not on PATH")
@pytest.mark.parametrize("pack", sorted(_registry_packs()))
def test_the_database_is_given_the_header_grants_and_nothing_else(stubbed, pack) -> None:
    queries = _registry_packs()[pack]
    documented = _header_grants(queries)
    assert documented, f"{pack}: the queries file's header documents no `#   GRANT …` line"
    assert pack in _DB_SETUP, f"{pack}: add what its setup may execute to _DB_SETUP"
    want = _DB_SETUP[pack]
    proc, read = stubbed(pack)
    assert proc.returncode == 0, proc.stderr
    argv = _normalised(read("argv")).splitlines()
    assert [line for line in argv if line.startswith("exec ")] == want["exec"]
    sent = [line for line in (read("stdin") + read("copied")).splitlines() if line.strip()]
    assert sent == want["before"] + [want["grant"].format(g) for g in documented] + want["after"]

    # The image is not asked to create the user: one it creates comes with
    # roles of the image's choosing (gvenzl/oracle-free: DB_DEVELOPER_ROLE).
    database = next(line for line in argv if line.startswith("run ") and "query-exporter" not in line)
    assert "APP_USER" not in database, database

    # The exporter runs THIS pack's queries file, as the monitor user, on both
    # database entries the file names (the liveness query has one of its own).
    exporter = next(line for line in argv if line.startswith("run ") and "query-exporter" in line)
    assert f"-v {queries}:/config/queries.yaml:ro" in exporter, exporter
    entries = yaml.safe_load(read("databases"))["databases"]
    assert sorted(entries) == [pack, f"{pack}_liveness"]
    for name, entry in entries.items():
        assert re.match(r"[a-z0-9+_]+://monitor:", entry["dsn"]), (name, entry["dsn"])

    # And the verdict is the checker's, asked about this pack.
    checker = read("checker").split()
    assert checker[1:4] == ["--pack", pack, "--compare-served"], checker
    assert checker[0].endswith("scripts/tools/lint/check_exporter_queries.py")


@pytest.mark.skipif(BASH is None, reason="bash is not on PATH")
@pytest.mark.parametrize("rc", [0, 1, 2])
def test_the_exit_code_is_the_checkers(stubbed, rc) -> None:
    proc, _read = stubbed(sorted(_registry_packs())[0], CHECKER_RC=str(rc))
    assert proc.returncode == rc, proc.stderr


@pytest.mark.skipif(BASH is None, reason="bash is not on PATH")
def test_an_unreadable_exporter_log_does_not_replace_the_checkers_exit_code(stubbed) -> None:
    """After a finding the script prints the exporter's log; failing to is not the verdict."""
    proc, _read = stubbed(sorted(_registry_packs())[0], CHECKER_RC="1", FAIL_EXPORTER_LOGS="1")
    assert proc.returncode == 1, (proc.returncode, proc.stderr)


def _copy_with_header(tmp_path: Path, pack: str, edit) -> Path:
    """The tree's script next to *pack*'s queries file with its header edited."""
    root = tmp_path / "repo"
    (root / "scripts/ops").mkdir(parents=True)
    (root / "rule-packs/exporters").mkdir(parents=True)
    (root / "requirements").mkdir()
    shutil.copy(SCRIPT, root / "scripts/ops" / SCRIPT.name)
    shutil.copy(ROOT / "requirements/ci-constraints.txt", root / "requirements")
    text = _registry_packs()[pack].read_text(encoding="utf-8")
    edited = edit(text)
    assert edited != text, "the header edit changed nothing"
    (root / "rule-packs/exporters" / f"{pack}.yaml").write_text(edited, encoding="utf-8")
    return root / "scripts/ops" / SCRIPT.name


@pytest.mark.skipif(BASH is None, reason="bash is not on PATH")
@pytest.mark.parametrize("edit", [
    lambda text: re.sub(r"(?m)^#   GRANT .*\n", "", text),
    lambda text: re.sub(r"(?m)^(#   GRANT .*)$", r"\1;", text, count=1),
    lambda text: re.sub(r"(?m)^#   (GRANT .*)$", r"#     \1", text, count=1),
], ids=["no-grant-lines", "trailing-semicolon", "other-indentation"])
def test_a_header_whose_grants_cannot_all_be_read_cannot_run(stubbed, tmp_path, edit) -> None:
    for pack in sorted(_registry_packs()):
        script = _copy_with_header(tmp_path / pack, pack, edit)
        proc, read = stubbed(pack, script=script)
        assert proc.returncode == 2, (pack, proc.returncode, proc.stderr)
        assert "GRANT" in proc.stderr
        assert read("argv") == "", "the database was started before the header was read"


@pytest.mark.skipif(BASH is None, reason="bash is not on PATH")
@pytest.mark.parametrize("env, message", [
    ({"FAIL": "pull"}, "image pull failed"),
    ({"FAIL": "network"}, "could not create the docker network"),
    ({"FAIL": "run-db"}, "could not start the"),
    ({"NEVER_READY": "1"}, "did not become ready"),
    ({"FAIL": "logs"}, "did not become ready"),
    ({"FAIL": "exec"}, "could not set up the"),
    ({"FAIL": "run-exporter"}, "could not start query-exporter"),
    ({"FAIL": "port"}, "query-exporter is not listening"),
], ids=["pull", "network", "db-container", "never-ready", "logs-unreadable", "setup", "exporter-container",
        "exporter-port"])
def test_failing_infrastructure_is_exit_2_by_name(stubbed, env, message) -> None:
    for pack in sorted(_registry_packs()):
        proc, read = stubbed(pack, **env)
        assert proc.returncode == 2, (pack, proc.returncode, proc.stderr)
        assert message in proc.stderr, proc.stderr
        assert read("checker") == "", "the checker ran although the infrastructure failed"


@pytest.mark.skipif(BASH is None, reason="bash is not on PATH")
def test_an_unknown_pack_is_a_usage_error() -> None:
    proc = subprocess.run([BASH, str(SCRIPT), "nope"], capture_output=True, text=True, encoding="utf-8",
                          timeout=30)
    assert proc.returncode == 2 and "usage" in proc.stderr
