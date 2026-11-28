"""Self-test for check_retired_keys_not_taught.py (#1200).

A key under the registry's ``retired_keys:`` may be named only where the job
is to talk about retirements (the registry, the changelogs, tests) or on a
line that says so with a ``retired-key-mention:`` marker. The registry is the
only list: these tests make their own registry and never name a shipped key.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "tools" / "lint"))
sys.path.insert(0, str(REPO / "tests"))

import check_retired_keys_not_taught as lint  # noqa: E402
from _platform_fs import symlink_or_skip  # noqa: E402

KEY = "zz_old_count"
OTHER = "zz_gone_bytes"
CRIT = "zz_gone_bytes_critical"
DESC = f"Share of the limit. Replaces the absolute key {KEY} (\"a unit change\")"

REGISTRY = f"""version: 1
packs:
  zz:
    display: ZZ
    exporter: zz_exporter
    default_on: false
    rule_pack_file: rule-packs/rule-pack-zz.yaml
keys:
  zz_usage_percent:
    pack: zz
    value: 80
    unit: '%'
    desc: '{DESC}'
    metric_class: saturation
    arming: operator
    portability: portable
deprecated_aliases: {{}}
retired_keys:
  {KEY}:
    replaced_by: zz_usage_percent
    tracking: 1
    hint: "converted to a share"
  {OTHER}:
    replaced_by: null
    tracking: 2
    hint: "nothing reads it"
  {CRIT}:
    replaced_by: null
    tracking: 2
    hint: "no critical tier"
