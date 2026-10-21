"""`da-tools migration-audit` (#2898, ADR-036 §8).

The cases are the fixed corpus, not new hand-written files:
tests/shared/parse_tree_corpus.json (`rows`, one per kind of the closed list,
the PyYAML-only refusals, the values whose meaning changes) and every row of
merge_key_policy_corpus.json with its parse-tree category
(parse_tree_merge_key_categories.json). Each row is written to its own
directory of ONE conf.d, so one da-guard run audits them all, and every row
must land in exactly the list its category names:

* ``pyyaml_only``                         → ``becomes_readable``
* ``unreadable:<kind>``, generator reads  → ``becomes_unreadable`` (that kind)
* ``unreadable:<kind>``, generator refuses → ``still_unreadable`` (that kind)
* ``both_read``                           → none of those three

The "today" half is held to the generator: `_read_today` must give every
corpus row the verdict the corpus froze from `_parse_config_files`.

da-guard is built from this checkout (`da_guard_binary`, tests/conftest.py):
without Go these tests fail, they do not skip.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "tools" / "ops"))
sys.path.insert(0, str(REPO / "scripts" / "tools"))
sys.path.insert(0, str(REPO / "tests" / "shared"))

import migration_audit as ma  # noqa: E402
from _lib_exitcodes import EXIT_CALLER_ERROR, EXIT_OK, EXIT_VIOLATION  # noqa: E402
from test_merge_key_policy_corpus import _encode  # noqa: E402

TOOL = REPO / "scripts" / "tools" / "ops" / "migration_audit.py"
CORPUS = json.loads((REPO / "tests/shared/parse_tree_corpus.json").read_text(encoding="utf-8"))
MK_DOCS = {r["id"]: r for r in json.loads(
    (REPO / "tests/shared/merge_key_policy_corpus.json").read_text(encoding="utf-8"))["rows"]}
MK_CATS = json.loads((REPO / "tests/shared/parse_tree_merge_key_categories.json")
                     .read_text(encoding="utf-8"))["categories"]
ROWS = {r["name"]: r for r in CORPUS["rows"]}


def _cases() -> list[tuple[str, str, bytes, str, str]]:
    """(relative path in the tree, file name, bytes, category, frozen generator verdict)."""
    out = []
    for r in CORPUS["rows"]:
        out.append((f"rows/{r['name']}/{r['file']}", r["file"], _encode(r["doc"], r.get("encoding")),
                    r["category"], r["generator"]))
    for r in CORPUS["merge_key_rows"]:
        doc = MK_DOCS[r["id"]]
        out.append((f"mk/{r['id']}/_domain_policy.yaml", "_domain_policy.yaml",
                    _encode(doc["doc"], doc.get("encoding")), MK_CATS[r["id"]], r["generator"]))
    return out


CASES = _cases()


def _write(root: Path, files: dict[str, bytes]) -> Path:
    for rel, data in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return root


@pytest.fixture(scope="module")
def corpus_tree(tmp_path_factory, da_guard_binary) -> tuple[Path, dict]:
    root = _write(tmp_path_factory.mktemp("corpus-tree"), {rel: data for rel, _, data, _, _ in CASES})
    return root, ma.audit(root, da_guard_binary)


@pytest.fixture(scope="module")
def corpus_report(corpus_tree) -> dict:
    return corpus_tree[1]


def _one_row(tmp_path: Path, binary: str, name: str) -> dict:
    r = ROWS[name]
    root = _write(tmp_path / "conf.d", {r["file"]: _encode(r["doc"], r.get("encoding"))})
    return ma.audit(root, binary)


# ── today's half is the generator's ──────────────────────────────────────

def test_today_reader_gives_every_corpus_row_the_generators_verdict(tmp_path) -> None:
    wrong = []
    for i, (_, fname, data, _, generator) in enumerate(CASES):
        p = tmp_path / str(i) / fname
        p.parent.mkdir()
        p.write_bytes(data)
        _, reason = ma._read_today(p)
        got = "read" if reason is None else "rejected"
        if got != generator:
            wrong.append((str(p), generator, got, reason))
    assert not wrong, wrong[:10]
    assert len(CASES) > 1000


# ── the four lists, row by row ───────────────────────────────────────────

def check_categories(report: dict) -> None:
    lists = ("becomes_readable", "becomes_unreadable", "still_unreadable")
    where: dict[str, tuple[str, dict]] = {}
    for name in lists:
        for e in report[name]:
            assert e["file"] not in where, f"{e['file']} in two lists"
            where[e["file"]] = (name, e)
    bad = []
    for rel, _, _, category, generator in CASES:
        got = where.get(rel)
        if category == "pyyaml_only":
            want = ("becomes_readable", None)
        elif category == "both_read":
            want = None
        else:
            kind = category.removeprefix("unreadable:")
            want = ("becomes_unreadable" if generator == "read" else "still_unreadable", kind)
        if want is None:
            if got is not None:
                bad.append((rel, category, got[0]))
        elif got is None or got[0] != want[0] or (want[1] and got[1]["kind"] != want[1]):
            bad.append((rel, category, generator, got and (got[0], got[1].get("kind"))))
    assert not bad, bad[:10]
    # Every kind the corpus has, in the list its generator verdict picks.
    kinds = {e["kind"] for e in report["becomes_unreadable"]} | {e["kind"] for e in report["still_unreadable"]}
    assert set(CORPUS["text_kinds"]) <= kinds


def test_every_corpus_row_lands_in_its_list(corpus_report) -> None:
    check_categories(corpus_report)
    assert corpus_report["status"] == "ok" and corpus_report["schema"] == ma.SCHEMA
    assert corpus_report["selection"] == []
    assert corpus_report["summary"] == {**{k: len(corpus_report[k]) for k in ma.ALL_LISTS},
                                         "files_compared": len(corpus_report["files_compared"])}
    both_read = {rel for rel, _, _, cat, _ in CASES if cat == "both_read"}
    assert set(corpus_report["files_compared"]) == both_read


def test_becomes_readable_carries_what_yaml_v3_reads(corpus_report) -> None:
    by_file = {e["file"]: e for e in corpus_report["becomes_readable"]}
    tab = by_file["rows/tab-after-colon/t.yaml"]
    assert tab["yaml_v3"] == {"tenants": {"t1": {"cpu": {"text": "80", "tag": "!!int", "value": "80"}}}}
    assert "'\\t'" in tab["pyyaml_error"] and "failed to parse" in tab["pyyaml_error"]
    assert by_file["rows/invalid-calendar-timestamp/t.yaml"]["yaml_v3"] == {
        "a": {"text": "2001-13-45", "tag": "!!str", "value": "2001-13-45"}}
    assert by_file["rows/flow-merge-key-colon/t.yaml"]["yaml_v3"]["x"]["<<:"]["tag"] == "!!null"
    # The loader's full path is not in the report: the file is named by rel.
    assert all(not os.path.isabs(e["pyyaml_error"].split('"')[1]) for e in corpus_report["becomes_readable"]
               if e["pyyaml_error"].count('"') >= 2)


def test_becomes_unreadable_names_the_kind(corpus_report) -> None:
    by_file = {e["file"]: e for e in corpus_report["becomes_unreadable"]}
    assert by_file["rows/bool-tag-yes/t.yaml"]["kind"] == "decode_error"
    assert by_file["rows/escaped-slash/t.yaml"]["kind"] == "syntax_error"
    assert by_file["rows/2700-merge-tagged-key/t1.yaml"]["kind"] == "tag_not_allowed"
    assert by_file["rows/tilde-key/t.yaml"]["kind"] == "null_key"
    assert by_file["rows/nonspecific-quoted/t.yaml"]["kind"] == "non_specific_tag"
    assert by_file["rows/bom-in-value/t.yaml"]["kind"] == "bom_not_at_start"


# ── values ───────────────────────────────────────────────────────────────

def check_values(report: dict) -> None:
    got = {(e["file"], tuple(e["path"])): e for e in report["values"]}
    f = "rows/bool-words-are-strings/t.yaml"
    e = got[(f, ("tenants", "t1", "_routing", "receiver", "send_resolved"))]
    assert e["line"] == 4
    assert e["pyyaml"] == {"type": "bool", "value": True}
    assert e["yaml_v3"] == {"text": "on", "tag": "!!str", "value": "on"}
    assert e["rewrites"] == [{"meaning": "boolean", "text": "true"},
                             {"meaning": "string", "text": '"on"'}]
    assert e["suggestion"] == 'replace on — if a boolean is meant, write true; if a string is meant, write "on"'
    assert got[(f, ("tenants", "t1", "_severity_dedup"))]["line"] == 5
    assert got[(f, ("tenants", "t1", "a"))]["pyyaml"]["value"] is False
    # `b: y` is a string to both: not listed.
    assert (f, ("tenants", "t1", "b")) not in got
    e = got[("rows/octal-value-and-keys/t.yaml", ("tenants", "0777", "cpu"))]
    assert e["pyyaml"] == {"type": "str", "value": "1e3"} and e["yaml_v3"]["value"] == "1000"
    assert e["line"] == 4
    # `0777` is 511 to both, `010` a key to both: not listed.
    assert ("rows/octal-value-and-keys/t.yaml", ("tenants", "010", "cpu")) not in got
    e = got[("rows/adr036-s2-example/t010.yaml", ("tenants", "010", "_routing", "receiver", "send_resolved"))]
    assert e["line"] == 7
    # A structure the two build differently: PyYAML a mapping, yaml.v3 the string "a:".
    e = got[("rows/2759-c-flow-colon-end/_domain_policy.yaml", ("domain_policies", "fin", "tenants", 1))]
    assert e["pyyaml"]["type"] == "mapping" and e["yaml_v3"]["text"] == "a:"
    # Only files both read are compared.
    unreadable = {x["file"] for k in ("becomes_unreadable", "still_unreadable", "becomes_readable")
                  for x in report[k]}
    assert not unreadable & {x["file"] for x in report["values"]}


def test_values_read_differently(corpus_report) -> None:
    check_values(corpus_report)


@pytest.mark.parametrize("py,v3,same", [
    (True, {"text": "yes", "tag": "!!str", "value": "yes"}, False),
    ("yes", {"text": "yes", "tag": "!!str", "value": "yes"}, True),
    (8, {"text": "010", "tag": "!!int", "value": "8"}, True),
    ("010", {"text": "010", "tag": "!!int", "value": "8"}, False),
    (True, {"text": "1", "tag": "!!int", "value": "1"}, False),
    (1000.0, {"text": "1.0e+3", "tag": "!!float", "value": "1000"}, True),
    (1000, {"text": "1000.0", "tag": "!!float", "value": "1000"}, False),
    (float("-inf"), {"text": "-.inf", "tag": "!!float"}, True),
    (float("nan"), {"text": ".NaN", "tag": "!!float"}, True),
    (float("inf"), {"text": ".nan", "tag": "!!float"}, False),
    (None, {"text": "~", "tag": "!!null", "value": None}, True),
    ("~", {"text": "~", "tag": "!!null", "value": None}, False),
    (750, {"text": "12:30", "tag": "!!str", "value": "12:30"}, False),
    (1000.0, {"text": "1000", "tag": "!!int", "value": "1000"}, False),
    ("a", {"text": "b", "tag": "!!str", "value": "b"}, False),
    ("2001-12-14", {"text": "2001-12-14", "tag": "!!timestamp"}, False),
    # ⚠️ Deliberate: parse-tree gives a !!timestamp no value (ADR-036 §2:
    # times carry only text and tag), so "both read a time" is all that can
    # be compared — and both read the same source text, so a different time
    # is not a reading this tool can see. Pinned so it is a choice, not a gap.
    (__import__("datetime").date(2001, 12, 14), {"text": "2002-01-01", "tag": "!!timestamp"}, True),
])
def test_same_scalar(py, v3, same) -> None:
    assert ma.same_scalar(py, v3) is same


# ── keys ─────────────────────────────────────────────────────────────────

def test_keys_only_one_side_has() -> None:
    """No corpus file both readers read differs in a key (a key difference
    there would be a reading neither corpus has measured), so the walk is
    driven with a yaml.v3 reading that differs from PyYAML's on purpose."""
    walk = ma._Walk("t.yaml", "a:\n  x: 1\n  y: 2\nb: 3\n")
    try:
        walk.run({"a": {"x": 1, "y": 2}, "b": 3},
                 {"a": {"x": {"text": "1", "tag": "!!int", "value": "1"},
                        "z": {"text": "2", "tag": "!!int", "value": "2"}},
                  "b": {"text": "3", "tag": "!!int", "value": "3"}})
    finally:
        walk.close()
    assert walk.values == []
    assert sorted((k["key"], k["only_in"], k["line"], k["path"]) for k in walk.keys) == [
        ("y", "pyyaml", 3, ["a"]), ("z", "yaml_v3", 1, ["a"])]


