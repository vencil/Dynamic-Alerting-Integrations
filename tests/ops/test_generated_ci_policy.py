#!/usr/bin/env python3
"""The deployment policy (`--policy`) in the CI `da-tools init` ships (#2900).

The policy file (`allowed_domains`) is the SSRF control and lives outside the
tenant repository (ADR-036 §5), so the generated CI can bring it in only from
a CI variable, `DA_TOOLS_POLICY`. Unset is announced, not failed (owner ruling
on #2900). What is held here:

* GitHub: the value reaches `run:` through the step's `env:`, never as an
  Actions expression inside the script (that pastes it into bash as syntax);
  it is the policy CONTENT, written to `$RUNNER_TEMP` as 0644 and mounted
  read-only; unset prints a `::notice::` and passes no `--policy`.
* GitLab: `DA_TOOLS_POLICY` is a File-type variable (its value is a path);
  a non-empty value that is not a file fails the job with a message naming
  the type; unset prints a NOTICE.
* The custom-rule `lint` gets no `--policy` on either platform, and neither
  does the pre-commit snippet (its hooks cannot mount a file outside the repo).

⛔ The behavioural half EXECUTES the generated shell — bash for GitHub, POSIX
`sh` for GitLab (the da-tools image is Alpine, so its shell is busybox ash;
`dash` stands in when no busybox is on PATH) — with `docker` / `da-tools`
replaced by a stub that records its argv. A reading of the text alone cannot
tell "the flag is in the script" from "the flag reaches the tool".
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

_TESTS_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = _TESTS_DIR.parent
# Literal path string on purpose: verify_diff's text_map registers this guard
# against the generator by scanning for it.
_INIT_PROJECT = _REPO_ROOT / "scripts/tools/ops/init_project.py"
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "tools" / "ops"))

import init_project as ip  # noqa: E402

_GH_WORKFLOW = Path(".github") / "workflows" / "dynamic-alerting.yaml"
_GL_PIPELINE = Path(".gitlab-ci.d") / "dynamic-alerting.yml"
_PRECOMMIT = Path(".pre-commit-config.da.yaml")

_VAR = "DA_TOOLS_POLICY"
_VARS_EXPR = "${{ vars.DA_TOOLS_POLICY }}"
# (job, step name) of the two GitHub steps that must carry the policy.
_GH_POLICY_STEPS = [
    ("validate", "Validate config (schema + routing + policy)"),
    ("generate", "Generate Alertmanager routes"),
]
_GL_POLICY_JOBS = ["validate-config", "blast-radius"]
_POLICY_YAML = "allowed_domains:\n  - hooks.example.com\n"

_BASH = shutil.which("bash")
# GitLab leg: the image's own shell first, then a POSIX stand-in.
_POSIX_SHELLS = [
    [p, "sh"] if name == "busybox" else [p]
    for name in ("busybox", "dash")
    if (p := shutil.which(name))
]


# The policy lines do not depend on `--deploy`; both are rendered anyway so a
# deploy-specific template branch cannot drop them unseen. An unknown value
# makes `run_init` raise, which is loud.
@pytest.fixture(scope="module", params=["kustomize", "helm"])
def generated(request, tmp_path_factory) -> Path:
    target = tmp_path_factory.mktemp(f"policy-{request.param}")
    ip.run_init(
        {
            "ci": "both",
            "deploy": request.param,
            "rule_packs": ["mariadb"],
            "tenants": ["t-policy"],
            "namespace": "monitoring",
            "da_tools_image": ip.DA_TOOLS_IMAGE,
        },
        str(target),
    )
    return target


def _workflow(root: Path) -> dict:
    return yaml.safe_load((root / _GH_WORKFLOW).read_text(encoding="utf-8"))


def _gh_step(wf: dict, job: str, name: str) -> dict:
    for step in wf["jobs"][job]["steps"]:
        if step.get("name") == name:
            return step
    raise AssertionError(
        f"step {name!r} is not in job {job!r}: "
        f"{[s.get('name') for s in wf['jobs'][job]['steps']]}")


def _gl_pipeline(root: Path) -> dict:
    return yaml.safe_load((root / _GL_PIPELINE).read_text(encoding="utf-8"))


def _gl_policy_block(job: dict, job_name: str) -> str:
    blocks = [s for s in job["script"] if _VAR in s]
    assert len(blocks) == 1, (
        f"GitLab job {job_name!r} should carry exactly one script item that "
        f"reads {_VAR}; found {len(blocks)}")
    return blocks[0]


# ── text: what the artifact says ──────────────────────────────────────────


def test_no_vars_expression_inside_any_run(generated) -> None:
    """`${{ vars.* }}` inside `run:` is script injection; `env:` is data."""
    wf = _workflow(generated)
    offenders = [
        (job_name, step.get("name"))
        for job_name, job in wf["jobs"].items()
        for step in job.get("steps", [])
        if re.search(r"\$\{\{\s*vars\.", step.get("run", ""))
    ]
    assert not offenders, (
        f"`${{{{ vars... }}}}` is expanded INTO the script text in {offenders}; "
        "bind it through the step's `env:` instead")


@pytest.mark.parametrize(("job", "name"), _GH_POLICY_STEPS)
def test_github_policy_steps_bind_the_variable_through_env(
        generated, job, name) -> None:
    step = _gh_step(_workflow(generated), job, name)
    assert (step.get("env") or {}).get(_VAR) == _VARS_EXPR, (
        f"{name!r} does not bind {_VAR} from `vars` in its `env:`; "
        f"env is {step.get('env')!r}")
    assert '"${policy_args[@]}"' in step["run"]
    assert '"${policy_mount[@]}"' in step["run"]


def test_the_lint_step_and_job_get_no_policy(generated) -> None:
    """`lint --policy` is the custom-rule GOVERNANCE policy: a different file."""
    wf = _workflow(generated)
    lint = _gh_step(wf, "validate", "Lint custom rules (if any)")
    assert "lint /data/rules" in lint["run"], "lint step changed shape"
    assert "--policy" not in lint["run"] and _VAR not in lint["run"]
    assert _VAR not in (lint.get("env") or {})

    gl_lint = _gl_pipeline(generated)["lint-custom-rules"]
    script = "\n".join(gl_lint["script"])
    assert "da-tools lint" in script, "lint job changed shape"
    assert "--policy" not in script and _VAR not in script


def test_precommit_hooks_pass_no_policy(generated) -> None:
    doc = yaml.safe_load((generated / _PRECOMMIT).read_text(encoding="utf-8"))
    entries = [h["entry"] for r in doc["repos"] for h in r["hooks"]]
    assert entries, "the pre-commit snippet carries no hooks"
    assert not [e for e in entries if "--policy" in e]
    assert _VAR in (generated / _PRECOMMIT).read_text(encoding="utf-8"), (
        "the snippet should say why webhook domains are not checked here and "
        f"that CI ({_VAR}) is the gate")


# ── behaviour: run the generated shell ────────────────────────────────────


def _argv_stub(bin_dir: Path, name: str, log: Path) -> None:
    """A `docker` / `da-tools` that records one argument per line."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / name
    stub.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$@\" > '{log}'\n",
        encoding="utf-8", newline="\n")
    stub.chmod(0o755)