"""


def _tree(tmp_path: Path, files: dict, registry: str | None = REGISTRY) -> "tuple[Path, list]":
    files = dict(files)
    if registry is not None:
        files[lint.REGISTRY_REL] = registry
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(text, bytes):
            p.write_bytes(text)
        else:
            p.write_text(text, encoding="utf-8")
    return tmp_path, sorted(files)


def _check(tmp_path: Path, files: dict, registry: str | None = REGISTRY) -> lint.Result:
    root, listed = _tree(tmp_path, files, registry)
    return lint.run_check(root, files=listed)


def _keys(result: lint.Result) -> list:
    return [(f.path, f.line, f.key) for f in result.findings]


# --- the word boundary, both sides ------------------------------------------

@pytest.mark.parametrize("line", [
    f"{KEY}: 80",
    f"  {KEY}: \"70\"",
    f"set `{KEY}` to 80",
    f"- record: tenant:alert_threshold:{KEY}",
    f"see {KEY}.",
    f"({KEY})",
    f"\"{KEY}\": [",
    f"{KEY}{{env=\"prod\"}}: 5",
    f"old-{KEY}-note",
])
def test_a_retired_key_as_a_word_is_a_finding(tmp_path, line):
    assert _keys(_check(tmp_path, {"docs/a.md": f"intro\n{line}\n"})) == [("docs/a.md", 2, KEY)]


@pytest.mark.parametrize("line", [
    f"{KEY}_usage_percent: 80",            # a live key the retired one is a prefix of
    f"{KEY}s: 80",
    f"mysql_global_status_{KEY}",          # a raw metric name that ends with it
    f"x{KEY}: 1",
    f"{KEY}_total",
    f"{KEY}9",
    KEY.upper(),
    "ZzOldCount",                           # re-spelled: not seen, by design
    f"tenant:{KEY}_ratio:max",
    KEY.replace("_", "-"),
])
def test_a_longer_or_other_name_is_not_a_mention(tmp_path, line):
    assert _keys(_check(tmp_path, {"docs/a.md": f"{line}\n"})) == []


def test_the_derived_critical_twin_is_retired_with_its_base(tmp_path):
    r = _check(tmp_path, {"docs/a.md": f"{KEY}_critical: 90\n{CRIT}: 1\n{CRIT}_critical: 1\n"})
    assert _keys(r) == [("docs/a.md", 1, KEY + "_critical"), ("docs/a.md", 2, CRIT)]
    # the replacement named for a derived twin is the twin of the base's
    assert "replaced by zz_usage_percent_critical" in r.findings[0].reason
    assert "no replacement" in r.findings[1].reason


# --- where it looks ----------------------------------------------------------

@pytest.mark.parametrize("rel", [
    "docs/guide.md", "docs/assets/data.json", "README.md", "README.en.md",
    "rule-packs/ALERT-REFERENCE.md", "k8s/03-monitoring/x.yaml",
    "helm/chart/values.yaml", "try-local/seed/conf.d/t.yaml", "scripts/demo.sh",
    "scripts/tools/ops/tool.py", "tools/portal/src/x.jsx",
    "components/threshold-exporter/config/conf.d/t.yaml", "AGENTS.md", "new-dir/x.txt",
    "components/threshold-exporter/app/pkg/config/types.go",
])
def test_reader_facing_files_are_scanned(tmp_path, rel):
    assert _keys(_check(tmp_path, {rel: f"{KEY}: 80\n"})) == [(rel, 1, KEY)]


@pytest.mark.parametrize("rel,why", [
    ("changelog.d/1-x.md", "changelog"),
    ("CHANGELOG.md", "changelog"),
    ("CHANGELOG-archive.md", "changelog"),
    ("tests/ops/test_x.py", "test"),
    ("tests/golden/fixtures/a.yaml", "test"),
    ("tools/portal/tests/a.test.ts", "test"),
    ("tools/portal/src/a.test.tsx", "test"),
    ("components/tenant-api/internal/handler/testdata/a.yaml", "test"),
    ("components/threshold-exporter/app/pkg/config/x_test.go", "test"),
])
def test_retirement_talk_and_tests_are_not_scanned(tmp_path, rel, why):
    r = _check(tmp_path, {rel: f"{KEY}: 80\n", "docs/ok.md": "fine\n"})
    assert _keys(r) == []
    assert r.exempt_files == {why: 1, "registry": 1}
    assert r.files_scanned == 1


#: The whole exemption, pinned: None is "scanned". A directory that merely
#: holds samples (`examples`, `fixtures` outside a tests tree) is scanned.
EXEMPTIONS = {
    "rule-packs/threshold-registry.yaml": "registry",
    "CHANGELOG.md": "changelog",
    "CHANGELOG-archive.md": "changelog",
    "changelog.d/1-x.md": "changelog",
    "changelog.d/README.md": "changelog",
    "tests/ops/test_x.py": "test",
    "tests/golden/fixtures/a.yaml": "test",
    "test/rulepack-e2e/run.sh": "test",
    "tools/portal/tests/a.test.ts": "test",
    "tools/portal/src/a.test.tsx": "test",
    "tests/e2e/visual.spec.ts": "test",
    "components/tenant-api/internal/handler/testdata/a.yaml": "test",
    "components/threshold-exporter/app/pkg/config/x_test.go": "test",
    "scripts/tools/ops/test_helper.py": "test",
    "docs/x/examples/a.yaml": None,
    "docs/fixtures/a.yaml": None,
    "k8s/crd/examples/example-thresholdconfig.yaml": None,
    "policies/examples/naming.rego": None,
    "helm/threshold-exporter/values.yaml": None,
    "helm/tenant-api/ci/values.yaml": None,
    "tools/portal/src/interactive/tools/x.jsx": None,
    "tools/portal/src/interactive/tools/_common/data/rule-packs-fallback.json": None,
    "components/threshold-exporter/app/pkg/config/types.go": None,
    "components/threshold-exporter/app/pkg/config/retired_threshold_keys_gen.go": None,
    "components/tenant-api/internal/handler/tenant.go": None,
    "components/threshold-exporter/config/conf.d/_defaults.yaml": None,
    "docs/CHANGELOG.md": None,
    "docs/testing.md": None,
    "docs/latest_test.md": None,
    "scripts/tools/lint/check_retired_keys_not_taught.py": None,
    "README.md": None,
    "try-local/seed/conf.d/t.yaml": None,
    ".github/workflows/ci.yml": None,
}


@pytest.mark.parametrize("rel,why", sorted(EXEMPTIONS.items()))
def test_the_exempt_paths_are_exactly_these(rel, why):
    assert lint.exempt_reason(rel) == why


def test_a_path_that_only_looks_like_a_changelog_is_scanned(tmp_path):
    r = _check(tmp_path, {"docs/CHANGELOG.md": f"{KEY}\n", "docs/changelog.d/a.md": f"{KEY}\n",
                          "docs/latest.md": f"{KEY}\n"})
    assert {f.path for f in r.findings} == {
        "docs/CHANGELOG.md", "docs/changelog.d/a.md", "docs/latest.md"}


def test_binary_symlink_and_missing_files_are_counted_not_scanned(tmp_path):
    root, listed = _tree(tmp_path, {"docs/a.png": b"\x89PNG\0" + KEY.encode(), "docs/ok.md": "x\n"})
    symlink_or_skip("ok.md", root / "docs" / "alias.md")
    r = lint.run_check(root, files=listed + ["docs/alias.md", "docs/deleted.md"])
    assert _keys(r) == []
    assert r.exempt_files == {"binary": 1, "symlink": 1, "not-a-file": 1, "registry": 1}


# --- the registry is the only list -------------------------------------------

def test_a_key_removed_from_retired_keys_is_no_longer_a_finding(tmp_path):
    files = {"docs/a.md": f"{OTHER}: 1\n"}
    assert _keys(_check(tmp_path / "with", files)) == [("docs/a.md", 1, OTHER)]
    without = REGISTRY.replace(
        f"  {OTHER}:\n    replaced_by: null\n    tracking: 2\n    hint: \"nothing reads it\"\n", "")
    assert without != REGISTRY
    r = _check(tmp_path / "without", files, without)
    assert _keys(r) == [] and OTHER not in r.keys


def test_the_module_holds_no_key_of_the_shipped_registry():
    source = (REPO / "scripts" / "tools" / "lint" / "check_retired_keys_not_taught.py").read_text(
        encoding="utf-8")
    shipped = lint.load_retired_keys(str(REPO / lint.REGISTRY_REL))["retired"]
    assert shipped
    assert [k for k in shipped if k in source] == []


# --- the escape at the spot ---------------------------------------------------

def test_a_marker_on_the_line_covers_the_named_key_only(tmp_path):
    text = (f"The old `{KEY}` was an absolute count. <!-- retired-key-mention: {KEY} -->\n"
            f"`{KEY}` and `{OTHER}` both went. <!-- retired-key-mention: {KEY} -->\n")
    r = _check(tmp_path, {"docs/a.md": text})
    assert _keys(r) == [("docs/a.md", 2, OTHER)]
    assert r.marker_exempted == 2


def test_a_marker_covers_the_next_line_and_no_further(tmp_path):
    text = (f"# retired-key-mention: {KEY}, {OTHER}\n"
            f"# {KEY} and {OTHER} were retired\n"
            f"# {KEY} again\n")
    r = _check(tmp_path, {"scripts/a.py": text})
    assert _keys(r) == [("scripts/a.py", 3, KEY)]
    assert r.marker_exempted == 2


def test_a_marker_that_covers_no_mention_is_a_finding(tmp_path):
    r = _check(tmp_path, {"docs/a.md": f"nothing here <!-- retired-key-mention: {KEY} -->\nnor here\n"})
    assert _keys(r) == [("docs/a.md", 1, KEY)]
    assert "stale exemption" in r.findings[0].reason
    assert r.marker_exempted == 0


def test_a_marker_whose_mention_was_rewritten_goes_stale(tmp_path):
    before = f"`{KEY}` is retired <!-- retired-key-mention: {KEY} -->\n"
    assert _keys(_check(tmp_path / "a", {"docs/a.md": before})) == []
    after = before.replace(f"`{KEY}`", "`zz_usage_percent`")
    r = _check(tmp_path / "b", {"docs/a.md": after})
    assert [f.reason.split(":")[0] for f in r.findings] == ["stale exemption"]


def test_a_marker_naming_a_key_that_is_not_retired_is_a_finding(tmp_path):
    r = _check(tmp_path, {"docs/a.md": "zz_usage_percent <!-- retired-key-mention: zz_usage_percent -->\n"})
    assert _keys(r) == [("docs/a.md", 1, "zz_usage_percent")]
    assert "not in the registry" in r.findings[0].reason


@pytest.mark.parametrize("marker", [
    "<!-- retired-key-mention: -->",
    "<!-- retired-key-mention:-->",
    "# retired-key-mention:",
    "# retired-key-mention: \t",
    "/* retired-key-mention: */",
])
def test_a_marker_that_names_no_key_is_a_finding(tmp_path, marker):
    r = _check(tmp_path, {"docs/a.md": f"`{KEY}` is retired {marker}\n"})
    assert sorted(_keys(r)) == [("docs/a.md", 1, ""), ("docs/a.md", 1, KEY)]
    assert "names no key" in [f for f in r.findings if not f.key][0].reason
    # ... also in a file that names no retired key at all
    r = _check(tmp_path / "bare", {"docs/a.md": f"nothing {marker}\n"})
    assert _keys(r) == [("docs/a.md", 1, "")]


def test_prose_about_the_marker_is_not_a_marker(tmp_path):
    text = ("mark it `retired-key-mention: <key>` on the line\n"
            'MARKER = "retired-key-mention:"\n'
            "f\"`{MARKER} {key}`\" and retired-key-mention: {key}\n")
    assert _keys(_check(tmp_path, {"docs/a.md": text})) == []


def test_the_marker_does_not_exempt_another_line_of_the_file(tmp_path):
    text = f"`{KEY}` retired <!-- retired-key-mention: {KEY} -->\n\n\n{KEY}: 80\n"
    assert _keys(_check(tmp_path, {"docs/a.md": text})) == [("docs/a.md", 4, KEY)]


# --- text generated from the registry -----------------------------------------

def test_a_registry_string_projected_into_json_is_skipped(tmp_path):
    for n, ascii_only in enumerate((False, True)):
        data = json.dumps({"keys": {"zz_usage_percent": {"desc": DESC}}}, ensure_ascii=ascii_only, indent=1)
        r = _check(tmp_path / str(n), {"docs/assets/data.json": data + "\n"})
        assert _keys(r) == []
        assert r.generated_skipped == 1 and r.marker_exempted == 0


def test_a_json_string_that_is_not_the_registrys_is_a_finding(tmp_path):
    data = json.dumps({"desc": DESC + " — and you can still write it"})
    assert _keys(_check(tmp_path / "a", {"docs/assets/data.json": data})) == [
        ("docs/assets/data.json", 1, KEY)]
    # verbatim registry text outside a .json file is not recognised
    assert _keys(_check(tmp_path / "b", {"docs/a.md": DESC + "\n"})) == [("docs/a.md", 1, KEY)]
    # a key of the JSON object is not the registry's string either
    assert _keys(_check(tmp_path / "c", {"docs/assets/data.json": json.dumps({KEY: {"desc": DESC}})})) == [
        ("docs/assets/data.json", 1, KEY)]


def test_a_json_string_token_outside_a_json_file_is_a_finding(tmp_path):
    token = json.dumps({"desc": DESC})
    assert _keys(_check(tmp_path / "json", {"docs/assets/data.json": token})) == []
    for rel in ("docs/a.md", "k8s/x.yaml", "tools/portal/src/data.js", "docs/assets/data.jsonl"):
        r = _check(tmp_path / rel.replace("/", "_"), {rel: token + "\n"})
        assert _keys(r) == [(rel, 1, KEY)], rel
        assert r.generated_skipped == 0


def test_a_registry_string_after_an_escaped_quote_is_not_the_whole_string(tmp_path):
    # `"say \"<DESC>"`: the registry's token text appears, but it starts at
    # an escaped quote, inside a longer string
    data = json.dumps({"desc": 'say "' + DESC})
    assert json.dumps(DESC) in data
    r = _check(tmp_path, {"docs/assets/data.json": data})
    assert _keys(r) == [("docs/assets/data.json", 1, KEY)]
    assert r.generated_skipped == 0


def test_strings_of_the_retirement_sections_are_not_generated_text(tmp_path):
    hint = f"was {KEY} once"
    registry = REGISTRY.replace('hint: "converted to a share"', f'hint: "{hint}"').replace(
        "deprecated_aliases: {}", f"deprecated_aliases:\n  zz_alias: {OTHER}")
    assert hint in registry and f"zz_alias: {OTHER}" in registry
    r = _check(tmp_path, {"docs/assets/a.json": json.dumps({"hint": hint}),
                          "docs/assets/b.json": json.dumps({"alias_of": OTHER})}, registry)
    assert _keys(r) == [("docs/assets/a.json", 1, KEY), ("docs/assets/b.json", 1, OTHER)]
    assert r.generated_skipped == 0


def test_the_generated_go_table_is_skipped_only_while_fresh(tmp_path):
    registry = (REPO / lint.REGISTRY_REL).read_text(encoding="utf-8")
    doc = lint.load_retired_keys(str(REPO / lint.REGISTRY_REL))["doc"]
    rel = os.path.relpath(lint.registry_lib.RETIRED_KEYS_GO_PATH, REPO).replace(os.sep, "/")
    assert lint.exempt_reason(rel) is None
    table = lint.registry_lib.render_retired_keys_go(doc)
    key = next(iter(doc["retired_keys"]))
    assert key in table
    r = _check(tmp_path / "fresh", {rel: table}, registry)
    assert _keys(r) == [] and r.generated_skipped >= len(doc["retired_keys"])
    # a hand-edited table, the table under another name, and another Go file
    assert _check(tmp_path / "stale", {rel: table + f"// {key}\n"}, registry).findings
    assert _check(tmp_path / "moved", {rel.replace("_gen.go", "_copy.go"): table}, registry).findings
    other = os.path.dirname(rel) + "/types.go"
    assert _keys(_check(tmp_path / "go", {other: f"// e.g. \"{key}\"\n"}, registry)) == [(other, 1, key)]


def _a_generated_block_naming_a_retired_key():
    """(rel, block, key) of a shipped --regen block that names a retired key."""
    loaded = lint.load_retired_keys(str(REPO / lint.REGISTRY_REL))
    pattern = lint.key_regex(lint.judged_keys(loaded["retired"]))
    for spec in lint.registry_lib.surface_specs(loaded["doc"]):
        m = pattern.search(spec["block"])
        if m:
            return os.path.relpath(spec["path"], REPO).replace(os.sep, "/"), spec["block"], m.group(0)
    pytest.skip("no shipped --regen block names a retired key")


def test_a_fresh_regen_block_is_skipped_and_a_stale_one_is_scanned(tmp_path):
    rel, block, key = _a_generated_block_naming_a_retired_key()
    registry = (REPO / lint.REGISTRY_REL).read_text(encoding="utf-8")
    fresh = "# hand-written\n" + block + "\ngroups: []\n"
    r = _check(tmp_path / "fresh", {rel: fresh}, registry)
    assert _keys(r) == [] and r.generated_skipped >= 1
    # the same block in a file --regen does not write it to
    assert _keys(_check(tmp_path / "elsewhere", {"docs/copy.md": fresh}, registry))
    # the block edited by hand is no longer what the generator renders
    stale = fresh.replace(key, key + " ", 1) if (key + " ") not in block else fresh.replace(key, key + ",", 1)
    assert stale != fresh
    assert key in {f.key for f in _check(tmp_path / "stale", {rel: stale}, registry).findings}
    # hand-written text beside a fresh block is still scanned
    beside = fresh + f"# {key} is fine to write\n"
    assert [f.line for f in _check(tmp_path / "beside", {rel: beside}, registry).findings] == [
        beside.count("\n")]


# --- fail-closed ---------------------------------------------------------------

def test_a_missing_registry_cannot_be_measured(tmp_path):
    with pytest.raises(lint.MeasureError, match="cannot read the threshold registry"):
        _check(tmp_path, {"docs/a.md": "x\n"}, registry=None)


def test_an_unparseable_registry_cannot_be_measured(tmp_path):
    with pytest.raises(lint.MeasureError, match="cannot read the threshold registry"):
        _check(tmp_path, {"docs/a.md": "x\n"}, registry="retired_keys: [\n")


@pytest.mark.parametrize("registry,why", [
    ("version: 1\nkeys: {}\n", "no `retired_keys:` section"),
    ("version: 1\nretired_keys:\n", "not a mapping"),
    ("version: 1\nretired_keys: [a]\n", "not a mapping"),
    ("version: 1\nretired_keys: null\n", "not a mapping"),
])
def test_a_registry_without_a_readable_section_cannot_be_measured(tmp_path, registry, why):
    with pytest.raises(lint.MeasureError, match=why):
        _check(tmp_path, {"docs/a.md": "x\n"}, registry=registry)


def test_a_section_the_loader_emptied_is_caught_from_the_text(tmp_path, monkeypatch):
    real = lint.registry_lib.load_registry

    def emptied(path):
        doc = real(path)
        doc["retired_keys"] = {}
        return doc
    monkeypatch.setattr(lint.registry_lib, "load_registry", emptied)
    with pytest.raises(lint.MeasureError, match="reads as 0 entries but the file's text shows 3"):
        _check(tmp_path, {"docs/a.md": f"{KEY}\n"})


def test_an_explicitly_empty_section_judges_nothing_but_still_needs_files(tmp_path):
    empty = "version: 1\nkeys: {}\nretired_keys: {}\n"
    r = _check(tmp_path / "a", {"docs/a.md": f"{KEY}: 1\n"}, empty)
    assert r.keys == () and r.findings == [] and r.files_scanned == 1
    # ... and a marker left behind names a key that is no longer retired
    r = _check(tmp_path / "m", {"docs/a.md": f"`{KEY}` <!-- retired-key-mention: {KEY} -->\n"}, empty)
    assert [f.reason.split(":")[0] for f in r.findings] == ["stale exemption"]
    with pytest.raises(lint.MeasureError, match="no file was scanned"):
        _check(tmp_path / "b", {"tests/a.md": "x\n"}, empty)


def test_a_scan_of_no_file_cannot_pass(tmp_path):
    with pytest.raises(lint.MeasureError, match="no file was scanned"):
        _check(tmp_path / "a", {"changelog.d/a.md": "x\n", "tests/t.py": "x\n"})
    root, _ = _tree(tmp_path / "b", {})
    with pytest.raises(lint.MeasureError, match="no file was scanned"):
        lint.run_check(root, files=[])


def test_a_file_that_cannot_be_read_cannot_be_measured(tmp_path, monkeypatch, capsys):
    root, listed = _tree(tmp_path, {"docs/a.md": "x\n", "docs/b.md": "y\n"})
    real = Path.read_bytes

    def unreadable(self):
        if self.name == "b.md":
            raise PermissionError(13, "Permission denied", str(self))
        return real(self)
    monkeypatch.setattr(Path, "read_bytes", unreadable)
    with pytest.raises(lint.MeasureError, match="cannot read docs/b.md"):
        lint.run_check(root, files=listed)
    monkeypatch.setattr(lint, "tracked_files", lambda _root: listed)
    assert lint.main(["--repo-root", str(root), "--ci"]) == 2
    assert "cannot read docs/b.md" in capsys.readouterr().err


def test_a_failing_git_listing_cannot_be_measured(tmp_path, monkeypatch, capsys):
    root, _ = _tree(tmp_path, {"docs/a.md": "x\n"})         # not a git work tree
    with pytest.raises(lint.MeasureError, match="`git ls-files` failed"):
        lint.tracked_files(root)
    assert lint.main(["--repo-root", str(root), "--ci"]) == 2
    assert "`git ls-files` failed" in capsys.readouterr().err

    def timed_out(*a, **k):
        raise lint.subprocess.TimeoutExpired("git", 120)
    monkeypatch.setattr(lint.subprocess, "run", timed_out)
    with pytest.raises(lint.MeasureError, match="`git ls-files` failed"):
        lint.tracked_files(REPO)


def test_the_listing_is_gits_whole_listing():
    import subprocess
    out = subprocess.run(["git", "ls-files", "-z"], cwd=str(REPO), capture_output=True,
                         timeout=120, check=True).stdout.decode("utf-8", "surrogateescape")
    assert lint.tracked_files(REPO) == [p for p in out.split("\0") if p]


def test_the_text_count_of_the_section(tmp_path):
    count = lint._section_entries_in_text
    assert count(REGISTRY) == 3
    assert count("retired_keys: {}\n") == 0
    assert count("retired_keys:   # none yet\nother: 1\n") == 0
    assert count("retired_keys:\n  a:\n    hint: x\n\n  # note\n  b: {}\nnext:\n  c: 1\n") == 2
    assert count("keys: {}\n") is None
    assert count("  retired_keys:\n    a: 1\n") is None      # not top-level
    assert count("retired_keys: {a: {}}\n") is None           # flow style: not countable


# --- the command ---------------------------------------------------------------

def _main(monkeypatch, capsys, root, listed, *argv):
    monkeypatch.setattr(lint, "tracked_files", lambda _root: listed)
    rc = lint.main(["--repo-root", str(root), *argv])
    captured = capsys.readouterr()
    return rc, captured.out + captured.err


def test_main_exit_codes(tmp_path, monkeypatch, capsys):
    root, listed = _tree(tmp_path / "bad", {"docs/a.md": f"{KEY}: 80\n"})
    rc, out = _main(monkeypatch, capsys, root, listed, "--ci")
    assert rc == 1 and f"docs/a.md:1: {KEY}" in out and "replaced by zz_usage_percent" in out
    rc, _ = _main(monkeypatch, capsys, root, listed)
    assert rc == 0
    root, listed = _tree(tmp_path / "ok", {
        "docs/a.md": f"`{KEY}` is retired <!-- retired-key-mention: {KEY} -->\n"})
    rc, out = _main(monkeypatch, capsys, root, listed, "--ci")
    assert rc == 0, out
    assert "3 retired key(s) judged, 1 file(s) scanned; 1 mention(s) exempted" in out
    root, listed = _tree(tmp_path / "none", {"docs/a.md": "x\n"}, registry=None)
    rc, out = _main(monkeypatch, capsys, root, listed, "--ci")
    assert rc == 2 and out.startswith("ERROR:")


def test_main_cannot_pass_where_git_lists_nothing(tmp_path, capsys):
    root, _ = _tree(tmp_path, {"docs/a.md": "x\n"})     # not a git work tree
    assert lint.main(["--repo-root", str(root), "--ci"]) == 2
    assert "ERROR:" in capsys.readouterr().err


def _git_repo(root: Path) -> None:
    for argv in (["init", "-q"], ["add", "-A"],
                 ["-c", "user.name=t", "-c", "user.email=t@example.invalid",
                  "commit", "-q", "--no-verify", "-m", "x"]):
        subprocess.run(["git", *argv], cwd=str(root), check=True,
                       capture_output=True, timeout=60)


def test_repo_root_is_not_overridden_by_an_inherited_git_dir(tmp_path, monkeypatch, capsys):
    """`--repo-root A` inside a hook of repository B scans A, not B's listing.

    git's GIT_DIR / GIT_WORK_TREE outrank cwd: left in the environment, the
    scan lists B's files (docs/ok.md only), reads them out of A, and passes
    while A's tracked docs/bad.md teaches a retired key."""
    a, _ = _tree(tmp_path / "a", {"docs/ok.md": "x\n", "docs/bad.md": f"`{KEY}: 80`\n"})
    b, _ = _tree(tmp_path / "b", {"docs/ok.md": "x\n"})
    _git_repo(a)
    _git_repo(b)
    monkeypatch.setenv("GIT_DIR", str(b / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(b))
    assert lint.main(["--repo-root", str(a), "--ci"]) == 1
    assert "docs/bad.md:1" in capsys.readouterr().err


# --- the shipped tree ------------------------------------------------------------

@pytest.fixture(scope="module")
def live() -> lint.Result:
    return lint.run_check(REPO)


def test_the_shipped_tree_teaches_no_retired_key(live):
    assert [f"{f.path}:{f.line}: {f.key} — {f.reason}" for f in live.findings] == []


def test_the_shipped_scan_is_not_vacuous(live):
    """Keys were judged and every reader-facing surface was really read."""
    shipped = lint.load_retired_keys(str(REPO / lint.REGISTRY_REL))["retired"]
    assert len(live.keys) == len(shipped) > 0
    assert live.files_scanned > 0
    tracked = lint.tracked_files(REPO)
    assert live.files_scanned + sum(live.exempt_files.values()) == len(tracked)
    # every top-level directory that holds a file the guard does not exempt
    # was scanned, the root's own files (README*) too — computed from git's
    # listing, so a walker that drops a surface is red here
    expected: dict = {}
    for rel in tracked:
        p = REPO / rel
        if lint.exempt_reason(rel) or p.is_symlink() or not p.is_file() or b"\0" in p.read_bytes():
            continue
        top = rel.split("/", 1)[0] if "/" in rel else "."
        expected[top] = expected.get(top, 0) + 1
    assert live.scanned_roots == expected
    for surface in ("docs", "rule-packs", "k8s", "helm", "try-local", "scripts", "tools", "."):
        assert live.scanned_roots.get(surface, 0) > 0, surface
    assert any(rel.startswith("tools/portal/src/") and not lint.exempt_reason(rel) for rel in tracked)
    assert any(rel.startswith("README") and not lint.exempt_reason(rel) for rel in tracked)


def test_ci_lint_job_runs_the_hook():
    from _ci_lint_wiring import assert_ci_lint_job_runs_hook
    assert_ci_lint_job_runs_hook(
        "retired-keys-not-taught", guards="retired threshold keys named in docs and samples (#1200)")