def test_audit_lists_key_differences(tmp_path, da_guard_binary, monkeypatch) -> None:
    """The same through `audit`: da-guard's real document, one key renamed."""
    root = _write(tmp_path / "conf.d", {"t.yaml": b"tenants:\n  t1:\n    cpu: 80\n"})
    real = ma.run_parse_tree

    def renamed(*args, **kwargs):
        doc = real(*args, **kwargs)
        t1 = doc["files"]["t.yaml"]["tenants"]["t1"]
        t1["mem"] = t1.pop("cpu")
        return doc

    monkeypatch.setattr(ma, "run_parse_tree", renamed)
    report = ma.audit(root, da_guard_binary)
    assert [(k["file"], k["key"], k["only_in"], k["line"]) for k in report["keys"]] == [
        ("t.yaml", "mem", "yaml_v3", 2), ("t.yaml", "cpu", "pyyaml", 3)]
    assert report["summary"]["keys"] == 2 and ma.has_differences(report)


# ── selection ────────────────────────────────────────────────────────────

def test_selection_lists_a_file_only_one_side_reads(tmp_path, da_guard_binary, monkeypatch) -> None:
    """Both sides select by the same rules on every tree measured here (the
    shared walk and carrier choice), so a selection difference is driven by
    making today's selection drop one file and add another."""
    root = _write(tmp_path / "conf.d", {"a.yaml": b"x: 1\n", "b.yaml": b"y: 1\n"})
    real = ma.read_tree_today

    def fake(r: Path):
        read, refused = real(r)
        read.pop("a.yaml")
        read["ghost.yaml"] = {}
        return read, refused

    monkeypatch.setattr(ma, "read_tree_today", fake)
    report = ma.audit(root, da_guard_binary)
    assert [(e["file"], e["read_by"]) for e in report["selection"]] == [
        ("a.yaml", "yaml_v3"), ("ghost.yaml", "pyyaml")]
    assert ma.has_differences(report)