_EXPR = re.compile(r"\$\{\{\s*([^}]+?)\s*\}\}")


def _render_run(body: str, wf_env: dict) -> str:
    def repl(m: "re.Match[str]") -> str:
        expr = m.group(1)
        if expr == "github.workspace":
            return "."
        if expr.startswith("env.") and expr[4:] in wf_env:
            return str(wf_env[expr[4:]])
        raise AssertionError(
            f"`run:` contains ${{{{ {expr} }}}}, which this harness has no "
            "stand-in for")
    return _EXPR.sub(repl, body)


def _run_github_step(root: Path, tmp: Path, job: str, name: str,
                     value: str) -> tuple[subprocess.CompletedProcess, Path, Path]:
    wf = _workflow(root)
    step = _gh_step(wf, job, name)
    env_value = (step.get("env") or {}).get(_VAR)
    # The binding is read FROM the step: with no `env:` entry the value never
    # reaches the script, which is exactly what this must be able to see.
    assert env_value == _VARS_EXPR, (
        f"{name!r} does not bind {_VAR} through `env:` ({env_value!r}), so "
        "the variable could never reach this script")
    work = tmp / "ws"
    (work / str(wf["env"]["CONFIG_DIR"])).mkdir(parents=True)
    runner_temp = tmp / "runner-temp"
    runner_temp.mkdir()
    log = tmp / "argv.txt"
    _argv_stub(tmp / "bin", "docker", log)
    script = tmp / "step.sh"
    # umask 077 first: a runner with a tight umask must still produce a file
    # the non-root container user can read, which only the explicit chmod buys.
    script.write_text("umask 077\n" + _render_run(step["run"], wf["env"]),
                      encoding="utf-8", newline="\n")
    env = {
        "PATH": f"{tmp / 'bin'}{os.pathsep}{os.environ['PATH']}",
        "RUNNER_TEMP": str(runner_temp),
        # What the runner does with an unset `vars.X`: an empty string.
        _VAR: value,
    }
    proc = subprocess.run(
        # `bash -e {0}` is what Actions runs a `run:` with no `shell:` as.
        [_BASH, "-e", str(script)], cwd=work, env=env,
        capture_output=True, encoding="utf-8", errors="replace", timeout=60)
    return proc, log, runner_temp / "da-tools-policy.yaml"


