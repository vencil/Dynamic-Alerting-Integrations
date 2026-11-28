"""The Go build cache has one writer and one shape (#4224 round 2, A1).

setup-go's cache keys on go.sum alone and never saves a key that exists, so
every job restored the build cache of the day go.sum last changed and rebuilt
every Go change since then (~131 s per Python shard). The fix is
.github/actions/go-build-cache (restore only, keyed on what the build reads)
plus go-build-cache.yaml (the one writer, on push to main). Held here:

  * a pull_request job that BUILDS this repo's Go binaries and does not run
    `go test` uses the composite and no `actions/setup-go` of its own.
    "Builds" is read from the job: a `run:` that calls scripts/ops/go_build.sh,
    `make` with a target whose recipe calls $(GO_BUILD) (read from the
    Makefile), a with_da_*.sh / with_admin_config_tools.sh wrapper, or
    VIBE_TEST_GO_BINARIES=build in its env.
    ⚠️ A job that builds only through something it does not name (the lint
    job's pre-commit hooks call the wrappers) is outside that reading; the
    second rule still keeps it off the stale cache.
  * no pull_request workflow turns setup-go's cache on explicitly
    (`cache: true`) — that is the go.sum-only key that went stale.
    `go test` / bench jobs are not ruled: `-race` and test binaries compile to
    other entries, and the bench jobs keep their own caches on purpose.
  * the composite turns setup-go's cache off, only restores, and its key
    hashes go_build.sh and the go.mod / go.sum / .go files of every module
    the Makefile's $(GO_BUILD) recipes build.
  * the warm workflow runs on push to main, builds every package a $(GO_BUILD)
    recipe builds,
    saves under the composite's key and paths, and is the only workflow that
    saves under that key.

Not shown here: that GitHub restores what this models, or that the restore
is faster than the build. That is measured on the runs after the merge.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
COMPOSITE = "./.github/actions/go-build-cache"
ACTION_FILE = ROOT / ".github" / "actions" / "go-build-cache" / "action.yml"
WARM = "go-build-cache.yaml"


def _load(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"{path.name}: not a YAML mapping"
    return data


def _triggers(wf: dict) -> set[str]:
    on = wf.get("on", wf.get(True))
    if isinstance(on, str):
        return {on}
    if isinstance(on, list):
        return set(on)
    return set(on or {})


def _workflows() -> list[Path]:
    found = sorted([*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")])
    assert found, f"no workflow under {WORKFLOWS}"
    return found


def go_build_targets(makefile: str) -> dict[str, set[tuple[str, str]]]:
    """{target: {(module dir, package)}} for every Makefile target whose
    recipe calls $(GO_BUILD). PURE."""
    out: dict[str, set[tuple[str, str]]] = {}
    target = None
    for line in makefile.splitlines():
        m = re.match(r"^([A-Za-z0-9_.-]+):(?!=)", line)
        if m:
            target = m.group(1)
            continue
        m = re.search(r"\$\(GO_BUILD\)\s+(\S+)\s+(\S+)", line)
        if line.startswith("\t") and m and target:
            out.setdefault(target, set()).add((m.group(1), m.group(2)))
    return out


TARGETS = go_build_targets((ROOT / "Makefile").read_text(encoding="utf-8"))


def _uses(step: dict) -> str:
    return str(step.get("uses", ""))


def builds_go(job: dict, targets) -> bool:
    """The job builds this repo's Go binaries, read from what it names. PURE."""
    steps = job.get("steps") or []
    text = "\n".join(str(s.get("run", "")) for s in steps)
    envs = [job.get("env") or {}, *[(s.get("env") or {}) for s in steps]]
    if "go_build.sh" in text:
        return True
    if re.search(r"with_(da_[a-z]+|admin_config_tools)\.sh", text):
        return True
    if any(str(e.get("VIBE_TEST_GO_BINARIES")) == "build" for e in envs):
        return True
    return any(re.search(rf"\bmake\b[^\n]*(?<![\w-]){re.escape(t)}(?![\w-])", text)
               for t in targets)


