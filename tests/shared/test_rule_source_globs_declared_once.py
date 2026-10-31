"""Where this repo ships rule files is declared once (#3438), and every tool
picks, names and writes those files through that declaration (#3471).

``_lib_consumer_read.RULE_SOURCES`` (``RULE_PACKS``, ``OPERATOR_RULES``,
``CONFIGMAP_RULES``) says which directory holds which family and which file
names there are rule files. Before #3471 some twenty tools under ``scripts/``
each spelled ``rule-pack-*.yaml`` / ``configmap-rules-*.yaml`` /
``da-rule-pack-*.yaml`` themselves: a ``.yml`` file was passed over, and a
YAML beside the rule files that is neither a rule file nor declared was
skipped in silence. This module pins:

* the helpers (``prefix``, ``member``, ``file_name``, ``file_stem``,
  ``files_in``, ``files_under``, ``named_in``);
* that the names the generators write are names the declaration lists;
* the copies that cannot import it — ``.pre-commit-config.yaml`` ``files:``
  and the workflows' path filters — match every declared rule file name of a
  family they watch (parity);
* the deletion guard: no Python string constant and no shell line under
  ``scripts/`` outside ``_lib_consumer_read.py`` spells a family's glob or
  prefix.

⛔ What the deletion guard cannot see: a glob built by concatenation
(``"rule-" + "pack-*"``), one read from a variable or a file, one spelled in a
docstring (docstrings are prose; only a constant EQUAL to a banned string is
flagged), and any language other than Python and shell. The real guarantee is
the declaration failing closed (``SourcesUnlisted`` names an undeclared YAML,
``NoDeclaredRuleFiles`` an empty family) and the coverage tripwire
(``scripts/tools/lint/_rule_tripwire.py``) naming rule-shaped YAML outside
it; this guard only stops the plain spelling from coming back.
"""
from __future__ import annotations

import ast
import fnmatch
import re
from pathlib import Path

import pytest
import yaml

import _lib_consumer_read as cr
from _platform_fs import symlink_or_skip
import check_rulepack_sync as sync
import generate_rulepack_configmaps as grc
import operator_generate as og

REPO = Path(__file__).resolve().parents[2]
_LIB = "scripts/tools/_lib_consumer_read.py"


def _samples(source: cr.RuleSource) -> "list[str]":
    """One repo path per declared rule file glob of *source*."""
    return [f"{source.directory}/{glob.replace('*', 'x')}" for glob in source.rule_files]


# --- the helpers ------------------------------------------------------------


@pytest.mark.parametrize("source", cr.RULE_SOURCES, ids=lambda s: s.directory)
def test_each_glob_has_one_star_after_the_shared_prefix(source):
    for glob in source.rule_files:
        assert glob.count("*") == 1, glob
        assert glob.split("*")[0] == source.prefix, glob


@pytest.mark.parametrize("source", cr.RULE_SOURCES, ids=lambda s: s.directory)
def test_member_and_file_name_round_trip(source):
    for glob in source.rule_files:
        name = glob.replace("*", "redis")
        assert source.member(name) == "redis"
        assert source.member(f"/some/dir/{name}") == "redis"
    assert source.file_name("redis") == source.rule_files[0].replace("*", "redis")
    assert source.member(source.file_name("a-b")) == "a-b"
    assert source.file_stem("redis") == source.prefix + "redis"
    assert source.member("redis.yaml") is None
    assert source.member(source.prefix + "redis.YAML") is None   # listing refuses it
    with pytest.raises(ValueError):
        source.file_name("a/b")


def test_files_under_is_files_in_the_declared_directory(tmp_path):
    d = tmp_path / cr.CONFIGMAP_RULES.directory
    d.mkdir(parents=True)
    (d / "configmap-rules-a.yaml").write_text("x: 1\n", encoding="utf-8")
    (d / "configmap-rules-b.yml").write_text("x: 1\n", encoding="utf-8")
    (d / "deployment-prometheus.yaml").write_text("x: 1\n", encoding="utf-8")
    assert [p.name for p in cr.CONFIGMAP_RULES.files_under(tmp_path)] == [
        "configmap-rules-a.yaml", "configmap-rules-b.yml"]
    (d / "stray.yaml").write_text("x: 1\n", encoding="utf-8")
    with pytest.raises(cr.SourcesUnlisted):
        cr.CONFIGMAP_RULES.files_under(tmp_path)
    with pytest.raises(cr.SourcesUnlisted):   # a missing directory is named
        cr.OPERATOR_RULES.files_under(tmp_path)


