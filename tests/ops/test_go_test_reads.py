"""scripts/ops/go_test_reads.py — exit-code contract on synthetic trees (#1399).

Each case builds a throwaway git repo with a small workflow and hand-written
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
            zbang:
              - "zmod/**"
              - "!zmod/zfix.json"
  other-detect:
    steps:
      - uses: dorny/paths-filter@v3
        with:
          filters: |
            zgo:
              - "**"
  twin-detect:
    steps:
      - uses: dorny/paths-filter@v3
        with:
          filters: |
            zgo:
              - "zmod/**"
      - uses: dorny/paths-filter@v3
        with:
          filters: |
            zgo:
              - "**"
  zleg:
    needs: detect-changes
    if: needs.detect-changes.outputs.zgo_changed == 'true' || needs.detect-changes.outputs.zdoc_changed == 'true'
  zand:
    needs: detect-changes
    if: needs.detect-changes.outputs.zgo_changed == 'true' && github.event_name == 'push'
  zbangleg:
    needs: detect-changes
    if: needs.detect-changes.outputs.zbang_changed == 'true'
  ztwo:
    if: needs.detect-changes.outputs.zgo_changed == 'true' || needs.other-detect.outputs.zgo_changed == 'true'
  ztwin:
    needs: twin-detect
    if: needs.twin-detect.outputs.zgo_changed == 'true'
"""
FILES = ["zmod/go.mod", "zmod/a_test.go", "zfix/kept.json", "zfix/other.json",
         "zdocs/x.md", "zwalk/one.yaml", "zwalk/deep/two.yaml", "Zmakefile"]
# ROOT_WALKERS names a real package; a synthetic log running there is exempt.
LEDGERED = "components/tenant-api/internal/gitops"


def _repo(tmp_path: Path, files: list[str] = FILES) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for f in files:
        (repo / f).parent.mkdir(parents=True, exist_ok=True)
        (repo / f).write_text("x\n", encoding="utf-8")
    (repo / "wf.yml").write_text(WORKFLOW, encoding="utf-8")
    for args in (["init", "-q"], ["add", "-A"]):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, timeout=60)
    if not files:  # `git add -A` staged wf.yml; the empty-repo case wants nothing tracked
        subprocess.run(["git", "rm", "-q", "--cached", "wf.yml"], cwd=repo, check=True,
                       capture_output=True, timeout=60)
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
        "open ../zwalk/one.yaml",          # ...so its direct children are walked
    ])
    r = _run(repo, logs)
    assert r.returncode == 0, r.stderr
    assert "2 tracked file(s) opened directly, 1 reached by listing" in r.stdout


@pytest.mark.parametrize("lines", [
    # plain
    ["open ../zfix/other.json"],
    # resolvable only through the chdir: ignoring chdir drops it as out-of-repo
    ["chdir {repo}/zfix", "open other.json"],
    # listing an ANCESTOR (not the parent) must not hide a direct read below it
    ["open ../zwalk", "open ../zwalk/deep/two.yaml"],
])
def test_an_uncovered_direct_read_fails_and_names_the_file(tmp_path: Path, lines) -> None:
    repo = _repo(tmp_path)
    logs = tmp_path / "logs"
    _log(logs, "a", repo / "zmod", [ln.format(repo=repo.as_posix()) for ln in lines])
    r = _run(repo, logs)
    assert r.returncode == 1, (r.stdout, r.stderr)
    named = lines[-1].split("/")[-1].replace("open ", "")
    assert named in r.stderr and "zfix/kept.json" not in r.stderr


def test_a_ledgered_root_walker_is_exempt_and_nothing_else_is(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    for pkg, want in ((LEDGERED, 0), ("zmod", 2)):
        logs = tmp_path / f"logs-{want}"
        _log(logs, "a", repo / pkg, [f"open {repo.as_posix()}", "open ../zfix/other.json"])
        assert _run(repo, logs).returncode == want, pkg


@pytest.mark.parametrize("case", [
    "no-logs", "no-header", "no-cwd", "cwd-outside-root", "empty-repo",
    "unmodelled-gate", "no-job", "bang-pattern", "two-detects", "two-filter-steps"])
def test_what_cannot_be_measured_exits_2_not_0(tmp_path: Path, case: str) -> None:
    repo = _repo(tmp_path, [] if case == "empty-repo" else FILES)
    logs = tmp_path / "logs"
    job = {"unmodelled-gate": "zand", "no-job": "zmissing", "bang-pattern": "zbangleg",
           "two-detects": "ztwo", "two-filter-steps": "ztwin"}.get(case, "zleg")
    if case == "no-logs":
        logs.mkdir()
    else:
        cwd = tmp_path / "elsewhere" if case == "cwd-outside-root" else repo / "zmod"
        _log(logs, "a", cwd, ["open ../zfix/kept.json"],
             header="# not a test log" if case == "no-header" else "# test log")
    if case == "no-cwd":
        (logs / "a.log.cwd").unlink()
    r = _run(repo, logs, job)
    assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)
