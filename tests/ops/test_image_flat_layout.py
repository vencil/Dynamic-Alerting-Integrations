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

import ast
import json
import os
import re
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

# build.sh's own sed, transcribed. Kept next to the assertion that it matched
# something, so a change on either side surfaces instead of silently making
# this harness less like the image.
_BUILD_SH_STRIP_RE = re.compile(
    r"""sys\.path\.insert.*os\.path\.join.*_THIS_DIR.*["']\.\.["']\)"""
)


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
    candidates = [Path(tempfile.gettempdir())]
    if os.name == "posix":
        # ⚠️ Not only $TMPDIR. A runner that exports a deep TMPDIR would leave
        # the anchor as the sole option, and the anchor is root-owned — the
        # exact combination that made this file silently skip in CI. `/tmp`
        # exists on every POSIX host and is world-writable, so name it
        # directly rather than trusting the environment to point at it.
        candidates.append(Path("/tmp"))
    for cand in candidates:
        cand = cand.resolve()
        # A file in <cand>/<unique> has len(cand.parts) + 1 ancestors; only a
        # single-component base (POSIX /tmp) lands on the image's count.
        if len(cand.parts) + 1 == IMAGE_ANCESTOR_COUNT and cand not in bases:
            bases.append(cand)
    bases.append(Path(Path(sys.executable).anchor))
    return bases


def _shadowable_names(tool_paths) -> "set[str]":
    """Module names a stray file next to the staged copies could shadow.

    ⛔ The set is derived, not listed: every top-level name the shipped tools
    import (plus the tools' own stems, since they import each other) — anything
    else in the same directory is simply never looked up, so rejecting a base
    for it is a false red. Parsed with ``ast`` rather than grepped, and a file
    that will not parse contributes nothing here because the import test itself
    is what reports that.
    """
    names: "set[str]" = set()
    for rel in tool_paths:
        src = TOOLS_SRC / rel
        names.add(Path(rel).stem)
        try:
            tree = ast.parse(src.read_text(encoding="utf-8-sig"))
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names.add(node.module.split(".")[0])
    return names