@pytest.mark.skipif(_BASH is None, reason="bash not on PATH")
@pytest.mark.parametrize(("job", "name"), _GH_POLICY_STEPS)
def test_github_step_without_policy_notices_and_passes_no_flag(
        generated, tmp_path, job, name) -> None:
    proc, log, policy = _run_github_step(generated, tmp_path, job, name, "")
    assert proc.returncode == 0, proc.stderr
    assert f"::notice::{_VAR} is not set" in proc.stdout, proc.stdout
    argv = log.read_text(encoding="utf-8").splitlines()
    assert "--policy" not in argv and not any("policy.yaml" in a for a in argv)
    assert not policy.exists()


@pytest.mark.skipif(_BASH is None, reason="bash not on PATH")
@pytest.mark.parametrize(("job", "name"), _GH_POLICY_STEPS)
def test_github_step_with_policy_content_mounts_and_passes_it(
        generated, tmp_path, job, name) -> None:
    # The payload doubles as an injection probe: as data it is inert; pasted
    # into the script it would create the marker file.
    value = _POLICY_YAML + '# "; touch injected; echo "$(touch injected)"\n'
    proc, log, policy = _run_github_step(generated, tmp_path, job, name, value)
    assert proc.returncode == 0, proc.stderr
    assert "::notice::" not in proc.stdout
    assert not (tmp_path / "ws" / "injected").exists()
    assert policy.read_text(encoding="utf-8") == value + "\n"
    assert stat.S_IMODE(policy.stat().st_mode) == 0o644
    argv = log.read_text(encoding="utf-8").splitlines()
    mount = f"{policy}:/data/policy.yaml:ro"
    assert mount in argv and argv[argv.index(mount) - 1] == "-v", argv
    image = str(_workflow(generated)["env"]["DA_TOOLS_IMAGE"])
    # The flag belongs to the subcommand, i.e. AFTER the image in docker argv.
    tail = argv[argv.index(image) + 1:]
    assert tail[-2:] == ["--policy", "/data/policy.yaml"], argv