def test_named_in_takes_only_rule_file_names(tmp_path):
    """The image's flat layout: tools and their data side by side."""
    (tmp_path / "rule-pack-a.yaml").write_text("groups: []\n", encoding="utf-8")
    (tmp_path / "rule-pack-b.yml").write_text("groups: []\n", encoding="utf-8")
    (tmp_path / "metric_observed_map.yaml").write_text("x: 1\n", encoding="utf-8")
    (tmp_path / "tool.py").write_text("", encoding="utf-8")
    assert [p.name for p in cr.RULE_PACKS.named_in(tmp_path)] == [
        "rule-pack-a.yaml", "rule-pack-b.yml"]
    assert cr.RULE_PACKS.named_in(tmp_path / "missing") == []
    symlink_or_skip(tmp_path / "gone.yaml", tmp_path / "rule-pack-c.yaml")
    with pytest.raises(cr.SourcesUnlisted):
        cr.RULE_PACKS.named_in(tmp_path)


# --- generators write declared names ----------------------------------------


def _declared_names(source):
    return {p.name for p in source.files_under(REPO)}


def test_generated_configmap_names_are_declared_and_present():
    """generate_rulepack_configmaps writes `target_name(pack)`: a name the
    declaration lists, and on this tree the file it lists."""
    declared = _declared_names(cr.CONFIGMAP_RULES)
    for pack in cr.RULE_PACKS.files_under(REPO):
        name = grc.target_name(cr.RULE_PACKS.member(pack))
        assert cr.CONFIGMAP_RULES.member(name) == cr.RULE_PACKS.member(pack)
        assert name in declared, name


def test_generated_prometheusrule_names_are_declared_and_present():
    """operator-generate writes `<metadata.name>.yaml`; that name is the
    declaration's and, on this tree, a file it lists."""
    declared = _declared_names(cr.OPERATOR_RULES)
    for pack in cr.RULE_PACKS.files_under(REPO):
        member = cr.RULE_PACKS.member(pack)
        crd = og.build_prometheus_rule(member, {"groups": []}, "monitoring")
        name = crd["metadata"]["name"] + ".yaml"
        assert cr.OPERATOR_RULES.member(name) == member
        assert name in declared, name


def test_three_copy_paths_are_declared():
    for pack in cr.RULE_PACKS.files_under(REPO):
        member = cr.RULE_PACKS.member(pack)
        cm, op = sync.copy_paths(REPO, member)
        assert cr.CONFIGMAP_RULES.member(cm) == member
        assert cr.OPERATOR_RULES.member(op) == member
        assert sync.source_path(REPO, member).name in _declared_names(cr.RULE_PACKS)


# --- copies that cannot import the declaration: parity ----------------------

_CONTROL = "zz-unrelated-dir/x.yaml"   # a hook that matches this is file-type generic