def test_selection_agrees_on_carriers_and_nested_files(tmp_path, da_guard_binary) -> None:
    root = _write(tmp_path / "conf.d", {
        "_defaults.yaml": b"x: 1\n", "_defaults.yml": b"x: 2\n",
        "team/_defaults.yml": b"y: 1\n", "team/_domain_policy.yaml": b"domain_policies: {}\n",
        "team/t.yaml": b"tenants: {t1: {cpu: 1}}\n", ".hidden/t.yaml": b"tenants: {t2: {}}\n",
        "notes.txt": b"x\n",
    })
    (root / "dir.yaml").mkdir()
    report = ma.audit(root, da_guard_binary)
    assert report["selection"] == []
    assert [(e["file"], e["kind"]) for e in report["still_unreadable"]] == [("dir.yaml", "not_regular_file")]
    assert not ma.has_differences(report)


# ── CLI: exit codes, --json, no fallback ─────────────────────────────────

def _cli(args: list[str], env_binary: str | None) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "DA_GUARD_BINARY"}
    env["PATH"] = os.pathsep.join(p for p in env.get("PATH", "").split(os.pathsep)
                                  if not (Path(p) / "da-guard").exists())
    if env_binary is not None:
        env["DA_GUARD_BINARY"] = env_binary
    return subprocess.run([sys.executable, str(TOOL), *args], capture_output=True, text=True,
                          encoding="utf-8", env=env, check=False, timeout=600)


