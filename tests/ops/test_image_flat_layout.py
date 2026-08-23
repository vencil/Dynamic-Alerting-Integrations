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
import warnings
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


def _dir_with_image_ancestor_count() -> "tuple[Path, Path]":
    """``(leaf, cleanup_root)`` where a file in *leaf* has the image's depth.

    *cleanup_root* is the single directory this creates under its base, so one
    ``rmtree`` removes everything it made. Raises ``OSError`` if no candidate
    base is usable — the caller decides whether that is a skip or a failure.

    ⚠️ This deliberately does NOT vet the directory that lands on ``sys.path``.
    Two versions tried and both were wrong in opposite directions: "reject the
    base if it holds any ``*.py``" failed the whole module on POSIX whenever
    ``/tmp`` held an unrelated script, and "reject it if it holds a name the
    shipped set imports directly" missed 119 of the 166 names actually loaded.
    Shadowing is now DETECTED after the fact from module provenance in the
    child — see ``_CHILD`` — which needs no predicate at all.
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
        # ⚠️ Context for the reader, NOT a check: the leaf's parent lands on
        # sys.path for this run, because **16 lines across 13 shipped modules**
        # insert their own parent directory after build.sh's strip (10 spell it
        # with a literal `".."`, 6 with `.parent` / `parents[1]`; the sed only
        # matches the `_THIS_DIR` spelling). At image depth that parent must be
        # a shared directory — depth three leaves no room for a private one.
        # Whether anything there actually shadowed a module is decided AFTER the
        # imports, from provenance, in `_CHILD`.
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
import json, os, sys, importlib.util
payload = json.load(sys.stdin)
mods, flat_dir, shadow_dir = payload["modules"], payload["flat"], payload["shadow"]
shipped = {name for name, _ in mods}
results = []


# ⛔ Report what was ACTUALLY shadowed, do not predict what COULD be. The leaf's
# parent is on sys.path for this run and at image depth it is forced to be a
# shared directory (`/tmp`, or the drive root). Two predictive guards were tried
# and both were wrong in opposite directions: "any *.py at all" rejected a base
# for a stray file nobody imports (failing the module on POSIX), and "a *.py
# whose stem the shipped set imports directly" missed 119 of the 166 names this
# harness actually loads — `logging`, `ssl`, and the transitive `attr` / `idna` /
# `rpds` among them — i.e. it traded a false red for a false GREEN. Provenance
# needs no list: a module whose file lives under the shadow dir (but not under
# the staged dir) WAS shadowed, and a package directory or a .pyd is caught the
# same way a .py is.
def _under(p, root):
    p, root = os.path.abspath(p), os.path.abspath(root)
    return p == root or p.startswith(root + os.sep)


shadowed = {}


def _sweep():
    # ⛔ Sweep BEFORE each reset, not once at the end. The reset below deletes
    # every shipped name from sys.modules, so a shadow-dir file whose name is a
    # SHIPPED module's — the highest-risk case, since the tools import each
    # other — had its evidence deleted before the final sweep could see it, and
    # the run reported CLEAN. Accumulating across iterations keeps it.
    for n, m in list(sys.modules.items()):
        f = getattr(m, "__file__", None)
        if f and _under(f, shadow_dir) and not _under(f, flat_dir):
            shadowed[n] = f


for name, path in mods:
    _sweep()
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
_sweep()
sys.stdout.write("\n__FLAT_LAYOUT_RESULTS__" + json.dumps(
    {"results": results, "shadowed": sorted(shadowed.items())}))
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
        flat, cleanup_root = _dir_with_image_ancestor_count()
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
            input=json.dumps({"modules": payload, "flat": str(flat),
                              "shadow": str(Path(flat).parent)}),
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
        # ⛔ rpartition, not partition. The marker is a plain string; if a
        # shipped module happens to print it at import time, splitting on the
        # FIRST occurrence hands json the noise and reproduces the very
        # JSONDecodeError this framing exists to remove. The last occurrence is
        # always the child's own write.
        noise, _, payload_json = proc.stdout.rpartition(_RESULTS_MARKER)
        if noise.strip():
            # ⚠️ warnings, not print. CI runs pytest without `-s` and with
            # `-n auto`, so a passing test's stdout is captured and dropped —
            # this file argues that exact point about a print 30 lines below,
            # and a print here would have been invisible in the only place it
            # matters. Warnings survive both.
            warnings.warn(
                f"[flat-layout] the child wrote to stdout during import:\n"
                f"{noise.strip()[-2000:]}", stacklevel=1)
        parsed = json.loads(payload_json)
        # ⛔ Shadowing is judged here, from what the child actually loaded.
        assert not parsed["shadowed"], (
            "a module was imported from the directory that shares sys.path "
            "with the staged copies, so this run measured the wrong files:\n"
            + "\n".join(f"  {n} <- {f}" for n, f in parsed["shadowed"])
            + "\nDelete or rename those files. ⛔ Pointing TMPDIR elsewhere does "
              "NOT help: `_image_depth_bases` names `/tmp` directly on POSIX "
              "precisely because a deep TMPDIR would leave only the root-owned "
              "anchor, and any directory of your own has more than one path "
              "component so it cannot reach the image's ancestor count. An "
              "earlier version of this message advised exactly that, which "
              "would have sent the reader to an action measured not to work — "
              "and the cheapest remaining response is to delete this guard."
        )
        yield parsed["results"], {Path(p).name for p in tool_paths}
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



@pytest.mark.parametrize("plant,expect_shadowed,victim_name", [
    (None, False, None),                 # 控制組：shadow dir 乾淨
    ("module", True, "zzshadowcanary"),  # 種一個真的被 import 到的模組
    ("package", True, "zzshadowcanary"),  # ⛔ 套件目錄——`glob("*.py")` 看不到它
    ("unrelated", False, None),          # ⛔ 沒人 import 的檔案不得誤紅
    ("shipped_name", True, "victim"),    # ⛔ 名字在出貨集合內——最高風險那一格
])
def test_shadowing_is_detected_by_provenance_not_predicted(
    tmp_path, plant, expect_shadowed, victim_name
):
    """⛔ 兩個方向都要釘，因為這條檢查在兩個方向上各錯過一次。

    先前是「shadow dir 有任何 `*.py` 就拒絕這個 base」——`/tmp` 有個無關腳本
    就讓整個模組在 POSIX 紅（`unrelated` 那格）。改成「名字在出貨集合直接
    import 的清單裡才拒絕」之後，換成漏掉 166 個實際載入名字中的 119 個，
    `logging` / `ssl` / `attr` 全部在外（`module` 那格的方向）——**用誤紅換到了
    偽綠**。

    現在不預測、改成事後查 provenance：模組的 `__file__` 落在 shadow dir（且不
    在 staged dir）就是被遮蔽了。套件目錄與 `.pyd` 同樣涵蓋，因為判準是檔案位置
    而不是副檔名。
    """
    shadow = tmp_path / "shadow"
    flat = shadow / "d"
    flat.mkdir(parents=True)
    # ⛔ probe 自己把 parent dir 插到 sys.path[0] —— 這是出貨模組真的會做的事
    # （build.sh 的 sed 不剝的那 6 行 `.parent` 拼法），也是遮蔽得以發生的
    # 唯一機制。少了它，flat 永遠先命中，這個測試就什麼都沒測到。
    (flat / "probe_mod.py").write_text(
        'import os, sys\nsys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))\ntry:\n    import zzshadowcanary\nexcept ImportError:\n    pass\ntry:\n    import victim\nexcept ImportError:\n    pass\n', encoding="utf-8")
    if plant == "module":
        (shadow / "zzshadowcanary.py").write_text("V=1\n", encoding="utf-8")
    elif plant == "package":
        pkg = shadow / "zzshadowcanary"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("V=1\n", encoding="utf-8")
    elif plant == "unrelated":
        (shadow / "nobody_imports_this.py").write_text("V=1\n", encoding="utf-8")
    elif plant == "shipped_name":
        # ⛔ 最高風險的一格，也是先前靜默漏掉的那一格：被遮蔽者的名字**在出貨
        # 集合內**。`_CHILD` 每輪開頭會把所有出貨名字從 sys.modules 刪掉，所以
        # 只在最後掃一次的話，證據在被看見之前就被刪了——實測回報 CLEAN。
        # 出貨模組彼此互相 import，所以這正是最可能發生的形狀。
        (flat / "victim.py").write_text("SRC = 'flat'\n", encoding="utf-8")
        (shadow / "victim.py").write_text("SRC = 'shadow'\n", encoding="utf-8")

    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join([str(flat), str(shadow)])
    modules = [["probe_mod", str(flat / "probe_mod.py")]]
    if plant == "shipped_name":
        # victim 也在出貨集合裡 —— 這正是讓 `_CHILD` 的 reset 把證據刪掉的條件。
        modules.append(["victim", str(flat / "victim.py")])
    proc = subprocess.run(
        [sys.executable, "-c", _CHILD],
        input=json.dumps({"modules": modules,
                          "flat": str(flat), "shadow": str(shadow)}),
        capture_output=True, text=True, encoding="utf-8",
        env=env, cwd=str(flat), timeout=60,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert _RESULTS_MARKER in proc.stdout, proc.stdout[-2000:]
    parsed = json.loads(proc.stdout.rpartition(_RESULTS_MARKER)[2])
    got = bool(parsed["shadowed"])
    assert got is expect_shadowed, (
        f"plant={plant!r}: shadowed={parsed['shadowed']}, "
        f"results={parsed['results']}")
    if expect_shadowed:
        names = [n for n, _ in parsed["shadowed"]]
        assert victim_name in names, (victim_name, parsed["shadowed"])
        assert all(str(shadow) in path for _, path in parsed["shadowed"])

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
            input=json.dumps({"modules": [["depth_probe", str(probe)]],
                              "flat": str(probe_dir),
                              "shadow": str(probe_dir.parent)}),
            capture_output=True, text=True, encoding="utf-8", timeout=60,
        )
        assert proc.returncode == 0, proc.stderr[-2000:]
        assert _RESULTS_MARKER in proc.stdout, (
            f"the control child produced no result payload.\n"
            f"stdout: {proc.stdout[-2000:]}\nstderr: {proc.stderr[-2000:]}"
        )
        (_, error, _), = json.loads(
            proc.stdout.rpartition(_RESULTS_MARKER)[2])["results"]
        assert error is not None and error.startswith("IndexError"), (
            "the control module was supposed to blow up at image depth but "
            f"reported {error!r} — the harness is not measuring what it claims"
        )
    finally:
        shutil.rmtree(cleanup_root, ignore_errors=True)
