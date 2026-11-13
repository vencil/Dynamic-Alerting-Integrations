"""Every consumer of docs/assets/dist/ builds it from source first (#3738).

WHY
---
The portal bundles under ``docs/assets/dist/`` are build output of
``tools/portal/`` (``npm run build`` → ``build.mjs``). Until #3738 they were
committed and every consumer trusted the committed copy. That copy went stale
on main even though CI had compared it on the PR's merge ref: branch
protection is not strict, so main moved between that check and the merge
(#3723 + #3729 → a bundle importing a chunk that no longer existed), and the
broken copy was deployed to Pages. Any "is the committed copy current?" check
has the same window. The fix is that nothing consumes a committed copy: each
consumer builds it, and the directory is no longer in git at all
(``tests/lint/test_portal_dist_not_tracked.py`` guards that half).

WHAT THIS PINS (one test per consumer, plus the deploy trigger)
---------------------------------------------------------------
* Pages deploy (``mkdocs-deploy.yaml``): a portal build runs before
  ``mkdocs build`` in the same job, after ``setup-node`` and ``npm ci``; and
  ``on.push.paths`` covers every tracked file under ``tools/portal/`` — the
  build's whole input. Without that path a push that only edits portal source
  never redeploys and Pages keeps serving the old bundles.
* Smoke tests (``ci.yml`` ``smoke-tests-run``) and the visual-baseline
  workflow: a portal build runs before the step that starts the server over
  ``docs/``.
* Every one of those steps (``setup-node``, ``npm ci``, the build) runs
  unconditionally: no step-level ``if:`` and no ``continue-on-error`` on the
  step or its job. Either one turns "the build ran" into "the build may have
  been skipped or failed", and the consumer then serves the checkout's copy
  with the job still green.
* The da-portal image: a ``portal-build`` stage runs ``npm ci`` and
  ``npm run build``, and the final stage takes ``assets/dist/`` from that
  stage — never from the build context's ``docs/assets/dist/``, neither by
  naming it nor by a later directory COPY whose destination contains
  ``/assets/dist`` (``COPY docs/assets/ …/assets/`` would overwrite the
  stage's bundles with a host-built copy). Single-file COPYs into
  ``/assets/`` stay allowed.

WHAT THIS DOES NOT PIN
----------------------
* That the build succeeds or the bundles work — Portal Tests (build + Vitest),
  the smoke run and Component Docker Build do that by running them.
* Consumers this file does not list. The list was taken by asking who serves,
  COPYs or reads ``docs/`` / ``dist`` (workflows, Dockerfiles, Makefile,
  scripts), not by grepping ``assets/dist``; a new consumer must be added here
  by hand.

Pure file parsing (YAML + Dockerfile text) plus one ``git ls-files`` via
``repo_files``; no network, no node.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from _tree import repo_files

_REPO = Path(__file__).resolve().parents[2]
_WF = _REPO / ".github" / "workflows"
_DOCKERFILE = _REPO / "components" / "da-portal" / "Dockerfile"
_PORTAL = "tools/portal"


# ── workflow readers ────────────────────────────────────────────────────────


def _load(name: str) -> dict:
    data = yaml.safe_load((_WF / name).read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"{name} did not parse to a mapping"
    return data


def _on(wf: dict) -> dict:
    # PyYAML (YAML 1.1) reads the bare key `on` as boolean True.
    on = wf.get("on", wf.get(True))
    assert isinstance(on, dict), f"workflow `on:` is not a mapping: {on!r}"
    return on


def _steps(wf: dict, job: str) -> list[dict]:
    steps = (wf.get("jobs") or {}).get(job, {}).get("steps")
    assert isinstance(steps, list) and steps, f"job {job!r} has no steps"
    return steps


def _in_portal(step: dict) -> bool:
    return str(step.get("working-directory", "")).rstrip("/") == _PORTAL


def _run(step: dict) -> str:
    return str(step.get("run", ""))


def _index(steps: list[dict], pred, what: str) -> int:
    hits = [i for i, s in enumerate(steps) if pred(s)]
    assert hits, f"no step {what}"
    return hits[0]


def _conditional(step: dict) -> list[str]:
    """Why `step` may not run, or may fail without failing the job."""
    why = []
    if "if" in step:
        why.append(f"if: {step['if']!r}")
    if "continue-on-error" in step:
        why.append(f"continue-on-error: {step['continue-on-error']!r}")
    return why


def _assert_job_fails_on_its_steps(wf: dict, job: str) -> None:
    body = (wf.get("jobs") or {}).get(job, {})
    assert "continue-on-error" not in body, (
        f"job {job} sets continue-on-error: {body['continue-on-error']!r} — a "
        f"failed portal build would leave the job green")


def _portal_build_index(steps: list[dict], job: str) -> int:
    """Index of the step that builds the bundles, after asserting an
    unconditional `npm ci` ran in tools/portal before it and the build step
    itself is unconditional. Accepts `npm run build` in tools/portal or
    `make portal-build` (the same command, via the Makefile)."""
    def is_build(s: dict) -> bool:
        run = _run(s)
        return (_in_portal(s) and re.search(r"\bnpm run build\b", run) is not None) or (
            re.search(r"\bmake\s+portal-build\b", run) is not None)
    b = _index(steps, is_build, f"in {job} builds the portal (npm run build in {_PORTAL})")
    why = _conditional(steps[b])
    assert not why, f"{job}: the portal build step is conditional ({'; '.join(why)})"
    ci = [i for i, s in enumerate(steps[:b])
          if _in_portal(s) and re.search(r"\bnpm ci\b", _run(s))]
    assert ci, f"{job}: the portal build has no `npm ci` in {_PORTAL} before it"
    bad = {i: _conditional(steps[i]) for i in ci if _conditional(steps[i])}
    assert len(bad) < len(ci), (
        f"{job}: every `npm ci` in {_PORTAL} before the build is conditional: {bad}")
    return b


def _setup_node_index(steps: list[dict], job: str) -> int:
    n = _index(steps, lambda s: str(s.get("uses", "")).startswith("actions/setup-node@"),
               f"in {job} uses actions/setup-node")
    why = _conditional(steps[n])
    assert not why, f"{job}: setup-node is conditional ({'; '.join(why)})"
    return n


def _glob_re(pattern: str) -> re.Pattern:
    """GitHub `paths` glob, the subset used here: `**` any depth, `*` one segment."""
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**", i):
            out += ".*"
            i += 2
        elif pattern[i] == "*":
            out += "[^/]*"
            i += 1
        else:
            out += re.escape(pattern[i])
            i += 1
    return re.compile(out + r"\Z")


def _uncovered(patterns: list[str], files: list[str]) -> list[str]:
    assert not any(p.startswith("!") for p in patterns), (
        "a negated `paths` entry is not modelled here — extend _uncovered()")
    regs = [_glob_re(p) for p in patterns]
    return [f for f in files if not any(r.match(f) for r in regs)]


def _portal_inputs() -> list[str]:
    files = sorted(p.relative_to(_REPO).as_posix()
                   for p in repo_files() if p.relative_to(_REPO).as_posix().startswith(_PORTAL + "/"))
    # The build's own inputs must be among them, or the walker lost the tree.
    for must in ("package.json", "package-lock.json", "build.mjs", "manifest.json"):
        assert f"{_PORTAL}/{must}" in files, f"{_PORTAL}/{must} is not tracked — re-derive this test"
    return files


# ── Pages deploy ────────────────────────────────────────────────────────────


def test_pages_deploy_triggers_on_every_portal_build_input() -> None:
    paths = _on(_load("mkdocs-deploy.yaml")).get("push", {}).get("paths")
    assert isinstance(paths, list) and paths, "mkdocs-deploy.yaml has no on.push.paths"
    missing = _uncovered(paths, _portal_inputs())
    assert not missing, (
        "mkdocs-deploy.yaml on.push.paths does not cover these portal build "
        "inputs, so a push changing only them would not redeploy the bundles "
        "(add 'tools/portal/**'):\n  " + "\n  ".join(missing[:20]))


def test_pages_deploy_builds_the_portal_before_mkdocs() -> None:
    wf = _load("mkdocs-deploy.yaml")
    jobs = [j for j, body in (wf.get("jobs") or {}).items()
            if any(re.search(r"\bmkdocs build\b", _run(s)) for s in body.get("steps") or [])]
    assert jobs, "no job in mkdocs-deploy.yaml runs `mkdocs build` — re-derive this test"
    for job in jobs:
        steps = _steps(wf, job)
        _assert_job_fails_on_its_steps(wf, job)
        mk = _index(steps, lambda s: re.search(r"\bmkdocs build\b", _run(s)) is not None,
                    f"in {job} runs mkdocs build")
        build = _portal_build_index(steps, job)
        node = _setup_node_index(steps, job)
        assert node < build < mk, (
            f"mkdocs-deploy.yaml:{job}: setup-node (step {node}) → portal build "
            f"(step {build}) → mkdocs build (step {mk}) is out of order; the site "
            f"would ship whatever docs/assets/dist/ the checkout holds")


# ── servers over docs/ ──────────────────────────────────────────────────────


@pytest.mark.parametrize("workflow,job", [
    ("ci.yml", "smoke-tests-run"),
    ("visual-baseline.yaml", "regenerate-baselines"),
])
def test_portal_server_serves_a_fresh_build(workflow: str, job: str) -> None:
    wf = _load(workflow)
    steps = _steps(wf, job)
    _assert_job_fails_on_its_steps(wf, job)
    serve = _index(steps, lambda s: "serve:portal" in _run(s),
                   f"in {job} starts the portal server (npm run serve:portal)")
    build = _portal_build_index(steps, job)
    node = _setup_node_index(steps, job)
    assert node < build < serve, (
        f"{workflow}:{job}: setup-node (step {node}) → portal build (step {build}) "
        f"→ server start (step {serve}) is out of order; the server would serve "
        f"the checkout's docs/assets/dist/")


# ── da-portal image ─────────────────────────────────────────────────────────


def _stages(text: str) -> list[tuple[str, str, list[str]]]:
    """(FROM line, stage name or '', instruction lines) per stage, with
    continuations joined and comments dropped."""
    stages: list[tuple[str, str, list[str]]] = []
    buf = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not buf and (not line or line.startswith("#")):
            continue
        if line.endswith("\\"):
            buf += line[:-1] + " "
            continue
        line, buf = buf + line, ""
        if re.match(r"(?i)^FROM\s", line):
            m = re.search(r"(?i)\sAS\s+(\S+)\s*$", line)
            stages.append((line, m.group(1) if m else "", []))
        elif stages:
            stages[-1][2].append(line)
    assert stages, "no FROM in the da-portal Dockerfile"
    return stages


def test_da_portal_image_builds_its_bundles() -> None:
    stages = _stages(_DOCKERFILE.read_text(encoding="utf-8"))
    named = {name: (frm, body) for frm, name, body in stages if name}
    assert "portal-build" in named, "da-portal Dockerfile has no `AS portal-build` stage"
    frm, body = named["portal-build"]
    assert "--platform=$BUILDPLATFORM" in frm, (
        "portal-build must run on $BUILDPLATFORM: its output is arch-independent JS, "
        "and without it the multi-arch release runs esbuild under QEMU")
    run = "\n".join(l for l in body if l.upper().startswith("RUN"))
    ci, build = re.search(r"\bnpm ci\b", run), re.search(r"\bnpm run build\b", run)
    assert ci and build and ci.start() < build.start(), (
        "portal-build must run `npm ci` and then `npm run build`")

    final = stages[-1][2]
    copies = [l for l in final if l.upper().startswith("COPY")]
    into_dist = [l for l in copies if "/usr/share/nginx/html/assets/dist" in l]
    assert into_dist, "the final stage COPYs nothing into /usr/share/nginx/html/assets/dist/"
    bad = [l for l in into_dist if "--from=portal-build" not in l]
    assert not bad, (
        "the image's bundles must come from the portal-build stage, not the build "
        "context's docs/assets/dist/ (a host build, if any):\n  " + "\n  ".join(bad))
    ctx = [l for l in copies if "--from=" not in l and re.search(r"\sdocs/assets/dist\b", l)]
    assert not ctx, "the final stage still COPYs docs/assets/dist from the context:\n  " + "\n  ".join(ctx)
    over = [l for l in copies if "--from=portal-build" not in l and _overwrites_dist(l)]
    assert not over, (
        "a COPY not from portal-build lands a directory over /assets/dist — it "
        "would replace the stage's bundles with whatever the source holds (the "
        "context's docs/assets/dist/, a host build if any):\n  " + "\n  ".join(over))


_DIST_DEST = "/usr/share/nginx/html/assets/dist/"


def _overwrites_dist(copy_line: str) -> bool:
    """Does this COPY put a DIRECTORY (or glob) at /assets/dist or an ancestor
    of it? A single-file source can only create one file, never a dist/ tree,
    so `COPY docs/assets/flows.json /usr/share/nginx/html/assets/flows.json`
    and `COPY a.json b.json /usr/share/nginx/html/assets/` are allowed."""
    tokens = [t for t in copy_line.split()[1:] if not t.startswith("--")]
    if len(tokens) < 2:
        return False
    *sources, dest = tokens
    dest = dest if dest.endswith("/") else dest + "/"
    if not _DIST_DEST.startswith(dest):
        return False
    return any(src.endswith("/") or src in (".", "./") or any(c in src for c in "*?[")
               or "." not in src.rsplit("/", 1)[-1]
               for src in sources)


def test_build_context_excludes_portal_node_modules() -> None:
    """The portal-build stage COPYs tools/portal/ after `npm ci`; a host
    node_modules in the context would overwrite the installed one (host-arch
    esbuild binary included)."""
    lines = [l.strip() for l in (_REPO / ".dockerignore").read_text(encoding="utf-8").splitlines()]
    assert "/tools/portal/node_modules/" in lines or "/tools/portal/node_modules" in lines, (
        ".dockerignore does not exclude /tools/portal/node_modules/")
    assert not any(l.startswith("!") and "tools/portal/node_modules" in l for l in lines), (
        ".dockerignore re-includes tools/portal/node_modules")


def test_image_smoke_build_triggers_on_every_portal_build_input() -> None:
    """Component Docker Build is the only pre-merge build of the da-portal
    image. Since #3738 the image builds its bundles from tools/portal/ and
    takes its context through the repo-root .dockerignore, so a PR touching
    only either must still build it."""
    paths = _on(_load("component-docker-build.yaml")).get("pull_request", {}).get("paths")
    assert isinstance(paths, list) and paths, "component-docker-build.yaml has no on.pull_request.paths"
    missing = _uncovered(paths, _portal_inputs() + [".dockerignore"])
    assert not missing, (
        "component-docker-build.yaml on.pull_request.paths misses inputs of the "
        "da-portal image build:\n  " + "\n  ".join(missing[:20]))
