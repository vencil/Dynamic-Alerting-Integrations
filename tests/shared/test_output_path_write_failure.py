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
Each shape fails for uid 0 as well as for a normal user:

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

THE TWO CLASSES (#3107) — ``Row.cls``
-------------------------------------
Every row says which kind of path its flag names. One rule (#3982, owner
2026-11-18), stated here and nowhere else:

* ``artifact`` — the tool reads the output back (measured: the control run,
  then the same command again, its ``--check``, and the row's ``readers``,
  under the audit hook logging reads), or its default is a file the repo
  tracks, or another program loads it whole (``EXTERNAL_READERS``, the one
  part no test can measure: only the owner adds to it, with the reason).
  Written by atomic replace (``_lib_io.write_repo_artifact``): a failed run
  leaves the old file whole.
* ``operator`` — everything else: the output only a person or a pipe reads.
  Opened in place by the kernel (``_lib_io.write_operator_output`` /
  ``open_operator_output``). An append is always operator.

``test_the_class_follows_the_read_back`` holds both directions: an operator
flag the tool reads back, and an artifact flag that neither the read-back
nor ``EXTERNAL_READERS`` explains, are faults. The rule replaced one that
asked for a tracked default and so needed five owner exceptions; every one
of them reads its output back or is on ``EXTERNAL_READERS``.

⚠️ NOT SEEN by the read-back measurement (independent review, #3982) — the
threat model is an honest regression in a Python tool, as for the write
audit: a read by a child that is not Python (``cat``, da-guard, git) or is
Python run with ``-I`` (the hook is not loaded); an ``os.open`` relative to
a ``dir_fd`` (the ``open`` event carries no ``dir_fd``); a read only on a
path the reruns do not take (output-dependent, or a command no row's
``readers`` names); a read through an ``a+`` open (logged as a write). A run
argparse refuses is not counted, and every row must have one counted run.

THE WRITE AUDIT (#3107) — WHICH ENTRY WROTE IT
----------------------------------------------
The control run is also the measurement of who wrote. It runs under
``_write_audit/sitecustomize.py``, an audit hook that logs every filesystem
change the tool and its Python children make, with the stack that made it.
After the run, every change UNDER THE OUTPUT PATH must have come through the
entry of the row's class (``_lib_io.OUTPUT_ENTRY_POINTS`` says which
functions those are; ``ensure_dir`` is the entry for a ``mkdir``).

"Under the output path" is the directory the control run hands the tool —
``good/``, which this test creates empty and which holds the flag's value
and nothing else. So it is the output FILE and anything put beside it (a
temp file, the parent's ``mkdir``), or the whole TREE of a directory flag —
every file in it, not one chosen as representative.

A write is attributed to an entry by the innermost entry frame on its
stack, and then held to the function that DOES that class's write: an
operator write must have been opened by ``_kernel_open_operator_output``, an
artifact's by ``_lib_atomic_write``. The entry's name on the stack is not
enough — an entry rewritten to open the path itself still carries it.

What is not counted, each for a stated reason and each with a test
(``TestWriteAuditPlacing``, ``TestTheHookItself``):

* an event on a descriptor that is no file (a pipe to a child);
* nothing else. A ``.pyc`` and ``/dev/null`` are not excluded, they are just
  not under ``good/``. The tool's ``TMPDIR`` is set to a directory beside
  ``good/``: pytest's ``tmp_path`` is itself under the system temp
  directory, so "it is a temp file" can never stand in for "it is elsewhere".

An event on a descriptor that IS a file (``fchmod``, ``ftruncate``,
``open(fd)``) or relative to a ``dir_fd`` is resolved by the hook and lands
where the file is. One the hook cannot place — an unresolvable descriptor, a
relative path given to ``os.open`` (the audit event carries no ``dir_fd``, so
it is relative to nobody-knows-what), a relative path in a working directory
that is gone — is a FAULT, not an event elsewhere.

What makes a row FAIL rather than pass on an empty log: no log at all, no
``loaded`` line from the tool's own process, no file written under the output
path, a ``hook-error`` line. And the log is held against three things it
cannot see into:

* **the disk.** A file the run left under the output path with NO logged
  write to it was put there by a process the hook cannot see. That is only
  accepted where the row says so — ``writer="da-guard"`` (a ``da-guard
  write`` child puts every file; Python must have opened none) or
  ``"python+da-guard"`` (both do) — and then the spawn has to be in the log.
  For a clean operator row every file's MODE is checked too (``Row.mode``):
  who opened a file says nothing about a later ``fchmod``.
* **child processes.** Every program a control run starts is listed, by row,
  in ``SPAWNS``. One that is not — a ``cp``, a ``chmod`` — fails the row, and
  so does a listed one that starts being handed a path under the output
  directory on its command line.
* **the code no control run reaches.** ``UNREACHED_WRITER_CALLS`` lists every
  writer call in a rowed tool's OWN module (not in the libraries it imports)
  that no control run's stack passed through on the way to an event under
  the output path: the audit says nothing about those, and the list says so
  by name.

Tools that do not write that way YET are on two lists, each exact in both
directions (``RAW_UNDER_OUT``, ``VIA_OTHER_CLASS``): they
record what the control runs logged, and an entry that is no longer true is
a failure telling you to remove it.

WHAT THIS GATE IS FOR, AND WHAT IT DOES NOT SEE
-----------------------------------------------
The threat is the HONEST REGRESSION: a write site the migration missed, one
put back to a bare write, a new tool that never went through an entry. It is
not a sandbox against code written to get past it. Everything below was
measured to pass, each needs writing on purpose, and none of it is to be
answered with more mechanism here — if one of these matters, it is a
different control (a review, a sandboxed runner), not another rule in this
file.

NOT SEEN:

* A child process on ``SPAWNS`` that writes the output itself while its
  command line does not name the path (it got it on stdin, in the
  environment, or computed it); and a ``python -I`` / ``-S`` child — it does
  not load the hook — overwriting a file that is ALREADY there. (A new file
  from either is caught: the disk is matched against the log.)
* A write to an existing output file through a symlink or a hard link that
  sits somewhere else: the event is logged where the link is.
* A write that raises no audit event (``sqlite3``, ``ctypes``, ``mmap`` of a
  descriptor opened for reading and writing elsewhere) onto an existing file.
* ``UNREACHED_WRITER_CALLS`` is keyed (tool, function, writer name) → a
  count. A second call of the same name on the same LINE as a reached one
  counts as reached; an unreached call swapped for a bare write, with a
  same-named call added under ``if False:`` in the same function, leaves the
  count where it was. And "unreached" means precisely "no event under the
  output path had a frame on that call's lines" — a call that ran and wrote
  elsewhere is on the list too.
* Writer calls in the LIBRARIES a rowed tool imports are not counted at all
  (today: one each in ``ops/_observed_map_lib.py`` and
  ``ops/_registry_lib.py``).
* The structural check compares frame NAMES: that the operator write came
  through a function called ``_kernel_open_operator_output``, the artifact's
  through ``_lib_atomic_write``. What those functions do inside —
  ``O_NOFOLLOW``, the ``fchmod``, the ``fsync`` — is
  ``test_lib_io_operator_output.py``'s and ``tests/dx/test_atomic_write.py``'s
  to hold, not this file's.
* Mode is checked for a CLEAN OPERATOR row only. A clean artifact row is not
  checked; a row on any of the three lists is not checked at all.
* "May only shrink" is, mechanically, "must equal what was measured". A
  change that adds a violation AND its list entry in one go passes every
  test; it is visible in the diff of this file, and nowhere else.

POPULATION — WHAT THIS TABLE CAN AND CANNOT SEE
----------------------------------------------
``test_every_flag_named_at_a_writer_has_a_row`` derives, from the tree, the
(tool, flag) pairs that pass ``flag=`` to one of the WRITER NAMES, in every
Python file of the repository that is not a test (``tests/_tree.repo_files``:
what git tracks) — every function ``_lib_io.OUTPUT_ENTRY_POINTS`` lists: the two classes' entries, the
old names, ``ensure_dir``, and (#1789) the ``output_write`` context manager
that wraps a raw sink. A tool that grows an output flag and names it in one
of those without adding a row goes red. So does a second flag on a tool that
already has a row for its first (#3107: the population used to be tools).
``test_every_output_flag_of_the_manifest_has_a_row`` adds the declaration
side for da-tools commands: a ``kind: output`` flag in
``caller_path_manifest.yaml`` must be a row.

⛔ The population is the writer names, nothing else. A tool that writes its
output with a RAW sink — a bare ``open(..., "w")``, ``Path.write_text``,
``os.makedirs``, ``shutil.copy2`` — and never routes it through a writer name
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

⛔ And one more hole, named rather than hidden: ``NO_ROW`` near the bottom of
this file lists (tool, flag) pairs that ARE in the derived population but
whose write no run here can reach — it needs infrastructure this test suite
does not have, or no argv gets to it at all.
An entry means "the behavioural gate is silent about this write", not "covered
elsewhere". It is exit-locked (fixed size, every entry re-checked against the
derived population) so it cannot quietly grow into the place where evidence
should be.
"""
from __future__ import annotations

import ast
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import pytest

import test_json_stdout_contract as _jsc  # noqa: E402  (tests/shared on sys.path)
from _platform_fs import DIR_WRITE_ERROR, has_posix_modes, symlink_or_skip
from _tree import repo_files
from _go_refusal import assert_refused  # (#3206)

# #2115: blind_spot_discovery 與 analyze_rule_pack_gaps（目錄模式）的值來自
# `da-guard served-values`；子行程經 `$DA_GUARD_BINARY` 找到本 repo 建出的 da-guard。
# #2476: `da_assembler --render-cr` 同理經 `$DA_CRDECODE_BINARY` 找到 da-crdecode。
pytestmark = pytest.mark.usefixtures("da_guard_env", "da_crdecode_env")

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


def _shard_source_dir(tmp: Path) -> str:
    """One `--sources` shard for `assemble_config_dir`: a flat conf.d."""
    d = tmp / "shard-a"
    if not d.is_dir():
        d.mkdir()
        # #3832: a default-armed key — the assembled output has no root
        # carrier, and an undeclared key there is unread (the write refused).
        (d / "alpha.yaml").write_text(
            "tenants:\n  alpha:\n    container_cpu: 100\n", encoding="utf-8")
    return str(d)


def _example_cr_for_a_rootless_dir(tmp: Path) -> str:
    """The in-repo example ThresholdConfig CR, minus its one key that needs
    the deployment's root carrier: `mysql_threads_running`, an operator key
    the platform ships no value for (the CR's own comment: "the key is
    declared" — by the deployment's defaults). The control run renders into
    a fresh `--config-dir` with no root file, which is judged (#3832), so an
    undeclared key there is unread and the write refused; every other key of
    the example is read in such a tree."""
    p = tmp / "example-thresholdconfig.yaml"
    if not p.is_file():
        text = THRESHOLDCONFIG_CR.read_text(encoding="utf-8")
        kept = [ln for ln in text.splitlines(keepends=True)
                if not ln.lstrip().startswith("mysql_threads_running:")]
        assert len(kept) == len(text.splitlines(keepends=True)) - 1, "example CR changed"
        p.write_text("".join(kept), encoding="utf-8")
    return str(p)


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


def _history_readers(c: "Ctx", out: Path) -> list[list[str]]:
    """`config_history`'s readers of its snapshots: `diff` reads the copies
    only where the two snapshots differ, so the shard is changed first — the
    rerun of the control argv then takes snapshot 2, and `diff 1 2` reads
    snapshot 1's copy, which the control run wrote (#3982)."""
    with (out / "alpha.yaml").open("a", encoding="utf-8") as fh:
        fh.write("# changed after the control run\n")
    return [["--config-dir", str(out), "diff", "1", "2"],
            ["--config-dir", str(out), "log"]]


def _inject_report(tmp: Path) -> str:
    """A synthetic `inject_waveform` report for `dx/waveform_score`.

    Built here rather than produced by `inject_waveform`, because that tool
    needs a live vmsingle plus a `vmalert` binary and cannot run offline —
    which is also why it has no row of its own (see `NO_ROW`).
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


def _plaintext_conf_d(tmp: Path) -> str:
    """`--config-dir` for `ops/migrate_credentials`: tenant `alpha` writes its
    Slack URL out in plain text — the one thing that makes the tool write a
    Secret manifest. Built per run under tmp: `--write` rewrites the file."""
    d = tmp / "plaintext-conf.d"
    if not d.is_dir():
        d.mkdir()
        (d / "alpha.yaml").write_text(
            "tenants:\n  alpha:\n    _routing:\n      receiver:\n        type: slack\n"
            "        api_url: https://hooks.slack.example.com/services/x\n",
            encoding="utf-8")
    return str(d)


def _sentinel_doc(out: Path, name: str) -> str:
    """`--target` for the two index generators: a document with an empty
    `<!-- {name}_START -->` … `<!-- {name}_END -->` block. Only written where
    the flag value can be a file — a bad-path shape that made it a directory
    keeps it one."""
    if out.parent.is_dir() and not out.exists():
        out.write_text(f"# doc\n\n<!-- {name}_START -->\n\n<!-- {name}_END -->\n",
                       encoding="utf-8")
    return str(out)


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
    cls: str                                    # "operator" | "artifact" — see THE TWO CLASSES
    build: Callable[[Ctx, Path], list[str]]     # (ctx, out_path) -> argv
    reach: str                                  # how the write is reached offline
    out_name: str = "out"
    cwd: Callable[[Ctx], Path] | None = None    # subprocess cwd, if any
    # Extra environment for the run, built from the context (#1525:
    # batch_diagnose reads its ConfigMap through a fake `kubectl` on PATH).
    env: Callable[[Ctx], dict[str, str]] | None = None
    # Bad-path shapes to drive. A FILE tool whose own mkdir -p creates the
    # parent (sync_glossary_abbr) legitimately succeeds on missing_parent, so
    # it lists parent_is_file only.
    shapes: tuple[str, ...] = ("missing_parent", "parent_is_file")
    # The head the tool prints in front of the shared ``cannot <action> …``
    # message, on the same line. The default is the one the shared emitter
    # (``_lib_io._die_on_write_error``) writes; a tool may replace it with a
    # head of its own when that head is a contract that predates this ticket:
    #   * ``waveform_score`` — ``ERROR [ERR_OUTPUT]: ``, the de-identified
    #     error code its ``--redact`` mode is documented to print in both
    #     modes (the SME triages by code without a re-run).
    # Declaring the head here is load-bearing in both directions — the line
    # must actually carry the declared one, so a tool that drops it OR swaps
    # it for another goes red rather than being matched by a looser regex.
    prefix: str = "ERROR: "
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
    # A second row for the same (tool, flag) that reaches the write by a
    # different terminal path (#3089: backtest's skip / no_changes) — part of
    # the id so the two stay distinguishable.
    variant: str = ""
    # Who puts the FILES under the output path: this Python process and its
    # Python children ("python"), a `da-guard write` child ("da-guard"), or
    # both ("python+da-guard"). The write audit cannot see inside a process
    # that is not Python, so every file the run leaves there is matched to a
    # logged write, and one that has none must be da-guard's — declared here,
    # with the spawn in the log — instead of passing on "no event was bad".
    writer: str = "python"
    # The exit code of the CONTROL run. 0 everywhere but where the tool's own
    # contract makes the run that writes a finding: `migrate_credentials`
    # writes its manifests exactly when it found a credential written out,
    # and that is its rc 1 ("the report is the answer").
    control_rc: int = 0
    # "shared": the bad path ends in the shared `cannot <action> <path>: … —
    # check the value given to <flag>` line. "own": the tool refuses the path
    # in a pre-check of its own, in its own words, before any writer runs —
    # still rc 2, still ONE `ERROR:` line naming the flag and the blocked
    # path, still no traceback (the contract at the top of this file), but
    # not the shared sentence.
    wording: str = "shared"
    # The mode the operator entry is asked for on this flag's files (see
    # `_mode_faults`). 0600 is the entry's default; a row says otherwise
    # where the tool does (`federation_keygen`'s JWKS is public: 0644).
    mode: int = 0o600
    # A defect in the tool that makes every bad-path case of this row fail,
    # named so the cases are strict xfails instead of a missing row: when the
    # tool is fixed they pass, the xfail turns red, and this comes off.
    known_defect: str | None = None
    # The tool's OTHER commands that read this output back, as argv for the
    # same tool (#3982 read-back rule): the control run and its rerun cannot
    # reach them (`config_history`'s `show` / `diff` read the snapshots).
    readers: Callable[[Ctx, Path], list[list[str]]] | None = None

    @property
    def id(self) -> str:
        base = f"{Path(self.tool).stem}[{self.flag}]"
        return f"{base}-{self.variant}" if self.variant else base


R = Row
ROWS: list[Row] = [
    # ── dx ─────────────────────────────────────────────────────────────────
    R("dx/describe_tenant.py", "-o/--output", "file", "operator",
      lambda c, out: ["--conf-d", str(_jsc.SEED_CONF_D), "--all", "-o", str(out)],
      reach="in-repo try-local seed conf.d; --all is what reaches the write",
      out_name="tenants.json"),
    R("dx/migrate_conf_d.py", "-o/--output-plan", "file", "operator",
      lambda c, out: ["--conf-d", str(_jsc.SEED_CONF_D), "-o", str(out)],
      reach="in-repo try-local seed conf.d (dry-run is the default)",
      out_name="migration-plan.json"),
    R("dx/scan_component_health.py", "--output", "file", "operator",
      lambda c, out: ["--output", str(out), "--today", "2026-09-08"],
      reach="in-repo docs/assets/tool-registry.yaml + tools/portal sources",
      out_name="component-health.json",
      # Its own `mkdir -p` creates the parent, so missing_parent is a
      # legitimate success. parent_is_file is what reaches the mkdir; the
      # write_text behind it is covered by the static pin, not by a row —
      # no shape here can make the write fail while the mkdir succeeds for
      # a FILE-shaped output flag.
      shapes=("parent_is_file",)),
    R("dx/compile_custom_alerts.py", "--out", "file", "artifact",
      lambda c, out: ["--config-dir", _custom_alerts_conf_d(c.tmp), "--out", str(out)],
      reach="tmp conf.d with one _custom_alerts threshold recipe",
      out_name="rule-pack-custom-alerts.yaml"),
    R("dx/generate_changelog.py", "-o/--output", "file", "operator",
      lambda c, out: ["-o", str(out)],
      reach="throw-away git repo with one conventional commit (cwd)",
      out_name="CHANGELOG-draft.md",
      cwd=lambda c: _git_repo_with_one_commit(c.tmp)),
    R("dx/generate_tenant_metadata.py", "--output", "file", "artifact",
      lambda c, out: ["--config-dir", str(_jsc.SEED_CONF_D), "--output", str(out)],
      reach="in-repo try-local seed conf.d",
      out_name="tenant-metadata.json",
      # `ensure_dir` creates a missing parent, so only the blocked parent
      # fails; the artifact entry's own failures name the same flag.
      shapes=("parent_is_file",)),
    # Three rows, one per `--layout`: each layout has its OWN
    # `output_dir.mkdir` (generate_flat / generate_hierarchical /
    # generate_synthetic_v2), so one row would only ever prove the default
    # one. The four sinks behind them — the two `slot_dir.mkdir`s and
    # `_write_yaml`'s mkdir + open — cannot be reached from here at all: the
    # tool refuses a non-empty output directory (rc=2, no ERROR line) before
    # any of them runs, which is what the `artifact_is_dir` shape would need.
    # They are wrapped, and the static pin is the only evidence for them.
    *[R("dx/generate_tenant_fixture.py", "-o/--output", "dir", "operator",
        (lambda layout: lambda c, out: ["--count", "2", "--layout", layout,
                                        "-o", str(out)])(_layout),
        reach=f"flags only; --layout {_layout}",
        out_name="conf.d", writer="da-guard", variant=_layout,
        shapes=("parent_is_file", "target_is_file"))
      for _layout in ("flat", "hierarchical", "synthetic-v2")],
    # `--summary-file` is why the population is derived from the WRITER and
    # not from `add_argument` names: a declaration-side scan for `--out*`
    # misses it entirely (measured). `missing_parent` and `parent_is_file`
    # reach the `open(..., "a")`; `target_is_dir` reaches the same call with a
    # different errno, and is kept because an APPEND to an existing path is
    # this tool's normal case (the CI runner's $GITHUB_STEP_SUMMARY already
    # exists) — the shape that most resembles the real one.
    R("dx/paired_trend_watch.py", "--summary-file", "file", "operator",
      lambda c, out: ["--dataset", str(PAIRED_DATASET), "--summary-file", str(out)],
      reach="the frozen six-night archival dataset in the repo; --dataset is "
            "the offline source (--from-gh would need the gh CLI)",
      out_name="paired-trend.md",
      shapes=("missing_parent", "parent_is_file", "target_is_dir")),
    R("dx/pair_bench_ratio.py", "--out", "file", "operator",
      lambda c, out: ["--reference", _bench_side(c.tmp, "ref"),
                      "--main", _bench_side(c.tmp, "main"),
                      "--reference-tag", "v1.0.0", "--out", str(out)],
      reach="two synthetic `go test -bench` transcripts with the same cpu: header",
      out_name="bench-paired.json",
      # `--out`'s own `mkdir -p` creates a missing parent, so only the
      # blocked-parent shape reaches a failure.
      shapes=("parent_is_file",)),
    R("dx/render_soak_diff.py", "--output", "file", "operator",
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
    R("dx/run_chaos_soak.py", "--output-dir", "dir", "operator",
      lambda c, out: ["--target-url", c.stub + "/soak",
                      "--config-dir", _soak_config_dir(c.tmp),
                      "--duration-min", "0", "--reload-interval-sec", "9999",
                      "--output-dir", str(out)],
      reach="the shared stub's /soak personality (Go-runtime /metrics body); "
            "the first-probe check is what a wrong stub would fail on",
      shapes=("parent_is_file", "target_is_file")),
    *[R("dx/run_chaos_soak.py", "--output-dir", "dir", "operator",
        lambda c, out: ["--target-url", c.stub + "/soak",
                        "--config-dir", _soak_config_dir(c.tmp),
                        "--duration-min", "0", "--reload-interval-sec", "9999",
                        "--output-dir", str(out)],
        reach=f"same; {_artefact} is blocked so the run dies at that sink",
        shapes=("artifact_is_dir",),
        artifact=_artefact, variant=_artefact)
      for _artefact in ("metrics-timeseries.csv", "summary.txt", "run-config.json")],
    R("dx/sync_glossary_abbr.py", "--output", "file", "artifact",
      lambda c, out: ["--glossary", _glossary(c.tmp), "--output", str(out)],
      reach="tmp glossary.md with two **ABBR (Expansion)** entries",
      out_name="abbreviations.md",
      shapes=("parent_is_file", "target_is_dir")),
    # A non-default `prefix`. This tool funnels every error through
    # `_emit_error`, which stamps a de-identified error CODE — a documented
    # `--redact` contract (the SME triages by code without a re-run, and under
    # `--redact` the path must not be printed at all). So the shared message
    # arrives behind `ERROR [ERR_OUTPUT]: ` instead of the shared `ERROR: `,
    # and declaring that head here means the tool cannot quietly drop either
    # half.
    R("dx/waveform_score.py", "--out", "file", "operator",
      lambda c, out: [_inject_report(c.tmp), "--tolerances", str(WAVEFORM_TOLERANCES),
                      "--out", str(out)],
      reach="a synthetic inject report whose one alert fires inside the fault "
            "window (verdict PASS ⇒ rc=0) plus the in-repo self-test tolerance "
            "matrix",
      out_name="waveform-score.json",
      prefix="ERROR [ERR_OUTPUT]: "),
    R("dx/waveform_compile.py", "--out", "dir", "operator",
      lambda c, out: ["--compile", "--out", str(out), "--allow-selftest",
                      str(WAVEFORM_FIXTURE)],
      reach="in-repo self-test waveform pack fixture",
      shapes=("parent_is_file", "target_is_file")),
    # ── lint ───────────────────────────────────────────────────────────────
    R("lint/trufflehog_to_sarif.py", "--output", "file", "operator",
      lambda c, out: ["--input", _trufflehog_ndjson(c.tmp), "--output", str(out)],
      reach="a one-finding NDJSON transcript; no trufflehog binary is needed, "
            "the tool only converts",
      out_name="trufflehog.sarif"),
    # ── ops: single-file outputs ───────────────────────────────────────────
    R("ops/analyze_rule_pack_gaps.py", "-o/--output", "file", "operator",
      lambda c, out: ["--config-dir", str(_jsc.SEED_CONF_D), "--output", str(out)],
      reach="in-repo try-local seed conf.d", out_name="gaps.json"),
    R("ops/backtest_threshold.py", "-o/--output", "file", "operator",
      lambda c, out: ["--tenant", "alpha", "--metric", "max_connections",
                      "--old-value", "100", "--new-value", "150",
                      "--prometheus", c.stub, "--output", str(out)],
      reach="local Prometheus stub (empty range vector)", out_name="backtest.json"),
    R("ops/backtest_threshold.py", "--markdown-output", "file", "operator",
      lambda c, out: ["--tenant", "alpha", "--metric", "max_connections",
                      "--old-value", "100", "--new-value", "150",
                      "--prometheus", c.stub, "--markdown-output", str(out)],
      reach="local Prometheus stub (empty range vector)", out_name="backtest.md"),
    # #3089: the two paths that run no backtest write both outputs too, and
    # an unwritable one is rc 2 there as well (it used to be rc 0, unwritten).
    *(R("ops/backtest_threshold.py", flag, "file", "operator",
        (lambda a, f: lambda c, out: [*a(c), f, str(out)])(argv, opt),
        reach=reach, out_name=name, variant=variant,
        shapes=("missing_parent", "parent_is_file", "target_is_dir"))
      for variant, argv, reach in (
          ("skip", lambda c: ["--tenant", "alpha", "--metric", "max_connections",
                              "--old-value", "100", "--new-value", "150",
                              "--prometheus", "http://127.0.0.1:1",
                              "--skip-if-unavailable"],
           "--skip-if-unavailable against a port nothing listens on"),
          ("no-changes", lambda c: ["--config-dir", str(_jsc.SEED_CONF_D),
                                    "--baseline", str(_jsc.SEED_CONF_D),
                                    "--prometheus", c.stub],
           "in-repo seed conf.d as both trees (zero changes) + local Prometheus stub"))
      for flag, opt, name in (("-o/--output", "--output", "backtest.json"),
                              ("--markdown-output", "--markdown-output", "backtest.md"))),
    R("ops/batch_diagnose.py", "-o/--output", "file", "operator",
      lambda c, out: ["--tenants", "db-a,db-b", "--prometheus", c.stub,
                      "--output", str(out)],
      reach="local Prometheus stub + the --json gate's fake kubectl (#1525)",
      out_name="health.json", env=lambda c: _fake_kubectl_env(c.tmp)),
    R("ops/discover_instance_mappings.py", "-o/--output", "file", "operator",
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
    # #3982 (owner 2026-11-18): artifact — `history.json` is read back on
    # every run and the `snap-N/` copies by `show` / `diff`; both through
    # `write_repo_artifact(mode=0o600)`, the directories through `ensure_dir`.
    R("ops/config_history.py", "--config-dir", "dir", "artifact",
      lambda c, out: ["--config-dir", _history_conf_d(out), "snapshot", "-m", "x"],
      reach="a one-shard conf.d built in place; `snapshot` is what writes",
      readers=_history_readers,
      out_name="conf.d", artifact=".da-history", variant="history-dir",
      shapes=("sibling_is_file",),
      produces=lambda out: out.parent / ".da-history" / "history.json"),
    R("ops/config_history.py", "--config-dir", "dir", "artifact",
      lambda c, out: ["--config-dir", _history_conf_d(out), "snapshot", "-m", "x"],
      reach="same, with the snapshot subdirectory blocked",
      readers=_history_readers,
      out_name="conf.d", artifact=".da-history/snap-1", variant="snapshot-dir",
      shapes=("sibling_is_file",),
      produces=lambda out: out.parent / ".da-history" / "history.json"),
    R("ops/config_history.py", "--config-dir", "dir", "artifact",
      lambda c, out: ["--config-dir", _history_conf_d(out), "snapshot", "-m", "x"],
      reach="same, with the snapshot COPY of alpha.yaml blocked",
      readers=_history_readers,
      out_name="conf.d", artifact=".da-history/snap-1/alpha.yaml",
      variant="snapshot-file", shapes=("sibling_is_dir",),
      produces=lambda out: out.parent / ".da-history" / "history.json"),
    # #3982: artifact by the read-back rule — the next run reads the files
    # this one rendered to decide which are its own (`_rendered_cr_id`).
    R("ops/da_assembler.py", "--config-dir", "dir", "artifact",
      lambda c, out: ["--render-cr", _example_cr_for_a_rootless_dir(c.tmp),
                      "--config-dir", str(out)],
      reach="in-repo example ThresholdConfig CR (minus its operator key, #3832) through --render-cr; the "
            "offline path needs no Kubernetes client",
      # ⛔ This tool used to exit 0 here having written nothing: `reconcile_one`
      # logged the OSError and `render_cr_file` returned EXIT_OK regardless
      # (measured rc=0 on both shapes). `parent_is_file` reaches the mkdir.
      # ⛔ No `artifact_is_dir` since #3206: a directory named `db-a.yaml` in
      # the existing --config-dir is a path da-guard validation names (exit
      # 4, a directory with a config file name), so the shared conf.d
      # refusal ends the run (rc 2, that path named — the behaviour matrix's
      # `config_named_dir` row) before any sink is reached; no CLI run can
      # put a directory where the rendered file goes. The sinks behind it
      # (`write_text` + `chmod`) were already masked from this row and are
      # held by the static pin.
      shapes=("parent_is_file", "target_is_file"), writer="da-guard"),
    R("ops/state_reconcile.py", "--manifest-path", "file", "artifact",
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
    R("ops/federation_keygen.py", "--jwks-out", "file", "operator",
      lambda c, out: ["--jwks-out", str(out)],
      reach="flags only; the private key goes to stdout (a pipe, never a tty) "
            "and the JWKS to --jwks-out — needs openssl for the keypair",
      out_name="federation-jwks.json",
      requires="openssl",
      # The JWKS is the PUBLIC half: the tool asks the entry for 0644 (#1789).
      mode=0o644),
    R("ops/generate_alertmanager_routes.py", "-o/--output", "file", "artifact",
      lambda c, out: ["--config-dir", str(_jsc.SEED_CONF_D), "--output-alertmanager",
                      "-o", str(out)],
      reach="in-repo try-local seed conf.d; --output-alertmanager writes the "
            "merged alertmanager.yml to -o",
      out_name="alertmanager-routes.yaml"),
    # #1560: the mount list is a second sink, written after -o; -o is a
    # writable tmp file so the run reaches it.
    R("ops/generate_alertmanager_routes.py", "--secret-mounts-out", "file", "operator",
      lambda c, out: ["--config-dir", str(_jsc.SEED_CONF_D), "--output-alertmanager",
                      "-o", str(c.tmp / "alertmanager.yml"), "--secret-mounts-out", str(out)],
      reach="same seed conf.d; -o writable, so the run reaches the mount-list write",
      out_name="secret-mounts.json"),
    # #3107: the findings document is its own sink, written on every exit of
    # every mode; --validate needs no -o, so this flag is the run's only file.
    R("ops/generate_alertmanager_routes.py", "--findings-json", "file", "operator",
      lambda c, out: ["--config-dir", str(_jsc.SEED_CONF_D), "--validate",
                      "--findings-json", str(out)],
      reach="same seed conf.d; --validate renders nothing to a file, so the "
            "findings document is the only write",
      out_name="findings.json"),
    R("ops/generate_tenant_mapping_rules.py", "-o/--output", "file", "operator",
      lambda c, out: ["--config-dir", _mapping_conf_d(c.tmp), "--metrics", "mysql_up",
                      "-o", str(out)],
      reach="tmp conf.d with a two-tenant _instance_mapping.yaml",
      out_name="mapping-rules.yaml"),
    R("ops/assemble_config_dir.py", "--output", "dir", "operator",
      lambda c, out: ["--sources", _shard_source_dir(c.tmp), "--output", str(out)],
      reach="one tmp conf.d shard with a single tenant file",
      # `--output` and `--manifest` are two flags on two different paths, so
      # they get a row each. Only the `mkdir` is the tool's own sink here:
      # since #3206 the files are put in place by `da-guard write` (a copy
      # batch), whose failures are its own named refusals, not this flag's.
      shapes=("parent_is_file", "target_is_file"), writer="da-guard"),
    R("ops/assemble_config_dir.py", "--manifest", "file", "operator",
      lambda c, out: ["--sources", _shard_source_dir(c.tmp),
                      "--output", str(c.tmp / "assembled"), "--manifest", str(out)],
      reach="same shard; `--output` is a writable tmp dir so the run reaches "
            "the manifest write, which is the sink under test",
      out_name="assembly-manifest.json",
      # The tool asks the entry for 0644 on this one.
      mode=0o644),
    R("ops/baseline_discovery.py", "-o/--output-dir", "dir", "operator",
      lambda c, out: ["--tenant", "t1", "--prometheus", c.stub,
                      "--duration", "1", "--interval", "1", "-o", str(out)],
      reach="the stub Prometheus answers every query (an empty vector), so the "
            "run exits 0 having written both CSVs (#3133: a closed port is a "
            "query not answered, rc 2)",
      # Two rows' worth of sinks in one tool: `parent_is_file` reaches the
      # `os.makedirs`, `artifact_is_dir` gets past it and reaches the
      # `open(..., 'wb')` + `chmod` inside `_write_csv_secure`. The FIRST CSV
      # is the artefact — the summary write behind it is masked, as the
      # module docstring says, and only the static pin speaks for it.
      shapes=("parent_is_file", "target_is_file", "artifact_is_dir"),
      artifact="baseline-t1-timeseries.csv"),
    # The convergence report of a --watch run: its own flag, its own path
    # (it lands under -o when the flag is absent — that is the -o row's).
    R("ops/validate_migration.py", "--convergence-output", "file", "operator",
      lambda c, out: ["--old", "up", "--new", "up", "--prometheus", c.stub + "/rich",
                      "--watch", "--rounds", "2", "--interval", "0",
                      "--auto-detect-convergence", "--stability-window", "2",
                      "-o", str(c.tmp / "validation"), "--convergence-output", str(out)],
      reach="local Prometheus stub (/rich: identical vectors ⇒ match); two watch "
            "rounds with a stability window of two converge (rc 0) and write the "
            "report",
      out_name="convergence.json",
      # Its own `mkdir -p` creates a missing parent.
      shapes=("parent_is_file", "target_is_dir")),
    # ── ops: output directories ────────────────────────────────────────────
    # #1560 ⑤-a: one Secret manifest per tenant, named after it, with --write.
    # Two things here are this tool's own contract, declared rather than
    # worked around: it refuses a bad DIR in a pre-check of its own ("nothing
    # was written" — the conf.d rewrite and the manifests go together, so the
    # refusal comes before either), and the run that writes manifests is the
    # run that found a credential written out, rc 1.
    R("ops/migrate_credentials.py", "--manifests-out", "dir", "operator",
      lambda c, out: ["--config-dir", _plaintext_conf_d(c.tmp), "--manifests-out", str(out),
                      "--write"],
      reach="a tmp conf.d whose one tenant writes its Slack URL out; --write "
            "rewrites that file (da-guard) and writes the tenant's Secret manifest",
      out_name="secrets", artifact="da-alpha.yaml",
      shapes=("target_is_file", "artifact_is_dir"),
      wording="own", control_rc=1),
    # #3982: artifact by the read-back rule — a `--force` run reads the
    # ownership ledger (`.da-init.yaml`) and the files the last run wrote.
    # Still written through the operator entry: VIA_OTHER_CLASS.
    R("ops/init_project.py", "-o/--output-dir", "dir", "artifact",
      lambda c, out: ["-o", str(out), "--non-interactive", "--tenants", "alpha",
                      "--rule-packs", "mariadb"],
      reach="non-interactive flags only",
      # Its rerun is refused (the path is occupied); `--force` is the run
      # that reads the last one's ledger back.
      readers=lambda c, out: [["-o", str(out), "--non-interactive", "--tenants", "alpha",
                               "--rule-packs", "mariadb", "--force"]],
      # The conf.d files in it are `da-guard write`'s; the rest is Python's.
      shapes=("parent_is_file", "target_is_file"), writer="python+da-guard"),
    # #3107: the two mkdir shapes above die at `ensure_dir_or_die`, so they
    # say nothing about the WRITE (`_write_file`, the one call every file of
    # the scaffold goes through). A directory where a file goes is only
    # reached with `--force`: without it init refuses a path that is already
    # occupied in a pre-check of its own (rc 1).
    R("ops/init_project.py", "-o/--output-dir", "dir", "artifact",
      lambda c, out: ["-o", str(out), "--non-interactive", "--tenants", "alpha",
                      "--rule-packs", "mariadb", "--force"],
      reach="the same flags with --force, so an occupied path is written over",
      shapes=("artifact_is_dir",), artifact=".pre-commit-config.da.yaml",
      variant="force", writer="python+da-guard"),
    R("ops/migrate_rule.py", "-o/--output-dir", "dir", "operator",
      lambda c, out: [_legacy_rules(c.tmp), "--tenant", "t1", "-o", str(out)],
      reach="tmp legacy Prometheus rules file",
      # The conf.d files in it are `da-guard write`'s; the rest is Python's.
      # #3107: `artifact_is_dir` reaches one of the five writes after the
      # mkdirs — the report. All five calls are driven one by one, in
      # process, by tests/ops/test_migrate_rule_outputs.py.
      shapes=("parent_is_file", "target_is_file", "artifact_is_dir"),
      artifact="migration-report.txt", writer="python+da-guard"),
    R("ops/migrate_to_operator.py", "--output-dir", "dir", "operator",
      lambda c, out: ["--source-dir", str(_jsc.K8S_MONITORING), "--output-dir", str(out)],
      reach="in-repo k8s/03-monitoring",
      # #3107: `artifact_is_dir` gets past the mkdir to the checklist write
      # (the last file); the CRD write before it has the row below.
      shapes=("parent_is_file", "target_is_file", "artifact_is_dir"),
      artifact="MIGRATION-CHECKLIST.md"),
    # The CRD files go through the library writer `_lib_yaml.write_yaml_crd`,
    # which raises; `main()`'s handler makes it rc 2. One of the CRDs the
    # in-repo ConfigMaps turn into is the artefact.
    R("ops/migrate_to_operator.py", "--output-dir", "dir", "operator",
      lambda c, out: ["--source-dir", str(_jsc.K8S_MONITORING), "--output-dir", str(out)],
      reach="same; a CRD file is blocked so the run dies in write_yaml_crd",
      shapes=("artifact_is_dir",), variant="crd",
      artifact="da-rule-pack-prometheus-rules-mariadb.yaml"),
    R("ops/onboard_platform.py", "-o/--output-dir", "dir", "operator",
      lambda c, out: ["--alertmanager-config", str(_jsc.ALERTMANAGER_YML), "-o", str(out)],
      reach="in-repo try-local alertmanager.yml",
      # #3107: `artifact_is_dir` gets past the mkdirs to the one write this
      # argv reaches (Phase 1's summary). The Phase 2 / Phase 3 writes and the
      # hints file are driven by tests/ops/test_onboard_platform.py.
      shapes=("parent_is_file", "target_is_file", "artifact_is_dir"),
      artifact="phase1-routing/routing-summary.csv"),
    R("ops/scaffold_tenant.py", "-o/--output-dir", "dir", "operator",
      lambda c, out: ["--tenant", "alpha", "--db", "mariadb", "--non-interactive",
                      "-o", str(out)],
      reach="non-interactive flags only",
      # The conf.d files in it are `da-guard write`'s; the rest is Python's.
      # #3107: `artifact_is_dir` reaches the report write behind the batch.
      shapes=("parent_is_file", "target_is_file", "artifact_is_dir"),
      artifact="scaffold-report.txt", writer="python+da-guard"),
    R("ops/validate_migration.py", "-o/--output-dir", "dir", "operator",
      lambda c, out: ["--old", "up", "--new", "up", "--prometheus", c.stub + "/rich",
                      "-o", str(out)],
      reach="local Prometheus stub (/rich: identical vectors ⇒ match)",
      # #3107: `artifact_is_dir` gets past the mkdir to the CSV write.
      shapes=("parent_is_file", "target_is_file", "artifact_is_dir"),
      artifact="validation-report.csv"),
    R("ops/generate_rule_pack_split.py", "--output-dir", "dir", "operator",
      lambda c, out: ["--rule-packs-dir", _one_rule_pack_dir(c.tmp), "--output-dir", str(out)],
      reach="tmp rule-packs dir holding a copy of rule-pack-mariadb.yaml",
      # `parent_is_file` dies at the first `_safe_mkdir` (edge-rules/), which
      # is already an `ensure_dir_or_die`. `artifact_is_dir` gets past both
      # mkdirs AND past the two split writes, and reaches the LAST raw sink
      # in the file — the `open(validation-report.json, "w")` that #1789
      # wrapped. Without this second shape the row would still be green with
      # that wrapper deleted.
      shapes=("parent_is_file", "target_is_file", "artifact_is_dir"),
      artifact="validation-report.json"),
    # #3107: and the split files themselves (`_safe_write`, an `_or_die` call
    # inside the per-pack `try … except Exception`): the edge file is written
    # first, so a directory in its place is that call's failure.
    R("ops/generate_rule_pack_split.py", "--output-dir", "dir", "operator",
      lambda c, out: ["--rule-packs-dir", _one_rule_pack_dir(c.tmp), "--output-dir", str(out)],
      reach="same; the pack's edge file is blocked so the run dies in _safe_write",
      shapes=("artifact_is_dir",), variant="split-file",
      artifact="edge-rules/rule-pack-mariadb.yaml"),
    R("ops/operator_generate.py", "--output-dir", "dir", "operator",
      lambda c, out: ["--rule-packs-dir", str(_jsc.RULE_PACKS), "--output-dir", str(out)],
      reach="in-repo rule-packs",
      # #3107: `artifact_is_dir` gets past the mkdir into the library writer
      # `_lib_yaml.write_yaml_crd`, which raises; `main()`'s handler around
      # `write_crds` is what makes that rc 2.
      shapes=("parent_is_file", "target_is_file", "artifact_is_dir"),
      artifact="da-rule-pack-mariadb.yaml"),
    # ── scripts/ops (outside scripts/tools; `tool` is relative to it) ──────
    R("../ops/configmap_assemble.py", "--output", "file", "artifact",
      lambda c, out: ["--config-dir", str(_jsc.SEED_CONF_D), "--output", str(out)],
      reach="in-repo try-local seed conf.d through a stand-in `kubectl` that "
            "builds the ConfigMap from --from-file (test_configmap_assemble's)",
      out_name="configmap.yaml",
      env=lambda c: _kubectl_shim_env(c.tmp),
      shapes=("parent_is_file", "target_is_dir")),
    # ── scripts/dx: the two index generators rewrite a sentinel block INSIDE
    # --target, so the target is read before it is written and must already
    # be a document holding the two sentinel lines.
    R("../dx/generate_planning_index.py", "--target", "file", "artifact",
      lambda c, out: ["--write", "--target", _sentinel_doc(out, "PLANNING_INDEX")],
      reach="a tmp doc holding an empty sentinel block; the repo's own planning "
            "sources fill it, so the run rewrites the doc",
      out_name="planning-index.md",
      # The only shape that reaches the tool's own refusal: every other one
      # leaves no document to read. (As root a read-only target is rewritten
      # all the same, so no shape reaches a write that fails AFTER the read.)
      shapes=("target_is_dir",)),
    R("../dx/generate_adr_index.py", "--target", "file", "artifact",
      lambda c, out: ["--write", "--target", _sentinel_doc(out, "ADR_INDEX")],
      reach="a tmp doc holding an empty sentinel block; the repo's own ADRs "
            "fill it, so the run rewrites the doc",
      out_name="architecture-and-design.md",
      shapes=("target_is_dir",),
      known_defect="generate_adr_index reads --target with no check in front of the "
                   "read (generate_planning_index calls check_repo_artifact first): a "
                   "target that is a directory ends as `unexpected internal error "
                   "(IsADirectoryError)`, not the write-failure line naming --target"),
    R("../ops/audit_rules_drift.py", "--out", "file", "operator",
      lambda c, out: ["--out", str(out)],
      reach="the repo's own rule corpus; the memory directory is absent off a "
            "maintainer machine, which the tool reports and carries on from",
      out_name="rules-drift.md",
      # Its own `mkdir -p` creates a missing parent.
      shapes=("parent_is_file", "target_is_dir")),
]


def _skip_if_unavailable(row: Row) -> None:
    if row.requires and shutil.which(row.requires) is None:
        pytest.skip(f"{row.id}: {row.requires} is not on PATH")


def _fake_kubectl_env(tmp: Path) -> dict[str, str]:
    """PATH with the --json gate's fake `kubectl` first (POSIX launcher; its
    threshold-config declares db-a and db-b)."""
    bindir = tmp / "fake-kubectl-bin"
    bindir.mkdir(parents=True, exist_ok=True)
    impl = bindir / "fake_kubectl.py"
    impl.write_text(_jsc._FAKE_KUBECTL_PY, encoding="utf-8")
    exe = bindir / "kubectl"
    exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{impl}" "$@"\n',
                   encoding="utf-8", newline="\n")
    exe.chmod(0o755)
    return {"PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}"}


def _kubectl_shim_env(tmp: Path) -> dict[str, str]:
    """PATH with test_configmap_assemble's stand-in `kubectl` first (it builds
    the ConfigMap YAML from `--from-file`), plus the opt-in the tool wants
    before it reads a conf.d inside the repo."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_cmasm_tests", REPO_ROOT / "tests" / "ops" / "test_configmap_assemble.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    bindir = tmp / "kubectl-shim"
    bindir.mkdir(parents=True, exist_ok=True)
    exe = bindir / "kubectl"
    exe.write_text(mod._SHIM.format(python=sys.executable), encoding="utf-8")
    exe.chmod(0o755)
    return {"PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}",
            "ALLOW_SAMPLE_CONFDIR": "1"}


def _files_under(scope: Path) -> frozenset[str]:
    """Everything under *scope* that is not a directory — a FIFO, a device
    node and a dangling symlink too — relative to it."""
    return frozenset(p.relative_to(scope).as_posix() for p in scope.rglob("*")
                     if p.is_symlink() or not p.is_dir())


def _run(row: Row, ctx: Ctx, out: Path, *,
         audit_log: Path | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    if row.env is not None:
        if os.name == "nt":
            pytest.skip(f"{row.id}: its fake executable is a POSIX launcher")
        env.update(row.env(ctx))
    if audit_log is not None:
        env.update(_audit_env(audit_log, ctx.tmp))
    cwd = row.cwd(ctx) if row.cwd else None
    argv = row.build(ctx, out)
    # After the row's fixtures, before the tool: what is already under the
    # control run's output directory is the fixture's, not the run's.
    before = _files_under(out.parent) if audit_log is not None else frozenset()
    proc = subprocess.run(
        [sys.executable, str(TOOLS_DIR / row.tool), *argv],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=TIMEOUT_S, env=env, cwd=cwd,
    )
    proc.files_before = before
    proc.run_cwd = cwd
    return proc


SHAPES = ("missing_parent", "parent_is_file", "target_is_dir",
          "target_is_file", "artifact_is_dir", "sibling_is_file",
          "sibling_is_dir")
# Shapes that pre-create ``Row.artifact`` and therefore require it.
_ARTIFACT_SHAPES = frozenset({"artifact_is_dir", "sibling_is_file",
                             "sibling_is_dir"})
# The two shapes that corrupt the flag value's PARENT.
_PARENT_SHAPES = frozenset({"missing_parent", "parent_is_file"})
# Shapes a ``kind="dir"`` tool answers by failing at a ``mkdir``: either at
# the flag value itself, or at the first subdirectory it makes inside it.
_DIR_MKDIR_SHAPES = _PARENT_SHAPES | {"target_is_file"}


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
    if shape == "target_is_file":
        # The mirror of the above, for a `--output-dir` tool: the flag value
        # is a regular FILE. Its `mkdir -p` fails, but so does anything that
        # LISTS the directory first — and that is the point. A pre-check
        # ("is it already populated?") runs before every write site, so it is
        # where such a tool actually dies; neither parent shape can see it,
        # because they kill the tool at its own mkdir first. Measured on
        # dx/generate_tenant_fixture as a NotADirectoryError traceback at
        # rc=1 with every wrapped write site below it unreached (#1789).
        out = tmp / row.out_name
        out.write_text("this is a file, not a directory\n", encoding="utf-8")
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


# The verbs ``_lib_io.OutputWriteError`` can put in ``cannot <action> …``.
# Longest-first so ``create directory`` is not eaten by a prefix.
_ACTIONS = ("create directory", "copy into", "append to", "inspect",
            "write", "append")


def _named_path(line: str, prefix: str) -> tuple[str, str]:
    """The ``(action, path)`` the standard line reports, parsed out of it.

    ``<prefix>cannot <action> <path>: <detail> — <hint>``.

    ⛔ Parsed, not substring-matched, because the parent-side shapes cannot
    otherwise be told apart: ``str(out.parent)`` is a PREFIX of ``str(out)``,
    so ``str(out.parent) in line`` was satisfied by a line that named the
    TARGET. That is how six sites (#1789 F6) shipped a mkdir wrapped with the
    output FILE as its path, printing "cannot create directory
    <my-report.json>" — a sentence about a path nobody was creating — with
    every row still green.
    """
    body = line[len(prefix):]
    assert body.startswith("cannot "), line
    body = body[len("cannot "):]
    for action in _ACTIONS:
        if body.startswith(action + " "):
            body = body[len(action) + 1:]
            break
    else:
        raise AssertionError(f"no known action verb in {line!r}")
    path, sep, _ = body.partition(": ")
    assert sep, line
    return action, path


def _expected_fragment(row: Row, shape: str, out: Path,
                       action: str = "write") -> Path:
    """The path the error line must name, per shape AND per reported verb.

    For ``artifact_is_dir`` / the sibling shapes the whole point is WHICH sink
    died, so the artefact path itself is required. For the two parent-side
    shapes the answer follows the verb the tool used, which is the tool's own
    statement about what it was doing:

    * ``create directory`` on a ``kind="file"`` row ⇒ the directory being
      created is the output file's PARENT;
    * anything else, and every ``kind="dir"`` row (where the flag value IS
      the directory the tool creates) ⇒ the flag value itself.
    """
    if shape in ("target_is_dir", "target_is_file"):
        return out
    if shape == "artifact_is_dir":
        return out / row.artifact
    if shape in ("sibling_is_file", "sibling_is_dir"):
        return out.parent / row.artifact
    if action == "create directory" and row.kind == "file":
        return out.parent
    return out


def _names_the_blocked_path(row: Row, shape: str, out: Path,
                            action: str, named: str) -> bool:
    """Is *named* — parsed out of the error line — the path this shape blocks?

    Exact, with ONE documented widening. A ``kind="dir"`` tool may create a
    SUBDIRECTORY of its output directory before anything else
    (``generate_rule_pack_split`` makes ``edge-rules/``, ``init_project`` its
    scaffold tree), and under a blocked parent that inner mkdir is the one
    that fails. It is still the operator's flag value, one level in.

    ⛔ The widening is gated on ``kind="dir"``, so it cannot rescue a FILE
    row: there ``out.parent`` and ``out`` are what have to be told apart, and
    ``out`` IS a descendant of ``out.parent`` — allowing descendants would
    hand back exactly the hole the mkdir-path defect came out of. It is also
    gated on the ``create directory`` verb, so the ``inspect`` / ``write``
    verbs still have to name the flag value exactly.
    """
    expected = _expected_fragment(row, shape, out, action)
    if named == str(expected):
        return True
    if (row.kind == "dir" and action == "create directory"
            and shape in _DIR_MKDIR_SHAPES):
        return expected in Path(named).parents
    return False


def _error_lines(stderr: str, prefix: str) -> list[str]:
    """The tool's standard write-error line(s) in *stderr*, *prefix* included.

    A row's ``prefix`` is an assertion that the tool emits exactly that head:
    the line must carry it AND the shared ``cannot <action> …`` text right
    after it. Declaring a head therefore cannot loosen the match, and a tool
    that swaps the default ``ERROR: `` for one its row does not declare stops
    matching — both directions are a visible red, which is the point of
    putting the head in the table instead of in the regex (rulebook D-05g: a
    declared classification can be declared wrong).
    """
    return [line for line in stderr.splitlines()
            if line.startswith(prefix)
            and line[len(prefix):].startswith("cannot ")]


def _own_refusal_problem(row: Row, shape: str, out: Path, stderr: str) -> str | None:
    """What is wrong with a `wording="own"` row's refusal, or None.

    Held to the contract at the top of this file minus the shared sentence:
    exactly one line with the row's head, and that line names the flag and
    the path this shape blocks. The path must START and END there — nothing
    a path could continue with on either side — so a line about
    `<out>/da-x.yaml`, or about `/elsewhere<out>`, does not pass for a shape
    that blocks `<out>` itself (the substring hole `_named_path` describes)."""
    lines = [line for line in stderr.splitlines() if line.startswith(row.prefix)]
    if len(lines) != 1:
        return f"expected exactly one {row.prefix!r} line, got {len(lines)}"
    line = lines[0]
    if "cannot " in line and "check the value given to" in line:
        return ("the line is the shared sentence — the row should say "
                'wording="shared" and be held to it')
    if row.flag not in line:
        return f"the refusal does not name {row.flag}"
    blocked = str(_expected_fragment(row, shape, out))
    if not re.search(r"(?<![\w./\\-])" + re.escape(blocked) + r"(?![\w./\\-])", line):
        return f"the refusal does not name the blocked path {blocked}"
    return None


def _fail(row: Row, proc: subprocess.CompletedProcess, why: str) -> str:
    return (f"{row.id}: {why}\n  argv : {proc.args[1:]}\n  exit : {proc.returncode}\n"
            f"  stdout:\n{proc.stdout[-1500:]}\n  stderr:\n{proc.stderr[-1500:]}")


# ═══════════════════════════════════════════════════════════════════════════
# The write audit (#3107): which entry each write under the output path came
# from. The control run is the measurement; everything here reads its log.
# ═══════════════════════════════════════════════════════════════════════════
AUDIT_DIR = Path(__file__).resolve().parent / "_write_audit"
CLASSES = ("operator", "artifact")
WRITERS = ("python", "da-guard", "python+da-guard")
#: Events that put a file's content or name in place (a `mkdir` does neither;
#: `os.rename-from` — the name a rename takes AWAY — is an event of its own,
#: attributed like any other, and puts nothing).
_FILE_EVENTS = frozenset({"open", "os.rename", "os.link", "os.symlink", "os.truncate"})


def _audit_env(log: Path, tmp: Path) -> dict[str, str]:
    """The environment that loads the hook into the tool and its Python
    children. ``TMPDIR`` is a directory BESIDE the output path, never the
    default: pytest's ``tmp_path`` is itself under the system temp directory,
    so "under the temp directory" would otherwise also say "under the output
    path", and a tool's scratch file could not be told from its output."""
    scratch = tmp / "tool-tmp"
    scratch.mkdir(exist_ok=True)
    return {"DA_WRITE_AUDIT_LOG": str(log), "TMPDIR": str(scratch),
            "PYTHONPATH": os.pathsep.join(
                [str(AUDIT_DIR), *filter(None, [os.environ.get("PYTHONPATH")])])}


def _read_audit_log(log: Path) -> list[dict] | None:
    """The events, or None when there is no log at all (the hook never ran)."""
    if not log.is_file():
        return None
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def _entry_frames() -> dict[str, str]:
    """``<file relative to the repo>:<function>`` → what kind of entry it is,
    for every function `_lib_io.OUTPUT_ENTRY_POINTS` names as one that
    writes. Read from the library, not listed here a second time."""
    import _lib_io
    frames = {}
    for kind, names in _lib_io.OUTPUT_ENTRY_POINTS.items():
        if kind in ("names_flag_only", "wrapper"):
            continue  # neither opens anything: the write under them is another frame's
        for name in names:
            module, func = name.split(".")
            frames[f"scripts/tools/{module}.py:{func}"] = kind
    return frames


def _via(event: dict, frames: dict[str, str]) -> str:
    """The kind of the INNERMOST entry on the event's stack, or ``raw``.
    Innermost: `write_yaml_crd` → `write_operator_output` is an operator
    write, and stays one whatever is above it."""
    for frame in event["stack"]:
        if frame in frames:
            return frames[frame]
    return "raw"


#: The two functions that DO the write of each class, below the public
#: entries. A write attributed to a class must have come through its one:
#: an `open_operator_output` rewritten to `io.open(path, "w")`, or a
#: `write_repo_artifact` that renames a temp file of its own, still has the
#: entry's frame on the stack and is neither a kernel open nor the atomic
#: writer. Private names, so `test_the_entry_point_list_is_complete` checks
#: they exist.
_OPERATOR_OPENER = "scripts/tools/_lib_io.py:_kernel_open_operator_output"
_ATOMIC_WRITER = "scripts/tools/_lib_atomic_write.py:"


def _placed(event: dict) -> str | None:
    """Where the event landed, as an absolute path with its directory
    resolved — or None when it is nowhere a file can be:

    * the hook marked it ``unplaced`` (it could not tell; see the hook's
      docstring). `_audit` turns every such event into a fault — it is never
      read against the working directory. `shutil.rmtree` under a
      `TemporaryDirectory` removes ``<name>`` relative to the temp
      directory's fd; joined to the working directory that would be a path
      inside whatever tree the tool was started in;
    * it was raised on a file descriptor that is no file (a pipe to a child,
      a socket): the hook resolved it to a name that is not a path.
    """
    path = event.get("path")
    if event.get("unplaced") or not isinstance(path, str):
        return None
    if not os.path.isabs(path):
        if "cwd" not in event:
            return None
        path = os.path.join(event["cwd"], path)
    head, tail = os.path.split(os.path.normpath(path))
    return os.path.join(os.path.realpath(head), tail)


def _under(path: str, scope: Path) -> str | None:
    """*path* relative to *scope* (``.`` for *scope* itself), or None when it
    is not inside it. By path component: ``good-2/x`` is not under ``good``.
    Nothing is excluded by name — a ``.pyc`` or ``/dev/null`` is simply not
    under an output directory, and one that were would be a finding."""
    root = os.path.realpath(scope)
    if path == root:
        return "."
    if path.startswith(root + os.sep):
        return path[len(root) + 1:].replace(os.sep, "/")
    return None


#: How ``SPAWNS`` marks a program that was handed a path under the output
#: directory on its command line: it can write there, and the audit cannot
#: see it do so.
GIVEN_THE_OUTPUT_PATH = " [given the output path]"


def _program(argv: tuple[str, ...]) -> str:
    """How a spawned program is named in ``SPAWNS``: its file name, and
    nothing else; any Python is ``python``.

    ⛔ Not its subcommand, and not how often. Which `da-guard` subcommands a
    run makes, and how many spawn events one start raises, is not a property
    of the tool: CPython starts a child through `os.posix_spawn` where the
    platform allows it and then raises that event AS WELL as
    `subprocess.Popen` (seen on CI, Python 3.14: one more "program" per
    start than on the 3.13 this list was first measured on), and a helper's
    `-h` probe or a cache decides what else is run. The name of the program
    is what stays the same everywhere."""
    name = os.path.basename(argv[0])
    return "python" if re.fullmatch(r"python[0-9.]*", name) else name


def _given_the_output_path(event: dict, scope: Path) -> bool:
    """Whether a spawn's command line names something under *scope*: an
    argument that is such a path (a relative one is read against the
    directory the child starts in), or one that contains the directory's
    name — ``--output=<path>``, a shell command with a redirect in it."""
    root = os.path.realpath(scope)
    for arg in map(str, (event.get("argv") or [])[1:]):
        if root in arg or str(scope) in arg:
            return True
        for value in {arg, arg.partition("=")[2]} - {""}:
            if value.startswith("-") or os.path.isabs(value) or "cwd" not in event:
                continue
            placed = _placed({"path": value, "cwd": event["cwd"]})
            if placed is not None and _under(placed, scope) is not None:
                return True
    return False


@dataclass(frozen=True)
class Audit:
    """What the log says about one run, under one scope."""
    logged: bool                              # a log exists at all
    loaded: bool                              # the tool's own process reported in
    sites: dict[str, tuple[tuple[str, str], ...]]   # kind → ((event, relative path), …)
    spawned: tuple[tuple[str, ...], ...]      # argv of every process spawned
    unlogged: tuple[str, ...] = ()            # files the run left that no logged write made
    faults: tuple[str, ...] = ()              # events that are wrong in themselves
    # (file, line) of every frame of every event under the scope
    reached: frozenset[tuple[str, int]] = frozenset()
    # `_program` of every spawn, marked GIVEN_THE_OUTPUT_PATH where it was
    programs: tuple[str, ...] = ()


def _tool_loaded(events: list[dict], tool: Path) -> bool:
    """Did the hook load in THE TOOL's process — not only in a Python child
    of it, whose `loaded` line lands in the same log?"""
    tool_path = os.path.realpath(tool)
    return any(e.get("ev") == "loaded"
               and any(os.path.realpath(a) == tool_path for a in e.get("argv", [])[:3])
               for e in events)


def _audit(events: list[dict] | None, scope: Path, tool: Path,
           made: frozenset[str] = frozenset()) -> Audit:
    """*made*: the files under *scope* that are there after the run and were
    not before it. One with no logged write to its path was put there by
    something the hook did not see."""
    if events is None:
        return Audit(False, False, {}, ())
    frames = _entry_frames()
    loaded = _tool_loaded(events, tool)
    sites: dict[str, list[tuple[str, str]]] = {}
    spawned = []
    faults: list[str] = []
    programs: set[str] = set()
    reached: set[tuple[str, int]] = set()
    for event in events:
        if event["ev"] == "loaded":
            continue
        if event["ev"] == "hook-error":
            faults.append(f"the hook failed on a {event.get('event')} event "
                          f"({event.get('error')}): that event was not measured")
            continue
        if event["ev"] == "spawn":
            argv = event["argv"] or [event["exe"]]
            argv = (os.path.basename(str(argv[0])), *map(str, argv[1:]))
            spawned.append(argv)
            # `da-guard write` is the one child that is MEANT to write there:
            # the row says so (`writer=`), and the disk is matched against it.
            programs.add(_program(argv) + (
                GIVEN_THE_OUTPUT_PATH if argv[:2] != ("da-guard", "write")
                and _given_the_output_path(event, scope) else ""))
            continue
        stack = event["stack"]
        lines = event.get("lines") or [0] * len(stack)
        if event.get("unplaced"):
            # Not "elsewhere": unknown. A write whose target cannot be named
            # could be under the output path.
            where = stack[0] if stack else "<no scripts/ frame>"
            faults.append(f"{event['ev']} {event.get('path')!r} from {where} could not be "
                          "placed (a descriptor or dir_fd the hook could not resolve, "
                          "or a relative path given to os.open) — the audit cannot "
                          "say it is not under the output path")
            continue
        placed = _placed(event)
        rel = _under(placed, scope) if placed is not None else None
        if rel is None:
            continue
        kind = _via(event, frames)
        reached.update((frame.rpartition(":")[0], line) for frame, line in zip(stack, lines))
        site = (event["ev"], rel)
        on_fd = "fd" in event
        if kind == "operator":
            opened_by_kernel = _OPERATOR_OPENER in stack
            if on_fd:
                # The entry wraps the fd it opened (`open(fd)`) and sets its
                # mode (`fchmod`, inside the opener): the same file again,
                # not a second write. Anything else on a descriptor is one.
                if not (event["ev"] == "open"
                        or (event["ev"] == "os.chmod" and opened_by_kernel)):
                    faults.append(f"{event['ev']} on a descriptor of {rel} under the "
                                  "operator entry, outside its opener")
                continue
            if event["ev"] == "open" and not opened_by_kernel:
                faults.append(f"open {rel} came through the operator entry but not "
                              "through `_kernel_open_operator_output`: the entry did "
                              "not let the kernel open it")
        elif kind == "artifact":
            if not any(frame.startswith(_ATOMIC_WRITER) for frame in stack):
                faults.append(f"{event['ev']} {rel} came through the artifact entry but "
                              "not through `_lib_atomic_write`: not the atomic writer")
        bucket = sites.setdefault(kind, [])
        if site not in bucket:
            bucket.append(site)
    logged = {rel for bucket in sites.values() for event, rel in bucket
              if event in _FILE_EVENTS}
    return Audit(True, loaded, {k: tuple(v) for k, v in sites.items()}, tuple(spawned),
                 tuple(sorted(made - logged)), tuple(faults),
                 frozenset(reached), tuple(sorted(programs)))


@dataclass(frozen=True)
class Seen:
    faults: list[str]            # wrong whatever any list says
    raw: tuple[str, ...]         # "<event> <path under the scope>" of each raw site, sorted
    raw_is_all: bool             # no write under the scope came through any entry
    other_class: str | None      # the OTHER class's entry wrote here
    spawned: tuple[str, ...] = ()   # the programs the run started (Audit.programs)


def _verdict(row: Row, audit: Audit) -> Seen:
    """Hold one run to the row's class. Pure: the harness controls below
    drive it with hand-made logs."""
    faults: list[str] = list(audit.faults)
    if not audit.logged:
        faults.append("the write audit left no log — the hook did not load, so nothing "
                      "was measured (this is not 'nothing was written')")
    elif not audit.loaded:
        faults.append("no `loaded` line from the tool's own process — the hook did not "
                      "load in it, so its writes were not measured")
    files = [(kind, site) for kind, sites in audit.sites.items() for site in sites
             if site[0] in _FILE_EVENTS]
    if row.writer not in WRITERS:
        faults.append(f"unknown writer {row.writer!r} (one of {WRITERS})")
    by_python, by_da_guard = "python" in row.writer, "da-guard" in row.writer
    if by_python and audit.loaded and not files:
        faults.append("no file was written under the output path by any audited "
                      "process — a dead row, or a writer the hook cannot see "
                      "(then the row must say `writer=`)")
    if not by_python and files:
        faults.append("the row says `da-guard write` puts the files, but Python "
                      f"wrote under the output path too: {files}")
    if by_da_guard:
        if not any(argv[:2] == ("da-guard", "write") for argv in audit.spawned):
            faults.append("the row says `da-guard write` puts files, and the run "
                          f"spawned none: {sorted(set(a[:2] for a in audit.spawned))}")
        if audit.loaded and not audit.unlogged:
            faults.append("the row says `da-guard write` puts files, and every file "
                          "the run left has a logged Python write — say what the "
                          "writer is")
    elif audit.unlogged:
        faults.append("file(s) under the output path that no audited process wrote: "
                      f"{list(audit.unlogged)} — put there by a process the hook "
                      "cannot see; the row must say which (`writer=`)")
    if row.cls not in CLASSES:
        faults.append(f"unknown class {row.cls!r} (one of {CLASSES})")
    other = next((c for c in CLASSES if c != row.cls and audit.sites.get(c)), None)

    for kind, sites in audit.sites.items():
        if kind == "operator":
            # The kernel opens an operator path in place: nothing is renamed
            # onto it, and no mode is set by name afterwards.
            for event, rel in sites:
                if event != "open":
                    faults.append(f"{event} {rel} under the operator entry: an operator "
                                  "output is opened in place, nothing else")
        elif kind == "artifact":
            renamed = {rel for event, rel in sites if event == "os.rename"}
            if not renamed:
                faults.append(f"the artifact entry renamed nothing into place: {sites}")
            for event, rel in sites:
                if event == "open" and rel in renamed:
                    faults.append(f"open {rel} under the artifact entry: the artifact "
                                  "itself was opened for writing (not atomic)")
        elif kind == "directory":
            for event, rel in sites:
                if event != "os.mkdir":
                    faults.append(f"{event} {rel} under ensure_dir: it makes directories only")
    raw = tuple(sorted(f"{event} {rel}" for event, rel in audit.sites.get("raw", ())))
    through_an_entry = any(sites for kind, sites in audit.sites.items() if kind != "raw")
    return Seen(faults, raw, bool(raw) and not through_an_entry, other,
                audit.programs)


def _is_clean(row: Row) -> bool:
    """On neither write list: the row claims every write under its output
    path came through its class's entry."""
    key = (row.tool, row.flag)
    return key not in RAW_UNDER_OUT and key not in VIA_OTHER_CLASS


def _mode_faults(row: Row, scope: Path, made: frozenset[str]) -> list[str]:
    """For a CLEAN operator row whose files Python writes: every regular
    file the run left is at the mode the entry was asked for (``Row.mode``,
    0600 unless the row says otherwise). The audit sees who opened a file,
    not what a later `fchmod`, or a `chmod` in a child, did to it; the disk
    does. Not for an artifact (its mode rule is the atomic writer's own, and
    under review in #3107), nor for a row on a list (its writer is not the
    entry yet, so there is no mode to hold it to)."""
    if row.cls != "operator" or row.writer != "python" or not _is_clean(row):
        return []
    if not has_posix_modes():
        return []
    out = []
    for rel in sorted(made):
        st = os.lstat(scope / rel)
        if stat.S_ISREG(st.st_mode) and stat.S_IMODE(st.st_mode) != row.mode:
            out.append(f"{rel} is mode {stat.S_IMODE(st.st_mode):04o} after the run; the "
                       f"operator entry writes {row.mode:04o} here")
    return out


# ── The lists that may only shrink ─────────────────────────────────────────
# Each is EXACT in both directions: a row that does what a list says and is
# not on it is red, and so is a row that is on a list and no longer does it
# ("take it off"). Nothing here is a judgement — every entry is what the
# control run under the audit hook logged on this tree. To see what a row
# logs, make its entry wrong and read the failure, or:
#   pytest tests/shared/test_output_path_write_failure.py -k control -q
# Keyed by (tool, flag). SPAWNS, further down, is the one list that is NOT
# exact both ways; it says why.
Key = tuple[str, str]

#: Rows with a write under the output path that came through NO entry: each
#: site as "<event> <path under the control run's output directory>", and
#: whether that is all the run wrote there ("all") or it also wrote through
#: an entry ("some"). A `mkdir` counts — `ensure_dir` is the entry for it.
RAW_UNDER_OUT: dict[Key, tuple[str, tuple[str, ...]]] = {
}

#: THE CLASS RULE, MECHANICAL HALF (#3982, owner 2026-11-18). A flag is an
#: artifact iff (a) the tool reads its output back — measured by
#: `test_the_class_follows_the_read_back`: the control run, then the same
#: command again, its `--check`, and the row's `readers`, under the audit
#: hook logging reads; or (b) its default is a file the repo tracks; or (c)
#: another program loads it whole (EXTERNAL_READERS: the half no test can
#: measure, so only the owner adds to it, and the reason is kept here).
#: Otherwise operator. Both directions are faults: an operator flag the tool
#: reads back, and an artifact flag (a) and (c) do not explain.
#: ⚠️ (b) has no list here: every row whose default is tracked
#: (generate_adr_index / generate_planning_index --target, sync_glossary_abbr
#: --output) also reads it back, so an entry would explain nothing (a)
#: does not — measured: removing one changed no outcome. A row that needs
#: (b) adds the list then, held to `git ls-files`. It stays the static
#: trigger for constant-path writers this gate does not run.
EXTERNAL_READERS: dict[Key, str] = {
    ("ops/generate_alertmanager_routes.py", "-o/--output"):
        "Alertmanager loads it as its configuration (owner 2026-11-15)",
    ("../ops/configmap_assemble.py", "--output"):
        "kubectl applies it as the ConfigMap manifest (owner 2026-10-24)",
}


#: Rows whose output came through the entry of the OTHER class than the row
#: declares, and which one. Each is a tool to move or a row to reclassify.
VIA_OTHER_CLASS: dict[Key, str] = {
    # #3982: the read-back rule makes it an artifact (its `--force` run reads
    # the `.da-init.yaml` ledger it wrote); the move to `write_repo_artifact`
    # is its own change (#4085).
    ("ops/init_project.py", "-o/--output-dir"): "operator",
}


#: The programs each flag's control runs may start (`_program`: the file
#: name). A process that is not Python writes where the audit cannot see, so
#: a run may start none that is not listed: a `cp` or a `chmod` in a child
#: is a write this gate would otherwise never hear of. One whose command
#: line names a path under the output directory is a separate entry, marked
#: GIVEN_THE_OUTPUT_PATH — a listed `git` that gains `--output=<the output>`
#: is not the listed `git`. (`da-guard write` is not marked: the row's
#: `writer=` and the log-against-disk match hold it.)
#:
#: ⚠️ One direction only: started ⊆ listed. "Listed and not started" is NOT
#: checked, because it is not the same everywhere — a row of the same flag
#: that takes another path starts fewer (`backtest --skip-if-unavailable`
#: starts none), and whether a helper is run at all can hang on a cache. A
#: stale entry here permits a program the tool no longer needs; it does not
#: hide a write, which is what the two-directional lists are for.
SPAWNS: dict[Key, tuple[str, ...]] = {
    ("../ops/configmap_assemble.py", "--output"): ("da-crdecode", "da-guard", "da-read", "kubectl", "python"),
    ("dx/compile_custom_alerts.py", "--out"): ("da-guard",),
    ("dx/describe_tenant.py", "-o/--output"): ("da-guard",),
    ("dx/generate_changelog.py", "-o/--output"): ("git",),
    ("dx/generate_tenant_fixture.py", "-o/--output"): ("da-guard",),
    ("dx/generate_tenant_metadata.py", "--output"): ("da-guard", "git"),
    ("dx/migrate_conf_d.py", "-o/--output-plan"): ("da-guard", "git"),
    ("dx/run_chaos_soak.py", "--output-dir"): ("da-guard",),
    ("dx/scan_component_health.py", "--output"): ("git",),
    ("ops/analyze_rule_pack_gaps.py", "-o/--output"): ("da-guard",),
    ("ops/assemble_config_dir.py", "--manifest"): ("da-crdecode", "da-guard", "da-read", "python"),
    ("ops/assemble_config_dir.py", "--output"): ("da-crdecode", "da-guard", "da-read", "python"),
    ("ops/backtest_threshold.py", "--markdown-output"): ("da-guard",),
    ("ops/backtest_threshold.py", "-o/--output"): ("da-guard",),
    ("ops/batch_diagnose.py", "-o/--output"): ("da-guard", "kubectl"),
    ("ops/config_history.py", "--config-dir"): ("da-guard [given the output path]",),
    ("ops/da_assembler.py", "--config-dir"): ("da-crdecode", "da-guard"),
    ("ops/federation_keygen.py", "--jwks-out"): ("openssl",),
    ("ops/generate_alertmanager_routes.py", "--findings-json"): ("da-crdecode", "da-guard", "da-read"),
    ("ops/generate_alertmanager_routes.py", "--secret-mounts-out"): ("da-crdecode", "da-guard", "da-read"),
    ("ops/generate_alertmanager_routes.py", "-o/--output"): ("da-crdecode", "da-guard", "da-read"),
    ("ops/generate_rule_pack_split.py", "--output-dir"): ("da-read",),
    ("ops/generate_tenant_mapping_rules.py", "-o/--output"): ("da-guard",),
    ("ops/init_project.py", "-o/--output-dir"): ("da-guard",),
    ("ops/migrate_credentials.py", "--manifests-out"): ("da-guard",),
    ("ops/migrate_rule.py", "-o/--output-dir"): ("da-guard", "da-read"),
    ("ops/migrate_to_operator.py", "--output-dir"): ("da-crdecode", "da-guard", "da-read"),
    ("ops/onboard_platform.py", "-o/--output-dir"): ("da-crdecode", "da-read"),
    ("ops/operator_generate.py", "--output-dir"): ("da-read",),
    ("ops/scaffold_tenant.py", "-o/--output-dir"): ("da-guard",),
}


def _list_faults(row: Row, seen: Seen) -> list[str]:
    key = (row.tool, row.flag)
    out = []
    listed = RAW_UNDER_OUT.get(key)
    found = (("all" if seen.raw_is_all else "some"), seen.raw) if seen.raw else None
    if found != listed:
        out.append(f"raw writes under the output path of {key}: the run made {found}, "
                   f"RAW_UNDER_OUT says {listed} — route a new one through its entry; "
                   "shrink or drop the entry for one that is gone")
    if seen.other_class != VIA_OTHER_CLASS.get(key):
        out.append(f"{key} is declared {row.cls} and the run wrote through the "
                   f"{seen.other_class} entry; VIA_OTHER_CLASS says "
                   f"{VIA_OTHER_CLASS.get(key)}")
    unlisted = sorted(set(seen.spawned) - set(SPAWNS.get(key, ())))
    if unlisted:
        out.append(f"{row.id} started {unlisted}, which SPAWNS does not list for {key} "
                   f"({SPAWNS.get(key, ())}) — a process that is not Python writes "
                   "unseen; list a program the write needs, and nothing else")
    return out


def _control_run(row: Row, tmp: Path, stub: str):
    """The control run of *row* under the audit hook: (the process, the
    output path, its audit, the files it left under `good/`)."""
    out = tmp / "good" / row.out_name
    out.parent.mkdir()
    log = tmp / "write-audit.jsonl"
    proc = _run(row, Ctx(tmp, stub), out, audit_log=log)
    made = _files_under(out.parent) - proc.files_before
    return proc, out, _audit(_read_audit_log(log), out.parent, TOOLS_DIR / row.tool, made), made


# ═══════════════════════════════════════════════════════════════════════════
# The gate
# ═══════════════════════════════════════════════════════════════════════════
_CASES = [pytest.param(row, shape, id=f"{row.id}-{shape}",
                       marks=[pytest.mark.xfail(strict=True, reason=row.known_defect)]
                       if row.known_defect else [])
          for row in ROWS for shape in row.shapes]


#: How many rows may carry `known_defect`. Like `_NO_ROW_CEILING`: the field
#: turns a red bad-path case into an expected one, so one more is an edit here.
_KNOWN_DEFECT_CEILING = 1


def test_a_known_defect_is_a_strict_xfail_and_nothing_else_is_marked():
    """`Row.known_defect` must stay a claim that can be proved wrong: strict,
    so the day the tool is fixed the case fails until the field is removed.
    A skip, or a lenient xfail, would keep saying "known" for ever."""
    assert sum(1 for row in ROWS if row.known_defect) == _KNOWN_DEFECT_CEILING, (
        "a row gained or lost a `known_defect`: the ceiling moves with a reviewed "
        "edit, and only down unless a ticket says otherwise")
    for case in _CASES:
        row = case.values[0]
        if row.known_defect:
            (mark,) = case.marks
            assert mark.name == "xfail" and mark.kwargs["strict"] is True, case.id
            assert mark.kwargs["reason"].strip(), case.id
        else:
            assert not case.marks, case.id


@pytest.mark.parametrize("row,shape", _CASES)
def test_unwritable_output_path_is_rc2_one_line_no_traceback(row, shape, tmp_path, stub_url):
    _skip_if_unavailable(row)
    out = _bad_path(tmp_path, shape, row)
    proc = _run(row, Ctx(tmp_path, stub_url), out)

    assert proc.returncode == EXIT_CALLER_ERROR, _fail(
        row, proc, f"expected rc={EXIT_CALLER_ERROR} (EXIT_CALLER_ERROR) for shape "
                   f"{shape}; rc=1 would read as EXIT_VIOLATION")
    assert "Traceback" not in proc.stderr, _fail(row, proc, "a traceback leaked")
    if row.wording == "own":
        problem = _own_refusal_problem(row, shape, out, proc.stderr)
        assert problem is None, _fail(row, proc, problem)
        return
    # The declared head must actually be emitted: for two rows it is a CI
    # contract of its own, so a row cannot use it to loosen the match.
    error_lines = _error_lines(proc.stderr, row.prefix)
    assert len(error_lines) == 1, _fail(
        row, proc, f"expected exactly one {row.prefix!r}+'cannot …' line, "
                   f"got {len(error_lines)}")
    line = error_lines[0]
    # Which path must be named depends on the shape and on the verb the tool
    # reported: see _expected_fragment. Compared for EQUALITY on the parsed
    # path, not as a substring — see _named_path for what that was hiding.
    action, named = _named_path(line, row.prefix)
    assert _names_the_blocked_path(row, shape, out, action, named), _fail(
        row, proc, f"the error line names {named!r} after 'cannot {action}', "
                   f"which is not the path this shape blocks")
    assert row.flag in line, _fail(row, proc, f"the error line does not name {row.flag}")
    assert "check the value given to" in line, _fail(
        row, proc, "the error line does not point the operator at the flag")


@pytest.mark.parametrize("row", ROWS, ids=[r.id for r in ROWS])
def test_control_writable_path_is_rc0_and_writes(row, tmp_path, stub_url):
    """The pair half: same argv, writable path ⇒ rc=0 and the output exists.

    A tool that exits 2 whenever the flag is present would pass the bad-path
    test above; this is what rules it out.
    """
    _skip_if_unavailable(row)
    proc, out, audit, made = _control_run(row, tmp_path, stub_url)

    assert proc.returncode == row.control_rc, _fail(
        row, proc, f"control run did not exit {row.control_rc}")
    assert "Traceback" not in proc.stderr, _fail(row, proc, "control run leaked a traceback")
    assert not _error_lines(proc.stderr, row.prefix), _fail(
        row, proc, "control run printed a write error")
    # ⛔ And once more WITHOUT the row's head. `_error_lines` is anchored on
    # the declared prefix, so for the two rows that declare a non-default one
    # a control run that printed the SHARED `ERROR: cannot …` line would slip
    # past the check above — the prefix would not match, the line would not be
    # counted, and a tool failing its write on a perfectly good path would
    # read as clean. These two fragments are in every standard line whatever
    # head is in front of them.
    for fragment in ("cannot ", "check the value given to"):
        assert fragment not in proc.stderr, _fail(
            row, proc, f"control run printed {fragment!r} — a write error on a "
                       f"writable path, whatever head is in front of it")
    if row.produces is not None:
        # A derived-output tool: the flag value is an INPUT the fixture just
        # populated, so "something exists there" proves nothing. Name what the
        # run had to produce instead.
        produced = row.produces(out)
        assert produced.is_file() and produced.stat().st_size > 0, _fail(
            row, proc, f"control run did not produce {produced}")
    elif row.kind == "file":
        assert out.is_file() and out.stat().st_size > 0, _fail(
            row, proc, f"control run did not write {out}")
    else:
        written = [p for p in out.rglob("*") if p.is_file()]
        assert written, _fail(row, proc, f"control run wrote nothing under {out}")
    # And WHICH ENTRY wrote it (#3107): everything the run changed under the
    # directory this test handed it — `good/`, which holds the output path
    # and nothing else — came through the entry of the row's class, or the
    # row is on one of the lists that may only shrink.
    seen = _verdict(row, audit)
    problems = seen.faults + _list_faults(row, seen) + _mode_faults(row, out.parent, made)
    assert not problems, _fail(row, proc, "write audit:\n  " + "\n  ".join(problems))


# ═══════════════════════════════════════════════════════════════════════════
# The class rule: does the tool read its output back? (#3982)
# ═══════════════════════════════════════════════════════════════════════════
def _read_back(row: Row, tmp: Path, stub: str) -> tuple[list[str], list[str]]:
    """(files of the control run's output that a later run read, the runs
    made). The control run first; then, under the hook logging reads under
    `good/`: the same argv again, its `--check` where the tool has one, and
    the row's `readers`. The argv is the control run's own — a row's `build`
    makes its fixtures, so it is not called again."""
    proc, out, _audit_, made = _control_run(row, tmp, stub)
    argv = list(proc.args[2:])
    runs = [argv]
    if '"--check"' in (TOOLS_DIR / row.tool).read_text(encoding="utf-8") and "--check" not in argv:
        runs.append([*argv, "--check"])
    if row.readers is not None:
        runs.extend(row.readers(Ctx(tmp, stub), out))
    ctx = Ctx(tmp, stub)
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    if row.env is not None:
        env.update(row.env(ctx))
    # What the control run wrote: the files it made, and for a file flag the
    # output path itself — a fixture may have made it first (an index
    # generator rewrites a doc that is already there), so it is not "made".
    made_abs = {os.path.realpath(out.parent / m) for m in made}
    if row.kind == "file":
        made_abs.add(os.path.realpath(out))
    read: set[str] = set()
    measured: list[str] = []
    for i, run in enumerate(runs):
        log = tmp / f"read-audit-{i}.jsonl"
        run_env = {**env, **_audit_env(log, tmp), "DA_WRITE_AUDIT_READS": str(out.parent)}
        done = subprocess.run([sys.executable, str(TOOLS_DIR / row.tool), *run],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=TIMEOUT_S, env=run_env,
                              cwd=proc.run_cwd)
        # A run argparse refused measured nothing (an added `--check` the
        # tool's flags exclude): it is not counted.
        if done.returncode == 2 and "usage:" in done.stderr:
            continue
        measured.append(" ".join(run))
        # A measurement that did not happen is not "no reads" (CodeRabbit):
        # the hook must have loaded, and must not have failed.
        events = _read_audit_log(log)
        assert events is not None and _tool_loaded(events, TOOLS_DIR / row.tool), (
            f"{row.id}: the audit hook did not load in the tool's process for the "
            f"rerun {' '.join(run)!r}")
        failed = [e for e in events if e.get("ev") == "hook-error"]
        assert not failed, f"{row.id}: the audit hook failed in the rerun: {failed[:3]}"
        for ev in events:
            if ev.get("ev") == "read" and ev.get("path") in made_abs:
                read.add(ev["path"])
    return sorted(read), measured


@pytest.mark.parametrize("row", ROWS, ids=[r.id for r in ROWS])
def test_the_class_follows_the_read_back(row, tmp_path, stub_url):
    """#3982 (owner 2026-11-18): artifact iff the tool reads the output
    back, or its default is tracked, or another program loads it whole —
    both directions, so neither class can be chosen against the rule."""
    _skip_if_unavailable(row)
    key = (row.tool, row.flag)
    read, runs = _read_back(row, tmp_path, stub_url)
    # Every row of a flag is measured (its argv decides what a rerun can
    # reach: `init_project`'s `--force` row reads, the other is refused), and
    # each must have measured something.
    assert runs, f"{row.id}: no rerun was measured — every one was refused by argparse"
    explained = bool(read) or key in EXTERNAL_READERS
    if row.cls == "operator":
        assert not read, (f"{row.id}: the tool reads its output back ({read}) through "
                          f"{runs} — an artifact by the rule, written in place")
        assert key not in EXTERNAL_READERS, row.id
    else:
        assert explained, (f"{row.id}: an artifact that no run read back ({runs}) and "
                           f"that EXTERNAL_READERS does not name — an operator output by "
                           f"the rule")
        if row.artifact and key not in EXTERNAL_READERS:
            # A row that names its sink is held to THAT sink: another file of
            # the same tool read back does not make this one an artifact.
            # Where `_expected_fragment` puts it: beside the output for a
            # sibling shape, inside it for `artifact_is_dir`.
            out = tmp_path / "good" / row.out_name
            base = out.parent if {"sibling_is_file", "sibling_is_dir"} & set(row.shapes) else out
            sink = os.path.realpath(out_sink := (base / row.artifact))
            assert any(r == sink or r.startswith(sink + os.sep) for r in read), (
                f"{row.id}: its sink {out_sink} was not read back by {runs} (read: {read})")
    if key in EXTERNAL_READERS:
        assert not read, (f"{row.id}: on EXTERNAL_READERS but the tool reads it back "
                          f"itself ({read}) — the measured half explains it; take it off")


def test_only_the_tools_own_loaded_line_counts():
    """A Python child's `loaded` line is in the same log; it does not say the
    tool's own process was measured (CodeRabbit, #4086)."""
    tool = TOOLS_DIR / "dx" / "describe_tenant.py"
    child = {"ev": "loaded", "argv": [sys.executable, "-c", "pass"]}
    own = {"ev": "loaded", "argv": [sys.executable, str(tool), "-o", "x"]}
    assert not _tool_loaded([child], tool)
    assert _tool_loaded([child, own], tool)
    assert not _tool_loaded([], tool)


def test_the_external_readers_are_artifact_rows_with_a_reason():
    rows = {(r.tool, r.flag): r for r in ROWS}
    for key, why in EXTERNAL_READERS.items():
        assert key in rows and rows[key].cls == "artifact", key
        assert "owner" in why and why.strip(), key


# ═══════════════════════════════════════════════════════════════════════════
# The other direction: an INPUT failure must not be dressed up as an output one
# ═══════════════════════════════════════════════════════════════════════════
def test_a_bad_source_entry_does_not_blame_the_output_flag(tmp_path):
    """`assemble_config_dir` copies `--sources` entries into `--output`.

    A source entry that is itself a DIRECTORY named `x.yaml` (measured, not
    hypothetical — `discover_yamls` lists it by extension, it does not stat
    it) once made `shutil.copy2` raise `IsADirectoryError` naming the SOURCE,
    inside the block wrapped for `--output`. Since #3206 the shared conf.d
    refusal ends the run on it before anything is copied, and the copy is
    `da-guard write`'s (a source that is no regular file: `bad_source`,
    naming the source).

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
    # And it is still recognisably the source that failed: since #3206 the
    # shared conf.d refusal, naming the entry under the source tree.
    assert_refused(proc.returncode, proc.stderr, ["x.yaml"])
    assert f"the conf.d at {src} holds" in proc.stderr, proc.stderr[-1500:]


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
    kw.setdefault("cls", "operator")
    return Row(tool="dx/run_chaos_soak.py", flag="--output-dir",
               build=lambda c, out: [], reach="synthetic control", **kw)


class TestErrorLineMatcher:
    _MSG = "cannot write /x/out.txt: Not a directory (errno 20) — check the value given to -o"
    _STD = f"ERROR: {_MSG}"          # the shared emitter's head
    _ANNOTATED = f"::error::{_MSG}"  # a synthetic non-default head

    def test_the_default_head_matches_the_shared_line(self):
        assert _error_lines(f"[warn] noise\n{self._STD}\n", "ERROR: ") == [self._STD]

    def test_declared_head_is_matched_with_the_line(self):
        assert _error_lines(f"{self._ANNOTATED}\n", "::error::") == [self._ANNOTATED]

    def test_declared_head_that_the_tool_stopped_emitting_does_not_match(self):
        """The CI annotation is a contract too: losing it must go red, not be
        silently accepted because the rest of the line is still right."""
        assert _error_lines(f"{self._STD}\n", "::error::") == []

    def test_an_undeclared_head_does_not_match(self):
        """A tool that swaps the default head goes red until its row says so."""
        assert _error_lines(f"{self._ANNOTATED}\n", "ERROR: ") == []

    def test_unrelated_stderr_is_not_counted(self):
        assert _error_lines("[warn] /metrics fetch failed\nERROR: bad --tenant\n",
                            "ERROR: ") == []

    def test_two_lines_are_two(self):
        assert len(_error_lines(f"{self._STD}\n{self._STD}\n", "ERROR: ")) == 2


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
        with pytest.raises(DIR_WRITE_ERROR):
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
        with pytest.raises(DIR_WRITE_ERROR):
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


class TestTargetIsFileShape:
    """The mirror of ``target_is_dir``, for ``--output-dir`` tools (#1789).

    A ``dir`` tool's flag value being a regular FILE is the shape that reaches
    whatever runs BEFORE the first write — an "is it already populated?"
    ``iterdir``, a ``glob``, a ``listdir``. ``dx/generate_tenant_fixture``
    died there with a ``NotADirectoryError`` traceback at rc=1 while every
    wrapped write site below it sat unreached, and neither parent shape could
    see it: they corrupt the parent, so the tool dies at its own mkdir first.
    """

    def test_it_makes_the_flag_value_a_file(self, tmp_path):
        row = _control_row(shapes=("target_is_file",))
        out = _bad_path(tmp_path, "target_is_file", row)
        assert out.is_file(), "the flag value itself must be the blocker"
        assert out.parent.is_dir(), "the parent must stay usable"
        with pytest.raises(NotADirectoryError):
            list(out.iterdir())

    def test_the_asserted_path_is_the_flag_value(self, tmp_path):
        row = _control_row(shapes=("target_is_file",))
        out = _bad_path(tmp_path, "target_is_file", row)
        assert _expected_fragment(row, "target_is_file", out) == out
        # A non-mkdir verb must name it EXACTLY: the subdirectory widening is
        # for `create directory` only.
        assert _names_the_blocked_path(row, "target_is_file", out, "inspect", str(out))
        assert not _names_the_blocked_path(row, "target_is_file", out, "inspect",
                                           str(out / "conf.d"))
        assert _names_the_blocked_path(row, "target_is_file", out,
                                       "create directory", str(out / "conf.d"))


class TestArtifactIsDirShape:
    def test_it_blocks_the_named_artifact_and_nothing_else(self, tmp_path):
        row = _control_row(shapes=("artifact_is_dir",), artifact="summary.txt")
        out = _bad_path(tmp_path, "artifact_is_dir", row)

        assert out.is_dir(), "the output directory must be usable — the tool's own mkdir -p has to succeed"
        assert (out / "summary.txt").is_dir()
        with pytest.raises(DIR_WRITE_ERROR):
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

    def test_the_mkdir_side_shapes_follow_the_reported_verb(self, tmp_path):
        """#1789 F6. A `create directory` verb on a FILE row means the tool
        was creating the output's parent; every other verb, and every `dir`
        row, means the flag value itself.

        A specific break that reddens this: make the parent branch of
        `_expected_fragment` return `out.parent` unconditionally again.
        """
        as_file = _control_row(kind="file")
        as_dir = _control_row(kind="dir")
        for shape in ("missing_parent", "parent_is_file"):
            root = tmp_path / shape
            root.mkdir()
            out = _bad_path(root, shape, as_file)
            assert not out.parent.is_dir(), shape
            assert _expected_fragment(as_file, shape, out,
                                      "create directory") == out.parent
            assert _expected_fragment(as_file, shape, out, "write") == out
            assert _expected_fragment(as_dir, shape, out,
                                      "create directory") == out
            # The widening, both signs: a `dir` tool may name a subdirectory
            # of its output directory; a `file` tool may not name the target
            # when it claims to be creating a directory.
            assert _names_the_blocked_path(as_dir, shape, out,
                                           "create directory", str(out / "edge-rules"))
            assert not _names_the_blocked_path(as_dir, shape, out,
                                               "create directory", str(out.parent))
            assert not _names_the_blocked_path(as_file, shape, out,
                                               "create directory", str(out))


# ═══════════════════════════════════════════════════════════════════════════
# Staleness guards
# ═══════════════════════════════════════════════════════════════════════════
def test_rows_point_at_real_tools_and_fixtures():
    for row in ROWS:
        assert (TOOLS_DIR / row.tool).is_file(), row.tool
        assert row.kind in ("file", "dir"), row.id
        assert [r.id for r in ROWS].count(row.id) == 1, (
            f"{row.id}: two rows share this id — give one a `variant`: a failure "
            "names its row by it")
        assert row.cls in CLASSES, row.id
        assert row.writer in WRITERS, row.id
        assert row.wording in ("shared", "own"), row.id
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


_UNRESOLVED = "<cannot tell which flag: {}>"


def _writer_names() -> frozenset[str]:
    """Every function `_lib_io.OUTPUT_ENTRY_POINTS` names — the entries, the
    two that only name the flag, and the wrappers. Read from the library:
    the hand-kept copy of this list that used to live here did not know
    `write_operator_output` / `write_repo_artifact` / `atomic_write_text`, so
    each tool migrated to them left this population without a word
    (`ops/federation_keygen` had; `ops/migrate_credentials` never entered)."""
    import _lib_io
    return frozenset(name.split(".")[1]
                     for names in _lib_io.OUTPUT_ENTRY_POINTS.values() for name in names)


def _cli_modules() -> list[tuple[str, Path, ast.Module]]:
    """(name relative to scripts/tools, path, tree) of every Python file of
    the repository that is not a test and not one of the modules the writers
    are defined in — libraries included; a caller that wants CLIs only skips
    the ``_``-prefixed ones.

    Every file, wherever it is and whatever it imports (#3107): the scan was
    ``scripts/tools``, then two directories, then "under ``scripts/`` and
    importing a writer module", and each narrowing was a place a tool could
    name a flag at a writer and have no row. What makes a module part of the
    population is the CALL (a writer name, with ``flag=``), which the callers
    of this look for. The list is `tests/_tree.repo_files` — what git tracks
    (and what is new and not ignored), never a walk of the checkout. A tool
    outside ``scripts/tools`` is named relative to it (``../ops/x.py``), the
    same string its ``Row.tool`` carries. A file that does not parse fails
    the scan: it could not be read, which is not "it calls no writer".
    """
    import _lib_io
    own = {TOOLS_DIR / f"{name.split('.')[0]}.py"
           for names in _lib_io.OUTPUT_ENTRY_POINTS.values() for name in names}
    out = []
    for py in sorted(repo_files(".py")):
        if py in own or py.relative_to(REPO_ROOT).parts[0] == "tests":
            continue
        tree = ast.parse(py.read_text(encoding="utf-8"), str(py))
        out.append((Path(os.path.relpath(py, TOOLS_DIR)).as_posix(), py, tree))
    return out


def _called_name(call: ast.Call) -> str | None:
    f = call.func
    return f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None


def _flag_values(node: ast.expr, module: ast.Module, parents: dict,
                 seen: frozenset = frozenset()) -> set[str]:
    """The flag strings the expression *node* can be, read off the module.

    A literal is itself (``None`` is no flag: an internally derived path). A
    conditional is both arms. A name is what it is bound to: a PARAMETER of
    the function it sits in is whatever that function's callers in this
    module pass for it (and its default); any other name is every assignment
    to it in that function, or at module level. A call of a module function
    is what the function returns. Anything else is reported as unresolved —
    a string the caller turns into a failure, never a silent "no flag".
    """
    def again(expr: ast.expr, more: frozenset = seen) -> set[str]:
        return _flag_values(expr, module, parents, more)

    if isinstance(node, ast.Constant):
        return {node.value} if isinstance(node.value, str) else set()
    if isinstance(node, ast.IfExp):
        return again(node.body) | again(node.orelse)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        returned = [r.value for fn in ast.walk(module)
                    if isinstance(fn, ast.FunctionDef) and fn.name == node.func.id
                    for r in ast.walk(fn) if isinstance(r, ast.Return) and r.value is not None]
        if returned and node.func.id not in seen:
            more = seen | {node.func.id}
            return set().union(*(again(value, more) for value in returned))
    if isinstance(node, ast.Name):
        scope = parents.get(node)
        while scope is not None and not isinstance(scope, ast.FunctionDef):
            scope = parents.get(scope)
        key = (getattr(scope, "name", None), node.id)
        if key in seen:
            return set()
        more = seen | {key}
        values: set[str] = set()
        bound = False
        if scope is not None:
            params = scope.args.args + scope.args.kwonlyargs
            if node.id in [a.arg for a in params]:
                bound = True
                at = [a.arg for a in scope.args.args].index(node.id) \
                    if node.id in [a.arg for a in scope.args.args] else None
                defaults = dict(zip([a.arg for a in scope.args.args][::-1],
                                    scope.args.defaults[::-1]))
                defaults.update({a.arg: d for a, d in zip(scope.args.kwonlyargs,
                                                         scope.args.kw_defaults) if d})
                if node.id in defaults:
                    values |= again(defaults[node.id], more)
                for call in ast.walk(module):
                    if isinstance(call, ast.Call) and _called_name(call) == scope.name:
                        for kw in call.keywords:
                            if kw.arg == node.id:
                                values |= again(kw.value, more)
                        if at is not None and len(call.args) > at:
                            values |= again(call.args[at], more)
        if not bound:
            for where in ([scope] if scope is not None else []) + [module]:
                body = ast.walk(where) if where is not module else iter(module.body)
                for stmt in body:
                    if isinstance(stmt, (ast.Assign, ast.AnnAssign)) and stmt.value is not None:
                        targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
                        if any(isinstance(t, ast.Name) and t.id == node.id for t in targets):
                            bound = True
                            values |= again(stmt.value, more)
                if bound:
                    break
        if bound:
            return values
    return {_UNRESOLVED.format(ast.unparse(node))}


def _flags_named_at_a_writer() -> dict[str, set[str]]:
    """tool → the flags it passes as ``flag=`` to a writer name, for every CLI
    module (not a ``_``-prefixed library: those forward the caller's flag).

    A tool is here when it DECLARES, at the point of writing, that the path
    came from argv. See the module docstring for what that cannot see: a raw
    sink never wrapped is not a missing row, it is absent — and so is a write
    whose entry is passed around as a value and called under another name
    (`_registry_lib` hands `write_repo_artifact` to `_write_or_stop`). Those
    pass no flag; the control runs' audit sees them when a row runs them.
    """
    found: dict[str, set[str]] = {}
    for tool, py, module in _cli_modules():
        if py.name.startswith("_"):
            continue
        flags = _flags_at_writers(module)
        if flags:
            found[tool] = flags
    return found


def _flags_at_writers(module: ast.Module) -> set[str]:
    """Every flag one module hands a writer name as ``flag=``."""
    writers = _writer_names()
    parents = {child: node for node in ast.walk(module)
               for child in ast.iter_child_nodes(node)}
    found: set[str] = set()
    for node in ast.walk(module):
        if not (isinstance(node, ast.Call) and _called_name(node) in writers):
            continue
        for kw in node.keywords:
            if kw.arg != "flag":
                continue
            if isinstance(kw.value, ast.Constant) and kw.value.value is None:
                continue  # said in so many words: an internally derived path
            # ⛔ Not "no flag": an expression that is not the literal None and
            # resolves to nothing is one this scan could not read (a name
            # bound to None and then to something it cannot follow, say).
            found |= (_flag_values(kw.value, module, parents)
                      or {_UNRESOLVED.format(ast.unparse(kw.value))})
    return found


def test_a_flag_expression_that_resolves_to_nothing_is_unresolved_not_absent():
    def flags(source: str) -> set[str]:
        return _flags_at_writers(ast.parse(source))
    assert flags("write_operator_output(p, t, flag=None)") == set()
    assert flags("write_operator_output(p, t, flag='--out')") == {"--out"}
    assert flags("F = '--out' if given else None\nwrite_operator_output(p, t, flag=F)") == {"--out"}
    # Bound, and to nothing this scan can read: that is not "names no flag".
    assert flags("F = None\nwrite_operator_output(p, t, flag=F)") == {
        _UNRESOLVED.format("F")}
    assert flags("write_operator_output(p, t, flag=pick())") == {_UNRESOLVED.format("pick()")}
    assert flags("ensure_dir(p, flag=args.flag)") == {_UNRESOLVED.format("args.flag")}
    assert flags("unrelated(p, flag='--x')") == set()


# ⛔ (tool, flag) pairs that DECLARE an output flag at their writer but cannot
# have a control pair here, each with the measured reason. Exit-locked like
# every other exception list in this ticket: `==` on the size, and an entry
# that is no longer in the derived population (or which grew a row) is a
# failure, so the list can only shrink.
#
# ⚠️ An entry is NOT "this is covered by something else". It is "the
# behavioural gate is silent about this write", and the only thing speaking
# for it is the static side (test_write_failure_class for a secure writer,
# test_output_write_sites_stay_guarded for a raw sink) — which sees the shape
# of the code, never the rc an operator gets.
NO_ROW: dict[Key, str] = {
    ("dx/inject_waveform.py", "--out"):
        "Its `--out` write is the LAST step of an injection run: the tool "
        "pushes a waveform into an isolated vmsingle over HTTP and then "
        "shells out to a `vmalert` binary with `-replay`. Neither exists in "
        "this repo's offline test environment (its own e2e tests skip on "
        "exactly that, tests/dx/test_inject_waveform.py), and every argv that "
        "gets as far as the write needs both. A `requires=` row would be a "
        "permanent skip that reads as a pass — and would still be wrong on a "
        "host that has `vmalert` but no vmsingle. #1789 gave it the shared "
        "`flag=\"--out\"` message anyway, and since #3107 ④ it writes "
        "through the operator entry and its parser refuses `-` and \"\" "
        "(tests/dx/test_inject_waveform.py `test_out_*`). What speaks for the "
        "write failure: its own "
        "`test_out_write_failure_exits_two`, which stubs out the injection "
        "pipeline, calls `main()` IN PROCESS and asserts rc=2 plus the "
        "standard line. That is a weaker pair than a row — no subprocess, no "
        "control half — and it is the reason this entry exists rather than "
        "being silently absent.",
    ("ops/state_reconcile.py", "--state-dir"):
        "The write is the rewrite of a state file after a schema migration, "
        "and no migration exists: `state_reconcile.MIGRATIONS` is empty (the "
        "schema has had one version), so every file that is not at the "
        "current version is reported as unresolvable and none is written. No "
        "argv reaches the call. `test_the_unreachable_state_migration_is_"
        "still_unreachable` holds that premise: the day a migration is "
        "registered it goes red, and this entry gives way to a row.",
}
_NO_ROW_CEILING = 2


def test_every_flag_named_at_a_writer_has_a_row():
    """Derived, not enumerated: a (tool, flag) that names an output flag at a
    writer must have a control pair here, or the row table has gone stale.

    Per FLAG, not per tool (#3107): `generate_alertmanager_routes` had rows
    for `-o` and `--secret-mounts-out`, so the tool counted as covered while
    `--findings-json` — a third sink behind its own flag — had none."""
    named = _flags_named_at_a_writer()
    assert named, "no tool passes flag= to a writer — scanner drift"
    unresolved = sorted((tool, flag) for tool, flags in named.items() for flag in flags
                        if flag.startswith("<"))
    assert not unresolved, (
        "a writer is handed a flag this scan cannot read off the module — teach "
        f"`_flag_values` the shape, do not skip it: {unresolved}")
    rowed = {(r.tool, r.flag) for r in ROWS}
    missing = sorted({(tool, flag) for tool, flags in named.items() for flag in flags}
                     - rowed - set(NO_ROW))
    assert not missing, (
        "(tool, flag) named at a writer with no control pair in ROWS: "
        f"{missing}")


def test_every_row_is_about_a_flag_its_tool_names_at_a_writer():
    """The other direction: a row whose flag the tool no longer names at any
    writer is evidence about nothing the population holds — the tool renamed
    the flag, or stopped declaring it."""
    named = _flags_named_at_a_writer()
    stale = sorted({(r.tool, r.flag) for r in ROWS
                    if r.flag not in named.get(r.tool, ())})
    assert not stale, f"row(s) for a flag the tool does not name at a writer: {stale}"


#: Modules that call an operator/artifact entry on a path NO FLAG names — a
#: fixed file of the repository. A row is a flag, so none can be written for
#: them: they are outside this table's population (the module docstring,
#: POPULATION), listed so that the file-level test below says which callers
#: it does not hold rather than pass over them. Exact both ways; an entry
#: that names a flag at a writer is not one of these.
ENTRY_CALLERS_WITH_NO_FLAG: dict[str, str] = {
    "lint/check_frontmatter_versions.py":
        "`--fix` rewrites the docs it has just scanned under the repository's "
        "own `docs/`; the tool takes no path. A write that fails is held by "
        "tests/lint/test_check_frontmatter_versions.py "
        "(test_cli_fix_that_cannot_write_a_doc_is_exit_2).",
    "ops/_observed_map_lib.py":
        "`threshold-recommend --generate-observed-map` rewrites the committed "
        "`metric_observed_map.yaml` beside the library; the flag is a switch, "
        "not a path. A write that fails is held by "
        "tests/ops/test_threshold_recommend.py "
        "(test_generate_observed_map_that_cannot_be_written_is_exit_2).",
}
_ENTRY_CALLERS_WITH_NO_FLAG_CEILING = 2


def _entry_calls_name_only(module: ast.Module, entries: set[str], flags: set[str]) -> bool:
    """Whether every call of an entry in *module* names a flag, and only
    flags in *flags* (an empty *flags*: never)."""
    if not flags:
        return False
    parents = {child: node for node in ast.walk(module)
               for child in ast.iter_child_nodes(node)}
    for node in ast.walk(module):
        if not (isinstance(node, ast.Call) and _called_name(node) in entries):
            continue
        kw = next((k for k in node.keywords if k.arg == "flag"), None)
        if kw is None or (isinstance(kw.value, ast.Constant) and kw.value.value is None):
            return False
        named = _flag_values(kw.value, module, parents)
        if not named or not named <= flags:
            return False
    return True


def test_a_no_row_flag_excuses_only_calls_that_name_it():
    entries = {"write_operator_output"}
    def ok(source: str) -> bool:
        return _entry_calls_name_only(ast.parse(source), entries, {"--out"})
    assert ok("write_operator_output(p, t, flag='--out')")
    assert not ok("write_operator_output(p, t, flag='--out')\n"
                  "write_operator_output(q, t, flag=None)")
    assert not ok("write_operator_output(p, t, flag='--out')\nwrite_operator_output(q, t)")
    assert not ok("write_operator_output(p, t, flag='--other')")
    assert not _entry_calls_name_only(ast.parse("write_operator_output(p, t, flag='--out')"),
                                      entries, set())


def test_every_caller_of_a_two_class_entry_has_a_row():
    """File level, and regardless of ``flag=``: a module under the CLI roots
    — a library too — that CALLS one of `_lib_io`'s operator or artifact
    entries has a row that runs it. The pair-level test above reads ``flag=``
    and skips libraries; a call that names no flag, or sits in a library, is
    still a write through an entry that some control run has to exercise
    (carried over from the file-level audit this table replaced). The callers
    no row can be written for are `ENTRY_CALLERS_WITH_NO_FLAG` (no flag at
    the writer) and the tools of `NO_ROW` (a flag, and a measured reason)."""
    import _lib_io
    entries = {name.split(".")[1] for kind in CLASSES
               for name in _lib_io.OUTPUT_ENTRY_POINTS[kind] if name.startswith("_lib_io.")}
    modules = {tool: tree for tool, _py, tree in _cli_modules()}
    callers = {tool for tool, tree in modules.items()
               if any(isinstance(node, ast.Call) and _called_name(node) in entries
                      for node in ast.walk(tree))}
    assert callers, "no caller of an operator/artifact entry found — the scan measured nothing"
    rowed = {r.tool for r in ROWS}
    # A tool whose EVERY entry call names a flag in NO_ROW is accounted for
    # there, with its reason (#3107 ④: inject_waveform --out reached this
    # population when it moved off the old names). One more call beside it —
    # `flag=None`, or another flag — is not, and still needs a row.
    no_row = {tool for tool in callers
              if _entry_calls_name_only(modules[tool], entries,
                                        {flag for t, flag in NO_ROW if t == tool})}
    missing = sorted(callers - rowed - no_row - set(ENTRY_CALLERS_WITH_NO_FLAG))
    assert not missing, f"module(s) calling an operator/artifact entry with no row: {missing}"
    stale = sorted(tool for tool in ENTRY_CALLERS_WITH_NO_FLAG
                   if tool not in callers or tool in rowed or _flags_at_writers(modules[tool]))
    assert not stale, (
        "listed as calling an entry with no flag, and it calls none, has a row, "
        f"or names a flag at a writer: {stale}")
    assert all(why.strip() for why in ENTRY_CALLERS_WITH_NO_FLAG.values())
    assert len(ENTRY_CALLERS_WITH_NO_FLAG) == _ENTRY_CALLERS_WITH_NO_FLAG_CEILING, sorted(
        ENTRY_CALLERS_WITH_NO_FLAG)


def test_the_no_row_list_is_exact_and_not_stale():
    """The exception list's own gate: fixed size, every entry still in the
    population, none of them quietly grown a row (which would make the
    exception a lie), and every entry carrying a reason."""
    assert len(NO_ROW) == _NO_ROW_CEILING, sorted(NO_ROW)
    named = _flags_named_at_a_writer()
    rowed = {(r.tool, r.flag) for r in ROWS}
    stale = []
    for (tool, flag), reason in sorted(NO_ROW.items()):
        where = f"{tool} {flag}"
        if not (TOOLS_DIR / tool).is_file():
            stale.append(f"{where}: no such tool any more")
        elif flag not in named.get(tool, ()):
            stale.append(f"{where}: no longer named at a writer — delete this entry")
        elif (tool, flag) in rowed:
            stale.append(f"{where}: has a row now — delete this entry")
        elif not reason.strip():
            stale.append(f"{where}: needs a reason")
    assert not stale, "\n".join(stale)


def test_the_unreachable_state_migration_is_still_unreachable():
    """NO_ROW's reason for `state_reconcile --state-dir`, held: with no
    migration registered, no run rewrites a state file."""
    import state_reconcile
    assert state_reconcile.MIGRATIONS == {}, (
        "a state migration is registered: the rewrite under --state-dir is "
        "reachable now — give it a row and drop its NO_ROW entry")


def test_the_entry_point_list_is_complete():
    """`_lib_io.OUTPUT_ENTRY_POINTS` is what the population and the audit are
    derived from, so a writer missing from it is a hole in both. Held from
    the code: every name resolves to a function that takes ``flag``; every
    top-level function of `_lib_io` / `_lib_atomic_write` that takes ``flag``
    is named; and so is every function of a library module that hands its
    own ``flag`` on to a named one (a wrapper)."""
    import _lib_io
    # The two private functions the audit holds each class's write to.
    assert callable(getattr(_lib_io, _OPERATOR_OPENER.rpartition(":")[2], None)), (
        f"{_OPERATOR_OPENER} is gone: name the function that opens an operator path now")
    assert (REPO_ROOT / _ATOMIC_WRITER.rstrip(":")).is_file(), _ATOMIC_WRITER
    named = {name for names in _lib_io.OUTPUT_ENTRY_POINTS.values() for name in names}
    assert len(named) == sum(len(v) for v in _lib_io.OUTPUT_ENTRY_POINTS.values()), (
        "a function is listed under two kinds")
    takes_flag: dict[str, ast.FunctionDef] = {}
    for py in sorted(f for f in repo_files(".py")
                     if f.parent == TOOLS_DIR and f.name.startswith("_")):
        for fn in ast.parse(py.read_text(encoding="utf-8")).body:
            if isinstance(fn, ast.FunctionDef) and "flag" in [
                    a.arg for a in fn.args.args + fn.args.kwonlyargs]:
                takes_flag[f"{py.stem}.{fn.name}"] = fn
    assert not sorted(named - set(takes_flag)), (
        f"listed, but no such function taking flag=: {sorted(named - set(takes_flag))}")
    short = {name.split(".")[1] for name in named}
    should = set()
    for name, fn in takes_flag.items():
        if name.split(".")[0] in ("_lib_io", "_lib_atomic_write"):
            should.add(name)
        elif any(isinstance(n, ast.Call) and _called_name(n) in short
                 and any(kw.arg == "flag" and isinstance(kw.value, ast.Name)
                         and kw.value.id == "flag" for kw in n.keywords)
                 for n in ast.walk(fn)):
            should.add(name)
    assert not sorted(should - named), (
        "function(s) that write an output path for a caller's flag and are not "
        f"in _lib_io.OUTPUT_ENTRY_POINTS: {sorted(should - named)}")


# ═══════════════════════════════════════════════════════════════════════════
# The flag manifest ⇔ this table (#3107 S2)
#
# `caller_path_manifest.yaml` declares every da-tools flag; `kind: output` is
# "a path the tool writes". Each such flag must be a row here — so declaring
# an output flag is what brings it under the bad-path pair and the write
# audit, with no second registry to keep in step.
# ═══════════════════════════════════════════════════════════════════════════

#: `kind: output` flags with no row, and why. Exact both ways, like the lists
#: above: a new output flag without a row is red, and so is an entry whose
#: flag got a row or left the manifest.
MANIFEST_OUTPUT_WITHOUT_ROW: dict[Key, str] = {
    ("generator-service", "--socket"):
        "Not a file the tool writes: the Unix socket the service binds and "
        "listens on (an old socket there is replaced, anything else refuses). "
        "No write entry is, or should be, on that path; `output` is the "
        "nearest kind the manifest has for a path the tool creates.",
    ("generator-service", "--work-dir"):
        "Not an output the operator reads: the scratch directory the service "
        "builds each judged tree in (`jobs/…`) and removes afterwards. A "
        "long-running service has no control run that ends rc 0 with a file "
        "left to look at.",
}


def _manifest_output_flags() -> dict[Key, str]:
    """(command, flag) → the script behind the command, for every flag the
    manifest declares ``kind: output``. The command → script map is the walk
    of the da-tools parsers the manifest's own gate uses."""
    import test_caller_path_manifest as manifest
    parsed, _unreached = manifest.parsed_arguments()
    outputs = {}
    for key, declared in manifest._entries(manifest.load_manifest()).items():
        if declared.get("kind") == "output":
            assert key in parsed, f"{key}: declared in the manifest, not on the parser"
            outputs[key] = parsed[key]
    return outputs


def manifest_join_faults(outputs: dict[Key, str], rows: list[Row],
                         without_row: dict[Key, str]) -> list[str]:
    """Hold the manifest's output flags to the rows. A row answers for a
    manifest flag when it is the same script and the flag is one of the
    spellings in ``Row.flag`` (``-o/--output-dir`` answers ``--output-dir``)."""
    rowed = {(row.tool, spelling) for row in rows for spelling in row.flag.split("/")}
    faults = []
    for (command, flag), script in sorted(outputs.items()):
        key = (command, flag)
        if (script, flag) in rowed:
            if key in without_row:
                faults.append(f"{command} {flag}: has a row now — drop it from "
                              "MANIFEST_OUTPUT_WITHOUT_ROW")
        elif key not in without_row:
            faults.append(f"{command} {flag}: declared `kind: output` and {script} has "
                          "no row for it — add the control pair")
    for command, flag in sorted(set(without_row) - set(outputs)):
        faults.append(f"{command} {flag}: not an output flag of the manifest any more — "
                      "drop it from MANIFEST_OUTPUT_WITHOUT_ROW")
    for key, reason in sorted(without_row.items()):
        if not reason.strip():
            faults.append(f"{key[0]} {key[1]}: needs a reason")
    return faults


def test_every_output_flag_of_the_manifest_has_a_row():
    outputs = _manifest_output_flags()
    assert outputs, "the manifest declares no output flag — the join measured nothing"
    faults = manifest_join_faults(outputs, ROWS, MANIFEST_OUTPUT_WITHOUT_ROW)
    assert not faults, "\n".join(faults)


class TestManifestJoin:
    _OUT = {("cmd", "--output-dir"): "ops/t.py"}
    _ROW = [Row("ops/t.py", "-o/--output-dir", "dir", "operator", lambda c, out: [],
                reach="synthetic")]

    def test_a_row_answers_for_either_spelling(self):
        assert manifest_join_faults(self._OUT, self._ROW, {}) == []

    def test_an_output_flag_without_a_row_is_a_fault(self):
        assert len(manifest_join_faults(self._OUT, [], {})) == 1
        other_script = [Row("ops/u.py", "-o/--output-dir", "dir", "operator",
                            lambda c, out: [], reach="synthetic")]
        assert len(manifest_join_faults(self._OUT, other_script, {})) == 1

    def test_a_listed_exception_stands_only_while_it_is_true(self):
        listed = {("cmd", "--output-dir"): "because"}
        assert manifest_join_faults(self._OUT, [], listed) == []
        assert manifest_join_faults(self._OUT, self._ROW, listed) == [
            "cmd --output-dir: has a row now — drop it from MANIFEST_OUTPUT_WITHOUT_ROW"]
        assert manifest_join_faults({}, [], listed) == [
            "cmd --output-dir: not an output flag of the manifest any more — "
            "drop it from MANIFEST_OUTPUT_WITHOUT_ROW"]
        assert manifest_join_faults(self._OUT, [], {("cmd", "--output-dir"): " "}) == [
            "cmd --output-dir: needs a reason"]


# ═══════════════════════════════════════════════════════════════════════════
# Write-audit controls — the hook and the verdict, on hand-made input
#
# Every control run is on a list or clean TODAY, so by themselves they say
# nothing about whether a wrong write would be caught. These drive each
# rule both ways: on a log written by hand, and on the real hook run over a
# few lines of Python.
# ═══════════════════════════════════════════════════════════════════════════
_OP = ["scripts/tools/_lib_io.py:_kernel_open_operator_output",
       "scripts/tools/_lib_io.py:open_operator_output",
       "scripts/tools/_lib_io.py:write_operator_output", "scripts/tools/ops/t.py:main"]
_ART = ["scripts/tools/_lib_atomic_write.py:_try_atomic",
        "scripts/tools/_lib_atomic_write.py:atomic_write_text",
        "scripts/tools/_lib_io.py:write_repo_artifact", "scripts/tools/ops/t.py:main"]
_MKDIR = ["scripts/tools/_lib_io.py:ensure_dir", "scripts/tools/ops/t.py:main"]
_RAW = ["scripts/tools/ops/t.py:main"]


def _audit_row(cls: str = "operator", kind: str = "file", **kw) -> Row:
    return Row("ops/t.py", "--out", kind, cls, lambda c, out: [], reach="synthetic", **kw)


class _Log:
    """A hand-made audit log for a run whose output directory is *scope*."""

    def __init__(self, scope: Path, loaded: bool = True):
        self.scope = scope
        self.tool = TOOLS_DIR / "ops" / "t.py"
        self.events: list[dict] = (
            [{"ev": "loaded", "argv": ["python3", str(self.tool)], "pid": 1}] if loaded else [])

    def add(self, ev: str, rel: str, stack: list[str], **more) -> "_Log":
        """An event on *rel* under the scope; `lines=` gives the frames' line
        numbers, `fd=` marks it as raised on a descriptor (resolved)."""
        self.events.append({"ev": ev, "path": str(self.scope / rel), "stack": stack,
                            "pid": 1, **more})
        return self

    def spawn(self, *argv: str) -> "_Log":
        self.events.append({"ev": "spawn", "exe": argv[0], "argv": list(argv), "pid": 1})
        return self

    def seen(self, row: Row, made: tuple[str, ...] = ()) -> Seen:
        return _verdict(row, _audit(self.events, self.scope, self.tool, frozenset(made)))


class TestWriteAuditVerdict:
    def test_an_operator_write_through_the_operator_entry_is_clean(self, tmp_path):
        seen = _Log(tmp_path).add("os.mkdir", ".", _MKDIR).add("open", "o.json", _OP).seen(
            _audit_row())
        assert seen == Seen([], (), False, None)

    def test_an_artifact_replaced_through_the_artifact_entry_is_clean(self, tmp_path):
        log = _Log(tmp_path).add("open", ".o.json.tmp", _ART)
        log.add("os.rename", "o.json", _ART, src=str(tmp_path / ".o.json.tmp"))
        assert log.seen(_audit_row("artifact")) == Seen([], (), False, None)

    def test_the_wrong_class_is_seen(self, tmp_path):
        """Both directions: the row's class is an assertion about the entry."""
        seen = _Log(tmp_path).add("open", "o.json", _OP).seen(_audit_row("artifact"))
        assert seen.other_class == "operator"
        log = _Log(tmp_path).add("open", ".t", _ART).add("os.rename", "o.json", _ART)
        assert log.seen(_audit_row("operator")).other_class == "artifact"

    def test_a_raw_write_is_listed_by_site(self, tmp_path):
        log = _Log(tmp_path).add("open", "o.json", _OP).add("open", "extra.json", _RAW)
        log.add("os.mkdir", "sub", _RAW)
        seen = log.seen(_audit_row())
        assert seen.raw == ("open extra.json", "os.mkdir sub") and not seen.raw_is_all
        only_raw = _Log(tmp_path).add("open", "o.json", _RAW).seen(_audit_row())
        assert only_raw.raw == ("open o.json",) and only_raw.raw_is_all

    def test_a_rename_out_of_the_output_path_is_a_write_there(self, tmp_path):
        """The destination is elsewhere, the name taken away is not: moving
        the output aside and letting the entry write it again leaves a clean
        file on disk and one raw event."""
        log = _Log(tmp_path).add("open", "o.json", _OP)
        log.add("os.rename-from", "o.json", _RAW).add("open", "o.json", _OP)
        assert log.seen(_audit_row()).raw == ("os.rename-from o.json",)

    def test_a_child_given_the_output_path_is_marked(self, tmp_path):
        good = tmp_path / "good"
        good.mkdir()

        def programs(*argv: str, cwd: str | None = None) -> tuple[str, ...]:
            log = _Log(good).add("open", "o.json", _OP).spawn(*argv)
            if cwd is not None:
                log.events[-1]["cwd"] = cwd
            return log.seen(_audit_row()).spawned

        mark = GIVEN_THE_OUTPUT_PATH
        assert programs("git", "log", "-1") == ("git",)
        assert programs("git", "log", f"--output={good}/o.json") == ("git" + mark,)
        assert programs("openssl", "rsa", "-out", str(good / "o.json")) == ("openssl" + mark,)
        assert programs("sh", "-c", f"cat /etc/hostname > {good}/o.json") == ("sh" + mark,)
        assert programs("cp", "a.txt", "o.json", cwd=str(good)) == ("cp" + mark,)
        assert programs("cp", "a.txt", "good/o.json", cwd=str(tmp_path)) == ("cp" + mark,)
        assert programs("cp", "a.txt", "--to=good/o.json", cwd=str(tmp_path)) == ("cp" + mark,)
        # Beside it is not under it, by path component or by name.
        assert programs("cp", "a.txt", str(tmp_path / "other" / "o.json")) == ("cp",)
        assert programs("cp", "a.txt", "o.json", cwd=str(tmp_path)) == ("cp",)
        # The one child that is meant to write there is held by `writer=`.
        assert programs("da-guard", "write", str(good)) == ("da-guard",)
        assert programs("da-guard", "parse-tree", str(good)) == ("da-guard" + mark,)

    def test_the_operator_entry_must_have_let_the_kernel_open_it(self, tmp_path):
        """`open_operator_output` rewritten as `io.open(path, "w")` still has
        the entry's frame on the stack — and is not the entry any more."""
        no_opener = [f for f in _OP if f != _OPERATOR_OPENER]
        seen = _Log(tmp_path).add("open", "o.json", no_opener).seen(_audit_row())
        assert any("not through `_kernel_open_operator_output`" in f for f in seen.faults)

    def test_the_artifact_entry_must_have_used_the_atomic_writer(self, tmp_path):
        """`write_repo_artifact` with a temp file and `os.replace` of its own."""
        own = ["scripts/tools/_lib_io.py:write_repo_artifact", "scripts/tools/ops/t.py:main"]
        log = _Log(tmp_path).add("open", ".o.tmp", own).add("os.rename", "o.json", own)
        faults = log.seen(_audit_row("artifact")).faults
        assert sum("not through `_lib_atomic_write`" in f for f in faults) == 2, faults

    def test_a_descriptor_event_of_the_operator_entry_is_the_same_write(self, tmp_path):
        """The entry wraps the fd it opened and sets its mode on it: the same
        file again. The same two events from anywhere else are writes."""
        wrap = [f for f in _OP if f != _OPERATOR_OPENER]
        log = _Log(tmp_path).add("open", "o.json", _OP).add("open", "o.json", wrap, fd=5)
        log.add("os.chmod", "o.json", _OP, fd=5)
        assert log.seen(_audit_row()) == Seen([], (), False, None)
        outside = _Log(tmp_path).add("open", "o.json", _OP).add("os.chmod", "o.json", wrap, fd=5)
        assert any("outside its opener" in f for f in outside.seen(_audit_row()).faults)
        later = _Log(tmp_path).add("open", "o.json", _OP).add("os.chmod", "o.json", _RAW, fd=5)
        assert later.seen(_audit_row()).raw == ("os.chmod o.json",)

    def test_an_event_the_hook_could_not_place_is_a_fault(self, tmp_path):
        """Not "elsewhere": unknown. It is never read against a directory."""
        log = _Log(tmp_path).add("open", "o.json", _OP)
        log.events.append({"ev": "open", "path": "o.json", "unplaced": True, "stack": _RAW,
                           "pid": 1})
        assert any("could not be placed" in f for f in log.seen(_audit_row()).faults)
        fd = _Log(tmp_path).add("open", "o.json", _OP)
        fd.events.append({"ev": "os.chmod", "path": 7, "fd": 7, "unplaced": True, "stack": [],
                          "pid": 1})
        assert any("could not be placed" in f for f in fd.seen(_audit_row()).faults)

    def test_a_hook_error_is_a_fault(self, tmp_path):
        log = _Log(tmp_path).add("open", "o.json", _OP)
        log.events.append({"ev": "hook-error", "event": "os.rename", "error": "IndexError()",
                           "pid": 1})
        assert any("the hook failed on a os.rename event" in f
                   for f in log.seen(_audit_row()).faults)

    def test_the_programs_a_run_starts_are_named(self, tmp_path):
        log = _Log(tmp_path).add("open", "o.json", _OP)
        log.spawn("/x/da-guard", "parse-tree", "--json").spawn("/usr/bin/cp", "/etc/hostname")
        log.spawn("/usr/local/bin/python3.13", "-c", "pass").spawn("git", "--no-pager", "log")
        log.spawn("/x/da-guard", "parse-tree")
        assert log.seen(_audit_row()).spawned == ("cp", "da-guard", "git", "python")

    def test_the_innermost_entry_decides(self, tmp_path):
        """What is asked is who opened the file. A wrapper over the operator
        entry (`write_yaml_crd`) leaves the write the operator entry's — and
        one entry calling another must not count twice."""
        nested = [*_OP[:3], "scripts/tools/_lib_yaml.py:write_yaml_crd",
                  "scripts/tools/ops/t.py:main"]
        seen = _Log(tmp_path).add("open", "o.json", nested).seen(_audit_row())
        assert seen == Seen([], (), False, None)
        # Two classes on one stack: the inner one is the write's. Read from
        # the outside in, this would be the artifact entry's.
        inside_artifact = [*_OP[:3], "scripts/tools/_lib_io.py:write_repo_artifact",
                           "scripts/tools/ops/t.py:main"]
        seen = _Log(tmp_path).add("open", "o.json", inside_artifact).seen(_audit_row())
        assert seen == Seen([], (), False, None)

    def test_no_log_is_a_fault_not_a_pass(self, tmp_path):
        seen = _verdict(_audit_row(), _audit(None, tmp_path, TOOLS_DIR / "ops" / "t.py"))
        assert len(seen.faults) == 1 and "left no log" in seen.faults[0]

    def test_a_tool_that_never_reported_in_is_a_fault(self, tmp_path):
        """A log some OTHER process wrote (a child) does not vouch for the tool."""
        log = _Log(tmp_path, loaded=False).add("open", "o.json", _OP)
        log.events.insert(0, {"ev": "loaded", "argv": ["python3", "-c", "pass"], "pid": 2})
        assert any("no `loaded` line" in f for f in log.seen(_audit_row()).faults)

    def test_no_file_written_under_the_output_path_is_a_fault(self, tmp_path):
        """Not "no bad event": the empty set is a failure."""
        seen = _Log(tmp_path).add("os.mkdir", ".", _MKDIR).seen(_audit_row())
        assert any("no file was written" in f for f in seen.faults)
        elsewhere = _Log(tmp_path / "good")
        elsewhere.events.append({"ev": "open", "path": str(tmp_path / "other" / "o.json"),
                                 "stack": _OP, "pid": 1})
        assert any("no file was written" in f for f in elsewhere.seen(_audit_row()).faults)

    def test_a_da_guard_row_needs_the_spawn_and_no_python_write(self, tmp_path):
        row = _audit_row(writer="da-guard")
        ok = _Log(tmp_path).add("os.mkdir", ".", _MKDIR).spawn("/x/da-guard", "write", "--json")
        assert ok.seen(row, made=("a.yaml",)).faults == []
        no_spawn = _Log(tmp_path).spawn("/x/da-guard", "parse-tree")
        assert any("spawned none" in f for f in no_spawn.seen(row, made=("a.yaml",)).faults)
        wrote = _Log(tmp_path).spawn("da-guard", "write").add("open", "o.json", _OP)
        assert any("Python wrote" in f for f in wrote.seen(row, made=("a.yaml",)).faults)
        nothing_left = _Log(tmp_path).spawn("da-guard", "write")
        assert any("say what the writer is" in f for f in nothing_left.seen(row).faults)

    def test_a_file_with_no_logged_write_needs_a_declared_writer(self, tmp_path):
        """The log against the disk: a file the run left that nothing in the
        log wrote is not waved through — the row has to say who."""
        log = _Log(tmp_path).add("open", "o.json", _OP).spawn("da-guard", "write")
        made = ("o.json", "conf.d/a.yaml")
        assert any("no audited process wrote: ['conf.d/a.yaml']" in f
                   for f in log.seen(_audit_row(), made).faults)
        assert log.seen(_audit_row(writer="python+da-guard"), made).faults == []
        # ... and "both" is held on both sides.
        assert any("say what the writer is" in f for f in
                   log.seen(_audit_row(writer="python+da-guard"), ("o.json",)).faults)
        only_guard = _Log(tmp_path).spawn("da-guard", "write")
        assert any("no file was written" in f for f in
                   only_guard.seen(_audit_row(writer="python+da-guard"), ("a.yaml",)).faults)
        # A temp file renamed into place is logged under its final name.
        atomic = _Log(tmp_path).add("open", ".o.tmp", _ART).add("os.rename", "o.json", _ART)
        assert atomic.seen(_audit_row("artifact"), ("o.json",)).faults == []

    def test_an_operator_path_is_only_ever_opened(self, tmp_path):
        seen = _Log(tmp_path).add("open", "o.json", _OP).add("os.chmod", "o.json", _OP).seen(
            _audit_row())
        assert any("os.chmod o.json under the operator entry" in f for f in seen.faults)

    def test_an_artifact_is_renamed_into_place_never_opened(self, tmp_path):
        in_place = _Log(tmp_path).add("open", "o.json", _ART).seen(_audit_row("artifact"))
        assert any("renamed nothing" in f for f in in_place.faults)
        both = _Log(tmp_path).add("open", "o.json", _ART).add("os.rename", "o.json", _ART)
        assert any("opened for writing" in f for f in both.seen(_audit_row("artifact")).faults)

    def test_ensure_dir_makes_directories_only(self, tmp_path):
        seen = _Log(tmp_path).add("open", "o.json", _MKDIR).seen(_audit_row())
        assert any("under ensure_dir" in f for f in seen.faults)


class TestWriteAuditPlacing:
    """What is, and is not, under the output path."""

    def test_a_descriptor_that_is_no_file_is_nowhere(self):
        """A pipe to a child, resolved by the hook to a name that is not a
        path; and a descriptor it could not resolve at all."""
        assert _placed({"ev": "open", "path": "pipe:[4711]", "fd": 7, "stack": []}) is None
        assert _placed({"ev": "open", "path": 7, "fd": 7, "unplaced": True,
                        "stack": []}) is None

    def test_an_unplaced_event_is_not_read_against_anything(self, tmp_path):
        event = {"ev": "open", "path": "o.json", "unplaced": True, "cwd": str(tmp_path),
                 "stack": []}
        assert _placed(event) is None
        absolute = {"ev": "open", "path": str(tmp_path / "o.json"), "unplaced": True,
                    "stack": []}
        assert _placed(absolute) is None

    def test_files_under_counts_everything_that_is_not_a_directory(self, tmp_path):
        (tmp_path / "d").mkdir()
        (tmp_path / "d" / "f.txt").write_text("x", encoding="utf-8")
        symlink_or_skip(tmp_path / "nowhere", tmp_path / "dangling")
        expected = {"d/f.txt", "dangling"}
        if hasattr(os, "mkfifo"):
            os.mkfifo(tmp_path / "pipe")
            expected.add("pipe")
        assert _files_under(tmp_path) == expected

    def test_a_dir_fd_relative_path_is_not_read_against_the_cwd(self, tmp_path):
        """`rmtree` under a TemporaryDirectory removes `<name>` relative to
        the temp directory's fd. Unresolved, it has no `cwd`; joining it to
        one would put a scratch file inside the tree the tool runs in."""
        assert _placed({"ev": "os.remove", "path": "o.json", "dir_fd": 9, "stack": []}) is None
        with_cwd = {"ev": "open", "path": "o.json", "cwd": str(tmp_path), "stack": []}
        assert _placed(with_cwd) == str(tmp_path / "o.json")

    def test_under_is_by_path_component(self, tmp_path):
        good = tmp_path / "good"
        good.mkdir()
        assert _under(str(good), good) == "."
        assert _under(str(good / "a" / "b.json"), good) == "a/b.json"
        assert _under(str(tmp_path / "good-2" / "b.json"), good) is None
        assert _under(str(tmp_path / "tool-tmp" / "x"), good) is None
        assert _under("/dev/null", good) is None
        assert _under(str(REPO_ROOT / "scripts/tools/__pycache__/x.pyc"), good) is None

    def test_the_tools_temp_directory_is_beside_the_output_path(self, tmp_path):
        """pytest's tmp_path is under the system temp directory. Were the
        tool's TMPDIR left alone, "a scratch file" and "under the output
        path" could not be told apart by where they are."""
        good = tmp_path / "good"
        good.mkdir()
        env = _audit_env(tmp_path / "log", tmp_path)
        assert _under(env["TMPDIR"], good) is None
        assert _under(env["TMPDIR"], tmp_path) == "tool-tmp"

    def test_a_symlinked_directory_is_resolved(self, tmp_path):
        real = tmp_path / "real"
        real.mkdir()
        symlink_or_skip(real, tmp_path / "link", target_is_directory=True)
        placed = _placed({"ev": "open", "path": str(tmp_path / "link" / "o.json"), "stack": []})
        assert _under(placed, real) == "o.json"


_HOOK_SNIPPET = """
import os, subprocess, sys, tempfile
out = sys.argv[1]
sys.path.insert(0, os.path.join(sys.argv[2], "ops", ".."))   # how a tool under ops/ finds it
import _lib_io
_lib_io.write_operator_output(os.path.join(out, "op.txt"), "x", flag=None)
_lib_io.write_repo_artifact(os.path.join(out, "art.txt"), "x")
_lib_io.ensure_dir(os.path.join(out, "sub"))
os.chdir(out)
open("raw.txt", "w").close()
with tempfile.TemporaryDirectory() as scratch:
    open(os.path.join(scratch, "in-tmp.txt"), "w").close()
open(os.devnull, "w").close()
open("raw.txt").close()
subprocess.run([sys.executable, "-c", "open('child.txt', 'w').close()"], check=True)
subprocess.run([sys.executable, "-c", "pass"], check=True)
"""


def _hooked(tmp: Path, code: str, *, env: dict[str, str] | None = None,
            args: tuple[str, ...] = ()) -> tuple[list[dict] | None, Path, Path]:
    """Run *code* under the hook with `good/` as argv[1]: (the log's events,
    `good/`, the script)."""
    good = tmp / "good"
    good.mkdir(exist_ok=True)
    script = tmp / "snippet.py"
    script.write_text(code, encoding="utf-8")
    log = tmp / "log.jsonl"
    subprocess.run([sys.executable, str(script), str(good), *args],
                   env={**os.environ, **(_audit_env(log, tmp) if env is None else env)},
                   check=True, timeout=TIMEOUT_S, capture_output=True)
    return _read_audit_log(log), good, script


class TestTheHookItself:
    """The real hook over a few lines of Python: what it logs is what the
    verdict above is fed."""

    def test_each_write_is_attributed_to_its_entry(self, tmp_path):
        events, good, script = _hooked(tmp_path, _HOOK_SNIPPET, args=(str(TOOLS_DIR),))
        audit = _audit(events, good, script)
        assert audit.logged and audit.loaded and not audit.faults
        assert audit.sites["operator"] == (("open", "op.txt"),)
        assert ("os.rename", "art.txt") in audit.sites["artifact"]
        assert ("open", "art.txt") not in audit.sites["artifact"]
        assert audit.sites["directory"] == (("os.mkdir", "sub"),)
        # A relative path (after the chdir) and a child Python's write both
        # land under the output path, raw; the read-mode open does not.
        assert set(audit.sites["raw"]) == {("open", "raw.txt"), ("open", "child.txt")}
        assert (Path(sys.executable).name, "-c", "pass") in audit.spawned

    def test_the_scratch_file_and_its_cleanup_are_not_under_the_output_path(self, tmp_path):
        events, _good, _script = _hooked(tmp_path, _HOOK_SNIPPET, args=(str(TOOLS_DIR),))
        scratch = [e for e in events if isinstance(e.get("path"), str)
                   and e["path"].endswith("in-tmp.txt")]
        assert {e["ev"] for e in scratch} == {"open", "os.remove"}, scratch
        for event in scratch:
            assert _under(_placed(event), tmp_path / "tool-tmp") is not None, event
        assert any(e.get("path") == "/dev/null" for e in events)

    def test_without_the_hook_on_the_path_there_is_no_log(self, tmp_path):
        """The environment minus PYTHONPATH: the run succeeds, nothing is
        logged, and the verdict is a fault — not an empty, passing log."""
        env = _audit_env(tmp_path / "log.jsonl", tmp_path)
        env["PYTHONPATH"] = os.environ.get("PYTHONPATH", "")
        events, good, script = _hooked(tmp_path, _HOOK_SNIPPET, env=env,
                                       args=(str(TOOLS_DIR),))
        audit = _audit(events, good, script)
        assert not audit.logged
        assert _verdict(_audit_row(), audit).faults

    def test_a_descriptor_event_is_resolved_to_its_file(self, tmp_path):
        """`fchmod` / `ftruncate` / `open(fd)` name no path; the hook reads it
        back from the descriptor, so they land where the file is."""
        if not Path("/proc/self/fd").is_dir():
            pytest.skip("no /proc: a descriptor cannot be resolved here")
        events, good, script = _hooked(tmp_path, """
import os, sys
fd = os.open(os.path.join(sys.argv[1], "f.txt"), os.O_WRONLY | os.O_CREAT, 0o600)
os.fchmod(fd, 0o666)
os.ftruncate(fd, 0)
os.fdopen(fd, "w").close()
""")
        audit = _audit(events, good, script)
        assert set(audit.sites["raw"]) == {("open", "f.txt"), ("os.chmod", "f.txt"),
                                           ("os.truncate", "f.txt")}
        assert sum("fd" in e for e in events) == 3 and not audit.faults

    def test_a_relative_os_open_cannot_be_placed(self, tmp_path):
        """The audit event of `os.open` carries no `dir_fd`: a relative path
        is relative to nobody-knows-what, and is a fault rather than a guess
        — here it IS relative to a directory descriptor, and lands in good/."""
        events, good, script = _hooked(tmp_path, """
import os, sys
d = os.open(sys.argv[1], os.O_RDONLY)
os.chdir(os.path.dirname(sys.argv[1]))
fd = os.open("smuggled.txt", os.O_WRONLY | os.O_CREAT, 0o600, dir_fd=d)
os.write(fd, b"x")
os.close(fd)
os.mkdir("made", dir_fd=d)
""")
        assert (good / "smuggled.txt").is_file()
        audit = _audit(events, good, script, _files_under(good))
        assert any("open 'smuggled.txt'" in f and "could not be placed" in f
                   for f in audit.faults), audit.faults
        assert audit.unlogged == ("smuggled.txt",)
        if Path("/proc/self/fd").is_dir():
            # ... while an event that DOES carry its dir_fd is placed by it.
            assert ("os.mkdir", "made") in audit.sites["raw"]

    def test_a_relative_path_in_a_working_directory_that_is_gone(self, tmp_path):
        """`os.getcwd()` raises there. The program must not see that: the
        event is logged unplaced, and the run goes on."""
        events, good, script = _hooked(tmp_path, """
import os, sys, tempfile
d = tempfile.mkdtemp()
os.chdir(d)
os.rmdir(d)
try:
    open("x.txt", "w")
except FileNotFoundError:
    pass
open(os.path.join(sys.argv[1], "after.txt"), "w").close()
""")
        audit = _audit(events, good, script)
        assert ("open", "after.txt") in audit.sites["raw"]
        assert any("open 'x.txt'" in f and "could not be placed" in f for f in audit.faults)

    def test_an_error_inside_the_hook_does_not_reach_the_program(self, tmp_path):
        """An event the hook cannot take apart (here: one with no arguments,
        raised by hand) leaves a `hook-error` line and nothing else."""
        events, good, script = _hooked(tmp_path, """
import os, sys
sys.audit("os.rename")
open(os.path.join(sys.argv[1], "after.txt"), "w").close()
""")
        errors = [e for e in events if e["ev"] == "hook-error"]
        assert len(errors) == 1 and errors[0]["event"] == "os.rename", events
        audit = _audit(events, good, script)
        assert ("open", "after.txt") in audit.sites["raw"]
        assert any("the hook failed" in f for f in audit.faults)

    def test_the_log_survives_the_program_closing_its_descriptors(self, tmp_path):
        """A sweep of the low descriptors must not take the log with it, and
        a log descriptor that WAS closed and whose number went to a file of
        the program must not be written to."""
        events, good, _script = _hooked(tmp_path, """
import os, sys
out = sys.argv[1]
os.closerange(3, 64)
open(os.path.join(out, "after-sweep.txt"), "w").close()
os.closerange(3, 4096)
mine = [open(os.path.join(out, f"mine-{n}.txt"), "w") for n in range(4)]
open(os.path.join(out, "after-all.txt"), "w").close()
[f.close() for f in mine]
""")
        opened = {os.path.basename(e["path"]) for e in events if e["ev"] == "open"}
        assert {"after-sweep.txt", "after-all.txt", "mine-3.txt"} <= opened, opened
        for n in range(4):
            assert (good / f"mine-{n}.txt").read_text(encoding="utf-8") == "", (
                "the hook wrote its log into a file of the program")

    def test_a_rename_is_logged_at_both_ends(self, tmp_path):
        events, good, script = _hooked(tmp_path, """
import os, sys
out = sys.argv[1]
src = os.path.join(out, "o.json")
open(src, "w").close()
os.rename(src, os.path.join(os.path.dirname(out), "moved.json"))
os.chdir(out)
open("o.json", "w").close()
os.rename("o.json", "../moved-2.json")
""")
        audit = _audit(events, good, script)
        assert audit.sites["raw"] == (("open", "o.json"), ("os.rename-from", "o.json"))
        assert not audit.faults

    def test_a_spawn_carries_its_whole_command_line(self, tmp_path):
        events, good, script = _hooked(tmp_path, """
import os, subprocess, sys
out = os.path.join(sys.argv[1], "o.txt")
subprocess.run([sys.executable, "-c", "pass", "a", "b", "c", "--to=" + out], check=True)
os.system("true " + out)
""")
        audit = _audit(events, good, script)
        assert audit.programs == ("python" + GIVEN_THE_OUTPUT_PATH,
                                  "sh" + GIVEN_THE_OUTPUT_PATH)

    def test_every_thread_is_logged(self, tmp_path):
        """One guard flag for the whole process dropped the events other
        threads raised while one was being written."""
        events, good, script = _hooked(tmp_path, """
import os, sys, threading
sys.setswitchinterval(1e-6)
def work(i):
    for j in range(100):
        with open(os.path.join(sys.argv[1], f"{i}-{j}.txt"), "w") as f:
            f.write("x")
threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
[t.start() for t in threads]
[t.join() for t in threads]
""")
        opened = {e["path"] for e in events
                  if e["ev"] == "open" and _under(_placed(e) or "", good) is not None}
        assert len(opened) == 800 == len(_files_under(good))

    def test_the_environments_own_sitecustomize_still_runs(self, tmp_path):
        """Python imports one `sitecustomize`. The hook's directory goes
        first on the path, so it runs the next one itself — with and without
        a log to write."""
        theirs = tmp_path / "their-site"
        theirs.mkdir()
        (theirs / "sitecustomize.py").write_text(
            "import os\nos.environ['THEIR_SITECUSTOMIZE'] = __file__\n", encoding="utf-8")
        code = "import os\nprint(os.environ.get('THEIR_SITECUSTOMIZE'))\n"
        for audited in (True, False):
            env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(AUDIT_DIR), str(theirs)])}
            env.pop("DA_WRITE_AUDIT_LOG", None)
            if audited:
                env["DA_WRITE_AUDIT_LOG"] = str(tmp_path / "log.jsonl")
            proc = subprocess.run([sys.executable, "-c", code], env=env, check=True,
                                  timeout=TIMEOUT_S, capture_output=True, text=True,
                                  encoding="utf-8")
            assert proc.stdout.strip() == str(theirs / "sitecustomize.py"), (audited, proc)
        assert (tmp_path / "log.jsonl").is_file()


class TestTheLists:
    """`_list_faults` on a row that is on no list, and on one that is on each."""
    _CLEAN = Seen([], (), False, None)

    def test_a_clean_row_on_no_list_has_no_fault(self):
        assert _list_faults(_audit_row(), self._CLEAN) == []

    def test_an_unlisted_write_or_program_is_a_fault(self):
        row = _audit_row()
        assert len(_list_faults(row, Seen([], ("open x",), True, None))) == 1
        assert len(_list_faults(row, Seen([], (), False, "artifact"))) == 1
        assert len(_list_faults(row, Seen([], (), False, None, ("cp",)))) == 1

    def test_a_program_that_is_not_listed_is_a_fault_and_one_not_started_is_not(self):
        row = next(r for r in ROWS if r.id == "federation_keygen[--jwks-out]")
        listed = SPAWNS[(row.tool, row.flag)]
        assert listed == ("openssl",)
        assert _list_faults(row, Seen([], (), False, None, listed)) == []
        assert _list_faults(row, Seen([], (), False, None, (*listed, "sh")))
        assert _list_faults(row, Seen([], (), False, None,
                                      ("openssl" + GIVEN_THE_OUTPUT_PATH,)))
        # One direction only — see SPAWNS for why.
        assert _list_faults(row, Seen([], (), False, None, ())) == []

    def test_a_listed_row_that_is_clean_now_must_come_off(self, monkeypatch):
        """Each list, on its own first entry: clean ⇒ red. A list the
        migration has emptied (#3982: VIA_OTHER_CLASS, RAW_UNDER_OUT) still has
        its judgement while the list exists: it is held on a synthetic entry."""
        # Two keys, not one: on a shared key either list's fault would answer
        # for the other.
        if not VIA_OTHER_CLASS:  # emptied by #3982 (owner 2026-11-18)
            monkeypatch.setitem(VIA_OTHER_CLASS, ("dx/zz_other_class.py", "--out"), "artifact")
        if not RAW_UNDER_OUT:  # emptied by #3982 (the last key, config_history)
            monkeypatch.setitem(RAW_UNDER_OUT, ("dx/zz_raw.py", "--out"),
                                ("all", ("open zz.json",)))
        for key in (sorted(RAW_UNDER_OUT)[0], sorted(VIA_OTHER_CLASS)[0]):
            row = Row(key[0], key[1], "file", "operator", lambda c, out: [], reach="synthetic",
                      variant="synthetic")
            assert _list_faults(row, self._CLEAN), key

    def test_a_raw_site_more_or_fewer_than_listed_is_a_fault(self, monkeypatch):
        # A synthetic entry: #3982 emptied RAW_UNDER_OUT, and its judgement
        # stays until the list itself is retired.
        key = ("dx/zz_synthetic.py", "--out-dir")
        monkeypatch.setitem(RAW_UNDER_OUT, key, ("some", ("open zz/a.json", "os.mkdir zz")))
        row = Row(key[0], key[1], "dir", "operator", lambda c, out: [], reach="synthetic")
        how, sites = RAW_UNDER_OUT[key]
        started = SPAWNS.get(key, ())
        assert _list_faults(row, Seen([], sites, how == "all", None, started)) == []
        # One site more, one fewer — with the listed all/some, so only the
        # sites differ; the next line differs in all/some alone.
        assert _list_faults(row, Seen([], (*sites, "open out/zz.json"), how == "all", None,
                                      started))
        assert _list_faults(row, Seen([], sites[1:], how == "all", None, started))
        assert _list_faults(row, Seen([], sites, how != "all", None, started))

    def test_every_list_entry_is_a_row(self):
        rowed = {(r.tool, r.flag) for r in ROWS}
        for name, listed in (("RAW_UNDER_OUT", RAW_UNDER_OUT),
                             ("VIA_OTHER_CLASS", VIA_OTHER_CLASS)):
            assert not sorted(set(listed) - rowed), (
                f"{name} names (tool, flag) no row runs: {sorted(set(listed) - rowed)}")
        assert not sorted(set(SPAWNS) - rowed), "SPAWNS names a (tool, flag) no row runs"
        assert all(SPAWNS.values()), "an empty SPAWNS entry says nothing: drop it"
        assert not sorted({tool for tool, _f, _n in UNREACHED_WRITER_CALLS}
                          - {r.tool for r in ROWS})

    def test_the_mode_of_a_clean_operator_rows_files_is_held(self, tmp_path, monkeypatch):
        """What an `fchmod` after the entry, or a `chmod` in a child, leaves
        behind: the audit names who opened the file, the disk says its mode."""
        if not has_posix_modes():
            pytest.skip("no POSIX modes here")
        clean = next(r for r in ROWS if r.id == "generate_alertmanager_routes[--findings-json]")
        assert _is_clean(clean) and clean.mode == 0o600
        out = tmp_path / "findings.json"
        out.write_text("{}", encoding="utf-8")
        out.chmod(0o600)
        assert _mode_faults(clean, tmp_path, frozenset({"findings.json"})) == []
        out.chmod(0o666)
        assert len(_mode_faults(clean, tmp_path, frozenset({"findings.json"}))) == 1
        # Not asked of a row on a list, nor of an artifact.
        listed = Row("dx/zz_synthetic.py", "--out", "file", "operator", lambda c, out: [],
                     reach="synthetic")
        monkeypatch.setitem(RAW_UNDER_OUT, (listed.tool, listed.flag), ("all", ("open zz",)))
        artifact = next(r for r in ROWS if r.cls == "artifact" and _is_clean(r))
        for row in (listed, artifact):
            assert _mode_faults(row, tmp_path, frozenset({"findings.json"})) == []


class TestOwnWordedRefusal:
    """`wording="own"` is not a looser match: one line, the flag, and the
    blocked path ending where it ends."""

    def _case(self, tmp_path):
        row = _control_row(wording="own", shapes=("target_is_file",))
        return row, _bad_path(tmp_path, "target_is_file", row)

    def test_one_line_naming_the_flag_and_the_blocked_path_passes(self, tmp_path):
        row, out = self._case(tmp_path)
        line = f"ERROR: --output-dir {out} is not a directory; nothing was written\n"
        assert _own_refusal_problem(row, "target_is_file", out, "note\n" + line) is None

    def test_a_path_under_the_blocked_one_does_not_pass_for_it(self, tmp_path):
        row, out = self._case(tmp_path)
        line = f"ERROR: --output-dir {out}/da-a.yaml: Not a directory; nothing was written\n"
        assert "blocked path" in _own_refusal_problem(row, "target_is_file", out, line)
        line = f"ERROR: --output-dir {out}-2 is not a directory\n"
        assert "blocked path" in _own_refusal_problem(row, "target_is_file", out, line)

    def test_the_flag_and_the_line_count_are_held(self, tmp_path):
        row, out = self._case(tmp_path)
        assert "does not name --output-dir" in _own_refusal_problem(
            row, "target_is_file", out, f"ERROR: {out} is not a directory\n")
        line = f"ERROR: --output-dir {out} is not a directory\n"
        assert "exactly one" in _own_refusal_problem(row, "target_is_file", out, line * 2)
        assert "exactly one" in _own_refusal_problem(row, "target_is_file", out, "")

    def test_a_longer_path_that_ends_in_the_blocked_one_does_not_pass_for_it(self, tmp_path):
        row, out = self._case(tmp_path)
        line = f"ERROR: --output-dir /elsewhere{out} is not a directory\n"
        assert "blocked path" in _own_refusal_problem(row, "target_is_file", out, line)
        line = f"ERROR: --output-dir x{out} is not a directory\n"
        assert "blocked path" in _own_refusal_problem(row, "target_is_file", out, line)
        quoted = f"ERROR: --output-dir '{out}' is not a directory\n"
        assert _own_refusal_problem(row, "target_is_file", out, quoted) is None

    def test_the_shared_sentence_is_not_an_own_wording(self, tmp_path):
        row, out = self._case(tmp_path)
        line = (f"ERROR: cannot create directory {out}: File exists (errno 17) — "
                "check the value given to --output-dir\n")
        assert "shared sentence" in _own_refusal_problem(row, "target_is_file", out, line)


# ═══════════════════════════════════════════════════════════════════════════
# What the audit never ran (#3107)
#
# The audit holds the writes a control run MAKES. A writer call the control
# argv does not reach — another mode's summary file, an error-path report —
# is not on any log, and swapping it for a bare `Path.write_text` changes
# nothing above. So the calls are counted from the source and matched against
# the lines the control runs' stacks passed through; one that no run reached
# is listed, by name, until a row reaches it.
# ═══════════════════════════════════════════════════════════════════════════

#: (tool, function, writer name) → how many calls of that writer in that
#: function no control run of the tool reached under its output path. Exact
#: both ways. Not counted: the two names that only name the flag
#: (`output_write`, `check_repo_artifact` — they open nothing, so no event
#: could carry them), and calls in the libraries a tool imports. What a
#: count cannot tell apart is in the module docstring, under NOT SEEN.
UNREACHED_WRITER_CALLS: dict[tuple[str, str, str], int] = {
    ("ops/migrate_rule.py", "main", "ensure_dir_or_die"): 1,
    ("ops/onboard_platform.py", "_write_draft", "ensure_dir_or_die"): 1,
    ("ops/onboard_platform.py", "_write_phase2_outputs", "ensure_dir_or_die"): 1,
    ("ops/onboard_platform.py", "_write_phase3_outputs", "ensure_dir_or_die"): 1,
    ("ops/onboard_platform.py", "_write_phase3_outputs", "write_operator_output_or_die"): 2,
    ("ops/onboard_platform.py", "write_migration_csv", "write_operator_output_or_die"): 1,
    ("ops/onboard_platform.py", "write_outputs", "write_onboard_hints"): 1,
    ("ops/operator_generate.py", "write_crds", "write_yaml_crd"): 1,
    ("ops/validate_migration.py", "main", "ensure_dir_or_die"): 1,
    ("ops/validate_migration.py", "main", "write_operator_output_or_die"): 1,
}


def _writer_calls(tool: str) -> list[tuple[str, str, int, int]]:
    """(enclosing function, writer name, first line, last line) of every call
    of a writing name in *tool*'s own module."""
    import _lib_io
    silent = {name.split(".")[1] for name in _lib_io.OUTPUT_ENTRY_POINTS["names_flag_only"]}
    names = _writer_names() - silent
    module = ast.parse((TOOLS_DIR / tool).read_text(encoding="utf-8"))
    parents = {child: node for node in ast.walk(module)
               for child in ast.iter_child_nodes(node)}
    calls = []
    for node in ast.walk(module):
        if isinstance(node, ast.Call) and _called_name(node) in names:
            scope = parents.get(node)
            while scope is not None and not isinstance(
                    scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
                scope = parents.get(scope)
            calls.append((getattr(scope, "name", "<module>"), _called_name(node),
                          node.lineno, node.end_lineno))
    return calls


def unreached_writer_calls(reached: dict[str, set[tuple[str, int]]]
                           ) -> dict[tuple[str, str, str], int]:
    """The writer calls of every rowed tool that no line in *reached* (tool →
    the (file, line) frames of its control runs' events) falls inside."""
    out: dict[tuple[str, str, str], int] = {}
    for tool in sorted({row.tool for row in ROWS}):
        own = os.path.normpath(f"scripts/tools/{tool}").replace(os.sep, "/")
        lines = {line for file, line in reached.get(tool, ()) if file == own}
        for function, name, first, last in _writer_calls(tool):
            if not any(first <= line <= last for line in lines):
                out[(tool, function, name)] = out.get((tool, function, name), 0) + 1
    return out


def test_every_writer_call_of_a_rowed_tool_is_reached_or_listed(tmp_path, stub_url):
    reached: dict[str, set[tuple[str, int]]] = {}
    for n, row in enumerate(ROWS):
        if row.requires and shutil.which(row.requires) is None:
            pytest.skip(f"{row.requires} is not on PATH: {row.id} cannot run, so what "
                        "its control run reaches is unknown")
        tmp = tmp_path / f"row-{n}"
        tmp.mkdir()
        _proc, _out, audit, _made = _control_run(row, tmp, stub_url)
        reached.setdefault(row.tool, set()).update(audit.reached)
    found = unreached_writer_calls(reached)
    assert found == UNREACHED_WRITER_CALLS, (
        "writer calls no control run reaches — a new one needs a row that runs "
        "it (or its entry here); one that is reached now comes off:\\n  not listed: "
        f"{sorted(set(found.items()) - set(UNREACHED_WRITER_CALLS.items()))}\\n  "
        f"listed, not found: "
        f"{sorted(set(UNREACHED_WRITER_CALLS.items()) - set(found.items()))}")


def test_unreached_writer_calls_are_counted_by_line():
    """On hand-made input: a call is reached when a frame's line is inside
    it, per call — a function with one of its two calls reached still owes
    the other."""
    tool = "ops/federation_keygen.py"
    (function, name, first, last), = [c for c in _writer_calls(tool)]
    own = f"scripts/tools/{tool}"
    assert unreached_writer_calls({}).get((tool, function, name)) == 1
    assert (tool, function, name) not in unreached_writer_calls({tool: {(own, last)}})
    assert (tool, function, name) in unreached_writer_calls({tool: {(own, last + 1)}})
    assert (tool, function, name) in unreached_writer_calls(
        {tool: {("scripts/tools/_lib_io.py", first)}})


#: A writer name used as a VALUE rather than called: aliased on import,
#: handed to another function, wrapped in `functools.partial`, looked up with
#: `getattr`. The scans above read calls by name, so a write made through
#: such a value is invisible to them. Each one is listed with what it is —
#: (file, name) → (how many such uses, why they are there). Exact both ways,
#: the count included: a second use beside a listed one is a new use.
WRITER_NAMES_USED_AS_VALUES: dict[tuple[str, str], tuple[int, str]] = {
    ("lint/check_open_encoding.py", "atomic_write_text"): (
        1, "a string in LF_PINNING_WRAPPERS: the lint's own list of writers that "
           "pin LF, a name it looks for in other files and never calls."),
    ("ops/_registry_lib.py", "write_repo_artifact"): (
        6, "handed to `_write_or_stop` as its `write` argument for the generated "
        "surfaces of the threshold registry (fixed repo paths, no flag). The "
        "write is `write_repo_artifact`'s own frame on a stack, so the audit "
        "attributes it when a row runs it; none does today."),
    ("ops/generate_rule_pack_split.py", "ensure_dir_or_die"): (
        1, "a truth test (`if ensure_dir_or_die:`) choosing between it and the "
        "bare `mkdir` of the import fallback, not a write."),
}


def _writer_names_used_as_values() -> dict[tuple[str, str], int]:
    """(module, writer name) → the number of places it is used other than as
    the function of a call or as a plain import."""
    writers = _writer_names()
    found: dict[tuple[str, str], int] = {}
    for tool, _py, tree in _cli_modules():
        called = {id(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
        for node in ast.walk(tree):
            name = None
            if isinstance(node, (ast.ImportFrom, ast.Import)):
                for alias in node.names:
                    if alias.name.rpartition(".")[2] in writers and alias.asname not in (
                            None, alias.name):
                        key = (tool, alias.name.rpartition(".")[2])
                        found[key] = found.get(key, 0) + 1
            elif isinstance(node, ast.Name) and id(node) not in called:
                name = node.id
            elif isinstance(node, ast.Attribute) and id(node) not in called:
                name = node.attr
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                name = node.value  # getattr(_lib_io, "write_operator_output"), a dispatch table
            if name in writers and not (isinstance(node, ast.Name)
                                        and isinstance(node.ctx, ast.Store)):
                found[(tool, name)] = found.get((tool, name), 0) + 1
    return found


def test_a_writer_name_is_only_ever_called_or_listed():
    found = _writer_names_used_as_values()
    listed = {key: count for key, (count, _why) in WRITER_NAMES_USED_AS_VALUES.items()}
    assert found == listed, (
        "a writer name used as a value (aliased, passed, partial, getattr) — the "
        "by-name scans cannot follow it; call it by name, or list it, with how "
        "many times:\\n  found, not as listed: "
        f"{sorted(set(found.items()) - set(listed.items()))}\\n  listed, not as found: "
        f"{sorted(set(listed.items()) - set(found.items()))}")
    assert all(why.strip() for _count, why in WRITER_NAMES_USED_AS_VALUES.values())