def job_problems(where: str, job: dict, targets) -> list[str]:
    """Problems with one pull_request job; [] when sound. PURE."""
    steps = job.get("steps") or []
    out = []
    for s in steps:
        if _uses(s).startswith("actions/setup-go") and (s.get("with") or {}).get("cache") is True:
            out.append(f"{where}: `actions/setup-go` with `cache: true` — its key is "
                       f"go.sum alone and never refreshes; use {COMPOSITE}")
    text = "\n".join(str(s.get("run", "")) for s in steps)
    if re.search(r"\bgo test\b", text) or not builds_go(job, targets):
        return out
    if not any(_uses(s) == COMPOSITE for s in steps):
        out.append(f"{where}: builds this repo's Go binaries without {COMPOSITE}")
    if any(_uses(s).startswith("actions/setup-go") for s in steps):
        out.append(f"{where}: a builder job sets up Go itself next to {COMPOSITE}")
    return out


def _pr_jobs():
    for path in _workflows():
        wf = _load(path)
        if "pull_request" not in _triggers(wf):
            continue
        for name, job in (wf.get("jobs") or {}).items():
            yield f"{path.name}:{name}", job


def test_the_makefile_reading_sees_the_builders() -> None:
    # Anchors the population: a regex that matched nothing would leave
    # builds_go() reading only the other signals, and the warm-workflow test
    # comparing against an empty set.
    assert {"da-guard-build", "da-read-build", "da-crdecode-build",
            "tenant-api-build"} <= set(TARGETS), TARGETS


def test_pull_request_builder_jobs_use_the_composite() -> None:
    jobs = list(_pr_jobs())
    builders = [w for w, j in jobs if builds_go(j, TARGETS)]
    assert len(builders) >= 5, builders  # the scan sees the builders at all
    problems = [p for w, j in jobs for p in job_problems(w, j, TARGETS)]
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize("job, expected", [
    ({"steps": [{"uses": "actions/setup-go@v6", "with": {"cache": True}},
                {"run": "make da-guard-build"}]}, 3),  # stale cache, no composite, own Go
    ({"steps": [{"uses": "actions/setup-go@v6"},
                {"run": "sh scripts/ops/go_build.sh a b c"}]}, 2),
    ({"steps": [{"run": "bash scripts/ops/with_da_read.sh x"}]}, 1),  # no composite
    ({"env": {"VIBE_TEST_GO_BINARIES": "build"}, "steps": [{"run": "pytest"}]}, 1),
    ({"steps": [{"uses": COMPOSITE}, {"run": "make da-read-build"}]}, 0),
    ({"steps": [{"uses": "actions/setup-go@v6"},
                {"run": "go test -race ./..."}]}, 0),          # go test: not ruled
    ({"steps": [{"uses": "actions/setup-go@v6", "with": {"cache": True}},
                {"run": "go test ./..."}]}, 1),                # but never cache: true
    ({"steps": [{"run": "make da-guard-build-extra"}]}, 0),    # not a target
], ids=["stale-and-own", "own-setup-go", "wrapper-no-composite", "env-build",
        "sound", "go-test", "go-test-cache-true", "other-target"])
def test_the_job_rule_can_fail(job, expected) -> None:
    targets = {"da-guard-build": {("a", "./cmd/a")}, "da-read-build": {("b", ".")}}
    assert len(job_problems("w.yml:j", job, targets)) == expected


def _composite() -> dict:
    return _load(ACTION_FILE)


def _module_dirs() -> set[str]:
    return {d for pairs in TARGETS.values() for d, _pkg in pairs}


def composite_problems(action: dict, module_dirs) -> list[str]:
    """Problems with the composite; [] when sound. PURE."""
    steps = (action.get("runs") or {}).get("steps") or []
    out = []
    go = [s for s in steps if _uses(s).startswith("actions/setup-go")]
    if len(go) != 1 or (go[0].get("with") or {}).get("cache") is not False:
        out.append("the composite must set up Go once with `cache: false`")
    if any(_uses(s).startswith(("actions/cache@", "actions/cache/save")) for s in steps):
        out.append("the composite saves a cache; only go-build-cache.yaml may")
    if not any(_uses(s).startswith("actions/cache/restore") for s in steps):
        out.append("the composite restores nothing")
    key_text = "\n".join(yaml.safe_dump(s) for s in steps)
    hashed = set(re.findall(r"'([^']+)'", " ".join(re.findall(r"hashFiles\(([^)]*)\)", key_text))))
    need = {"scripts/ops/go_build.sh"}
    for d in module_dirs:
        globbed = {p for p in hashed if p.startswith(d + "/")}
        for suffix in ("go.mod", "go.sum", "*.go"):
            if not any(p.endswith(suffix) for p in globbed):
                need.add(f"{d}/…{suffix}")
    missing = sorted(n for n in need if n not in hashed)
    if missing:
        out.append(f"the cache key does not hash {missing} — a change there "
                   f"would restore a build of another tree as an exact hit")
    return out


