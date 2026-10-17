#!/usr/bin/env python3
"""Tests for _observed_map_lib (#719) — observed-map extraction + drift-guard.

Validates the containment-check extractor and the consistency checker against
synthetic rule packs (hermetic — no dependence on the real rule-packs/ content),
plus a smoke check that the committed map passes its own drift-guard.
"""
import os
import sys

import pytest

_OPS = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "tools", "ops"))
if _OPS not in sys.path:
    sys.path.insert(0, _OPS)

import _observed_map_lib as L  # noqa: E402

yaml = pytest.importorskip("yaml")


def _write_pack(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p)


# A minimal pack: one clean upper-bound alert, one lower-bound alert.
PACK_CLEAN = """
groups:
  - name: g
    rules:
      - record: tenant:foo_usage:max
        expr: max by(tenant) (foo_usage)
      - record: tenant:alert_threshold:foo
        expr: max by(tenant) (user_threshold{metric="foo"})
      - alert: FooHigh
        expr: |
          tenant:foo_usage:max
          > on(tenant) group_left tenant:alert_threshold:foo
      - record: tenant:bar_count:max
        expr: max by(tenant) (bar_count)
      - record: tenant:alert_threshold:bar
        expr: max by(tenant) (user_threshold{metric="bar"})
      - alert: BarLow
        expr: |
          tenant:bar_count:max
          < on(tenant) group_left tenant:alert_threshold:bar
"""

# A composite alert referencing two observed series for one threshold key.
PACK_COMPOSITE = """
groups:
  - name: g
    rules:
      - record: tenant:a_usage:max
        expr: max by(tenant) (a_usage)
      - record: tenant:b_usage:max
        expr: max by(tenant) (b_usage)
      - record: tenant:alert_threshold:combo
        expr: max by(tenant) (user_threshold{metric="combo"})
      - alert: ComboHigh
        expr: |
          (tenant:a_usage:max > on(tenant) group_left tenant:alert_threshold:combo)
          or
          (tenant:b_usage:max > on(tenant) group_left tenant:alert_threshold:combo)
"""

# Orphan: threshold recording rule with NO alert referencing it.
PACK_ORPHAN = """
groups:
  - name: g
    rules:
      - record: tenant:alert_threshold:lonely
        expr: max by(tenant) (user_threshold{metric="lonely"})
"""


class TestExtraction:
    def test_clean_upper_bound_resolves(self, tmp_path):
        p = _write_pack(tmp_path, "rule-pack-clean.yaml", PACK_CLEAN)
        m = L.build_map([p])
        assert m["foo"]["observed_series"] == "tenant:foo_usage:max"
        assert m["foo"]["direction"] == ">"
        assert m["foo"]["scope"] == "tenant"
        assert not m["foo"].get("needs_review")

    def test_unclassified_lower_bound_records_the_fact(self, tmp_path):
        """#3017：map 記事實。沒分類的 `<` key 照樣記下比較的序列與方向，
        推薦器仍以 lower-bound marker 略過（govern 列為未治理下界），backtest
        讀得到；分類缺漏改由 drift-guard 擋（見 TestConsistency）。"""
        p = _write_pack(tmp_path, "rule-pack-clean.yaml", PACK_CLEAN)
        m = L.build_map([p])
        e = m["bar"]
        assert e["observed_series"] == "tenant:bar_count:max"
        assert e["direction"] == "<"
        assert not e.get("needs_review") and L.MODE_FIELD not in e
        series, why = L.usable_for_recommend(e)
        assert series is None and "lower-bound (<)" in why
        assert L.usable_for_backtest(e) == ("tenant:bar_count:max", "below", None)

    def test_composite_needs_review_with_candidates(self, tmp_path):
        p = _write_pack(tmp_path, "rule-pack-composite.yaml", PACK_COMPOSITE)
        m = L.build_map([p])
        assert m["combo"].get("needs_review") is True
        assert set(m["combo"]["candidates"]) == {"tenant:a_usage:max", "tenant:b_usage:max"}
        assert "observed_series" not in m["combo"]

    def test_denylist_excludes_threshold_and_metadata(self, tmp_path):
        p = _write_pack(tmp_path, "rule-pack-clean.yaml", PACK_CLEAN)
        m = L.build_map([p])
        # the alert_threshold series must never appear as an observed candidate
        assert m["foo"]["observed_series"] != "tenant:alert_threshold:foo"


class TestDirectionParsing:
    """Grammar-space guards for _direction_before (self-review hardening)."""

    def test_prefix_sibling_does_not_steal_direction(self):
        # A composite expr referencing BOTH <key> and <key>_critical: the bare
        # str.find would match ':cpu' inside ':cpu_critical' first and read the
        # wrong operator. Word-boundary matching must read each token's own dir.
        expr = (
            "tenant:x:max < on(tenant) tenant:alert_threshold:mysql_threads_running_critical "
            "or tenant:y:max > on(tenant) tenant:alert_threshold:mysql_threads_running"
        )
        assert L._direction_before(expr, "mysql_threads_running") == ">"
        assert L._direction_before(expr, "mysql_threads_running_critical") == "<"

    def test_ge_le_resolve_to_gt_lt(self):
        assert L._direction_before("a >= tenant:alert_threshold:k", "k") == ">"
        assert L._direction_before("a <= tenant:alert_threshold:k", "k") == "<"

    def test_scaling_regex_catches_paren_scalar(self):
        assert L.SCALING_RE.search("foo * 100")
        assert L.SCALING_RE.search("foo * (100)")
        assert L.SCALING_RE.search("foo / 1024")
        # the metadata join must NOT look like scaling
        assert not L.SCALING_RE.search("x * on(tenant) group_left(owner) tenant_metadata_info")


class TestResolveObserved:
    def test_resolved_clean(self):
        series, reason = L.resolve_observed(
            {"scope": "tenant", "direction": ">", "observed_series": "tenant:x:max"}
        )
        assert series == "tenant:x:max"
        assert reason is None

    def test_needs_review_skips(self):
        series, reason = L.resolve_observed(
            {"scope": "tenant", "needs_review": True, "reason": "composite"}
        )
        assert series is None
        assert reason == "composite"

    def test_unsupported_scope_skips(self):
        series, reason = L.resolve_observed(
            {"scope": "tenant_version", "observed_series": "tenant_version:x:vlabeled"}
        )
        assert series is None
        assert "unsupported scope" in reason

    def test_lower_bound_skips_even_if_resolved(self):
        series, reason = L.resolve_observed(
            {"scope": "tenant", "direction": "<", "observed_series": "tenant:x:max"}
        )
        assert series is None
        assert "lower-bound" in reason

    def test_missing_direction_skips(self):
        # CodeRabbit #3334234447: a hand-edited entry with a resolved series but
        # no direction must NOT slip through to a recommendation.
        series, reason = L.resolve_observed(
            {"scope": "tenant", "observed_series": "tenant:x:max"}
        )
        assert series is None
        assert "direction" in reason


