"""scripts/ops/go_test_reads.py — exit-code contract on synthetic trees (#1399).

Each case builds a throwaway git repo with a small workflow and hand-written
test logs, then runs the script as the Go legs do: as a subprocess, from the
repo root. The real ci.yml wiring is asserted by the path-filter modules; this
file pins what the script decides from a log.

The script resolves POSIX paths only and exits 2 on any other repo root. Where
tmp_path is such a root (Windows: `C:/...`), the cases that need a log to be
read past that check skip; the cases that exit 2 earlier still run.
"""
from __future__ import annotations

import importlib.util
import posixpath
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


def _posix_root(tmp_path: Path) -> bool:
    return posixpath.isabs(tmp_path.resolve().as_posix())


def _needs_posix_root(tmp_path: Path) -> None:
    if not _posix_root(tmp_path):
        pytest.skip(f"tmp_path {tmp_path.resolve().as_posix()} is not a POSIX absolute "
                    "path: go_test_reads.py exits 2 on such a root before it reads "
                    "the log, so this case cannot reach what it asserts")


@pytest.fixture()
def tool(monkeypatch):
    # The script imports its sibling paths_filter_glob by bare name.
    monkeypatch.syspath_prepend(str(SCRIPT.parent))
    spec = importlib.util.spec_from_file_location("go_test_reads_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(repo: Path, logs: Path, job: str = "zleg") -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--job", job, "--logs", str(logs),
         "--workflow", str(repo / "wf.yml"), "--root", str(repo)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)


def test_covered_direct_reads_pass_and_probes_and_walks_do_not_count(tmp_path: Path) -> None:
    _needs_posix_root(tmp_path)
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
    _needs_posix_root(tmp_path)
    repo = _repo(tmp_path)
    logs = tmp_path / "logs"
    _log(logs, "a", repo / "zmod", [ln.format(repo=repo.as_posix()) for ln in lines])
    r = _run(repo, logs)
    assert r.returncode == 1, (r.stdout, r.stderr)
    named = lines[-1].split("/")[-1].replace("open ", "")
    assert named in r.stderr and "zfix/kept.json" not in r.stderr


def test_a_ledgered_root_walker_is_exempt_and_nothing_else_is(tmp_path: Path) -> None:
    _needs_posix_root(tmp_path)
    repo = _repo(tmp_path)
    # Zmakefile sits in the root and is uncovered: exempt, the listing hides it
    # (rc 0, counted as walked); anywhere else the root listing is refused.
    lines = [f"open {repo.as_posix()}", f"open {repo.as_posix()}/Zmakefile"]
    for pkg, want in ((LEDGERED, 0), ("zmod", 2)):
        logs = tmp_path / f"logs-{want}"
        _log(logs, "a", repo / pkg, lines)
        r = _run(repo, logs)
        assert r.returncode == want, (pkg, r.stdout, r.stderr)
        if want == 0:
            assert "0 tracked file(s) opened directly, 1 reached by listing" in r.stdout


@pytest.mark.parametrize("case", [
    "no-logs", "no-header", "no-cwd", "cwd-outside-root", "empty-repo",
    "unmodelled-gate", "no-job", "bang-pattern", "two-detects", "two-filter-steps"])
def test_what_cannot_be_measured_exits_2_not_0(tmp_path: Path, case: str) -> None:
    if case == "cwd-outside-root":
        # The only case here that fails AFTER the root check; the others exit 2
        # for their own reason first, so they still prove something on any root.
        _needs_posix_root(tmp_path)
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


def test_a_non_posix_root_is_unmeasurable_not_zero_reads(tmp_path: Path, tool) -> None:
    """Runs on every platform: the root is a string, no such directory is needed.

    posixpath does not see `C:/...` as absolute, so the absolute open below
    would be joined onto cwd, miss the tracked file and count as zero reads.
    """
    logs = tmp_path / "logs"
    logs.mkdir()
    root = "C:/Users/x/repo"
    (logs / "a.log").write_text(
        f"# test log\nopen {root}/zfix/other.json\n", encoding="utf-8")
    (logs / "a.log.cwd").write_text(f"{root}/zmod\n", encoding="utf-8")
    with pytest.raises(tool.Unmeasurable, match="not a POSIX absolute path") as exc:
        tool.direct_reads(logs / "a.log", root, frozenset({"zfix/other.json"}))
    assert root in str(exc.value)


def test_a_posix_root_is_still_measured(tmp_path: Path, tool) -> None:
    """The same log under a `/` root: the check above must not fire."""
    logs = tmp_path / "logs"
    logs.mkdir()
    root = "/home/x/repo"
    (logs / "a.log").write_text(
        f"# test log\nopen {root}/zfix/other.json\n", encoding="utf-8")
    (logs / "a.log.cwd").write_text(f"{root}/zmod\n", encoding="utf-8")
    assert tool.direct_reads(logs / "a.log", root, frozenset({"zfix/other.json"})) == (
        {"zfix/other.json"}, 0)


def test_the_cli_exits_2_on_a_non_posix_root(tmp_path: Path) -> None:
    if _posix_root(tmp_path):
        pytest.skip("tmp_path is a POSIX absolute path; --root is resolved against "
                    "the filesystem, so a non-POSIX root cannot be passed here")
    repo = _repo(tmp_path)
    logs = tmp_path / "logs"
    _log(logs, "a", repo / "zmod", [f"open {repo.as_posix()}/zfix/other.json"])
    r = _run(repo, logs)
    assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)
    assert "cannot measure" in r.stderr and "not a POSIX absolute path" in r.stderr
    assert "opened directly" not in r.stdout
