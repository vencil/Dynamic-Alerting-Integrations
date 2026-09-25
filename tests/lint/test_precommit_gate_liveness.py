"""Liveness contract for pre-commit gates that only ever run as subprocesses (#1984).

A gate can be dead without any unit test noticing: its script still detects
the violation, but the hook no longer runs it the way a commit would — the
``entry`` lost a flag (drop ``--ci`` from ``lint_html_doc_links`` and it reports
and exits 0), ``files:`` / ``exclude:`` / ``types:`` stop selecting the files
the rule is about, the hook moved to ``stages: [manual]``, or its isolated
environment lost an ``additional_dependencies`` entry. A test of the script's
functions sees none of that — it never reads the hook.

So this test does not re-implement any of pre-commit's selection rules: in a
scratch git repo holding a copy of the tracked tree, it runs the real
``pre-commit run <hook-id>`` against the copied ``.pre-commit-config.yaml``.

  - Violation: plant it, stage it, and run the hook the way a commit does
    (staged files, default stage). The hook must fail (exit 1, its
    ``- hook id:`` block present, no traceback).
  - Legal: plant it, stage it, run the hook over ``--all-files``. It must
    report ``Passed`` — ``Skipped`` means the checking path never ran.

Each hook is checked against ONE violation shape; other rule branches of the
same gate are not covered here (see #1984 for the measured boundary).

If pre-commit cannot build a hook's environment (it exits 3, e.g. offline
with a cold cache), the case fails as UNMEASURED rather than as a dead gate.

Known deviation from a real commit: diff-based gates get ``LINT_DIFF_BASE=HEAD``
because the scratch repo has no ``origin/*`` refs.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]

try:  # pragma: no cover - import probe
    import pre_commit as _pre_commit_mod  # noqa: F401

    _HAS_PRE_COMMIT = True
except ImportError:  # pragma: no cover - dev hosts without pre-commit
    _HAS_PRE_COMMIT = False

# Same convention as tests/ops/test_prepush_hook_wiring.py: CI sets
# VIBE_REQUIRE_PRE_COMMIT=1, and then a missing pre-commit fails by name
# (test_pre_commit_present_when_required) instead of skipping the module.
_REQUIRE_PRE_COMMIT = os.environ.get("VIBE_REQUIRE_PRE_COMMIT") == "1"

pytestmark = pytest.mark.skipif(
    not _HAS_PRE_COMMIT and not _REQUIRE_PRE_COMMIT,
    reason="pre-commit not installed (set VIBE_REQUIRE_PRE_COMMIT=1 to fail instead)",
)

_BAT = "scripts/ops/dx-run.bat"
_ARIA_PROBE = "tools/portal/src/interactive/tools/zz_liveness_probe_aria.jsx"
_GLOSSARY_ANCHOR = "**ADR (Architecture Decision Record)**"
_GLOSSARY_PLANT = "**ZZQ (Zeta Zulu Quebec)**\n:   liveness probe term.\n\n" + _GLOSSARY_ANCHOR
_AUDIENCE_ANCHOR = "  file: getting-started/wizard.jsx\n  audience:\n  - platform\n"


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def _append(root: Path, rel: str, text: str) -> None:
    with open(root / rel, "a", encoding="utf-8", newline="") as fh:
        fh.write(text)


def _replace_once(root: Path, rel: str, old: str, new: str) -> None:
    path = root / rel
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    assert count == 1, f"fixture anchor {old!r} occurs {count} times in {rel}; it must be unique"
    path.write_text(text.replace(old, new), encoding="utf-8", newline="")


def _unchanged(root: Path) -> None:
    return None


def _configmap_unmounted(root: Path) -> None:
    src = (root / "k8s/03-monitoring/configmap-rules-liveness.yaml").read_text(encoding="utf-8")
    _write(root, "k8s/03-monitoring/configmap-rules-zzprobe.yaml",
           src.replace("prometheus-rules-liveness", "prometheus-rules-zzprobe"))


def _glossary_synced(root: Path) -> None:
    _replace_once(root, "docs/glossary.md", _GLOSSARY_ANCHOR, _GLOSSARY_PLANT)
    subprocess.run([sys.executable, "-X", "utf8", "scripts/tools/dx/sync_glossary_abbr.py"],
                   cwd=root, check=True, capture_output=True, timeout=120)


@dataclass(frozen=True)
class Case:
    hook_id: str
    violation: Callable[[Path], None]
    legal: Callable[[Path], None]


CASES = [
    Case("ad-hoc-git-scripts-check",
         lambda r: _write(r, "_probe_commit.bat", "@echo off\r\n"),
         lambda r: _write(r, "scripts/ops/_probe_legal.bat", "@echo off\r\n")),
    Case("bat-ascii-purity-check",
         lambda r: _append(r, _BAT, "REM 中文\r\n"),
         lambda r: _append(r, _BAT, "REM ascii probe\r\n")),
    Case("configmap-mount-completeness", _configmap_unmounted, _unchanged),
    Case("dist-source-consistency-check",
         lambda r: _append(r, "docs/assets/dist/alert-builder.js", "\n// probe\n"),
         lambda r: (_append(r, "docs/assets/dist/alert-builder.js", "\n// probe\n"),
                    _write(r, "tools/portal/src/zz_liveness_probe_source.js", "// probe\n"))),
    Case("portal-audience-enum-check",
         lambda r: _replace_once(r, "docs/assets/tool-registry.yaml", _AUDIENCE_ANCHOR,
                                 _AUDIENCE_ANCHOR.replace("- platform", "- zzprobe")),
         lambda r: _replace_once(r, "docs/assets/tool-registry.yaml", _AUDIENCE_ANCHOR,
                                 _AUDIENCE_ANCHOR.replace("- platform", "- sre"))),
    Case("skip-a11y-justification-check",
         lambda r: _write(r, "tests/e2e/zz-probe.spec.ts", "const cfg = {\n  skipA11y: true,\n};\n"),
         lambda r: _write(r, "tests/e2e/zz-probe.spec.ts",
                          "const cfg = {\n  // skipA11y: TD-040 third-party widget\n  skipA11y: true,\n};\n")),
    Case("threshold-observed-map-drift-check",
         lambda r: _replace_once(r, "scripts/tools/ops/metric_observed_map.yaml",
                                 "observed_series: tenant:clickhouse_active_connections:max",
                                 "observed_series: tenant:zzprobe_nonexistent:max"),
         _unchanged),
    Case("html-doc-links-check",
         lambda r: _replace_once(r, "docs/interactive/index.html", "</body>",
                                 '<a href="../../zzprobe.md">x</a>\n</body>'),
         lambda r: _replace_once(r, "docs/interactive/index.html", "</body>",
                                 '<a href="../adr/">x</a>\n</body>')),
    Case("open-encoding-audit",
         lambda r: _write(r, "scripts/tools/zz_liveness_probe_enc.py",
                          "def f(p):\n    return open(p).read()\n"),
         lambda r: _write(r, "scripts/tools/zz_liveness_probe_enc.py",
                          'def f(p):\n    return open(p, encoding="utf-8").read()\n')),
    Case("aria-references-check",
         lambda r: _write(r, _ARIA_PROBE, '<p aria-describedby="zz-probe-missing">x</p>\n'),
         lambda r: _write(r, _ARIA_PROBE,
                          '<p id="zz-probe-note">n</p>\n<p aria-describedby="zz-probe-note">x</p>\n')),
    Case("glossary-check",
         lambda r: _replace_once(r, "docs/glossary.md", _GLOSSARY_ANCHOR, _GLOSSARY_PLANT),
         _glossary_synced),
]


def _hook_ids() -> set[str]:
    with open(REPO_ROOT / ".pre-commit-config.yaml", encoding="utf-8") as fh:
        config = yaml.safe_load(fh)
    return {h["id"] for repo in config["repos"] for h in repo.get("hooks", [])}


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True,
                          text=True, timeout=120).stdout


@pytest.fixture(scope="module")
def scratch_repo(tmp_path_factory) -> Path:
    """A git repo whose single commit is a copy of the tracked working tree."""
    root = tmp_path_factory.mktemp("gate-liveness")
    listed = subprocess.run(["git", "ls-files", "-z"], cwd=REPO_ROOT, check=True,
                            capture_output=True, text=True, timeout=120).stdout.split("\0")
    for rel in filter(None, listed):
        src = REPO_ROOT / rel
        if src.is_file():
            dst = root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "-c", "user.email=liveness@example.invalid", "-c", "user.name=liveness",
         "-c", "commit.gpgsign=false", "commit", "-q", "--no-verify", "-m", "base")
    return root


def _reset(root: Path) -> None:
    _git(root, "reset", "-q", "--hard", "HEAD")
    _git(root, "clean", "-q", "-fd")


def _pre_commit(root: Path, hook_id: str, *extra: str) -> subprocess.CompletedProcess:
    _git(root, "add", "-A")
    env = {k: v for k, v in os.environ.items() if k not in ("GITHUB_BASE_REF", "PRE_COMMIT")}
    env["LINT_DIFF_BASE"] = "HEAD"
    return subprocess.run(
        [sys.executable, "-X", "utf8", "-m", "pre_commit", "run", hook_id, "--color=never", *extra],
        cwd=root, capture_output=True, text=True, timeout=600, env=env)


def _assert_measured(proc: subprocess.CompletedProcess, hook_id: str) -> str:
    """Fail as UNMEASURED when pre-commit exited with neither 0 nor 1.

    pre-commit reserves 0/1 for the hooks' verdict; anything else (3 when a
    hook environment cannot be built, e.g. offline with a cold cache) means the
    gate was never run. Reporting that as "the gate is dead" would be false.
    """
    out = proc.stdout + proc.stderr
    if proc.returncode not in (0, 1):
        pytest.fail(
            f"UNMEASURED {hook_id}: pre-commit rc={proc.returncode} is not a hook verdict "
            f"(environment build / install failure — e.g. no network with a cold "
            f"pre-commit cache). The gate was not checked.\n{out[-3000:]}")
    return out


def test_pre_commit_present_when_required():
    assert _HAS_PRE_COMMIT, "VIBE_REQUIRE_PRE_COMMIT=1 but pre-commit is not importable"


def test_every_case_names_an_existing_hook():
    missing = [c.hook_id for c in CASES if c.hook_id not in _hook_ids()]
    assert not missing, f"hook ids gone from .pre-commit-config.yaml: {missing}"


@pytest.mark.parametrize("case", CASES, ids=[c.hook_id for c in CASES])
def test_violation_fails_hook_at_commit(case, scratch_repo):
    _reset(scratch_repo)
    try:
        case.violation(scratch_repo)
        proc = _pre_commit(scratch_repo, case.hook_id)
    finally:
        _reset(scratch_repo)
    out = _assert_measured(proc, case.hook_id)
    assert "Traceback" not in out, f"{case.hook_id} crashed:\n{out[-3000:]}"
    assert proc.returncode == 1, (
        f"{case.hook_id}: pre-commit rc={proc.returncode} on a staged violation "
        f"(rc 0 = the hook did not run or did not fail)\n{out[-3000:]}")
    assert f"- hook id: {case.hook_id}" in out, f"{case.hook_id}: failure not attributed to it\n{out[-3000:]}"


@pytest.mark.parametrize("case", CASES, ids=[c.hook_id for c in CASES])
def test_legal_input_passes_hook(case, scratch_repo):
    _reset(scratch_repo)
    try:
        case.legal(scratch_repo)
        proc = _pre_commit(scratch_repo, case.hook_id, "--all-files")
    finally:
        _reset(scratch_repo)
    out = _assert_measured(proc, case.hook_id)
    assert proc.returncode == 0, f"{case.hook_id}: rc={proc.returncode} on legal input\n{out[-3000:]}"
    lines = [ln for ln in out.splitlines() if ln.rstrip().endswith(("Passed", "Skipped"))]
    assert lines and lines[-1].rstrip().endswith("Passed"), (
        f"{case.hook_id}: hook did not report Passed (Skipped = never checked)\n{out[-3000:]}")