class TestConsistency:
    def test_clean_map_no_errors(self, tmp_path):
        p = _write_pack(tmp_path, "rule-pack-clean.yaml", PACK_CLEAN)
        m = L.build_map([p])
        res = L.check_consistency(m, [p])
        # The only error is the unclassified '<' key (#3017 gate below).
        assert [e for e in res["errors"] if not e.startswith("bar:")] == []

    def test_unclassified_lower_bound_is_error(self, tmp_path):
        """#3017：`<` key 不再以 needs_review 隱含「未分類」；沒分類就是 drift。"""
        p = _write_pack(tmp_path, "rule-pack-clean.yaml", PACK_CLEAN)
        res = L.check_consistency(L.build_map([p]), [p])
        assert any(e.startswith("bar:") and "not classified" in e for e in res["errors"])

    def test_stale_observed_series_is_error(self, tmp_path):
        p = _write_pack(tmp_path, "rule-pack-clean.yaml", PACK_CLEAN)
        m = L.build_map([p])
        m["foo"]["observed_series"] = "tenant:NONEXISTENT:max"
        res = L.check_consistency(m, [p])
        assert any("foo" in e for e in res["errors"])

    def test_scope_prefix_mismatch_is_error(self, tmp_path):
        p = _write_pack(tmp_path, "rule-pack-clean.yaml", PACK_CLEAN)
        m = L.build_map([p])
        m["foo"]["scope"] = "tenant_version"  # but series is tenant:-prefixed
        res = L.check_consistency(m, [p])
        assert any("scope" in e for e in res["errors"])

    def test_stale_removed_key_is_error(self, tmp_path):
        # CodeRabbit #3334234459: a key no longer in the rule packs but still in
        # the map (incl. needs_review form) must be flagged as drift.
        p = _write_pack(tmp_path, "rule-pack-clean.yaml", PACK_CLEAN)
        m = L.build_map([p])
        m["ghost_metric"] = {  # not present in any pack
            "scope": "tenant",
            "candidates": ["tenant:ghost:max"],
            "needs_review": True,
            "reason": "composite",
        }
        res = L.check_consistency(m, [p])
        assert any("ghost_metric" in e and "stale" in e.lower() for e in res["errors"])

    def test_orphan_threshold_classified(self, tmp_path):
        p = _write_pack(tmp_path, "rule-pack-orphan.yaml", PACK_ORPHAN)
        m = L.build_map([p])  # lonely has no alert → not extracted
        res = L.check_consistency(m, [p])
        assert "lonely" in res["orphan_thresholds"]
        assert res["errors"] == []

    def test_known_deferred_is_info_not_error(self, tmp_path):
        # synthesize a pack that references container_cpu via an alert so it is
        # an "all key" but keep it out of the map → must land in infos, not gaps.
        pack = """
groups:
  - name: g
    rules:
      - record: tenant:alert_threshold:container_cpu
        expr: max by(tenant) (user_threshold{metric="cpu"})
      - alert: CpuHigh
        expr: tenant:x:max > on(tenant) group_left tenant:alert_threshold:container_cpu
"""
        p = _write_pack(tmp_path, "rule-pack-kubernetes.yaml", pack)
        # empty map → container_cpu absent
        res = L.check_consistency({}, [p])
        assert any("container_cpu" in i for i in res["infos"])
        assert "container_cpu" not in res["coverage_gaps"]


class TestLoadFailsClosed:
    """#3017：缺檔、讀不了、沒有 keys 一律 raise，不再回 {}（消費端會把 {}
    讀成「沒有任何 key 有對映」而靜默放行）。"""

    @pytest.mark.parametrize("body", [
        None,                      # 檔案不存在
        "",                        # 空檔
        "version: 1\n",            # 沒有 keys
        "keys: {}\n",              # keys 為空
        "keys: [a, b]\n",          # keys 不是 mapping
        "keys: [\n",               # YAML 壞掉
        "- just\n- a list\n",       # 頂層不是 mapping
    ], ids=["missing", "empty", "no-keys", "empty-keys", "keys-list", "bad-yaml", "top-list"])
    def test_unusable_map_raises(self, tmp_path, body):
        p = tmp_path / "metric_observed_map.yaml"
        if body is not None:
            p.write_text(body, encoding="utf-8")
        with pytest.raises(L.ObservedMapError, match=str(p).replace("\\", "\\\\")):
            L.load_observed_map(str(p))

    @pytest.mark.parametrize("body, kind", [
        (None, "missing"), ("", "empty"), ("keys: {}\n", "empty"),
        ("keys: [\n", "unreadable"), ("keys: [a, b]\n", "unreadable"),
    ])
    def test_error_kind_and_one_line(self, tmp_path, body, kind):
        """kind 決定生成器能不能重建；訊息一行（YAMLError 的 str() 有多行）。"""
        p = tmp_path / "metric_observed_map.yaml"
        if body is not None:
            p.write_text(body, encoding="utf-8")
        with pytest.raises(L.ObservedMapError) as ei:
            L.load_observed_map(str(p))
        assert ei.value.kind == kind
        assert "\n" not in str(ei.value)

    @pytest.mark.parametrize("body", ["", "keys: {}\n", "version: 1\n"])
    def test_generator_rebuilds_a_keyless_map(self, tmp_path, body):
        """沒有 key 的 map 沒有人工 pick 可保留：重新產生，不因 fail-closed 卡住。"""
        out = tmp_path / "metric_observed_map.yaml"
        out.write_text(body, encoding="utf-8")
        L.write_observed_map(out_path=str(out))
        assert L.load_observed_map(str(out)) == L.load_observed_map()

    def test_generator_refuses_an_unreadable_map(self, tmp_path):
        """YAML 壞掉的 map 可能還有人工 pick：拒絕覆寫，提示修正或刪除。"""
        out = tmp_path / "metric_observed_map.yaml"
        out.write_text("keys: [\n", encoding="utf-8")
        with pytest.raises(L.ObservedMapError, match="fix or delete"):
            L.write_observed_map(out_path=str(out))
        assert out.read_text(encoding="utf-8") == "keys: [\n"

    @pytest.mark.parametrize("body,why", [
        ("keys:\n  k: 5\n", "is a int, not a mapping"),
        ("keys:\n  k:\n    observed_series: [a, b]\n", "non-string 'observed_series'"),
        ("keys:\n  k:\n    direction: 1\n", "non-string 'direction'"),
        ("keys:\n  k:\n    scope: [tenant]\n", "non-string 'scope'"),
        ("keys:\n  k:\n    candidates: tenant:a:max\n", "'candidates' that is not a list"),
        ("keys:\n  k:\n    candidates: [1]\n", "'candidates' that is not a list"),
        ("keys:\n  1:\n    direction: '>'\n", "is not keyed by a string"),
    ], ids=["int-entry", "list-series", "int-direction", "list-scope", "str-candidates",
            "int-candidate", "int-key"])
    def test_malformed_entry_is_unreadable(self, tmp_path, body, why):
        """#3017 review：型別不對的條目以前會在消費端變成 traceback（`.get`
        打在 int 上）或拼出假的 PromQL（list 當 series 名）。一行、指名 key。"""
        p = tmp_path / "metric_observed_map.yaml"
        p.write_text(body, encoding="utf-8")
        with pytest.raises(L.ObservedMapError) as ei:
            L.load_observed_map(str(p))
        assert ei.value.kind == "unreadable"
        assert why in str(ei.value) and "entry " in str(ei.value)
        assert "\n" not in str(ei.value)

    def test_generator_refuses_a_malformed_map(self, tmp_path):
        """型別壞掉的 map 一樣是 unreadable：生成器拒絕覆寫（可能還有人工 pick）。"""
        out = tmp_path / "metric_observed_map.yaml"
        body = "keys:\n  k:\n    observed_series: [a, b]\n"
        out.write_text(body, encoding="utf-8")
        with pytest.raises(L.ObservedMapError, match="fix or delete"):
            L.write_observed_map(out_path=str(out))
        assert out.read_text(encoding="utf-8") == body

    def test_usable_map_returns_keys(self, tmp_path):
        p = tmp_path / "metric_observed_map.yaml"
        p.write_text("keys:\n  k:\n    direction: '>'\n", encoding="utf-8")
        assert L.load_observed_map(str(p)) == {"k": {"direction": ">"}}

    def test_default_path_is_read_at_call_time(self, tmp_path, monkeypatch):
        monkeypatch.setattr(L, "DEFAULT_MAP_PATH", str(tmp_path / "absent.yaml"))
        with pytest.raises(L.ObservedMapError, match="not found"):
            L.load_observed_map()


