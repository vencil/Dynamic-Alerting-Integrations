"""Unit tests for scripts/ops/plan_race_shards.py (nightly race sharding, #3541).

What each group guards:
  * assign      — greedy longest-first actually separates the heavy packages;
                  every fallback (no timings, unknown packages, tiny module)
                  still yields only NON-EMPTY shards that cover every package
                  exactly once. A dropped package is a silently unchecked one.
  * go list     — a failing or garbled `go list` degrades to ONE leg running the
                  module's own patterns (the pre-#3541 run), never to fewer
                  packages; packages without test files are kept, because
                  `go test` still builds them.
  * timings     — package-level Elapsed, and a timed-out package recorded at
                  least at the timeout so it stays "heavy" tomorrow.
  * fetch       — fail-open: every GitHub failure is a warning and a
                  round-robin plan, never an exception that kills the night.
  * merge       — fail-closed: a shard without both its stream and its rc file
                  makes the module incomplete; a merged stream parses into
                  exactly the fragment the unsharded stream did.
"""
from __future__ import annotations

import io
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "ops"))

import plan_race_shards as prs  # noqa: E402
from parse_go_test_json import build_fragment  # noqa: E402

P = [f"m/p{i}" for i in range(8)]


def _cover(groups: list[list[str]], pkgs: list[str]) -> None:
    flat = [p for g in groups for p in g]
    assert sorted(flat) == sorted(pkgs), f"packages lost or duplicated: {groups}"
    assert all(groups), f"an empty shard was emitted: {groups}"


# ── assign ───────────────────────────────────────────────────────────────────


def test_greedy_puts_each_heavy_package_on_its_own_shard() -> None:
    timings = {"m/p0": 1, "m/p1": 1700, "m/p2": 2, "m/p3": 1500, "m/p4": 1300,
               "m/p5": 200, "m/p6": 100, "m/p7": 3}
    groups, method = prs.assign(P, timings, shards=4)
    _cover(groups, P)
    assert method == "lpt"
    heavy = {"m/p1", "m/p3", "m/p4"}
    for g in groups:
        assert len(heavy & set(g)) <= 1, f"two heavy packages share a shard: {groups}"
    # Inside a shard, `go list` order is kept (stable `go test` argument order).
    for g in groups:
        assert g == sorted(g, key=P.index)
    loads = sorted(sum(timings[p] for p in g) for g in groups)
    assert loads == [306, 1300, 1500, 1700]


def test_no_timings_at_all_is_round_robin_in_go_list_order() -> None:
    groups, method = prs.assign(P, None, shards=4)
    assert method == "round-robin"
    assert groups == [["m/p0", "m/p4"], ["m/p1", "m/p5"], ["m/p2", "m/p6"], ["m/p3", "m/p7"]]


def test_timings_for_none_of_the_current_packages_is_round_robin() -> None:
    groups, method = prs.assign(P, {"other/pkg": 999.0}, shards=4)
    assert method == "round-robin"
    _cover(groups, P)


def test_new_package_without_timing_weighs_the_median_of_known() -> None:
    # Known: 10, 20, 30 -> median 20. The new package m/new (20) must be placed
    # like a 20 s package: on the shard that is lightest at that point.
    pkgs = ["m/a", "m/b", "m/c", "m/new"]
    timings = {"m/a": 10.0, "m/b": 20.0, "m/c": 30.0}
    groups, method = prs.assign(pkgs, timings, shards=2)
    assert method == "lpt"
    _cover(groups, pkgs)
    # LPT order c(30), b(20), new(20; ties go to `go list` order), a(10):
    # c->s0 (30), b->s1 (20), new->s1 (20 < 30, now 40), a->s0 (30 < 40).
    assert sorted(map(sorted, groups)) == [["m/a", "m/c"], ["m/b", "m/new"]]


def test_fewer_packages_than_shards_never_emits_an_empty_shard() -> None:
    groups, method = prs.assign(["m/only", "m/two"], {"m/only": 5.0}, shards=4)
    assert len(groups) == 2
    _cover(groups, ["m/only", "m/two"])
    assert prs.assign([], {"x": 1.0}, shards=4) == ([], "empty")
    groups, _ = prs.assign(["m/one"], None, shards=4)
    assert groups == [["m/one"]]


