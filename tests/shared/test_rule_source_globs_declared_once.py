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
* the declaration refuses two files of one member (``x.yaml`` beside
  ``x.yml``: every reader keys by member, one would replace the other), and
  the callers name what it refuses (bump_docs, threshold reachability,
  benchmark.sh — which also lists before it deletes anything);
* the copies in other languages — Go tests and ``.github/CODEOWNERS`` —
  spell every declared glob of a family they spell one of;
* the deletion guard: no non-docstring Python string constant (an
  f-string's literal parts included) and no non-comment shell line under
  ``scripts/`` outside ``_lib_consumer_read.py`` holds a family prefix in a
  token that names a file or a glob (ends in ``-``, holds ``*`` / ``{`` /
  ``%``, or ends in ``.yaml`` / ``.yml``); ``tests/`` is held to globs only
  (a fixture writes concrete names; a test reading the real repo takes them
  from the declaration). ``ALLOWED`` lists the exceptions with why, and an
  entry that no longer matches is red.

⛔ What the deletion guard cannot see: a prefix split across constants
(``"rule-" + "pack-*"``), a directory glob without the family prefix
(``Path("rule-packs").glob("*.yaml")``), a bytes literal, a prefix in
another case, one read from a variable, a file or the environment, one spelled in a docstring or a comment (prose), and any
language other than Python and shell (Go, CODEOWNERS and the YAML configs
are held by the parity tests above instead; JS/JSX only by review). The
real guarantee is the declaration failing closed (``SourcesUnlisted`` names
an undeclared YAML or two files of one pack, ``NoDeclaredRuleFiles`` an
empty family) and the coverage tripwire
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


# --- the declaration refuses two files of one member (#3471 F1) ---------------


@pytest.mark.parametrize("source", cr.RULE_SOURCES, ids=lambda s: s.directory)
def test_two_files_of_one_member_are_named(tmp_path, source):
    """`rule-pack-redis.yaml` beside `rule-pack-redis.yml`: every reader keys
    the files by member, so one would silently replace the other."""
    d = tmp_path / source.directory
    d.mkdir(parents=True)
    for glob in source.rule_files:
        (d / glob.replace("*", "redis")).write_text("groups: []\n", encoding="utf-8")
    (d / source.file_name("other")).write_text("groups: []\n", encoding="utf-8")
    for read in (lambda: source.files_under(tmp_path), lambda: source.named_in(d)):
        with pytest.raises(cr.SourcesUnlisted) as exc:
            read()
        assert sorted(Path(p).name for p, _ in exc.value.files) == sorted(
            g.replace("*", "redis") for g in source.rule_files)
        assert all("same" in why for _, why in exc.value.files)


def test_generators_stop_on_two_files_of_one_pack(tmp_path):
    """The live failure the review measured: `cp rule-pack-redis.yaml
    rule-pack-redis.yml`, and `generate_rulepack_configmaps --check` kept
    rc 0 with one copy replacing the other. The declaration refuses it."""
    rp = tmp_path / cr.RULE_PACKS.directory
    rp.mkdir()
    for name in ("rule-pack-redis.yaml", "rule-pack-redis.yml"):
        (rp / name).write_text("groups: []\n", encoding="utf-8")
    with pytest.raises(cr.SourcesUnlisted):
        cr.rule_pack_files(rp)
    with pytest.raises(cr.SourcesUnlisted):
        cr.repo_rule_files(tmp_path)


# --- callers name what the declaration refuses (#3471 F2) --------------------


def _stray_repo(tmp_path):
    """A copy of the repo's rule trees with an undeclared YAML in rule-packs/."""
    import shutil
    for source in cr.RULE_SOURCES:
        shutil.copytree(REPO / source.directory, tmp_path / source.directory,
                        ignore=shutil.ignore_patterns("README*", "*.md"))
    (tmp_path / cr.RULE_PACKS.directory / "stray.yaml").write_text("x: 1\n", encoding="utf-8")
    return tmp_path


def test_bump_docs_names_an_undeclared_yaml(tmp_path, monkeypatch, capsys):
    import bump_docs
    repo = _stray_repo(tmp_path)
    real = bump_docs.count_rule_packs   # the module bump_docs itself imported
    monkeypatch.setattr(bump_docs, "count_rule_packs", lambda: real(
        repo / cr.RULE_PACKS.directory, repo / cr.CONFIGMAP_RULES.directory))
    with pytest.raises(SystemExit) as exc:
        bump_docs._count_rule_packs()
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "stray.yaml" in err and "Traceback" not in err


def test_threshold_reachability_names_an_undeclared_yaml(monkeypatch, capsys):
    import _lib_consumer_read
    import check_threshold_reachability as ctr

    def refuse():
        raise _lib_consumer_read.SourcesUnlisted([("rule-packs/stray.yaml", "undeclared")])
    monkeypatch.setattr(ctr, "run_check", refuse)
    monkeypatch.setattr("sys.argv", ["check_threshold_reachability.py", "--ci"])
    assert ctr.main() == 2
    err = capsys.readouterr().err
    assert "rule-packs/stray.yaml: undeclared" in err


_BENCH = REPO / "scripts" / "benchmark.sh"


def _bench_listing() -> str:
    src = _BENCH.read_text(encoding="utf-8")
    m = re.search(r"RULE_CM_FILES=\$\(python3 -c '(.*?)' \"", src, re.S)
    assert m, "benchmark.sh no longer lists the rules ConfigMaps through python3 -c"
    return m.group(1)


def test_benchmark_lists_before_it_deletes():
    """F3: a listing that fails after the tier deletes would leave the cluster
    half torn down — the list is taken before the first `kubectl delete`."""
    src = _BENCH.read_text(encoding="utf-8")
    assert src.index("RULE_CM_FILES=$(") < src.index("kubectl delete")


def test_benchmark_listing_names_an_undeclared_yaml_rc2(tmp_path):
    import subprocess
    import sys
    d = tmp_path / cr.CONFIGMAP_RULES.directory
    d.mkdir(parents=True)
    (d / "configmap-rules-a.yaml").write_text("x: 1\n", encoding="utf-8")
    run = [sys.executable, "-c", _bench_listing(), str(REPO / "scripts" / "tools"), str(tmp_path)]
    ok = subprocess.run(run, capture_output=True, text=True, timeout=60, encoding="utf-8")
    assert ok.returncode == 0 and ok.stdout.strip().endswith("configmap-rules-a.yaml")
    (d / "stray.yaml").write_text("x: 1\n", encoding="utf-8")
    bad = subprocess.run(run, capture_output=True, text=True, timeout=60, encoding="utf-8")
    assert bad.returncode == 2
    assert "stray.yaml" in bad.stderr and "Traceback" not in bad.stderr


# --- copies in other languages: Go tests, CODEOWNERS (#3471 F5) --------------

_FAMILY_TOKEN = re.compile(
    r"[\w./*{}%\[\]|-]*(?:" + "|".join(re.escape(s.prefix) for s in cr.RULE_SOURCES)
    + r")[\w./*{}%\[\]|-]*")


def _declared_globs_by_family(token_globs):
    """{source: {declared glob basenames spelled}} for the glob tokens given."""
    out = {}
    for tok in token_globs:
        base = tok.rsplit("/", 1)[-1]
        for source in cr.RULE_SOURCES:
            if base in source.rule_files:
                out.setdefault(source, set()).add(base)
    return out


def _parity_problems(where, token_globs):
    return [f"{where}: spells {sorted(spelled)} of {source.directory}'s "
            f"{list(source.rule_files)}"
            for source, spelled in _declared_globs_by_family(token_globs).items()
            if spelled != set(source.rule_files)]


def test_go_tests_spell_every_declared_glob_of_a_family_they_glob():
    """Go cannot import the declaration: a Go file that globs one of a
    family's declared names globs them all (`.yaml` and `.yml`)."""
    go_files = sorted((REPO / "components").rglob("*.go"))
    assert len(go_files) > 50, "Go walk found almost nothing"
    bad, seen = [], 0
    for f in go_files:
        # interpreted ("…") and raw (`…`) string literals alike
        literals = [a or b for a, b in re.findall(
            r'"((?:[^"\\]|\\.)*)"|`([^`]*)`', f.read_text(encoding="utf-8"))]
        globs = [t for lit in literals for t in _FAMILY_TOKEN.findall(lit) if "*" in t]
        seen += bool(_declared_globs_by_family(globs))
        bad += _parity_problems(f.relative_to(REPO).as_posix(), globs)
    assert seen, "no Go file globs a rule family — the measurement itself is empty"
    assert not bad, "\n".join(bad)


def test_codeowners_spells_every_declared_glob_of_a_family_it_owns():
    lines = (REPO / ".github" / "CODEOWNERS").read_text(encoding="utf-8").splitlines()
    patterns = [ln.split()[0] for ln in lines if ln.strip() and not ln.lstrip().startswith("#")]
    globs = [p for p in patterns if _FAMILY_TOKEN.fullmatch(p) and "*" in p]
    assert globs, "CODEOWNERS owns no rule family by glob — the measurement is empty"
    assert not _parity_problems("CODEOWNERS", globs)


def test_parity_sees_a_missing_suffix():
    assert _parity_problems("x", ["rule-packs/rule-pack-*.yaml"])
    assert not _parity_problems("x", ["rule-packs/rule-pack-*.yaml", "rule-packs/rule-pack-*.yml"])


# --- deletion guard (#3471 F4) ------------------------------------------------
#
# A string that names a rule-family FILE or GLOB: a token holding a family
# prefix (`rule-pack-`, `da-rule-pack-`, `configmap-rules-`) that ends in
# `-` (a prefix awaiting a concatenation, `%` or `.format`), holds a `*`, a
# `{`, a `%`, or ends in `.yaml` / `.yml`. A token that names no YAML file and
# no glob — `--rule-pack-dir`, `rule-pack-diff`, `rule-pack-stats.md`,
# `readme-rule-pack-badge` — is a different thing that shares the spelling.


def family_tokens(text: str) -> "list[str]":
    out = []
    for tok in _FAMILY_TOKEN.findall(text):
        low = tok.lower()
        if (low.endswith("-") or any(c in low for c in "*{%")
                or low.endswith((".yaml", ".yml"))):
            out.append(tok)
    return out


def _docstring_ids(tree):
    ids = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) \
                and n.body and isinstance(n.body[0], ast.Expr) \
                and isinstance(n.body[0].value, ast.Constant) \
                and isinstance(n.body[0].value.value, str):
            ids.add(id(n.body[0].value))
    return ids


def python_hits(text: str, rel: str, globs_only: bool = False) -> "list[tuple[str, int, str]]":
    """(rel, line, token) for every non-docstring string constant (an
    f-string's literal parts included) that names a rule-family file or glob."""
    tree = ast.parse(text)
    skip = _docstring_ids(tree)
    return [(rel, n.lineno, tok) for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in skip
            for tok in family_tokens(n.value) if not globs_only or "*" in tok]


def shell_hits(text: str, rel: str) -> "list[tuple[str, int, str]]":
    return [(rel, i, tok) for i, line in enumerate(text.splitlines(), 1)
            if not line.lstrip().startswith("#") for tok in family_tokens(line)]


#: (path, token regex, why) — each must still match a hit (a stale entry is red).
ALLOWED: "tuple[tuple[str, str, str], ...]" = (
    ("scripts/tools/ops/scaffold_tenant.py", r"rule-packs/rule-pack-[a-z0-9]+\.yaml",
     "the scaffold catalogue names each pack's own source file, one per pack"),
    ("scripts/tools/lint/check_build_completeness.py", r"(rule-pack-[a-z0-9]+|configmap-rules-platform)\.yaml",
     "REQUIRED_DATA_FILES: the named files the da-tools image must carry"),
    ("scripts/tools/dx/_recipe_preview.py", r"rule-pack-custom-alerts\.yaml",
     "the one generated custom-alerts pack, by name"),
    ("scripts/tools/dx/compile_custom_alerts.py", r"rule-packs/rule-pack-custom-alerts\.yaml",
     "the file this compiler writes, by name"),
    ("scripts/tools/ops/lint_custom_rules.py", r"rule-packs/rule-pack-custom-alerts\.yaml",
     "the custom-alerts pack, by name"),
    ("scripts/tools/ops/_grar_validate.py", r"configmap-rules-platform\.yaml",
     "the hand-authored platform rules ConfigMap, by name"),
    ("scripts/tools/lint/check_image_pin_capability.py", r"configmap-rules-platform\.yaml",
     "a caveat message to a person"),
    ("scripts/tools/lint/check_vmalert_coverage.py", r"rule-pack-",
     "baseline-file header prose naming promtool fixtures (`rule-pack-<db>-threshold_test.yaml`)"),
    ("scripts/tools/ops/rule_pack_diff.py", r"rule-packs/rule-pack-mariadb\.yaml",
     "--help epilog example"),
    ("tests/lint/test_check_pint.py", r"(\]\]\*)?rule-packs/rule-pack-(\*\.yaml)?",
     "pins .pint.hcl's include pattern, a file this repo's Python cannot feed"),
    ("scripts/tools/dx/generate_rule_pack_readme.py",
     r"k8s/03-monitoring/configmap-rules-(\*|mongodb)\.yaml|configmap-rules-\*\.yaml",
     "README prose for people (the generated rule-packs/README.md)"),
    ("scripts/tools/dx/generate_byo_rulepack_table.py", r"configmap-rules-\*",
     "a message to a person"),
    ("scripts/tools/lint/check_configmap_mount_completeness.py", r"configmap-rules-\*(\.yaml)?",
     "messages to a person"),
    ("scripts/tools/ops/migrate_to_operator.py", r"/\*-rule-pack-\*\.yaml",
     "checklist prose for people (`kubectl apply -f <dir>/*-rule-pack-*.yaml`)"),
    ("scripts/tools/ops/lint_custom_rules.py", r"rule-pack-\*\.yaml\|yml",
     "--rule-pack-dir help text"),
    ("scripts/tools/ops/silencer_drift_check.py", r"rule-pack-\*\.yaml\|yml",
     "--rule-pack-dir help text"),
    ("tests/lint/test_check_configmap_mount_completeness.py", r"configmap-rules-\*",
     "asserts the lint's own message"),
)


def _sources(root: str, suffix: str):
    for p in sorted((REPO / root).rglob(f"*{suffix}")):
        rel = p.relative_to(REPO).as_posix()
        if rel not in (_LIB, "tests/shared/test_rule_source_globs_declared_once.py") \
                and "node_modules" not in p.parts and "__pycache__" not in p.parts:
            yield rel, p.read_text(encoding="utf-8")


def _all_hits():
    py = list(_sources("scripts", ".py"))
    assert len(py) > 100, "scan found almost nothing — the walk itself is broken"
    hits = [h for rel, text in py for h in python_hits(text, rel)]
    hits += [h for rel, text in _sources("scripts", ".sh") for h in shell_hits(text, rel)]
    # tests/: only globs. A test reading the REAL repo takes its files from
    # the declaration; a fixture writes concrete names and needs no glob.
    hits += [h for rel, text in _sources("tests", ".py") for h in python_hits(text, rel, True)]
    return hits


def test_no_tool_spells_a_rule_family_file_or_glob():
    hits = _all_hits()
    allowed = [(p, re.compile(rx)) for p, rx, _why in ALLOWED]
    bad = [h for h in hits if not any(h[0] == p and rx.fullmatch(h[2]) for p, rx in allowed)]
    assert not bad, ("spell these through _lib_consumer_read.RULE_SOURCES "
                     "(files_in / files_under / named_in / member / file_name), or "
                     "allow-list them in ALLOWED with why:\n"
                     + "\n".join(f"{r}:{n}: {t}" for r, n, t in bad))


def test_every_allow_list_entry_still_matches():
    hits = _all_hits()
    stale = [(p, rx) for p, rx, _why in ALLOWED
             if not any(h[0] == p and re.fullmatch(rx, h[2]) for h in hits)]
    assert not stale, f"stale ALLOWED entries — delete them: {stale}"


@pytest.mark.parametrize("snippet", [
    'x = sorted(d.glob("rule-pack-*.yaml"))',
    'n = p.stem.replace("configmap-rules-", "")',
    'f = f"da-rule-pack-{name}.yaml"',
    'ok = name.startswith("da-rule-pack-")',
    'p = f"k8s/configmap-rules-{x}.yaml"',
    'p = "configmap-rules-%s.yaml" % x',
    'p = "configmap-rules-{}.yaml".format(x)',
    'p = "rule-packs/rule-pack-" + x',
    'p = "rule-pack-redis.yml"',
])
def test_the_python_guard_sees_the_plain_spellings(snippet):
    assert python_hits(snippet, "x.py")


def test_the_guard_passes_over_prose_and_other_names():
    assert not python_hits('"""reads rule-pack-*.yaml"""\n', "x.py")
    assert not python_hits('a = "--rule-pack-dir"; b = "rule-pack-stats.md"; '
                           'c = "readme-rule-pack-badge"; d = "rule-pack-diff"', "x.py")
    assert python_hits('x = "rule-pack-*.yaml"', "t.py", globs_only=True)
    assert not python_hits('x = "rule-pack-redis.yaml"', "t.py", globs_only=True)
    assert shell_hits('for f in "$D"/configmap-rules-*.yaml; do :; done', "x.sh")
    assert shell_hits('kubectl apply -f "$D/configmap-rules-${p}.yaml"', "x.sh")
    assert not shell_hits("# configmap-rules-*.yaml in a comment", "x.sh")
