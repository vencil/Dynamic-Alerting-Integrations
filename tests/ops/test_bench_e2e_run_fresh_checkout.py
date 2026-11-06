"""bench_e2e_run.sh on a fresh checkout: the synthetic fixture tree is absent.

Since #1443 nothing under `tests/e2e-bench/fixture/synthetic-v*/` is tracked,
so a fresh checkout (and every `bench-e2e-record.yaml` dispatch) starts with the
directory missing. Under `set -euo pipefail` a `find` on a missing path used to
fail the tenant-count pipeline and end the script right after its banner, rc 1,
with no message. These tests run the real script from a copied repo layout with
`python3` and `docker` stubs on PATH, so nothing touches the real tree or Docker.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "ops" / "bench_e2e_run.sh"

# Records every call; for the fixture generator it also creates the --output
# tree the way the real generator does, so the script can go on to stage it.
_PYTHON_STUB = """#!/bin/sh
echo "$*" >> "$STUB_LOG"
case "$*" in
  *generate_tenant_fixture.py*)
    out=""; prev=""
    for a in "$@"; do
      if [ "$prev" = "--output" ]; then out="$a"; fi
      prev="$a"
    done
    mkdir -p "$out" && printf 'defaults:\\n  bench_trigger: 50\\n' > "$out/_defaults.yaml"
    printf 'tenants:\\n  t0: {}\\n' > "$out/t0.yaml"
    ;;
esac
exit 0
"""
# Docker fails, so the run stops at the first container step after staging.
_DOCKER_STUB = "#!/bin/sh\necho \"docker $*\" >> \"$STUB_LOG\"\nexit 1\n"


def _layout(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    root = tmp_path / "repo"
    (root / "scripts" / "ops").mkdir(parents=True)
    shutil.copy2(SCRIPT, root / "scripts" / "ops" / SCRIPT.name)
    (root / "tests" / "e2e-bench").mkdir(parents=True)
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    for name, body in (("python3", _PYTHON_STUB), ("docker", _DOCKER_STUB)):
        (stubs / name).write_text(body, encoding="utf-8", newline="\n")
        (stubs / name).chmod(0o755)
    log = tmp_path / "calls.log"
    env = dict(os.environ, PATH=f"{stubs}{os.pathsep}{os.environ['PATH']}",
               STUB_LOG=str(log), COUNT="1", FIXTURE_TENANT_COUNT="1")
    return root, log, env


@pytest.mark.parametrize("kind", ["synthetic-v1", "synthetic-v2"])
def test_a_missing_fixture_tree_is_generated_not_a_silent_exit(tmp_path, kind):
    root, log, env = _layout(tmp_path)
    env["E2E_FIXTURE_KIND"] = kind
    fixture = root / "tests" / "e2e-bench" / "fixture" / kind / "conf.d"
    assert not fixture.exists()

    proc = subprocess.run(["bash", str(root / "scripts" / "ops" / SCRIPT.name)],
                          env=env, capture_output=True, text=True,
                          encoding="utf-8", timeout=60)
    calls = log.read_text(encoding="utf-8") if log.exists() else ""

    assert "generate_tenant_fixture.py" in calls, (
        "the script never reached the fixture generator — on a fresh checkout "
        f"it stopped first (rc={proc.returncode}).\nstdout:\n{proc.stdout}\n"
        f"stderr:\n{proc.stderr}")
    assert "[bench-e2e] staging fixture into fixture/active/conf.d/" in proc.stdout
    assert (root / "tests" / "e2e-bench" / "fixture" / "active" / "conf.d"
            / "_defaults.yaml").is_file()


def test_an_existing_tree_with_tenants_is_reused_not_regenerated(tmp_path):
    """Control: the guard must not turn every run into a regeneration."""
    root, log, env = _layout(tmp_path)
    env["E2E_FIXTURE_KIND"] = "synthetic-v1"
    fixture = root / "tests" / "e2e-bench" / "fixture" / "synthetic-v1" / "conf.d"
    fixture.mkdir(parents=True)
    (fixture / "_defaults.yaml").write_text("defaults:\n  bench_trigger: 50\n",
                                            encoding="utf-8")
    (fixture / "t0.yaml").write_text("tenants:\n  t0: {}\n", encoding="utf-8")

    proc = subprocess.run(["bash", str(root / "scripts" / "ops" / SCRIPT.name)],
                          env=env, capture_output=True, text=True,
                          encoding="utf-8", timeout=60)
    calls = log.read_text(encoding="utf-8") if log.exists() else ""

    assert "generate_tenant_fixture.py" not in calls, calls
    assert "[bench-e2e] staging fixture into fixture/active/conf.d/" in proc.stdout, (
        proc.returncode, proc.stdout, proc.stderr)
