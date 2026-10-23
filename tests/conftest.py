"""Shared test fixtures and sys.path setup for da-tools test suite.

This conftest.py is loaded automatically by pytest before any test module.
It sets up sys.path so every test can ``import scaffold_tenant``, ``import
_lib_python``, ``import bump_docs``, etc. without per-file boilerplate.

Factory helpers are defined in ``tests/factories.py``.
"""
import os
import shutil
import sys
import tempfile

import pytest

# ── sys.path: scripts/tools + all subdirs ────────────────────────────
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TOOLS_DIR = os.path.join(REPO_ROOT, "scripts", "tools")

for _path in [
    _TOOLS_DIR,
    os.path.join(_TOOLS_DIR, "ops"),
    os.path.join(_TOOLS_DIR, "dx"),
    os.path.join(_TOOLS_DIR, "lint"),
]:
    if _path not in sys.path:
        sys.path.insert(0, _path)

# ── sys.path: da-tools entrypoint (for test_entrypoint.py) ──────────
_DA_TOOLS_DIR = os.path.join(
    REPO_ROOT, "components", "da-tools", "app",
)
if _DA_TOOLS_DIR not in sys.path:
    sys.path.insert(0, os.path.abspath(_DA_TOOLS_DIR))

# ── Local imports (factories helper) ──────────────────────────────────
from factories import populate_routing_dir  # noqa: E402
from _da_guard_build import go_build_env  # noqa: E402  (#3174: 與出貨同形，CGO_ENABLED=0)

# ── Runtime encoding gate (#2005): see tests/_encoding_gate.py ─────────
pytest_plugins = ["_encoding_gate"]


# ── Hypothesis: repo-wide settings profile ───────────────────────────
#
# Repo default: NO per-example deadline. A deadline is an OPT-IN tripwire
# a test may add for itself (see the rule at the bottom of this block).
#
# Why: Hypothesis's stock 200ms per-example deadline measures the HOST'S
# I/O SCHEDULER, not the property under test, for any example that touches
# the filesystem — and the FIRST example of a run additionally pays warm-up
# cost. When that first call crosses the budget but the replay does not,
# Hypothesis raises `FlakyFailure: ... Falsified on the first call but did
# not on a subsequent one`. That is a timing artefact, not a property
# violation.
#
# The giveaway: tests/ops/test_property_based.py::TestFileSha256::
# test_same_content_same_hash asserts "identical content produces an
# identical hash" — a property that CANNOT be falsified — yet it failed at
# 290.92ms against the 200ms deadline (replay: 2.11ms).
#
# Measured scope, stated honestly so nobody over-reads it:
#   - Reproduces on a Windows dev host, but only UNDER LOAD. It is not
#     reliably reproducible: a later idle sweep on the same host ran 14
#     consecutive green runs at the stock deadline. Treat it as a
#     load-dependent flake, NOT a fixed failure rate of the test.
#   - A Linux container stayed green over 5 quiet runs and 3 full `-n 16`
#     contention runs (7235 tests each) at the stock deadline, and recent
#     CI "Python Tests" failures were traced to unrelated causes.
# So this is a DX fix that also removes a LATENT CI risk, not a fix for an
# active CI fire. The latent risk is real: the module is NOT in the
# --ignore list of the job that actually runs pytest — `Python Tests —
# run (3.13)` (ci.yml; "Python Tests (3.13)" is the aggregation gate that
# runs no tests) — and that job uses `-n auto` with `-x`, so if the timing
# margin ever closes there, one trip reddens a required check.
#
# What this does NOT replace: CI's `pytest-timeout --timeout=300` is a
# per-test HANG backstop only. It is 1500x coarser than a per-example
# deadline and cannot catch the algorithmic regression a deadline is meant
# to catch — it is a different guarantee, not an equivalent substitute.
# `HealthCheck.too_slow` does still apply to tests that don't suppress it.
#
# THE RULE (apply per test, never per file):
#   - touches I/O            -> no deadline (this profile's default)
#   - pure function, and you
#     want a perf tripwire   -> opt in with an explicit
#                               `@settings(deadline=...)`, which OVERRIDES
#                               this profile.
# tests/shared/test_property_tools.py does exactly that: 79 pure-function
# properties opt into deadline=500, while its 3 filesystem-touching ones
# opt back out via PILOT_SETTINGS_IO — one of those was observed failing
# at 916.75ms against that same 500ms budget.
#
# Escape hatch: `HYPOTHESIS_PROFILE=strict` restores the stock 200ms.
# CAVEAT: it only affects tests with NO explicit deadline. Every property
# in test_property_tools.py sets one, so `strict` is a no-op there —
# don't read a green run under `strict` as "no timing problem".
#
# The import is guarded because several CI jobs run `pytest tests/...`
# with only `pip install pytest [pyyaml]` and no hypothesis — e.g. ci.yml's
# recipe-preview, promtool-goldens and vm-alert-parity steps,
# vm-anchor-on-pin-change.yml, nightly-vm-replay.yaml, and
# scripts/ops/federation_e2e_run.sh's venv. (Non-exhaustive: verify before
# relying on this list.) This conftest loads for those runs too, so an
# unguarded import would break their collection.
#
# The guard is deliberately narrow — it swallows ONLY "hypothesis itself
# is absent". A broken install (hypothesis present but raising ImportError
# from its own dependencies) propagates instead of silently leaving the
# stock deadline in place, which would be a fail-open.
try:
    from hypothesis import settings as _hypothesis_settings  # noqa: E402