def test_cli_exit_0_on_a_tree_both_read_the_same(tmp_path, da_guard_binary) -> None:
    root = _write(tmp_path / "conf.d", {"t.yaml": b"tenants:\n  t1:\n    cpu: 80\n    on_x: \"on\"\n"})
    proc = _cli(["--config-dir", str(root), "--json"], da_guard_binary)
    assert proc.returncode == EXIT_OK, proc.stderr
    doc = json.loads(proc.stdout)
    assert doc["schema"] == ma.SCHEMA and doc["status"] == "ok"
    assert set(doc) >= {"summary", "da_guard_version", "pyyaml_version", *ma.ALL_LISTS}
    assert doc["files_compared"] == ["t.yaml"]
    text = _cli(["--config-dir", str(root)], da_guard_binary)
    assert text.returncode == EXIT_OK and "No differences" in text.stdout
    assert "1 file(s) both read were compared value by value: t.yaml" in text.stdout


def test_cli_exit_1_and_text_report(tmp_path, da_guard_binary) -> None:
    files = {f"{n}/{ROWS[n]['file']}": _encode(ROWS[n]["doc"], None)
             for n in ("bool-words-are-strings", "tab-after-colon", "bool-tag-yes",
                       "nonspecific-slo-objective", "duplicate-key")}
    root = _write(tmp_path / "conf.d", files)
    proc = _cli(["--config-dir", str(root)], da_guard_binary)
    assert proc.returncode == EXIT_VIOLATION, proc.stderr
    out = proc.stdout
    for title in ("Values read differently", "Files read today that parse-tree refuses",
                  "Files refused today that parse-tree reads", "Files both refuse (no change)"):
        assert title in out, title
    assert "bool-words-are-strings/t.yaml:4  tenants.t1._routing.receiver.send_resolved" in out
    assert "cpu: !!int 80 (written \"80\")" in out
    js = _cli(["--config-dir", str(root), "--json"], da_guard_binary)
    assert js.returncode == EXIT_VIOLATION and json.loads(js.stdout)["summary"]["values"] == 3


