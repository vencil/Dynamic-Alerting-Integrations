"""check_exporter_capture.py — a pack's raw selectors against real /metrics scrapes (#3833).

Pinned contracts
----------------
1. STATIC: every registry pack is in exactly one of `exporters:`, `queries:`,
   COVERED_ELSEWHERE, UNVERIFIED. A pack in none fails (a new pack cannot be
   added unchecked and unlisted); a pack that is checked AND listed fails (the
   ledger only shrinks); a row for a pack the registry does not have fails.
   kubernetes, oracle and db2 are covered by a stated rule, not by omission.
2. A selector is served when Prometheus' own matcher selects a captured
   series (da-read): the family name alone is not enough — a matcher that
   matches nothing is a finding, and it says which of the three it is (family
   in no capture / label on no series / no series matches) and where the
   family was seen.
3. `A or B` is served by either side; the side nothing serves must be under
   `unchecked_alternatives`, and a stale entry there fails.
4. Every capture serves a presence selector.
5. The captures given are exactly the ones the registry entry asks for, and a
   file that is not a scrape is exit 2, not a finding.
6. The committed pack against a scrape of the pinned exporter
   (tests/lint/fixtures/exporter_capture/, taken from postgres_exporter
   v0.21.1 on PostgreSQL 16.15 — see its README) has no finding; without the
   registry's flag, or with the pack reading only the old lag name, it has.

⚠️ What this does NOT cover: that the fixture scrapes are what the pinned
IMAGES serve — only the CI job (scripts/ops/exporter_capture_runtime.sh) runs
those. The fixtures are a reduced copy of a local run, kept so the checker's
logic is tested where docker is not.
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/tools/lint"))
sys.path.insert(0, str(ROOT / "scripts/tools/ops"))
import _registry_lib as registry_lib  # noqa: E402
import check_exporter_capture as cec  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures" / "exporter_capture"
DOC = registry_lib.committed_registry_doc()


# ── 1. the static half ─────────────────────────────────────────────────────

def test_every_registry_pack_is_checked_covered_or_listed() -> None:
    assert cec.coverage_problems(DOC) == []
    assert cec.driver_problems(DOC) == []


def test_the_three_ways_of_being_covered_are_each_in_use() -> None:
    """Not by omission: each pack's coverage is one stated thing."""
    packs = DOC["packs"]
    assert "postgresql" in registry_lib.pack_exporters(DOC)
    assert {p for p, m in packs.items() if m.get("queries")} == {"oracle", "db2"}
    assert set(cec.COVERED_ELSEWHERE) == {"kubernetes"}
    assert set(packs) == (set(registry_lib.pack_exporters(DOC)) | {"oracle", "db2"}
                          | set(cec.COVERED_ELSEWHERE) | set(cec.UNVERIFIED))


def _doc(**edits) -> dict:
    doc = copy.deepcopy(DOC)
    for pack, meta in edits.items():
        if meta is None:
            del doc["packs"][pack]
        else:
            doc["packs"].setdefault(pack, {}).update(meta)
    return doc


def test_a_pack_neither_checked_nor_listed_fails() -> None:
    doc = _doc()
    del doc["packs"]["postgresql"]["exporters"]          # out of the structured field …
    problems = cec.coverage_problems(doc)                # … and (already) not in the ledger
    assert len(problems) == 1 and "'postgresql' is not checked" in problems[0]
    new = cec.coverage_problems(_doc(newpack={"display": "x", "exporter": "x"}))
    assert len(new) == 1 and "'newpack' is not checked" in new[0]


def test_a_ledger_row_for_a_checked_pack_fails() -> None:
    problems = cec.coverage_problems(DOC, unverified={**cec.UNVERIFIED, "postgresql": "still listed"})
    assert len(problems) == 1 and "STALE-EXEMPTION" in problems[0] and "'postgresql'" in problems[0]
    queries = cec.coverage_problems(DOC, unverified={**cec.UNVERIFIED, "db2": "still listed"})
    assert len(queries) == 1 and "'db2'" in queries[0]
    covered = cec.coverage_problems(DOC, covered={**cec.COVERED_ELSEWHERE, "postgresql": "x"})
    assert len(covered) == 1 and "'postgresql'" in covered[0]


