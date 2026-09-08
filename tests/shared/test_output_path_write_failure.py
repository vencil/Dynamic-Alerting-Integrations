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
* ``artifact_is_dir`` — the output DIRECTORY is fine and writable; the file
  the tool will create inside it (``Row.artifact``) already exists as a
  directory, so the write fails EISDIR. The two shapes above die at the
  tool's own ``mkdir``, which means for a multi-artefact tool they only ever
  prove the FIRST sink is guarded. This one lets the tool get past its mkdir
  and reach a specific later write.
* ``target_is_dir`` (#1789) — the flag value ITSELF is a directory. For a
  tool that creates its parent with ``mkdir -p`` the two parent shapes both
  die at that mkdir; this one satisfies the mkdir and fails at the write
  proper, which for an atomic writer is the ``os.replace`` at the very end.
* ``sibling_is_file`` / ``sibling_is_dir`` (#1789) — for a tool whose output
  path is DERIVED from the flag rather than being it. ``ops/config_history``
  takes ``--config-dir`` (an INPUT directory that must exist and hold YAML)
  and writes its snapshots to ``<parent>/.da-history/…``. None of the three
  shapes above can express that: they all corrupt the flag value itself, and
  a ``--config-dir`` that does not exist is rejected long before any write.
  These two build a VALID flag value and pre-create ``Row.artifact`` —
  resolved against the flag value's PARENT, so it can name the derived tree —
  as a regular file (blocks a ``mkdir``) or as a directory (blocks a write).

Permission bits are deliberately not used: CI and the dev container run as
root, where ``chmod 0o500`` is ignored and the test would pass vacuously.

⚠️ A ``Row`` is per-(tool, shape), and a tool run stops at its first
exception: whichever sink dies FIRST masks every sink after it. So a green
row is evidence about ONE sink, not about the tool. Reaching a later sink
needs its own row with a shape that leaves the earlier ones satisfiable —
that is what ``artifact_is_dir`` plus a distinct ``Row.artifact`` buys.

REACHABILITY
------------
Every row reaches its write offline. Tools that need Prometheus get the
in-process stub server from ``test_json_stdout_contract`` (the same one the
``--json`` and ``--dry-run`` gates use); ``generate_changelog`` gets a
throw-away git repository with one conventional commit.

POPULATION — WHAT THIS TABLE CAN AND CANNOT SEE
----------------------------------------------
``test_every_flag_naming_tool_has_a_row`` derives, from the tree, the set of
tool modules that pass ``flag=`` to one of the WRITER KEYS — the secure
writers, ``ensure_dir``, and (#1789) the ``output_write`` context manager
that wraps a raw sink. A tool that grows an output flag and names it in one
of those without adding a row goes red.

⛔ The population is the writer keys, nothing else. A tool that writes its
output with a RAW sink — a bare ``open(..., "w")``, ``Path.write_text``,
``os.makedirs``, ``shutil.copy2`` — and never routes it through a writer key
is INVISIBLE here: it does not appear as a missing row, it does not appear at
all. That is a property of deriving the population from the fix rather than
from the defect, and it is why the raw sinks carry a separate static pin in
``tests/shared/test_output_write_sites_stay_guarded.py``. Read a green run
here as "every tool that DECLARED an output flag at its writer has a control
pair", never as "every tool with an output flag".

A declaration-side population (grepping ``add_argument`` for ``--out*``) was
measured and rejected: 37 hits, 2 of them input flags mis-collected, and it
still missed ``--summary-file``. (``dx/paired_trend_watch``'s row is the one
that flag names — the derived population found it, the grep would not have.)

⛔ And one more hole, named rather than hidden: ``NO_OFFLINE_ROW`` at the
bottom of this file lists tools that ARE in the derived population but whose
write cannot be reached without infrastructure this test suite does not have.
An entry means "the behavioural gate is silent about this tool", not "covered
elsewhere". It is exit-locked (fixed size, every entry re-checked against the
derived population) so it cannot quietly grow into the place where evidence
should be.
"""
from __future__ import annotations

import json
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
WAVEFORM_TOLERANCES = (REPO_ROOT / "tests" / "dx" / "fixtures" / "waveform"
                       / "tolerances" / "selftest_tolerances.yaml")
THRESHOLDCONFIG_CR = REPO_ROOT / "k8s" / "crd" / "examples" / "example-thresholdconfig.yaml"
PAIRED_DATASET = (REPO_ROOT / "docs" / "internal" / "audit-reports"
                  / "bench-paired-2026-08")

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


def _custom_alerts_conf_d(tmp: Path) -> str:
    """A conf.d tree with one `_custom_alerts` recipe — enough to compile."""
    d = tmp / "custom-alerts-conf.d"
    d.mkdir()
    (d / "a.yaml").write_text(
        "tenants:\n"
        "  ta:\n"
        "    _custom_alerts:\n"
        '      - {recipe: threshold, name: cpu_hot, metric: node_cpu, op: ">",\n'
        '         window: 5m, threshold: "80:warning"}\n',
        encoding="utf-8")
    return str(d)


def _soak_output_dir(tmp: Path) -> str:
    """A `run_chaos_soak.py` output directory, hand-built.

    Flat samples on purpose: the renderer's verdict is "no single-direction
    trend ⇒ PASS", and only a rc=0 run is a usable control pair.
    """
    d = tmp / "soak-out"
    d.mkdir()
    (d / "run-config.json").write_text(
        '{"target_url": "http://127.0.0.1:1", "duration_min": 1,\n'
        ' "reload_interval_sec": 9999, "metrics_poll_sec": 10,\n'
        ' "started_at_utc": "2026-09-08T00:00:00+00:00",\n'
        ' "ended_at_utc": "2026-09-08T00:01:00+00:00", "reload_count": 0}\n',
        encoding="utf-8")
    (d / "metrics-timeseries.csv").write_text(
        "timestamp_utc,elapsed_sec,reload_count_so_far,go_goroutines,"
        "process_resident_memory_bytes\n"
        "2026-09-08T00:00:00Z,0,0,10,1000\n"
        "2026-09-08T00:00:10Z,10,0,10,1000\n"
        "2026-09-08T00:00:20Z,20,0,10,1000\n"
        "2026-09-08T00:00:30Z,30,0,10,1000\n",
        encoding="utf-8")
    return str(d)


def _bench_side(tmp: Path, name: str) -> str:
    """One side of a paired bench run: a `cpu:` header plus ns/op rows.

    Both sides are IDENTICAL, so every benchmark is evaluated and the verdict
    is a clean rc=0 — the control half needs that.
    """
    p = tmp / f"bench-{name}.txt"
    p.write_text(
        "goos: linux\ngoarch: amd64\npkg: example/app\n"
        "cpu: Stub CPU @ 1.00GHz\n"
        "BenchmarkAlpha-4   1000   1200 ns/op\n"
        "BenchmarkAlpha-4   1000   1210 ns/op\n"
        "BenchmarkAlpha-4   1000   1190 ns/op\n",
        encoding="utf-8")
    return str(p)


def _effective_config_json(tmp: Path, name: str) -> str:
    """One side of a blast-radius diff, produced by the tool that feeds it.

    `blast_radius` reads `describe_tenant --all --output` JSON, so the input is
    generated with `describe_tenant` itself against the in-repo seed conf.d
    rather than hand-written: a hand-written stand-in would drift from the
    producer's schema and the row would start proving nothing.
    """
    p = tmp / f"effective-{name}.json"
    if not p.exists():
        subprocess.run(
            [sys.executable, str(TOOLS_DIR / "dx" / "describe_tenant.py"),
             "--conf-d", str(_jsc.SEED_CONF_D), "--all", "-o", str(p)],
            check=True, capture_output=True, timeout=TIMEOUT_S)
    return str(p)


def _shard_source_dir(tmp: Path) -> str:
    """One `--sources` shard for `assemble_config_dir`: a flat conf.d."""
    d = tmp / "shard-a"
    if not d.is_dir():
        d.mkdir()
        (d / "alpha.yaml").write_text(
            "tenants:\n  alpha:\n    max_connections: 100\n", encoding="utf-8")
    return str(d)


def _history_conf_d(out: Path) -> str:
    """`--config-dir` for `ops/config_history`: a real conf.d with one shard.

    Built by the row rather than by the shape, because for this tool the flag
    value is an INPUT that has to exist and hold at least one YAML carrier —
    the output goes to `<parent>/.da-history`.
    """
    out.mkdir(parents=True, exist_ok=True)
    (out / "alpha.yaml").write_text(
        "tenants:\n  alpha:\n    max_connections: 100\n", encoding="utf-8")
    return str(out)


def _inject_report(tmp: Path) -> str:
    """A synthetic `inject_waveform` report for `dx/waveform_score`.

    Built here rather than produced by `inject_waveform`, because that tool
    needs a live vmsingle plus a `vmalert` binary and cannot run offline —
    which is also why it has no row of its own (see `NO_OFFLINE_ROW`).
    The one alert fires INSIDE the fault window, so the verdict is PASS and
    the control half exits 0; a FAIL verdict would exit 1.
    """
    alert = {"alertname": "CandidateA", "fire_offset_s": 1000,
             "last_fire_offset_s": 1060, "resolve_offset_s": 1090,
             "firing_sample_count": 3,
             "labels": {"alertname": "CandidateA", "waveform_signature": "0",
                        "waveform_variant": "base", "severity": "warning"}}
    report = {
        "tool": "inject-waveform", "pack_id": "synthetic-pack",
        "records": [{"signature_index": 0, "fault_class": "selftest-fault",
                     "metric": "selftest_metric", "variant": "base",
                     "series": None, "expects": "must_detect", "labels": {},
                     "fired": True, "alerts": [alert]}],
        "window": {"span_s": 12000, "step_s": 30},
        "metadata": {"series": [{"signature_index": 0, "variant": "base",
                                 "labels": {}, "expects": "must_detect",
                                 "fault_window_s": [300, 9270],
                                 "hold_start_s": None}]},
        "unattributed_alerts": [],
    }
    p = tmp / "inject-report.json"
    p.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    return str(p)


def _trufflehog_ndjson(tmp: Path) -> str:
    """`--input` for `lint/trufflehog_to_sarif`: one unverified finding.

    Unverified on purpose: a VERIFIED finding makes the tool exit
    EXIT_VERIFIED_FINDING (1) after a successful write, and the control half
    of the pair asserts rc=0.
    """
    p = tmp / "trufflehog.json"
    p.write_text(
        '{"DetectorName": "Generic", "Verified": false, "Raw": "x", '
        '"SourceMetadata": {"Data": {"Filesystem": {"file": "a.txt", "line": 1}}}}\n',
        encoding="utf-8")
    return str(p)


def _bench_baseline(tmp: Path) -> str:
    """`--baseline` for `dx/write_baseline_marker`: two countable rows.

    Countable by the CONSUMER's regex (`analyze_bench_history._BENCH_RE`),
    which the tool imports rather than restates: a line the regex rejects
    would make the tool refuse with "no parseable benchmark rows" (rc=1)
    before it ever reaches the write this row is about.
    """
    p = tmp / "bench-baseline.txt"
    p.write_text("BenchmarkFoo-8\t1000\t123 ns/op\n"
                 "BenchmarkBar-8\t2000\t456 ns/op\n", encoding="utf-8")
    return str(p)


def _soak_config_dir(tmp: Path) -> str:
    """`--config-dir` for `dx/run_chaos_soak`: a throw-away conf.d.

    The tool PERTURBS a carrier in here on every reload tick (that is the
    chaos), so the row must never point it at the in-repo seed. With
    `--duration-min 0` the poll/reload loop does not run at all and nothing
    is written here, but the copy is what makes that a property of the
    fixture rather than of the argv.
    """
    d = tmp / "soak-conf.d"
    if not d.is_dir():
        d.mkdir()
        (d / "alpha.yaml").write_text(
            "tenants:\n  alpha:\n    max_connections: 100\n", encoding="utf-8")
    return str(d)


def _state_dir(tmp: Path) -> str:
    """`--state-dir` for `ops/state_reconcile`: one current-schema state file.

    Current schema on purpose: no migration is triggered, so the ONLY write
    the run makes is the manifest — the sink the row is about.
    """
    d = tmp / "state"
    if not d.is_dir():
        d.mkdir()
        (d / "c1.json").write_text(
            '{"schema_version": "1.0", "cluster": "c1"}\n', encoding="utf-8")
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
    # Text the tool prints BEFORE the standard line, on the same line. Two
    # tools have one, and in both the prefix is a contract of its own that
    # predates this ticket, so it is declared here and stripped before the
    # shared assertions rather than being allowed to relax them:
    #   * ``write_baseline_marker`` — a GitHub ``::error::`` annotation, read
    #     by the nightly's log parser;
    #   * ``waveform_score`` — ``ERROR [ERR_OUTPUT]: ``, the de-identified
    #     error code its ``--redact`` mode is documented to print in both
    #     modes (the SME triages by code without a re-run).
    # Declaring a prefix is load-bearing in both directions — the line must
    # actually carry it. ⚠️ It also DOUBLES the word ERROR in both lines,
    # which is ugly and deliberate: the alternative was to loosen the shared
    # matcher for two tools, and a matcher that accepts more shapes proves
    # less about all of them.
    prefix: str = ""
    # For the ``artifact_is_dir`` shape: the name of the file the tool creates
    # INSIDE its output directory, which the harness pre-creates as a
    # directory. Names the ONE sink this row is evidence about.
    artifact: str | None = None
    # An external executable the row's reachability depends on, skipped when
    # it is absent. ``ops/federation_keygen`` shells out to ``openssl`` to
    # make the keypair, and without it the run dies BEFORE the write under
    # test — a red that says nothing about this contract. Mirrors the
    # ``_needs_openssl`` skip in tests/ops/test_federation_keygen.py.
    # ⚠️ A skip is not a pass: the row is silently absent wherever the binary
    # is, which is why this field is per-row and named rather than a blanket
    # try/except around the run.
    requires: str | None = None
    # For a tool whose output is DERIVED from the flag rather than being it:
    # the path the CONTROL run must have produced, computed from the flag
    # value. Without it the control for such a row would assert on the flag
    # value itself — which the fixture populated — and would stay green for a
    # tool that wrote nothing at all.
    produces: Callable[[Path], Path] | None = None

    @property
    def id(self) -> str:
        return f"{Path(self.tool).stem}[{self.flag}]"


R = Row
ROWS: list[Row] = [
    # ── dx ─────────────────────────────────────────────────────────────────
    R("dx/describe_tenant.py", "-o/--output", "file",
      lambda c, out: ["--conf-d", str(_jsc.SEED_CONF_D), "--all", "-o", str(out)],
      reach="in-repo try-local seed conf.d; --all is what reaches the write",
      out_name="tenants.json"),
    R("dx/migrate_conf_d.py", "-o/--output-plan", "file",
      lambda c, out: ["--conf-d", str(_jsc.SEED_CONF_D), "-o", str(out)],
      reach="in-repo try-local seed conf.d (dry-run is the default)",
      out_name="migration-plan.json"),
    R("dx/scan_component_health.py", "--output", "file",
      lambda c, out: ["--output", str(out), "--today", "2026-09-08"],
      reach="in-repo docs/assets/tool-registry.yaml + tools/portal sources",
      out_name="component-health.json",
      # Its own `mkdir -p` creates the parent, so missing_parent is a
      # legitimate success. parent_is_file is what reaches the mkdir; the
      # write_text behind it is covered by the static pin, not by a row —
      # no shape here can make the write fail while the mkdir succeeds for
      # a FILE-shaped output flag.
      shapes=("parent_is_file",)),
    R("dx/compile_custom_alerts.py", "--out", "file",
      lambda c, out: ["--config-dir", _custom_alerts_conf_d(c.tmp), "--out", str(out)],
      reach="tmp conf.d with one _custom_alerts threshold recipe",
      out_name="rule-pack-custom-alerts.yaml"),
    R("dx/generate_changelog.py", "-o/--output", "file",
      lambda c, out: ["-o", str(out)],
      reach="throw-away git repo with one conventional commit (cwd)",
      out_name="CHANGELOG-draft.md",
      cwd=lambda c: _git_repo_with_one_commit(c.tmp)),
    R("dx/generate_tenant_metadata.py", "--output", "file",
      lambda c, out: ["--config-dir", str(_jsc.SEED_CONF_D), "--output", str(out)],
      reach="in-repo try-local seed conf.d",
      out_name="tenant-metadata.json",
      # Its own `mkdir -p` creates a missing parent, so only the blocked
      # parent fails. The `chmod 0644` sits inside the same wrapped block as
      # the write, so a chmod failure lands on the same flag.
      shapes=("parent_is_file",)),
    # Three rows, one per `--layout`: each layout has its OWN
    # `output_dir.mkdir` (generate_flat / generate_hierarchical /
    # generate_synthetic_v2), so one row would only ever prove the default
    # one. The four sinks behind them — the two `slot_dir.mkdir`s and
    # `_write_yaml`'s mkdir + open — cannot be reached from here at all: the
    # tool refuses a non-empty output directory (rc=2, no ERROR line) before
    # any of them runs, which is what the `artifact_is_dir` shape would need.
    # They are wrapped, and the static pin is the only evidence for them.
    *[R("dx/generate_tenant_fixture.py", "-o/--output", "dir",
        (lambda layout: lambda c, out: ["--count", "2", "--layout", layout,
                                        "-o", str(out)])(_layout),
        reach=f"flags only; --layout {_layout}",
        out_name="conf.d",
        shapes=("parent_is_file",))
      for _layout in ("flat", "hierarchical", "synthetic-v2")],
    # `--summary-file` is why the population is derived from the WRITER and
    # not from `add_argument` names: a declaration-side scan for `--out*`
    # misses it entirely (measured). `missing_parent` and `parent_is_file`
    # reach the `open(..., "a")`; `target_is_dir` reaches the same call with a
    # different errno, and is kept because an APPEND to an existing path is
    # this tool's normal case (the CI runner's $GITHUB_STEP_SUMMARY already
    # exists) — the shape that most resembles the real one.
    R("dx/paired_trend_watch.py", "--summary-file", "file",
      lambda c, out: ["--dataset", str(PAIRED_DATASET), "--summary-file", str(out)],
      reach="the frozen six-night archival dataset in the repo; --dataset is "
            "the offline source (--from-gh would need the gh CLI)",
      out_name="paired-trend.md",
      shapes=("missing_parent", "parent_is_file", "target_is_dir")),
    R("dx/pair_bench_ratio.py", "--out", "file",
      lambda c, out: ["--reference", _bench_side(c.tmp, "ref"),
                      "--main", _bench_side(c.tmp, "main"),
                      "--reference-tag", "v1.0.0", "--out", str(out)],
      reach="two synthetic `go test -bench` transcripts with the same cpu: header",
      out_name="bench-paired.json",
      # `--out`'s own `mkdir -p` creates a missing parent, so only the
      # blocked-parent shape reaches a failure.
      shapes=("parent_is_file",)),
    R("dx/render_soak_diff.py", "--output", "file",
      lambda c, out: ["--input-dir", _soak_output_dir(c.tmp), "--output", str(out),
                      "--warmup-sec", "0"],
      reach="hand-built soak output dir (run-config.json + flat timeseries CSV)",
      out_name="soak-report.md"),
    # Four rows, four sinks. `run_chaos_soak` writes three artefacts under
    # `--output-dir` and the run stops at the first failure, so one row would
    # only ever prove the `mkdir`. `--duration-min 0` makes the poll loop a
    # no-op (the `finally` still runs and still writes both files) and
    # `--reload-interval-sec 9999` keeps the chaos from touching the fixture
    # conf.d. All three artefact sinks were measured individually reachable.
    R("dx/run_chaos_soak.py", "--output-dir", "dir",
      lambda c, out: ["--target-url", c.stub + "/soak",
                      "--config-dir", _soak_config_dir(c.tmp),
                      "--duration-min", "0", "--reload-interval-sec", "9999",
                      "--output-dir", str(out)],
      reach="the shared stub's /soak personality (Go-runtime /metrics body); "
            "the first-probe check is what a wrong stub would fail on",
      shapes=("parent_is_file",)),
    *[R("dx/run_chaos_soak.py", "--output-dir", "dir",
        lambda c, out: ["--target-url", c.stub + "/soak",
                        "--config-dir", _soak_config_dir(c.tmp),
                        "--duration-min", "0", "--reload-interval-sec", "9999",
                        "--output-dir", str(out)],
        reach=f"same; {_artefact} is blocked so the run dies at that sink",
        shapes=("artifact_is_dir",),
        artifact=_artefact)
      for _artefact in ("metrics-timeseries.csv", "summary.txt", "run-config.json")],
    # One of the two rows with a `prefix` (the other is `dx/waveform_score`,
    # for a different reason). This tool writes a GitHub annotation
    # (`::error::`) in front of the standard line because the nightly's log
    # parser reads annotations — see the comment at its `except
    # OutputWriteError`, which explains why it does not use the shared
    # decorator. `target_is_dir` is the shape that reaches the `write_text`;
    # `parent_is_file` reaches the `mkdir -p` in front of it. `missing_parent`
    # is a legitimate success (the tool creates the parent).
    # ⛔ The write moved 1 → 2 here; the three INPUT refusals (`--baseline`
    # missing / unreadable / no parseable rows) still exit 1 and
    # tests/dx/test_write_baseline_marker.py pins that split.
    R("dx/write_baseline_marker.py", "--out", "file",
      lambda c, out: ["--baseline", _bench_baseline(c.tmp), "--out", str(out)],
      reach="a two-row synthetic `go test -bench` dump",
      out_name="bench-baseline.rows",
      shapes=("parent_is_file", "target_is_dir"),
      prefix="::error::"),
    R("dx/sync_glossary_abbr.py", "--output", "file",
      lambda c, out: ["--glossary", _glossary(c.tmp), "--output", str(out)],
      reach="tmp glossary.md with two **ABBR (Expansion)** entries",
      out_name="abbreviations.md",
      shapes=("parent_is_file",)),
    # `prefix` again, for a different reason than write_baseline_marker's.
    # This tool funnels every error through `_emit_error`, which stamps a
    # de-identified error CODE — a documented `--redact` contract (the SME
    # triages by code without a re-run, and under `--redact` the path must not
    # be printed at all). So the standard line arrives BEHIND
    # `ERROR [ERR_OUTPUT]: `, doubling the word ERROR; that is the declared
    # cost of keeping both contracts, and declaring it here means the tool
    # cannot quietly drop either half.
    R("dx/waveform_score.py", "--out", "file",
      lambda c, out: [_inject_report(c.tmp), "--tolerances", str(WAVEFORM_TOLERANCES),
                      "--out", str(out)],
      reach="a synthetic inject report whose one alert fires inside the fault "
            "window (verdict PASS ⇒ rc=0) plus the in-repo self-test tolerance "
            "matrix",
      out_name="waveform-score.json",
      prefix="ERROR [ERR_OUTPUT]: "),
    R("dx/waveform_compile.py", "--out", "dir",
      lambda c, out: ["--compile", "--out", str(out), "--allow-selftest",
                      str(WAVEFORM_FIXTURE)],
      reach="in-repo self-test waveform pack fixture",
      shapes=("parent_is_file",)),
    # ── lint ───────────────────────────────────────────────────────────────
    R("lint/trufflehog_to_sarif.py", "--output", "file",
      lambda c, out: ["--input", _trufflehog_ndjson(c.tmp), "--output", str(out)],
      reach="a one-finding NDJSON transcript; no trufflehog binary is needed, "
            "the tool only converts",
      out_name="trufflehog.sarif"),
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
    # Three rows, one derived sink each. `config_history` has no output flag:
    # every path it writes hangs off `<--config-dir>/../.da-history`, so the
    # flag it must name is the input one. The FOURTH sink — `_save_history`'s
    # `history.json` write — has no row: every shape that makes history.json
    # unwritable also makes it unreadable, and the deliberately unguarded READ
    # at `_load_history` dies first (measured: rc=1 traceback at that line).
    # It is wrapped; the static pin is the only evidence for it.
    R("ops/config_history.py", "--config-dir", "dir",
      lambda c, out: ["--config-dir", _history_conf_d(out), "snapshot", "-m", "x"],
      reach="a one-shard conf.d built in place; `snapshot` is what writes",
      out_name="conf.d", artifact=".da-history",
      shapes=("sibling_is_file",),
      produces=lambda out: out.parent / ".da-history" / "history.json"),
    R("ops/config_history.py", "--config-dir", "dir",
      lambda c, out: ["--config-dir", _history_conf_d(out), "snapshot", "-m", "x"],
      reach="same, with the snapshot subdirectory blocked",
      out_name="conf.d", artifact=".da-history/snap-1",
      shapes=("sibling_is_file",),
      produces=lambda out: out.parent / ".da-history" / "history.json"),
    R("ops/config_history.py", "--config-dir", "dir",
      lambda c, out: ["--config-dir", _history_conf_d(out), "snapshot", "-m", "x"],
      reach="same, with the snapshot COPY of alpha.yaml blocked",
      out_name="conf.d", artifact=".da-history/snap-1/alpha.yaml",
      shapes=("sibling_is_dir",),
      produces=lambda out: out.parent / ".da-history" / "history.json"),
    R("ops/da_assembler.py", "--config-dir", "dir",
      lambda c, out: ["--render-cr", str(THRESHOLDCONFIG_CR), "--config-dir", str(out)],
      reach="in-repo example ThresholdConfig CR through --render-cr; the "
            "offline path needs no Kubernetes client",
      # ⛔ This tool used to exit 0 here having written nothing: `reconcile_one`
      # logged the OSError and `render_cr_file` returned EXIT_OK regardless
      # (measured rc=0 on both shapes). `parent_is_file` reaches the mkdir;
      # `artifact_is_dir` reaches the rendered file itself — which is also
      # where the idempotence read of that same path dies, and that is as far
      # as a row can get: with `db-a.yaml` a directory the run never returns
      # from that read, so the `write_text` + `chmod` behind it are masked
      # (verified: deleting THEIR wrapper leaves this row green and turns the
      # static pin red — the two gates cover this file between them, neither
      # alone).
      shapes=("parent_is_file", "artifact_is_dir"),
      artifact="db-a.yaml"),
    R("ops/state_reconcile.py", "--manifest-path", "file",
      lambda c, out: ["--state-dir", _state_dir(c.tmp), "--manifest-path", str(out)],
      reach="a state dir with one current-schema file: no migration runs, so "
            "the manifest write is the only sink the run reaches",
      out_name="manifest.json",
      # ⚠️ This row USED to be the only evidence for `write_json`'s atomic
      # sinks: the pre-existing `except BaseException: <unlink temp>; raise`
      # cleanup satisfied the static pin's "a stopping handler is a guard"
      # arm, so the pin stayed green with the wrapper deleted while this row
      # went red. That arm was tightened afterwards (a bare re-raise passes a
      # raw OSError outwards, which nothing above catches), and both gates now
      # go red when the wrapper is removed — verified both ways, and the
      # reasoning is in test_output_write_sites_stay_guarded's docstring.
      # `missing_parent` is a legitimate success here — the tool creates the
      # manifest's parent itself. `parent_is_file` reaches that mkdir;
      # `target_is_dir` gets past it and reaches the `os.replace` that ends
      # the atomic write, which is the sink the mkdir-side shape masks.
      shapes=("parent_is_file", "target_is_dir"),
      # Both defaults (`--state-dir .da/state`, `--manifest-path
      # .da/manifest.json`) are CWD-relative; run in tmp so a future argv
      # slip writes there and not into the checkout.
      cwd=lambda c: c.tmp),
    R("ops/federation_keygen.py", "--jwks-out", "file",
      lambda c, out: ["--jwks-out", str(out)],
      reach="flags only; the private key goes to stdout (a pipe, never a tty) "
            "and the JWKS to --jwks-out — needs openssl for the keypair",
      out_name="federation-jwks.json",
      requires="openssl"),
    R("ops/generate_alertmanager_routes.py", "-o/--output", "file",
      lambda c, out: ["--config-dir", str(_jsc.SEED_CONF_D), "-o", str(out)],
      reach="in-repo try-local seed conf.d; the default render mode writes the "
            "routing fragment to -o",
      out_name="alertmanager-routes.yaml"),
    R("ops/generate_tenant_mapping_rules.py", "-o/--output", "file",
      lambda c, out: ["--config-dir", _mapping_conf_d(c.tmp), "--metrics", "mysql_up",
                      "-o", str(out)],
      reach="tmp conf.d with a two-tenant _instance_mapping.yaml",
      out_name="mapping-rules.yaml"),
    R("ops/assemble_config_dir.py", "--output", "dir",
      lambda c, out: ["--sources", _shard_source_dir(c.tmp), "--output", str(out)],
      reach="one tmp conf.d shard with a single tenant file",
      # `--output` and `--manifest` are two flags on two different paths, so
      # they get a row each. Only the `mkdir` is reachable from here: the
      # `shutil.copy2` behind it CANNOT be made to fail by the artifact_is_dir
      # shape, because copy2 with a directory destination copies INTO it
      # (measured: rc=0, the file lands at <out>/alpha.yaml/alpha.yaml). That
      # sink is wrapped and only the static pin speaks for it.
      shapes=("parent_is_file",)),
    R("ops/assemble_config_dir.py", "--manifest", "file",
      lambda c, out: ["--sources", _shard_source_dir(c.tmp),
                      "--output", str(c.tmp / "assembled"), "--manifest", str(out)],
      reach="same shard; `--output` is a writable tmp dir so the run reaches "
            "the manifest write, which is the sink under test",
      out_name="assembly-manifest.json"),
    R("ops/blast_radius.py", "-o/--output", "file",
      lambda c, out: ["--base", _effective_config_json(c.tmp, "base"),
                      "--pr", _effective_config_json(c.tmp, "pr"),
                      "-o", str(out)],
      reach="two `describe_tenant --all` dumps of the in-repo seed conf.d "
            "(identical: an empty diff is still a written report, rc=0)",
      out_name="blast-radius.json"),
    R("ops/baseline_discovery.py", "-o/--output-dir", "dir",
      lambda c, out: ["--tenant", "t1", "--prometheus", "http://127.0.0.1:1",
                      "--duration", "1", "--interval", "1", "-o", str(out)],
      reach="a closed port for Prometheus: every query fails, every sample is "
            "None, and the tool still exits 0 having written both CSVs",
      # Two rows' worth of sinks in one tool: `parent_is_file` reaches the
      # `os.makedirs`, `artifact_is_dir` gets past it and reaches the
      # `open(..., 'wb')` + `chmod` inside `_write_csv_secure`. The FIRST CSV
      # is the artefact — the summary write behind it is masked, as the
      # module docstring says, and only the static pin speaks for it.
      shapes=("parent_is_file", "artifact_is_dir"),
      artifact="baseline-t1-timeseries.csv"),
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
      # `parent_is_file` dies at the first `_safe_mkdir` (edge-rules/), which
      # is already an `ensure_dir_or_die`. `artifact_is_dir` gets past both
      # mkdirs AND past the two split writes, and reaches the LAST raw sink
      # in the file — the `open(validation-report.json, "w")` that #1789
      # wrapped. Without this second shape the row would still be green with
      # that wrapper deleted.
      shapes=("parent_is_file", "artifact_is_dir"),
      artifact="validation-report.json"),
    R("ops/operator_generate.py", "--output-dir", "dir",
      lambda c, out: ["--rule-packs-dir", str(_jsc.RULE_PACKS),
                      "--config-dir", str(_jsc.SEED_CONF_D), "--output-dir", str(out)],
      reach="in-repo rule-packs + seed conf.d",
      shapes=("parent_is_file",)),
]


def _skip_if_unavailable(row: Row) -> None:
    if row.requires and shutil.which(row.requires) is None:
        pytest.skip(f"{row.id}: {row.requires} is not on PATH")


def _run(row: Row, ctx: Ctx, out: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    cwd = row.cwd(ctx) if row.cwd else None
    return subprocess.run(
        [sys.executable, str(TOOLS_DIR / row.tool), *row.build(ctx, out)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=TIMEOUT_S, env=env, cwd=cwd,
    )


SHAPES = ("missing_parent", "parent_is_file", "target_is_dir",
          "artifact_is_dir", "sibling_is_file", "sibling_is_dir")
# Shapes that pre-create ``Row.artifact`` and therefore require it.
_ARTIFACT_SHAPES = frozenset({"artifact_is_dir", "sibling_is_file",
                             "sibling_is_dir"})


def _bad_path(tmp: Path, shape: str, row: Row) -> Path:
    """The value handed to *row*'s flag, built so the named sink fails."""
    if shape == "missing_parent":
        return tmp / "absent" / row.out_name
    if shape == "parent_is_file":
        blocker = tmp / "blocker"
        blocker.write_text("this is a file, not a directory\n", encoding="utf-8")
        return blocker / row.out_name
    if shape == "target_is_dir":
        # The parent is fine, so the tool's own `mkdir -p` succeeds; the path
        # it was told to WRITE is a directory.
        out = tmp / row.out_name
        out.mkdir(parents=True)
        return out
    if shape == "artifact_is_dir":
        # The output directory itself is fine — the tool's own mkdir -p
        # succeeds — and the ARTEFACT it is about to write is a directory.
        assert row.artifact, f"{row.id}: shape artifact_is_dir needs Row.artifact"
        out = tmp / row.out_name
        (out / row.artifact).mkdir(parents=True)
        return out
    if shape in ("sibling_is_file", "sibling_is_dir"):
        # The flag value is a perfectly good directory; what is blocked is a
        # path the tool DERIVES from it, resolved against its parent.
        assert row.artifact, f"{row.id}: shape {shape} needs Row.artifact"
        out = tmp / row.out_name
        out.mkdir(parents=True, exist_ok=True)
        blocked = tmp / row.artifact
        blocked.parent.mkdir(parents=True, exist_ok=True)
        if shape == "sibling_is_dir":
            blocked.mkdir()
        else:
            blocked.write_text("this is a file, not a directory\n", encoding="utf-8")
        return out
    raise AssertionError(shape)


def _expected_fragment(row: Row, shape: str, out: Path) -> Path:
    """The path the error line must name, per shape.

    For the two parent-side shapes the tool may legitimately name either the
    file or the directory it tried to create, so the common parent is what
    can be asserted. For ``artifact_is_dir`` the whole point is WHICH sink
    died, so the artefact path itself is required.
    """
    if shape == "target_is_dir":
        return out
    if shape == "artifact_is_dir":
        return out / row.artifact
    if shape in ("sibling_is_file", "sibling_is_dir"):
        return out.parent / row.artifact
    return out.parent


def _error_lines(stderr: str, prefix: str) -> list[str]:
    """The tool's standard write-error line(s) in *stderr*, *prefix* included.

    A row that declares a ``prefix`` is asserting the tool emits it: the line
    must carry the prefix AND the standard text after it. Declaring a prefix
    therefore cannot loosen the match, and a tool that GROWS a prefix its row
    does not declare stops matching — both directions are a visible red, which
    is the point of putting the annotation in the table instead of in the
    regex (rulebook D-05g: a declared classification can be declared wrong).
    """
    return [line for line in stderr.splitlines()
            if line.startswith(prefix)
            and line[len(prefix):].startswith("ERROR: cannot ")]


def _fail(row: Row, proc: subprocess.CompletedProcess, why: str) -> str:
    return (f"{row.id}: {why}\n  argv : {proc.args[1:]}\n  exit : {proc.returncode}\n"
            f"  stdout:\n{proc.stdout[-1500:]}\n  stderr:\n{proc.stderr[-1500:]}")


# ═══════════════════════════════════════════════════════════════════════════
# The gate
# ═══════════════════════════════════════════════════════════════════════════
_CASES = [(row, shape) for row in ROWS for shape in row.shapes]


@pytest.mark.parametrize("row,shape", _CASES, ids=[f"{r.id}-{s}" for r, s in _CASES])
def test_unwritable_output_path_is_rc2_one_line_no_traceback(row, shape, tmp_path, stub_url):
    _skip_if_unavailable(row)
    out = _bad_path(tmp_path, shape, row)
    proc = _run(row, Ctx(tmp_path, stub_url), out)

    assert proc.returncode == EXIT_CALLER_ERROR, _fail(
        row, proc, f"expected rc={EXIT_CALLER_ERROR} (EXIT_CALLER_ERROR) for shape "
                   f"{shape}; rc=1 would read as EXIT_VIOLATION")
    assert "Traceback" not in proc.stderr, _fail(row, proc, "a traceback leaked")
    # A row that declares a `prefix` must still emit it: the prefix is a
    # contract (a CI annotation), so a row cannot use it to loosen the match.
    error_lines = _error_lines(proc.stderr, row.prefix)
    assert len(error_lines) == 1, _fail(
        row, proc, f"expected exactly one {row.prefix!r}+'ERROR: cannot …' line, "
                   f"got {len(error_lines)}")
    line = error_lines[0]
    # Which path must be named depends on the shape: see _expected_fragment.
    assert str(_expected_fragment(row, shape, out)) in line, _fail(
        row, proc, "the error line does not name the path")
    assert row.flag in line, _fail(row, proc, f"the error line does not name {row.flag}")
    assert "check the value given to" in line, _fail(
        row, proc, "the error line does not point the operator at the flag")
    assert not line[len(row.prefix):].startswith("ERROR: cannot ERROR"), _fail(
        row, proc, "the message is doubled")


@pytest.mark.parametrize("row", ROWS, ids=[r.id for r in ROWS])
def test_control_writable_path_is_rc0_and_writes(row, tmp_path, stub_url):
    """The pair half: same argv, writable path ⇒ rc=0 and the output exists.

    A tool that exits 2 whenever the flag is present would pass the bad-path
    test above; this is what rules it out.
    """
    _skip_if_unavailable(row)
    out = tmp_path / "good" / row.out_name
    out.parent.mkdir()
    proc = _run(row, Ctx(tmp_path, stub_url), out)

    assert proc.returncode == 0, _fail(row, proc, "control run did not exit 0")
    assert "Traceback" not in proc.stderr, _fail(row, proc, "control run leaked a traceback")
    assert "ERROR: cannot " not in proc.stderr, _fail(
        row, proc, "control run printed a write error")
    if row.produces is not None:
        # A derived-output tool: the flag value is an INPUT the fixture just
        # populated, so "something exists there" proves nothing. Name what the
        # run had to produce instead.
        made = row.produces(out)
        assert made.is_file() and made.stat().st_size > 0, _fail(
            row, proc, f"control run did not produce {made}")
    elif row.kind == "file":
        assert out.is_file() and out.stat().st_size > 0, _fail(
            row, proc, f"control run did not write {out}")
    else:
        written = [p for p in out.rglob("*") if p.is_file()]
        assert written, _fail(row, proc, f"control run wrote nothing under {out}")


# ═══════════════════════════════════════════════════════════════════════════
# The other direction: an INPUT failure must not be dressed up as an output one
# ═══════════════════════════════════════════════════════════════════════════
def test_a_bad_source_entry_does_not_blame_the_output_flag(tmp_path):
    """`assemble_config_dir` copies `--sources` entries into `--output`.

    A source entry that is itself a DIRECTORY named `x.yaml` (measured, not
    hypothetical — `discover_yamls` lists it by extension, it does not stat
    it) makes `shutil.copy2` raise `IsADirectoryError` naming the SOURCE.
    That failure happens INSIDE the block wrapped for `--output`, and the
    wrapper is what decides not to claim it: `output_write` converts only an
    `OSError` that names its own path or an ancestor of it.

    Turning this one into the standard line would print "cannot copy into
    <output>/x.yaml … check the value given to --output" and send the
    operator to edit the one flag that is correct. So the pin is negative:
    whatever the tool does with a bad source, it must not name `--output`.
    """
    src = tmp_path / "sources"
    (src / "x.yaml").mkdir(parents=True)
    proc = subprocess.run(
        [sys.executable, str(TOOLS_DIR / "ops" / "assemble_config_dir.py"),
         "--sources", str(src), "--output", str(tmp_path / "assembled")],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=TIMEOUT_S, env={**os.environ, "PYTHONUTF8": "1"})

    assert proc.returncode != 0, proc.stdout
    assert "check the value given to --output" not in proc.stderr, (
        "a source-side failure was mis-attributed to the output flag:\n"
        + proc.stderr[-1500:])
    assert "cannot copy into" not in proc.stderr, proc.stderr[-1500:]
    # And it is still recognisably the source that failed.
    assert str(src / "x.yaml") in proc.stderr, proc.stderr[-1500:]


# ═══════════════════════════════════════════════════════════════════════════
# Harness controls — the row machinery, exercised on synthetic input
#
# `prefix` and the `artifact_is_dir` shape arrive (#1789) before the rows that
# use them. Without these controls both would be dead code that no run touches
# and no failure could reveal: a `_bad_path` that quietly built a writable
# directory, or a matcher that ignored `prefix` entirely, would leave every
# existing row green. They call the production helpers, not copies of them.
# ═══════════════════════════════════════════════════════════════════════════
def _control_row(**kw) -> Row:
    kw.setdefault("kind", "dir")
    return Row(tool="dx/run_chaos_soak.py", flag="--output-dir",
               build=lambda c, out: [], reach="synthetic control", **kw)


class TestErrorLineMatcher:
    _STD = "ERROR: cannot write /x/out.txt: Not a directory (errno 20) — check the value given to -o"

    def test_plain_line_matches_with_no_prefix(self):
        assert _error_lines(f"[warn] noise\n{self._STD}\n", "") == [self._STD]

    def test_declared_prefix_is_matched_with_the_line(self):
        line = f"::error::{self._STD}"
        assert _error_lines(f"{line}\n", "::error::") == [line]

    def test_declared_prefix_that_the_tool_stopped_emitting_does_not_match(self):
        """The CI annotation is a contract too: losing it must go red, not be
        silently accepted because the rest of the line is still right."""
        assert _error_lines(f"{self._STD}\n", "::error::") == []

    def test_undeclared_prefix_does_not_match(self):
        """A tool that GROWS a prefix goes red until the row declares it."""
        assert _error_lines(f"::error::{self._STD}\n", "") == []

    def test_unrelated_stderr_is_not_counted(self):
        assert _error_lines("[warn] /metrics fetch failed\nERROR: bad --tenant\n", "") == []

    def test_two_lines_are_two(self):
        assert len(_error_lines(f"{self._STD}\n{self._STD}\n", "")) == 2


class TestSiblingShapes:
    """The derived-output shapes: the flag value stays USABLE.

    That is the whole difference from `parent_is_file` — if these corrupted
    the flag value the tool would be rejected at argument validation and the
    row would go green on the wrong failure.
    """

    def test_the_flag_value_is_a_usable_directory(self, tmp_path):
        row = _control_row(shapes=("sibling_is_file",), artifact=".da-history")
        out = _bad_path(tmp_path, "sibling_is_file", row)
        assert out.is_dir()
        (out / "alpha.yaml").write_text("x\n", encoding="utf-8")

    def test_sibling_is_file_blocks_a_mkdir(self, tmp_path):
        row = _control_row(shapes=("sibling_is_file",), artifact=".da-history")
        out = _bad_path(tmp_path, "sibling_is_file", row)
        blocked = out.parent / ".da-history"
        assert blocked.is_file()
        with pytest.raises(FileExistsError):
            blocked.mkdir(exist_ok=True)

    def test_sibling_is_dir_blocks_a_write(self, tmp_path):
        row = _control_row(shapes=("sibling_is_dir",), artifact=".da-history/snap-1/a.yaml")
        out = _bad_path(tmp_path, "sibling_is_dir", row)
        blocked = out.parent / ".da-history" / "snap-1" / "a.yaml"
        assert blocked.is_dir(), "intermediate components must be created too"
        with pytest.raises(IsADirectoryError):
            blocked.write_text("x\n", encoding="utf-8")

    def test_the_asserted_path_is_the_sibling(self, tmp_path):
        for shape, art in (("sibling_is_file", ".da-history"),
                           ("sibling_is_dir", ".da-history/snap-1/a.yaml")):
            row = _control_row(shapes=(shape,), artifact=art)
            out = _bad_path(tmp_path / shape, shape, row)
            assert _expected_fragment(row, shape, out) == out.parent / art

    def test_it_refuses_to_build_without_an_artifact(self, tmp_path):
        with pytest.raises(AssertionError):
            _bad_path(tmp_path, "sibling_is_file", _control_row(shapes=("sibling_is_file",)))


class TestTargetIsDirShape:
    def test_the_parent_is_usable_and_the_target_is_a_directory(self, tmp_path):
        row = _control_row(kind="file", out_name="manifest.json",
                           shapes=("target_is_dir",))
        out = _bad_path(tmp_path, "target_is_dir", row)
        assert out.is_dir() and out.parent.is_dir()
        with pytest.raises(IsADirectoryError):
            out.write_text("x\n", encoding="utf-8")

    def test_the_asserted_path_is_the_target_itself(self, tmp_path):
        row = _control_row(kind="file", out_name="manifest.json",
                           shapes=("target_is_dir",))
        out = _bad_path(tmp_path, "target_is_dir", row)
        assert _expected_fragment(row, "target_is_dir", out) == out


class TestRequiresSkip:
    """`requires` must skip only when the binary is really missing.

    A predicate that skipped unconditionally would turn every row carrying it
    into a silent no-op, and the run would still be green — so both signs are
    driven here, on the production helper.
    """

    def test_a_missing_binary_skips(self):
        row = _control_row(requires="definitely-not-a-real-binary-1789")
        with pytest.raises(pytest.skip.Exception):
            _skip_if_unavailable(row)

    def test_a_present_binary_does_not_skip(self):
        _skip_if_unavailable(_control_row(requires=Path(sys.executable).name))

    def test_no_requirement_does_not_skip(self):
        _skip_if_unavailable(_control_row())


class TestArtifactIsDirShape:
    def test_it_blocks_the_named_artifact_and_nothing_else(self, tmp_path):
        row = _control_row(shapes=("artifact_is_dir",), artifact="summary.txt")
        out = _bad_path(tmp_path, "artifact_is_dir", row)

        assert out.is_dir(), "the output directory must be usable — the tool's own mkdir -p has to succeed"
        assert (out / "summary.txt").is_dir()
        with pytest.raises(IsADirectoryError):
            open(out / "summary.txt", "w", encoding="utf-8")
        # A sibling artefact still writes: this shape blocks ONE sink, which
        # is what lets a row reach a sink the mkdir-side shapes never touch.
        (out / "metrics-timeseries.csv").write_text("x\n", encoding="utf-8")

    def test_the_asserted_path_is_the_artifact_not_the_parent(self, tmp_path):
        row = _control_row(shapes=("artifact_is_dir",), artifact="summary.txt")
        out = _bad_path(tmp_path, "artifact_is_dir", row)
        assert _expected_fragment(row, "artifact_is_dir", out) == out / "summary.txt"

    def test_it_refuses_to_build_without_an_artifact(self, tmp_path):
        with pytest.raises(AssertionError):
            _bad_path(tmp_path, "artifact_is_dir", _control_row(shapes=("artifact_is_dir",)))

    def test_the_mkdir_side_shapes_still_assert_the_parent(self, tmp_path):
        row = _control_row()
        for shape in ("missing_parent", "parent_is_file"):
            root = tmp_path / shape
            root.mkdir()
            out = _bad_path(root, shape, row)
            assert not out.parent.is_dir(), shape
            assert _expected_fragment(row, shape, out) == out.parent


# ═══════════════════════════════════════════════════════════════════════════
# Staleness guards
# ═══════════════════════════════════════════════════════════════════════════
def test_rows_point_at_real_tools_and_fixtures():
    for row in ROWS:
        assert (TOOLS_DIR / row.tool).is_file(), row.tool
        assert row.kind in ("file", "dir"), row.id
        assert row.shapes, row.id
        assert set(row.shapes) <= set(SHAPES), f"{row.id}: unknown shape(s)"
        # The two fields are each other's precondition: a row that names an
        # artefact without driving the shape proves nothing about that sink,
        # and the shape without the artefact cannot build its bad path.
        assert (row.artifact is not None) == bool(_ARTIFACT_SHAPES & set(row.shapes)), (
            f"{row.id}: Row.artifact and an artefact-blocking shape "
            f"({sorted(_ARTIFACT_SHAPES)}) must be declared together")
    assert WAVEFORM_FIXTURE.is_file()
    assert WAVEFORM_TOLERANCES.is_file()
    assert THRESHOLDCONFIG_CR.is_file()
    assert (PAIRED_DATASET / "nights.json").is_file()
    for p in (_jsc.SEED_CONF_D, _jsc.RULE_PACKS, _jsc.K8S_MONITORING, _jsc.ALERTMANAGER_YML):
        assert p.exists(), p


def test_soak_metrics_stub_is_a_working_target():
    """The `/soak` personality of the shared stub really feeds run_chaos_soak.

    `dx/run_chaos_soak` aborts before the soak (rc=2, no output written) when
    `/metrics` is unreachable, and warns when nothing it tracks comes back —
    so a stub that answers 200 with the WRONG body would make its rows die on
    the reachability check instead of on the write under test, and the row
    would still be green. Parsed with the tool's own parser, not a copy.
    """
    import run_chaos_soak

    status, body, ctype = _jsc._stub_payload("/soak/metrics")
    assert status == 200 and ctype.startswith("text/plain")
    parsed = run_chaos_soak.parse_metrics(body.decode("utf-8"))
    assert set(parsed) == set(run_chaos_soak.TRACKED_METRICS), sorted(parsed)
    assert parsed["go_goroutines"] == 42.0, "labelled samples must be skipped"

    # And the pre-existing personalities are untouched: the plain exporter
    # body still carries no Go runtime metrics (discover_instance_mappings
    # reads these paths).
    for path in ("/metrics", "/rich/metrics"):
        _, plain, _ = _jsc._stub_payload(path)
        assert not run_chaos_soak.parse_metrics(plain.decode("utf-8"))
    assert b"schema=" in _jsc._stub_payload("/rich/metrics")[1]


def _tools_naming_a_flag() -> set[str]:
    """Tool modules under scripts/tools that pass ``flag=`` to a WRITER KEY —
    i.e. declare an argv-sourced output path at the point of writing.

    The keys are the secure writers, ``ensure_dir``, and the ``output_write``
    context manager (#1789), which is how a RAW sink (bare ``open``,
    ``mkdir``, ``shutil.copy2``) joins this population without changing the
    bytes it emits. See the module docstring for what this population cannot
    see: a raw sink that was never wrapped is not a missing row, it is
    absent."""
    import ast
    writers = {"write_text_or_die", "write_json_or_die", "ensure_dir_or_die", "ensure_dir",
               "write_text_secure", "write_json_secure", "write_yaml_crd",
               "write_onboard_hints", "output_write"}
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


# ⛔ Tools that DECLARE an output flag at their writer but cannot have a
# control pair here, each with the measured reason. Exit-locked like every
# other exception list in this ticket: `==` on the size, and an entry whose
# tool is no longer in the derived population (or which grew a row) is a
# failure, so the list can only shrink.
#
# ⚠️ An entry is NOT "this tool is covered by something else". It is "the
# behavioural gate is silent about this tool", and the only thing speaking
# for it is the static side (test_write_failure_class for a secure writer,
# test_output_write_sites_stay_guarded for a raw sink) — which sees the shape
# of the code, never the rc an operator gets.
NO_OFFLINE_ROW: dict[str, str] = {
    "dx/inject_waveform.py":
        "Its `--out` write is the LAST step of an injection run: the tool "
        "pushes a waveform into an isolated vmsingle over HTTP and then "
        "shells out to a `vmalert` binary with `-replay`. Neither exists in "
        "this repo's offline test environment (its own e2e tests skip on "
        "exactly that, tests/dx/test_inject_waveform.py), and every argv that "
        "gets as far as the write needs both. A `requires=` row would be a "
        "permanent skip that reads as a pass — and would still be wrong on a "
        "host that has `vmalert` but no vmsingle. #1789 gave it the shared "
        "`flag=\"--out\"` message anyway. What speaks for it instead: "
        "test_write_failure_class statically (its writer is "
        "`write_text_secure`), and behaviourally its own "
        "`test_out_write_failure_exits_two`, which stubs out the injection "
        "pipeline, calls `main()` IN PROCESS and asserts rc=2 plus the "
        "standard line. That is a weaker pair than a row — no subprocess, no "
        "control half — and it is the reason this entry exists rather than "
        "being silently absent.",
}
_NO_OFFLINE_ROW_CEILING = 1


def test_every_flag_naming_tool_has_a_row():
    """Derived, not enumerated: a tool that names an output flag in its
    writer must have a control pair here, or the row table has gone stale."""
    naming = _tools_naming_a_flag()
    assert naming, "no tool passes flag= to a secure writer — scanner drift"
    covered = {r.tool for r in ROWS}
    missing = sorted(naming - covered - set(NO_OFFLINE_ROW))
    assert not missing, (
        "tool(s) name an output flag in a secure writer but have no control "
        f"pair in ROWS: {missing}")


def test_the_no_offline_row_list_is_exact_and_not_stale():
    """The exception list's own gate: fixed size, every entry still in the
    population, none of them quietly grown a row (which would make the
    exception a lie), and every entry carrying a reason."""
    assert len(NO_OFFLINE_ROW) == _NO_OFFLINE_ROW_CEILING, sorted(NO_OFFLINE_ROW)
    naming = _tools_naming_a_flag()
    covered = {r.tool for r in ROWS}
    stale = []
    for tool, reason in sorted(NO_OFFLINE_ROW.items()):
        if not (TOOLS_DIR / tool).is_file():
            stale.append(f"{tool}: no such tool any more")
        elif tool not in naming:
            stale.append(f"{tool}: no longer names a flag at its writer — delete this entry")
        elif tool in covered:
            stale.append(f"{tool}: has a row now — delete this entry")
        elif not reason.strip():
            stale.append(f"{tool}: needs a reason")
    assert not stale, "\n".join(stale)