class TestLookupAndPolicies:
    """#3017：同一份事實、兩種政策——recommend 與 backtest 各自決定能不能用。"""

    def test_lookup_kinds(self):
        """#3111 (e)：回傳種類，不是要呼叫端比對字串的 reason。"""
        m = L.load_observed_map()
        assert L.lookup(m, "mysql_connections") == (L.LookupKind.COMPARED, m["mysql_connections"])
        assert L.lookup(m, "container_cpu") == (L.LookupKind.KNOWN_DEFERRED, None)
        assert L.lookup(m, "mongodb_connections_current_critical") == (L.LookupKind.NO_ALERT, None)

    def test_lookup_reasons(self):
        assert L.lookup_reason(L.LookupKind.COMPARED, "k") is None
        assert "KNOWN_DEFERRED" in L.lookup_reason(L.LookupKind.KNOWN_DEFERRED, "container_cpu")
        assert L.lookup_reason(L.LookupKind.NO_ALERT, "k").startswith("no shipped alert compares")

    def test_known_deferred_wins_over_an_entry(self):
        """KNOWN_DEFERRED 的 key 不應有條目（閘門守）；萬一有，仍以 KNOWN_DEFERRED 論。"""
        assert L.lookup({"container_cpu": {"direction": ">"}}, "container_cpu") == (
            L.LookupKind.KNOWN_DEFERRED, None)

    def test_backtest_policy_on_committed_lower_bound_keys(self):
        """#3017 的 4 個下界 key 全都能回測，且方向是 below。"""
        m = L.load_observed_map()
        for k in ("db2_bufferpool_hit_ratio", "kafka_active_controllers",
                  "kafka_broker_count", "rabbitmq_consumers"):
            series, direction, why = L.usable_for_backtest(m[k])
            assert (direction, why) == ("below", None) and series, k

    @pytest.mark.parametrize("entry, needle", [
        ({"needs_review": True, "reason": "pick one", "direction": ">"}, "needs review: pick one"),
        ({"scope": "tenant_version", "direction": ">", "observed_series": "x"}, "unsupported scope"),
        ({"scope": "tenant", "observed_series": "x"}, "direction"),
        ({"scope": "tenant", "direction": ">", "candidates": ["a", "b"]}, "composite"),
    ])
    def test_backtest_policy_refuses_with_a_reason(self, entry, needle):
        series, direction, why = L.usable_for_backtest(entry)
        assert series is None and direction is None and needle in why

    def test_recommend_policy_ignores_the_fact_shape(self):
        """改記事實（#3017）不改推薦判定：每個出廠條目換回舊形狀
        （candidates 而非 observed_series）時，usable_for_recommend 結果相同。"""
        for key, entry in L.load_observed_map().items():
            if not entry.get(L.MODE_FIELD) == "not-applicable":
                continue
            old_shape = {k: v for k, v in entry.items() if k != "observed_series"}
            old_shape["candidates"] = [entry["observed_series"]]
            assert L.usable_for_recommend(old_shape) == L.usable_for_recommend(entry), key


class TestCommittedMap:
    """The committed map must pass its own drift-guard (no errors)."""

    def test_committed_map_consistent(self):
        m = L.load_observed_map()  # raises when absent (#3017): never a skip
        res = L.check_consistency(m, L.default_pack_paths())
        assert res["errors"] == [], f"committed map has drift: {res['errors']}"

    def test_committed_map_consistent_with_exit_lock(self):
        # #916 Item B: the real-map lint path enforces the KNOWN_DEFERRED exit
        # lock; the committed map + real packs must still be green under it.
        m = L.load_observed_map()  # raises when absent (#3017): never a skip
        res = L.check_consistency(
            m, L.default_pack_paths(), enforce_known_deferred=True
        )
        assert res["errors"] == [], f"exit-lock drift: {res['errors']}"


# ---------------------------------------------------------------------------
# #916 Item B — merge-preserve + drift hardening
# ---------------------------------------------------------------------------