def _precommit_hooks():
    cfg = yaml.safe_load((REPO / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    for repo in cfg["repos"]:
        for hook in repo["hooks"]:
            if hook.get("files"):
                yield hook["id"], re.compile(hook["files"])


def test_precommit_files_patterns_match_every_declared_name_of_a_family_they_watch():
    """A hook whose `files:` takes one declared rule file name of a family
    (and is not a file-type-generic hook) takes them all: a `.yml` rule pack
    fires the hooks a `.yaml` one does."""
    bad = []
    for hook_id, rx in _precommit_hooks():
        if rx.search(_CONTROL):
            continue
        for source in cr.RULE_SOURCES:
            hits = {s: bool(rx.search(s)) for s in _samples(source)}
            if any(hits.values()) and not all(hits.values()):
                bad.append(f"{hook_id}: {source.directory} {hits}")
    assert not bad, "\n".join(bad)


def _path_lists(node, key=None):
    """Every list of path globs in a parsed workflow: `paths:` triggers and
    dorny/paths-filter `filters:` (a YAML string of its own)."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "filters" and isinstance(v, str):
                parsed = yaml.safe_load(v)
                if isinstance(parsed, dict):
                    for globs in parsed.values():
                        if isinstance(globs, list):
                            yield [g for g in globs if isinstance(g, str)]
            elif k == "paths" and isinstance(v, list):
                yield [g for g in v if isinstance(g, str)]
            else:
                yield from _path_lists(v, k)
    elif isinstance(node, list):
        for v in node:
            yield from _path_lists(v, key)


def test_workflow_path_filters_match_every_declared_name_of_a_family_they_watch():
    lists = []
    for wf in sorted((REPO / ".github" / "workflows").glob("*.y*ml")):
        doc = yaml.safe_load(wf.read_text(encoding="utf-8"))
        lists += [(wf.name, globs) for globs in _path_lists(doc)]
    assert lists, "no path filter parsed — the measurement itself is empty"
    bad = []
    for wf, globs in lists:
        plain = [g for g in globs if not g.startswith("!")]
        for source in cr.RULE_SOURCES:
            hits = {s: any(fnmatch.fnmatchcase(s, g) for g in plain) for s in _samples(source)}
            if any(hits.values()) and not all(hits.values()):
                bad.append(f"{wf}: {source.directory} {hits} in {plain}")
    assert not bad, "\n".join(bad)


# --- deletion guard ---------------------------------------------------------

#: Exactly what may not be spelled outside _lib_consumer_read.py: each
#: family's prefix, the prefix with a bare `*`, and each declared glob.
BANNED = frozenset(
    s for source in cr.RULE_SOURCES
    for s in (source.prefix, source.prefix + "*", *source.rule_files))


def test_banned_set_is_what_the_docstring_says():
    assert BANNED == {
        "rule-pack-", "rule-pack-*", "rule-pack-*.yaml", "rule-pack-*.yml",
        "da-rule-pack-", "da-rule-pack-*", "da-rule-pack-*.yaml", "da-rule-pack-*.yml",
        "configmap-rules-", "configmap-rules-*", "configmap-rules-*.yaml",
        "configmap-rules-*.yml",
    }


def python_hits(text: str, rel: str) -> "list[str]":
    return [f"{rel}:{n.lineno}: {n.value!r}" for n in ast.walk(ast.parse(text))
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and n.value in BANNED]


_SHELL_GLOB = re.compile("|".join(re.escape(s.prefix + "*") for s in cr.RULE_SOURCES))


def shell_hits(text: str, rel: str) -> "list[str]":
    return [f"{rel}:{i}: {line.strip()}" for i, line in enumerate(text.splitlines(), 1)
            if not line.lstrip().startswith("#") and _SHELL_GLOB.search(line)]


def _scripts(suffix):
    for p in sorted((REPO / "scripts").rglob(f"*{suffix}")):
        rel = p.relative_to(REPO).as_posix()
        if rel != _LIB:
            yield rel, p.read_text(encoding="utf-8")


def test_no_tool_spells_a_rule_family_glob_or_prefix():
    files = list(_scripts(".py"))
    assert len(files) > 100, "scan found almost nothing — the walk itself is broken"
    hits = [h for rel, text in files for h in python_hits(text, rel)]
    hits += [h for rel, text in _scripts(".sh") for h in shell_hits(text, rel)]
    assert not hits, ("spell these through _lib_consumer_read.RULE_SOURCES "
                      "(files_in / files_under / named_in / member / file_name):\n"
                      + "\n".join(hits))


@pytest.mark.parametrize("snippet", [
    'x = sorted(d.glob("rule-pack-*.yaml"))',
    'n = p.stem.replace("configmap-rules-", "")',
    'f = f"da-rule-pack-{name}.yaml"',
    'ok = name.startswith("da-rule-pack-")',
])
def test_the_python_guard_sees_the_plain_spellings(snippet):
    assert python_hits(snippet, "x.py")


def test_the_guard_does_not_flag_prose_or_a_named_file():
    assert not python_hits('"""reads rule-pack-*.yaml"""\nx = "rule-pack-redis.yaml"', "x.py")
    assert shell_hits('for f in "$D"/configmap-rules-*.yaml; do :; done', "x.sh")
    assert not shell_hits("# configmap-rules-*.yaml in a comment", "x.sh")