def test_zero_weight_packages_spread_instead_of_piling_onto_shard_one() -> None:
    pkgs = ["m/heavy", "m/z1", "m/z2", "m/z3"]
    groups, _ = prs.assign(pkgs, {"m/heavy": 100.0}, shards=4,
                           zero_weight=frozenset({"m/z1", "m/z2", "m/z3"}))
    assert len(groups) == 4, f"zero-weight packages collapsed into fewer shards: {groups}"
    _cover(groups, pkgs)


def test_shards_below_one_is_refused() -> None:
    with pytest.raises(ValueError):
        prs.assign(P, None, shards=0)


# ── go list + plan ───────────────────────────────────────────────────────────


def _go_list_runner(listing: dict[str, str], rc: int = 0):
    def run(args, cwd=None):
        out = listing.get(Path(cwd).name if cwd else "", "")
        return subprocess.CompletedProcess(args, rc, out.encode(), b"boom")
    return run


_LISTING = {
    "tenant-api": "t/a\t3\t0\nt/b\t0\t1\nt/helper\t0\t0\nt/c\t2\t0\nt/d\t1\t0\n",
    "app": "e/x\t1\t0\ne/y\t1\t0\n",
}


def test_plan_covers_every_module_in_both_matrices(tmp_path: Path) -> None:
    out = prs.plan(None, shards=4, runner=_go_list_runner(_LISTING), root=tmp_path)
    race_modules = {leg["module"] for leg in out["race"]}
    merge_modules = [m["module"] for m in out["merge"]]
    assert merge_modules == prs.module_names()
    assert race_modules == set(prs.module_names())
    for m in out["merge"]:
        legs = [leg for leg in out["race"] if leg["module"] == m["module"]]
        assert [leg["shard"] for leg in legs] == m["shards"].split(","), (
            "merge must expect exactly the shards the race matrix runs"
        )
        assert all(leg["packages"].strip() for leg in legs), "a leg with no packages"
        assert all(leg["shards"] == len(legs) for leg in legs)
        spec = next(s for s in prs.MODULES if s["module"] == m["module"])
        assert m["packages"] == spec["packages"] and m["workdir"] == spec["workdir"]
    ta = sorted(p for leg in out["race"] if leg["module"] == "tenant-api"
                for p in leg["packages"].split())
    # t/helper has no test files and is still run: go test BUILDS it.
    assert ta == ["t/a", "t/b", "t/c", "t/d", "t/helper"]
    te = [leg for leg in out["race"] if leg["module"] == "threshold-exporter"]
    assert len(te) == 2, "2 packages must give 2 shards, not 4 with 2 empty"


def test_go_list_failure_falls_back_to_one_leg_with_the_module_patterns(
        tmp_path: Path, capsys) -> None:
    out = prs.plan(None, shards=4, runner=_go_list_runner({}, rc=1), root=tmp_path)
    for spec in prs.MODULES:
        legs = [leg for leg in out["race"] if leg["module"] == spec["module"]]
        assert len(legs) == 1 and legs[0]["packages"] == spec["packages"]
        assert next(m for m in out["merge"] if m["module"] == spec["module"])["shards"] == "1"
    assert "::warning" in capsys.readouterr().err


def test_go_list_that_matches_nothing_or_is_garbled_falls_back(tmp_path: Path) -> None:
    for listing in ({}, {"tenant-api": "not\ta-valid-line\n", "app": "\n"}):
        out = prs.plan(None, shards=4, runner=_go_list_runner(listing), root=tmp_path)
        assert all(m["method"] == "single-leg-fallback" for m in out["merge"])


def test_plan_uses_timings_from_the_timings_dir(tmp_path: Path) -> None:
    tdir = tmp_path / "timings"
    tdir.mkdir()
    (tdir / "race-timings-tenant-api.json").write_text(json.dumps(
        {"t/a": 1700, "t/b": 1500, "t/c": 1, "t/d": 1}), encoding="utf-8")
    (tdir / "race-timings-threshold-exporter.json").write_text("{not json", encoding="utf-8")
    out = prs.plan(tdir, shards=4, runner=_go_list_runner(_LISTING), root=tmp_path)
    methods = {m["module"]: m["method"] for m in out["merge"]}
    assert methods == {"tenant-api": "lpt", "threshold-exporter": "round-robin"}
    legs = [leg["packages"].split() for leg in out["race"] if leg["module"] == "tenant-api"]
    assert not any({"t/a", "t/b"} <= set(g) for g in legs)