def test_a_ledger_row_for_no_pack_or_without_a_reason_fails() -> None:
    assert any("not a registry pack" in p for p in
               cec.coverage_problems(DOC, unverified={**cec.UNVERIFIED, "gone": "x"}))
    assert any("has no reason" in p for p in
               cec.coverage_problems(DOC, unverified={**cec.UNVERIFIED, "redis": " "}))


def test_a_driver_without_exporters_and_exporters_without_a_driver_fail(tmp_path) -> None:
    drivers = ROOT / "scripts/ops/exporter_capture"
    doc = _doc()
    del doc["packs"]["postgresql"]["exporters"]
    assert any("postgresql.sh is a driver for a pack without" in p
               for p in cec.driver_problems(doc, drivers))
    doc = _doc(redis={"exporters": DOC["packs"]["postgresql"]["exporters"]})
    assert any("'redis' has `exporters:` but no driver" in p for p in cec.driver_problems(doc, drivers))


# ── the registry gate: `exporter:` is derived from `exporters:` ────────────

def test_the_exporter_line_is_the_entries_names() -> None:
    assert registry_lib.exporters_problems(DOC) == []
    doc = _doc(postgresql={"exporter": "some other text"})
    assert any("exporter is 'some other text'" in p for p in registry_lib.exporters_problems(doc))
    doc = _doc(redis={"unchecked_alternatives": [{"metric": "redis_x", "reason": "r"}]})
    assert any("without exporters" in p for p in registry_lib.exporters_problems(doc))
    doc = _doc(postgresql={"unchecked_alternatives": [{"metric": "mysql_x", "reason": "r"}]})
    assert any("under none of the pack's metric_prefixes" in p
               for p in registry_lib.exporters_problems(doc))


def test_the_schema_refuses_an_unpinned_or_unknown_shaped_entry() -> None:
    assert registry_lib.validate_registry(DOC) == []
    entry = DOC["packs"]["postgresql"]["exporters"][0]
    for edit in ({"digest": "sha256:abc"}, {"tag": "latest tag"}, {"flags": ["collector.x"]},
                 {"states": []}, {"server": {"image": "postgres", "tag": "16"}}, {"note": "x"}):
        bad = _doc(postgresql={"exporters": [{**entry, **edit}]})
        assert registry_lib.validate_registry(bad), edit
    no_flags = {k: v for k, v in entry.items() if k != "flags"}
    assert registry_lib.validate_registry(_doc(postgresql={"exporters": [no_flags]}))


def test_the_pack_header_states_version_flags_and_states() -> None:
    entry = DOC["packs"]["postgresql"]["exporters"][0]
    text = "\n".join(registry_lib.render_exporter_capture_lines(DOC, "postgresql"))
    for want in (entry["name"], entry["tag"], entry["digest"], *entry["flags"],
                 entry["server"]["tag"], *entry["states"], "pg_replication_lag"):
        assert want in text, want
    assert registry_lib.render_exporter_capture_lines(DOC, "redis") == []
    header = (ROOT / DOC["packs"]["postgresql"]["rule_pack_file"]).read_text(encoding="utf-8")
    assert text in header


# ── the exposition reader ──────────────────────────────────────────────────

def test_parse_exposition_reads_names_labels_and_escapes() -> None:
    text = ('# HELP a x\n# TYPE a gauge\na 1\n'
            'b{l="v",m="q\\"uo\\\\te\\nnl"} 2.5 1700000000000\n'
            'c_bucket{le="+Inf"} 3\n\n')
    assert cec.parse_exposition(text) == [
        ("a", {}), ("b", {"l": "v", "m": 'q"uo\\te\nnl'}), ("c_bucket", {"le": "+Inf"})]
    assert cec.series_text("b", {"m": 'q"uo\\te\nnl', "l": "v"}) == 'b{l="v",m="q\\"uo\\\\te\\nnl"}'
    with pytest.raises(cec.CannotRun):
        cec.parse_exposition("<html>not a scrape</html>\n")


