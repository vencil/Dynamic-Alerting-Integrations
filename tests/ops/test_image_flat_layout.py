"""Every shipped tool must still import when the image flattens it (#1494).

⛔ Why this exists, and why nothing else could have caught it.

``build.sh`` copies each ``TOOL_FILES`` entry with a bare
``cp "$TOOLS_SRC/$f" "$SCRIPT_DIR/tools/"`` — the destination is one directory,
so ``ops/foo.py`` becomes ``tools/foo.py`` — and the Dockerfile then does
``WORKDIR /opt/da-tools`` + ``COPY tools/ ./``. A shipped module therefore ends
up at ``/opt/da-tools/foo.py``, which has exactly **three** ancestors. In a repo
checkout the same file sits at ``scripts/tools/ops/foo.py`` with eight or nine.

``_grar_validate.py`` computed a path with ``Path(__file__).resolve().parents[3]``
at **module scope**. Correct in the repo, ``IndexError`` in the image — and two
module-scope importers (``generate_alertmanager_routes``, ``byo_check``) meant
``generate-routes`` and ``byo-check`` would have died before their first line
the next time the image was built.

The reason 5000+ tests were green on it: ``tests/conftest.py`` puts the real
``scripts/tools``, ``scripts/tools/ops``, ``scripts/tools/dx`` and
``scripts/tools/lint`` directories on ``sys.path``, so every existing test
imports these modules **in the repo layout**. Nothing anywhere imported them in
the layout customers actually run. The CI docker builds could not see it either:
the PR-time build stubs da-tools with an *empty* ``tools/`` directory (and its
path filter does not even include ``scripts/tools/ops/**``), and the release
build never executes the image it pushes — not even ``--help``.

⛔ This test is deliberately BEHAVIOURAL rather than a second static rule.
``check_build_completeness.check_layout_depth_assumptions`` reads the source and
can be out-thought by a spelling it does not model; this one runs the import and
does not care how the path was written. The two are the pair: the static rule
also covers the *silent* variant (a ``.parent`` chain saturates at the
filesystem root instead of raising), which an import that merely survives cannot
detect.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LINT_DIR = REPO_ROOT / "scripts" / "tools" / "lint"
TOOLS_SRC = REPO_ROOT / "scripts" / "tools"

sys.path.insert(0, str(LINT_DIR))
from _lint_helpers import (  # noqa: E402
    parse_build_sh_repo_data_files,
    parse_build_sh_tool_paths,
)

# `/opt/da-tools/x.py` -> parents are (/opt/da-tools, /opt, /). Three.
IMAGE_ANCESTOR_COUNT = 3


def _image_depth_bases() -> "list[Path]":
    """Candidate parents to build the image-depth directory under, best first.

    ⛔ The first version of this used ``Path(sys.executable).anchor`` only —
    i.e. it created its directory at the filesystem root. On Linux ``/`` is
    root-owned and a CI runner is not root, so ``mkdir`` raised EACCES and the
    whole module SKIPPED. `python-tests-run` (`ci.yml`) has no ``container:``
    and the pytest invocation carries no ``-rs``, so the skip was invisible and
    the job stayed green: this file's guard did not run on the platform it was
    written for. Measured with ``--user 1001:1001``: ``PermissionError``.

    ``$TMPDIR`` comes first because on POSIX it is ``/tmp`` — one component —
    so ``/tmp/<unique>/f.py`` has exactly the image's three ancestors AND is
    world-writable. Windows temp is many components deep and cannot be
    shortened, so there the anchor is still the only way to reach depth three.
    """
    bases = []
    tmp = Path(tempfile.gettempdir()).resolve()
    # A file in <tmp>/<unique> has len(tmp.parts) + 1 ancestors; only a
    # single-component tmp (POSIX /tmp) can land on the image's count.
    if len(tmp.parts) + 1 == IMAGE_ANCESTOR_COUNT:
        bases.append(tmp)
    bases.append(Path(Path(sys.executable).anchor))
    return bases


def _dir_with_image_ancestor_count() -> "tuple[Path, Path]":
    """``(leaf, cleanup_root)`` where a file in *leaf* has the image's depth.

    *cleanup_root* is the single directory this creates under its base, so one
    ``rmtree`` removes everything it made. Raises ``OSError`` if no candidate
    base is usable — the caller decides whether that is a skip or a failure.
    """
    last_error = None
    for base in _image_depth_bases():
        cleanup_root = base / f"vibe1494-{uuid.uuid4().hex[:8]}"
        leaf = cleanup_root
        while len((leaf / "probe.py").parents) < IMAGE_ANCESTOR_COUNT:
            leaf = leaf / "d"
        if len((leaf / "probe.py").parents) != IMAGE_ANCESTOR_COUNT:
            continue  # base is already deeper than the image; try the next
        try:
            leaf.mkdir(parents=True)
        except OSError as exc:
            last_error = exc
            continue
        return leaf, cleanup_root
    raise OSError(
        f"no writable base yields an image-depth path "
        f"(tried {[str(b) for b in _image_depth_bases()]}): {last_error}"
    )


# Imported by the child process below. Kept as a module-level constant so the
# assertion message can quote it verbatim.
_CHILD = r"""
import json, sys, importlib.util
payload = json.load(sys.stdin)
shipped = {name for name, _ in payload}
results = []
for name, path in payload:
    # Each entrypoint subcommand is its OWN process in the image, so a module
    # broken halfway through must not be left in sys.modules for the next one
    # to import from. Measured: without this reset, `_grar_validate` failing at
    # line 334 still satisfied `from _grar_validate import
    # find_ungated_equal_label_inhibits` (bound at line ~190, before the blow-up)
    # and byo_check reported CLEAN while the image would have died. The harness
    # under-reported, in the reassuring direction.
    for cached in list(sys.modules):
        if cached in shipped:
            del sys.modules[cached]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
        results.append([name, None, None])
    except BaseException as exc:
        missing = getattr(exc, "name", None) if isinstance(
            exc, ModuleNotFoundError) else None
        results.append([name, type(exc).__name__ + ": " + str(exc)[:200],
                        missing])
