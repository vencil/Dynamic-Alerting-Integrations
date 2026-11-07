"""generate_rulepack_derived — the `maintenance` label decides the maintenance clause (#3309).

The label is the one written decision; these tests pin that the clause follows
it in every direction a hand edit could break it, that the rewrite is the
canonical form and idempotent, and that the canonical tail is spelled the way
the Go custom-alert compiler spells it.
"""
from __future__ import annotations

import importlib.util
import re
import shutil
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "generate_rulepack_derived", _REPO / "scripts/tools/dx/generate_rulepack_derived.py")
grd = importlib.util.module_from_spec(_SPEC)
sys.modules["generate_rulepack_derived"] = grd
_SPEC.loader.exec_module(grd)

TAIL = grd.MAINTENANCE_TAIL


# ── derive_expr ──────────────────────────────────────────────────────────────

def test_wrapped_clause_becomes_the_canonical_tail():
    wrapped = (
        "(\n"
        "  (\n"
        "    a > on(tenant) group_left b\n"
        "  )\n"
        "  unless on(tenant)\n"
        '  (user_state_filter{filter="maintenance"} == 1)\n'
        ")"
    )
    assert grd.derive_expr(wrapped, mute=True) == "(\n  a > on(tenant) group_left b\n)\n" + TAIL


def test_canonical_form_is_a_fixed_point():
    expr = "x > 1\n" + TAIL
    assert grd.derive_expr(expr, mute=True) == expr
    assert grd.derive_expr(grd.derive_expr(expr, mute=True), mute=True) == expr


def test_page_strips_the_clause():
    assert grd.derive_expr("x > 1\n" + TAIL, mute=False) == "x > 1"


def test_mute_adds_the_clause_to_a_bare_expr():
    assert grd.derive_expr("pg_up == 0", mute=True) == "pg_up == 0\n" + TAIL


def test_top_level_or_is_parenthesised_before_the_tail():
    # `A or B unless M` parses as `A or (B unless M)`: only B would be muted.
    got = grd.derive_expr("a > 1 or b > 1", mute=True)
    assert got == "(\n  a > 1 or b > 1\n)\n" + TAIL


def test_or_inside_parens_or_label_values_is_not_top_level():
    assert not grd._top_level_or('(a or b) > 1')
    assert not grd._top_level_or('x{job="or"} > 1')
    assert not grd._top_level_or('x_or_y > 1')
    assert grd._top_level_or('a or b')


def test_two_clauses_are_refused():
    with pytest.raises(grd.ShapeError):
        grd.derive_expr("(a\n" + TAIL + ")\nor\n(b\n" + TAIL + ")", mute=True)


@pytest.mark.parametrize("variant", [
    'x unless on(tenant) (user_state_filter{filter="maintenance"} == bool 1)',
    'x unless on(tenant) (user_state_filter{filter=~"maintenance"} == 1)',
    'x and on(tenant) (user_state_filter{filter="maintenance"} == 0)',
])
def test_non_canonical_maintenance_references_are_refused(variant):
    with pytest.raises(grd.ShapeError):
        grd.derive_expr(variant, mute=True)


def test_a_clause_in_the_middle_is_refused():
    with pytest.raises(grd.ShapeError):
        grd.derive_expr("(x " + TAIL.replace("\n", " ") + ") > on(tenant) y", mute=True)


def test_other_user_state_filters_are_left_alone():
    expr = 'x * on(tenant) group_left max by(tenant) (user_state_filter{filter="container_crashloop"})'
    assert grd.derive_expr(expr, mute=False) == expr


def test_a_commented_out_clause_does_not_count():
    expr = "x > 1\n# unless on(tenant) (user_state_filter{filter=\"maintenance\"} == 1)"
    assert grd.clause_count(expr) == 0


# ── sync_text ────────────────────────────────────────────────────────────────