def test_modules_cli_prints_the_ssot(capsys) -> None:
    assert prs.main(["modules"]) == 0
    assert capsys.readouterr().out.strip() == ",".join(prs.module_names())


# ── timings ──────────────────────────────────────────────────────────────────


def _ev(**kw) -> str:
    return json.dumps(kw)


def test_stream_timings_reads_package_level_events_and_timeouts() -> None:
    lines = [
        _ev(Action="start", Package="m/a"),
        _ev(Action="pass", Package="m/a", Test="TestX", Elapsed=900.0),  # test-level: ignored
        _ev(Action="pass", Package="m/a", Elapsed=12.5),
        _ev(Action="skip", Package="m/none", Elapsed=0),
        _ev(Action="output", Package="m/slow", Test="TestS",
            Output="panic: test timed out after 30m0s\n"),
        _ev(Action="fail", Package="m/slow", Elapsed=1795.2),
        _ev(Action="fail", Package="m/slow2"),  # no Elapsed at all
        _ev(Action="output", Package="m/slow2", Output="panic: test timed out after 1h0m0s\n"),
        "not json",
        _ev(Action="output", Package="m/liar", Output="  panic: test timed out after 30m0s\n"),
        _ev(Action="pass", Package="m/liar", Elapsed=2.0),
    ]
    assert prs.stream_timings(lines) == {
        "m/a": 12.5, "m/liar": 2.0, "m/slow": 1800.0, "m/slow2": 3600.0,
    }


def test_go_duration_parser() -> None:
    assert prs.go_duration_seconds("30m0s") == 1800.0
    assert prs.go_duration_seconds("1h2m3.5s") == 3723.5
    assert prs.go_duration_seconds("500ms") == 0.5
    assert prs.go_duration_seconds("abc") is None
    assert prs.go_duration_seconds("") is None


def test_timeout_constant_matches_the_workflow() -> None:
    text = (ROOT / ".github" / "workflows" / "nightly-race.yaml").read_text(encoding="utf-8")
    assert f"-timeout={prs.GO_TEST_TIMEOUT_S // 60}m" in text, (
        "GO_TEST_TIMEOUT_S drifted from the workflow's go test -timeout"
    )


# ── fetch ────────────────────────────────────────────────────────────────────