print(json.dumps(results))
"""


@pytest.fixture(scope="module")
def flat_import_results():
    """Copy the shipped set into an image-depth directory and import each one."""
    tool_paths = sorted(parse_build_sh_tool_paths())
    data_paths = sorted(parse_build_sh_repo_data_files())
    assert tool_paths, "TOOL_FILES parsed empty — the harness proves nothing"

    try:
        flat, cleanup_root = _dir_with_image_ancestor_count()
    except OSError as exc:
        # ⛔ fail, not skip, wherever a base SHOULD have worked. On POSIX
        # $TMPDIR always yields image depth and is world-writable, so an error
        # there means the gate is broken, not inapplicable — and a silent skip
        # is exactly how this file spent its first version not running in CI.
        if os.name == "posix":
            pytest.fail(
                f"no image-depth directory could be created on a POSIX host, "
                f"where $TMPDIR should always work: {exc}"
            )
        pytest.skip(f"no writable image-depth path on this host: {exc}")

    try:
        # Same flattening build.sh performs: destination is one directory.
        for rel in tool_paths:
            shutil.copy2(TOOLS_SRC / rel, flat / Path(rel).name)
        for rel in data_paths:
            shutil.copy2(REPO_ROOT / rel, flat / Path(rel).name)

        # ⚠️ build.sh additionally seds out the parent-dir `sys.path.insert`
        # line. Not replicated: re-implementing it here would be a second copy
        # of that rule, free to drift from the one in build.sh. Leaving the
        # line in is the conservative direction — the parent directory it adds
        # to sys.path is EMPTY (this harness creates the chain itself, so the
        # directory exists but holds nothing), which can only fail to resolve
        # something, never resolve something the image would not.
        # ⛔ An earlier version of this comment claimed the directory did not
        # exist. It does; the conclusion survives but the reasoning did not.
        payload = [
            [Path(rel).stem, str(flat / Path(rel).name)]
            for rel in tool_paths if rel.endswith(".py")
        ]
        env = dict(os.environ)
        env["PYTHONPATH"] = str(flat)
        proc = subprocess.run(
            [sys.executable, "-c", _CHILD],
            input=json.dumps(payload),
            capture_output=True, text=True, encoding="utf-8",
            cwd=str(flat), env=env, timeout=300,
        )
        assert proc.returncode == 0, (
            f"the import harness itself failed (rc={proc.returncode}).\n"
            f"stdout: {proc.stdout[-2000:]}\nstderr: {proc.stderr[-2000:]}"
        )
        yield json.loads(proc.stdout), {Path(p).name for p in tool_paths}
    finally:
        shutil.rmtree(cleanup_root, ignore_errors=True)


def test_every_shipped_module_imports_under_the_image_layout(
    flat_import_results,
):
    """No shipped module may fail to import when flattened.

    ⛔ A ``ModuleNotFoundError`` for a third-party distribution is NOT counted
    as a failure: the image ships a virtualenv (``COPY --from=builder
    /opt/venv``) that this test host does not have, so that class of error says
    something about the runner, not about the layout. Every other exception is
    fatal — ``IndexError`` from a depth assumption is exactly the shape #1494
    had. The environment-only skips are printed so the exemption can never
    quietly grow to cover a real failure.
    """
    results, shipped_names = flat_import_results

    env_only, real = [], []
    for name, error, missing in results:
        if error is None:
            continue
        # A missing module that IS part of the shipped set would be a real
        # packaging failure, so only genuinely external names are excused.
        if missing and f"{missing}.py" not in shipped_names:
            env_only.append((name, missing))
        else:
            real.append((name, error))

    print(f"\nimported {len(results)} shipped modules at image depth "
          f"({IMAGE_ANCESTOR_COUNT} ancestors); "
          f"{len(env_only)} skipped for missing third-party distributions: "
          f"{sorted({m for _, m in env_only})}")

    assert not real, (
        "shipped module(s) fail to import once the image flattens them — this "
        "is what the customer's `da-tools <cmd>` hits on the first line:\n"
        + "\n".join(f"  {n}: {e}" for n, e in real)
    )


def test_the_harness_would_notice_a_depth_assumption(flat_import_results):
    """Control: the same harness must FAIL on a deliberately broken module.

    ⛔ Without this, a harness that silently imported nothing (empty payload,
    wrong path, swallowed exception) would report the same clean pass as a
    healthy tree. The mutation is the exact shape this PR removed from
    ``_grar_validate.py``.
    """
    results, _ = flat_import_results
    assert results, "control cannot run: the harness imported zero modules"

    probe_dir, cleanup_root = _dir_with_image_ancestor_count()
    try:
        probe = probe_dir / "depth_probe.py"
        probe.write_text(
            "from pathlib import Path\n"
            "ROOT = Path(__file__).resolve().parents[3]\n",
            encoding="utf-8",
        )
        proc = subprocess.run(
            [sys.executable, "-c", _CHILD],
            input=json.dumps([["depth_probe", str(probe)]]),
            capture_output=True, text=True, encoding="utf-8", timeout=60,
        )
        assert proc.returncode == 0, proc.stderr[-2000:]
        (_, error, _), = json.loads(proc.stdout)
        assert error is not None and error.startswith("IndexError"), (
            "the control module was supposed to blow up at image depth but "
            f"reported {error!r} — the harness is not measuring what it claims"
        )
    finally:
        shutil.rmtree(cleanup_root, ignore_errors=True)