_PACK = """groups:
  - name: g
    rules:
      - record: r:core
        expr: |
          y > 1
          unless on(tenant)
          (user_state_filter{filter="maintenance"} == 1)
      - alert: A
        expr: x > 1
        for: 1m
        labels:
          severity: warning
          maintenance: @A@
      - alert: B
        expr: |
          (
            (z > 1)
            unless on(tenant) (user_state_filter{filter="maintenance"} == 1)
          )
        labels:
          severity: warning
          maintenance: @B@
"""


def test_sync_rewrites_every_item_to_its_label():
    out, problems = grd.sync_text(_PACK.replace("@A@", "mute").replace("@B@", "page"), "p.yaml")
    assert problems == []
    assert "      - alert: A\n        expr: |\n          x > 1\n          unless on(tenant)\n" in out
    assert "        expr: |\n          (z > 1)\n        labels:" in out
    # a record never carries the clause
    assert "      - record: r:core\n        expr: |\n          y > 1\n      - alert: A" in out
    again, problems = grd.sync_text(out, "p.yaml")
    assert again == out and problems == []


def test_sync_reports_a_missing_or_unknown_label():
    pack = _PACK.replace("@A@", "mute").replace("@B@", "sometimes").replace("          maintenance: mute\n", "")
    _, problems = grd.sync_text(pack, "p.yaml")
    assert any("alert A: missing label" in p for p in problems)
    assert any("alert B: maintenance='sometimes'" in p for p in problems)


def test_every_shipped_pack_is_already_a_fixed_point():
    """The committed packs are what the generator writes (the --check contract)."""
    packs = [p for p in grd._lib_consumer_read.tenant_rule_pack_files(_REPO / "rule-packs")
             if grd._pack(p) != grd.CUSTOM_ALERTS_PACK]
    assert len(packs) >= 10
    for p in packs:
        text = p.read_text(encoding="utf-8")
        out, problems = grd.sync_text(text, p.name)
        assert problems == [], problems
        assert out == text, f"{p.name} is stale: run scripts/tools/dx/generate_rulepack_derived.py"


# ── semantic check (alert + the records it reads) ────────────────────────────

class _RF:
    def __init__(self, rules):
        self.path = "x.yaml"
        self.documents = [{"groups": [{"name": "g", "rules": rules}]}]


def _alert(name, expr, value):
    return {"alert": name, "expr": expr, "labels": {"severity": "warning", "maintenance": value}}


def test_semantic_check_follows_recording_rules():
    rules = [{"record": "r:core", "expr": "y > 1\n" + TAIL},
             _alert("ViaCore", "r:core", "mute"),
             _alert("PageButMuted", "r:core", "page"),
             _alert("MuteButBare", "y > 1", "mute"),
             _alert("Unlabelled", "y > 1", None)]
    problems = grd.semantic_problems([_RF(rules)])
    assert not any("ViaCore" in p for p in problems)
    assert any("PageButMuted" in p and "hold 1" in p for p in problems)
    assert any("MuteButBare" in p and "hold 0" in p for p in problems)
    assert any("Unlabelled" in p for p in problems)


def test_semantic_check_catches_a_double_clause():
    rules = [{"record": "r:core", "expr": "y > 1\n" + TAIL},
             _alert("Twice", "r:core\n" + TAIL, "mute")]
    assert any("Twice" in p and "hold 2" in p for p in grd.semantic_problems([_RF(rules)]))


# ── one spelling ─────────────────────────────────────────────────────────────

def test_tail_is_the_go_compilers_maintenance_tail():
    src = (_REPO / "components/threshold-exporter/app/pkg/config/custom_alert_compile.go"
           ).read_text(encoding="utf-8")
    m = re.search(r'const maintenanceTail = "((?:[^"\\]|\\.)*)"', src)
    assert m, "maintenanceTail const not found"
    assert m.group(1).encode().decode("unicode_escape") == TAIL
    for value, const in ((grd.MUTE, "maintenanceMute"), (grd.PAGE, "maintenancePage")):
        assert re.search(rf'{const}\s*=\s*"{value}"', src), const


