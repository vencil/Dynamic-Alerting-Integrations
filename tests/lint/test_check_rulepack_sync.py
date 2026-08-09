"""Tests for check_rulepack_sync.py — rule-pack 3-copy semantic drift guard.

Pinned contracts
----------------
1. **Serialization-agnostic**: block-scalar vs `\\n`-escaped vs different
   whitespace/comments around the same PromQL compare EQUAL (`_norm_expr`).
2. **Token-minify + comment-strip** (Gemini review): `by(t)` == `by (t)`,
   and a `#` line comment must not change the normalized expr.
3. **Drift detection**: a record whose expr genuinely differs is flagged;
   a missing/extra rule is flagged.
4. **Three formats parse**: raw rule-pack / ConfigMap data-keys / CRD.
5. **Live dogfood**: after PR3-pre-1, the HA `sum`/`max` axis is consistent;
   remaining cross-copy differences are the (known, separate) enrichment
   drift — so this test does NOT assert repo-wide sync (that lands with the
   configmap regeneration). It asserts the comparator MECHANICS only.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_TOOLS_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "tools", "lint")
sys.path.insert(0, _TOOLS_DIR)

import check_rulepack_sync as sync  # noqa: E402


def test_norm_expr_whitespace_and_token_minify():
    a = sync._norm_expr("max by(tenant) (user_threshold{metric=\"x\"})")
    b = sync._norm_expr("max  by (tenant)  (\n  user_threshold{metric=\"x\"}\n)")
    assert a == b


def test_norm_expr_comment_strip():
    a = sync._norm_expr("sum by(tenant) (user_threshold)")
    b = sync._norm_expr("sum by(tenant) (\n  # a comment\n  user_threshold\n)")
    assert a == b


def test_norm_expr_operator_spacing():
    assert sync._norm_expr("a > on(tenant) b") == sync._norm_expr("a>on(tenant)b")


# ⛔ Normalization is for SERIALIZATION, and a string literal is not
# serialization — a label matcher's value is the query. Every pair below used to
# normalize to the same thing, in all three of PromQL's quote styles, and the
# `#` cases did not merely collide: they truncated the expression to a fragment
# (`up{job="a`). Two consumers, both wrong in the fail-open direction — this
# module's drift gate stops seeing a real difference between a source pack and
# its deployed copies, and `_rule_tree._alert_names` accepts a hand-authored
# rule as a faithful copy of a pack it only resembles, which is the provenance
# test that decides whether the platform contracts apply to it.
@pytest.mark.parametrize("a,b", [
    ('up{job="a#b"} == 0', 'up{job="a#c"} == 0'),          # hash in a value
    ("up{job='x#y'} == 0", "up{job='x#z'} == 0"),          # single-quoted
    ('up{job=`p#q`} == 0', 'up{job=`p#r`} == 0'),          # backtick, raw
    ('up{msg="a  b"} == 0', 'up{msg="a b"} == 0'),         # whitespace
    ('up{path=~"/a, b"} == 0', 'up{path=~"/a,b"} == 0'),   # different regexes
    ('up{re=~"(a )b"} == 0', 'up{re=~"(a)b"} == 0'),       # different regexes
    ('up{msg="a > b"} == 0', 'up{msg="a>b"} == 0'),        # operator chars
])
def test_norm_expr_does_not_rewrite_inside_string_literals(a, b):
    assert sync._norm_expr(a) != sync._norm_expr(b), (
        f"{a!r} and {b!r} are different queries and normalized to the same "
        f"thing: {sync._norm_expr(a)!r}")
    # …and the literal survives intact, rather than being merely non-equal.
    assert '"a  b"' in sync._norm_expr('up{msg="a  b"} == 0')


def test_norm_expr_still_strips_a_real_comment_containing_a_quote():
    """The interleaving rule, which handling either token alone gets wrong.

    `#` before a quote opens a comment (the apostrophe in "don't" is inside it);
    a quote before a `#` opens a string (the hash is data). One left-to-right
    alternation gives both; two independent passes give whichever ran first.
    """
    plain = sync._norm_expr("x == 0")
    assert sync._norm_expr("x == 0  # don't do this") == plain
    assert sync._norm_expr('x == 0  # a "quoted" note') == plain
    assert sync._norm_expr("x == 0\n# trailing comment line") == plain


def test_norm_expr_is_unchanged_on_every_shipped_expression():
    """The no-op claim, asserted rather than asserted-in-prose.

    String-awareness is a behaviour change to a CI drift gate, so the tree it
    guards has to be measured, not reasoned about. Recomputed here rather than
    pinned to a snapshot: what matters is that the normal form of a real
    expression contains no comment and no rewritten literal.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]
                          / "scripts" / "tools" / "lint"))
    from _rule_tree import _rule_containers  # noqa: PLC0415

    checked = 0
    for _where, doc, _prov in _rule_containers():
        for group in (doc.get("groups") or []):
            for rule in ((group or {}).get("rules") or []):
                expr = rule.get("expr")
                if expr is None:
                    continue
                checked += 1
                normalized = sync._norm_expr(expr)
                # Idempotent: normalizing twice is normalizing once. A
                # string-unaware pass fails this the moment a literal contains
                # any of the characters the passes rewrite.
                assert sync._norm_expr(normalized) == normalized, expr
    assert checked >= 900, (
        f"only {checked} expressions examined — the sweep is not covering the "
        "tree it claims to")