def test_cli_without_da_guard_fails_and_does_not_parse_itself(tmp_path) -> None:
    root = _write(tmp_path / "conf.d", {"t.yaml": b"a: yes\n"})
    proc = _cli(["--config-dir", str(root), "--json"], str(tmp_path / "no-such-da-guard"))
    assert proc.returncode == EXIT_CALLER_ERROR
    doc = json.loads(proc.stdout)
    assert doc["status"] == "caller_error" and "not found" in doc["reason"]
    assert all(doc[k] == [] for k in ma.ALL_LISTS)
    assert "ERROR:" in proc.stderr and "does not parse the YAML another way" in proc.stderr
    proc = _cli(["--config-dir", str(root)], None)
    assert proc.returncode == EXIT_CALLER_ERROR and proc.stdout == ""


def test_caller_error_document_has_the_success_shape(tmp_path, da_guard_binary) -> None:
    # One schema version, one shape: a consumer reading .summary.X must get a
    # number on exit 2 as on exit 0/1, not a missing key.
    root = _write(tmp_path / "conf.d", {"t.yaml": b"a: yes\n"})
    ok = json.loads(_cli(["--config-dir", str(root), "--json"], da_guard_binary).stdout)
    bad = json.loads(_cli(["--config-dir", str(root), "--json"],
                          str(tmp_path / "no-such-da-guard")).stdout)
    assert bad["status"] == "caller_error"
    assert set(bad) - {"reason"} == set(ok)
    assert set(bad["summary"]) == set(ok["summary"])
    assert all(isinstance(v, int) for v in bad["summary"].values())


def test_cli_missing_config_dir(tmp_path, da_guard_binary) -> None:
    proc = _cli(["--config-dir", str(tmp_path / "nope"), "--json"], da_guard_binary)
    assert proc.returncode == EXIT_CALLER_ERROR
    assert json.loads(proc.stdout)["status"] == "caller_error"


def _fake_da_guard(tmp_path: Path, body: str) -> str:
    p = tmp_path / "fake-da-guard"
    p.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    p.chmod(0o755)
    return str(p)


_ENTRY = ('{"path": "t.yaml", "depth": 0, "class": "tenant", "tenant_named": "t", '
          '"defaults_document": false, "readers": ["exporter"], "not_read": null, '
          '"role": "config", "problem": null}')


@pytest.mark.parametrize("body,needle", [
    ('echo \'{"schema": "da-guard.parse-tree/v3", "entries": [], "files": {}}\'\n',
     "schema 'da-guard.parse-tree/v3'"),
    # A da-guard from before the per-path entries (#3206) is named as older.
    ('echo \'{"schema": "da-guard.parse-tree/v1", "selected": {}}\'\n', "older than this tool"),
    ('echo \'{"schema": "da-guard.parse-tree/v2", "mode": "tree", "defaults_carriers": {}, '
     '"files": {}, "rendered_by": {}}\'\n', "field 'entries' missing"),
    ("echo not-json\n", "wrote no document"),
    ('echo \'{"schema": "da-guard.parse-tree/v2", "mode": "tree", "defaults_carriers": {}, '
     '"entries": [' + _ENTRY + '], "files": {}, "rendered_by": {}}\'\n',
     "neither content nor a problem"),
    ('echo "flag provided but not defined" >&2; exit 2\n', "older than this tool"),
    ("exit 3\n", "exited 3"),
])
def test_cli_refuses_a_da_guard_answer_it_cannot_read(tmp_path, body, needle) -> None:
    root = _write(tmp_path / "conf.d", {"t.yaml": b"a: 1\n"})
    proc = _cli(["--config-dir", str(root), "--json"], _fake_da_guard(tmp_path, body))
    assert proc.returncode == EXIT_CALLER_ERROR, proc.stderr
    doc = json.loads(proc.stdout)
    assert doc["status"] == "caller_error" and needle in doc["reason"], doc["reason"]