# ── end to end, through da-read ──────────────────────────────────────────────

def test_check_passes_on_the_repo(da_read_env):
    assert grd.main(["--check"]) == 0


def test_check_fails_on_a_hand_edited_pack(da_read_env, tmp_path):
    rp = tmp_path / "rule-packs"
    shutil.copytree(_REPO / "rule-packs", rp)
    p = rp / "rule-pack-postgresql.yaml"
    text = p.read_text(encoding="utf-8")
    # A hand edit drops the clause of one mute alert.
    head, sep, rest = text.partition("      - alert: PostgreSQLRecentRestart\n")
    rest = rest.replace("          unless on(tenant)\n          (user_state_filter{filter=\"maintenance\"} == 1)\n", "", 1)
    p.write_text(head + sep + rest, encoding="utf-8")
    assert grd.main(["--check", "--rule-packs-dir", str(rp)]) == 1
    # The generator puts it back; the check passes again.
    assert grd.main(["--rule-packs-dir", str(rp)]) == 0
    assert grd.main(["--check", "--rule-packs-dir", str(rp)]) == 0
    assert p.read_text(encoding="utf-8") == text


# ── shapes the text model must refuse rather than mis-rewrite (review F2/F3) ──

_ONE = """groups:
  - name: g
    rules:
      - alert: A
        expr: @EXPR@
        labels:
          severity: info
          maintenance: mute
"""


def test_a_multi_line_plain_scalar_is_refused_not_spliced():
    pack = _ONE.replace("@EXPR@", "(time() - x)\n          < 300")
    with pytest.raises(grd.ShapeError, match="multi-line plain scalar"):
        grd.sync_text(pack, "p.yaml")


@pytest.mark.parametrize("indicator", ["|2", ">-", ">"])
def test_other_block_scalar_styles_are_refused(indicator):
    pack = _ONE.replace("@EXPR@", indicator + "\n          x > 1")
    with pytest.raises(grd.ShapeError):
        grd.sync_text(pack, "p.yaml")


def test_inline_labels_are_refused_with_a_reason():
    pack = _ONE.replace("        labels:\n          severity: info\n          maintenance: mute\n",
                        "        labels: {severity: info, maintenance: mute}\n").replace("@EXPR@", "x > 1")
    with pytest.raises(grd.ShapeError, match="labels written inline"):
        grd.sync_text(pack, "p.yaml")


def test_only_a_label_key_counts_not_text_inside_a_label_value():
    pack = (_ONE.replace("@EXPR@", "x > 1")
            .replace("          maintenance: mute\n",
                     "          note: |\n            maintenance: page\n          maintenance: mute\n"))
    items = grd.parse_items(pack.split("\n"))
    assert items[0].label == "mute"


def test_a_near_miss_filter_value_is_refused():
    with pytest.raises(grd.ShapeError):
        grd.clause_count('x unless on(tenant) (user_state_filter{filter=~"mainten.+"} == 1)')


# ── check 4: selectors that reach the maintenance series (Prometheus' reading) ──

#: Spellings that select the maintenance series without the canonical clause:
#: the first from the review that retired check_maintenance_symmetry (#1200),
#: the rest from the review of a text check that replaced it — each passed
#: every other hook, and in a `page` alert muted it in a maintenance window
#: (promtool-verified by the reviewer).
_STRAY_SELECTORS = [
    'user_state_filter{filter=~"main.*"}',
    'user_state_filter{filter!="container_crashloop"}',
    r'user_state_filter{filter="mai\x6etenance"}',
    '{__name__=~"user_state_.*", filter=~"main.*"}',
    '{__name__=~"user_state_.*", filter=`maintenance`}',
    "{__name__=~\"user_state_.*\", filter='maintenance'}",
    '{filter="maintenance"}',
    'user_state_filter',
]


