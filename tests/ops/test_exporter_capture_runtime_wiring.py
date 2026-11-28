"""Every pack with `exporters:` is captured from its real exporter by CI (#3833).

`check_exporter_capture.py` decides whether a pack's raw selectors are served
by a set of /metrics scrapes; the scrapes exist only when
`scripts/ops/exporter_capture_runtime.sh <pack>` starts the pinned server and
exporter. That is worth nothing for a pack no CI step runs it for.

Pinned contracts
----------------
1. The packs the script can run (`--list`) are the registry packs with
   `exporters:`, and each has a driver (scripts/ops/exporter_capture/<pack>.sh).
2. Each of those packs has a ci.yml step that is one call of the script and
   NOTHING else, in a job that reports a name under `required:` in
   .github/required-checks.yaml — the rule (and its can-fail table) of
   tests/ops/test_exporter_queries_runtime_wiring.py, asked of this script.
3. What the script starts is the registry entry's: the exporter image and the
   server image by `<image>:<tag>@<digest>`, the exporter with exactly the
   entry's flags, one capture per declared state — and its verdict is the
   checker's, exit code passed through.
4. Infrastructure failing — no docker, an image that cannot be pulled, a
   server that never becomes ready, an exporter that never reports the server
   up — is exit 2 with a message naming it, never exit 1 (a finding).
5. The static half (no `--pack`) is a pre-commit hook that runs on every
   commit (`always_run`: pre-commit matches `files:` against added and
   changed files only, so a deleted driver would not trigger it): without it
   a pack added with neither `exporters:` nor a ledger row is caught only by
   pytest.

3–4 run the real script and the real driver with `docker` / `curl` / `sleep`
and the checker's python replaced by recorders.

⚠️ What this does NOT cover: that the driver's docker commands bring up the
server — that is the CI step itself (nothing here runs a container). And
whether the job's name is ticked in branch protection lives in GitHub's
settings (required-checks-drift.yml compares).
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/ops/exporter_capture_runtime.sh"
CI = ROOT / ".github/workflows/ci.yml"
SSOT = ROOT / ".github/required-checks.yaml"
BASH = shutil.which("bash")
_SCRIPT_NAME = SCRIPT.name

sys.path.insert(0, str(ROOT / "scripts/tools/lint"))
sys.path.insert(0, str(ROOT / "scripts/tools/ops"))
sys.path.insert(0, str(ROOT / "tests"))
import _registry_lib as registry_lib  # noqa: E402
import check_exporter_capture as cec  # noqa: E402
from _platform_fs import symlink_or_skip  # noqa: E402

# The one-call rule lives beside the script it was written for.
_spec = importlib.util.spec_from_file_location(
    "_queries_wiring", Path(__file__).with_name("test_exporter_queries_runtime_wiring.py"))
_queries_wiring = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_queries_wiring)

DOC = registry_lib.committed_registry_doc()
PACKS = sorted(registry_lib.pack_exporters(DOC))


def wiring_problems(ci: dict, required: set[str], packs: list[str]) -> list[str]:
    return _queries_wiring.wiring_problems(ci, required, packs, script=_SCRIPT_NAME,
                                           lacks="has no `exporters:` entry")


# ── 1. the script's packs are the registry's, each with a driver ────────────

@pytest.mark.skipif(BASH is None, reason="bash is not on PATH")
def test_the_script_runs_exactly_the_registry_packs_with_exporters() -> None:
    assert PACKS, "no registry pack has `exporters:` — did the registry read change?"
    out = subprocess.run([BASH, str(SCRIPT), "--list"], capture_output=True, text=True,
                         encoding="utf-8", timeout=60, check=True)
    assert sorted(out.stdout.split()) == PACKS
    assert not cec.driver_problems(DOC)


# ── 2. each pack has a CI step that is the invocation and nothing else ──────

def test_every_pack_with_exporters_has_a_required_ci_step() -> None:
    ci = yaml.safe_load(CI.read_text(encoding="utf-8"))
    required = {e["name"] for e in yaml.safe_load(SSOT.read_text(encoding="utf-8"))["required"]}
    problems = wiring_problems(ci, required, PACKS)
    assert not problems, "\n  ".join([""] + problems)


_CMD = f"bash scripts/ops/{_SCRIPT_NAME} a"


def _ci(run: str = _CMD, **job) -> dict:
    return {"jobs": {"j": {"name": "J", "steps": [{"name": "s", "run": run}], **job}}}


@pytest.mark.parametrize("ci, packs, expected", [
    (_ci(), ["a"], 0),
    (_ci(f"bash scripts/ops/with_da_read.sh {_CMD}\n"), ["a"], 0),
    (_ci(), ["a", "b"], 1),
    (_ci("bash scripts/ops/exporter_queries_runtime.sh a"), ["a"], 1),
    (_ci(f"bash scripts/ops/{_SCRIPT_NAME} z"), ["a"], 2),
    (_ci(**{"if": "github.event_name == 'push'"}), ["a"], 1),
    (_ci(**{"continue-on-error": True}), ["a"], 1),
    ({"jobs": {"j": {"name": "Other", "steps": [{"name": "s", "run": _CMD}]}}}, ["a"], 1),
    (_ci(f"{_CMD}\necho done"), ["a"], 2),
    (_ci(f"pip install x\n{_CMD}"), ["a"], 2),
    (_ci(f"{_CMD} || true"), ["a"], 2),
])
def test_the_wiring_rule_can_fail_for_this_script(ci, packs, expected) -> None:
    """The rule is the queries script's (its own table is longer); these show
    it is asked about THIS script's name: another script's step does not count,
    and a second line or a `||` beside the call leaves the pack without a step."""
    assert len(wiring_problems(ci, {"J"}, packs)) == expected


# ── 3–4. the script and the driver, with docker / curl / sleep / the checker recorded ──

_DOCKER_STUB = r"""#!/usr/bin/env bash
# Recorder: argv to $REC/argv. $FAIL names a subcommand that fails; for `run`
# and `exec` it may also be `run:<substring>` / `exec:<substring>` of the argv.
printf '%s\n' "$*" >> "$REC/argv"
fail=${FAIL:-}
if [[ "$fail" == "$1" || ( "$fail" == "$1":* && "$*" == *"${fail#*:}"* ) ]]; then
  echo "docker: simulated failure of $1" >&2
  exit 125
