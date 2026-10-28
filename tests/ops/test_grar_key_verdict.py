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


@pytest.mark.parametrize("files,rc", [(THRESHOLDS_ONLY, 0), (NOTHING_RENDERED, 1), (ROUTED, 0)],
                         ids=["no-routing", "nothing-rendered", "routed"])
def test_validate_reports_the_shared_verdict_and_keeps_its_exit_code(tmp_path, files, rc):
    res, doc = _cli(tmp_path, files, "--validate", "--strict")
    assert res.returncode == rc, res.stderr
    tree = tmp_path / "conf.d"
    want = [kv.finding_line(f, 5) for f in kv.judge_tree(tree).findings]
    assert any("mysql_conections" in w for w in want), want  # the case judges something
    for w in want:
        assert f"key verdict: {w}" in res.stderr, res.stderr
    recs = _key_records(doc)
    assert [r["message"] for r in recs] == [f"WARN: key verdict: {w}" for w in want]
    for r in recs:
        assert (r["kind"], r["blocks"], r["severity"]) == ("key_unread", "never", "warning"), r
        assert (r["tenant"], r["file"]) == ("t-a", "t-a.yaml"), r
    assert {r["field"] for r in recs} >= {"mysql_conections"}


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


def test_no_root_platform_file_is_said(tmp_path):
    files = {"t-a.yaml": "tenants:\n  t-a:\n    mysql_connections: \"70\"\n"}
    res, doc = _cli(tmp_path, files, "--validate")
    assert res.returncode == 0, res.stderr
    recs = _key_records(doc)
    assert [(r["kind"], r["severity"], r["blocks"]) for r in recs] == [
        ("key_undecidable", "note", "never")], recs
    assert "cannot be judged (undecidable)" in recs[0]["message"]


# ── in-process: a verdict that cannot be given, and who asks for one ──────

def _main(tmp_path, monkeypatch, judge, files=THRESHOLDS_ONLY, **kw):
    d = h.write_tree(tmp_path / "conf.d", files)
    monkeypatch.setattr(kv, "judge_tree", judge)
    with pytest.raises(SystemExit) as exc:
        gar.main(["--config-dir", str(d), "--validate"], **kw)
    return gar._exit_code_of(exc.value)


@pytest.mark.parametrize("exc", [
    tv.ServedValuesError("da-guard served-values exited -6", -6, ""),
    tv.DaGuardNotFoundError("no da-guard"),
    SystemExit(3),
    ValueError("anything else"),
], ids=["served-values", "no-da-guard", "system-exit", "value-error"])
def test_a_verdict_that_fails_is_a_notice_never_a_refusal(tmp_path, monkeypatch, capsys, exc):
    def fail(*_a, **_k):
        raise exc
    assert _main(tmp_path, monkeypatch, fail) == 0
    err = capsys.readouterr().err
    assert "NOTICE: key verdict incomplete: the keys no reader reads were not judged" in err, err
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