def _page_alert_muted_by(tmp_path, selector: str) -> Path:
    rp = tmp_path / "rule-packs"
    shutil.copytree(_REPO / "rule-packs", rp)
    p = rp / "rule-pack-redis.yaml"
    text = p.read_text(encoding="utf-8")
    old = '      - alert: RedisDown\n        expr: redis_up == 0\n'
    assert old in text, "fixture anchor moved: RedisDown's expr"
    p.write_text(text.replace(old, (
        '      - alert: RedisDown\n        expr: |\n'
        '          redis_up == 0\n'
        f'          unless on(tenant) ({selector} == 1)\n'), 1), encoding="utf-8")
    return rp


@pytest.mark.parametrize("selector", _STRAY_SELECTORS)
def test_a_page_alert_muted_by_any_spelling_fails_the_check(da_read_env, tmp_path, capsys, selector):
    rp = _page_alert_muted_by(tmp_path, selector)
    assert grd.main(["--check", "--rule-packs-dir", str(rp)]) == 1
    assert "could select the maintenance series outside the maintenance clause" in capsys.readouterr().err


def test_selecting_another_filter_by_name_passes(da_read_env, tmp_path):
    """Negative control: the same edit with a filter that is not maintenance."""
    rp = _page_alert_muted_by(tmp_path, 'user_state_filter{filter="container_crashloop"}')
    assert grd.main(["--check", "--rule-packs-dir", str(rp)]) == 0


def test_stray_selector_offsets_are_bytes_mapped_to_characters():
    """da-read reports byte offsets; a non-ASCII comment before the clause must
    not shift the containment test."""
    expr = "x # 註解\n" + TAIL
    start = expr.encode("utf-8").index(b"user_state_filter")
    hit = {"selector": grd.MAINTENANCE_SERIES, "start": start,
           "end": start + len(grd.MAINTENANCE_SERIES)}
    assert grd.stray_selector_problems([("p", "alert A", expr)], lambda exprs: [[hit]]) == []
    outside = "user_state_filter{filter=\"maintenance\"} > 0\n" + TAIL
    hit2 = {"selector": grd.MAINTENANCE_SERIES, "start": 0, "end": len(grd.MAINTENANCE_SERIES)}
    assert len(grd.stray_selector_problems([("p", "alert B", outside)], lambda exprs: [[hit2]])) == 1


# ── C-gen: tenant metadata annotations (#2566 / #1200 D-C) ────────────────────

META = grd.METADATA_ANNOTATIONS


def _meta_pack(labels: str, annotations: str | None) -> str:
    text = ("groups:\n- name: g\n  rules:\n  - alert: A\n    expr: up == 0\n"
            f"    labels:\n{labels}")
    if annotations is not None:
        text += f"    annotations:\n{annotations}"
    return text + "  - alert: B\n    expr: up == 1\n    labels:\n      maintenance: page\n"


_TENANT = '      maintenance: page\n      tenant: "{{ $labels.tenant }}"\n'


def _meta_lines(indent: int = 6, **override) -> str:
    vals = {**META, **override}
    return "".join(f'{" " * indent}{k}: "{v}"\n' for k, v in vals.items())


def _annotations_of(text: str, alert: str = "A") -> dict:
    import yaml
    for r in yaml.safe_load(text)["groups"][0]["rules"]:
        if r.get("alert") == alert:
            return r.get("annotations") or {}
    raise AssertionError(alert)


def test_a_tenant_alert_gets_the_three_annotations():
    new, problems = grd.sync_text(_meta_pack(_TENANT, '      summary: "x"\n'), "p")
    assert problems == []
    ann = _annotations_of(new)
    assert {k: ann[k] for k in META} == META and ann["summary"] == "x"


def test_an_alert_without_annotations_gets_a_block():
    new, _ = grd.sync_text(_meta_pack(_TENANT, None), "p")
    assert _annotations_of(new) == META
    assert "maintenance: page" in new.split("- alert: B")[1]  # the next item is untouched


def test_a_hand_edited_template_is_put_back():
    text = _meta_pack(_TENANT, '      summary: "x"\n' + _meta_lines(owner="team-x"))
    new, _ = grd.sync_text(text, "p")
    assert _annotations_of(new)["owner"] == META["owner"]