def test_the_composite_restores_a_key_of_what_the_build_reads() -> None:
    problems = composite_problems(_composite(), _module_dirs())
    assert not problems, "\n".join(problems)


def test_the_composite_rule_can_fail() -> None:
    action = _composite()
    dirs = _module_dirs()
    for mutate, why in [
        (lambda s: s.replace("cache: false", "cache: true"), "setup-go cache on"),
        (lambda s: s.replace("'scripts/ops/go_build.sh', ", ""), "flags not hashed"),
        (lambda s: s.replace("'components/da-read/**/*.go', ", ""), "a module's sources"),
        (lambda s: s.replace("actions/cache/restore@", "actions/cache@"), "saves"),
    ]:
        text = mutate(ACTION_FILE.read_text(encoding="utf-8"))
        assert text != ACTION_FILE.read_text(encoding="utf-8"), why
        assert composite_problems(yaml.safe_load(text), dirs), why
    assert not composite_problems(action, dirs)


def warm_problems(wf: dict, targets) -> list[str]:
    """Problems with the warm workflow; [] when sound. PURE."""
    out = []
    on = wf.get("on", wf.get(True)) or {}
    branches = ((on.get("push") or {}) if isinstance(on, dict) else {}).get("branches")
    if branches != ["main"] or "pull_request" in _triggers(wf):
        out.append("the writer must run on push to main and never on a PR")
    steps = [s for j in (wf.get("jobs") or {}).values() for s in (j.get("steps") or [])]
    ids = [s.get("id") for s in steps if _uses(s) == COMPOSITE]
    if len(ids) != 1 or not ids[0]:
        out.append(f"the writer must use {COMPOSITE} once, with an `id`")
        return out
    sid = ids[0]
    text = "\n".join(str(s.get("run", "")) for s in steps)
    built = {pkg for t, pkgs in targets.items()
             if re.search(rf"\bmake\b[^\n]*(?<![\w-]){re.escape(t)}(?![\w-])", text)
             for pkg in pkgs}
    unbuilt = sorted({pkg for pkgs in targets.values() for pkg in pkgs} - built)
    if unbuilt:
        out.append(f"the writer does not build {unbuilt}: a job building them "
                   f"would compile them on every run")
    saves = [s for s in steps if _uses(s).startswith("actions/cache/save")]
    if len(saves) != 1 or (saves[0].get("with") or {}) != {
            "path": f"${{{{ steps.{sid}.outputs.paths }}}}",
            "key": f"${{{{ steps.{sid}.outputs.key }}}}"}:
        out.append("the writer must save once, under the composite's key and paths")
    return out


def test_the_warm_workflow_writes_what_the_builders_restore() -> None:
    problems = warm_problems(_load(WORKFLOWS / WARM), TARGETS)
    assert not problems, "\n".join(problems)


def test_the_warm_rule_can_fail() -> None:
    sound = _load(WORKFLOWS / WARM)
    targets = TARGETS
    assert not warm_problems(sound, targets)
    on_pr = dict(sound, on={"pull_request": {}, "push": {"branches": ["main"]}})
    assert warm_problems(on_pr, targets)
    assert warm_problems(sound, {**targets, "new-build": {("components/x", "./cmd/x")}})
    text = (WORKFLOWS / WARM).read_text(encoding="utf-8")
    assert warm_problems(yaml.safe_load(
        text.replace("outputs.key }}\n", "outputs.key }}-x\n")), targets)


def test_only_the_warm_workflow_saves_under_the_composite_key() -> None:
    writers = []
    for path in _workflows():
        wf = _load(path)
        for job in (wf.get("jobs") or {}).values():
            steps = job.get("steps") or []
            ids = {s.get("id") for s in steps if _uses(s) == COMPOSITE and s.get("id")}
            for s in steps:
                key = str((s.get("with") or {}).get("key", ""))
                if any(f"steps.{i}.outputs.key" in key for i in ids) or "go-build-" in key:
                    writers.append(path.name)
    assert writers == [WARM], writers
