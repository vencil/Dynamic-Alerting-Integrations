"""Every distribution a pinned CI package pulls in is itself pinned (#3538).

``requirements/ci-constraints.txt`` freezes CI's Python deps so a new upstream
release cannot silently change test outcomes. ``check_unpinned_deps`` holds
that line only for the packages a ``pip install`` step NAMES; a package's own
dependencies resolve to whatever is latest that day. #3538 is that hole: only
``pytest-cov`` was pinned, its dependency ``coverage`` floated from 7.17.0 to
7.17.1, and a subprocess-measured ``KeyboardInterrupt`` started exiting 1
instead of dying by SIGINT — a red ``Python Coverage`` shard no diff caused.

This guard closes it from the installed metadata, offline: starting at every
pinned package that is installed in the running interpreter, walk
``Requires-Dist`` recursively (environment markers evaluated for THIS
interpreter; an extra's requirements followed only when something upstream
asked for that extra) and require every distribution reached to have a
``name==version`` line in the constraints file, names compared after PEP 503
normalisation.

Only a distribution whose INSTALLED version equals its pin is walked — as a
starting point or when reached. Its ``Requires-Dist`` is then the same one CI
reads, because CI installs through ``-c requirements/ci-constraints.txt`` and
so always has the pinned version. A drifted local install (say python-dateutil
2.9 against a 4.0.0 pin) declares a different dependency set — 2.9 needs
``six``, which CI never installs — and walking it would report pins CI does not
need. Such a distribution is not walked and not failed; it is listed as
"drifted, not walked" through ``warnings.warn``. In CI nothing drifts, so the
walk there covers the whole reachable set.

What it SEES: the install set of the interpreter that runs pytest — in CI the
``Python Tests`` lane, locally whatever the dev container / session-start
installed. What it does NOT see: packages other jobs install into their own
interpreters (e.g. the MkDocs build's stack, when this interpreter lacks it),
requirements files other than ci-constraints, and anything below a drifted
distribution. A pinned package that is not installed here is skipped as a
starting point, but at least one pinned package must be installed AT its pin,
or the walk proved nothing and the test fails.

No network, no lockfile, no subprocess.
"""
from __future__ import annotations

import importlib.metadata as md
import re
import warnings
from collections import deque
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

_REPO = Path(__file__).resolve().parents[2]
_CONSTRAINTS = _REPO / "requirements" / "ci-constraints.txt"
_PIN = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*==\s*([^\s;#]+)")


def _pins() -> dict[str, str]:
    pins: dict[str, str] = {}
    for line in _CONSTRAINTS.read_text(encoding="utf-8").splitlines():
        m = _PIN.match(line)
        if m:
            pins[canonicalize_name(m.group(1))] = m.group(2)
    assert pins, f"no `name==version` pin read from {_CONSTRAINTS}"
    return pins


def _distribution(name: str) -> md.Distribution | None:
    try:
        return md.distribution(name)
    except md.PackageNotFoundError:
        return None


def _applies(req: Requirement, extras: frozenset[str]) -> bool:
    """Does ``req`` apply here, given the extras its dependant was asked for?"""
    if req.marker is None:
        return True
    return any(req.marker.evaluate({"extra": e}) for e in ("", *sorted(extras)))


def _reached(roots: list[str], pins: dict[str, str]
             ) -> tuple[dict[str, list[str]], dict[str, str]]:
    """Breadth-first walk from ``roots``.

    Returns (each reached distribution -> its shortest path, each drifted
    distribution -> its installed version). A distribution is expanded only
    when it is installed at exactly its pinned version.
    """
    path_to: dict[str, list[str]] = {}
    drifted: dict[str, str] = {}
    seen: set[tuple[str, frozenset[str]]] = set()
    queue = deque((root, frozenset(), [root]) for root in roots)
    while queue:
        name, extras, path = queue.popleft()
        if (name, extras) in seen:
            continue
        seen.add((name, extras))
        path_to.setdefault(name, path)
        dist = _distribution(name)
        if dist is None:  # required but absent: still a name that must be pinned
            continue
        if name in pins and dist.version != pins[name]:
            drifted[name] = dist.version
            continue
        for raw in dist.requires or []:
            req = Requirement(raw)
            if _applies(req, extras):
                dep = canonicalize_name(req.name)
                queue.append((dep, frozenset(req.extras), [*path, dep]))
    return path_to, drifted


def test_every_transitive_dependency_of_a_pinned_package_is_pinned() -> None:
    pins = _pins()
    installed = {name: dist for name in pins
                 if (dist := _distribution(name)) is not None}
    roots = [name for name, dist in installed.items() if dist.version == pins[name]]
    assert roots, (
        f"none of the {len(pins)} packages pinned in {_CONSTRAINTS.name} is "
        f"installed at its pinned version in this interpreter ({len(installed)} "
        "installed, all drifted) — the walk has no starting point and would "
        "pass without checking anything")
    path_to, drifted = _reached(roots, pins)
    drifted.update({name: dist.version for name, dist in installed.items()
                    if dist.version != pins[name]})
    if drifted:
        warnings.warn(
            "drifted, not walked (installed != pin, so its Requires-Dist is not "
            "the one CI reads): " + ", ".join(
                f"{name} {version} (pin {pins[name]})"
                for name, version in sorted(drifted.items())),
            stacklevel=1)
    unpinned = sorted(
        (name, path) for name, path in path_to.items() if name not in pins)
    assert not unpinned, (
        f"{len(unpinned)} distribution(s) reached from a pinned package have no "
        f"`==` pin in requirements/ci-constraints.txt, so CI installs whatever "
        "is latest that day (#3538). Pin each at the version CI currently "
        "installs:\n  " + "\n  ".join(
            f"{name} (installed {_installed(name)}) via {' -> '.join(path)}"
            for name, path in unpinned))


def _installed(name: str) -> str:
    dist = _distribution(name)
    return dist.version if dist is not None else "not installed"