@pytest.mark.parametrize("labels", [
    "      maintenance: page\n",                                                  # no tenant label
    '      maintenance: page\n      tenant: "{{ $labels.tenant }}"\n      alert_source: platform\n',
])
def test_an_alert_that_shows_no_tenant_loses_them(labels):
    new, _ = grd.sync_text(_meta_pack(labels, '      summary: "x"\n' + _meta_lines()), "p")
    assert set(_annotations_of(new)) == {"summary"}


def test_metadata_sync_is_a_fixed_point_and_leaves_right_lines_in_place():
    text = _meta_pack(_TENANT, '      summary: "x"\n\n' + _meta_lines() + '      other: "y"\n')
    new, _ = grd.sync_text(text, "p")
    assert new == text
    again, _ = grd.sync_text(grd.sync_text(_meta_pack(_TENANT, None), "p")[0], "p")
    assert again == grd.sync_text(_meta_pack(_TENANT, None), "p")[0]


def test_check_fails_when_one_annotation_is_dropped(da_read_env, tmp_path, capsys):
    rp = tmp_path / "rule-packs"
    shutil.copytree(_REPO / "rule-packs", rp)
    p = rp / "rule-pack-redis.yaml"
    text = p.read_text(encoding="utf-8")
    head, sep, rest = text.partition("      - alert: RedisDown\n")
    owner = f'          owner: "{META["owner"]}"\n'
    assert owner in rest
    p.write_text(head + sep + rest.replace(owner, "", 1), encoding="utf-8")
    assert grd.main(["--check", "--rule-packs-dir", str(rp)]) == 1
    assert grd.main(["--rule-packs-dir", str(rp)]) == 0
    assert grd.main(["--check", "--rule-packs-dir", str(rp)]) == 0


def test_semantic_check_judges_the_compiled_pack_too():
    """The custom-alerts pack is never written, only checked: a compiler that
    stops emitting the annotations is caught on the loaded rules."""
    class RF:
        path = "rule-pack-custom-alerts.yaml"
        documents = [{"groups": [{"rules": [{
            "alert": "CustomRecipeSilent", "expr": "x",
            "labels": {"tenant": "{{ $labels.tenant }}", "maintenance": "page"},
            "annotations": {"summary": "s"}}]}]}]
    problems = grd.semantic_problems([RF()], selectors_of=lambda exprs: [[] for _ in exprs])
    assert any("shows the tenant's metadata" in p for p in problems), problems


# ── the writer's net: a rewrite changes nothing but the derived parts ─────────
# Shapes from the C-gen adversarial review (#2566): each was written with rc 0
# and passed --check and promtool while it silently changed another value.

def _pack_with(tmp_path, group: str) -> Path:
    rp = tmp_path / "rule-packs"
    shutil.copytree(_REPO / "rule-packs", rp)
    p = rp / "rule-pack-redis.yaml"
    p.write_text(p.read_text(encoding="utf-8").rstrip("\n") + "\n" + group, encoding="utf-8")
    return rp


_GROUP_HEAD = ("  - name: review-shapes\n    rules:\n      - alert: ReviewShape\n"
               "        expr: up == 0\n        labels:\n          maintenance: page\n"
               "          tenant: \"{{ $labels.tenant }}\"\n        annotations:\n")


def _loaded_annotations(rp: Path) -> dict:
    import yaml
    for g in yaml.safe_load((rp / "rule-pack-redis.yaml").read_text(encoding="utf-8"))["groups"]:
        for r in g["rules"]:
            if r.get("alert") == "ReviewShape":
                return r["annotations"]
    raise AssertionError("ReviewShape not found")