# ── mutations: the checks above must see the comparison go ───────────────

def test_mutation_comparison_off_turns_the_values_check_red(tmp_path, da_guard_binary,
                                                            monkeypatch) -> None:
    monkeypatch.setattr(ma, "same_scalar", lambda py, v3: True)
    report = _one_row(tmp_path, da_guard_binary, "bool-words-are-strings")
    assert report["values"] == []
    with pytest.raises(KeyError):
        check_values(report)


def test_mutation_today_reads_everything_turns_the_category_check_red(corpus_report) -> None:
    """Folding becomes_readable into still_unreadable (one category's output
    gone) is seen by check_categories."""
    mutated = dict(corpus_report)
    mutated["still_unreadable"] = corpus_report["still_unreadable"] + [
        {**e, "kind": "syntax_error"} for e in corpus_report["becomes_readable"]]
    mutated["becomes_readable"] = []
    with pytest.raises(AssertionError):
        check_categories(mutated)


# ── round 2 (blind review of #2898) ──────────────────────────────────────

# Values the corpus has few of: integers past int64, non-finite floats, YAML
# 1.1 sexagesimals and octals, a `tenants:` list (read as source text today).
EDGE_FILES = {
    "edge/t.yaml": ("a: 1.0e+400\nb: 99999999999999999999\nc: -9223372036854775809\n"
                    "d: 0o17\ne: 12:30\nf: 1e3\ng: 190:20:30.15\nh: on\ni: Off\n"
                    "j: 18446744073709551616\n").encode(),
    "edge/_domain_policy.yaml": (b"domain_policies:\n  fin:\n"
                                 b"    tenants: [010, 1e3, 0777, ~, yes, t1, 99999999999999999999]\n"),
}

def test_every_rewrite_makes_both_readers_agree(tmp_path, corpus_tree, da_guard_binary) -> None:
    """The root fix for wrong suggestions: each spelling offered is written
    back in place of the value, the file is audited again alone, and that
    value must no longer differ (and the file must still be read by both)."""
    root, report = corpus_tree
    edge_root = _write(tmp_path / "edge-tree", EDGE_FILES)
    edge_report = ma.audit(edge_root, da_guard_binary)
    entries = [(root, e) for e in report["values"]] + [(edge_root, e) for e in edge_report["values"]]
    tried, bad = 0, []
    for n, (base, e) in enumerate(entries):
        text = e["yaml_v3"]["text"] if e["yaml_v3"] and "text" in e["yaml_v3"] else None
        if not e["rewrites"]:
            assert text is None or e["pyyaml"]["type"] in ("mapping", "sequence"), e
            continue
        original = (base / e["file"]).read_bytes()
        span = e.get("span")  # every entry with a spelling says where the value is written
        assert span, e
        assert span["line"] == e["line"] and e["suggestion"].startswith(f"replace {span['written']} ")
        for j, opt in enumerate(e["rewrites"]):
            tree = tmp_path / f"rw{n}-{j}"
            lines = original.decode("utf-8").split("\n")
            src = lines[span["line"] - 1]
            lines[span["line"] - 1] = src[:span["start"]] + opt["text"] + src[span["end"]:]
            _write(tree, {e["file"]: "\n".join(lines).encode("utf-8")})
            again = ma.audit(tree, da_guard_binary)
            tried += 1
            left = [x for x in again["values"] if x["path"] == e["path"]]
            if left or e["file"] not in again["files_compared"]:
                bad.append((e["file"], e["path"], opt, left[:1] or again["becomes_unreadable"][:1]))
    assert not bad, bad
    assert tried >= 40, tried
    # No lossy spelling is offered: past-int64 integers and `tenants:` items
    # get the quoted string only; a non-finite float gets `.inf`.
    by = {(e["file"], tuple(e["path"])): e for e in edge_report["values"]}
    for key in (("edge/t.yaml", ("b",)), ("edge/t.yaml", ("c",)), ("edge/t.yaml", ("j",)),
                ("edge/_domain_policy.yaml", ("domain_policies", "fin", "tenants", 1)),
                ("edge/_domain_policy.yaml", ("domain_policies", "fin", "tenants", 6))):
        assert [o["meaning"] for o in by[key]["rewrites"]] == ["string"], (key, by[key]["rewrites"])
    assert {"meaning": "number", "text": ".inf"} in by[("edge/t.yaml", ("a",))]["rewrites"]
    assert all("inf" != o["text"] for e in edge_report["values"] for o in e["rewrites"])


