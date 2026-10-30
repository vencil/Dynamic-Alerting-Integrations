"""ADR-036 §12: `tenant-api state show` prints `tenant_api_version`, the version
of the tenant-api that judged the records. It is only meaningful if the image
build sets it: the Dockerfile's `ARG VERSION` reaches `-X main.version`, the
release workflow passes the tenant-api/v* tag as that build-arg, and the
symbol the linker sets is the one `state show` prints.

The first three are read from the files (a refactor that drops one of them
turns this red); the last is behaviour — a binary built with the same
`-X main.version=...` prints that value.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from _go_build import go_build_argv

REPO = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO / "components" / "tenant-api" / "Dockerfile"
RELEASE = REPO / ".github" / "workflows" / "release.yaml"
SERVER = REPO / "components" / "tenant-api" / "cmd" / "server"

#: What the Dockerfile's build (scripts/ops/go_build.sh, #3425) must pass to the linker.
LDFLAG = '-X "main.version=${VERSION}"'


def _dockerfile_instructions() -> list[str]:
    """Instructions with their continuation lines joined, comments dropped."""
    out, cur = [], ""
    for line in DOCKERFILE.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("#"):
            continue
        if line.rstrip().endswith("\\"):
            cur += line.rstrip()[:-1] + " "
            continue
        cur += line
        if cur.strip():
            out.append(cur.strip())
        cur = ""
    return out


def test_dockerfile_passes_the_version_arg_to_the_linker():
    ins = _dockerfile_instructions()
    builds = [i for i, s in enumerate(ins)
              if s.startswith("RUN ") and "scripts/ops/go_build.sh" in s and "./cmd/server" in s]
    assert len(builds) == 1, f"expected one go_build.sh build of ./cmd/server, got {builds}"
    build = ins[builds[0]]
    assert LDFLAG in build, f"the go build does not pass {LDFLAG!r}: {build}"
    # The ARG must be declared in the same stage, before the build.
    stage_start = max(i for i, s in enumerate(ins[:builds[0]]) if s.startswith("FROM "))
    args = [s for s in ins[stage_start:builds[0]] if re.match(r"ARG\s+VERSION(=|\s|$)", s)]
    assert args, "ARG VERSION is not declared in the build stage before the go build"


def test_release_passes_the_tag_as_version():
    wf = yaml.safe_load(RELEASE.read_text(encoding="utf-8"))
    steps = wf["jobs"]["release-tenant-api"]["steps"]
    builds = [s for s in steps if (s.get("with") or {}).get("file") == "components/tenant-api/Dockerfile"]
    assert len(builds) == 1, builds
    build_args = [ln.strip() for ln in (builds[0]["with"].get("build-args") or "").splitlines()]
    assert "VERSION=v${{ steps.version.outputs.VERSION }}" in build_args, build_args
    version_step = next(s for s in steps if s.get("id") == "version")
    assert "tenant-api/v" in version_step["run"], version_step


def test_the_linked_symbol_is_what_state_show_prints(tmp_path):
    src = "\n".join(p.read_text(encoding="utf-8") for p in SERVER.glob("*.go")
                    if not p.name.endswith("_test.go"))
    assert re.search(r"^var version string$", src, re.M), "main.version is not a package-level string"
    go = shutil.which("go")
    if go is None:
        pytest.fail("`go` is not on PATH: the linked version cannot be measured")
    binary = tmp_path / "tenant-api"
    proc = subprocess.run(go_build_argv(SERVER.parents[1], "./cmd/server", binary,
                                        ldflags_x=["main.version=v9.9.9-test"]),
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=900)
    assert proc.returncode == 0, proc.stderr
    state = tmp_path / "state"
    state.mkdir()
    (state / "_groups.yaml").write_text("groups: {}\n", encoding="utf-8")
    out = subprocess.run([str(binary), "state", "show", "--from", str(state)],
                         capture_output=True, check=True, timeout=60)
    assert json.loads(out.stdout)["tenant_api_version"] == "v9.9.9-test"