def test_a_metadata_key_spelled_over_two_lines_is_replaced_whole(da_read_env, tmp_path):
    rp = _pack_with(tmp_path, _GROUP_HEAD +
                    "          summary: hello world\n"
                    "          runbook_url: https://example.com/a\n"
                    "            /b\n")
    assert grd.main(["--rule-packs-dir", str(rp)]) == 0
    ann = _loaded_annotations(rp)
    assert ann["summary"] == "hello world"          # was 'hello world /b'
    assert {k: ann[k] for k in META} == META
    assert grd.main(["--check", "--rule-packs-dir", str(rp)]) == 0


def test_a_quoted_metadata_key_is_the_same_key(da_read_env, tmp_path):
    rp = _pack_with(tmp_path, _GROUP_HEAD + '          summary: "s"\n          "tier": "gold"\n')
    assert grd.main(["--rule-packs-dir", str(rp)]) == 0  # was: a duplicate key, file left unloadable
    assert _loaded_annotations(rp)["tier"] == META["tier"]


def test_a_rewrite_that_would_change_another_value_is_not_written(da_read_env, tmp_path):
    """A keep-chomp (`|+`) last annotation: inserting after it would drop its
    trailing newline. The net refuses the write and leaves the file as it was."""
    rp = _pack_with(tmp_path, _GROUP_HEAD + "          summary: |+\n            keep trailing\n\n")
    before = (rp / "rule-pack-redis.yaml").read_text(encoding="utf-8")
    assert grd.main(["--rule-packs-dir", str(rp)]) == 2
    assert (rp / "rule-pack-redis.yaml").read_text(encoding="utf-8") == before


def test_rewrite_problems_ignores_only_the_derived_parts():
    old = "groups:\n- name: g\n  rules:\n  - alert: A\n    expr: up == 0\n    annotations:\n      summary: a\n"
    new = old.replace("summary: a\n", 'summary: a\n      owner: "x"\n')
    assert grd.rewrite_problems(old, new, "rule-pack-x.yaml") == []
    assert grd.rewrite_problems(old, old.replace("summary: a", "summary: b"), "rule-pack-x.yaml")


def test_a_rewrite_that_would_change_a_group_label_is_not_written(da_read_env, tmp_path):
    """Second review round: text inside a group-label block scalar that looks
    like an alert was 'completed' by the writer, changing the label every alert
    of the group carries. The net compares group keys too."""
    group = ("  - name: advG\n    labels:\n      note: |\n        - alert: Phantom\n"
             "          expr: up\n          labels:\n            tenant: x\n"
             "            maintenance: page\n    rules:\n      - alert: AdvReal\n"
             "        expr: up == 0\n        labels:\n          maintenance: page\n")
    rp = _pack_with(tmp_path, group)
    before = (rp / "rule-pack-redis.yaml").read_text(encoding="utf-8")
    assert grd.main(["--rule-packs-dir", str(rp)]) == 2
    assert (rp / "rule-pack-redis.yaml").read_text(encoding="utf-8") == before


# ── #3678: the two gaps #3651's adversarial review found ────────────────────
# tests/rulepacks/maintenance-derived-gaps_test.yaml is the promtool proof
# that each shape breaks the maintenance contract; these pin that --check
# refuses them (and only them).

def _page_alert_reading(tmp_path, tail: str) -> Path:
    rp = tmp_path / "rule-packs"
    shutil.copytree(_REPO / "rule-packs", rp)
    p = rp / "rule-pack-redis.yaml"
    text = p.read_text(encoding="utf-8")
    old = '      - alert: RedisDown\n        expr: redis_up == 0\n'
    assert old in text, "fixture anchor moved: RedisDown's expr"
    p.write_text(text.replace(old, f'      - alert: RedisDown\n        expr: redis_up == 0 {tail}\n', 1),
                 encoding="utf-8")
    return rp


@pytest.mark.parametrize("tail", [
    'and on(tenant) ALERTS{alertname="RedisMemoryHigh"}',
    'and on(tenant) ALERTS_FOR_STATE',
    'and on(tenant) {__name__=~"ALERTS.*"}',
])
def test_a_rule_reading_alert_series_fails_the_check(da_read_env, tmp_path, capsys, tail):
    rp = _page_alert_reading(tmp_path, tail)
    assert grd.main(["--check", "--rule-packs-dir", str(rp)]) == 1
    assert "the series of firing alerts" in capsys.readouterr().err


