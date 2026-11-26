"""Tests for scripts/ops/inject_default_key.py.

Use case: bench harness orchestrator's idempotent `bench_trigger=50`
injection into `_defaults.yaml` (Track A A6 helper, replaces the inline
Python in bench_e2e_run.sh).

Coverage focuses on:
  - Numeric guard (cycle-6 RCA contract)
  - Idempotence (re-run after generator already wrote the key)
  - Three-way file state (exists+has-key / exists+no-key / missing)
  - main() exit-code contract
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
TOOL_PATH = REPO_ROOT / "scripts" / "ops" / "inject_default_key.py"


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location("inject_default_key", TOOL_PATH)
    assert spec is not None and spec.loader is not None
    m = importlib.util.module_from_spec(spec)
    sys.modules["inject_default_key"] = m
    spec.loader.exec_module(m)
    return m


def test_inject_into_existing_defaults_block(mod, tmp_path):
    """Standard generator-output shape: `defaults:` block exists, key absent."""
    p = tmp_path / "_defaults.yaml"
    p.write_text("defaults:\n  mysql_connections: 80\n", encoding="utf-8")
    rc = mod.inject(p, "bench_trigger", "50")
    assert rc == 0
    text = p.read_text(encoding="utf-8")
    assert "bench_trigger: 50" in text
    assert "mysql_connections: 80" in text  # original preserved


def test_inject_idempotent_when_key_already_present(mod, tmp_path):
    """Re-run safe: key already there → no change, exit 0."""
    p = tmp_path / "_defaults.yaml"
    p.write_text(
        "defaults:\n  mysql_connections: 80\n  bench_trigger: 50\n",
        encoding="utf-8",
    )
    before = p.read_text(encoding="utf-8")
    rc = mod.inject(p, "bench_trigger", "50")
    assert rc == 0
    assert p.read_text(encoding="utf-8") == before  # bytewise identical


def test_inject_creates_file_when_missing(mod, tmp_path):
    """Customer-anon fixture without `_defaults.yaml` → create one."""
    p = tmp_path / "subdir" / "_defaults.yaml"  # nested missing
    rc = mod.inject(p, "bench_trigger", "50")
    assert rc == 0
    assert p.exists()
    text = p.read_text(encoding="utf-8")
    assert text.startswith("defaults:")
    assert "bench_trigger: 50" in text


def test_inject_creates_defaults_block_in_existing_yaml_without_one(mod, tmp_path):
    """File has `tenants:` etc. but no `defaults:` block → prepend one."""
    p = tmp_path / "_defaults.yaml"
    p.write_text("# Some other top-level\ntenants: {}\n", encoding="utf-8")
    rc = mod.inject(p, "bench_trigger", "50")
    assert rc == 0
    text = p.read_text(encoding="utf-8")
    assert text.startswith("defaults:")
    assert "  bench_trigger: 50" in text
    assert "tenants: {}" in text  # original preserved


def test_inject_rejects_non_numeric(mod, tmp_path):
    """Cycle-6 contract: non-numeric values reject."""
    p = tmp_path / "_defaults.yaml"
    p.write_text("defaults:\n", encoding="utf-8")
    rc = mod.inject(p, "bench_trigger", "X:critical")
    assert rc == 1
    # File untouched
    assert p.read_text(encoding="utf-8") == "defaults:\n"


@pytest.mark.parametrize("value", ["1_000", "0x1F", "0x1p4", "nan", "inf", "-Infinity", "1e999",
                                   "123456789012345678901", "١٠", "\x1c5", ""])
def test_inject_rejects_what_the_exporter_serves_no_default_for(mod, tmp_path, value):
    """#3457: the numeric guard is the exporter's grammar of a threshold
    number. `int()` / `float()` read `1_000`, `nan`, `inf` and non-ASCII
    digits; written under `defaults:`, the exporter serves no default for
    any of them."""
    p = tmp_path / "_defaults.yaml"
    p.write_text("defaults:\n", encoding="utf-8")
    assert mod.inject(p, "bench_trigger", value) == 1
    assert p.read_text(encoding="utf-8") == "defaults:\n"


@pytest.mark.parametrize("value, written", [("50", "50"), (" 50 ", "50"), ("\t1e3\u0085", "1e3"),
                                            ("+.5", "+.5")])
def test_inject_writes_the_value_trimmed_as_the_exporter_trims(mod, tmp_path, value, written):
    p = tmp_path / "_defaults.yaml"
    p.write_text("defaults:\n", encoding="utf-8")
    assert mod.inject(p, "bench_trigger", value) == 0
    assert p.read_text(encoding="utf-8") == f"defaults:\n  bench_trigger: {written}\n"


def test_inject_accepts_int_string(mod, tmp_path):
    p = tmp_path / "_defaults.yaml"
    p.write_text("defaults:\n", encoding="utf-8")
    assert mod.inject(p, "k", "42") == 0


def test_inject_accepts_float_string(mod, tmp_path):
    p = tmp_path / "_defaults.yaml"
    p.write_text("defaults:\n", encoding="utf-8")
    assert mod.inject(p, "k", "1.5") == 0
    assert "k: 1.5" in p.read_text(encoding="utf-8")


def test_main_usage_error_returns_1(mod, capsys):
    rc = mod.main(["inject_default_key.py"])
    assert rc == 1
    assert "usage:" in capsys.readouterr().err


def test_main_too_many_args_returns_1(mod, capsys):
    rc = mod.main(["inject_default_key.py", "a", "b", "c", "d"])
    assert rc == 1


def test_main_happy_path_returns_0(mod, tmp_path):
    p = tmp_path / "_defaults.yaml"
    p.write_text("defaults:\n", encoding="utf-8")
    rc = mod.main(
        ["inject_default_key.py", str(p), "bench_trigger", "50"]
    )
    assert rc == 0



def test_the_command_line_reaches_main(tmp_path):
    """#3579: the process enters through `_lib_exitcodes.run(_cli)`, which
    calls with no arguments; `_cli` hands `main` the command line."""
    import subprocess
    p = tmp_path / "_defaults.yaml"
    p.write_text("defaults:\n", encoding="utf-8")
    proc = subprocess.run([sys.executable, str(TOOL_PATH), str(p), "bench_trigger", "50"],
                          capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert "bench_trigger: 50" in p.read_text(encoding="utf-8")

# ── The inert-shape refusal (#1412) ─────────────────────────────────────────
#
# ⛔ A different question from the numeric guard above, and the reason both
# exist: `mysql_connections_critical: 90` passes every check this tool had. It
# is numeric, it is a valid YAML key, the file still parses — and under
# `defaults:` the exporter never looks at it (`resolveCriticalRows` reads the
# critical tier from tenant overrides keyed on `defaults[<base>]`;
# a dimensional key needs a `dimensions:` declaration this tool does not
# write, #3475). No parse error, no WARN, no series.
#
# This tool is also why the refusal lives HERE rather than in a downstream gate:
# it writes into fixture trees that no `check_threshold_reachability` face reads,
# so it was one of the two producers #1412 found invisible. The census in that
# gate exempts this module BY NAMING this refusal — so if these tests go, the
# exemption is a claim with nothing behind it.
@pytest.mark.parametrize("key", [
    "mysql_connections_critical",
    "pg_replication_lag_critical",
    "es_disk_usage_percent{index=main}",
])
def test_inert_default_shapes_are_refused(mod, tmp_path, key, capsys):
    p = tmp_path / "_defaults.yaml"
    before = "defaults:\n  mysql_connections: 80\n"
    p.write_text(before, encoding="utf-8")

    rc = mod.inject(p, key, "90")

    assert rc == 1, f"{key!r} was accepted under `defaults:`"
    # ⛔ The file is UNCHANGED. A refusal that still wrote would be worse than no
    # refusal: the operator reads an error and the inert key ships anyway.
    assert p.read_text(encoding="utf-8") == before
    err = capsys.readouterr().err
    assert "fails SILENTLY" in err
    assert "<tenant>.yaml" in err, (
        "the refusal must say where the tier DOES belong; an error that only "
        "says no leaves the operator to guess"
    )


def test_the_base_key_of_a_refused_tier_is_still_accepted(mod, tmp_path):
    """The refusal is about the SHAPE, not about the metric.

    Without this cell a tool that refused every key would satisfy the test above
    — the "must-fire" control has to be paired with a "must-not-fire" one.
    """
    p = tmp_path / "_defaults.yaml"
    p.write_text("defaults:\n  mysql_connections: 80\n", encoding="utf-8")
    assert mod.inject(p, "pg_replication_lag", "30") == 0
    assert "pg_replication_lag: 30" in p.read_text(encoding="utf-8")


# ── #3981: the write goes through the artifact entry ────────────────────────

def _run_cli(*args: str, cwd: Path, fsize: int | None = None):
    """The tool as a process; with *fsize*, a file-size limit (bytes) and
    SIGXFSZ ignored, so an oversized write fails with EFBIG."""
    import os
    import signal
    import subprocess

    def limit():
        import resource
        signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
        resource.setrlimit(resource.RLIMIT_FSIZE, (fsize, fsize))

    return subprocess.run(
        [sys.executable, str(TOOL_PATH), *args], capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=60,
        preexec_fn=limit if fsize is not None else None, cwd=cwd,
        env={**os.environ, "PYTHONUTF8": "1"})


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX resource limits")
def test_an_existing_file_that_cannot_be_replaced_is_rc2_one_line(tmp_path):
    """The rewrite of an existing `_defaults.yaml` that lacks the key fails
    (here: the file-size limit) → rc 2, one line naming `PATH`, and the
    file keeps its bytes: the replacement is atomic."""
    target = tmp_path / "_defaults.yaml"
    before = "defaults:\n  a: 1\n"
    target.write_text(before, encoding="utf-8")
    proc = _run_cli(str(target), "bench_trigger", "50", cwd=tmp_path, fsize=4)
    assert proc.returncode == 2, proc.stderr
    lines = [ln for ln in proc.stderr.splitlines() if ln.strip()]
    assert len(lines) == 1 and lines[0].startswith("ERROR: "), proc.stderr
    assert "PATH" in lines[0]
    assert target.read_text(encoding="utf-8") == before


@pytest.mark.skipif(sys.platform == "win32", reason="no mkfifo")
def test_a_fifo_target_is_refused_before_it_is_read(tmp_path):
    """A FIFO used to block the read forever: refused, rc 2, naming `PATH`."""
    import os
    fifo = tmp_path / "_defaults.yaml"
    os.mkfifo(fifo)
    proc = _run_cli(str(fifo), "bench_trigger", "50", cwd=tmp_path)
    assert proc.returncode == 2, proc.stderr
    assert "PATH" in proc.stderr and "Traceback" not in proc.stderr


def test_standard_output_is_refused(tmp_path):
    """`-` used to create a file named `-` in the cwd."""
    proc = _run_cli("-", "bench_trigger", "50", cwd=tmp_path)
    assert proc.returncode == 2, proc.stderr
    assert "PATH" in proc.stderr
    assert not (tmp_path / "-").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_a_rewritten_file_is_0644_as_conf_d_files_are(tmp_path):
    """#3535: a conf.d file is a repo artifact, 0644 — the exporter reads the
    tree as another uid, so an operator output's 0600 would hide it."""
    target = tmp_path / "_defaults.yaml"
    target.write_text("defaults:\n  a: 1\n", encoding="utf-8")
    target.chmod(0o600)
    proc = _run_cli(str(target), "bench_trigger", "50", cwd=tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert oct(target.stat().st_mode & 0o777) == oct(0o644)