def _zip(name: str, payload: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(name, payload)
    return buf.getvalue()


def _gh(routes: dict[str, tuple[int, bytes]]):
    calls: list[str] = []

    def run(args, cwd=None):
        path = args[2]
        calls.append(path)
        for prefix, (rc, out) in routes.items():
            if path.startswith(prefix):
                return subprocess.CompletedProcess(args, rc, out, b"err")
        return subprocess.CompletedProcess(args, 1, b"", b"404")
    run.calls = calls
    return run


def test_fetch_takes_the_newest_unexpired_artifact_per_module(tmp_path: Path) -> None:
    runs = {"workflow_runs": [{"id": 3}, {"id": 2}, {"id": 1}]}
    gh = _gh({
        "repos/o/r/actions/workflows/nightly-race.yaml/runs": (0, json.dumps(runs).encode()),
        # newest run: tenant-api expired, exporter present
        "repos/o/r/actions/runs/3/artifacts": (0, json.dumps({"artifacts": [
            {"id": 30, "name": "race-timings-tenant-api", "expired": True},
            {"id": 31, "name": "race-timings-threshold-exporter", "expired": False},
            {"id": 32, "name": "race-raw-tenant-api-1", "expired": False},
        ]}).encode()),
        "repos/o/r/actions/runs/2/artifacts": (0, json.dumps({"artifacts": [
            {"id": 20, "name": "race-timings-tenant-api", "expired": False},
            {"id": 21, "name": "race-timings-threshold-exporter", "expired": False},
        ]}).encode()),
        "repos/o/r/actions/artifacts/31/zip": (0, _zip("race-timings-threshold-exporter.json",
                                                       b'{"e/x": 3.5}')),
        "repos/o/r/actions/artifacts/20/zip": (0, _zip("race-timings-tenant-api.json",
                                                       b'{"t/a": 7}')),
    })
    found = prs.fetch_timings("o/r", "nightly-race.yaml", "main", tmp_path,
                              prs.module_names(), runner=gh)
    assert found == {"threshold-exporter": "3", "tenant-api": "2"}
    assert json.loads((tmp_path / "race-timings-tenant-api.json").read_text()) == {"t/a": 7.0}
    assert "repos/o/r/actions/artifacts/30/zip" not in gh.calls, "an expired artifact was fetched"
    assert "repos/o/r/actions/runs/1/artifacts" not in gh.calls, "kept looking after both found"
    assert "branch=main" in gh.calls[0] and "status=completed" in gh.calls[0]


def test_fetch_failures_are_warnings_never_exceptions(tmp_path: Path, capsys) -> None:
    # 1) the run listing itself fails
    assert prs.fetch_timings("o/r", "w.yaml", "main", tmp_path, ["a"], runner=_gh({})) == {}
    # 2) garbage everywhere: bad JSON, bad zip, wrong member, wrong shape
    gh = _gh({
        "repos/o/r/actions/workflows/w.yaml/runs": (0, json.dumps(
            {"workflow_runs": [{"id": 1}, {"id": 2}, {"id": 3}, {"id": 4}]}).encode()),
        "repos/o/r/actions/runs/1/artifacts": (0, b"not json"),
        "repos/o/r/actions/runs/2/artifacts": (0, json.dumps({"artifacts": [
            {"id": 9, "name": "race-timings-a", "expired": False}]}).encode()),
        "repos/o/r/actions/artifacts/9/zip": (0, b"not a zip"),
        "repos/o/r/actions/runs/3/artifacts": (0, json.dumps({"artifacts": [
            {"id": 8, "name": "race-timings-a", "expired": False}]}).encode()),
        "repos/o/r/actions/artifacts/8/zip": (0, _zip("race-timings-a.json", b'{"p": -1}')),
        "repos/o/r/actions/runs/4/artifacts": (0, json.dumps({"artifacts": [
            {"id": 7, "name": "race-timings-a", "expired": False}]}).encode()),
        "repos/o/r/actions/artifacts/7/zip": (0, _zip("other.json", b'{"p": 1}')),
    })
    assert prs.fetch_timings("o/r", "w.yaml", "main", tmp_path, ["a"], runner=gh) == {}
    assert not (tmp_path / "race-timings-a.json").exists()
    err = capsys.readouterr().err
    assert err.count("::warning") >= 5


def test_fetch_cli_exits_zero_even_when_everything_fails(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(prs, "_run", lambda args, cwd=None: subprocess.CompletedProcess(
        args, 1, b"", b"no network"))
    monkeypatch.setattr(prs.fetch_timings, "__defaults__", (prs._run,))
    assert prs.main(["fetch-timings", "--repo", "o/r", "--workflow", "w.yaml",
                     "--out-dir", str(tmp_path)]) == 0


# ── merge ────────────────────────────────────────────────────────────────────


def _shard(raw_dir: Path, module: str, sid: str, lines: list[str] | None, rc: int | None) -> None:
    raw_dir.mkdir(parents=True, exist_ok=True)
    if lines is not None:
        (raw_dir / f"race-raw-{module}-{sid}.jsonl").write_text(
            "".join(line + "\n" for line in lines), encoding="utf-8")
    if rc is not None:
        (raw_dir / f"race-rc-{module}-{sid}").write_text(f"{rc}\n", encoding="utf-8")


def _stream() -> list[str]:
    """Two packages, one failing test, one timeout — enough to make fields differ."""
    return [
        _ev(Action="start", Package="m/a"),
        _ev(Action="run", Package="m/a", Test="TestOK"),
        _ev(Action="pass", Package="m/a", Test="TestOK", Elapsed=0.1),
        _ev(Action="run", Package="m/b", Test="TestBad"),
        _ev(Action="fail", Package="m/b", Test="TestBad", Elapsed=0.2),
        _ev(Action="pass", Package="m/a", Elapsed=1.0),
        _ev(Action="output", Package="m/c", Test="TestSlow",
            Output="panic: test timed out after 30m0s\n"),
        _ev(Action="output", Package="m/c", Test="TestSlow", Output="\trunning tests:\n"),
        _ev(Action="output", Package="m/c", Test="TestSlow", Output="\t\tTestSlow (30m0s)\n"),
        _ev(Action="output", Package="m/c", Test="TestSlow", Output="\n"),
        _ev(Action="fail", Package="m/b", Elapsed=2.0),
        _ev(Action="fail", Package="m/c", Elapsed=1800.1),
    ]


def test_merged_shards_parse_into_the_same_fragment_as_the_unsplit_stream(
        tmp_path: Path) -> None:
    whole = _stream()
    (tmp_path / "whole.jsonl").write_text("".join(x + "\n" for x in whole), encoding="utf-8")
    by_pkg: dict[str, list[str]] = {}
    for line in whole:
        by_pkg.setdefault(json.loads(line)["Package"], []).append(line)
    raw = tmp_path / "raw"
    _shard(raw, "m", "1", by_pkg["m/b"], 1)
    _shard(raw, "m", "2", by_pkg["m/a"] + by_pkg["m/c"], 1)
    res = prs.merge("m", ["1", "2"], raw, tmp_path / "merged.jsonl")
    assert res == {"module": "m", "go_exit_code": 1, "complete": True, "missing_shards": []}

    def frag(p: Path) -> dict:
        return build_fragment("m", p, 1, "u", "w", "./...")
    assert frag(tmp_path / "merged.jsonl") == frag(tmp_path / "whole.jsonl")


def test_merge_rc_is_the_first_nonzero_shard_rc(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    _shard(raw, "m", "1", [], 0)
    _shard(raw, "m", "2", [], 2)
    _shard(raw, "m", "3", [], 1)
    assert prs.merge("m", ["1", "2", "3"], raw, tmp_path / "o.jsonl")["go_exit_code"] == 2
    raw2 = tmp_path / "raw2"
    _shard(raw2, "m", "1", [], 0)
    _shard(raw2, "m", "2", [], 0)
    res = prs.merge("m", ["1", "2"], raw2, tmp_path / "o2.jsonl")
    assert res["go_exit_code"] == 0 and res["complete"] is True


@pytest.mark.parametrize("missing", ["artifact", "rc", "stream"])
def test_a_missing_shard_makes_the_module_incomplete(tmp_path: Path, missing: str) -> None:
    raw = tmp_path / "raw"
    _shard(raw, "m", "1", [_ev(Action="pass", Package="m/a", Elapsed=1.0)], 0)
    if missing == "rc":
        _shard(raw, "m", "2", [_ev(Action="pass", Package="m/b", Elapsed=1.0)], None)
    elif missing == "stream":
        _shard(raw, "m", "2", None, 0)
    res = prs.merge("m", ["1", "2"], raw, tmp_path / "o.jsonl")
    assert res["complete"] is False and res["missing_shards"] == ["2"]
    # Even with every present shard green, the recorded exit code is not 0.
    assert res["go_exit_code"] != 0


def test_merge_cli_writes_outputs_and_annotates_missing(tmp_path: Path, monkeypatch,
                                                        capsys) -> None:
    out_file = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out_file))
    raw = tmp_path / "raw"
    _shard(raw, "m", "1", [], 0)
    assert prs.main(["merge", "--module", "m", "--shards", "1,2", "--raw-dir", str(raw),
                     "--output", str(tmp_path / "o.jsonl")]) == 0
    assert out_file.read_text().splitlines() == ["go_exit_code=1", "complete=false"]
    assert "::error" in capsys.readouterr().out


def test_workflow_withholds_the_fragment_of_an_incomplete_module() -> None:
    """The other half of fail-closed: `complete=false` must stop the parse + upload.

    The report then has no fragment for the module and files `no-result`
    (pinned by test_missing_fragment_reads_as_worse_not_clean).
    """
    import yaml
    wf = yaml.safe_load((ROOT / ".github" / "workflows" / "nightly-race.yaml")
                        .read_text(encoding="utf-8"))
    steps = wf["jobs"]["merge"]["steps"]
    gated = {"Extract failing tests", "Upload fragment", "Record per-package timings",
             "Upload per-package timings"}
    problems = []
    for s in steps:
        if s.get("name") in gated and "steps.merge.outputs.complete == 'true'" not in str(s.get("if")):
            problems.append(f"{s['name']}: if={s.get('if')!r}")
    names = {s.get("name") for s in steps}
    problems += [f"step {n!r} is gone" for n in gated - names]
    assert not problems, "an incomplete module could still be parsed/uploaded:\n  " + "\n  ".join(problems)