def test_extract_and_diff():
    g_a = [{"name": "g", "interval": "15s", "rules": [
        {"record": "r", "expr": "max by(tenant) (user_threshold)"}]}]
    g_b = [{"name": "g", "interval": "15s", "rules": [
        {"record": "r", "expr": "max  by (tenant) ( user_threshold )"}]}]  # same semantics
    assert sync._diff_maps(sync._extract(g_a), sync._extract(g_b)) == []

    g_c = [{"name": "g", "interval": "15s", "rules": [
        {"record": "r", "expr": "sum by(tenant) (user_threshold)"}]}]  # different op
    findings = sync._diff_maps(sync._extract(g_a), sync._extract(g_c))
    assert any("content differs" in f for f in findings)


def test_groups_from_configmap(tmp_path):
    cm = tmp_path / "configmap-rules-t.yaml"
    cm.write_text(
        "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: prometheus-rules-t\n"
        "data:\n  t-recording.yml: |\n    groups:\n      - name: g\n        rules:\n"
        "          - record: r\n            expr: max by(tenant) (user_threshold)\n",
        encoding="utf-8")
    groups = sync._groups_from_configmap(cm)
    assert groups and groups[0]["name"] == "g"


def test_check_pack_missing_copies(tmp_path):
    (tmp_path / "rule-packs").mkdir()
    (tmp_path / "rule-packs" / "rule-pack-t.yaml").write_text("groups: []\n", encoding="utf-8")
    ok, findings = sync.check_pack(tmp_path, "t")
    assert not ok and any("missing" in f for f in findings)


import yaml  # noqa: E402


def _write_three_copies(root, name, src_op="max", cm_op="max", op_op="max"):
    """Write rule-pack + configmap + operator copies of one pack."""
    (root / "rule-packs").mkdir(exist_ok=True)
    (root / "k8s" / "03-monitoring").mkdir(parents=True, exist_ok=True)
    (root / "operator-manifests").mkdir(exist_ok=True)

    def groups(op):
        return [{"name": "g", "interval": "15s", "rules": [
            {"record": "tenant:alert_threshold:x",
             "expr": f"{op} by(tenant) (user_threshold{{metric=\"x\"}})"}]}]

    (root / "rule-packs" / f"rule-pack-{name}.yaml").write_text(
        yaml.dump({"groups": groups(src_op)}), encoding="utf-8")
    cm = {"apiVersion": "v1", "kind": "ConfigMap",
          "metadata": {"name": f"prometheus-rules-{name}"},
          "data": {f"{name}-recording.yml": yaml.dump({"groups": groups(cm_op)})}}
    (root / "k8s" / "03-monitoring" / f"configmap-rules-{name}.yaml").write_text(
        yaml.dump(cm), encoding="utf-8")
    op = {"apiVersion": "monitoring.coreos.com/v1", "kind": "PrometheusRule",
          "spec": {"groups": groups(op_op)}}
    (root / "operator-manifests" / f"da-rule-pack-{name}.yaml").write_text(
        yaml.dump(op), encoding="utf-8")


def test_check_pack_in_sync(tmp_path):
    """All three copies semantically identical → ok (exercises all 3 parsers)."""
    _write_three_copies(tmp_path, "t")
    ok, findings = sync.check_pack(tmp_path, "t")
    assert ok and findings == []


def test_check_pack_configmap_drift(tmp_path):
    """A configmap whose op differs from source is flagged."""
    _write_three_copies(tmp_path, "t", src_op="max", cm_op="sum")
    ok, findings = sync.check_pack(tmp_path, "t")
    assert not ok and any("configmap" in f and "content differs" in f for f in findings)


def test_main_exit_codes(tmp_path, monkeypatch):
    monkeypatch.setattr(sync, "_repo_root", lambda: tmp_path)
    _write_three_copies(tmp_path, "t")
    monkeypatch.setattr(sys, "argv", ["check_rulepack_sync.py", "--ci"])
    assert sync.main() == 0
    # introduce drift in the operator copy
    _write_three_copies(tmp_path, "t", src_op="max", op_op="sum")
    assert sync.main() == 1
