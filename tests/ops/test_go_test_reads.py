"""scripts/ops/go_test_reads.py — exit-code contract on synthetic trees (#1399).

Each case builds a throwaway git repo with a two-leg workflow and hand-written
test logs, then runs the script as the Go legs do: as a subprocess, from the
repo root. The real ci.yml wiring is asserted by the path-filter modules; this
file pins what the script decides from a log.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "ops" / "go_test_reads.py"

WORKFLOW = """\
jobs:
  detect-changes:
    steps:
      - uses: dorny/paths-filter@v3
        with:
          filters: |
            zgo:
              - "zmod/**"
              - "zfix/kept.json"
            zdoc:
              - "zdocs/**"
  zleg:
    needs: detect-changes
    if: needs.detect-changes.outputs.zgo_changed == 'true' || needs.detect-changes.outputs.zdoc_changed == 'true'
  zand:
    needs: detect-changes
    if: needs.detect-changes.outputs.zgo_changed == 'true' && github.event_name == 'push'
"""
FILES = ["zmod/go.mod", "zmod/a_test.go", "zfix/kept.json", "zfix/other.json",
         "zdocs/x.md", "zwalk/one.yaml", "zwalk/two.yaml", "Zmakefile"]


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    for f in FILES:
        (repo / f).parent.mkdir(parents=True, exist_ok=True)
        (repo / f).write_text("x\n", encoding="utf-8")
    wf = repo / "wf.yml"
    wf.write_text(WORKFLOW, encoding="utf-8")
    for args in (["init", "-q"], ["add", "-A"]):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, timeout=60)
    return repo


def _log(logs: Path, name: str, cwd: Path, lines: list[str], header: str = "# test log") -> None:
    logs.mkdir(exist_ok=True)
    (logs / f"{name}.log").write_text("\n".join([header, *lines]) + "\n", encoding="utf-8")
    (logs / f"{name}.log.cwd").write_text(cwd.as_posix() + "\n", encoding="utf-8")


def _run(repo: Path, logs: Path, job: str = "zleg") -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--job", job, "--logs", str(logs),
         "--workflow", str(repo / "wf.yml"), "--root", str(repo)],
        capture_output=True, text=True, timeout=60)


def test_covered_direct_reads_pass_and_probes_and_walks_do_not_count(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    logs = tmp_path / "logs"
    _log(logs, "a", repo / "zmod", [
        "open ../zfix/kept.json",          # direct, covered by zgo
        "open ../zdocs/x.md",              # direct, covered by the OTHER gate name
        "stat ../Zmakefile",               # a probe: never a dependency
        "open ../zwalk",                   # a directory listing...
        "open ../zwalk/one.yaml",          # ...so its children are walked
        "chdir " + (repo / "zfix").as_posix(),
        "open kept.json",                  # relative to the new cwd
    ])
    r = _run(repo, logs)
    assert r.returncode == 0, r.stderr
    assert "2 tracked file(s) opened directly, 1 reached by listing" in r.stdout


def test_an_uncovered_direct_read_fails_and_names_the_file(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    logs = tmp_path / "logs"
    _log(logs, "a", repo / "zmod", ["open ../zfix/kept.json", "open ../zfix/other.json"])
    r = _run(repo, logs)
    assert r.returncode == 1
    assert "zfix/other.json" in r.stderr and "zfix/kept.json" not in r.stderr


@pytest.mark.parametrize("case", ["no-logs", "no-header", "no-cwd", "unmodelled-gate", "no-job"])
def test_what_cannot_be_measured_exits_2_not_0(tmp_path: Path, case: str) -> None:
    repo = _repo(tmp_path)
    logs = tmp_path / "logs"
    job = "zleg"
    if case == "no-logs":
        logs.mkdir()
    else:
        _log(logs, "a", repo / "zmod", ["open ../zfix/kept.json"],
             header="# not a test log" if case == "no-header" else "# test log")
    if case == "no-cwd":
        (logs / "a.log.cwd").unlink()
    if case == "unmodelled-gate":
        job = "zand"
    if case == "no-job":
        job = "zmissing"
    r = _run(repo, logs, job)
    assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)
