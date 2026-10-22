"""The da-tools release runs its candidate image before promoting it (#3258).

Two halves, because either one rotting leaves a release that publishes an
image nobody executed:

  * **Where** — `release.yaml`'s da-tools job has exactly one step that runs
    `scripts/ops/smoke_da_tools_image.sh` on `${IMG}@${DIGEST}`, where DIGEST
    is the build step's own digest (the object Trivy scanned and promotion
    re-tags), placed after Trivy and before every publishing step, and
    nothing on the step can turn its failure into a pass (`if:`,
    `continue-on-error`, a command after the call). Parsed from the YAML, not
    grepped, and each refusal is controlled with a synthetic job.
  * **What** — the script is driven through a fake `docker` on PATH that
    reports each call and plays back one failure at a time. Every branch the
    script claims to catch must turn it non-zero, and the healthy run must
    reach every COMMAND_MAP key and mount the fixture at each of the three
    file-mode combinations it judges.

What this does NOT show: that a real image passes. That was measured when the
step was written (a locally built image, plus two broken derivatives: a
missing tool script and a missing PyYAML), and it is what the release job
itself measures on every `tools/v*` tag.
"""
from __future__ import annotations

import ast
import copy
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "release.yaml"
SCRIPT = ROOT / "scripts" / "ops" / "smoke_da_tools_image.sh"
ENTRYPOINT = ROOT / "components" / "da-tools" / "app" / "entrypoint.py"
FIXTURE = ROOT / "tests" / "fixtures" / "da-tools-smoke"
JOB = "release-da-tools"
SCRIPT_REL = "scripts/ops/smoke_da_tools_image.sh"
_CALL = re.compile(r'\s*bash scripts/ops/smoke_da_tools_image\.sh "\$\{IMG\}@\$\{DIGEST\}"\s*')

_PUBLISH = re.compile(
    r"\bimagetools\s+create\b|\bcosign\s+sign(-blob)?\b|\bsyft\s|"
    r"\bgh\s+release\s+(create|upload)\b|\bhelm\s+push\b")

BASH = shutil.which("bash")
# The script stages files at 0644 and 0600 and the fake reads those modes
# back; NTFS cannot hold that difference, and the uid check needs os.getuid().
needs_posix_bash = pytest.mark.skipif(
    BASH is None or os.name == "nt", reason="needs bash and POSIX file modes")


