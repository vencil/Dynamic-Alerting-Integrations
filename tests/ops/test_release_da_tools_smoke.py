"""The da-tools release runs its candidate image before promoting it (#3258).

Two halves, because either one rotting leaves a release that publishes an
image nobody executed:

  * **Where** — `release.yaml`'s da-tools job has exactly one step that runs
    `scripts/ops/smoke_da_tools_image.sh` on `${IMG}@${DIGEST}`, where DIGEST
    is the build step's own digest (the object Trivy scanned and promotion
    re-tags), placed after Trivy and before every publishing step. Parsed
    from the YAML, not grepped.
  * **What** — the script is driven through a fake `docker` on PATH that
    reports each call and plays back one failure at a time. Every branch the
    script claims to catch must turn it non-zero, and the healthy run must
    reach every COMMAND_MAP key and mount the fixture at both file modes.

What this does NOT show: that a real image passes. That was measured when the
step was written (a locally built image, plus two broken derivatives: a
missing tool script and a missing PyYAML), and it is what the release job
itself measures on every `tools/v*` tag.
"""
from __future__ import annotations

import ast
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

_PUBLISH = re.compile(
    r"\bimagetools\s+create\b|\bcosign\s+sign(-blob)?\b|\bsyft\s|"
    r"\bgh\s+release\s+(create|upload)\b|\bhelm\s+push\b")

BASH = shutil.which("bash")
needs_bash = pytest.mark.skipif(BASH is None, reason="needs bash")


def _code(script: str) -> str:
    return "\n".join(ln for ln in script.splitlines()
                     if not ln.lstrip().startswith("#"))


def _steps() -> list[dict]:
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return doc["jobs"][JOB]["steps"]


def _command_map() -> list[str]:
    tree = ast.parse(ENTRYPOINT.read_text(encoding="utf-8"))
    (node,) = [n for n in tree.body if isinstance(n, ast.Assign)
               and any(getattr(t, "id", None) == "COMMAND_MAP" for t in n.targets)]
    return list(ast.literal_eval(node.value))


# ── where ──────────────────────────────────────────────────────────────────

def test_the_smoke_runs_on_the_scanned_digest_between_trivy_and_publishing():
    steps = _steps()
    smoke = [i for i, s in enumerate(steps) if SCRIPT_REL in _code(str(s.get("run") or ""))]
    assert len(smoke) == 1, f"{JOB}: expected one step running {SCRIPT_REL}, found {len(smoke)}"
    idx = smoke[0]
    step = steps[idx]

    builds = [s for s in steps
              if str(s.get("uses") or "").startswith("docker/build-push-action")]
    assert len(builds) == 1 and builds[0].get("id"), builds
    build_id = builds[0]["id"]
    env = step.get("env") or {}
    assert re.fullmatch(
        r"\$\{\{\s*steps\." + re.escape(build_id) + r"\.outputs\.digest\s*\}\}",
        str(env.get("DIGEST") or "")), (
        f"the smoke's DIGEST is {env.get('DIGEST')!r}, not the build step's "
        f"digest — it could be running something other than what is promoted")
    assert str(env.get("IMG") or "").endswith("/da-tools"), env
    assert re.search(r'smoke_da_tools_image\.sh\s+"\$\{IMG\}@\$\{DIGEST\}"',
                     _code(step["run"])), step["run"]

    trivy = [i for i, s in enumerate(steps)
             if str(s.get("uses") or "").startswith("aquasecurity/trivy-action")]
    assert len(trivy) == 1, trivy
    assert idx > trivy[0], f"the smoke (#{idx}) runs before Trivy (#{trivy[0]})"

    publishing = [i for i, s in enumerate(steps)
                  if _PUBLISH.search(_code(str(s.get("run") or "")))]
    assert publishing, "no publishing step found — this ordering check would be vacuous"
    early = [steps[i].get("name") for i in publishing if i < idx]
    assert not early, f"published before the smoke ran: {early}"


def test_the_two_fixture_trees_differ_only_in_the_inherited_defaults():
    old = {p.name: p.read_bytes() for p in (FIXTURE / "old").iterdir()}
    new = {p.name: p.read_bytes() for p in (FIXTURE / "new").iterdir()}
    assert old.keys() == new.keys() and "_defaults.yaml" in old, (old.keys(), new.keys())
    differing = sorted(n for n in old if old[n] != new[n])
    assert differing == ["_defaults.yaml"], differing


# ── what: the script against a fake docker ────────────────────────────────

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
    modes = []
    for m in rec["mounts"]:
        host = m.split(":")[0]
        for f in sorted(os.listdir(host)):
            modes.append(stat.S_IMODE(os.stat(os.path.join(host, f)).st_mode))
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
    if args[:1] == ["config-diff"]:
        unreadable = any(not (m & 0o004) for m in modes)
        if unreadable:
            sys.exit(int(env("FAKE_UNREADABLE_RC", "2")))
        report = env("FAKE_REPORT",
                     "- `_defaults.yaml` `defaults` (modified)\\n| container_memory | 85 | 90 |")
        if report:
            print(report)
        sys.exit(int(env("FAKE_DIFF_RC", "1")))
    sys.exit(f"fake docker: unexpected call {args}")
''')

def _is_diff(call: dict) -> bool:
    return call["args"][:1] == ["config-diff"] and "--help" not in call["args"]


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


@needs_bash
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
    assert [set(c["modes"]) for c in diffs] == [{0o644}, {0o600}], diffs
    assert all(m.endswith(":ro") for c in diffs for m in c["mounts"]), diffs


@needs_bash
@pytest.mark.parametrize("fake, needle", [
    ({"FAKE_HELP_FAIL": "config-diff"}, "da-tools config-diff --help exited 2"),
    ({"FAKE_HELP_FAIL": "--help"}, "da-tools --help exited 2"),
    ({"FAKE_HELP_EMPTY": "explain-keys"}, "explain-keys --help exited 0 but printed nothing"),
    ({"FAKE_DIFF_RC": "0"}, "0644 files exited 0, expected 1"),
    ({"FAKE_REPORT": ""}, "exited 1 but wrote no report"),
    ({"FAKE_REPORT": "something else"}, "names neither _defaults.yaml nor container_memory"),
    ({"FAKE_UNREADABLE_RC": "1"}, "0600 files (unreadable to uid 10001) exited 1"),
    ({"FAKE_UNREADABLE_RC": "0"}, "0600 files (unreadable to uid 10001) exited 0"),
], ids=["subcommand-help-fails", "top-help-fails", "help-prints-nothing",
        "diff-says-no-change", "diff-empty-report", "diff-report-misses-change",
        "unreadable-reads-as-change", "unreadable-reads-as-no-change"])
def test_each_failure_the_script_claims_to_catch_fails_it(tmp_path, fake, needle):
    proc, _ = _run(tmp_path, **fake)
    assert proc.returncode == 1, (proc.returncode, proc.stderr)
    assert needle in proc.stderr, proc.stderr


@needs_bash
def test_an_image_running_as_the_file_owner_is_refused_not_passed(tmp_path):
    proc, calls = _run(tmp_path, FAKE_UID=str(os.getuid()))
    assert proc.returncode == 2, (proc.returncode, proc.stderr)
    assert "mount check would measure nothing" in proc.stderr
    assert not [c for c in calls if _is_diff(c)]