fi
case "$1" in
  port) echo "127.0.0.1:1" ;;
  exec) [[ "$2" == "-i" ]] && cat > /dev/null ;;
esac
exit 0
"""

# The checker's python: --list / --plan go to the real interpreter (they read
# the registry), the `--pack … --capture …` call is recorded.
_PYTHON_STUB = """#!/usr/bin/env bash
if [[ " $* " != *" --pack "* ]]; then exec "$REAL_PYTHON" "$@"; fi
printf '%s\\n' "$*" >> "$REC/checker"
exit "${CHECKER_RC:-0}"
"""

# A scrape in which the exporter reports the server up, unless $SCRAPE says otherwise.
_CURL_STUB = """#!/usr/bin/env bash
printf '%s\\n' "${SCRAPE-pg_up 1}"
"""


@pytest.fixture
def stubbed(tmp_path):
    """Run the script with its collaborators stubbed; returns run(pack, **env)."""
    bin_dir, rec = tmp_path / "bin", tmp_path / "rec"
    bin_dir.mkdir()
    rec.mkdir()
    for name, body in (("docker", _DOCKER_STUB), ("pystub", _PYTHON_STUB), ("curl", _CURL_STUB),
                       ("sleep", "#!/usr/bin/env bash\nexit 0\n")):
        path = bin_dir / name
        path.write_text(body, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)

    def run(pack: str, path: "str | None" = None, **extra):
        env = {**os.environ, "PATH": path or f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
               "REC": str(rec), "PYTHON": str(bin_dir / "pystub"),
               "REAL_PYTHON": sys.executable, **extra}
        proc = subprocess.run([BASH, str(SCRIPT), pack], capture_output=True, text=True,
                              encoding="utf-8", timeout=180, env=env, cwd=tmp_path)

        def read(name: str) -> str:
            return (rec / name).read_text(encoding="utf-8") if (rec / name).exists() else ""
        return proc, read
    run.bin_dir = bin_dir
    return run


@pytest.mark.skipif(BASH is None, reason="bash is not on PATH")
@pytest.mark.parametrize("pack", PACKS)
def test_the_script_starts_what_the_registry_entry_names(stubbed, pack) -> None:
    entries = registry_lib.pack_exporters(DOC)[pack]
    proc, read = stubbed(pack)
    assert proc.returncode == 0, proc.stderr
    argv = read("argv").splitlines()
    pulled = [line.split()[-1] for line in argv if line.startswith("pull ")]
    runs = [line for line in argv if line.startswith("run ")]
    captures = []
    for i, e in enumerate(entries):
        exporter, server = registry_lib.image_ref(e), registry_lib.image_ref(e["server"])
        assert "@sha256:" in exporter and "@sha256:" in server
        assert exporter in pulled and server in pulled
        assert any(f" {server}" in line for line in runs), f"no container of {server}"
        started = [line for line in runs if f" {exporter}" in line]
        assert len(started) == len(e["states"]), started
        for line in started:
            # the exporter's arguments are the entry's flags, all of them, nothing else
            assert line.split(f" {exporter}", 1)[1].split() == e["flags"], line
        captures += [f"{i}:{state}" for state in e["states"]]
    checker = read("checker").split()
    assert checker[0].endswith("scripts/tools/lint/check_exporter_capture.py")
    assert checker[1:3] == ["--pack", pack]
    assert checker[3::2] == ["--capture"] * len(captures)
    assert [c.split("=")[0] for c in checker[4::2]] == captures


@pytest.mark.skipif(BASH is None, reason="bash is not on PATH")
@pytest.mark.parametrize("rc", [0, 1, 2])
def test_the_exit_code_is_the_checkers(stubbed, rc) -> None:
    proc, _read = stubbed(PACKS[0], CHECKER_RC=str(rc))
    assert proc.returncode == rc, proc.stderr


@pytest.mark.skipif(BASH is None, reason="bash is not on PATH")
@pytest.mark.parametrize("env, message", [
    ({"FAIL": "info"}, "the docker daemon is not reachable"),
    ({"FAIL": "pull"}, "image pull failed"),
    ({"FAIL": "network"}, "could not create the docker network"),
    ({"FAIL": "run"}, "could not start the container"),
    ({"FAIL": "exec:pg_isready"}, "did not become ready"),
    ({"FAIL": "exec"}, "did not become ready"),
    ({"FAIL": "run:-exp-"}, "could not start the container"),
    ({"FAIL": "port"}, "is not listening"),
    ({"SCRAPE": "pg_up 0"}, "never reported the server up"),
    ({"SCRAPE": ""}, "never reported the server up"),
], ids=["no-daemon", "pull", "network", "server-container", "server-never-ready", "exec-unavailable",
        "exporter-container", "exporter-port", "exporter-reports-down", "empty-scrape"])
def test_failing_infrastructure_is_exit_2_by_name(stubbed, env, message) -> None:
    for pack in PACKS:
        proc, read = stubbed(pack, **env)
        assert proc.returncode == 2, (pack, proc.returncode, proc.stderr)
        assert message in proc.stderr, proc.stderr
        assert read("checker") == "", "the checker ran although the infrastructure failed"


@pytest.mark.skipif(BASH is None, reason="bash is not on PATH")
def test_without_docker_the_script_cannot_run(stubbed) -> None:
    """No docker on PATH is exit 2 by name — never a pass, never a finding."""
    bare = stubbed.bin_dir.parent / "bare"
    bare.mkdir()
    for tool in ("bash", "dirname", "seq", "mktemp", "rm", "tr", "env"):
        found = shutil.which(tool)
        if found:
            symlink_or_skip(found, bare / tool)
    proc, read = stubbed(PACKS[0], path=str(bare))
    assert proc.returncode == 2, (proc.returncode, proc.stderr)
    assert "docker is not installed" in proc.stderr
    assert read("checker") == ""


@pytest.mark.skipif(BASH is None, reason="bash is not on PATH")
def test_an_unknown_pack_is_a_usage_error() -> None:
    proc = subprocess.run([BASH, str(SCRIPT), "nope"], capture_output=True, text=True, encoding="utf-8",
                          timeout=60)
    assert proc.returncode == 2 and "usage" in proc.stderr


def test_the_static_half_is_a_pre_commit_hook() -> None:
    config = yaml.safe_load((ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    hooks = [h for repo in config["repos"] for h in repo.get("hooks", [])
             if "scripts/tools/lint/check_exporter_capture.py" in str(h.get("entry", ""))]
    assert len(hooks) == 1, hooks
    hook = hooks[0]
    assert "--pack" not in hook["entry"] and "--ci" in hook["entry"].split()
    assert hook.get("pass_filenames") is False and "stages" not in hook
    assert hook.get("always_run") is True and "files" not in hook