def test_tenants_list_items_are_compared_by_value(tmp_path, da_guard_binary) -> None:
    """ADR-036 §2: the semantic layer reads type and value, so a policy's
    `tenants: [010, 1e3, 0777]` differs from today's source text."""
    root = _write(tmp_path / "conf.d", {"_domain_policy.yaml":
                  b"domain_policies:\n  fin:\n    tenants: [010, 1e3, 0777, t1]\n"})
    got = [(e["path"][-1], e["pyyaml"]["value"], e["yaml_v3"]["tag"])
           for e in ma.audit(root, da_guard_binary)["values"]]
    assert sorted(got) == [(0, "010", "!!int"), (1, "1e3", "!!float"), (2, "0777", "!!int")]


def test_lines_of_aliased_and_merged_values(tmp_path, da_guard_binary) -> None:
    """A value reached through an alias or a merge is reported at its
    anchor's / the merged mapping's line — the one line to change."""
    root = _write(tmp_path / "conf.d", {"t.yaml": (
        b"x: &b on\n"            # 1
        b"m: &m {k: off}\n"      # 2
        b"t:\n"                  # 3
        b"  a: *b\n"             # 4
        b"  c:\n"                # 5
        b"    <<: *m\n"          # 6
        b"  l: [*b]\n"           # 7
        b"  v:\n"                # 8
        b"    yes\n")})          # 9
    got = {tuple(e["path"]): e["line"] for e in ma.audit(root, da_guard_binary)["values"]}
    assert got == {("x",): 1, ("m", "k"): 2, ("t", "a"): 1, ("t", "c", "k"): 2,
                   ("t", "l", 0): 1, ("t", "v"): 9}


@pytest.mark.parametrize("doc,needle", [
    ('{"entries": [1]}', "entries[0] is not an object"),
    ('{"entries": [{"path": "t.yaml"}]}', "entries[0] ('t.yaml'): no depth"),
    ('{"entries": [{"path": "t.yaml", "depth": 0, "class": "tenant", "tenant_named": "t", '
     '"defaults_document": false, "readers": "exporter", "not_read": null, "role": "config", '
     '"problem": null}]}', "readers is not a list"),
    ('{"entries": [{"path": "t.yaml", "depth": 0, "class": "tenant", "tenant_named": "t", '
     '"defaults_document": false, "readers": ["exporter"], "not_read": null, "role": "maybe", '
     '"problem": null}]}', "role 'maybe'"),
    ('{"files": {"t.yaml": [1]}}', "files['t.yaml'] is not a mapping"),
    ('{"files": {"t.yaml": {"a": 1}}}', "wrong shape"),
    ('{"files": {"t.yaml": {"a": {"text": "x", "tag": "!!str", "value": [1]}}}}', "wrong shape"),
    ('{"files": {"t.yaml": {"a": {"text": "x"}}}}', "wrong shape"),
])
def test_cli_refuses_a_parse_tree_document_of_the_wrong_shape(tmp_path, doc, needle) -> None:
    base = {"schema": "da-guard.parse-tree/v2", "mode": "tree", "defaults_carriers": {},
            "entries": [json.loads(_ENTRY)], "files": {"t.yaml": {}}, "rendered_by": {}}
    patch = json.loads(doc)
    base.update(patch)
    if "entries" in patch and "files" not in patch:
        base["files"] = {}
    root = _write(tmp_path / "conf.d", {"t.yaml": b"a: 1\n"})
    fake = _fake_da_guard(tmp_path, "cat <<'JSON'\n" + json.dumps(base) + "\nJSON\n")
    proc = _cli(["--config-dir", str(root), "--json"], fake)
    assert proc.returncode == EXIT_CALLER_ERROR, proc.stderr
    out = json.loads(proc.stdout)
    assert out["status"] == "caller_error" and needle in out["reason"], out["reason"]
    assert "Traceback" not in proc.stderr


@pytest.mark.parametrize("value", ["nan", "inf", "-inf", "0", "-1", "abc"])
def test_timeout_must_be_finite_and_positive(tmp_path, value) -> None:
    proc = _cli(["--config-dir", str(tmp_path), "--timeout", value], None)
    assert proc.returncode == EXIT_CALLER_ERROR
    assert "--timeout" in proc.stderr and "Traceback" not in proc.stderr