def _run_gitlab_block(root: Path, tmp: Path, job_name: str, shell: list[str],
                      value: str | None) -> tuple[subprocess.CompletedProcess, Path]:
    pipe = _gl_pipeline(root)
    block = _gl_policy_block(pipe[job_name], job_name)
    work = tmp / "ws"
    work.mkdir()
    log = tmp / "argv.txt"
    _argv_stub(tmp / "bin", "da-tools", log)
    script = tmp / "job.sh"
    script.write_text(block, encoding="utf-8", newline="\n")
    env = {
        "PATH": f"{tmp / 'bin'}{os.pathsep}{os.environ['PATH']}",
        "CONFIG_DIR": str(pipe["variables"]["CONFIG_DIR"]),
    }
    if value is not None:
        env[_VAR] = value
    proc = subprocess.run(
        [*shell, "-e", str(script)], cwd=work, env=env,
        capture_output=True, encoding="utf-8", errors="replace", timeout=60)
    return proc, log


_needs_posix_sh = pytest.mark.skipif(
    not _POSIX_SHELLS, reason="neither busybox nor dash on PATH")
_shells = pytest.mark.parametrize(
    "shell", _POSIX_SHELLS or [["sh"]],
    ids=lambda s: Path(s[0]).name)


@_needs_posix_sh
@_shells
@pytest.mark.parametrize("job_name", _GL_POLICY_JOBS)
def test_gitlab_without_policy_notices_and_passes_no_flag(
        generated, tmp_path, job_name, shell) -> None:
    proc, log = _run_gitlab_block(generated, tmp_path, job_name, shell, None)
    assert proc.returncode == 0, proc.stderr
    assert f"NOTICE: {_VAR} is not set" in proc.stdout, proc.stdout
    assert "--policy" not in log.read_text(encoding="utf-8").splitlines()


@_needs_posix_sh
@_shells
@pytest.mark.parametrize("job_name", _GL_POLICY_JOBS)
def test_gitlab_with_a_file_variable_passes_its_path(
        generated, tmp_path, job_name, shell) -> None:
    # A File-type variable: the runner wrote the content to a temp file and
    # the variable holds that path — with a space, as runner paths may.
    policy = tmp_path / "runner tmp" / "DA_TOOLS_POLICY"
    policy.parent.mkdir()
    policy.write_text(_POLICY_YAML, encoding="utf-8")
    proc, log = _run_gitlab_block(
        generated, tmp_path, job_name, shell, str(policy))
    assert proc.returncode == 0, proc.stderr
    assert "NOTICE" not in proc.stdout
    argv = log.read_text(encoding="utf-8").splitlines()
    assert argv[-2:] == ["--policy", str(policy)], argv


@_needs_posix_sh
@_shells
@pytest.mark.parametrize("job_name", _GL_POLICY_JOBS)
def test_gitlab_with_a_non_file_variable_fails_naming_the_type(
        generated, tmp_path, job_name, shell) -> None:
    # The common mistake: a Variable-type variable holding the YAML itself.
    proc, log = _run_gitlab_block(
        generated, tmp_path, job_name, shell, _POLICY_YAML)
    assert proc.returncode == 1, (proc.returncode, proc.stdout, proc.stderr)
    assert "type File" in proc.stderr and _VAR in proc.stderr, proc.stderr
    assert not log.exists(), "da-tools ran although the variable was refused"


def test_the_notice_link_lands_on_an_anchor_that_exists() -> None:
    """The notice/error text sends the customer to a docs anchor; keep it real.

    The link checker reads heading anchors only, so the explicit `<a id>` this
    URL targets is invisible to it — held here instead.
    """
    page, _, fragment = ip._POLICY_DOCS_URL.partition("#")
    rel = page.split("/Dynamic-Alerting-Integrations/", 1)[1].rstrip("/")
    doc = _REPO_ROOT / "docs" / f"{rel}.md"
    assert doc.is_file(), f"{ip._POLICY_DOCS_URL} maps to no page ({doc})"
    assert f'<a id="{fragment}"></a>' in doc.read_text(encoding="utf-8"), (
        f"{doc.name} has no anchor {fragment!r} for the CI notice to land on")