except ModuleNotFoundError as _exc:  # pragma: no cover - env-dependent
    if _exc.name != "hypothesis":
        raise  # broken dependency, not "hypothesis not installed"
else:
    _hypothesis_settings.register_profile("vibe", deadline=None)
    _hypothesis_settings.register_profile("strict", deadline=200)
    # `or "vibe"`: an env var set-but-empty (common with `docker run -e VAR`
    # when VAR is unset on the host) must not brick collection. An unknown
    # name falls back with a warning rather than raising, because
    # load_profile() raising here aborts the ENTIRE tests/ tree with a
    # traceback pointing at conftest — and "ci"/"dev" are names a developer
    # may already export globally for an unrelated project.
    _profile = os.environ.get("HYPOTHESIS_PROFILE") or "vibe"
    if _profile not in ("vibe", "strict"):
        print(
            f"[conftest] unknown HYPOTHESIS_PROFILE={_profile!r}; "
            f"falling back to 'vibe' (valid: vibe, strict)",
            file=sys.stderr,
        )
        _profile = "vibe"
    _hypothesis_settings.load_profile(_profile)


# ── Session-scoped subprocess UTF-8 setup ────────────────────────────
#
# When tests use `subprocess.run([sys.executable, ...], capture_output=True,
# text=True, encoding='utf-8')`, the parent decodes child output as UTF-8
# but the CHILD Python's `sys.stdout.encoding` defaults to the host
# console codepage. On zh-TW Windows that's cp950 (Big5) and CJK
# characters in script output (e.g., `print("中文錯誤")`) become un-
# decodable bytes once they reach the parent's UTF-8 decoder, surfacing
# as `UnicodeDecodeError: 'utf-8' codec can't decode byte 0xa1 …`.
#
# Setting `PYTHONIOENCODING=utf-8` in the test process environment
# propagates to every spawned child Python (since `subprocess.run` without
# an explicit `env=` kwarg inherits the parent env). The child then
# emits UTF-8 bytes, matching what the parent expects.
#
# Linux CI runners default to UTF-8 anyway so this is a no-op there;
# the session scope means it runs once per pytest run, not per test.
@pytest.fixture(scope="session", autouse=True)
def _force_utf8_subprocess_io():
    """Force UTF-8 IO for subprocess'd Python children (cross-platform parity)."""
    old = os.environ.get("PYTHONIOENCODING")
    os.environ["PYTHONIOENCODING"] = "utf-8"
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("PYTHONIOENCODING", None)
        else:
            os.environ["PYTHONIOENCODING"] = old


