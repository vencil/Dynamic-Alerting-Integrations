"""Contract for CI's sharded pytest legs (`--shard=K/N`, tests/_shard.py).

A shard split fails in exactly one dangerous direction: silently. If the
matrix lists fewer shards than the command divides by, or two jobs disagree on
which shard owns a test, some tests run nowhere and every job is still green.
These tests pin the three things that keep the N jobs covering the tree
exactly once:

1. the assignment is a pure function of the nodeid (not ``hash()``, which is
   salted per process),
2. the real conftest hook partitions a real collection — disjoint, and the
   union is the unsharded collection,
3. each sharded ci.yml job's matrix is exactly ``1..N`` for the ``N`` its
   pytest command divides by.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _shard import parse_shard_spec, shard_of  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
CI_YML = REPO_ROOT / ".github" / "workflows" / "ci.yml"

# Every ci.yml job that runs pytest with --shard. A job that starts passing
# --shard without being listed here is caught by
# test_every_sharded_job_is_pinned below.
SHARDED_JOBS = ("python-tests-run", "python-coverage")

_SHARD_ARG_RE = re.compile(r"--shard=\$\{\{\s*matrix\.shard\s*\}\}/(\d+)")

SAMPLE_IDS = [f"tests/pkg/test_m{i % 17}.py::test_case[{i}]" for i in range(600)]


@pytest.mark.parametrize("n", [1, 2, 3, 4, 7])
def test_every_nodeid_lands_in_exactly_one_shard(n):
    for nid in SAMPLE_IDS:
        assert 1 <= shard_of(nid, n) <= n
    counts = [sum(shard_of(nid, n) == k for nid in SAMPLE_IDS)
              for k in range(1, n + 1)]
    assert sum(counts) == len(SAMPLE_IDS)
    # Not a balance guarantee, only a check that the hash is not degenerate
    # (e.g. everything mapped to shard 1).
    assert min(counts) > 0


def test_assignment_does_not_depend_on_the_process_hash_seed():
    """Two processes with different PYTHONHASHSEED must agree.

    Regressing shard_of to ``hash()`` makes this fail: str hashes differ per
    seed, so xdist workers / CI jobs would each compute a different partition.
    """
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]);"
        "from _shard import shard_of;"
        "print(','.join(str(shard_of(f'x::t{i}', 3)) for i in range(200)))"
    )
    outs = set()
    for seed in ("1", "2", "12345"):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        proc = subprocess.run(
            [sys.executable, "-c", code, str(REPO_ROOT / "tests")],
            capture_output=True, text=True, env=env, timeout=60, check=True,
        )
        outs.add(proc.stdout.strip())
    assert len(outs) == 1


@pytest.mark.parametrize("spec", ["0/3", "4/3", "1/0", "3", "a/b", "-1/3", ""])
def test_malformed_spec_is_rejected(spec):
    with pytest.raises(ValueError):
        parse_shard_spec(spec)


def test_wellformed_spec_parses():
    assert parse_shard_spec("2/3") == (2, 3)
    assert parse_shard_spec(" 1/1 ") == (1, 1)


def _collect(*extra: str) -> set[str]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q",
         "-p", "no:cacheprovider", "-p", "no:xdist",
         str(Path(__file__).relative_to(REPO_ROOT)), *extra],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return {line for line in proc.stdout.splitlines() if "::" in line}


def test_the_conftest_hook_partitions_a_real_collection():
    """Through tests/conftest.py, not the helper: shards are disjoint and
    their union is the unsharded collection."""
    full = _collect()
    assert full, "collected nothing — the partition check below would be vacuous"
    parts = [_collect(f"--shard={k}/2") for k in (1, 2)]
    assert parts[0].isdisjoint(parts[1])
    assert parts[0] | parts[1] == full


def test_a_malformed_shard_option_is_a_usage_error():
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q",
         "-p", "no:cacheprovider", "-p", "no:xdist",
         str(Path(__file__).relative_to(REPO_ROOT)), "--shard=4/3"],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=300,
    )
    # pytest's UsageError exit code; a silent "collected 0" would be rc 5.
    assert proc.returncode == 4, proc.stdout + proc.stderr


def _ci_jobs() -> dict:
    return yaml.safe_load(CI_YML.read_text(encoding="utf-8"))["jobs"]


def _pytest_runs(job: dict) -> list[str]:
    return [s["run"] for s in job.get("steps", [])
            if isinstance(s.get("run"), str) and re.search(r"\bpytest\b", s["run"])]


def test_every_sharded_job_is_pinned():
    sharded = {jid for jid, job in _ci_jobs().items()
               if any("--shard" in r for r in _pytest_runs(job))}
    assert sharded == set(SHARDED_JOBS)


@pytest.mark.parametrize("job_id", SHARDED_JOBS)
def test_matrix_and_command_name_the_same_shard_count(job_id):
    job = _ci_jobs()[job_id]
    ns = {int(m) for r in _pytest_runs(job) for m in _SHARD_ARG_RE.findall(r)}
    assert len(ns) == 1, f"{job_id}: expected one --shard=${{{{ matrix.shard }}}}/N, got {ns}"
    (n,) = ns
    shards = job["strategy"]["matrix"]["shard"]
    assert shards == list(range(1, n + 1)), (
        f"{job_id}: matrix.shard is {shards} but the pytest command divides "
        f"by {n}; any shard missing from the matrix is a slice of the tree "
        "that runs nowhere while every job stays green"
    )