def test_reading_another_series_the_same_way_passes(da_read_env, tmp_path):
    """Negative control: same join, a series that is not an alert's."""
    rp = _page_alert_reading(tmp_path, 'and on(tenant) redis_up')
    assert grd.main(["--check", "--rule-packs-dir", str(rp)]) == 0


_MUTE_GROUP = ("  - name: f2-shape\n    rules:\n      - alert: F2Shape\n        expr: |\n"
               "          {lhs}\n          unless on(tenant)\n"
               "          (user_state_filter{{filter=\"maintenance\"}} == 1)\n"
               "        labels:\n          maintenance: mute\n")


@pytest.mark.parametrize("lhs", [
    'sum without(tenant) (up{job="clickhouse"} == 0)',
    'sum(up{job="clickhouse"} == 0) > 0',
    'max by(job) (up{job="clickhouse"}) == 0',
])
def test_a_mute_clause_whose_left_side_dropped_tenant_fails_the_check(da_read_env, tmp_path, capsys, lhs):
    rp = _pack_with(tmp_path, _MUTE_GROUP.format(lhs=lhs))
    assert grd.main(["--check", "--rule-packs-dir", str(rp)]) == 1
    assert "drops `tenant`" in capsys.readouterr().err


def test_a_mute_clause_whose_left_side_keeps_tenant_passes(da_read_env, tmp_path):
    """Negative control: the aggregation keeps `tenant`."""
    rp = _pack_with(tmp_path, _MUTE_GROUP.format(lhs='max by(tenant) (up{job="clickhouse"}) == 0'))
    assert grd.main(["--check", "--rule-packs-dir", str(rp)]) == 0


def test_a_left_side_the_parse_tree_cannot_prove_is_refused():
    expr = "x" + "\n" + TAIL
    end = len(expr.encode("utf-8"))
    unknown = grd.clause_flow_problems([("p", "alert A", expr)],
                                       lambda exprs: [[{"op": "unless", "start": 0, "end": end, "lhs": "unknown"}]])
    assert unknown and "cannot prove" in unknown[0]
    missing = grd.clause_flow_problems([("p", "alert A", expr)], lambda exprs: [[]])
    assert missing and "no `unless on(tenant)`" in missing[0]
    kept = grd.clause_flow_problems([("p", "alert A", expr)],
                                    lambda exprs: [[{"op": "unless", "start": 0, "end": end, "lhs": "kept"}]])
    assert kept == []


_RECORD_HIDES = ("  - name: f2-via-record\n    rules:\n"
                 "      - record: job:f2probe_up:min\n        expr: min by(job) (up{job=\"clickhouse\"})\n"
                 "      - alert: F2ViaRecord\n        expr: |\n          job:f2probe_up:min == 0\n"
                 "          unless on(tenant)\n          (user_state_filter{filter=\"maintenance\"} == 1)\n"
                 "        labels:\n          maintenance: mute\n")


def test_a_record_cannot_hide_a_dropped_tenant(da_read_env, tmp_path, capsys):
    """Review of #3678: the F2 shape moved one rule away (the record drops
    tenant, the alert's clause reads the record)."""
    rp = _pack_with(tmp_path, _RECORD_HIDES)
    assert grd.main(["--check", "--rule-packs-dir", str(rp)]) == 1
    assert "drops `tenant`" in capsys.readouterr().err


def test_a_union_with_a_dropped_side_fails_the_check(da_read_env, tmp_path, capsys):
    rp = _pack_with(tmp_path, _MUTE_GROUP.format(
        lhs='(up{job="clickhouse"} == 0 or sum(up{job="clickhouse"}) == 0)'))
    assert grd.main(["--check", "--rule-packs-dir", str(rp)]) == 1
    assert "drops `tenant`" in capsys.readouterr().err