@pytest.fixture(autouse=True)
def _no_ignored_signal_outlives_its_test():
    """A test that leaves SIGINT / SIGTERM at SIG_IGN fails (#3249): the
    disposition is the worker's, every later test's children inherit it, and
    a `killpg(SIGTERM)` there does nothing — a later test times out for a
    reason that says nothing about it. Only SIG_IGN: a Python handler is
    reset to default across exec, so it reaches no child. The disposition is
    put back either way, so the leak is reported once, where it happened."""
    import signal
    names = ("SIGINT", "SIGTERM")
    before = {n: signal.getsignal(getattr(signal, n)) for n in names}
    yield
    leaked = []
    for n in names:
        now = signal.getsignal(getattr(signal, n))
        if now is signal.SIG_IGN and before[n] is not signal.SIG_IGN:
            leaked.append(n)
            signal.signal(getattr(signal, n), before[n])
    if leaked:
        pytest.fail(f"left {', '.join(leaked)} at SIG_IGN for the rest of the "
                    "worker (#3249); restore it in the test (patch_config: "
                    "SIGNALS.reset())", pytrace=False)


@pytest.fixture(autouse=True)
def _disarm_crash_envelope():
    """A tool's ``main([... "--json"])`` called in-process arms the
    process-wide crash envelope (#3094) with whatever ``sys.stdout`` was
    then (a capture or fake stream); a tool run in-process as ``__main__``
    may set the process caller-error and crash codes (tenant_verify: 1);
    stdout write failures are recorded process-wide (#3124). None of it may
    outlive that test — a stale armed stream that fails turned a green run
    into rc 2 at interpreter exit, depending on test order. Idempotent
    reset of every piece, not save-then-restore (see test-map.md)."""
    yield
    ec = sys.modules.get("_lib_exitcodes")
    if ec is not None:
        ec.reset_exit_state()


@pytest.fixture
def unlistable_dir(monkeypatch):
    """`deny(root, target)`: from now on, every da-guard document (any mode)
    whose root is *target* or a directory above it says *target* cannot be
    listed — one entry, role `unlistable_dir`, class `directory`, problem
    `read_failure`, nothing under it ("." when the document is of *target*
    itself) — exactly the entry `parse-tree` writes for a walk error (#3206).

    For an in-process test of a Python reader: the walk is da-guard's, so a
    `chmod 000` is invisible to uid 0 and patching `os.scandir` no longer
    reaches it. What the walk itself does with an unlistable directory is
    pinned in Go (pkg/config role tests) and run for real as uid 65534
    (`tests/shared/test_lib_parse_tree_entries.py`); this stands in only for
    its verdict. The document is rewritten BEFORE the reader checks it, so
    the reader's own whole-document checks still run."""
    import json as _json
    import _lib_parse_tree as pt
    real = pt.read_document
    real_run = pt._run
    targets: list[str] = []

    def _run(args, binary, timeout, stdin=None, *, check=None, what=pt.SUBCOMMAND):
        out = real_run(args, binary, timeout, stdin, check=lambda d: None, what=what)
        if "--config-dir" in args and "--path" not in args:
            root = os.path.realpath(args[args.index("--config-dir") + 1])
            for target in targets:
                rel = os.path.relpath(target, root).replace(os.sep, "/")
                if rel == os.pardir or rel.startswith(os.pardir + "/"):
                    continue
                under = (lambda p, rel=rel: rel == "." or p == rel or p.startswith(rel + "/"))
                out["entries"] = [e for e in out["entries"] if not under(e["path"])]
                if "files" in out:
                    out["files"] = {f: c for f, c in out["files"].items() if not under(f)}
                    out["rendered_by"] = {f: c for f, c in out["rendered_by"].items()
                                          if not under(f)}
                out["defaults_carriers"] = {d: c for d, c in out["defaults_carriers"].items()
                                            if not under(c)}
                out["entries"].append({
                    "path": rel, "depth": 0 if rel == "." else rel.count("/"),
                    "class": "directory", "tenant_named": None, "defaults_document": False,
                    "config_extension": rel.rsplit("/", 1)[-1].lower().endswith((".yaml", ".yml")),
                    "readers": [], "not_read": None,
                    "role": "unlistable_dir",
                    "problem": {"kind": "read_failure",
                                "message": "the directory cannot be listed; nothing under "
                                           "it is read (walk_error)"},
                    "verdict": "unservable"})
                out["entries"].sort(key=lambda e: e["path"])
        return real(_json.dumps(out).encode("utf-8"), check=check, what=what)

    def deny(root, target):
        del root  # every document above *target* is rewritten, the root's too
        targets.append(os.path.realpath(target))
        monkeypatch.setattr(pt, "_run", _run)

    return deny