# Single-key foo packs for check_consistency drift tests (no bar → no unrelated
# stale noise). Tenant-agnostic synthetic keys only (no real tenant id).
PACK_FOO_UP = """
groups:
  - name: g
    rules:
      - record: tenant:foo_usage:max
        expr: max by(tenant) (foo_usage)
      - record: tenant:alert_threshold:foo
        expr: max by(tenant) (user_threshold{metric="foo"})
      - alert: FooHigh
        expr: |
          tenant:foo_usage:max
          > on(tenant) group_left tenant:alert_threshold:foo
"""

PACK_FOO_DOWN = """
groups:
  - name: g
    rules:
      - record: tenant:foo_usage:max
        expr: max by(tenant) (foo_usage)
      - record: tenant:alert_threshold:foo
        expr: max by(tenant) (user_threshold{metric="foo"})
      - alert: FooLow
        expr: |
          tenant:foo_usage:max
          < on(tenant) group_left tenant:alert_threshold:foo
"""

PACK_FOO_SCALED = """
groups:
  - name: g
    rules:
      - record: tenant:foo_usage:max
        expr: max by(tenant) (foo_usage)
      - record: tenant:alert_threshold:foo
        expr: max by(tenant) (user_threshold{metric="foo"})
      - alert: FooHigh
        expr: |
          tenant:foo_usage:max * 100
          > on(tenant) group_left tenant:alert_threshold:foo
"""

# A composite pack that makes "foo" ambiguous (two observed series → needs_review).
PACK_FOO_COMPOSITE = """
groups:
  - name: g
    rules:
      - record: tenant:a_usage:max
        expr: max by(tenant) (a_usage)
      - record: tenant:b_usage:max
        expr: max by(tenant) (b_usage)
      - record: tenant:alert_threshold:foo
        expr: max by(tenant) (user_threshold{metric="foo"})
      - alert: FooHigh
        expr: |
          (tenant:a_usage:max > on(tenant) group_left tenant:alert_threshold:foo)
          or
          (tenant:b_usage:max > on(tenant) group_left tenant:alert_threshold:foo)
"""

# All three KNOWN_DEFERRED keys have a threshold record (→ in all_threshold_keys),
# but ONLY container_cpu is referenced by an alert (→ in fresh build_map). Lets the
# exit-lock (a) "alert-extractable" branch fire cleanly for container_cpu alone.
PACK_DEFERRED_LEAK = """
groups:
  - name: g
    rules:
      - record: tenant_version:alert_threshold:container_cpu
        expr: max by(tenant, version) (user_threshold{metric="container_cpu"})
      - record: tenant_version:alert_threshold:container_memory
        expr: max by(tenant, version) (user_threshold{metric="container_memory"})
      - record: tenant_version:alert_threshold:container_cpu_throttle
        expr: max by(tenant, version) (user_threshold{metric="container_cpu_throttle"})
      - alert: ContainerCpuHigh
        expr: |
          tenant:cpu_usage:max
          > on(tenant) group_left tenant_version:alert_threshold:container_cpu
"""


def _manual(**over):
    """A minimal explicit-manual old entry (resolved_via: manual)."""
    e = {
        "pack": "old-pack.yaml",
        "scope": "tenant",
        "direction": ">",
        "observed_series": "tenant:a_usage:max",
        "resolved_via": "manual",
    }
    e.update(over)
    return e


def _fresh_composite(**over):
    """A minimal non-determinate fresh entry (candidates, needs_review)."""
    e = {
        "pack": "new-pack.yaml",
        "scope": "tenant",
        "direction": ">",
        "candidates": ["tenant:a_usage:max", "tenant:b_usage:max"],
        "needs_review": True,
        "reason": "2 observed candidates in composite alert — pick one",
    }
    e.update(over)
    return e


