"""#3703：key verdict 文件的 `key_checks`（`version` label、已改名的鍵）不把作者寫的鍵與標籤值帶進產生器的訊息。

`test_reserved_value_canary_matrix.py` 守保留鍵的值；這裡守 `key_checks` 這條通道。Go 交出的是結構化記錄
（kind、key、head、replacement_head、reason），`reason` 是每個 kind 一句的常數句；產生器那一行由
Python 用常數句型組成（`_lib_key_verdict.key_check_line`），鍵與指標名只經 `shown_key` 出去。

* 矩陣由 kind 的封閉集合推導：`_lib_tenant_values.KEY_CHECK_KINDS`，它由
  `tests/shared/test_key_check_kinds_parity.py` 對照 Go 的 `config.KeyCheckKinds` 常數。Go 多一個
  kind 而這裡沒有寫法 → `test_the_matrix_is_every_kind` 紅。
* 每個 kind 把 canary 放在三個位置：標籤值、標籤名、指標名。指標名那格用超過 64 字元的名字——
  名稱形的指標名本來就照印（`shown_key` 的規則），長名字則只能以替身出現。renamed 類的指標名
  是登錄表裡的舊名，作者放不進 canary，那三格逐一列在 `_NOT_APPLICABLE` 並寫理由。
* 跑 `--validate --strict --findings-json`，stdout、stderr、findings 檔一起比對、不分大小寫；並證明
  那一格真的被判了：對應的 finding kind 剛好一筆、`field` 是鍵的替身、訊息帶 kind、指標名
  （名稱形時）與同一個替身。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.usefixtures("judging_run")

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(REPO / "scripts" / "tools"))
from test_routing_value_canary_matrix import validate_run  # noqa: E402
from _lib_validation import shown_key  # noqa: E402
import _lib_tenant_values as tv  # noqa: E402

# A test canary, not a credential: no real host, user or password. Mixed case
# on purpose; no `,`, `"` or `}` — they would end the label.
_CANARY = "CaNaRy-https://u:PW@hooks.example/x"  # trufflehog:ignore
_STAND_IN = re.compile(r"<key:[0-9a-f]{6}>")
_LONG_HEAD = "canary_" + "m" * 70  # not name-like: longer than 64 chars


def _no_canary(text: str) -> None:
    assert "canary" not in text.lower(), text


#: Where the canary goes: (metric name, label name, label value).
_PLACES = {
    "label_value": ("container_cpu", "env", _CANARY),
    "label_name": ("container_cpu", "canary_lbl", "x"),
    "metric_name": (_LONG_HEAD, "env", "x"),
}


def _version(head: str, name: str, value: str, version: str) -> dict[str, str]:
    return {f'{head}{{{name}="{value}",{version}}}': "1"}


#: Per kind: the tenant keys that cause it, from (metric name, label name,
#: label value). A renamed kind's metric name is the alias table's.
_KEYS = {
    "version_label_regex": lambda h, n, v: _version(h, n, v, 'version=~"v.*"'),
    "version_label_empty": lambda h, n, v: _version(h, n, v, 'version=""'),
    "version_label_default": lambda h, n, v: _version(h, n, v, 'version="default"'),
    "version_label_charset": lambda h, n, v: _version(h, n, "x", f'version="{v} BAD"'),
    "renamed": lambda _h, n, v: {f'mysql_cpu{{{n}="{v}"}}': "1"},
    "renamed_shadowed": lambda _h, n, v: {f'mysql_cpu{{{n}="{v}"}}': "1",
                                          f'mysql_threads_running{{{n}="{v}"}}': "2"},
    "renamed_critical_dimensional": lambda _h, n, v: {f'mysql_cpu_critical{{{n}="{v}"}}': "1"},
}

#: The metric name of each kind's key, where it is not the place's.
_HEADS = {"renamed": "mysql_cpu", "renamed_shadowed": "mysql_cpu",
          "renamed_critical_dimensional": "mysql_cpu_critical"}

_NOT_APPLICABLE = {
    (k, "metric_name"): "a retired spelling is one the alias table lists: its metric name is "
                        "the table's, never the author's"
    for k in tv.KEY_CHECK_RENAMED_KINDS
}

_CASES = [(kind, place) for kind in sorted(tv.KEY_CHECK_KINDS) for place in _PLACES
          if (kind, place) not in _NOT_APPLICABLE]


def test_the_matrix_is_every_kind():
    """每個 kind 都有寫法，沒有多的；不適用的格子都附理由。"""
    assert set(_KEYS) == set(tv.KEY_CHECK_KINDS)
    assert set(_HEADS) == set(tv.KEY_CHECK_RENAMED_KINDS) <= set(tv.KEY_CHECK_KINDS)
    assert all(kind in tv.KEY_CHECK_KINDS and place in _PLACES and reason.strip()
               for (kind, place), reason in _NOT_APPLICABLE.items())


def _files(keys: dict[str, str]) -> dict[str, dict]:
    root = {"defaults": {"container_cpu": 80, "mysql_threads_running": 70,
                         "mysql_threads_running_critical": 90}}
    return {"_defaults.yaml": root, "t-a.yaml": {"tenants": {"t-a": {"container_cpu": "85", **keys}}}}


@pytest.mark.parametrize("kind,place", _CASES, ids=[f"{k}-{p}" for k, p in _CASES])
def test_no_key_check_carries_the_key_as_written(tmp_path, kind, place):
    head, name, value = _PLACES[place]
    findings, text = validate_run(tmp_path, _files(_KEYS[kind](head, name, value)))
    _no_canary(text)
    finding_kind = "key_renamed" if kind in tv.KEY_CHECK_RENAMED_KINDS else "key_version_label"
    checks = [f for f in findings if f["kind"] in ("key_renamed", "key_version_label")]
    assert [f["kind"] for f in checks] == [finding_kind], findings
    [f] = checks
    assert f["tenant"] == "t-a" and f["blocks"] == "never", f
    assert _STAND_IN.fullmatch(f["field"]), f
    message = f["message"]
    assert f"key check {kind}: " in message and f"key {f['field']}" in message, f
    shown_head = shown_key(_HEADS.get(kind, head))
    assert f"metric {shown_head}," in message, f
    if place == "metric_name":
        assert _STAND_IN.fullmatch(shown_head), shown_head
    if kind in tv.KEY_CHECK_RENAMED_KINDS:
        assert "replacement metric mysql_threads_running" in message, f


# ── Records of the wrong shape (#3703 review) ─────────────────────────────
#
# A record da-guard writes wrong is output this reader cannot read: it is
# reported as such (a `DaGuardError` that is `broken`, or `stale` for the
# shape an older da-guard wrote), never read as some other answer — and the
# message that reports it names the place and a type, never a value: the
# record carries the author's key. A shim runs the real da-guard and breaks
# its `key_checks` one way per cell; every way of breaking a record is
# derived from `tv.KeyCheck._fields`, the reader's one list of the fields.

import json  # noqa: E402
import os  # noqa: E402
import subprocess  # noqa: E402

import _lib_key_verdict as klv  # noqa: E402

_GAR = REPO / "scripts" / "tools" / "ops" / "generate_alertmanager_routes.py"

_SHIM = r'''
import json, os, subprocess, sys
p = subprocess.run([os.environ["KC_REAL_GUARD"], *sys.argv[1:]], capture_output=True)
brk = json.loads(os.environ["KC_BREAK"])
if (sys.argv[1:2] or [""])[0] != brk["sub"]:
    sys.stdout.buffer.write(p.stdout); sys.stderr.buffer.write(p.stderr); sys.exit(p.returncode)
d = json.loads(p.stdout)
kv = brk["sub"] == "key-verdict"
checks = d["key_checks"] if kv else {t: v["key_checks"] for t, v in d["tenants"].items()}
if not checks.get("t-a"):
    sys.exit("shim: the real da-guard wrote no key check for t-a: %r" % (checks,))
where, value = brk["where"], brk.get("value")
if where == "container":
    d["key_checks"] = value
elif where == "tenant" and kv:
    d["key_checks"]["t-a"] = value
elif where == "tenant":
    d["tenants"]["t-a"]["key_checks"] = value
elif where == "record":
    checks["t-a"][0] = value
else:
    rec = checks["t-a"][0]
    if brk["op"] == "drop":
        del rec[where]
    else:
        rec[where] = value
sys.stdout.write(json.dumps(d))
sys.stderr.buffer.write(p.stderr)
sys.exit(p.returncode)
'''

_SUBS = ("key-verdict", "served-values")
_WRONG_TYPE = [_CANARY]       # no field of a record is a list
_EXTRA_FIELD = "extra"
assert _EXTRA_FIELD not in tv.KeyCheck._fields

#: A canary *string* in these fields is a record of the right shape: `key`
#: and `head` are the author's text by definition (a message shows them
#: through `shown_key`), so the cell expects the record read and the canary
#: shown only as a stand-in. Every other field's string is checked
#: (`kind` against KEY_CHECK_KINDS, `replacement_head` against the kind —
#: the tree's one record is a `version` kind, which has none).
_A_STRING_IS_READ = {"key", "head"}

_REASON_WHY = ("`reason` is Go's constant sentence for the kind, which a report prints as Go "
               "wrote it (ADR-038 §11); key_check_test.go holds every Reason free of the "
               "author's text. A canary da-guard itself put there would be printed")

#: Cells not run, each with why.
_SKIPPED = {
    ("served-values", "container"): "served-values has no `key_checks` object of its own: each "
                                    "tenant carries its list (the `tenant` cells)",
    ("key-verdict", "set-string-reason"): _REASON_WHY,
    ("served-values", "set-string-reason"): _REASON_WHY,
}


def _breaks() -> list[tuple[str, dict, str]]:
    """(id, break, expected) — expected: "broken", "older" or "read"."""
    out = [(f"add-{_EXTRA_FIELD}", {"where": _EXTRA_FIELD, "op": "set", "value": _CANARY}, "broken")]
    for f in tv.KeyCheck._fields:
        out.append((f"drop-{f}", {"where": f, "op": "drop"}, "broken"))
        out.append((f"set-type-{f}", {"where": f, "op": "set", "value": _WRONG_TYPE}, "broken"))
        out.append((f"set-string-{f}", {"where": f, "op": "set", "value": _CANARY},
                    "read" if f in _A_STRING_IS_READ else "broken"))
    out += [
        ("container", {"where": "container", "value": [_CANARY]}, "broken"),
        ("tenant", {"where": "tenant", "value": _CANARY}, "broken"),
        ("tenant-older-shape", {"where": "tenant",
                                "value": {"version_labels": [f"WARN {_CANARY}"], "renames": []}}, "older"),
        ("record", {"where": "record", "value": _CANARY}, "broken"),
    ]
    return out


_BREAK_CASES = [(sub, cid, brk, exp) for sub in _SUBS for cid, brk, exp in _breaks()
                if (sub, cid) not in _SKIPPED]


def test_every_field_is_broken_every_way():
    """矩陣由 `KeyCheck._fields` 推導：每個欄位都有缺、型別錯、換成字串三格；略過的格子都附理由。"""
    ids = {cid for _s, cid, _b, _e in _BREAK_CASES} | {cid for _s, cid in _SKIPPED}
    for f in tv.KeyCheck._fields:
        assert {f"drop-{f}", f"set-type-{f}", f"set-string-{f}"} <= ids, f
    assert _A_STRING_IS_READ <= set(tv.KeyCheck._fields)
    assert all(sub in _SUBS and why.strip() for (sub, _c), why in _SKIPPED.items())


@pytest.fixture(scope="module")
def _shim(tmp_path_factory) -> Path:
    real = os.environ.get("DA_GUARD_BINARY", "")
    assert real and Path(real).is_file(), "no da-guard to wrap ($DA_GUARD_BINARY)"
    path = tmp_path_factory.mktemp("kc-shim") / "da-guard"
    path.write_text(f"#!{sys.executable}\n{_SHIM}", encoding="utf-8")
    path.chmod(0o755)
    return path


def _break_tree(tmp_path: Path) -> Path:
    """One tenant, one key check (version_label_default), the canary in the key as written."""
    d = tmp_path / "conf.d"
    d.mkdir()
    for name, body in _files({f'container_cpu{{env="{_CANARY}",version="default"}}': "1"}).items():
        (d / name).write_text(json.dumps(body), encoding="utf-8")
    return d


@pytest.mark.parametrize("sub,cid,brk,expected", _BREAK_CASES,
                         ids=[f"{s}-{c}" for s, c, _b, _e in _BREAK_CASES])
def test_a_broken_record_is_reported_without_its_content(tmp_path, monkeypatch, _shim,
                                                         sub, cid, brk, expected):
    conf_d = _break_tree(tmp_path)
    real = os.environ["DA_GUARD_BINARY"]
    env = {"KC_REAL_GUARD": real, "KC_BREAK": json.dumps({"sub": sub, **brk}),
           "DA_GUARD_BINARY": str(_shim)}
    for k, v in env.items():
        monkeypatch.setenv(k, v)

    # (a) The reader's answer: an error of the class this output is, or the record.
    def read():
        if sub == "key-verdict":
            return klv.judge_tree(conf_d).key_checks["t-a"]
        return tv.load_served_tree(conf_d).tenants["t-a"].key_checks
    if expected == "read":
        [c] = read()
        assert getattr(c, brk["where"]) == _CANARY
    else:
        with pytest.raises(tv.DaGuardError) as ei:
            read()
        err = ei.value
        stale = expected == "older" and sub == "served-values"
        assert (err.stale, err.broken) == (stale, not stale), (err.stale, err.broken, str(err))
        _no_canary(str(err))

    # (b) The route generator: nothing of the record in stdout, stderr or findings.
    out = tmp_path / "f.json"
    p = subprocess.run([sys.executable, str(_GAR), "--config-dir", str(conf_d), "--validate",
                        "--strict", "--findings-json", str(out)],
                       capture_output=True, text=True, encoding="utf-8", timeout=300,
                       env={**os.environ, **env})
    assert "Traceback" not in p.stderr, p.stderr
    assert out.exists(), p.stderr
    written = out.read_text(encoding="utf-8")
    _no_canary(p.stdout + p.stderr + written)
    kinds = {f["kind"] for f in json.loads(written)["findings"]}
    if expected == "read":
        assert "key_version_label" in kinds, kinds
    elif sub == "key-verdict":
        assert "key_verdict_incomplete" in kinds, kinds
    else:
        assert "custom_alert_delivery_unchecked" in kinds, kinds


# ── key_check_line itself (#3703 review) ──────────────────────────────────
# The route generator's tenants and alias-table names are name-like, so the
# matrices above never see `key_check_line` show the tenant or the
# replacement head as a stand-in; these feed it ones that are not.

_NOT_A_NAME = "CaNaRy tenant://u:PW@h/x"


def _record(kind: str, replacement_head: str | None) -> tv.KeyCheck:
    return tv.KeyCheck(kind, 'm{env="x",version="default"}', "m", replacement_head, "a reason")


def test_key_check_line_shows_the_tenant_through_shown_key():
    line = klv.key_check_line(_NOT_A_NAME, _record("version_label_default", None))
    _no_canary(line)
    assert f"tenant={shown_key(_NOT_A_NAME)}:" in line, line
    assert _STAND_IN.fullmatch(shown_key(_NOT_A_NAME))


def test_key_check_line_shows_the_replacement_head_through_shown_key():
    line = klv.key_check_line("t-a", _record("renamed", _NOT_A_NAME))
    _no_canary(line)
    assert f"replacement metric {shown_key(_NOT_A_NAME)} " in line, line
