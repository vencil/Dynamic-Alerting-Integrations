"""Unwritable output path ⇒ rc=2 + one line naming the flag, for every argv-path tool (#1641).

THE CONTRACT
------------
A tool whose output path comes from argv (``-o`` / ``--output`` /
``--output-dir`` / ``--out`` / ``--markdown-output`` / …) and cannot write
there must:

* exit ``EXIT_CALLER_ERROR`` (2) — the exit-code SSOT's "IO failure" cell —
  and NOT ``EXIT_VIOLATION`` (1), which a CI consumer reads as "your config
  has a finding";
* print ONE ``ERROR:`` line to stderr that names the path AND the flag the
  operator should check;
* print NO traceback.

Every row below is a CONTROL PAIR (ticket §建議做法 3): the same argv with a
writable path must exit 0 and actually produce the output. Without the
control, an implementation that exits 2 whenever ``-o`` is present would pass
the bad-path half and never be caught.

HOW THE BAD PATH IS MADE (root-safe)
------------------------------------
Two shapes, both failing for uid 0 as well as for a normal user:

* ``missing_parent`` — ``<tmp>/absent/<out>``: the parent directory does not
  exist, so ``open()`` fails ENOENT. Only meaningful for tools that write a
  FILE at the given path; a ``--output-dir`` tool would ``mkdir -p`` the
  parent and succeed, which is correct behaviour, not a failure.
* ``parent_is_file`` — ``<tmp>/blocker/<out>`` where ``blocker`` is a regular
  file: ``open()`` and ``mkdir -p`` both fail ENOTDIR. This is the shape that
  exercises the tool's own ``mkdir`` (routed through ``ensure_dir_or_die``),
  i.e. the failure that happens BEFORE the first secure write.

Permission bits are deliberately not used: CI and the dev container run as
root, where ``chmod 0o500`` is ignored and the test would pass vacuously.

REACHABILITY
------------
Every row reaches its write offline. Tools that need Prometheus get the
in-process stub server from ``test_json_stdout_contract`` (the same one the
``--json`` and ``--dry-run`` gates use); ``generate_changelog`` gets a
throw-away git repository with one conventional commit.

STALENESS GUARD
---------------
``test_every_flag_naming_tool_has_a_row`` derives, from the tree, the set of
tool modules that pass ``flag=`` to a secure writer / ``ensure_dir`` and
asserts each has a row here. A tool that grows an output flag and names it
in the writer without adding a row goes red.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import pytest

import test_json_stdout_contract as _jsc  # noqa: E402  (tests/shared on sys.path)

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS_DIR = REPO_ROOT / "scripts" / "tools"
WAVEFORM_FIXTURE = REPO_ROOT / "tests" / "dx" / "fixtures" / "waveform" / "selftest_disk_used_percent.yaml"

TIMEOUT_S = 120
EXIT_CALLER_ERROR = 2


# ═══════════════════════════════════════════════════════════════════════════
# Stub server (Prometheus personality) — session scoped, same handler as the
# --json / --dry-run gates.
# ═══════════════════════════════════════════════════════════════════════════
@pytest.fixture(scope="session")
def stub_url():
    from http.server import ThreadingHTTPServer
    import threading

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _jsc._StubHandler)
    srv.daemon_threads = True
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()


# ═══════════════════════════════════════════════════════════════════════════
# Minimal fixtures (built under tmp_path per row)
# ═══════════════════════════════════════════════════════════════════════════
def _legacy_rules(tmp: Path) -> str:
    p = tmp / "legacy-rules.yaml"
    p.write_text(
        "groups:\n"
        "  - name: legacy\n"
        "    rules:\n"
        "      - alert: HighConnectionCount\n"
        "        expr: mysql_global_status_threads_connected > 100\n"
        "        for: 5m\n"
        "        labels:\n"
        "          severity: warning\n"
        "        annotations:\n"
        "          summary: connection count high\n",
        encoding="utf-8")
    return str(p)


def _glossary(tmp: Path) -> str:
    p = tmp / "glossary.md"
    p.write_text(
        "# Glossary\n\n"
        "**ADR (Architecture Decision Record)** — a design decision.\n\n"
        "**BYO (Bring Your Own)** — customer-supplied component.\n",
        encoding="utf-8")
    return str(p)


def _mapping_conf_d(tmp: Path) -> str:
    d = tmp / "conf.d"
    d.mkdir()
    (d / "_instance_mapping.yaml").write_text(
        "instance_tenant_mapping:\n"
        "  'stub:9104':\n"
        "    - tenant: alpha\n"
        "      filter: 'schema=\"app\"'\n"
        "    - tenant: beta\n"
        "      filter: 'schema=\"billing\"'\n",
        encoding="utf-8")
    return str(d)


def _one_rule_pack_dir(tmp: Path) -> str:
    d = tmp / "rule-packs"
    d.mkdir()
    shutil.copy(_jsc.RULE_PACKS / "rule-pack-mariadb.yaml", d / "rule-pack-mariadb.yaml")
    return str(d)


def _git_repo_with_one_commit(tmp: Path) -> Path:
    repo = tmp / "repo"
    repo.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
           "GIT_CONFIG_NOSYSTEM": "1"}
    for argv in (["git", "init", "-q"],
                 ["git", "-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty",
                  "-m", "feat: alpha threshold"]):
        subprocess.run(argv, cwd=repo, env=env, check=True, capture_output=True,
                       timeout=60)
    return repo


# ═══════════════════════════════════════════════════════════════════════════
# Row table
# ═══════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class Ctx:
    tmp: Path
    stub: str


@dataclass(frozen=True)
class Row:
    tool: str                                   # relative to scripts/tools
    flag: str                                   # must appear in stderr verbatim
    kind: str                                   # "file" | "dir"
    build: Callable[[Ctx, Path], list[str]]     # (ctx, out_path) -> argv
    reach: str                                  # how the write is reached offline
    out_name: str = "out"
    cwd: Callable[[Ctx], Path] | None = None    # subprocess cwd, if any
    # Bad-path shapes to drive. A FILE tool whose own mkdir -p creates the
    # parent (sync_glossary_abbr) legitimately succeeds on missing_parent, so
    # it lists parent_is_file only.
    shapes: tuple[str, ...] = ("missing_parent", "parent_is_file")

    @property
    def id(self) -> str:
        return f"{Path(self.tool).stem}[{self.flag}]"


R = Row
ROWS: list[Row] = [
    # ── dx ─────────────────────────────────────────────────────────────────
    R("dx/generate_changelog.py", "-o/--output", "file",
      lambda c, out: ["-o", str(out)],
      reach="throw-away git repo with one conventional commit (cwd)",
      out_name="CHANGELOG-draft.md",
      cwd=lambda c: _git_repo_with_one_commit(c.tmp)),
    R("dx/sync_glossary_abbr.py", "--output", "file",
      lambda c, out: ["--glossary", _glossary(c.tmp), "--output", str(out)],
      reach="tmp glossary.md with two **ABBR (Expansion)** entries",
      out_name="abbreviations.md",
      shapes=("parent_is_file",)),
    R("dx/waveform_compile.py", "--out", "dir",
      lambda c, out: ["--compile", "--out", str(out), "--allow-selftest",
                      str(WAVEFORM_FIXTURE)],
      reach="in-repo self-test waveform pack fixture",
      shapes=("parent_is_file",)),
    # ── ops: single-file outputs ───────────────────────────────────────────
    R("ops/analyze_rule_pack_gaps.py", "-o/--output", "file",
      lambda c, out: ["--config-dir", str(_jsc.SEED_CONF_D), "--output", str(out)],
      reach="in-repo try-local seed conf.d", out_name="gaps.json"),
    R("ops/backtest_threshold.py", "-o/--output", "file",
      lambda c, out: ["--tenant", "alpha", "--metric", "max_connections",
                      "--old-value", "100", "--new-value", "150",
                      "--prometheus", c.stub, "--output", str(out)],
      reach="local Prometheus stub (empty range vector)", out_name="backtest.json"),
    R("ops/backtest_threshold.py", "--markdown-output", "file",
      lambda c, out: ["--tenant", "alpha", "--metric", "max_connections",
                      "--old-value", "100", "--new-value", "150",
                      "--prometheus", c.stub, "--markdown-output", str(out)],
      reach="local Prometheus stub (empty range vector)", out_name="backtest.md"),
    R("ops/batch_diagnose.py", "-o/--output", "file",
      lambda c, out: ["--tenants", "alpha,beta", "--prometheus", c.stub,
                      "--output", str(out)],
      reach="local Prometheus stub", out_name="health.json"),
    R("ops/discover_instance_mappings.py", "-o/--output", "file",
      lambda c, out: ["--prometheus", c.stub + "/rich", "--instance", "stub:9104",
                      "--output", str(out)],
      reach="local Prometheus stub (/rich personality carries schema=)",
      out_name="mapping.yaml"),
    R("ops/generate_tenant_mapping_rules.py", "-o/--output", "file",
      lambda c, out: ["--config-dir", _mapping_conf_d(c.tmp), "--metrics", "mysql_up",
                      "-o", str(out)],
      reach="tmp conf.d with a two-tenant _instance_mapping.yaml",
      out_name="mapping-rules.yaml"),
    # ── ops: output directories ────────────────────────────────────────────
    R("ops/init_project.py", "-o/--output-dir", "dir",
      lambda c, out: ["-o", str(out), "--non-interactive", "--tenants", "alpha",
                      "--rule-packs", "mariadb"],
      reach="non-interactive flags only",
      shapes=("parent_is_file",)),
    R("ops/migrate_rule.py", "-o/--output-dir", "dir",
      lambda c, out: [_legacy_rules(c.tmp), "-o", str(out)],
      reach="tmp legacy Prometheus rules file",
      shapes=("parent_is_file",)),
    R("ops/migrate_to_operator.py", "--output-dir", "dir",
      lambda c, out: ["--source-dir", str(_jsc.K8S_MONITORING),
                      "--config-dir", str(_jsc.SEED_CONF_D), "--output-dir", str(out)],
      reach="in-repo k8s/03-monitoring + seed conf.d",
      shapes=("parent_is_file",)),
    R("ops/onboard_platform.py", "-o/--output-dir", "dir",
      lambda c, out: ["--alertmanager-config", str(_jsc.ALERTMANAGER_YML), "-o", str(out)],
      reach="in-repo try-local alertmanager.yml",
      shapes=("parent_is_file",)),
    R("ops/scaffold_tenant.py", "-o/--output-dir", "dir",
      lambda c, out: ["--tenant", "alpha", "--db", "mariadb", "--non-interactive",
                      "-o", str(out)],
      reach="non-interactive flags only",
      shapes=("parent_is_file",)),
    R("ops/validate_migration.py", "-o/--output-dir", "dir",
      lambda c, out: ["--old", "up", "--new", "up", "--prometheus", c.stub + "/rich",
                      "-o", str(out)],
      reach="local Prometheus stub (/rich: identical vectors ⇒ match)",
      shapes=("parent_is_file",)),
    R("ops/generate_rule_pack_split.py", "--output-dir", "dir",
      lambda c, out: ["--rule-packs-dir", _one_rule_pack_dir(c.tmp), "--output-dir", str(out)],
      reach="tmp rule-packs dir holding a copy of rule-pack-mariadb.yaml",
      shapes=("parent_is_file",)),
    R("ops/operator_generate.py", "--output-dir", "dir",
      lambda c, out: ["--rule-packs-dir", str(_jsc.RULE_PACKS),
                      "--config-dir", str(_jsc.SEED_CONF_D), "--output-dir", str(out)],
      reach="in-repo rule-packs + seed conf.d",
      shapes=("parent_is_file",)),
]


def _run(row: Row, ctx: Ctx, out: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    cwd = row.cwd(ctx) if row.cwd else None
    return subprocess.run(
        [sys.executable, str(TOOLS_DIR / row.tool), *row.build(ctx, out)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=TIMEOUT_S, env=env, cwd=cwd,
    )


def _bad_path(tmp: Path, shape: str, name: str) -> Path:
    if shape == "missing_parent":
        return tmp / "absent" / name
    if shape == "parent_is_file":
        blocker = tmp / "blocker"
        blocker.write_text("this is a file, not a directory\n", encoding="utf-8")
        return blocker / name
    raise AssertionError(shape)


def _fail(row: Row, proc: subprocess.CompletedProcess, why: str) -> str:
    return (f"{row.id}: {why}\n  argv : {proc.args[1:]}\n  exit : {proc.returncode}\n"
            f"  stdout:\n{proc.stdout[-1500:]}\n  stderr:\n{proc.stderr[-1500:]}")


# ═══════════════════════════════════════════════════════════════════════════
# The gate
# ═══════════════════════════════════════════════════════════════════════════
_CASES = [(row, shape) for row in ROWS for shape in row.shapes]


@pytest.mark.parametrize("row,shape", _CASES, ids=[f"{r.id}-{s}" for r, s in _CASES])
def test_unwritable_output_path_is_rc2_one_line_no_traceback(row, shape, tmp_path, stub_url):
    out = _bad_path(tmp_path, shape, row.out_name)
    proc = _run(row, Ctx(tmp_path, stub_url), out)

    assert proc.returncode == EXIT_CALLER_ERROR, _fail(
        row, proc, f"expected rc={EXIT_CALLER_ERROR} (EXIT_CALLER_ERROR) for shape "
                   f"{shape}; rc=1 would read as EXIT_VIOLATION")
    assert "Traceback" not in proc.stderr, _fail(row, proc, "a traceback leaked")
    error_lines = [l for l in proc.stderr.splitlines() if l.startswith("ERROR: cannot ")]
    assert len(error_lines) == 1, _fail(
        row, proc, f"expected exactly one 'ERROR: cannot …' line, got {len(error_lines)}")
    line = error_lines[0]
    # The parent directory string is a prefix of every path the tool could
    # name here (the file itself, or the directory it tried to create).
    assert str(out.parent) in line, _fail(row, proc, "the error line does not name the path")
    assert row.flag in line, _fail(row, proc, f"the error line does not name {row.flag}")
    assert "check the value given to" in line, _fail(
        row, proc, "the error line does not point the operator at the flag")


@pytest.mark.parametrize("row", ROWS, ids=[r.id for r in ROWS])
def test_control_writable_path_is_rc0_and_writes(row, tmp_path, stub_url):
    """The pair half: same argv, writable path ⇒ rc=0 and the output exists.

    A tool that exits 2 whenever the flag is present would pass the bad-path
    test above; this is what rules it out.
    """
    out = tmp_path / "good" / row.out_name
    out.parent.mkdir()
    proc = _run(row, Ctx(tmp_path, stub_url), out)

    assert proc.returncode == 0, _fail(row, proc, "control run did not exit 0")
    assert "Traceback" not in proc.stderr, _fail(row, proc, "control run leaked a traceback")
    assert "ERROR: cannot " not in proc.stderr, _fail(
        row, proc, "control run printed a write error")
    if row.kind == "file":
        assert out.is_file() and out.stat().st_size > 0, _fail(
            row, proc, f"control run did not write {out}")
    else:
        written = [p for p in out.rglob("*") if p.is_file()]
        assert written, _fail(row, proc, f"control run wrote nothing under {out}")


# ═══════════════════════════════════════════════════════════════════════════
# Staleness guards
# ═══════════════════════════════════════════════════════════════════════════
def test_rows_point_at_real_tools_and_fixtures():
    for row in ROWS:
        assert (TOOLS_DIR / row.tool).is_file(), row.tool
        assert row.kind in ("file", "dir"), row.id
    assert WAVEFORM_FIXTURE.is_file()
    for p in (_jsc.SEED_CONF_D, _jsc.RULE_PACKS, _jsc.K8S_MONITORING, _jsc.ALERTMANAGER_YML):
        assert p.exists(), p


def _tools_naming_a_flag() -> set[str]:
    """Tool modules under scripts/tools that pass ``flag=`` to a secure
    writer / ``ensure_dir`` — i.e. declare an argv-sourced output path."""
    import ast
    writers = {"write_text_or_die", "write_json_or_die", "ensure_dir_or_die", "ensure_dir",
               "write_text_secure", "write_json_secure", "write_yaml_crd",
               "write_onboard_hints"}
    found: set[str] = set()
    for py in sorted(TOOLS_DIR.rglob("*.py")):
        if py.name.startswith("_"):
            continue  # libraries forward `flag=`; they are not CLI rows
        tree = ast.parse(py.read_text(encoding="utf-8"), str(py))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
            if name in writers and any(kw.arg == "flag" and kw.value is not None
                                       and not (isinstance(kw.value, ast.Constant)
                                                and kw.value.value is None)
                                       for kw in node.keywords):
                found.add(py.relative_to(TOOLS_DIR).as_posix())
                break
    return found


def test_every_flag_naming_tool_has_a_row():
    """Derived, not enumerated: a tool that names an output flag in its
    writer must have a control pair here, or the row table has gone stale."""
    naming = _tools_naming_a_flag()
    assert naming, "no tool passes flag= to a secure writer — scanner drift"
    covered = {r.tool for r in ROWS}
    missing = sorted(naming - covered)
    assert not missing, (
        "tool(s) name an output flag in a secure writer but have no control "
        f"pair in ROWS: {missing}")