def test_the_captures_must_be_the_ones_the_entry_asks_for(tmp_path) -> None:
    scrape = tmp_path / "s.prom"
    scrape.write_text("pg_up 1\n", encoding="utf-8")
    want = ["0:primary", "0:replica"]
    assert sorted(cec.read_captures([f"0:primary={scrape}", f"0:replica={scrape}"], want)) == want
    for specs in ([f"0:primary={scrape}"], [f"0:primary={scrape}", f"0:other={scrape}"],
                  [f"0:primary={scrape}", f"0:primary={scrape}"], ["0:primary"]):
        with pytest.raises(cec.CannotRun):
            cec.read_captures(specs, want)
    empty = tmp_path / "e.prom"
    empty.write_text("# HELP only\n", encoding="utf-8")
    with pytest.raises(cec.CannotRun, match="no sample line"):
        cec.read_captures([f"0:primary={empty}", f"0:replica={scrape}"], want)
    with pytest.raises(cec.CannotRun, match="cannot read"):
        cec.read_captures([f"0:primary={tmp_path / 'missing'}", f"0:replica={scrape}"], want)


# ── 2–4. findings, on a small synthetic pack ───────────────────────────────

_SYNTH_DOC = {"packs": {"demo": {
    "rule_pack_file": "unused", "metric_prefixes": ["demo_"], "presence": ["demo_up"]}}}


def _findings(tmp_path, exprs: list[str], captures: dict[str, str], **meta) -> list[str]:
    rules = "".join(f"      - record: 'r:rule{i}'\n        expr: '{e}'\n" for i, e in enumerate(exprs))
    pack = tmp_path / "rule-pack-demo.yaml"
    pack.write_text("groups:\n  - name: g\n    rules:\n" + rules, encoding="utf-8")
    doc = copy.deepcopy(_SYNTH_DOC)
    doc["packs"]["demo"].update(meta)
    return cec.capture_findings(
        "demo", doc, {cid: cec.parse_exposition(text) for cid, text in captures.items()},
        rule_pack=pack)


_UP = "demo_up 1\n"


def test_a_served_selector_and_a_foreign_one_are_no_finding(tmp_path, da_read_env) -> None:
    out = _findings(tmp_path,
                    ['sum(rate(demo_total{mode=~"a|b"}[5m])) / on() user_threshold{x="y"}',
                     "tenant:other:record > 0",
                     # a label the series lacks is "" to Prometheus
                     'demo_total{shard=""}', 'demo_total{shard!="1"}', 'demo_total{shard=~"|1"}'],
                    {"0:a": _UP + 'demo_total{mode="b"} 1\n'})
    assert out == []


def test_each_kind_of_missing_is_named_with_where_the_family_was_seen(tmp_path, da_read_env) -> None:
    out = _findings(
        tmp_path,
        ["demo_gone", 'demo_total{mode="zzz"}', 'demo_total{shard="1"}', 'rate(demo_total{mode!~".*"}[5m])'],
        {"0:a": _UP + 'demo_total{mode="b"} 1\n', "0:b": _UP})
    assert len(out) == 4, out
    assert "rule r:rule0 reads demo_gone — the family demo_gone is in no capture (0:a, 0:b)" in out[0]
    assert ('rule r:rule1 reads demo_total{mode="zzz"} — no series of demo_total matches the matchers; '
            "the family was seen in 0:a") in out[1]
    assert "no series of demo_total carries the label shard; the family was seen in 0:a" in out[2]
    assert 'reads demo_total{mode!~".*"}[5m]' in out[3]


def test_a_series_in_any_one_capture_serves_the_selector(tmp_path, da_read_env) -> None:
    captures = {"0:a": _UP, "0:b": _UP + 'demo_lag{role="standby"} 3\n'}
    assert _findings(tmp_path, ['demo_lag{role="standby"}'], captures) == []


