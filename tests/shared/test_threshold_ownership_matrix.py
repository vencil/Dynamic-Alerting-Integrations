"""「值屬於哪個檔」語意矩陣：threshold-recommend／threshold-govern（#2116）。

同一個值（tenant-a 的 `mysql_connection_usage_percent` = 60）依序放在：根 `defaults:`、平台檔
`tenants:`、租戶檔、子樹 `_defaults.yaml`、profile；外加三個形狀——租戶檔寫舊拼法
`mysql_cpu`（ALIAS）、租戶檔在子目錄（NESTED）、同一租戶一個自有一個繼承（MIXED）。
tenant-b 是對照組：永遠在根目錄自己的檔寫 `mysql_connection_usage_percent: "60"`，每格都要進計畫。

歸屬以 `da-guard effective` 的 key_sources 為準（layer == tenant 才是租戶自有）：測試的
期望值同時由 oracle 推出、也寫死，避免兩邊一起錯。

Prometheus 以 stub 取代：每條觀測序列回 2000 個 40.0（現值 60 → 建議 40，#2964）。跑的是
真的 `threshold_govern.run()`；它只讀（ADR-027 §4 移除了經 tenant-api 的寫入路徑），所以
這裡驗的是計畫：只含租戶自有的 key，並指出要改的租戶檔。

da-guard 由 conftest 的 session fixture 以 `go build` 建出；建不起來就 fail、不 skip。
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pytest
import yaml

import _lib_tenant_values as tv
import threshold_govern as tg
import threshold_recommend as tr

pytestmark = pytest.mark.usefixtures("da_guard_env")

# The root declares every key a tenant overrides: an undeclared key is not
# served at all (#1976), so a realistic tree declares it.
_BASE = "defaults:\n  mysql_connection_usage_percent: 80\n  mysql_threads_running: 80\n"
_B = 'tenants:\n  tenant-b:\n    mysql_connection_usage_percent: "60"\n'           # control
_A_PLAIN = 'tenants:\n  tenant-a:\n    foo_unmapped_key: "3"\n'
_A_OWN = 'tenants:\n  tenant-a:\n    mysql_connection_usage_percent: "60"\n'

# where -> (files, expected tenant-a owned keys, expected inherited
#           mysql_connection_usage_percent source (layer, file) or None, the file tenant-a's
#           plan names, or None when tenant-a has no plan)
MATRIX = {
    "defaults": ({"_defaults.yaml": "defaults:\n  mysql_connection_usage_percent: 60\n  mysql_threads_running: 80\n",
                  "tenant-a.yaml": _A_PLAIN, "tenant-b.yaml": _B},
                 set(), ("defaults", "_defaults.yaml"), None),
    "platform-tenants": ({"_defaults.yaml": _BASE,
                          "_platform.yaml": "tenants:\n  tenant-a:\n    mysql_connection_usage_percent: 60\n",
                          "tenant-a.yaml": _A_PLAIN, "tenant-b.yaml": _B},
                         set(), ("platform", "_platform.yaml"), None),
    "tenant-file": ({"_defaults.yaml": _BASE, "tenant-a.yaml": _A_OWN, "tenant-b.yaml": _B},
                    {"mysql_connection_usage_percent"}, None, "tenant-a.yaml"),
    "subtree": ({"_defaults.yaml": _BASE,
                 "team/_defaults.yaml": "defaults:\n  mysql_connection_usage_percent: 60\n",
                 "team/tenant-a.yaml": _A_PLAIN, "tenant-b.yaml": _B},
                set(), ("defaults", "team/_defaults.yaml"), None),
    "profile": ({"_defaults.yaml": _BASE,
                 "_profiles.yaml": "profiles:\n  p1:\n    mysql_connection_usage_percent: 60\n",
                 "tenant-a.yaml": 'tenants:\n  tenant-a:\n    _profile: p1\n',
                 "tenant-b.yaml": _B},
                set(), ("profile", "_profiles.yaml"), None),
    "alias": ({"_defaults.yaml": _BASE,
               "tenant-a.yaml": 'tenants:\n  tenant-a:\n    mysql_cpu: "60"\n',
               "tenant-b.yaml": _B},
              {"mysql_cpu"}, ("defaults", "_defaults.yaml"), "tenant-a.yaml"),
    "nested": ({"_defaults.yaml": _BASE, "team/tenant-a.yaml": _A_OWN, "tenant-b.yaml": _B},
               {"mysql_connection_usage_percent"}, None, "team/tenant-a.yaml"),
    "mixed": ({"_defaults.yaml": "defaults:\n  mysql_connection_usage_percent: 60\n  mysql_threads_running: 80\n",
               "tenant-a.yaml": 'tenants:\n  tenant-a:\n    mysql_threads_running: "60"\n',
               "tenant-b.yaml": _B},
              {"mysql_threads_running"}, ("defaults", "_defaults.yaml"), "tenant-a.yaml"),
}


def _tree(root: Path, files: dict[str, str]) -> Path:
    conf_d = root / "conf.d"
    for rel, body in files.items():
        p = conf_d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    return conf_d


@pytest.fixture
def stubs(monkeypatch):
    # 2000 points a minute apart: ~33 distinct hours, above the #2941 floor of 24.
    monkeypatch.setattr(tr, "query_prometheus_range_ts",
                        lambda url, promql, *a, **k: [
                            (1_700_000_000.0 + 60 * i, 40.0) for i in range(2000)])


def _args(conf_d: Path) -> argparse.Namespace:
    return argparse.Namespace(
        config_dir=str(conf_d), prometheus="http://prom.stub", tenant=None, lookback="7d",
        min_samples=100, min_delta_pct=25.0)


def _owned_by_oracle(conf_d: Path, tenant: str) -> set[str]:
    eff = tv.load_effective(conf_d)[tenant]
    return {k for k, ks in eff.key_sources.items()
            if ks.layer == "tenant" and not tr.is_reserved_key(k)}


@pytest.mark.parametrize("where", sorted(MATRIX))
def test_recommend_splits_own_from_inherited(where, tmp_path, stubs):
    files, own, inherited_src, _ = MATRIX[where]
    conf_d = _tree(tmp_path, files)
    reports = {r.tenant: r for r in tr.run_analysis(str(conf_d), prometheus_url="http://prom.stub")}
    a = reports["tenant-a"]
    filler = {"foo_unmapped_key"}  # tenant-own, no observed-map entry: listed, skipped
    assert {r.key for r in a.keys} - filler == own == _owned_by_oracle(conf_d, "tenant-a") - filler, where
    by_key = {r.key: r for r in a.inherited}
    if inherited_src is not None:
        r = by_key["mysql_connection_usage_percent"]
        assert (r.source_layer, r.source_file) == inherited_src, where
        # listed only: not queried, no recommended value (#2116)
        assert (r.recommended, r.promql) == (None, ""), where
    assert all(r.source_layer == "tenant" for r in a.keys), where
    # control
    b = reports["tenant-b"]
    assert [(r.key, r.source_layer, r.source_file) for r in b.keys] == [
        ("mysql_connection_usage_percent", "tenant", "tenant-b.yaml")], where


@pytest.mark.parametrize("where", sorted(MATRIX))
def test_govern_plans_own_keys_only(where, tmp_path, stubs):
    files, own, _, a_file = MATRIX[where]
    conf_d = _tree(tmp_path, files)
    res = tg.run(_args(conf_d))
    by_tenant = {p.tenant: p for p in res.plans}

    # control: tenant-b's own key is planned, in every position.
    b = by_tenant["tenant-b"]
    assert ([c.key for c in b.changes], b.source_file) == (["mysql_connection_usage_percent"], "tenant-b.yaml")
    if a_file is None:
        assert "tenant-a" not in by_tenant, where
    else:
        a = by_tenant["tenant-a"]
        # the spelling the file writes, and the file to change (a nested one too)
        assert {c.key for c in a.changes} == own, where
        assert a.source_file == a_file, where
        assert all(c.new_value == '"40"' for c in a.changes), where
    inherited = {(k.tenant, k.key) for k in res.inherited}
    assert not (inherited & {(p.tenant, c.key) for p in res.plans for c in p.changes}), where


def test_inherited_keys_never_enter_the_plan(tmp_path, stubs):
    """Tenants that inherit their only mapped key get no plan; tenants whose
    own file sits in a sub-directory are planned with that file (#2116)."""
    n = 7
    files = {"_defaults.yaml": "defaults:\n  mysql_connection_usage_percent: 60\n  mysql_threads_running: 80\n",
             "_platform.yaml": "tenants:\n" + "".join(
                 f"  p{i}:\n    mysql_threads_running: 60\n" for i in range(n)),
             "zz-control.yaml": 'tenants:\n  zz-control:\n    mysql_connection_usage_percent: "60"\n'}
    for i in range(n):
        files[f"d{i}.yaml"] = f'tenants:\n  d{i}:\n    foo_unmapped_key: "3"\n'
        files[f"p{i}.yaml"] = f'tenants:\n  p{i}:\n    foo_unmapped_key: "3"\n'
        files[f"team/n{i}.yaml"] = f'tenants:\n  n{i}:\n    mysql_connection_usage_percent: "60"\n'
    conf_d = _tree(tmp_path, files)
    res = tg.run(_args(conf_d))
    assert {(p.tenant, p.source_file) for p in res.plans} == (
        {(f"n{i}", f"team/n{i}.yaml") for i in range(n)} | {("zz-control", "zz-control.yaml")})
    assert len(res.inherited) >= 2 * n


def test_export_patch_never_writes_an_inherited_value(tmp_path, stubs):
    conf_d = _tree(tmp_path, MATRIX["mixed"][0])
    patch = tr.format_export_patch(
        tr.run_analysis(str(conf_d), prometheus_url="http://prom.stub", tenant_filter="tenant-a"))
    doc = yaml.safe_load(patch)
    assert set(doc["tenants"]["tenant-a"]) == {"mysql_threads_running"}
    own = next(ln for ln in patch.splitlines() if ln.strip().startswith("mysql_threads_running:"))
    assert "# was 60 (from tenant-a.yaml)" in own
    inh = [ln for ln in patch.splitlines() if "(inherited) mysql_connection_usage_percent" in ln]
    assert len(inh) == 1 and inh[0].lstrip().startswith("#")
    assert "mysql_connection_usage_percent: inherited, is 60 (from _defaults.yaml)" in inh[0]
    assert '"' not in inh[0], "an inherited key carries no recommended value"



# ── export-patch: an inherited line can never become a value line ────────
_OWN_60 = 'tenants:\n  t:\n    mysql_connection_usage_percent: "60"\n'


def _patch_of(conf_d: Path) -> tuple[str, dict]:
    patch = tr.format_export_patch(
        tr.run_analysis(str(conf_d), prometheus_url="http://prom.stub", tenant_filter="t"))
    return patch, yaml.safe_load(patch)


@pytest.mark.parametrize("files", [
    # a key holding a newline (F1): written raw, it splits the comment into a value line
    {"_defaults.yaml": 'defaults:\n  mysql_connection_usage_percent: 80\n'
                       '  "zz\\n    mysql_connection_usage_percent: \\"1\\" #": 5\n',
     "t.yaml": _OWN_60},
    # a value holding a newline (F2)
    {"_defaults.yaml": "defaults:\n  mysql_connection_usage_percent: 80\n  mysql_threads_running: 80\n",
     "_platform.yaml": 'tenants:\n  t:\n    mysql_threads_running: "80\\n    mysql_threads_running: 1 #"\n',
     "t.yaml": _OWN_60},
], ids=["newline-in-key", "newline-in-value"])
def test_an_inherited_comment_cannot_become_a_value_line(files, tmp_path, stubs):
    conf_d = _tree(tmp_path, files)
    patch, doc = _patch_of(conf_d)
    # only the tenant's own key, with its recommended value (control: 40)
    assert doc["tenants"]["t"] == {"mysql_connection_usage_percent": "40"}, patch
    values = [ln for ln in patch.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    assert len(values) == 3 and values[:2] == ["tenants:", "  t:"], patch
    assert values[2].lstrip().startswith('mysql_connection_usage_percent: "40"'), patch


def test_inherited_reserved_keys_are_not_listed(tmp_path, stubs):
    """F9: a `_` key a tenant inherits is not a threshold — `_policies` from a
    root `_defaults.yaml` without `defaults:` (merged whole) and a non-reserved
    `_foo` from a platform `tenants:` block."""
    conf_d = _tree(tmp_path, {
        "_defaults.yaml": "_policies:\n  - name: x\nmysql_connection_usage_percent: 80\n",
        "_platform.yaml": "tenants:\n  t:\n    _foo: 3\n",
        "t.yaml": _OWN_60})
    eff = tv.load_effective(conf_d)["t"]
    assert {"_policies", "_foo"} <= set(eff.effective_config)  # premise: Go carries them
    [rep] = tr.run_analysis(str(conf_d), prometheus_url="http://prom.stub")
    assert [r.key for r in rep.inherited] == [], [r.key for r in rep.inherited]
    assert [r.key for r in rep.keys] == ["mysql_connection_usage_percent"]


def test_an_inherited_key_reports_no_confidence(tmp_path, stubs):
    """F8: not queried is not "too few samples" — no LOW, in text or JSON."""
    conf_d = _tree(tmp_path, MATRIX["mixed"][0])
    reports = tr.run_analysis(str(conf_d), prometheus_url="http://prom.stub", tenant_filter="tenant-a")
    [inh] = tr.json_document(reports)["tenants"][0]["inherited"]
    assert (inh["key"], inh["confidence"]) == ("mysql_connection_usage_percent", tr.CONFIDENCE_NOT_QUERIED)
    row = next(ln for ln in tr.format_text_report(reports).splitlines()
               if ln.strip().startswith("mysql_connection_usage_percent"))
    assert "LOW" not in row and tr.CONFIDENCE_NOT_QUERIED in row, row


# ── govern: nested tenants in the summary ─────────────────────────────────
def _nested_first_tree(tmp_path: Path, n_nested: int, n_root: int) -> Path:
    files = {"_defaults.yaml": _BASE}
    for i in range(n_nested):  # "a…" sorts before "z…"
        files[f"team/a{i}.yaml"] = f'tenants:\n  a{i}:\n    mysql_connection_usage_percent: "60"\n'
    for i in range(n_root):
        files[f"z{i}.yaml"] = f'tenants:\n  z{i}:\n    mysql_connection_usage_percent: "60"\n'
    return _tree(tmp_path, files)


def test_json_summary_counts_nested_as_the_text_does(tmp_path, stubs):
    """F7: nothing is written, so a tenant whose file is in a sub-directory is
    as actionable as any other — JSON and text count it alike."""
    conf_d = _nested_first_tree(tmp_path, n_nested=2, n_root=1)
    res = tg.run(_args(conf_d))
    summary = tg.json_document(res.plans)["summary"]
    assert (summary["tenants_actionable"], summary["changes"]) == (3, 3)
    assert "skipped_nested" not in summary
    text = tg.format_text_report(res.plans).splitlines()
    assert text[-1].startswith("Summary: 3 tenant(s) / 3 change(s) need retuning."), text[-1]
    assert any(ln.startswith("Tenant: a0 (1 change(s)) — team/a0.yaml") for ln in text), text


# ── export-patch: a name that is not a plain YAML name gets no value line ──
# The line breaks `str.splitlines` (and YAML) honour that a careless join keeps,
# a non-BMP character (a JSON-quoted key spells it as a surrogate pair, which
# the exporter's yaml.v3 refuses) and `null`, the one plain word the exporter
# does not read back as written.
_INJECT = '{brk}  z:{brk}    mysql_connection_usage_percent: "1" #'
_ODD = {"lf": "zz" + _INJECT.format(brk="\n"),
        "u2028": "zz" + _INJECT.format(brk="\u2028"),
        "nel": "zz" + _INJECT.format(brk="\x85"),
        "emoji": "x\U0001F600", "null": "null"}


def _q(name: str) -> str:
    """`name` as a YAML double-quoted key the exporter reads back as is:
    printable ASCII as is, everything else as a backslash-u / backslash-U
    escape (never a surrogate pair)."""
    out = []
    for c in name:
        o = ord(c)
        out.append(c if 0x20 <= o < 0x7F and c not in '"\\'
                   else f"\\u{o:04x}" if o < 0x10000 else f"\\U{o:08x}")
    return '"' + "".join(out) + '"'


@pytest.mark.parametrize("odd", sorted(_ODD))
@pytest.mark.parametrize("where", ["tenant-name-comment", "tenant-name-block", "own-skipped-key"])
def test_no_name_or_key_breaks_out_of_the_patch(where, odd, tmp_path, stubs):
    """A tenant name or tenant-own key that is not a plain YAML name never
    reaches a value line: the patch says so in a comment, still parses, and
    keeps the control tenant `z`'s recommendation (40) — read back both by
    PyYAML and, as a tenant file, by the exporter's own load (da-guard)."""
    name = _ODD[odd]
    files = {"_defaults.yaml": _BASE,
             "z.yaml": 'tenants:\n  z:\n    mysql_connection_usage_percent: "60"\n'}
    if where == "tenant-name-comment":   # tenant with no exportable key: comment lines only
        files["a.yaml"] = f'tenants:\n  {_q(name)}:\n    foo_unmapped_key: "3"\n'
    elif where == "tenant-name-block":   # tenant with an exportable key
        files["a.yaml"] = f'tenants:\n  {_q(name)}:\n    mysql_connection_usage_percent: "60"\n'
    else:                                # tenant-own unmapped key, in z's own block
        files["z.yaml"] += f'    {_q(name)}: "3"\n'
    conf_d = _tree(tmp_path, files)
    tenants = set(tv.load_effective(conf_d))
    assert tenants == ({"z"} if where == "own-skipped-key" else {"z", name})  # premise
    patch = tr.format_export_patch(tr.run_analysis(str(conf_d), prometheus_url="http://prom.stub"))

    assert yaml.safe_load(patch) == {"tenants": {"z": {"mysql_connection_usage_percent": "40"}}}, patch
    values = [ln for ln in patch.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    assert values[:2] == ["tenants:", "  z:"] and len(values) == 3, patch
    assert values[2].startswith('    mysql_connection_usage_percent: "40"'), patch
    if where == "tenant-name-block":
        said = [ln for ln in patch.splitlines() if "(not written) mysql_connection_usage_percent" in ln]
        assert len(said) == 1 and said[0].startswith("# [") and "recommended 40" in said[0], patch
        assert "the tenant name is not a plain YAML name" in said[0], patch
    elif where == "tenant-name-comment":
        said = [ln for ln in patch.splitlines() if "(skipped) foo_unmapped_key" in ln]
        assert len(said) == 1 and said[0].startswith("# ["), patch
    else:
        said = [ln for ln in patch.splitlines() if ln.startswith("    # (skipped) ")]
        assert len(said) == 1, patch

    # merged as a tenant file, the patch decodes for the exporter's own load
    back = _tree(tmp_path / "back", {"_defaults.yaml": _BASE, "z.yaml": patch})
    eff = tv.load_effective(back)
    assert set(eff) == {"z"}, patch
    assert eff["z"].effective_config["mysql_connection_usage_percent"] == "40", eff["z"]


def test_a_key_that_is_not_a_plain_name_is_not_written():
    """The key-side gate (an exportable key is a mapped one, so a tree cannot
    reach it today): a plain tenant keeps its plain key's value line and gets a
    `(not written)` comment for the other."""
    def key(k):
        return tr.KeyRecommendation(key=k, current_value="60", recommended=20.0,
                                    delta_pct=-66.7, confidence="HIGH", reason="r")
    patch = tr.format_export_patch([tr.TenantRecommendation(
        tenant="t", keys=[key("mysql_connection_usage_percent"), key("null"), key("a\nb")])])
    assert yaml.safe_load(patch) == {"tenants": {"t": {"mysql_connection_usage_percent": "20"}}}, patch
    said = [ln for ln in patch.splitlines() if "(not written)" in ln]
    assert [ln.split(":")[0] for ln in said] == ["    # (not written) a?b",
                                                "    # (not written) null"], patch
    assert all("the key is not a plain YAML name" in ln for ln in said), patch


@pytest.mark.parametrize("name", ["db-a", "tenant-a", "db.prod-1", "a1", "redis_0", "e5x",
                                  "010", "123", "2024", "10-20", "true", "yes", "2024-01-01"])
def test_ordinary_names_are_plain(name):
    assert tr._plain_name(name)


@pytest.mark.parametrize("name", [
    "null", "Null", "NULL", "~", "-1", ".inf", ".nan", "", "a b", "é", "x\U0001F600", "a\nb"])
def test_names_the_exporter_reads_otherwise_are_not_plain(name):
    assert not tr._plain_name(name)


@pytest.mark.parametrize("tid", ["010", "2024", "10-20", "true", "2024-01-01"])
def test_a_numeric_or_boolean_looking_tenant_id_keeps_its_value_line(tid, tmp_path, stubs):
    """Tenant ids such as store numbers or years are legal and the exporter
    keys them by their text: the patch writes their value line bare, and that
    line, merged as a tenant file, is read back by da-guard as the same id."""
    conf_d = _tree(tmp_path, {"_defaults.yaml": _BASE,
                              "a.yaml": f'tenants:\n  {_q(tid)}:\n    mysql_connection_usage_percent: "60"\n'})
    assert set(tv.load_effective(conf_d)) == {tid}  # premise
    patch = tr.format_export_patch(tr.run_analysis(str(conf_d), prometheus_url="http://prom.stub"))
    values = [ln for ln in patch.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    assert values[:2] == ["tenants:", f"  {tid}:"], patch
    assert values[2].startswith('    mysql_connection_usage_percent: "40"'), patch
    back = tv.load_effective(_tree(tmp_path / "back", {"_defaults.yaml": _BASE, "a.yaml": patch}))
    assert set(back) == {tid}, patch
    assert back[tid].effective_config["mysql_connection_usage_percent"] == "40", patch
