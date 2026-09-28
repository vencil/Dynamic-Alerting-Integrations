"""Go toolchain parity — go.mod is the SSOT for which Go every surface builds with (#1278).

The shipped images carry the stdlib of whatever toolchain their Dockerfile
builder ran, and Trivy reads it straight out of the binary's build info. #1058
and #1278 were the same failure twice: the builder stayed on an older patch
than CI and than go.mod said, so every green CI run was compiled by a
toolchain the image never shipped, and the stdlib CVEs surfaced only in the
nightly image scan. The split had FOUR independent spellings, none compared:

  * each module's go.mod `go` directive       (1.26.3 / 1.26.4 / 1.26 / 1.23)
  * each Dockerfile's `golang:<ver>` builder   (1.26.3 / 1.26.5)
  * each workflow's setup-go `go-version`      ("1.26" — floating)
  * the dev container's Go feature `version`   ("1.23")

This module pins them to one value. The SSOT is the go.mod `go` directive:
it is the only spelling the Go toolchain itself enforces (GOTOOLCHAIN=auto
refuses to build a module with an older toolchain), so the others follow it.

⛔ Pure file parsing — no Go, no Docker, no network — so it runs in the
standard Python Tests job.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from _tree import REPO_ROOT as ROOT
from _tree import repo_files

# The module whose go.mod is the platform-wide Go SSOT: the dev container and
# every surface that builds no module of its own (e.g. the federation audit
# sidecar, which compiles upstream mtail) follow it.
SSOT_MODULE = "components/threshold-exporter/app"

# go.mod files whose `go` directive is deliberately NOT the SSOT, with why.
# ⛔ A floor is a minimum-compatibility claim, not a build toolchain: nothing
# may point setup-go at it (asserted below), or CI would build with the floor.
GO_DIRECTIVE_FLOORS: dict[str, str] = {
    "scripts/tools/ops": (
        "#1816: `go 1.23` is a compatibility floor so `go run bench_filter.go` "
        "keeps working under GOTOOLCHAIN=local on older toolchains (the bench "
        "wrappers run it in-module); stdlib-only, never shipped in an image"),
}

# Workflow setup-go steps allowed to keep a literal `go-version` (a deliberate
# multi-version matrix, say). Empty today: every step reads a go.mod.
FLOATING_GO_VERSION_ALLOWED: dict[tuple[str, str], str] = {}

_GO_DIRECTIVE = re.compile(r"^go\s+(\S+)\s*$", re.MULTILINE)
_GOLANG_FROM = re.compile(
    r"^FROM\s+(?:--platform=\S+\s+)?golang:([^\s@]+)", re.MULTILINE | re.IGNORECASE)
_MATRIX_REF = re.compile(r"\$\{\{\s*matrix\.([A-Za-z0-9_-]+)\s*\}\}")


def _rel(*suffixes: str) -> list[str]:
    """Repo-relative POSIX paths via tests/_tree.py (never rglob: it would scan
    the .claude/worktrees copies as if they were this tree)."""
    return sorted(p.relative_to(ROOT).as_posix() for p in repo_files(*suffixes))


def _go_directive(module_dir: str) -> str:
    text = (ROOT / module_dir / "go.mod").read_text(encoding="utf-8")
    found = _GO_DIRECTIVE.findall(text)
    assert len(found) == 1, f"{module_dir}/go.mod: expected one `go` line, got {found}"
    return found[0]


def _modules() -> list[str]:
    mods = [str(Path(p).parent) for p in _rel("go.mod")]
    assert SSOT_MODULE in mods, (
        f"the SSOT module {SSOT_MODULE!r} has no tracked go.mod — every "
        "assertion below would compare against nothing")
    return mods


def _ssot() -> str:
    return _go_directive(SSOT_MODULE)


def _module_for(dockerfile: str, modules: list[str]) -> str:
    """Nearest ancestor directory holding a go.mod; the SSOT when there is none
    (a builder that compiles upstream source, not a module of this repo)."""
    d = Path(dockerfile).parent
    while str(d) not in (".", ""):
        if str(d) in modules:
            return str(d)
        d = d.parent
    return SSOT_MODULE


def test_ssot_is_a_full_patch_version() -> None:
    """A bare `go 1.26` would let every downstream spelling float again."""
    assert re.fullmatch(r"\d+\.\d+\.\d+", _ssot()), (
        f"{SSOT_MODULE}/go.mod says `go {_ssot()}`; the SSOT must name a patch "
        "release — it is what the builders and setup-go resolve to.")


def test_every_module_go_directive_equals_ssot() -> None:
    ssot = _ssot()
    skew = {m: _go_directive(m) for m in _modules()
            if m not in GO_DIRECTIVE_FLOORS and _go_directive(m) != ssot}
    assert not skew, (
        f"go.mod `go` directive skew against the SSOT {SSOT_MODULE} (go {ssot}): "
        f"{skew}. Bump them together (`go mod edit -go={ssot}`), or — if one is "
        "a deliberate floor — list it in GO_DIRECTIVE_FLOORS with the reason.")


def test_floors_are_real_and_not_above_ssot() -> None:
    mods = _modules()
    ssot = tuple(int(x) for x in _ssot().split("."))
    for m in GO_DIRECTIVE_FLOORS:
        assert m in mods, f"GO_DIRECTIVE_FLOORS names {m!r}, which has no go.mod"
        floor = tuple(int(x) for x in _go_directive(m).split("."))
        assert floor <= ssot, f"{m} floor go {floor} is ABOVE the SSOT {ssot}"


def _golang_builders() -> list[tuple[str, str]]:
    found = []
    for df in _rel("Dockerfile"):
        text = (ROOT / df).read_text(encoding="utf-8")
        found += [(df, tag) for tag in _GOLANG_FROM.findall(text)]
    return found


def test_go_dockerfile_builders_match_their_go_mod() -> None:
    """The builder that compiles a shipped binary == the go.mod it builds."""
    builders = _golang_builders()
    # Anchor: the derivation must still see the image this all started from.
    assert any(df == f"{SSOT_MODULE}/Dockerfile" for df, _ in builders), (
        f"no golang builder found in {SSOT_MODULE}/Dockerfile — the FROM parser "
        f"or the file moved; builders seen: {builders}")
    modules = _modules()
    bad = []
    for df, tag in builders:
        m = re.fullmatch(r"(\d+\.\d+(?:\.\d+)?)(?:-.*)?", tag)
        version = m.group(1) if m else tag
        module = _module_for(df, modules)
        want = _go_directive(module) if module not in GO_DIRECTIVE_FLOORS else _ssot()
        if version != want:
            bad.append(f"{df}: golang:{tag} but {module}/go.mod says go {want}")
    assert not bad, (
        "Dockerfile Go builder != go.mod `go` directive — the image would ship a "
        "different stdlib than CI tested (the #1058 / #1278 split):\n  "
        + "\n  ".join(bad))


def test_devcontainer_go_feature_matches_ssot() -> None:
    text = (ROOT / ".devcontainer" / "devcontainer.json").read_text(encoding="utf-8")
    block = re.findall(
        r'"ghcr\.io/devcontainers/features/go:\d+"\s*:\s*\{([^}]*)\}', text)
    assert len(block) == 1, f"expected one Go feature block, found {len(block)}"
    version = re.findall(r'"version"\s*:\s*"([^"]+)"', block[0])
    assert len(version) == 1, f"Go feature: expected one `version`, got {version}"
    assert version[0] == _ssot(), (
        f"devcontainer Go feature version {version[0]!r} != {SSOT_MODULE}/go.mod "
        f"`go {_ssot()}`. Local builds then depend on GOTOOLCHAIN=auto fetching a "
        "different toolchain than the one the container advertises.")


def _setup_go_steps():
    """Yield (workflow, job_id, job, with) for every actions/setup-go step."""
    for wf in _rel(".yml", ".yaml"):
        if not wf.startswith(".github/workflows/"):
            continue
        doc = yaml.safe_load((ROOT / wf).read_text(encoding="utf-8")) or {}
        for job_id, job in (doc.get("jobs") or {}).items():
            for step in (job or {}).get("steps") or []:
                if str(step.get("uses", "")).startswith("actions/setup-go@"):
                    yield wf, job_id, job, (step.get("with") or {})


def _expand_matrix(value: str, job: dict) -> list[str]:
    refs = _MATRIX_REF.findall(value)
    if not refs:
        return [value]
    matrix = ((job.get("strategy") or {}).get("matrix")) or {}
    rows = list(matrix.get("include") or [])
    out = []
    for row in rows:
        for ref in refs:
            assert ref in row, f"matrix.{ref} missing from include row {row}"
        out.append(_MATRIX_REF.sub(lambda mm, r=row: str(r[mm.group(1)]), value))
    assert out, f"{value!r} references matrix keys {refs} but the job has no include rows"
    return out


def test_workflows_take_go_from_go_mod() -> None:
    steps = list(_setup_go_steps())
    assert steps, "no actions/setup-go step found — the workflow walk is broken"
    modules = _modules()
    floating, wrong_file = [], []
    for wf, job_id, job, with_ in steps:
        key = (Path(wf).name, job_id)
        if "go-version" in with_:
            if key not in FLOATING_GO_VERSION_ALLOWED:
                floating.append(f"{wf}::{job_id} go-version: {with_['go-version']!r}")
            continue
        if "go-version-file" not in with_:
            floating.append(f"{wf}::{job_id} sets neither go-version nor go-version-file")
            continue
        for path in _expand_matrix(str(with_["go-version-file"]), job):
            module = str(Path(path).parent)
            if Path(path).name != "go.mod" or module not in modules:
                wrong_file.append(f"{wf}::{job_id} go-version-file {path!r} is not a tracked go.mod")
            elif module in GO_DIRECTIVE_FLOORS:
                wrong_file.append(
                    f"{wf}::{job_id} reads {path}, a compatibility FLOOR "
                    f"({GO_DIRECTIVE_FLOORS[module]}) — point it at a module on the SSOT")
    assert not floating, (
        "setup-go must read the toolchain from go.mod (`go-version-file:`), not a "
        "literal: a floating \"1.26\" resolves to whatever 1.26.x the runner has, "
        "which is not what the Dockerfile builder ships (#1278). Offenders:\n  "
        + "\n  ".join(floating))
    assert not wrong_file, "\n".join(wrong_file)


def test_matrix_expansion_substitutes_every_row() -> None:
    """Pin the helper the workflow test depends on (it must not pass vacuously)."""
    job = {"strategy": {"matrix": {"include": [
        {"workdir": "a/b"}, {"workdir": "c"}]}}}
    assert _expand_matrix("${{ matrix.workdir }}/go.mod", job) == ["a/b/go.mod", "c/go.mod"]
    assert _expand_matrix("x/go.mod", {}) == ["x/go.mod"]
    with pytest.raises(AssertionError):
        _expand_matrix("${{ matrix.workdir }}/go.mod", {})
