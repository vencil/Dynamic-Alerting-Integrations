"""#3703：conf.d 的檔名與目錄名不進訊息——route generator 的動態 canary 矩陣。

`test_routing_value_canary_matrix.py`、`test_reserved_value_canary_matrix.py` 守
的是設定作者寫的「值」與「鍵名」；這裡守作者決定的另一種文字：conf.d 樹裡的
檔名與目錄名。規則：一個不合法的名字（`_lib_confd.configmap_key_problem` 不放
行的，例如含空白或 `@`）不會原樣出現在 generator 的 stdout、stderr 與
`--findings-json` 任何欄位；合法的照印。唯一出口是 `_lib_validation.shown_file`，
`Finding.file` 在建構子過它。

* 每個情境把 canary 放進檔名或目錄名，跑 `--validate --strict
  --findings-json`，斷言 (a) 該位置真的被判了——預期的 finding kind（或
  沒有 finding 的位置：該 WARN 的固定片段）在，且那個名字的替身
  `<key:xxxxxx>` 出現在輸出裡；(b) canary 不出現在任何輸出（不分大小寫）。
* 一部分情境另跑 `--output-alertmanager`：render 那條路的輸出（含寫出的
  alertmanager.yml）也沒有 canary。
* 正向：合法檔名照印，`file` 等於原樣相對路徑——包括 63 字元 tenant id 的
  `<id>.yaml`（68 字元）與 `.yml`：tenant-api 拿 `file` 比對自己寫的路徑
  （`gate.go` 的 `Related`）。
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "tools"))
from _lib_validation import shown_file, shown_key  # noqa: E402

_GAR = REPO / "scripts" / "tools" / "ops" / "generate_alertmanager_routes.py"
_CANARY = "zqx"  # every canary name holds it; compared lower-cased

_ROOT = "defaults:\n  cpu_usage_percent: 80\n"
_D = "ZqX DIR@y/"  # a directory name that is not a legal file name
_WH = "{receiver: {type: webhook, url: 'https://a.example/x'}}"
_UNK = "    nosuch_key_zz: \"1\"\n"


def _tenant(tid: str, extra: str = "") -> str:
    return f"tenants:\n  {tid}:\n    cpu_usage_percent: \"85\"\n{extra}"


#: name → (files, what proves the place was judged, the path whose stand-in
#: the output shows). A file name `@symlink:<name>` is a dangling symlink to
#: its body, `@fifo:<name>` a FIFO. The proof is ("kind", k): a finding of
#: kind k; or ("text", s): a line of stdout / stderr holding s (a place the
#: generator only warns about on stderr, with no finding).
_SCENARIOS: dict[str, tuple[dict[str, str], tuple[str, str], tuple[str, ...]]] = {
    "key_verdict": ({"_defaults.yaml": _ROOT, "ZqX CANARY@x.yaml": _tenant("db-x", _UNK)},
                    ("kind", "key_unread"), ("ZqX CANARY@x.yaml",)),
    "unreadable": ({"_defaults.yaml": _ROOT, "db-ok.yaml": _tenant("db-ok"),
                    "ZqX BAD@x.yaml": "tenants:\n  db-y: [unclosed\n"},
                   ("kind", "tenant_file_unreadable"), ("ZqX BAD@x.yaml",)),
    "duplicate": ({"_defaults.yaml": _ROOT, "ZqX DUP1@a.yaml": _tenant("db-x"),
                   "ZqX DUP2@b.yaml": _tenant("db-x")},
                  ("kind", "duplicate_tenant"), ("ZqX DUP1@a.yaml", "ZqX DUP2@b.yaml")),
    "subdir": ({"_defaults.yaml": _ROOT, _D + "db-x.yaml": _tenant("db-x", _UNK)},
               ("kind", "key_unread"), (_D + "db-x.yaml",)),
    "special": ({"_defaults.yaml": _ROOT, "db-ok.yaml": _tenant("db-ok"),
                 "@symlink:ZqX LINK@x.yaml": "nowhere-zqx.yaml",
                 "@fifo:ZqX FIFO@x.yaml": ""},
                ("kind", "tenant_file_unreadable"), ("ZqX LINK@x.yaml", "ZqX FIFO@x.yaml")),
    "platform": ({"_defaults.yaml": _ROOT, "db-ok.yaml": _tenant("db-ok"),
                  "_ZqX PLAT@x.yaml": "_routing_defaults:\n  receiver: 1\n",
                  "ZqX NOTE@x.txt": "hello\n"},
                 ("kind", "missing_receiver_field"), ("_ZqX PLAT@x.yaml",)),
    "dir_null_receiver": ({"_defaults.yaml": _ROOT,
                           _D + "_defaults.yaml": "_routing_defaults:\n  receiver: null\n",
                           _D + "db-x.yaml": _tenant("db-x")},
                          ("kind", "routing_defaults_null_below_root"),
                          (_D + "_defaults.yaml",)),
    "dir_enforced": ({"_defaults.yaml": _ROOT,
                      _D + "_ZqX E@x.yaml": "_routing_enforced:\n  enabled: true\n",
                      _D + "db-x.yaml": _tenant("db-x")},
                     ("kind", "routing_enforced_below_root"), (_D + "_ZqX E@x.yaml",)),
    "dir_unselected_carrier": ({"_defaults.yaml": _ROOT, _D + "_defaults.yaml": "defaults: {}\n",
                                _D + "_defaults.yml": "defaults: {}\n",
                                _D + "db-x.yaml": _tenant("db-x")},
                               ("kind", "unselected_defaults_carrier"),
                               (_D + "_defaults.yml",)),
    "policy_misplaced": ({"_defaults.yaml": _ROOT, "db-ok.yaml": _tenant("db-ok"),
                          "_ZqX POL@x.yaml": "domain_policies:\n  d: {tenants: [db-ok]}\n"},
                         ("kind", "domain_policy_unusable"), ("_ZqX POL@x.yaml",)),
    "policy_scope": ({"_defaults.yaml": _ROOT, "db-ok.yaml": _tenant("db-ok"),
                      _D + "_domain_policy.yaml": "domain_policies:\n  d: {tenants: [db-ok]}\n",
                      _D + "db-x.yaml": _tenant("db-x")},
                     ("kind", "domain_policy_out_of_scope"), (_D + "_domain_policy.yaml",)),
    "policy_unusable": ({"_defaults.yaml": _ROOT, "db-ok.yaml": _tenant("db-ok"),
                         _D + "_domain_policy.yaml": "domain_policies: [1]\n",
                         _D + "db-x.yaml": _tenant("db-x")},
                        ("kind", "domain_policy_unusable"), (_D + "_domain_policy.yaml",)),
    "profile_dup": ({"_defaults.yaml": _ROOT,
                     "_routing_profiles.yaml": f"routing_profiles:\n  p: {_WH}\n",
                     _D + "_routing_profiles.yaml": f"routing_profiles:\n  p: {_WH}\n",
                     _D + "db-x.yaml": _tenant("db-x")},
                    ("kind", "routing_profile_duplicate"), (_D + "_routing_profiles.yaml",)),
    "orphan_platform": ({"_defaults.yaml": _ROOT, "db-ok.yaml": _tenant("db-ok"),
                         "_ZqX PLAT@x.yaml": "tenants:\n  db-zz:\n    cpu_usage_percent: '1'\n"},
                        ("text", "ignored — no tenant file declares tenant 'db-zz'"),
                        ("_ZqX PLAT@x.yaml",)),
    "empty_tenants": ({"_defaults.yaml": _ROOT, "db-ok.yaml": _tenant("db-ok"),
                       "ZqX EMPTY@x.yaml": "tenants:\n"},
                      ("text", "'tenants:' is declared but empty"), ("ZqX EMPTY@x.yaml",)),
    "tenants_list": ({"_defaults.yaml": _ROOT, "db-ok.yaml": _tenant("db-ok"),
                      "ZqX LIST@x.yaml": "tenants: [a]\n"},
                     ("kind", "tenant_file_unreadable"), ("ZqX LIST@x.yaml",)),
    # Also the da-guard reading's ParseFailedError, said again from its
    # structured fields (`_not_read_text`).
    "tenant_not_mapping": ({"_defaults.yaml": _ROOT, "db-ok.yaml": _tenant("db-ok"),
                            "ZqX TNM@x.yaml": "tenants:\n  db-q: 3\n"},
                           ("kind", "key_verdict_incomplete"), ("ZqX TNM@x.yaml",)),
    "invalid_tenant_id": ({"_defaults.yaml": _ROOT,
                           "ZqX ID@x.yaml": "tenants:\n  Bad_ID:\n    cpu_usage_percent: '1'\n"},
                          ("kind", "invalid_tenant_id"), ("ZqX ID@x.yaml",)),
    "subdir_tenants_in_platform": ({"_defaults.yaml": _ROOT, "db-ok.yaml": _tenant("db-ok"),
                                    _D + "_p.yaml":
                                        "tenants:\n  db-ok:\n    cpu_usage_percent: '1'\n"},
                                   ("text", "'tenants:' block ignored"), (_D + "_p.yaml",)),
    "routing_defaults_list": ({"_defaults.yaml": _ROOT, "db-ok.yaml": _tenant("db-ok"),
                               _D + "_defaults.yaml": "_routing_defaults: [1]\n",
                               _D + "db-x.yaml": _tenant("db-x")},
                              ("kind", "routing_defaults_not_mapping"), (_D + "_defaults.yaml",)),
    "dedup_invalid": ({"_defaults.yaml": _ROOT,
                       "ZqX SD@x.yaml": _tenant("db-x", "    _severity_dedup: maybe\n")},
                      ("kind", "severity_dedup_invalid"), ("ZqX SD@x.yaml",)),
    "credential_plain": ({"_defaults.yaml": _ROOT, "ZqX CR@x.yaml": _tenant(
        "db-x", "    _routing:\n      receiver: {type: slack, api_url: "
                "'https://hooks.slack.com/services/T/B/X'}\n")},  # trufflehog:ignore
        ("kind", "credential_plaintext"), ("ZqX CR@x.yaml",)),
    "unread_routing": ({"_defaults.yaml": _ROOT, "db-ok.yaml": _tenant("db-ok"),
                        _D + "_defaults.yaml": f"defaults:\n  _routing: {_WH}\n",
                        _D + "db-x.yaml": _tenant("db-x")},
                       ("kind", "routing_in_unread_location"), (_D + "_defaults.yaml",)),
    # A name that is not valid UTF-8: da-guard refuses the whole tree, and
    # the generator's rc-2 line repeats da-guard's stderr, which quotes the
    # name. Known: see `_KNOWN`.
    "not_utf8_name": ({"_defaults.yaml": _ROOT, "db-ok.yaml": _tenant("db-ok"),
                       "@bytes:ZqX\udcff@x.yaml": _tenant("db-x")},
                      ("text", "cannot read the conf.d through `da-guard parse-tree`"), ()),
}

#: Scenarios also rendered (`--output-alertmanager`): the render path's
#: messages and its written alertmanager.yml carry no canary either.
#: Cells that break the rule today, as xfail(strict=True): the day one is
#: fixed it XPASSes and goes red until its entry is deleted.
_KNOWN = {
    "not_utf8_name": (
        "#3703 open, owner ruling: `parse_tree_failure_exit` repeats da-guard's "
        "stderr, which quotes the file name; tests/shared/test_confd_behaviour_matrix.py "
        "(ADR-036 §7, `refused` row) requires every tool to name that file, so "
        "not printing it breaks that contract"),
}


def _params(names):
    return [pytest.param(n, marks=[pytest.mark.xfail(strict=True, raises=AssertionError,
                                                     reason=_KNOWN[n])] if n in _KNOWN else [])
            for n in names]


_RENDERED = ("unreadable", "duplicate", "invalid_tenant_id", "dir_unselected_carrier", "policy_scope",
             "credential_plain", "tenant_not_mapping", "special", "not_utf8_name")


def _build(d: Path, files: dict[str, str]) -> None:
    for name, body in files.items():
        kind, _, rel = name.rpartition(":") if name.startswith("@") else ("", "", name)
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            if kind == "@symlink":
                os.symlink(body, p)
            elif kind == "@fifo":
                os.mkfifo(p)
            elif kind == "@bytes":
                with open(os.fsencode(p), "w", encoding="utf-8") as fh:
                    fh.write(body)
            else:
                p.write_text(body, encoding="utf-8")
        except (AttributeError, NotImplementedError, OSError) as e:
            # No os.mkfifo (Windows), no symlink privilege, or a filesystem
            # that refuses a name that is not valid UTF-8 (APFS).
            pytest.skip(f"this platform cannot make {kind or 'file'} {rel!r}: {e}")


def _run(tmp_path: Path, files: dict[str, str], *mode: str) -> tuple[list[dict], str]:
    """Write *files* as a conf.d and run the generator in *mode* with
    `--findings-json`: the findings, and every byte of output as one text
    (stdout, stderr, the findings file, a written alertmanager.yml)."""
    d = tmp_path / "conf.d"
    d.mkdir()
    _build(d, files)
    empty = tmp_path / "bin"
    empty.mkdir()
    out = tmp_path / "f.json"
    am = tmp_path / "am.yml"
    extra = ["-o", str(am)] if "--output-alertmanager" in mode else []
    p = subprocess.run([sys.executable, str(_GAR), "--config-dir", str(d), *mode, *extra,
                        "--findings-json", str(out)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=300, env={**os.environ, "PATH": str(empty)})
    assert "Traceback" not in p.stderr, p.stderr
    assert p.returncode in (0, 1, 2), (p.returncode, p.stderr)
    assert out.exists(), p.stderr
    written = out.read_text(encoding="utf-8", errors="replace")
    findings = json.loads(written)["findings"]
    # A tree da-guard refuses whole is the run's caller error (rc 2,
    # `run_failed`): that is what `not_utf8_name` measures; anywhere else a
    # run that judged nothing has no canary to find.
    if "@bytes:" not in "".join(files):
        assert "run_failed" not in {f["kind"] for f in findings}, findings
    rendered = am.read_text(encoding="utf-8", errors="replace") if am.exists() else ""
    return findings, p.stdout + p.stderr + written + rendered


def _judged(findings: list[dict], text: str, proof: tuple[str, str], shows: tuple[str, ...]):
    how, what = proof
    if how == "kind":
        assert what in {f["kind"] for f in findings}, (what, findings)
    else:
        assert what in text, (what, text[-3000:])
    for rel in shows:
        assert shown_file(rel) in text, (rel, shown_file(rel), text[-3000:])


@pytest.mark.parametrize("name", _params(sorted(_SCENARIOS)))
def test_no_filename_canary_under_validate(tmp_path, name):
    files, proof, shows = _SCENARIOS[name]
    findings, text = _run(tmp_path, files, "--validate", "--strict")
    _judged(findings, text, proof, shows)
    assert _CANARY not in text.lower(), text


@pytest.mark.parametrize("name", _params(_RENDERED))
def test_no_filename_canary_under_render(tmp_path, name):
    files, _proof, shows = _SCENARIOS[name]
    _findings, text = _run(tmp_path, files, "--output-alertmanager")
    for rel in shows:
        assert shown_file(rel) in text, (rel, text[-3000:])
    assert _CANARY not in text.lower(), text


_LONG_A, _LONG_B = "a" * 63, "b" * 63


def test_legal_names_are_printed_as_written(tmp_path):
    """`db-x.yaml`, `team-a/db-y.yaml`, and a 63-char tenant id's `<id>.yaml`
    (68 chars) and `<id>.yml`: `file` is the path relative to the conf.d root,
    as written (tenant-api's `Related` compares it with the path it wrote)."""
    files = {"_defaults.yaml": _ROOT, "db-x.yaml": _tenant("db-x", _UNK),
             "team-a/db-y.yaml": _tenant("db-y", _UNK),
             f"{_LONG_A}.yaml": _tenant(_LONG_A, _UNK),
             f"team-a/{_LONG_B}.yml": _tenant(_LONG_B, _UNK)}
    findings, text = _run(tmp_path, files, "--validate", "--strict")
    got = {f["tenant"]: f["file"] for f in findings if f["kind"] == "key_unread"}
    assert got == {"db-x": "db-x.yaml", "db-y": "team-a/db-y.yaml",
                   _LONG_A: f"{_LONG_A}.yaml", _LONG_B: f"team-a/{_LONG_B}.yml"}, findings
    assert "<key:" not in text, text


# ── shown_file itself ───────────────────────────────────────────────────

@pytest.mark.parametrize("path", [
    "db-x.yaml", "team-a/db-y.yaml", f"{'a' * 63}.yaml", f"{'a' * 63}.yml",
    "_defaults.yaml", ".hidden.yaml", "a" * 253, "x/" + "a" * 253,
])
def test_shown_file_prints_a_legal_path_as_written(path):
    assert shown_file(path) == path


@pytest.mark.parametrize("segment", [
    "a" * 254, "..", ".", "..x", "a b", "x@y.yaml", "", "ZqX\udcff.yaml", "é.yaml",
], ids=lambda s: ascii(s)[:24])  # ascii(): a lone surrogate in a test id breaks xdist
def test_shown_file_replaces_an_illegal_segment(segment):
    got = shown_file(f"team-a/{segment}/db-x.yaml")
    # shown_key's stand-in: one format, one digest — also for a segment
    # shown_key itself would print (`..`, 254 chars of a name-like charset).
    digest = hashlib.sha256(segment.encode("utf-8", "surrogatepass")).hexdigest()
    # A bool and ascii(): a failure report holding a lone surrogate (pytest's
    # comparison explanation included) breaks xdist too.
    ok = got == f"team-a/<key:{digest[:6]}>/db-x.yaml"
    assert ok, ascii(got)


def test_shown_file_and_shown_key_share_the_stand_in():
    assert shown_file("a b") == shown_key("a b") != "a b"


@pytest.mark.parametrize("path", [
    None, "db-x.yaml", "ZqX DIR@y/db-x.yaml", "a b/c d/e@f", "..", "a" * 254 + "/x.yaml",
])
def test_shown_file_is_idempotent(path):
    once = shown_file(path)
    assert shown_file(once) == once
    assert once is None or _CANARY not in once.lower()


def test_a_finding_rebuilt_from_a_shown_file_keeps_it():
    """`Finding` puts `file` through `shown_file` in its constructor, and
    `with_text` / a pickle rebuild hand the shown `file` back to it: the
    stand-in survives as it is, not a stand-in of a stand-in."""
    import pickle
    sys.path.insert(0, str(REPO / "scripts" / "tools" / "ops"))
    from _grar_merge import Finding

    f = Finding("x", kind="k", blocks="never", file="ZqX DIR@y/db-x.yaml")
    assert f.file == shown_file("ZqX DIR@y/db-x.yaml") != "ZqX DIR@y/db-x.yaml"
    assert f.with_text("y").file == f.file
    assert pickle.loads(pickle.dumps(f)).file == f.file
    assert Finding("x", kind="k", blocks="never", file=f"{_LONG_A}.yaml").file == f"{_LONG_A}.yaml"