class TestMergeMaps:
    def test_state1_drop_removed_key_warns(self):
        # (1) key carried in old but gone from fresh → dropped + WARN.
        old = {"gone": _manual(observed_series="tenant:gone:max")}
        merged, warns, stats = L.merge_maps(old, {})
        assert "gone" not in merged
        assert stats["dropped"] == 1
        assert any("gone" in w and "dropped" in w for w in warns)

    def test_state1_drop_emits_warn_on_stderr(self, tmp_path, capsys):
        # (1) capsys stderr: write_observed_map prints the drop WARN to stderr.
        packs = [_write_pack(tmp_path, "rule-pack-foo.yaml", PACK_FOO_UP)]
        out = tmp_path / "map.yaml"
        seed = {
            "version": 1,
            "_generated_by": "seed",
            "keys": {
                "foo": {
                    "pack": "old", "scope": "tenant", "direction": ">",
                    "observed_series": "tenant:foo_usage:max",
                },
                "ghost": _manual(observed_series="tenant:ghost:max"),
            },
        }
        out.write_text(yaml.safe_dump(seed, sort_keys=False), encoding="utf-8")
        summary = L.write_observed_map(out_path=str(out), pack_paths=packs)
        err = capsys.readouterr().err
        assert "[WARN]" in err and "ghost" in err
        assert summary["dropped"] == 1

    def test_state2_demote_pick_invalidated_annotates_and_keeps_refs(self):
        # (2) manual pick no longer a candidate → demote, reason annotated, refs kept.
        old = _manual(
            observed_series="tenant:old_pick:max",
            candidates=["tenant:old_pick:max"],
            refs=["ticket-42"],
        )
        merged, warns, stats = L.merge_maps({"k": old}, {"k": _fresh_composite()})
        e = merged["k"]
        assert e.get("needs_review") is True
        assert "observed_series" not in e  # fell back to fresh composite
        assert "manual resolution invalidated" in e["reason"]
        assert "pick no longer a candidate" in e["reason"]
        assert e["refs"] == ["ticket-42"]  # human rationale preserved
        assert stats["demoted"] == 1
        assert any("k" in w and "invalidated" in w for w in warns)

    def test_state3_preserve_is_allowlist_rebuild(self):
        # (3) valid manual pick preserved via ALLOWLIST rebuild — no stale fields.
        old = _manual(
            pack="STALE-PACK.yaml",
            observed_series="tenant:a_usage:max",
            candidates=["tenant:a_usage:max", "tenant:b_usage:max"],
            needs_review=True,               # sloppy leftover — must be stripped
            reason="stale composite reason",  # must be stripped
        )
        fresh = _fresh_composite(pack="FRESH-PACK.yaml")
        merged, warns, stats = L.merge_maps({"k": old}, {"k": fresh})
        e = merged["k"]
        assert e == {
            "pack": "FRESH-PACK.yaml",       # rebuilt from FRESH fields
            "scope": "tenant",
            "direction": ">",
            "observed_series": "tenant:a_usage:max",
            "resolved_via": "manual",
        }
        assert "reason" not in e and "needs_review" not in e and "candidates" not in e
        assert stats["preserved"] == 1
        assert any("preserved" in w for w in warns)

    def test_state3_preserve_carries_mode_field(self):
        # ALLOWLIST rebuild round-trips MODE_FIELD (Item A output) when present.
        old = _manual(**{L.MODE_FIELD: "ratio"})
        merged, _, stats = L.merge_maps({"k": old}, {"k": _fresh_composite()})
        assert merged["k"].get(L.MODE_FIELD) == "ratio"
        assert stats["preserved"] == 1

    def test_unmarked_manual_shape_is_protected(self):
        # (4) candidates + observed_series co-present (no resolved_via/refs) is the
        # human-resolved shape → protected (preserved), rebuilt as resolved_via manual.
        old = {
            "k": {
                "pack": "p", "scope": "tenant", "direction": ">",
                "observed_series": "tenant:a_usage:max",
                "candidates": ["tenant:a_usage:max", "tenant:b_usage:max"],
            }
        }
        merged, _, stats = L.merge_maps(old, {"k": _fresh_composite()})
        assert stats["preserved"] == 1
        assert merged["k"]["resolved_via"] == "manual"
        assert merged["k"]["observed_series"] == "tenant:a_usage:max"

    def test_generated_clean_goes_composite_falls_back_not_preserved(self, tmp_path):
        # (5) regression: a pure generated (no fingerprint) resolved entry that goes
        # composite in fresh must fall back to needs_review — NOT preserved, NOT
        # promoted to manual.
        old = L.build_map([_write_pack(tmp_path, "rule-pack-foo-a.yaml", PACK_FOO_UP)])
        fresh = L.build_map([_write_pack(tmp_path, "rule-pack-foo-b.yaml", PACK_FOO_COMPOSITE)])
        assert "observed_series" in old["foo"] and not L._is_manual(old["foo"])
        merged, warns, stats = L.merge_maps(old, fresh)
        e = merged["foo"]
        assert e.get("needs_review") is True
        assert "observed_series" not in e
        assert e.get("resolved_via") != "manual"
        assert stats["preserved"] == 0 and stats["demoted"] == 0
        assert not any("foo" in w for w in warns)

    def test_explicit_manual_overridden_by_determinate_fresh(self):
        # (6) explicit manual pick differs from a now-determinate fresh series →
        # fresh wins, WARN overridden, no crash. Old refs (rationale for the now-
        # rejected pick) must NOT be carried onto the new generator pick.
        old = _manual(observed_series="tenant:old_pick:max", refs=["rationale-for-old-pick"])
        fresh = {
            "k": {
                "pack": "p", "scope": "tenant", "direction": ">",
                "observed_series": "tenant:new_pick:max",
            }
        }
        merged, warns, stats = L.merge_maps({"k": old}, fresh)
        assert merged["k"]["observed_series"] == "tenant:new_pick:max"
        assert "resolved_via" not in merged["k"]  # fresh-wins entirely
        assert "refs" not in merged["k"]  # rejected pick's rationale not carried over
        assert stats["overridden"] == 1
        assert any("overridden" in w for w in warns)

    def test_direction_flip_demotes(self):
        # (7) manual pick whose direction flipped in fresh → demote.
        old = _manual(candidates=["tenant:a_usage:max"])
        fresh = {
            "k": {
                "pack": "p", "scope": "tenant", "direction": "<",
                "candidates": ["tenant:a_usage:max"], "needs_review": True,
                "reason": "lower-bound metric",
            }
        }
        merged, _, stats = L.merge_maps({"k": old}, fresh)
        assert stats["demoted"] == 1
        assert "direction changed" in merged["k"]["reason"]

    def test_fresh_direction_ambiguous_demotes(self):
        # (8) fresh dropped its single direction (now ambiguous) → demote.
        old = _manual(candidates=["tenant:a_usage:max"])
        fresh = {
            "k": {
                "pack": "p", "scope": "tenant", "directions": ["<", ">"],
                "candidates": ["tenant:a_usage:max"], "needs_review": True,
                "reason": "ambiguous direction",
            }
        }
        merged, _, stats = L.merge_maps({"k": old}, fresh)
        assert stats["demoted"] == 1
        assert "ambiguous" in merged["k"]["reason"]

    def test_scaling_introduced_demotes(self):
        # (9) fresh now scales the observed operand → demote.
        old = _manual(candidates=["tenant:a_usage:max"])
        fresh = _fresh_composite(
            candidates=["tenant:a_usage:max"], scaled=True,
            reason="alert applies numeric scaling to observed operand",
        )
        merged, _, stats = L.merge_maps({"k": old}, {"k": fresh})
        assert stats["demoted"] == 1
        assert "numeric scaling" in merged["k"]["reason"]

    def test_revalidate_half_baked_manual_demotes_no_keyerror(self):
        # (10) a manual entry (via refs) missing observed_series must demote, not raise.
        old = {"pack": "p", "scope": "tenant", "direction": ">", "refs": ["note"]}
        merged, _, stats = L.merge_maps({"k": old}, {"k": _fresh_composite()})
        assert stats["demoted"] == 1
        assert "no observed_series" in merged["k"]["reason"]
        assert merged["k"]["refs"] == ["note"]  # refs still carried on demote

    def test_refs_null_safety_and_fresh_wins_preservation(self):
        # (11a) no old refs → no null refs field on the merged entry.
        old = {"k": {"pack": "p", "scope": "tenant", "direction": ">",
                     "observed_series": "tenant:a_usage:max"}}
        fresh = {"k": {"pack": "p", "scope": "tenant", "direction": ">",
                       "observed_series": "tenant:a_usage:max"}}
        merged, warns, stats = L.merge_maps(old, fresh)
        assert "refs" not in merged["k"]
        # (11b) generated entry carrying refs, fresh-wins path → refs preserved,
        # no override (same series).
        old2 = {"k": {"pack": "p", "scope": "tenant", "direction": ">",
                      "observed_series": "tenant:a_usage:max", "refs": ["r1"]}}
        merged2, warns2, stats2 = L.merge_maps(old2, fresh)
        assert merged2["k"]["refs"] == ["r1"]
        assert stats2["overridden"] == 0

    def test_idempotent_pin_on_committed_map(self):
        # (12) merge_maps(committed, build_map(real packs)) == committed (deep).
        committed = L.load_observed_map()  # raises when absent (#3017): never a skip
        fresh = L.build_map(L.default_pack_paths())
        merged, warns, stats = L.merge_maps(committed, fresh)
        assert merged == committed, "regen is not idempotent over the committed map"
        assert stats == {"preserved": 0, "demoted": 0, "dropped": 0, "overridden": 0}
        assert warns == []


