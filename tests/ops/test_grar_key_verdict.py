"""ADR-038 §5：路由產生器 `--validate` 報出共用判決（`_lib_key_verdict`）。

- 判到的鍵印在 stderr，行文與 validate-config `unread_keys` 列相同，各是一筆
  `blocks: never` 的 finding；結束碼不變（#3016 之前只報不擋）。
- 產生器在 `_validate_mode` 之前就結束的樹（沒有可產生的路由或抑制規則，
  rc 1）也判。
- 只在 `--validate`；拒收的樹不判。
- 判不出來（da-guard 失敗，或判決丟出任何例外，含 SystemExit）只是一行
  NOTICE，結束碼照產生器的判定（決定 5）。
- 產生器容器以 `key_verdict=False` 呼叫：它自己在子期限內判，不在 `gar.main`
  裡再判一次。

Tenant ids are fixture names (CLAUDE.md #9).
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _grar_findings_cases as cases  # noqa: E402
import _gsvc_harness as h  # noqa: E402

import _gsvc_child as child  # noqa: E402
import _gsvc_proto as proto  # noqa: E402
import _lib_key_verdict as kv  # noqa: E402
import _lib_tenant_values as tv  # noqa: E402
import generate_alertmanager_routes as gar  # noqa: E402
from _lib_validation import shown_key  # noqa: E402

#: The route generator reads trees Go names paths on as its own findings.
pytestmark = pytest.mark.usefixtures("judging_run")

DEFAULTS = "defaults:\n  mysql_connections: 80\n"
# No `_routing`: only the severity-dedup inhibit rule is rendered.
THRESHOLDS_ONLY = {
    "_defaults.yaml": DEFAULTS,
    "t-a.yaml": "tenants:\n  t-a:\n    mysql_connections: \"70\"\n"
                "    mysql_conections: \"70\"\n    _routng: x\n",
}
# Nothing to render (no `_routing`, dedup off): the generator ends before
# `_validate_mode`, at "No valid routes or inhibit rules generated" (rc 1).
NOTHING_RENDERED = {
    "_defaults.yaml": DEFAULTS,
    "t-a.yaml": "tenants:\n  t-a:\n    mysql_conections: \"70\"\n    _severity_dedup: disable\n",
}
ROUTED = {
    "_defaults.yaml": DEFAULTS,
    "t-a.yaml": "tenants:\n  t-a:\n    mysql_connections: \"70\"\n    mysql_conections: \"70\"\n"
                "    _routing:\n      receiver:\n        type: webhook\n"
                "        url: https://hooks.example.com/t-a\n",
}


def _cli(tmp_path: Path, files: dict[str, str], *argv: str):
    d = h.write_tree(tmp_path / "conf.d", files)
    out = tmp_path / "findings.json"
    res = cases.run_gar(d, list(argv), ["--findings-json", str(out)])
    return res, json.loads(out.read_text(encoding="utf-8"))


def _key_records(doc: dict) -> list[dict]:
    return [r for r in doc["findings"] if r["kind"].startswith("key_")]


@pytest.mark.parametrize("files", [THRESHOLDS_ONLY, NOTHING_RENDERED, ROUTED],
                         ids=["no-routing", "nothing-rendered", "routed"])
def test_validate_fails_on_the_shared_verdicts_errors(tmp_path, files):
    """#3016: an error the shared verdict gives fails `--validate` (rc 1) on
    every path the run ends on — after rendering, and where it ends with
    nothing to render."""
    res, doc = _cli(tmp_path, files, "--validate", "--strict")
    assert res.returncode == 1, res.stderr
    tree = tmp_path / "conf.d"
    want = [kv.finding_line(f, 5) for f in kv.judge_tree(tree).findings]
    assert any("mysql_conections" in w for w in want), want  # the case judges something
    for w in want:
        assert f"key verdict: {w}" in res.stderr, res.stderr
    recs = _key_records(doc)
    assert [r["message"] for r in recs] == [f"ERROR: key verdict: {w}" for w in want]
    for r in recs:
        assert (r["kind"], r["blocks"], r["severity"]) == ("key_unread", "validate", "error"), r
        assert (r["tenant"], r["file"]) == ("t-a", "t-a.yaml"), r
    assert {r["field"] for r in recs} >= {"mysql_conections"}
    assert doc["exit_code"] == 1


def test_a_reserved_keys_schedule_fails_validate(tmp_path):
    """#3834: a reserved key written with a time window in the tenant's own
    file is read as its `default` alone (#3269) — before this, `--validate`
    passed it (`OK: all configs valid`). Now the verdict's schedule_unread
    error fails the run; the same window on a threshold key is read and
    passes."""
    window = "      default: disable\n      overrides:\n        - {window: \"01:00-02:00\", value: all}\n"
    res, doc = _cli(tmp_path, {"_defaults.yaml": DEFAULTS,
                               "t-a.yaml": "tenants:\n  t-a:\n    _silent_mode:\n" + window},
                    "--validate")
    assert res.returncode == 1, res.stderr
    assert [(r["kind"], r["blocks"], r["severity"], r["field"]) for r in _key_records(doc)] == [
        ("key_schedule_unread", "validate", "error", "_silent_mode")], doc
    res, doc = _cli(tmp_path / "threshold", {
        "_defaults.yaml": DEFAULTS,
        "t-a.yaml": "tenants:\n  t-a:\n    mysql_connections:\n      default: \"70\"\n"
                    "      overrides:\n        - {window: \"01:00-02:00\", value: \"90\"}\n"},
        "--validate")
    assert res.returncode == 0, res.stderr
    assert _key_records(doc) == [], doc


def test_a_warning_alone_does_not_fail_validate(tmp_path):
    """A `disable` that switches nothing off is a warning (decision 8): it is
    reported, `blocks: never`, and the run passes."""
    files = {"_defaults.yaml": DEFAULTS,
             "t-a.yaml": "tenants:\n  t-a:\n    mysql_connections: \"70\"\n    mysql_slave_lagg: disable\n"}
    res, doc = _cli(tmp_path, files, "--validate")
    assert res.returncode == 0, res.stderr
    assert [(r["kind"], r["blocks"], r["severity"]) for r in _key_records(doc)] == [
        ("key_disable_noop", "never", "warning")], doc


def test_the_verdict_lines_are_validate_configs(tmp_path):
    """One formatter: validate-config's `unread_keys` row prints the same lines."""
    sys.path.insert(0, str(cases.REPO / "scripts" / "tools" / "ops"))
    import validate_config as vc
    res, _doc = _cli(tmp_path, THRESHOLDS_ONLY, "--validate")
    row = vc.check_unread_keys(str(tmp_path / "conf.d"))
    assert row["details"], row
    for line in row["details"]:
        assert f"key verdict: {line}" in res.stderr, (line, res.stderr)


def test_without_validate_nothing_is_judged(tmp_path):
    res, doc = _cli(tmp_path, ROUTED, "--dry-run")
    assert "key verdict" not in res.stderr
    assert _key_records(doc) == []


def test_a_refused_tree_is_not_judged(tmp_path):
    files = dict(THRESHOLDS_ONLY, **{"t-b.yaml": "tenants:\n  t-a:\n    mysql_connections: \"1\"\n"})
    res, doc = _cli(tmp_path, files, "--validate")
    assert res.returncode == 1, res.stderr  # a tenant declared twice
    assert "key verdict" not in res.stderr
    assert _key_records(doc) == []


def test_a_threshold_key_no_root_platform_file_lets_the_verdict_judge_fails(tmp_path):
    """#3016 option A: an undecidable threshold key fails `--validate`, as
    tenant-api refuses such a write (422)."""
    files = {"t-a.yaml": "tenants:\n  t-a:\n    mysql_connections: \"70\"\n"}
    res, doc = _cli(tmp_path, files, "--validate")
    assert res.returncode == 1, res.stderr
    recs = _key_records(doc)
    assert [(r["kind"], r["severity"], r["blocks"]) for r in recs] == [
        ("key_undecidable", "error", "validate")], recs
    assert "1 threshold key(s) cannot be judged (undecidable)" in recs[0]["message"]


def test_an_undecidable_key_beside_an_error_is_marked_as_blocking_too(tmp_path):
    """#3016 review: with an error also there, the undecidable line still
    says it blocks — fixing the error alone leaves rc 1 (Go's
    `undecidable_blocks`, not inferred from the findings)."""
    files = {"t-a.yaml": "tenants:\n  t-a:\n    mysql_connections: \"70\"\n    _routng: x\n"}
    res, doc = _cli(tmp_path, files, "--validate")
    assert res.returncode == 1, res.stderr
    got = {(r["kind"], r["severity"], r["blocks"]) for r in _key_records(doc)}
    assert ("key_undecidable", "error", "validate") in got, doc
    assert ("key_unread", "error", "validate") in got, doc


def test_a_key_name_with_a_control_character_is_never_printed_raw(tmp_path):
    """#3016 review: the FAIL list repeats the blocking key verdict lines —
    a tenant's key name in them goes through safe_label there too; a key
    that is not name-like is not printed at all (#1560 ⑤-b: it may be a
    URL), only its stand-in (#3703)."""
    files = dict(THRESHOLDS_ONLY, **{"t-a.yaml": "tenants:\n  t-a:\n    \"mysql_conn\\e[2Jx\": \"1\"\n"})
    res, _ = _cli(tmp_path, files, "--validate")
    assert res.returncode == 1, res.stderr
    shown = f"`{shown_key('mysql_conn' + chr(0x1b) + '[2Jx')}`"  # #3703: its stand-in
    assert "FAIL:" in res.stderr and shown in res.stderr.split("FAIL:")[1], res.stderr
    assert "mysql_conn" not in res.stderr
    assert "\x1b" not in res.stderr


def test_a_routing_only_root_defaults_file_passes(tmp_path):
    """#3016: a root `_defaults.yaml` holding only `_routing_defaults` (no
    `defaults:` wrapper, so effective reads the whole document) is valid —
    the route generator reads the key there; the verdict must not call it
    unread now that the gates block."""
    files = {"_defaults.yaml": "_routing_defaults:\n  receiver:\n    type: webhook\n"
                               "    url: https://hooks.example.com/noc\n",
             "t-a.yaml": "tenants:\n  t-a:\n    _routing:\n      receiver:\n        type: webhook\n"
                         "        url: https://hooks.example.com/t-a\n"}
    res, doc = _cli(tmp_path, files, "--validate")
    assert res.returncode == 0, res.stderr
    assert [r for r in _key_records(doc) if r["kind"] == "key_unread"] == [], doc


def test_a_tree_with_only_routing_and_no_root_platform_file_passes(tmp_path):
    """ADR-038 decision 7: a tree with only routing settings has no
    threshold key the verdict could not judge — rc 0, the line a note."""
    files = {"t-a.yaml": "tenants:\n  t-a:\n    _routing:\n      receiver:\n        type: webhook\n"
                         "        url: https://hooks.example.com/t-a\n"}
    res, doc = _cli(tmp_path, files, "--validate")
    assert res.returncode == 0, res.stderr
    assert [(r["kind"], r["severity"], r["blocks"]) for r in _key_records(doc)] == [
        ("key_undecidable", "note", "never")], doc


# ── in-process: a verdict that cannot be given, and who asks for one ──────

def _main(tmp_path, monkeypatch, judge, files=THRESHOLDS_ONLY, **kw):
    d = h.write_tree(tmp_path / "conf.d", files)
    monkeypatch.setattr(kv, "judge_tree", judge)
    with pytest.raises(SystemExit) as exc:
        gar.main(["--config-dir", str(d), "--validate"], **kw)
    return gar._exit_code_of(exc.value)


@pytest.mark.parametrize("exc, rc", [
    (tv.ServedValuesError("da-guard served-values exited -6", -6, ""), 1),
    # The binary's own fault is no verdict on the config: rc 2, as
    # validate-config's unread_keys (#3016 review).
    (tv.DaGuardNotFoundError("no da-guard"), 2),
    (tv.DaGuardError("da-guard key-verdict exited 139", 139, "", broken=True), 2),
    (tv.DaGuardError("da-guard key-verdict: no field blocks", 0, "", stale=True), 2),
    (SystemExit(3), 1),
    (ValueError("anything else"), 1),
], ids=["served-values", "no-da-guard", "broken", "stale", "system-exit", "value-error"])
def test_a_verdict_that_fails_fails_validate(tmp_path, monkeypatch, capsys, exc, rc):
    """#3016: no verdict, no pass — as tenant-api refuses a write it has no
    verdict for (decision 5)."""
    def fail(*_a, **_k):
        raise exc
    assert _main(tmp_path, monkeypatch, fail) == rc
    err = capsys.readouterr().err
    assert "ERROR: key verdict incomplete: the keys no reader reads were not judged" in err, err
    assert type(exc).__name__ in err


def test_key_verdict_false_asks_for_none(tmp_path, monkeypatch, capsys):
    calls = []

    def judge(*a, **k):
        calls.append(a)
        return kv.TreeVerdict({}, [], False)
    assert _main(tmp_path, monkeypatch, judge, key_verdict=False) == 0
    assert calls == []
    assert "key verdict" not in capsys.readouterr().err
    assert _main(tmp_path, monkeypatch, judge) == 0  # the control: the CLI default asks
    assert len(calls) == 1


def test_the_generator_container_does_not_judge_keys_inside_the_generator(tmp_path, monkeypatch):
    """The container runs `gar.main` in its judgement process and judges the
    keys itself afterwards (`_gsvc_child.key_verdict`, its own deadline): the
    generator must not judge them a second time, outside it."""
    repo = h.init_repo(tmp_path / "repo")
    base = h.commit_tree(repo, {"_defaults.yaml": DEFAULTS,
                                "t-a.yaml": "tenants:\n  t-a:\n    mysql_connections: \"50\"\n"})
    write = proto.Write("t-a.yaml", h.b64("tenants:\n  t-a:\n    mysql_conections: \"50\"\n"))
    req = proto.ValidateRequest(base, (write,), 20000)
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    job = child.ChildJob(request=req, job_dir=str(job_dir), repo=str(repo), config_subdir="conf.d",
                         git="git", object_format="sha1", memory_limit=0, policy_copy=None,
                         policy_sha256=None, store_entries={}, max_transfer=1 << 20,
                         deadline_at=time.monotonic() + 60)
    calls = []

    def judge(*a, **k):
        calls.append(a)
        return kv.TreeVerdict({}, [], False)
    monkeypatch.setattr(kv, "judge_tree", judge)
    monkeypatch.setattr(child, "key_verdict", lambda *_a: {"status": "stubbed"})
    r, w = os.pipe()
    try:
        out = child._judge(job, child._Stream(w, 1 << 20))
    finally:
        os.close(w)
        while os.read(r, 1 << 16):
            pass
        os.close(r)
    assert out.get("rc") == 0, out
    assert out.get("key_verdict") == {"status": "stubbed"}, out  # the container's own ran
    assert calls == [], "the generator judged the keys inside the container"


# ── ADR-038 §11: the exporter's key checks, printed as it words them ──────

KEY_CHECKS = {
    "_defaults.yaml": "defaults:\n  container_cpu: 80\n  mysql_threads_running: 30\n",
    "t-a.yaml": "tenants:\n  t-a:\n    mysql_cpu: \"40\"\n"
                "    container_cpu{version=\"V2!\"}: \"5\"\n",
}


def test_validate_prints_the_exporters_key_checks(tmp_path):
    """A `version` label the exporter refuses and a retired spelling: the
    exporter's own lines (da-guard served-values' key_checks), not a judgement
    of this tool's; reported only (`blocks: never`). The run fails on the
    dimensional key itself, which no row reads (its `version` label is not
    declared under `dimensions:`)."""
    res, doc = _cli(tmp_path, KEY_CHECKS, "--validate")
    assert res.returncode == 1, res.stderr
    recs = {r["kind"]: r for r in _key_records(doc)}
    label, renamed = recs.get("key_version_label"), recs.get("key_renamed")
    assert label and 'version "V2!"' in label["message"], doc
    assert renamed and '"mysql_cpu" was renamed to "mysql_threads_running"' in renamed["message"], doc
    for r in (label, renamed):
        assert r["blocks"] == "never" and r["tenant"] == "t-a", r
        assert r["message"].strip() in res.stderr, res.stderr


def test_without_validate_no_key_check_is_printed(tmp_path):
    """Render mode judges no key name: the tool no longer warns on keys."""
    d = h.write_tree(tmp_path / "conf.d", KEY_CHECKS)
    res = cases.run_gar(d, [], [])
    assert "renamed" not in res.stderr and "version" not in res.stderr, res.stderr