# ── Session-scoped constant fixtures ──────────────────────────────────

@pytest.fixture(scope="session")
def metric_dictionary():
    """metric-dictionary.yaml 完整內容（session scope，僅載入一次）。"""
    import yaml
    dict_path = os.path.join(REPO_ROOT, "scripts", "tools", "metric-dictionary.yaml")
    with open(dict_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


@pytest.fixture(scope="session")
def da_guard_binary(tmp_path_factory):
    """從本 repo 的 Go 原始碼建出的 da-guard（#2115）。

    讀取端改接 `da-guard served-values` 後，值來自 Go；建不起來（含沒有 go）
    一律 fail、不 skip——量不到 Go 的綠燈等於沒測。每個 xdist worker 建一次。
    """
    import subprocess
    built = os.environ.get(_SESSION_DA_GUARD_ENV, "")
    if built and os.path.isfile(built):
        return built  # pytest_configure built it from this checkout already
    go = shutil.which("go")
    if go is None:
        pytest.fail("`go` is not on PATH: da-guard cannot be built, so the readers that "
                    "take their values from it cannot be measured")
    out = tmp_path_factory.mktemp("da-guard-bin") / "da-guard"
    proc = subprocess.run(
        [go, "build", "-buildvcs=false", "-o", str(out), "./cmd/da-guard"],
        cwd=os.path.join(REPO_ROOT, "components", "threshold-exporter", "app"),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        check=False, timeout=600, env=go_build_env())
    if proc.returncode != 0:
        pytest.fail(f"go build da-guard failed (rc={proc.returncode}):\n{proc.stderr}")
    return str(out)


@pytest.fixture
def da_guard_env(monkeypatch, da_guard_binary):
    """`$DA_GUARD_BINARY` 指向 `da_guard_binary`，只在本測試期間（子行程繼承）。"""
    monkeypatch.setenv("DA_GUARD_BINARY", da_guard_binary)
    return da_guard_binary


@pytest.fixture
def judging_run():
    """The test runs as a judge (`_lib_parse_cache.judging`, #3206): the
    reader's fail-closed exit on a tree Go names paths on stands aside, as
    it does for `validate_config` and the route generator. For tests of the
    library mechanics those judges use on such trees (the unusable-entry
    wording, a partial served-values read); a tool's own behaviour on such a
    tree is the shared refusal (`test_confd_behaviour_matrix`)."""
    import _lib_parse_cache
    with _lib_parse_cache.judging():
        yield


@pytest.fixture(scope="session")
def da_crdecode_binary(tmp_path_factory):
    """從本 repo 的 Go 原始碼建出的 da-crdecode（#2476）。

    `da_assembler --render-cr` 以它解碼 CR；建不起來（含沒有 go）一律 fail、
    不 skip，理由同 `da_guard_binary`。每個 xdist worker 建一次。
    """
    import subprocess
    go = shutil.which("go")
    if go is None:
        pytest.fail("`go` is not on PATH: da-crdecode cannot be built, so "
                    "`da_assembler --render-cr` cannot be measured")
    out = tmp_path_factory.mktemp("da-crdecode-bin") / "da-crdecode"
    proc = subprocess.run(
        [go, "build", "-buildvcs=false", "-o", str(out), "./cmd/da-crdecode"],
        cwd=os.path.join(REPO_ROOT, "components", "threshold-exporter", "app"),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        check=False, timeout=600)
    if proc.returncode != 0:
        pytest.fail(f"go build da-crdecode failed (rc={proc.returncode}):\n{proc.stderr}")
    return str(out)


@pytest.fixture
def da_crdecode_env(monkeypatch, da_crdecode_binary):
    """`$DA_CRDECODE_BINARY` 指向 `da_crdecode_binary`，只在本測試期間。"""
    monkeypatch.setenv("DA_CRDECODE_BINARY", da_crdecode_binary)
    return da_crdecode_binary


# ── Function-scoped fixtures ──────────────────────────────────────────

@pytest.fixture
def config_dir():
    """Provide a temporary directory for config files, cleaned up after test."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


@pytest.fixture
def routing_dir():
    """預載兩個 tenant routing YAML 的暫存目錄。

    提供 db-a（webhook）+ db-b（slack）兩個 tenant 的標準 config，
    適用於 integration test 中 scaffold → generate_routes 管線。

    Yields:
        str — 含 db-a.yaml + db-b.yaml 的暫存目錄路徑。
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        populate_routing_dir(tmpdir)
        yield tmpdir


@pytest.fixture
def cli_argv(monkeypatch):
    """Replace `sys.argv` with the given args. Auto-restored after test.

    Convenience over the repeated
    `monkeypatch.setattr(sys, "argv", [...])` pattern (195 sites across
    the suite as of audit verification). Variadic API — pass the
    program name as the first positional arg, then flags + values:

        def test_x(cli_argv):
            cli_argv("script.py", "--input", str(tmp_path / "in.yaml"))
            ...

    Same semantics as monkeypatch.setattr — the fixture's monkeypatch
    dependency means the override is automatically reverted at end of
    test. Tests that ALSO patch other things keep their monkeypatch
    arg alongside cli_argv.
    """
    def _set(*args):
        monkeypatch.setattr(sys, "argv", list(args))
    return _set


@pytest.fixture
def amtool_required():
    """A test whose verdict comes from Alertmanager's own ``amtool`` (#2293).

    No ``amtool`` on PATH → skip, EXCEPT under ``VIBE_REQUIRE_AMTOOL=1`` (the
    CI Python Tests jobs, which install it): there a missing binary means the
    install step regressed, so the test FAILS instead of turning into a quiet
    skip — same fail-closed pattern as ``VIBE_REQUIRE_VECTOR``. Use it as
    ``pytest.mark.usefixtures("amtool_required")``.
    """
    if shutil.which("amtool") is None:
        if os.environ.get("VIBE_REQUIRE_AMTOOL") == "1":
            pytest.fail("VIBE_REQUIRE_AMTOOL=1 but `amtool` is not on PATH — "
                        "the CI 'Install amtool' step is missing or broke")
        pytest.skip("amtool not on PATH")


@pytest.fixture
def amtool_accepts(monkeypatch, tmp_path):
    """Put a fake ``amtool`` that accepts every config first on ``PATH``.

    #2311: validate-config's ``routes`` row reports WARN ("Not validated by
    Alertmanager") when no amtool is on PATH, as on a dev machine without it
    (the CI Python Tests jobs install a real one). A test whose point is "a
    healthy tree is all PASS" uses this so the assertion never depends on
    which amtool, if any, is installed. It sets the process ``PATH``, so an
    in-process ``shutil.which`` and a child process both see it; a test that
    builds its own child ``env`` must prepend the returned directory itself.
    Returns that directory.
    """
    bin_dir = tmp_path / "amtool-accepts-bin"
    bin_dir.mkdir()
    if sys.platform == "win32":
        (bin_dir / "amtool.bat").write_text("@echo SUCCESS\r\n@exit /b 0\r\n",
                                            encoding="utf-8")
    else:
        stub = bin_dir / "amtool"
        stub.write_text("#!/bin/sh\necho SUCCESS\nexit 0\n", encoding="utf-8")
        stub.chmod(0o755)
    monkeypatch.setenv("PATH",
                       str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    return bin_dir


@pytest.fixture
def patch_repo_root(monkeypatch, tmp_path):
    """Replace a tool module's repo-root constant with `tmp_path`.

    Returns a callable: `patch_repo_root(module, attr="REPO_ROOT") -> Path`.
    The returned Path is the same `tmp_path` so the caller can populate
    fixture data on it.

    Why this exists: 60+ tests across 13 files were repeating
    `monkeypatch.setattr(my_module, "REPO_ROOT", tmp_path)` (or `PROJECT_ROOT`,
    `CLAUDE_MD`, etc.) verbatim. The audit's TRK-242 sweep showed how easily
    one renamed constant breaks every test that hard-codes the attribute
    name; this fixture funnels them through one indirection.

    Usage:
        def test_foo(patch_repo_root):
            root = patch_repo_root(my_module)              # default REPO_ROOT
            (root / "docs").mkdir()
            ...

        def test_bar(patch_repo_root):
            root = patch_repo_root(my_module, "PROJECT_ROOT")   # explicit attr
            ...

        def test_file_path(patch_repo_root):
            root = patch_repo_root(my_module)
            claude_md = root / "CLAUDE.md"
            claude_md.write_text("...")
            patch_repo_root(my_module, "CLAUDE_MD", value=claude_md)
            ...
    """
    def _patch(module, attr: str = "REPO_ROOT", value=None):
        target = tmp_path if value is None else value
        monkeypatch.setattr(module, attr, target)
        return tmp_path
    return _patch
# ── Subprocess coverage (TRK-379) ────────────────────────────────────
#
# Much of this suite drives tools as `subprocess.run([sys.executable, tool,
# ...])`. Those children are invisible to coverage by default, so a module
# reached ONLY that way reports exactly like a module no test reaches at all:
# "could not measure" and "measured, nothing wrong" become the same reading.
#
# The mechanism is coverage's own `a1_coverage.pth`, which every interpreter
# runs at startup: it calls `coverage.process_startup()` when
# COVERAGE_PROCESS_START is set. Exporting it in the parent is therefore
# enough -- a hand-written sitecustomize.py (what #1746 originally prescribed)
# is NOT needed and would be a second copy of a mechanism coverage ships.
#
# There is no alternative route: pytest-cov 7.x removed its own subprocess
# support entirely (the package no longer contains the string COV_CORE), so
# COVERAGE_PROCESS_START is the only thing that makes these children visible.
#
# The coverage context the child measurement is tagged with. ⛔ The tool that
# reads it (scripts/tools/dx/list_subprocess_only_modules.py) holds the same
# string; test_the_child_config_carries_the_context_the_tool_reads binds the two.
SUBPROCESS_CONTEXT = "subprocess"


# ⛔ The child config is DERIVED FROM THE PARENT's live Coverage config, not
# re-read from pyproject.toml. Three CodeRabbit findings on #1885 shared this
# one root cause, and all three were reproduced before this rewrite:
#
#   * data_file — pytest-cov builds its parent Coverage in an earlier hook, so
#     setting COVERAGE_FILE here cannot move the parent. Forcing the child to
#     REPO_ROOT/.coverage while the parent honoured an externally-set
#     COVERAGE_FILE left the children orphaned: measured with
#     `COVERAGE_FILE=<elsewhere> pytest --cov`, the parent wrote 1 file and
#     **31 child data files piled up in the repo root**, never combined. Every
#     subprocess measurement silently vanished — the exact shape this wiring
#     exists to abolish.
#   * source — `coverage_gap_analysis.py` runs pytest with a source-VALUED
#     `--cov=<paths>`, which overrides coverage's own source list. A child built
#     from pyproject.toml would then measure a wider scope than the parent, so
#     the two sides of the context comparison would not be the same population.
#   * --no-cov — pytest-cov still registers `_cov` and sets `_disabled` when
#     `--no-cov` is passed, so keying only on "is the plugin registered" exported
#     the child config against an explicit request not to measure.
#
# Deriving from the parent closes all three at once and deletes the second copy
# of the configuration rather than keeping it in step by hand. `serialize()`
# absolutises paths, so a child started in another cwd still writes beside the
# parent.
def pytest_addoption(parser):
    parser.addoption(
        "--shard",
        default=None,
        metavar="K/N",
        help="Run only the tests tests/_shard.py assigns to shard K of N "
             "(1-based). CI's python-tests-run matrix uses it.",
    )


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config, items):
    """Keep only this shard's tests when ``--shard=K/N`` is given."""
    spec = config.getoption("--shard")
    # `is None`, not falsiness: an explicit `--shard=` is a malformed spec and
    # must be a usage error, not a silent full run.
    if spec is None:
        return
    from _shard import parse_shard_spec, shard_of

    try:
        k, n = parse_shard_spec(spec)
    except ValueError as exc:
        raise pytest.UsageError(str(exc)) from None
    keep, drop = [], []
    for item in items:
        (keep if shard_of(item.nodeid, n) == k else drop).append(item)
    if drop:
        config.hook.pytest_deselected(items=drop)
    items[:] = keep


_SESSION_DA_GUARD_ENV = "VIBE_TEST_DA_GUARD_BINARY"
_SESSION_DA_GUARD_DIR: "str | None" = None


def _provide_session_da_guard() -> None:
    """ADR-036 步驟 4（#2906）：路由產生器與經它讀 conf.d 的工具（validate-config、
    policy-engine、explain-route、notification-tester、check_routing_profiles、產生器
    容器）都經 `da-guard parse-tree` 讀 conf.d，沒有 da-guard 就是結束碼 2。呼叫它們
    的測試遍布各處，還有在收集期（模組層）就讀 conf.d 的，fixture 管不到——所以在
    pytest_configure 從本 checkout 建一次，設進 `$DA_GUARD_BINARY`（子行程與 xdist
    worker 都繼承）。沒有 go 就不設：要它的測試照樣以 da-guard 缺失失敗，不 skip。
    要量「沒有 da-guard」的測試自己 `monkeypatch.delenv`。"""
    global _SESSION_DA_GUARD_DIR
    built = os.environ.get(_SESSION_DA_GUARD_ENV, "")
    if built and os.path.isfile(built):  # an xdist worker: the controller built it
        os.environ["DA_GUARD_BINARY"] = built
        return
    import subprocess
    go = shutil.which("go")
    if go is None:
        return
    d = tempfile.mkdtemp(prefix="vibe-da-guard-")
    out = os.path.join(d, "da-guard")
    proc = subprocess.run(
        [go, "build", "-buildvcs=false", "-o", out, "./cmd/da-guard"],
        cwd=os.path.join(REPO_ROOT, "components", "threshold-exporter", "app"),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        check=False, timeout=600, env=go_build_env())
    if proc.returncode != 0:
        shutil.rmtree(d, ignore_errors=True)
        return  # da_guard_binary fails with go's message where it is asked for
    # #3178: tests that run as root drop to `nobody` (`setpriv --reuid=65534`)
    # so a chmod-000 directory really is unreadable, and the tool they run
    # reaches this da-guard. `mkdtemp` makes the directory 0700 and `go build`
    # follows the umask, so `nobody` could not execute it and the child died
    # with PermissionError before reading conf.d. Open both to read + execute
    # for every uid — never write: in /tmp that would let another uid swap
    # the binary the whole session executes.
    os.chmod(d, 0o755)
    os.chmod(out, 0o755)
    _SESSION_DA_GUARD_DIR = d
    os.environ[_SESSION_DA_GUARD_ENV] = out
    os.environ["DA_GUARD_BINARY"] = out


def pytest_unconfigure(config):
    if _SESSION_DA_GUARD_DIR is not None:
        shutil.rmtree(_SESSION_DA_GUARD_DIR, ignore_errors=True)


# ── A crash is not a named rc 2 (#3092) ──────────────────────────────
#
# A crash ends at rc 2 (#3090), like every named caller error, so a test that
# only checks `returncode == 2` stays green when the tool's own error handler
# is gone and it crashes instead. For the whole run the crash code is moved
# off 2: `_lib_exitcodes` and the da-tools entrypoint read
# DA_TEST_CRASH_EXIT_CODE at every crash and end there (production never
# sets it). Every child inherits it — and a child given its own `env=`
# gets it too, through the Popen shim below, unless that env names the
# variable itself.
#
# A test that pins the REAL crash code (2, or a tool's own) opts out with
# `@pytest.mark.real_crash_exit_code`: the variable is removed from
# os.environ for that test, and the shim then removes it from an explicit
# `env=` as well (a module-level `dict(os.environ)` copied it at import).
# Gate: tests/shared/test_crash_code_under_tests.py.
TEST_CRASH_EXIT_CODE_ENV = "DA_TEST_CRASH_EXIT_CODE"  # = _lib_exitcodes' (pinned)
TEST_CRASH_EXIT_CODE = "70"
_REAL_CRASH_MARKER = "real_crash_exit_code"


def _with_test_crash_exit_code(env):
    """*env* as the child should get it — a new mapping when it changes, the
    caller's is never touched: plus DA_TEST_CRASH_EXIT_CODE from os.environ
    unless *env* names it; without it when the test opted out (os.environ
    has none)."""
    value = os.environ.get(TEST_CRASH_EXIT_CODE_ENV)
    as_bytes = any(isinstance(k, bytes) for k in env)
    key = TEST_CRASH_EXIT_CODE_ENV.encode() if as_bytes else TEST_CRASH_EXIT_CODE_ENV
    if value is None:
        if key not in env:
            return env
        return {k: v for k, v in env.items() if k != key}
    if key in env:
        return env
    out = dict(env)
    out[key] = value.encode() if as_bytes else value
    return out


# Popen(args, bufsize, executable, stdin, stdout, stderr, preexec_fn,
# close_fds, shell, cwd, env, ...): env's position, for a positional call.
_POPEN_ENV_INDEX = 10


def _install_popen_env_shim():
    """Wrap ``subprocess.Popen.__init__`` (once per process) so an explicit
    ``env=`` carries the test crash code. ``__init__``, not the name
    ``subprocess.Popen``: ``subprocess.run`` / ``check_output`` and a module
    that did ``from subprocess import Popen`` all end up there."""
    import functools
    import subprocess
    inner = subprocess.Popen.__init__
    if getattr(inner, "_da_test_crash_exit_code_shim", False):
        return

    @functools.wraps(inner)
    def __init__(self, *args, **kwargs):
        if kwargs.get("env") is not None:
            kwargs["env"] = _with_test_crash_exit_code(kwargs["env"])
        elif len(args) > _POPEN_ENV_INDEX and args[_POPEN_ENV_INDEX] is not None:
            args = (*args[:_POPEN_ENV_INDEX],
                    _with_test_crash_exit_code(args[_POPEN_ENV_INDEX]),
                    *args[_POPEN_ENV_INDEX + 1:])
        return inner(self, *args, **kwargs)

    __init__._da_test_crash_exit_code_shim = True
    subprocess.Popen.__init__ = __init__


def _arm_test_crash_exit_code():
    # The marker itself is registered in pyproject.toml with the others.
    os.environ[TEST_CRASH_EXIT_CODE_ENV] = TEST_CRASH_EXIT_CODE
    _install_popen_env_shim()


@pytest.fixture(autouse=True)
def _real_crash_exit_code(request, monkeypatch):
    """`@pytest.mark.real_crash_exit_code`: no test crash code for this test
    (os.environ, so its children and the Popen shim see none either)."""
    if request.node.get_closest_marker(_REAL_CRASH_MARKER) is not None:
        monkeypatch.delenv(TEST_CRASH_EXIT_CODE_ENV, raising=False)


def pytest_configure(config):
    """Build the session's da-guard (`_provide_session_da_guard`); move the
    crash code off 2 (`_arm_test_crash_exit_code`); make subprocess-invoked
    tools visible to coverage, when coverage is on."""
    _provide_session_da_guard()
    _arm_test_crash_exit_code()
    plugin = config.pluginmanager.get_plugin("_cov")
    if plugin is None or getattr(plugin, "_disabled", False):
        return
    parent = getattr(getattr(plugin, "cov_controller", None), "cov", None)
    if parent is None:          # coverage plugin present but never started
        return

    from coverage.config import CoverageConfig

    # ⚠️ Copy via serialize/deserialize: mutating `parent.config` would change
    # what the PARENT measures. `deserialize` is a classmethod returning a new
    # config, not an in-place load.
    child = CoverageConfig.deserialize(parent.config.serialize())
    child.context = SUBPROCESS_CONTEXT
    os.environ["COVERAGE_PROCESS_CONFIG"] = child.serialize()
    os.environ.pop("COVERAGE_PROCESS_START", None)