class TestExitLock:
    def test_deferred_key_now_alert_extractable_is_error(self, tmp_path):
        # (13a) a KNOWN_DEFERRED key becoming alert-extractable → error under enforce.
        p = _write_pack(tmp_path, "rule-pack-kubernetes.yaml", PACK_DEFERRED_LEAK)
        res = L.check_consistency({}, [p], enforce_known_deferred=True)
        assert any(
            e.startswith("container_cpu:") and "alert-extractable" in e
            for e in res["errors"]
        )

    def test_deferred_key_gone_from_packs_is_error(self, tmp_path):
        # (13b) a KNOWN_DEFERRED key absent from the packs entirely → error.
        p = _write_pack(tmp_path, "rule-pack-clean.yaml", PACK_CLEAN)
        res = L.check_consistency({}, [p], enforce_known_deferred=True)
        assert any(
            e.startswith("container_cpu:") and "gone from" in e for e in res["errors"]
        )

    def test_enforce_false_leaves_hermetic_behavior_unchanged(self, tmp_path):
        # (13c) default (enforce=False) never adds exit-lock errors.
        p = _write_pack(tmp_path, "rule-pack-clean.yaml", PACK_CLEAN)
        res = L.check_consistency({}, [p], enforce_known_deferred=False)
        assert res["errors"] == []


class TestConsistencyDrift:
    def test_direction_drift_is_error(self, tmp_path):
        # check_consistency ERROR: mapped direction '>' but rule-pack flipped to '<'.
        m = L.build_map([_write_pack(tmp_path, "rule-pack-foo-up.yaml", PACK_FOO_UP)])
        p2 = _write_pack(tmp_path, "rule-pack-foo-down.yaml", PACK_FOO_DOWN)
        res = L.check_consistency(m, [p2])
        assert any("direction drift" in e and e.startswith("foo:") for e in res["errors"])

    def test_scaled_drift_is_error(self, tmp_path):
        # check_consistency ERROR: alert now scales the observed operand.
        m = L.build_map([_write_pack(tmp_path, "rule-pack-foo-up.yaml", PACK_FOO_UP)])
        p2 = _write_pack(tmp_path, "rule-pack-foo-scaled.yaml", PACK_FOO_SCALED)
        res = L.check_consistency(m, [p2])
        assert any("scales" in e and e.startswith("foo:") for e in res["errors"])


class TestStringSafety:
    """(14) threshold_govern classifies skip-reasons by substring; the strings
    THIS module actually EMITS must never collide with those markers. Bound to
    source — every assertion runs a real merge/revalidate/drift path and checks
    its output, so a future edit that injects a marker into a phrase fails here
    (not a hand-copied literal list that can silently drift out of sync).
    """

    FORBIDDEN = ("lower-bound (<)", "by-design not-applicable")

    def _assert_clean(self, produced):
        assert produced, "no strings were produced — scenario did not fire"
        for s in produced:
            for bad in self.FORBIDDEN:
                assert bad not in s, f"emitted {s!r} collides with marker {bad!r}"

    def test_revalidate_why_phrases_are_marker_free(self):
        # Drive every _revalidate failure branch for its ACTUAL `why`.
        base_fresh = {"pack": "p", "scope": "tenant", "direction": ">",
                      "candidates": ["tenant:a_usage:max"]}
        cases = [
            ({"scope": "tenant", "direction": ">"}, base_fresh),          # no series
            (_manual(observed_series="tenant:zzz:max"), base_fresh),      # not a candidate
            (_manual(), {"pack": "p", "scope": "tenant",
                         "candidates": ["tenant:a_usage:max"]}),          # dir ambiguous
            (_manual(), {**base_fresh, "direction": "<"}),                # dir changed
            (_manual(), {**base_fresh, "scaled": True}),                  # scaling introduced
            (_manual(), {**base_fresh, "scope": "tenant_version"}),       # scope changed
        ]
        whys = []
        for old_e, fresh_e in cases:
            ok, why = L._revalidate(old_e, fresh_e)
            assert ok is False and why, f"expected a failure why for {fresh_e}"
            whys.append(why)
        self._assert_clean(whys)

    def test_merge_emitted_strings_are_marker_free(self):
        produced = []
        # DROP warn
        _, w, _ = L.merge_maps({"gone": _manual(observed_series="tenant:g:max")}, {})
        produced += w
        # DEMOTE warn + merged reason (fresh reason is upper-bound → whole reason
        # is legitimately marker-free, so the full string is checkable).
        m, w, _ = L.merge_maps(
            {"k": _manual()}, {"k": _fresh_composite(candidates=["tenant:zzz:max"])}
        )
        produced += w
        produced.append(m["k"]["reason"])
        # PRESERVE warn
        _, w, _ = L.merge_maps({"k": _manual()}, {"k": _fresh_composite()})
        produced += w
        # OVERRIDE warn
        _, w, _ = L.merge_maps(
            {"k": _manual(observed_series="tenant:old:max")},
            {"k": {"pack": "p", "scope": "tenant", "direction": ">",
                   "observed_series": "tenant:new:max"}},
        )
        produced += w
        self._assert_clean(produced)

    def test_check_consistency_drift_errors_are_marker_free(self, tmp_path):
        m = L.build_map([_write_pack(tmp_path, "rule-pack-foo-up.yaml", PACK_FOO_UP)])
        p_down = _write_pack(tmp_path, "rule-pack-foo-down.yaml", PACK_FOO_DOWN)
        p_scaled = _write_pack(tmp_path, "rule-pack-foo-scaled.yaml", PACK_FOO_SCALED)
        errs = (
            L.check_consistency(m, [p_down])["errors"]
            + L.check_consistency(m, [p_scaled])["errors"]
        )
        self._assert_clean(errs)


# ---------------------------------------------------------------------------
# #916 Item A — lower-bound (<) three-state classification
# ---------------------------------------------------------------------------

