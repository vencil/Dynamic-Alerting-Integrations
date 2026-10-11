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
def corpus_report(tmp_path_factory, da_guard_binary) -> dict:
    root = _write(tmp_path_factory.mktemp("corpus-tree"), {rel: data for rel, _, data, _, _ in CASES})
    return ma.audit(root, da_guard_binary)


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
    assert "write true" in e["suggestion"] and 'quote it: "on"' in e["suggestion"]
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


# ── the exporter's two changes ───────────────────────────────────────────

def check_exporter_changes(report: dict) -> None:
    got = {(e["file"], e["change"]): e for e in report["exporter_changes"]}
    e = got[("rows/nonspecific-merge-key-alias/t.yaml", "merge_key")]
    assert e["line"] == 3 and e["path"] == ["tenants", "t1"]
    e = got[("rows/nonspecific-slo-objective/t.yaml", "slo_objective")]
    assert e["line"] == 6 and e["path"] == ["tenants", "t1", "_custom_alerts", 0, "slo"]
    assert '"99.9"' in e["after"]
    # `! "x"` (not a key `<<`, not an objective) and `! 123` (plain: the
    # patch does not touch it) change nothing in the exporter.
    files = {f for f, _ in got}
    assert "rows/nonspecific-quoted/t.yaml" not in files
    assert "rows/nonspecific-plain/t.yaml" not in files
    # A nested `_` file is the generator's only: the exporter never reads it.
    assert not any(f.startswith("mk/") for f in files)
    # parse-tree refuses both files: they are in becomes_unreadable too.
    bu = {x["file"]: x["kind"] for x in report["becomes_unreadable"]}
    assert bu["rows/nonspecific-merge-key-alias/t.yaml"] == "non_specific_tag"
    assert bu["rows/nonspecific-slo-objective/t.yaml"] == "non_specific_tag"


def test_exporter_changes(corpus_report) -> None:
    check_exporter_changes(corpus_report)


def test_exporter_changes_block_and_single_quoted() -> None:
    text = ("a: &a {cpu: 1}\nt: {! '<<': *a}\nalerts:\n- slo:\n    objective: ! |\n      99.9\n"
            "- slo: {objective: ! 99.9}\n")
    got = [(e["change"], e["line"]) for e in ma.exporter_changes("t.yaml", text)]
    assert got == [("merge_key", 2), ("slo_objective", 5)]


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
                  "Files refused today that parse-tree reads", "Exporter readings that change",
                  "Files both refuse (no change)"):
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


def test_cli_missing_config_dir(tmp_path, da_guard_binary) -> None:
    proc = _cli(["--config-dir", str(tmp_path / "nope"), "--json"], da_guard_binary)
    assert proc.returncode == EXIT_CALLER_ERROR
    assert json.loads(proc.stdout)["status"] == "caller_error"


def _fake_da_guard(tmp_path: Path, body: str) -> str:
    p = tmp_path / "fake-da-guard"
    p.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    p.chmod(0o755)
    return str(p)


@pytest.mark.parametrize("body,needle", [
    ('echo \'{"schema": "da-guard.parse-tree/v2", "selected": {}, "files": {}, '
     '"unreadable": [], "not_read": []}\'\n', "schema 'da-guard.parse-tree/v2'"),
    ('echo \'{"schema": "da-guard.parse-tree/v1", "selected": {}}\'\n', "field 'files'"),
    ("echo not-json\n", "wrote no document"),
    ('echo \'{"schema": "da-guard.parse-tree/v1", "selected": {"t.yaml": ["exporter"]}, '
     '"files": {}, "unreadable": [], "not_read": []}\'\n', "is not \"files\" plus"),
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


def test_mutation_exporter_changes_off_turns_its_check_red(tmp_path, da_guard_binary,
                                                           monkeypatch) -> None:
    monkeypatch.setattr(ma, "exporter_changes", lambda rel, text: [])
    report = _one_row(tmp_path, da_guard_binary, "nonspecific-slo-objective")
    assert report["exporter_changes"] == []
    with pytest.raises(KeyError):
        check_exporter_changes(report)


def test_mutation_today_reads_everything_turns_the_category_check_red(corpus_report) -> None:
    """Folding becomes_readable into still_unreadable (one category's output
    gone) is seen by check_categories."""
    mutated = dict(corpus_report)
    mutated["still_unreadable"] = corpus_report["still_unreadable"] + [
        {**e, "kind": "syntax_error"} for e in corpus_report["becomes_readable"]]
    mutated["becomes_readable"] = []
    with pytest.raises(AssertionError):
        check_categories(mutated)