def test_an_unexpected_failure_is_exit_2_without_a_traceback(tmp_path, monkeypatch, capsys) -> None:
    def boom(*a, **k):
        raise RuntimeError("something\nbroke")
    monkeypatch.setattr(ma, "audit", boom)
    rc = ma.main(["--config-dir", str(tmp_path), "--json"])
    out, err = capsys.readouterr()
    assert rc == EXIT_CALLER_ERROR
    doc = json.loads(out)
    assert doc["status"] == "caller_error"
    assert doc["reason"] == "the audit could not finish: RuntimeError: something broke"
    assert "Traceback" not in err and err.startswith("ERROR: the audit could not finish")


def test_deep_content_renders_as_text(tmp_path, da_guard_binary) -> None:
    """A file only yaml.v3 reads, nested 3,000 deep (a TAB keeps PyYAML out):
    the text report lists it, no RecursionError."""
    root = _write(tmp_path / "conf.d", {"t.yaml": ("a:\t" + "[" * 3000 + "]" * 3000 + "\n").encode()})
    proc = _cli(["--config-dir", str(root)], da_guard_binary)
    assert proc.returncode == EXIT_VIOLATION, proc.stderr[-500:]
    assert "(depth 2999) - [0] []" in proc.stdout


def test_missing_da_guard_message_keeps_its_lines(tmp_path) -> None:
    root = _write(tmp_path / "conf.d", {"t.yaml": b"a: 1\n"})
    proc = _cli(["--config-dir", str(root)], str(tmp_path / "none"))
    assert proc.returncode == EXIT_CALLER_ERROR
    lines = proc.stderr.splitlines()
    assert lines[0].startswith("ERROR: ") and "Error:" not in lines[0], lines[0]
    assert any(ln.startswith("  This audit reads yaml.v3") for ln in lines[1:]), lines


def test_today_selection_is_the_generators(tmp_path, monkeypatch) -> None:
    """Differential: the files `_grar_parse._parse_config_files` actually
    reads (through `_lib_parse_cache.ConfdReader.read` since ADR-036 step 4),
    against `read_tree_today`. If the generator's file selection moves, this
    turns red. Known, listed difference:

    * `dir.yaml` — a directory with a config name: no loader opens it; both
      book it as refused (the generator via `files_skipped`).
    """
    import _grar_parse
    import _lib_parse_cache
    root = _write(tmp_path / "conf.d", {
        "t.yaml": b"tenants: {t1: {cpu: 1}}\n", "Upper.YAML": b"tenants: {t2: {cpu: 1}}\n",
        "_defaults.yaml": b"defaults: {cpu: 1}\n", "_DEFAULTS.yml": b"defaults: {cpu: 2}\n",
        "sub/x.yml": b"tenants: {t3: {cpu: 1}}\n", "sub/_defaults.yaml": b"defaults: {cpu: 3}\n",
        "sub/_defaults.yml": b"_routing_enforced: {enabled: true}\n",
        "sub/_domain_policy.yaml": b"domain_policies: {}\n", "bad.yaml": b"a: [\n",
        "latin.yaml": "# \u00e9\n".encode("latin-1"), ".hidden/h.yaml": b"tenants: {}\n",
        "notes.txt": b"x\n", "sub/deep/_routing_profiles.yml": b"routing_profiles: {}\n",
    })
    (root / "dir.yaml").mkdir()
    opened: list[str] = []
    real = _lib_parse_cache.ConfdReader.read

    def spy(self, path, *a, **k):  # every conf.d read of the generator
        opened.append(Path(path).relative_to(root).as_posix())
        return real(self, path, *a, **k)

    monkeypatch.setattr(_lib_parse_cache.ConfdReader, "read", spy)
    res = _grar_parse._parse_config_files(str(root))
    # tenant_declarations / declared_tenant_ids read the tenant files again
    opened = sorted(set(opened))
    read, refused = ma.read_tree_today(root)
    expected_refused_unopened = {"dir.yaml"}
    assert set(opened) == (set(read) | set(refused)) - expected_refused_unopened
    # #2906: the unselected carrier below the root (it writes
    # `_routing_enforced`) is no longer opened — before, the generator read
    # it to refuse that key (`_enforced_in_unread_file`, removed); now it is
    # an `unselected_defaults_carrier` finding whatever it holds.
    assert "sub/_defaults.yml" not in opened
    assert {"_DEFAULTS.yml", "sub/_defaults.yml"} <= {
        f.file for f in res["unselected_carriers"]}
    assert set(refused) == {f for f, _ in res["files_skipped"]}
    assert set(refused) == {"bad.yaml", "latin.yaml", "dir.yaml"}