# A ``<`` alert on a NOT_APPLICABLE_KEYS key (kafka_active_controllers).
PACK_NA_KEY = """
groups:
  - name: g
    rules:
      - record: tenant:kafka_active_controllers:max
        expr: max by(tenant) (kafka_active_controllers)
      - record: tenant:alert_threshold:kafka_active_controllers
        expr: max by(tenant) (user_threshold{metric="kafka_active_controllers"})
      - alert: KafkaControllersLow
        expr: |
          tenant:kafka_active_controllers:max
          < on(tenant) group_left tenant:alert_threshold:kafka_active_controllers
"""

# A ``<`` alert on the LOWER_BOUND_RESOLVED key with the dict-specified series.
PACK_RESOLVED_KEY = """
groups:
  - name: g
    rules:
      - record: tenant:db2_bufferpool_hit_ratio:min
        expr: min by(tenant) (db2_bufferpool_hit_ratio)
      - record: tenant:alert_threshold:db2_bufferpool_hit_ratio
        expr: max by(tenant) (user_threshold{metric="db2_bufferpool_hit_ratio"})
      - alert: Db2HitRatioLow
        expr: |
          tenant:db2_bufferpool_hit_ratio:min
          < on(tenant) group_left tenant:alert_threshold:db2_bufferpool_hit_ratio
"""

# Same key but the observed series was RENAMED away from the dict pick → the
# resolved classification must NOT honor it (no mode; the drift-guard fails,
# naming the stale pick — #3017).
PACK_RESOLVED_KEY_RENAMED = """
groups:
  - name: g
    rules:
      - record: tenant:db2_bufferpool_hit_ratio:avg
        expr: avg by(tenant) (db2_bufferpool_hit_ratio)
      - record: tenant:alert_threshold:db2_bufferpool_hit_ratio
        expr: max by(tenant) (user_threshold{metric="db2_bufferpool_hit_ratio"})
      - alert: Db2HitRatioLow
        expr: |
          tenant:db2_bufferpool_hit_ratio:avg
          < on(tenant) group_left tenant:alert_threshold:db2_bufferpool_hit_ratio
"""


def _lb_pack(key: str, exprs: list[str]) -> str:
    """A pack with one '<' alert per expr on threshold key ``key``."""
    rules = [f"      - record: tenant:alert_threshold:{key}\n"
             f"        expr: max by(tenant) (user_threshold{{metric=\"{key}\"}})"]
    for i, ex in enumerate(exprs):
        rules.append(f"      - alert: A{i}\n        expr: |\n          {ex}")
    return "groups:\n  - name: g\n    rules:\n" + "\n".join(rules) + "\n"


class TestLowerBoundGateOnAmbiguousAlerts:
    """#3017 審查：`<` 的分類守門不能在 alert 有歧義（composite／scaled）時消失。"""

    COMPOSITE = ["(tenant:q_a:max < on(tenant) group_left tenant:alert_threshold:{k}) or "
                 "(tenant:q_b:max < on(tenant) group_left tenant:alert_threshold:{k})"]
    SCALED = ["tenant:q_a:max * 100 < on(tenant) group_left tenant:alert_threshold:{k}"]

    @pytest.mark.parametrize("exprs", [COMPOSITE, SCALED], ids=["composite", "scaled"])
    def test_unclassified_ambiguous_lower_bound_is_error(self, tmp_path, exprs):
        p = _write_pack(tmp_path, "rule-pack-q.yaml", _lb_pack("qux", [e.format(k="qux") for e in exprs]))
        m = L.build_map([p])
        assert m["qux"].get("needs_review") is True and "observed_series" not in m["qux"]
        errs = L.check_consistency(m, [p])["errors"]
        assert any(x.startswith("qux:") and "not classified" in x for x in errs), errs

    @pytest.mark.parametrize("exprs", [COMPOSITE, SCALED], ids=["composite", "scaled"])
    def test_not_applicable_ambiguous_alert_keeps_candidates(self, tmp_path, monkeypatch, exprs):
        """composite／scaled 的 N/A key 不得把 cands[0] 當成比較序列。"""
        monkeypatch.setitem(L.NOT_APPLICABLE_KEYS, "qux", "test")
        p = _write_pack(tmp_path, "rule-pack-q.yaml", _lb_pack("qux", [e.format(k="qux") for e in exprs]))
        e = L.build_map([p])["qux"]
        assert e[L.MODE_FIELD] == "not-applicable"
        assert "observed_series" not in e and e["candidates"]
        assert L.usable_for_backtest(e)[0] is None

    def test_resolved_pick_correct_but_not_regenerated_says_regenerate(self, tmp_path, monkeypatch):
        p = _write_pack(tmp_path, "rule-pack-q.yaml", _lb_pack(
            "qux", ["tenant:q_a:min < on(tenant) group_left tenant:alert_threshold:qux"]))
        m = L.build_map([p])                       # not yet in the allowlist: no mode
        monkeypatch.setitem(L.LOWER_BOUND_RESOLVED, "qux", "tenant:q_a:min")
        errs = [x for x in L.check_consistency(m, [p])["errors"] if x.startswith("qux:")]
        assert errs and "regenerate" in errs[0] and "rename" not in errs[0], errs

    def test_regenerate_applies_a_new_classification_to_a_manual_pick(self, tmp_path, monkeypatch):
        """手動 resolve 的 composite `<` key 後來加進 NOT_APPLICABLE_KEYS：
        照守門的「regenerate」提示重產一次就要綠（mode 取 fresh 的）。"""
        p = _write_pack(tmp_path, "rule-pack-q.yaml", _lb_pack(
            "qux", [e.format(k="qux") for e in self.COMPOSITE]))
        fresh0 = L.build_map([p])
        manual = {**fresh0["qux"], "observed_series": "tenant:q_a:max", "resolved_via": "manual"}
        manual.pop("needs_review"); manual.pop("reason")
        monkeypatch.setitem(L.NOT_APPLICABLE_KEYS, "qux", "test")
        merged, _w, stats = L.merge_maps({"qux": manual}, L.build_map([p]))
        assert stats["preserved"] == 1
        assert merged["qux"][L.MODE_FIELD] == "not-applicable"
        assert [x for x in L.check_consistency(merged, [p])["errors"] if x.startswith("qux:")] == []