def _code(script: str) -> list[str]:
    return [ln for ln in script.splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")]


def _load() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _command_map() -> list[str]:
    tree = ast.parse(ENTRYPOINT.read_text(encoding="utf-8"))
    (node,) = [n for n in tree.body if isinstance(n, ast.Assign)
               and any(getattr(t, "id", None) == "COMMAND_MAP" for t in n.targets)]
    return list(ast.literal_eval(node.value))


# ── where ──────────────────────────────────────────────────────────────────

def check_smoke_gate(doc: dict) -> list[str]:
    """Every way `doc`'s da-tools job fails to gate promotion on the smoke."""
    job = (doc.get("jobs") or {}).get(JOB) or {}
    steps = job.get("steps") or []
    if job.get("continue-on-error"):
        return [f"{JOB} has continue-on-error, so no failing step stops the release"]
    smoke = [i for i, s in enumerate(steps)
             if any(SCRIPT_REL in ln for ln in _code(str(s.get("run") or "")))]
    if len(smoke) != 1:
        return [f"{JOB}: expected one step running {SCRIPT_REL}, found {len(smoke)}"]
    idx = smoke[0]
    step = steps[idx]
    problems = []

    # ⛔ What makes it a GATE rather than a step that runs: its failure has
    # to fail the job. An `if:` can skip it, `continue-on-error` reports a
    # failure as success, and a command after the call (or `|| true` on it)
    # decides the step's rc instead of the smoke.
    for key in ("if", "continue-on-error"):
        if key in step:
            problems.append(f"the smoke step carries `{key}: {step[key]!r}`, "
                            f"which lets a failing smoke stop gating promotion")
    lines = _code(str(step.get("run") or ""))
    if not lines or lines[0].strip() != "set -euo pipefail":
        problems.append("the smoke step's run: does not start with `set -euo pipefail`")
    if not lines or not _CALL.fullmatch(lines[-1]):
        problems.append(
            f"the smoke step's last command is {lines[-1] if lines else None!r}, "
            f'not exactly `bash {SCRIPT_REL} "${{IMG}}@${{DIGEST}}"` — anything '
            f"after it, or appended to it, decides the step's exit code instead")

    builds = [s for s in steps
              if str(s.get("uses") or "").startswith("docker/build-push-action")]
    if len(builds) != 1 or not builds[0].get("id"):
        problems.append(f"{JOB}: expected one build-push step with an id")
    else:
        env = step.get("env") or {}
        want = r"\$\{\{\s*steps\." + re.escape(builds[0]["id"]) + r"\.outputs\.digest\s*\}\}"
        if not re.fullmatch(want, str(env.get("DIGEST") or "")):
            problems.append(f"the smoke's DIGEST is {env.get('DIGEST')!r}, not the "
                            f"build step's digest — it could run something other "
                            f"than what is promoted")
        if not str(env.get("IMG") or "").endswith("/da-tools"):
            problems.append(f"the smoke's IMG is {env.get('IMG')!r}")

    trivy = [i for i, s in enumerate(steps)
             if str(s.get("uses") or "").startswith("aquasecurity/trivy-action")]
    if len(trivy) != 1:
        problems.append(f"{JOB}: expected one Trivy step, found {len(trivy)}")
    elif idx < trivy[0]:
        problems.append(f"the smoke (#{idx}) runs before Trivy (#{trivy[0]})")

    publishing = [i for i, s in enumerate(steps)
                  if _PUBLISH.search("\n".join(_code(str(s.get("run") or ""))))]
    if not publishing:
        problems.append("no publishing step found — the ordering check would be vacuous")
    early = [steps[i].get("name") for i in publishing if i < idx]
    if early:
        problems.append(f"published before the smoke ran: {early}")
    return problems


def test_the_release_gates_promotion_on_the_smoke():
    assert check_smoke_gate(_load()) == []


def _smoke_step(doc: dict) -> dict:
    (step,) = [s for s in doc["jobs"][JOB]["steps"] if SCRIPT_REL in str(s.get("run") or "")]
    return step


def _mutate_call(doc: dict, suffix: str) -> None:
    step = _smoke_step(doc)
    step["run"] = step["run"].rstrip("\n") + suffix + "\n"


def _move_after_signing(doc: dict) -> None:
    steps = doc["jobs"][JOB]["steps"]
    step = _smoke_step(doc)
    steps.remove(step)
    sign = next(i for i, s in enumerate(steps) if "cosign sign" in str(s.get("run") or ""))
    steps.insert(sign + 1, step)


@pytest.mark.parametrize("mutate, needle", [
    (lambda d: _mutate_call(d, " || true"), "last command"),
    (lambda d: _mutate_call(d, "\necho done"), "last command"),
    (lambda d: _smoke_step(d).__setitem__("continue-on-error", True), "continue-on-error"),
    (lambda d: _smoke_step(d).__setitem__("if", "false"), "`if:"),
    (lambda d: d["jobs"][JOB].__setitem__("continue-on-error", True), "no failing step"),
    (lambda d: _smoke_step(d)["env"].__setitem__("DIGEST", "${{ steps.other.outputs.digest }}"),
     "not the build step's digest"),
    (_move_after_signing, "published before the smoke ran"),
], ids=["or-true", "command-after", "step-continue-on-error", "step-if",
        "job-continue-on-error", "other-digest", "after-signing"])
def test_control_each_way_of_disarming_the_gate_is_refused(mutate, needle):
    doc = copy.deepcopy(_load())
    mutate(doc)
    problems = check_smoke_gate(doc)
    assert any(needle in p for p in problems), problems


def test_the_two_fixture_trees_differ_only_in_the_inherited_defaults():
    old = {p.name: p.read_bytes() for p in (FIXTURE / "old" / "conf.d").iterdir()}
    new = {p.name: p.read_bytes() for p in (FIXTURE / "new" / "conf.d").iterdir()}
    assert old.keys() == new.keys() and "_defaults.yaml" in old, (old.keys(), new.keys())
    differing = sorted(n for n in old if old[n] != new[n])
    assert differing == ["_defaults.yaml"], differing


# ── what: the script against a fake docker ────────────────────────────────

# Plays config-diff's exit-code contract (ADR-037 decision 4) by which MOUNT
# is unreadable: base side → 1 and every rendered view `not_compared`; PR
# side → 2 and `not_computed`. FAKE_<CASE>_RC / FAKE_<CASE>_OUT override one
# case's rc or stdout (CASE: OK, BASE, PR; OUT is JSON for --format json,
# text otherwise) so each test plays back exactly one failure.
_FAKE_DOCKER = textwrap.dedent('''\
    #!/usr/bin/env python3
    """Stand-in for `docker run`; behaviour from FAKE_* env, calls to FAKE_LOG."""
    import json, os, stat, sys
    a = sys.argv[1:]
    assert a[:2] == ["run", "--rm"], a
    a = a[2:]
    rec = {"platform": None, "entrypoint": None, "mounts": []}
    while a and a[0].startswith("-"):
        flag, val, a = a[0], a[1], a[2:]
        if flag == "--platform": rec["platform"] = val
        elif flag == "--entrypoint": rec["entrypoint"] = val
        elif flag == "-v": rec["mounts"].append(val)
        else: sys.exit(f"fake docker: unexpected flag {flag}")
    rec["image"], rec["args"] = a[0], a[1:]
    modes = {}
    for m in rec["mounts"]:
        host, target = m.split(":")[0], m.split(":")[1]
        modes[target] = sorted({stat.S_IMODE(os.stat(os.path.join(host, f)).st_mode)
                                for f in os.listdir(host)})
    rec["modes"] = modes
    with open(os.environ["FAKE_LOG"], "a") as fh:
        fh.write(json.dumps(rec) + "\\n")
    env = os.environ.get
    if rec["entrypoint"] == "id":
        print(env("FAKE_UID", "10001")); sys.exit(0)
    args = rec["args"]
    if args[-1:] == ["--help"]:
        cmd = args[0] if len(args) == 2 else "--help"
        if cmd == env("FAKE_HELP_FAIL"):
            print("Traceback: boom", file=sys.stderr); sys.exit(2)
        if cmd != env("FAKE_HELP_EMPTY"):
            print("usage: " + cmd)
        sys.exit(0)
    if args[:1] != ["config-diff"]:
        sys.exit(f"fake docker: unexpected call {args}")
    unreadable = lambda t: any(not (m & 0o004) for m in modes.get(t, []))
    case = "PR" if unreadable("/data/new") else "BASE" if unreadable("/data/old") else "OK"
    as_json = "--format" in args and args[args.index("--format") + 1] == "json"
    view = {"OK": "computed", "BASE": "not_compared", "PR": "not_computed"}[case]
    doc = {"schema": "da-tools.config-diff/v1",
           "views": {"served": {"status": view}, "routes": {"status": view},
                     "platform": {"status": "computed"}},
           "threshold_changes": [{"key": "container_memory"}] if case == "OK" else [],
           "platform_settings": [{"file": "_defaults.yaml",
                                  "change": "modified" if case == "OK" else "unreadable",
                                  "status": "rendered" if case == "OK" else "not_compared"}]}
    text = json.dumps(doc) if as_json else "# Config Diff Report\\n"
    out = env(f"FAKE_{case}_{'JSON' if as_json else 'MD'}", text)
    if out:
        print(out)
    sys.exit(int(env(f"FAKE_{case}_RC", {"OK": "1", "BASE": "1", "PR": "2"}[case])))
''')

IMAGE = "ghcr.io/example/da-tools@sha256:" + "0" * 64


def _run(tmp_path: Path, **fake: str) -> tuple[subprocess.CompletedProcess, list[dict]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    docker = bin_dir / "docker"
    docker.write_text(_FAKE_DOCKER.replace("python3", Path(sys.executable).as_posix(), 1),
                      encoding="utf-8")
    docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
    log = tmp_path / "calls.jsonl"
    log.write_text("", encoding="utf-8")
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
           "FAKE_LOG": str(log), **fake}
    proc = subprocess.run([BASH, str(SCRIPT), IMAGE], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env, timeout=300)
    calls = [json.loads(ln) for ln in log.read_text(encoding="utf-8").splitlines()]
    return proc, calls


def _is_diff(call: dict) -> bool:
    return call["args"][:1] == ["config-diff"] and "--help" not in call["args"]


def _doc(**override) -> str:
    base = {"schema": "da-tools.config-diff/v1",
            "views": {"served": {"status": "not_compared"},
                      "routes": {"status": "not_compared"}},
            "threshold_changes": [],
            "platform_settings": [{"file": "_defaults.yaml", "change": "unreadable",
                                   "status": "not_compared"}]}
    base.update(override)
    return json.dumps(base)


@needs_posix_bash
def test_a_healthy_image_passes_and_every_check_was_actually_run(tmp_path):
    proc, calls = _run(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert {c["platform"] for c in calls} == {"linux/amd64"}
    assert {c["image"] for c in calls} == {IMAGE}
    helped = {c["args"][0] for c in calls
              if c["args"][-1:] == ["--help"] and len(c["args"]) == 2}
    assert helped == set(_command_map()), sorted(set(_command_map()) ^ helped)
    assert any(c["args"] == ["--help"] for c in calls), "top-level --help not run"
    diffs = [c for c in calls if _is_diff(c)]
    seen = [(c["modes"]["/data/old"], c["modes"]["/data/new"],
             "json" if "--format" in c["args"] else "md") for c in diffs]
    assert seen == [([0o644], [0o644], "md"), ([0o644], [0o644], "json"),
                    ([0o600], [0o644], "json"), ([0o644], [0o600], "json")], seen
    assert all(m.endswith(":ro") for c in diffs for m in c["mounts"]), diffs


@needs_posix_bash
@pytest.mark.parametrize("fake, needle", [
    ({"FAKE_HELP_FAIL": "config-diff"}, "da-tools config-diff --help exited 2"),
    ({"FAKE_HELP_FAIL": "--help"}, "da-tools --help exited 2"),
    ({"FAKE_HELP_EMPTY": "explain-keys"}, "explain-keys --help exited 0 but printed nothing"),
    # both sides readable
    ({"FAKE_OK_RC": "0"}, "readable trees exited 0, expected 1"),
    ({"FAKE_OK_MD": ""}, "exited 1 but wrote no report"),
    ({"FAKE_OK_JSON": _doc(views={"served": {"status": "computed"}})},
     "does not carry container_memory"),
    ({"FAKE_OK_JSON": "not json"}, "stdout is not JSON"),
    ({"FAKE_OK_JSON": _doc(schema="da-tools.config-diff/v2")}, "schema is"),
    # base side unreadable — the shape the retired config-diff.yaml burned on
    ({"FAKE_BASE_RC": "0"}, "base side unreadable to uid 10001 exited 0, expected 1"),
    ({"FAKE_BASE_JSON": ""}, "stdout is not JSON"),
    ({"FAKE_BASE_JSON": _doc(threshold_changes=[{"key": "container_memory"}])},
     "from a base it could not read"),
    ({"FAKE_BASE_JSON": _doc(views={"served": {"status": "computed"}})},
     "expected every one 'not_compared'"),
    ({"FAKE_BASE_JSON": _doc(platform_settings=[])}, "expected _defaults.yaml unreadable"),
    # PR side unreadable
    ({"FAKE_PR_RC": "1"}, "PR side unreadable to uid 10001 exited 1, expected 2"),
    ({"FAKE_PR_RC": "0"}, "PR side unreadable to uid 10001 exited 0, expected 2"),
    ({"FAKE_PR_JSON": _doc()}, "expected every one 'not_computed'"),
], ids=["subcommand-help-fails", "top-help-fails", "help-prints-nothing",
        "ok-says-no-change", "ok-empty-markdown", "ok-misses-change", "ok-not-json",
        "ok-unknown-schema",
        "base-says-no-change", "base-empty-report", "base-reports-a-change",
        "base-view-computed", "base-row-missing",
        "pr-reads-as-change", "pr-reads-as-no-change", "pr-view-not-compared"])
def test_each_failure_the_script_claims_to_catch_fails_it(tmp_path, fake, needle):
    proc, _ = _run(tmp_path, **fake)
    assert proc.returncode == 1, (proc.returncode, proc.stderr)
    assert needle in proc.stderr, proc.stderr


@needs_posix_bash
def test_an_image_running_as_the_file_owner_is_refused_not_passed(tmp_path):
    proc, calls = _run(tmp_path, FAKE_UID=str(os.getuid()))
    assert proc.returncode == 2, (proc.returncode, proc.stderr)
    assert "mount check would measure nothing" in proc.stderr
    assert not [c for c in calls if _is_diff(c)]


@needs_posix_bash
def test_a_usage_error_is_rc_2_not_a_failed_check(tmp_path):
    proc = subprocess.run([BASH, str(SCRIPT)], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=60)
    assert proc.returncode == 2 and "usage:" in proc.stderr, (proc.returncode, proc.stderr)