def test_or_is_served_by_either_side_and_the_other_must_be_listed(tmp_path, da_read_env) -> None:
    captures = {"0:a": _UP + "demo_new 1\n"}
    alt = [{"metric": "demo_old", "reason": "the older name"}]
    expr = ["max by(tenant) (demo_new or demo_old)"]
    assert _findings(tmp_path, expr, captures, unchecked_alternatives=alt) == []
    unlisted = _findings(tmp_path, expr, captures)
    assert len(unlisted) == 1 and "reads demo_old as one side of an `or`" in unlisted[0]
    # neither side served: both are findings, and the listed entry excuses nothing
    neither = _findings(tmp_path, ["demo_x or demo_old"], captures, unchecked_alternatives=alt)
    assert sum("is in no capture" in f for f in neither) == 2
    # `and` / arithmetic need both sides
    both = _findings(tmp_path, ["demo_new / demo_old"], captures)
    assert len(both) == 1 and "reads demo_old — the family demo_old is in no capture" in both[0]


def test_a_stale_unchecked_alternative_fails(tmp_path, da_read_env) -> None:
    alt = [{"metric": "demo_old", "reason": "the older name"}]
    served = _findings(tmp_path, ["demo_new or demo_old"],
                       {"0:a": _UP + "demo_new 1\ndemo_old 1\n"}, unchecked_alternatives=alt)
    assert len(served) == 1 and "STALE-EXEMPTION" in served[0]
    unread = _findings(tmp_path, ["demo_new"], {"0:a": _UP + "demo_new 1\n"}, unchecked_alternatives=alt)
    assert len(unread) == 1 and "STALE-EXEMPTION" in unread[0]


def test_every_capture_must_serve_a_presence_selector(tmp_path, da_read_env) -> None:
    out = _findings(tmp_path, ["demo_new"], {"0:a": _UP + "demo_new 1\n", "0:b": "demo_new 1\n"})
    assert len(out) == 1 and "capture 0:b serves none of the pack's presence selectors" in out[0]


def test_a_pack_without_prefixes_cannot_be_judged(tmp_path, da_read_env) -> None:
    with pytest.raises(cec.CannotRun, match="metric_prefixes"):
        _findings(tmp_path, ["demo_new"], {"0:a": _UP}, metric_prefixes=[])


# ── 6. the committed pack against scrapes of the pinned exporter ───────────

def _fixture(name: str) -> list:
    return cec.parse_exposition((FIXTURES / name).read_text(encoding="utf-8"))


def _pinned(flagged: bool = True) -> dict:
    suffix = "" if flagged else ".no-flags"
    return {"0:primary": _fixture(f"postgresql.primary{suffix}.prom"),
            "0:replica": _fixture(f"postgresql.replica{suffix}.prom")}


def test_the_postgresql_pack_is_served_by_the_pinned_exporter(da_read_env) -> None:
    assert cec.capture_findings("postgresql", DOC, _pinned()) == []


def test_without_the_registrys_flag_the_restart_alert_has_no_series(da_read_env) -> None:
    """The flag in the registry entry is load-bearing: scrapes of the same
    exporter started without it lack the postmaster collector's series."""
    assert DOC["packs"]["postgresql"]["exporters"][0]["flags"] == ["--collector.postmaster"]
    out = cec.capture_findings("postgresql", DOC, _pinned(flagged=False))
    assert len(out) == 1, out
    assert ("rule PostgreSQLRecentRestart reads pg_postmaster_start_time_seconds — the family "
            "pg_postmaster_start_time_seconds is in no capture (0:primary, 0:replica)") in out[0]


def test_reading_only_the_old_lag_name_is_a_finding(tmp_path, da_read_env) -> None:
    """The pack before #4354: the pinned exporter serves no pg_replication_lag."""
    pack = ROOT / DOC["packs"]["postgresql"]["rule_pack_file"]
    text = pack.read_text(encoding="utf-8")
    new = "pg_replication_lag_seconds or pg_replication_lag\n"
    assert text.count(new) == 1
    old = tmp_path / pack.name
    old.write_text(text.replace(new, "pg_replication_lag\n"), encoding="utf-8")
    out = cec.capture_findings("postgresql", DOC, _pinned(), rule_pack=old)
    assert any("rule tenant:pg_replication_lag:max reads pg_replication_lag — the family "
               "pg_replication_lag is in no capture" in f for f in out), out
    # … and the registry's unchecked alternative is then stale, which is also said
    assert any("STALE-EXEMPTION" in f for f in out)