class TestLowerBoundClassification:
    def test_not_applicable_key_gets_mode_and_keeps_fields(self, tmp_path):
        m = L.build_map([_write_pack(tmp_path, "rule-pack-kafka.yaml", PACK_NA_KEY)])
        e = m["kafka_active_controllers"]
        assert e[L.MODE_FIELD] == "not-applicable"
        assert not e.get("needs_review")
        assert e["direction"] == "<"                    # direction kept
        # #3017: the compared series is a fact (backtest replays it); the mode
        # is recommender policy only. observed_series XOR candidates.
        assert e["observed_series"] == "tenant:kafka_active_controllers:max"
        assert "candidates" not in e
        assert L._NOT_APPLICABLE_MARKER in e["reason"]
        assert "lower-bound (<)" not in e["reason"]      # must not collide with T3 marker
        series, why = L.usable_for_recommend(e)
        assert series is None and L._NOT_APPLICABLE_MARKER in why
        assert L.usable_for_backtest(e) == ("tenant:kafka_active_controllers:max", "below", None)

    def test_resolved_key_gets_series_and_percentile_lower(self, tmp_path):
        m = L.build_map([_write_pack(tmp_path, "rule-pack-db2.yaml", PACK_RESOLVED_KEY)])
        e = m["db2_bufferpool_hit_ratio"]
        assert e[L.MODE_FIELD] == "percentile-lower"
        assert e["observed_series"] == "tenant:db2_bufferpool_hit_ratio:min"
        assert e["direction"] == "<"
        assert not e.get("needs_review")
        assert "candidates" not in e                     # resolved → series XOR candidates

    def test_resolved_series_mismatch_fails_the_drift_guard(self, tmp_path):
        # the pack renamed the series away from LOWER_BOUND_RESOLVED's pick → the
        # generator must NOT honor a stale pick (no percentile-lower mode, so the
        # recommender skips it); the fact is still recorded (#3017) and the
        # drift-guard fails loud, naming the stale pick.
        p = _write_pack(tmp_path, "rule-pack-db2.yaml", PACK_RESOLVED_KEY_RENAMED)
        m = L.build_map([p])
        e = m["db2_bufferpool_hit_ratio"]
        assert L.MODE_FIELD not in e
        assert e["observed_series"] == "tenant:db2_bufferpool_hit_ratio:avg"
        series, why = L.usable_for_recommend(e)
        assert series is None and "lower-bound (<)" in why
        errs = L.check_consistency(m, [p])["errors"]
        assert any("LOWER_BOUND_RESOLVED pick" in x and ":min" in x for x in errs), errs

    def test_not_applicable_key_without_its_mode_is_error(self, tmp_path):
        p = _write_pack(tmp_path, "rule-pack-kafka.yaml", PACK_NA_KEY)
        m = L.build_map([p])
        del m["kafka_active_controllers"][L.MODE_FIELD]
        errs = L.check_consistency(m, [p])["errors"]
        assert any("NOT_APPLICABLE_KEYS but recommendation_mode" in x for x in errs), errs

    def test_committed_map_classification_pin(self):
        # DATA-PIN: the committed map's four `<` entries carry exactly the #916
        # classification (regen output pinned so a silent drift fails here).
        m = L.load_observed_map()  # raises when absent (#3017): never a skip
        db2 = m["db2_bufferpool_hit_ratio"]
        assert db2[L.MODE_FIELD] == "percentile-lower"
        assert db2["observed_series"] == "tenant:db2_bufferpool_hit_ratio:min"
        assert not db2.get("needs_review")
        for na in ("kafka_active_controllers", "kafka_broker_count", "rabbitmq_consumers"):
            e = m[na]
            assert e[L.MODE_FIELD] == "not-applicable", na
            assert not e.get("needs_review"), na
            assert L._NOT_APPLICABLE_MARKER in e["reason"], na

    def test_regen_idempotent_over_committed_lower_bound(self):
        # merge_maps(committed, build_map(real)) == committed for the `<` entries
        # (the whole map is pinned by test_idempotent_pin_on_committed_map; this
        # narrows the failure message to the lower-bound cohort).
        committed = L.load_observed_map()  # raises when absent (#3017): never a skip
        fresh = L.build_map(L.default_pack_paths())
        merged, _w, _s = L.merge_maps(committed, fresh)
        for k in ("db2_bufferpool_hit_ratio", "kafka_active_controllers",
                  "kafka_broker_count", "rabbitmq_consumers"):
            assert merged[k] == committed[k], f"{k} not idempotent"


class TestModeMembershipDriftGuard:
    """recommendation_mode authority closure (#916 §2 point 2): a mode is only
    legal for a key in the matching allowlist — a hand-edited spoof is an ERROR."""

    def test_percentile_lower_on_non_allowlisted_key_is_error(self, tmp_path):
        p = _write_pack(tmp_path, "rule-pack-clean.yaml", PACK_CLEAN)
        m = L.build_map([p])                             # foo is a clean upper-bound key
        m["foo"][L.MODE_FIELD] = "percentile-lower"      # spoof: foo ∉ LOWER_BOUND_RESOLVED
        res = L.check_consistency(m, [p])
        assert any("foo" in e and "percentile-lower" in e for e in res["errors"])

    def test_not_applicable_on_non_allowlisted_key_is_error(self, tmp_path):
        p = _write_pack(tmp_path, "rule-pack-clean.yaml", PACK_CLEAN)
        m = L.build_map([p])
        m["foo"][L.MODE_FIELD] = "not-applicable"        # spoof: foo ∉ NOT_APPLICABLE_KEYS
        res = L.check_consistency(m, [p])
        assert any("foo" in e and "not-applicable" in e for e in res["errors"])

    def test_legit_modes_are_not_flagged(self, tmp_path):
        p = _write_pack(tmp_path, "rule-pack-db2.yaml", PACK_RESOLVED_KEY)
        m = L.build_map([p])
        res = L.check_consistency(m, [p])
        assert res["errors"] == []


class TestDefaultPackPathsNeverEmpty:
    """#1501: an empty pack list must be an error, not "no keys anywhere"."""

    def test_repo_layout_finds_the_repo_packs(self):
        paths = L.default_pack_paths()
        assert paths and all(
            os.path.basename(os.path.dirname(p)) == "rule-packs" for p in paths)

    def test_no_reachable_dir_raises(self, monkeypatch):
        monkeypatch.setattr(L, "DEFAULT_RULE_PACKS_DIR", None)
        with pytest.raises(L.RulePacksNotFoundError, match="no rule packs reachable"):
            L.default_pack_paths()

    def test_dir_without_packs_raises(self, monkeypatch, tmp_path):
        monkeypatch.setattr(L, "DEFAULT_RULE_PACKS_DIR", str(tmp_path))
        with pytest.raises(L.RulePacksNotFoundError, match="holds no"):
            L.default_pack_paths()