def _dir_with_image_ancestor_count(at_risk: "set[str]") -> "tuple[Path, Path]":
    """``(leaf, cleanup_root)`` where a file in *leaf* has the image's depth.

    *cleanup_root* is the single directory this creates under its base, so one
    ``rmtree`` removes everything it made. *at_risk* is the shadowable-name set
    from :func:`_shadowable_names`; a base is rejected only when the directory
    that lands on ``sys.path`` holds one of those names. Raises ``OSError`` if
    no candidate base is usable — the caller decides skip vs failure.
    """
    # ⛔ One reason PER BASE, not just the last one. Reporting only the final
    # failure names the anchor's EACCES even when the real story is that the
    # preferred base was rejected for a different cause entirely — a message
    # that sends the reader to fix the wrong thing.
    reasons: "list[str]" = []
    for base in _image_depth_bases():
        cleanup_root = base / f"vibe1494-{uuid.uuid4().hex[:8]}"
        leaf = cleanup_root
        while len((leaf / "probe.py").parents) < IMAGE_ANCESTOR_COUNT:
            leaf = leaf / "d"
        if len((leaf / "probe.py").parents) != IMAGE_ANCESTOR_COUNT:
            reasons.append(f"{base}: already deeper than the image layout")
            continue
        # ⛔ The leaf's PARENT is on sys.path during this run, so a stray
        # module sitting next to us would be imported instead of the real one.
        # At image depth that parent is forced to be a shared directory
        # (`/tmp`, or the drive root) — depth three leaves no room for a
        # private one — and **16 lines across 13 shipped modules** still insert
        # their parent directory after build.sh's strip (10 lines spell it with
        # literal `".."`, 6 with `.parent` / `parents[1]`; build.sh's sed only
        # matches the `_THIS_DIR` spelling).
        #
        # ⚠️ Two numbers here were wrong before and are corrected rather than
        # dropped: it said "ten shipped modules", which counted only the `".."`
        # family — the FIRST module that actually puts the parent on sys.path
        # in this harness is `_federation_revocation_reconciler`, a `parents[1]`
        # spelling that was outside that count. And "measured: 40+ modules
        # break" was measured BEFORE the strip above existed; with the strip,
        # a planted `yaml.py` is already shadowed too late to matter, so what
        # this rejection actually buys is protection for names imported AFTER
        # the first surviving parent-dir insert.
        # ⛔ Check the directory that actually lands on sys.path — the LEAF's
        # parent — not the base. They coincide on POSIX (`/tmp/<uuid>`), but on
        # Windows the base is the drive anchor and the leaf is one level deeper,
        # so checking `base` inspected `C:\` (which can never shadow anything)
        # while the directory that DOES go on sys.path went unchecked. Worse,
        # any stray `.py` at `C:\` then rejected every base and the module
        # SKIPPED — reopening, on the main development platform, exactly the
        # silent-skip hole this file was just fixed for.
        #
        # ⛔ Judge by CONSEQUENCE, not by appearance. Rejecting the base for ANY
        # `*.py` sitting there was an appearance test, and on POSIX it fails the
        # whole module: `/tmp` on a shared runner or a developer box very often
        # holds some unrelated script, the preferred base is then rejected, the
        # anchor fallback raises EACCES for a non-root user, and the fixture
        # calls `pytest.fail`. A stray file nobody imports would turn this file
        # red — the opposite error from the silent skip it was written to end,
        # and just as useless. Only a name the shipped set actually IMPORTS can
        # shadow anything, so that is the predicate.
        shadow_dir = leaf.parent
        intruders = (sorted(p.name for p in shadow_dir.glob("*.py")
                            if p.stem in at_risk)
                     if shadow_dir.exists() else [])
        if intruders:
            reasons.append(
                f"{shadow_dir}: contains {intruders[:5]}, whose name(s) the "
                f"shipped set imports — a module there would win through the "
                f"parent-dir sys.path entries those tools carry. Remove them "
                f"or point TMPDIR elsewhere.")
            continue
        try:
            leaf.mkdir(parents=True)
        except OSError as exc:
            reasons.append(f"{base}: {exc}")
            continue
        return leaf, cleanup_root
    raise OSError(
        "no usable base for an image-depth directory; each candidate was "
        "rejected for its own reason: " + " | ".join(reasons)
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
sys.stdout.write("\n__FLAT_LAYOUT_RESULTS__" + json.dumps(results))
"""

# ⛔ The child's payload must be locatable inside its stdout, not BE its stdout.
# A shipped module that prints anything at import time (a deprecation notice, a
# degraded-probe warning routed to stdout) made `json.loads(proc.stdout)` raise
# JSONDecodeError from inside the fixture — a message naming neither the module
# nor the text it printed. `returncode == 0` does not catch it, because the
# child still exits 0.
_RESULTS_MARKER = "__FLAT_LAYOUT_RESULTS__"


@pytest.fixture(scope="module")
def flat_import_results():
    """Copy the shipped set into an image-depth directory and import each one."""
    tool_paths = sorted(parse_build_sh_tool_paths())
    data_paths = sorted(parse_build_sh_repo_data_files())
    assert tool_paths, "TOOL_FILES parsed empty — the harness proves nothing"

    try:
        flat, cleanup_root = _dir_with_image_ancestor_count(
            _shadowable_names(tool_paths))
    except OSError as exc:
        # ⛔ fail, not skip, wherever a base SHOULD have worked. On POSIX
        # $TMPDIR always yields image depth and is world-writable, so an error
        # there means the gate is broken, not inapplicable — and a silent skip
        # is exactly how this file spent its first version not running in CI.
        if os.name == "posix":
            pytest.fail(
                f"no image-depth directory could be built on a POSIX host, "
                f"where /tmp normally provides one. Each candidate's own "
                f"reason is below — fix the one you care about rather than "
                f"the last one listed: {exc}"
            )
        pytest.skip(f"no writable image-depth path on this host: {exc}")

    try:
        # Same flattening build.sh performs: destination is one directory.
        for rel in tool_paths:
            shutil.copy2(TOOLS_SRC / rel, flat / Path(rel).name)
        for rel in data_paths:
            shutil.copy2(REPO_ROOT / rel, flat / Path(rel).name)

        # build.sh strips the repo-layout parent-dir `sys.path.insert` lines
        # from its copies, so the image does not carry them. Replicated here
        # with build.sh's own pattern, because NOT replicating it was wrong in
        # both directions: the harness diverged from the image, AND every line
        # left in inserts the leaf's parent — a SHARED directory at image
        # depth — onto sys.path.
        #
        # ⛔ Two earlier versions of this comment were false. The first said
        # the parent directory does not exist (it does). The second said it is
        # empty (it is `/tmp`, or the drive root). The pattern below is a
        # second copy of build.sh's rule and can drift from it, which is a real
        # cost — the assertion after it makes the drift visible rather than
        # silent: build.sh's sed matches only the `_THIS_DIR` spelling, so a
        # measured TEN shipped lines survive it and still run in the image.
        stripped = 0
        for copy in flat.glob("*.py"):
            text = copy.read_text(encoding="utf-8")
            kept = [ln for ln in text.splitlines(keepends=True)
                    if not _BUILD_SH_STRIP_RE.search(ln)]
            if len(kept) != len(text.splitlines(keepends=True)):
                stripped += 1
                copy.write_text("".join(kept), encoding="utf-8")
        assert stripped, (
            "the build.sh strip pattern matched nothing — either build.sh "
            "changed its sed and this copy did not follow, or the staging "
            "step is not copying what it thinks it is"
        )
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
        assert _RESULTS_MARKER in proc.stdout, (
            f"the child produced no result payload — it exited 0 without "
            f"reaching the final write.\nstdout: {proc.stdout[-2000:]}\n"
            f"stderr: {proc.stderr[-2000:]}"
        )
        noise, _, payload_json = proc.stdout.partition(_RESULTS_MARKER)
        if noise.strip():
            # Not a failure: import-time chatter is the tools' business. But it
            # must be visible, because it is exactly what used to corrupt the
            # payload and get reported as a parse error somewhere else.
            print(f"[flat-layout] import-time stdout from the child:\n"
                  f"{noise.strip()[-2000:]}")
        yield json.loads(payload_json), {Path(p).name for p in tool_paths}
    finally:
        shutil.rmtree(cleanup_root, ignore_errors=True)


def test_every_shipped_module_imports_under_the_image_layout(
    flat_import_results,
):
    """No shipped module may fail to import when flattened.

    ⛔ The third-party exemption is ASSERTED EMPTY, not merely reported.

    An earlier version excused any module whose failure was a
    ``ModuleNotFoundError`` for a name outside the shipped set, on the grounds
    that the image ships a virtualenv this host lacks — and printed the
    excused list "so the exemption can never quietly grow". Both halves were
    wrong. The exemption is per-MODULE, not per-line: Python stops at the
    first error, so a single ``import <third-party>`` ABOVE a depth assumption
    means the assumption never executes and the whole module is forgiven —
    measured, both guards green on a module that dies on its first line in the
    customer's image. And the print was invisible where it mattered: CI runs
    pytest without ``-s``, so passing-test stdout is captured and dropped.

    An assertion needs nobody to read it. The expected set is empty because
    every shipped module imports cleanly against the image's own dependency
    set; if that stops being true, this fails loudly and the exemption gets
    discussed rather than absorbed.
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

    assert not env_only, (
        "shipped module(s) could not be imported because a third-party "
        "distribution is missing here:\n"
        + "\n".join(f"  {n}: missing {m}" for n, m in env_only)
        + "\n⛔ This is not a free pass. Python stops at the first error, so "
        "everything below that import — including any layout assumption this "
        "test exists to catch — never ran, and the module is UNCHECKED. "
        "Either install the distribution where this test runs, or establish "
        "that the image does not ship this module."
    )

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

    # 這一格只放一個自己命名的 probe 模組，不 import 出貨集合，所以沒有任何
    # 名字會被遮蔽 —— 傳空集合即為它真正的風險面。
    probe_dir, cleanup_root = _dir_with_image_ancestor_count(set())
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
        assert _RESULTS_MARKER in proc.stdout, (
            f"the control child produced no result payload.\n"
            f"stdout: {proc.stdout[-2000:]}\nstderr: {proc.stderr[-2000:]}"
        )
        (_, error, _), = json.loads(
            proc.stdout.partition(_RESULTS_MARKER)[2])
        assert error is not None and error.startswith("IndexError"), (
            "the control module was supposed to blow up at image depth but "
            f"reported {error!r} — the harness is not measuring what it claims"
        )
    finally:
        shutil.rmtree(cleanup_root, ignore_errors=True)
