"""Rule-tree reader — what alerting rules this repo actually ships.

The repo's rule files, as declared by path (`_lib_consumer_read.RULE_SOURCES`
and `CHART_VALUES_RULES`, #3438) and read by `_lib_consumer_read.rule_files_in`
— the one reading of "which rule files does a file hand to Prometheus" — plus
the provenance rules that decide which of those rules belong to the
hand-authored PLATFORM tree rather than to a generated copy of a rule pack.

⛔ Nothing here decides from content which files hold rules (#3438, owner
ruling A: which files are read is declared by path). It used to: every tracked
YAML under the shipped roots was read and classified by its own `kind` /
`groups` tests, so a rules ConfigMap placed anywhere was found. That property
is kept, as an ALARM instead of a reading: `_rule_tripwire` names every
tracked YAML outside the declaration that looks like rules, and the contract
tests go red on it. A file reaches the contracts only by being declared.

⛔ This lives in a module, not in a test file, for one concrete reason: it used
to live in tests/ops/test_generate_routes_orchestration.py, where nothing else
could import it — so `_real_platform_label_sets` there and
`platform_alert_identities` in scripts/tools/ops/_grar_validate.py each grew
their OWN answer to "what is the platform rule tree", both by hard-coding the
single filename `configmap-rules-platform.yaml`. Three readers, no cross-check.
The duplication was a consequence of the location.

The production reader keeps its own narrow path on purpose — it ships inside an
image that carries no repo tree and falls back to a constant when the ConfigMap
is unreachable. What it gains from this module is a CROSS-CHECK: see
test_generate_routes_orchestration.py::TestPlatformReaderParity.
"""
from __future__ import annotations

import codecs
import functools
import json
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath

# Guarded and deduped, unlike the bare double-insert most siblings use: this
# module is imported by test suites whose conftest has already added the same
# directory, and `check_rulepack_sync` below inserts one more on its own.
_THIS_DIR = str(Path(__file__).resolve().parent)
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)
_TOOLS_DIR = str(Path(__file__).resolve().parent.parent)
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)
# ⛔ #3206 (ADR-036 §12): nothing here reads YAML itself. A manifest is read
# the way kubectl reads it (da-crdecode) and every rule file — a rule pack, a
# ConfigMap data value, a PrometheusRule's spec — the way Prometheus' rule
# loader reads it (`da-read prometheus-rules`). Without either binary this
# module raises `ConsumerReadError`; it never falls back to another reading.
import _lib_consumer_read  # noqa: E402


def _find_repo_root() -> str:
    """Walk up to the directory that holds `.git`, with a legible failure.

    ⛔ Not `parents[3]`. That hard-codes "four levels below the root", which is
    true here and false the moment this file is copied somewhere flatter — and
    a sibling in this very directory (`check_rulepack_sync.py`) is already
    copied into a flat `core/lint/` by components/recipe-preview/Dockerfile.
    The failure mode of the index form is `IndexError: 3` at import time, with
    nothing saying what went wrong.
    """
    here = Path(__file__).resolve()
    for candidate in here.parents:
        if (candidate / ".git").exists():
            return str(candidate)
    raise RuntimeError(
        f"cannot locate the repository root above {here} — this module reads "
        "the shipped rule tree with `git ls-files` and has nothing to read "
        "without one. If it is being used outside a checkout, that is the bug.")


_REPO_ROOT = _find_repo_root()

# ⛔ Read by tests/lint/test_check_pint.py with a REGEX, not an import, so that
# .pint.hcl's include pattern and this prefix cannot drift apart. It has no
# in-module caller by design: which files are read is the rule-source
# declaration and whether a rule is platform is decided by provenance, so this
# is only used for HUMAN-READABLE messages. Deleting it as "unused" breaks that lockstep.
_PLATFORM_CM_PREFIX = "configmap-rules-platform"
# Both extensions: a rules ConfigMap named `.yml` was silently skipped by the
# scanner, which is the same "escapes the gate by being named differently" hole.
_RULES_FILE_EXTS = (".yaml", ".yml")
# The deploy-copy / source naming convention that ties the two trees together:
# k8s/03-monitoring/configmap-rules-<pack>.yaml is GENERATED from
# rule-packs/rule-pack-<pack>.yaml. `check_portal_rulepack_claims.py` reads the
# same pairing (its `path.stem.replace(...)` derivations at :135-142, and the
# docstring at :33-36 names configmap-rules-platform.yaml as the one with "NO
# rule-pack counterpart").
_RULES_CM_PREFIX = "configmap-rules-"
# The rule-packs source tree, as the rule-source declaration names it.
_RULE_PACKS_DIR = _lib_consumer_read.RULE_PACKS.directory + "/"
_RULE_PACK_PREFIX = "rule-pack-"


# BOM → codec, longest first: the UTF-32 BOMs BEGIN with the UTF-16 ones
# (`ff fe 00 00` starts with `ff fe`), so testing UTF-16 first would decode a
# UTF-32 file as UTF-16 and produce garbage instead of rules.
_BOMS = (
    (codecs.BOM_UTF32_LE, "utf-32-le"), (codecs.BOM_UTF32_BE, "utf-32-be"),
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF16_LE, "utf-16-le"), (codecs.BOM_UTF16_BE, "utf-16-be"),
)


def _decode_manifest(raw: bytes) -> "tuple[str | None, str]":
    """(text, encoding) for a manifest's bytes; text is None if nothing decodes.

    ⛔ NOT `read_text(encoding="utf-8")`. That raises on a UTF-16 file, and both
    readers here used to swallow the exception and `continue` — so a rules
    ConfigMap saved as UTF-16 was invisible to every contract AND left no trace.
    Measured against the decoder `kubectl apply` streams manifests through
    (`k8s.io/apimachinery/pkg/util/yaml.NewYAMLOrJSONDecoder`, 1.36):

        UTF-8              -> applied
        UTF-8 + BOM        -> applied
        UTF-16 BE + BOM    -> APPLIED   <- deploys, and we could not see it
        UTF-16 LE + BOM    -> "incomplete UTF-16 character"

    The BE/LE asymmetry is not a quirk to rely on: `yaml.NewYAMLReader` splits
    documents on the `\\n` BYTE, which stays pair-aligned in big-endian and
    splits mid-codepoint in little-endian. So one of the two ships rules we
    never gated (fail-open) and the other silently ships nothing (silent zero),
    and neither said a word. Decode both, and let the sentinel object to the
    encoding separately — see `_rule_shaped_but_unparsed`.

    `utf-8-sig` for the plain-BOM case is load-bearing beyond tidiness:
    `read_text("utf-8")` keeps the `\\ufeff`, which makes the first line of
    `_header_provenance` fail `startswith("#")` and drops a generated file's
    provenance — reclassifying it as hand-authored platform.
    """
    for bom, enc in _BOMS:
        if raw.startswith(bom):
            # ⛔ Slice the BOM off rather than trusting the codec to. Only
            # `utf-8-sig` and the endian-less `utf-16`/`utf-32` do that; the
            # explicit `utf-16-le` &c. hand back a leading `\\ufeff`, and that
            # single invisible character is the provenance bug described below
            # — reintroduced for four encodings instead of one. Found by
            # test_every_bom_decodes_to_the_same_text, not by reasoning.
            try:
                return raw[len(bom):].decode(
                    "utf-8" if enc == "utf-8-sig" else enc), enc
            except UnicodeDecodeError:
                return None, enc
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return None, "unknown"


def _read_manifest(rel: str) -> "tuple[str | None, str]":
    """`_decode_manifest` for a repo-relative path; (None, "unreadable") on OSError."""
    try:
        raw = (Path(_REPO_ROOT) / rel).read_bytes()
    except OSError:
        return None, "unreadable"
    return _decode_manifest(raw)


def _strip_rules_ext(name: str) -> str | None:
    """`foo.yaml` / `foo.yml` -> `foo`; anything else -> None."""
    for ext in _RULES_FILE_EXTS:
        if name.endswith(ext):
            return name[: -len(ext)]
    return None


@functools.lru_cache(maxsize=1)
def _generated_pack_names() -> frozenset:
    """Pack names that HAVE a rule-packs/ source: the declared rule packs
    (`_lib_consumer_read.rule_pack_files`) that Prometheus' rule loader reads
    and that have a `groups` key.

    ⛔ NOT ON THE CLASSIFICATION PATH. Provenance is decided by
    `_source_pack_alerts`, which compares CONTENT. What is left is an oracle
    used only by the discovery tests (and by `_reset_caches`). Keep it
    honest anyway — the tests read it as ground truth about which packs
    exist:

    A name alone does not make a pack. The file must load and actually have
    `groups:`; a decoy is only convincing if it has to contain the thing it
    claims to be the source of.
    """
    packs = _lib_consumer_read.rule_pack_files(
        Path(_REPO_ROOT, _lib_consumer_read.RULE_PACKS.directory))
    names = set()
    # The packs as Prometheus' rule loader reads them, in one da-read run; a
    # pack the loader refuses is not a pack (it is the sentinel's business).
    for path, rf in zip(packs, _lib_consumer_read.prometheus_rule_file_results(packs)):
        if rf.problem is not None:
            continue
        # ⛔ Test for the KEY, not for a truthy value. `rule-pack-custom-alerts`
        # is generated, and a conf.d tree that declares no `_custom_alerts` —
        # an ordinary, supported deployment — makes the generator emit
        # `groups: []`. Truthiness reads that empty artifact as "no source
        # pack", reclassifies its ConfigMap as hand-authored platform, and then
        # demands `alert_source: platform` on TENANT alerts.
        if "groups" not in rf.documents[0]:
            continue
        stem = _strip_rules_ext(path.name)
        names.add(stem[len(_RULE_PACK_PREFIX):])
    return frozenset(names)


def _is_platform_cm_location(where: str) -> bool:
    """True iff `where` names a rule inside a HAND-AUTHORED rules ConfigMap.

    `where` is "<configmap-path>:<data-key>" on the ConfigMap side and a bare
    rule-pack path on the source side; only the former can be platform.

    THE CRITERION IS "no rule-packs/ source", not "the filename starts with
    configmap-rules-platform". `_iter_repo_alert_rules`'s own docstring already
    states the intent — "any HAND-AUTHORED rules ConfigMap OUTSIDE rule-packs/,
    which is configmap-rules-platform.yaml today AND WHATEVER IS ADDED LATER" —
    and a filename prefix is a proxy for that intent that fails two ways:

      1. it compared the RELATIVE PATH, so `subdir/configmap-rules-platform.yaml`
         (a placement the scanner above deliberately reaches, being recursive)
         escaped every gate bounded by this predicate; and
      2. a hand-authored ConfigMap NOT named `platform*` escaped presence
         coverage entirely, while the alert_source RESERVED contract simultaneously
         reported it as an offender — i.e. the diagnostic pushed the maintainer to
         DELETE the correct label. That is the same "the gate steers you into the
         broken state" failure the prefix itself was introduced to fix, one level up.

    Deriving from the generator's own provenance makes the classification a
    property of the artifact instead of a property of its name. WHICH files
    are read is the declaration's (`_lib_consumer_read.RULE_SOURCES`, #3438);
    a rules file outside it is not read but named by `_rule_tripwire`. Pinned
    by test_platform_cm_discovery_is_content_based and
    test_unknown_provenance_defaults_to_platform.

    A soft spot worth knowing: a PrometheusRule's provenance comes from its
    `da-rule-pack-<x>` object name alone, so the name attests to nothing about
    the CONTENT it wraps.
    """
    return where in _platform_rule_locations()


@functools.lru_cache(maxsize=1)
def _platform_rule_locations() -> frozenset:
    """Every container that is NOT a generated copy of a source rule pack.

    ⛔ Unknown provenance counts as PLATFORM. That direction is the whole point:
    a hand-authored alerting tree the scanner cannot attribute to a generator is
    exactly the thing that must not slip past the alert_source / runbook gates,
    and defaulting the other way would restore, in the classifier, the escape
    hatch that content-based discovery just closed in the finder.

    A container is generated only when it names a source pack that ACTUALLY
    EXISTS — a header or object name pointing at a pack that is not there is
    evidence of a stale or fabricated artifact, not of provenance.
    """
    pack_alerts = _source_pack_alerts()
    out = set()
    for where, doc, claimed in _rule_containers():
        if where.startswith(_RULE_PACKS_DIR):
            continue                       # the source tree is never a deployment
        mine = _alert_names(doc)
        # ⛔ A CLAIM is not provenance; a MATCH is. The header, the object name
        # and the path are all just strings an author controls, so "generated"
        # was previously conferred by writing one line — a forged
        # `# GENERATED from rule-packs/rule-pack-redis.yaml`, or a three-line
        # `groups: []` decoy pack, bought a hand-authored alerting tree its way
        # out of every contract. Requiring the artifact's alerts to actually
        # BE in the pack it names makes forgery cost as much as writing the
        # rules, and makes an empty decoy worthless (an empty pack is a
        # superset of nothing).
        if claimed and mine and mine <= pack_alerts.get(claimed, frozenset()):
            continue
        # …and a copy whose NAME says nothing is still generated if its content
        # says so. Without this, an operator manifest called anything other than
        # `da-rule-pack-<x>` was declared platform-scope and the diagnostic told
        # the maintainer to put `alert_source: platform` on tenant alerts —
        # reserved-label misuse, the exact failure the old prefix criterion was
        # replaced for.
        if mine and any(mine <= alerts for alerts in pack_alerts.values()):
            continue
        if not mine and claimed and claimed in pack_alerts:
            continue                       # recording-only key of a real pack
        out.add(where)
    return frozenset(out)


# Borrowed from the rule-pack copy-drift guard: it is already this repo's
# answer to "are these two serializations the same rule", already handles the
# emitter differences (block scalar vs \n-escaped, `by (x)` vs `by(x)`), and is
# already a CI gate. Reimplementing expr equality here is how the two drifted.
from check_rulepack_sync import _norm_expr  # noqa: E402


def _alert_names(doc) -> frozenset:
    """(alert name, expr) pairs a rules document declares.

    ⛔ The NAME alone is not identity. Matching on names made "generated" cost
    one borrowed string: a hand-authored ConfigMap declaring `alert: RedisDown`
    over `expr: da_platform_writer_lock_lost > 0` was adopted by the redis pack
    and left all four contracts. Pairing the name with its expression means a
    copy has to actually be a copy — which is what the word was supposed to
    mean, and what the comment claiming forgery "costs as much as writing the
    rules" needed in order to be true.

    ⛔ expr is compared through check_rulepack_sync's `_norm_expr`, not a bare
    `.strip()`. A bare string compare made `redis_up == 0` and `redis_up==0`
    different rules: the deployed copy stopped matching its pack and TWELVE
    tenant alerts were reclassified as platform, each with a diagnostic telling
    the maintainer to add the RESERVED `alert_source: platform` — the exact
    misdirection this criterion replaced the filename prefix to avoid, now
    reachable by two characters of whitespace. That helper already exists, is
    already the repo's answer to "are these the same rule", and is already a
    CI gate; a second, weaker definition of expr equality was the bug.
    """
    pairs = set()
    for group in (doc.get("groups") or []):
        if isinstance(group, dict):
            for rule in (group.get("rules") or []):
                if isinstance(rule, dict) and "alert" in rule:
                    body = {k: v for k, v in rule.items() if k != "expr"}
                    pairs.add((rule["alert"],
                               _norm_expr(rule.get("expr", "")),
                               json.dumps(body, sort_keys=True, default=str)))
    return frozenset(pairs)


@functools.lru_cache(maxsize=1)
def _source_pack_alerts() -> dict:
    """{pack name: the (alert, expr) pairs its rule-packs/ source declares}.

    ⛔ Only the pack FILES themselves may define what a pack contains:
    `rule-pack-<name>.{yaml,yml}` under rule-packs/ (the filename is the
    pack's identity, see below). Accepting any container under rule-packs/
    that *claimed* a provenance reopened forgery on the source side: a file
    carrying a fake `# GENERATED from rule-packs/rule-pack-redis.yaml` header
    could write its own alerts into redis's trusted set, and a manifest
    matching them then walked out of all four contracts. Fixing the consumer
    side while leaving the producer side credulous just moved where the lie
    had to be told. (The declaration reads rule-packs/ at the top level only
    since #3438; the depth-agnostic test below stays, since it costs nothing
    and does not depend on that.)
    """
    out, seen_files = {}, {}
    for where, doc, prov in _rule_containers():
        if not prov:
            continue
        # The FILENAME is the pack's identity; its depth is not.
        name = PurePosixPath(where).name
        expected = {f"{_RULE_PACK_PREFIX}{prov}{ext}" for ext in _RULES_FILE_EXTS}
        if where.startswith(_RULE_PACKS_DIR) and name in expected:
            seen_files.setdefault(prov, set()).add(where)
            out.setdefault(prov, set()).update(_alert_names(doc))
    # ⛔ ONE file per pack name. Recursion is right — a pack moved into a
    # subdirectory is still that pack — but `.update()` merging whatever else
    # claims the name is not: dropping a second `rule-pack-redis.yaml` into
    # rule-packs/recipes/examples/conf.d/ silently added its alerts to redis's
    # trusted set, and any manifest matching them then walked out of all four
    # contracts. The earlier top-level-only rule blocked that by accident of
    # path uniqueness, at the cost of a false positive on legitimate moves.
    # Uniqueness is the property both cases actually wanted.
    duplicated = {k: sorted(v) for k, v in seen_files.items() if len(v) > 1}
    if duplicated:
        raise AssertionError(
            f"more than one file claims the same rule pack: {duplicated}. A "
            "pack name is an identity; two files claiming it means one of them "
            "is forging provenance, or a copy was left behind after a move.")
    return {k: frozenset(v) for k, v in out.items()}


# Directories whose YAML the coverage tripwire (`_rule_tripwire`) does not
# look at. Everything else under the shipped roots is in its scope — the point
# is that a real rules artifact cannot escape the declaration by being placed
# somewhere nobody was told about.
# ⛔ Deliberately EMPTY. Once the scan narrowed to k8s/ + operator-manifests/
# + rule-packs/, this list matched zero files in the tree — while remaining a
# working escape hatch: `mkdir k8s/03-monitoring/fixtures/` hid a rules
# ConfigMap from every contract at the cost of one directory name. A filter
# that protects nothing and hides something is pure attack surface. If a real
# fixture tree ever lands under a shipped root, exclude it by explicit path
# with a reason, not by a name any directory can adopt.
_SCAN_SKIP_PARTS = frozenset()


# ⛔ The SHIPPED roots: the coverage tripwire's scope (`_tracked_yaml_paths`).
# Anchored on this repo's own existing answer to "what is
# a deployed manifest": scripts/tools/lint/check_k8s_manifests.py (the L4 raw-
# manifest SAST) scans MANIFEST_ROOT = "k8s" — all of it, recursively.
#
# The previous attempt derived scope by regex-ing `kubectl apply -f` out of
# scripts/setup.sh. That looked more principled and was strictly worse: setup.sh
# is a 62-line Kind bootstrap that deploys namespaces and monitoring and stops,
# so k8s/04-tenant-api/ and k8s/crd/ silently left the scan — and k8s/crd/ is
# deployed by `make assembler-install-crd`, from a Makefile the parser never
# read. One of the two directories it dropped was the very example the previous
# commit claimed to have fixed. A derivation is only as good as the source it
# derives from, and that source was not the deployment SSOT.
#
# RECURSIVE, unlike `kubectl apply -f <dir>`: a rules file moved into a
# subdirectory must not leave every contract unnoticed (CodeRabbit, PR #1270)
# — since #3438 it is not read there, and the tripwire names it.
_SHIPPED_ROOTS = ("k8s/", "operator-manifests/", "rule-packs/", "helm/")


@functools.lru_cache(maxsize=1)
def _expected_rule_files() -> frozenset:
    """The files that MUST each yield at least one container: the
    declaration (`_lib_consumer_read.repo_rule_files`), repo-relative.

    ⛔ The anchor is the declaration itself, not a reading of the tree: a
    reader that goes blind to a file cannot shrink the set it is checked
    against. Coverage is asserted PER FILE (every declared file still
    produces rules), never as a count — a total floor is both too loose (a
    whole pack can go dark under it) and too tight (retiring a pack trips
    it). Fail-closed: a declared source that matches no file raises
    `NoDeclaredRuleFiles`, and an undeclared YAML beside the rule files
    raises `SourcesUnlisted` (`repo_rule_files`).
    """
    return frozenset(_declared_rule_files())


def _declared_rule_files() -> "list[str]":
    """`_lib_consumer_read.repo_rule_files` under `_REPO_ROOT`, as
    repo-relative POSIX paths, in declaration order."""
    root = Path(_REPO_ROOT)
    return [p.relative_to(root).as_posix()
            for p in _lib_consumer_read.repo_rule_files(root)]


def _tracked_yaml_paths():
    """Every tracked YAML file under the shipped roots, minus fixture/vendor
    trees. Sorted, repo-relative. The coverage tripwire's scope
    (`_rule_tripwire`); no rule file is read from this list."""
    # ⛔ List everything and filter case-INSENSITIVELY. `git ls-files '*.yaml'`
    # matches the pathspec case-sensitively, so a file spelled `.YAML` — legal,
    # and the exact trick a red-team run used — is simply never listed. Filtering
    # in Python is the difference between "the scanner declined to look" and
    # "the scanner looked and found nothing".
    # ⛔ `-z` + NUL split, matching _git_tracked_paths below. Plain `.split()`
    # breaks on whitespace, so `k8s/configmap rules platform.yaml` arrives as
    # three fragments and none of them opens; and without -z git applies
    # core.quotePath, so a CJK filename comes back as `"k8s/r\303\250gles.yaml"`
    # and fails the suffix test. In a zh-primary repo that is not hypothetical,
    # and both failures are the scanner declining to look — the exact thing the
    # case-insensitive suffix match above exists to prevent.
    # ⛔ `surrogateescape`, not `replace` (#1374): -z hands back the raw path
    # bytes, and a name that is not valid UTF-8 must keep its identity — `replace`
    # turns it into a different name that opens nothing. Same policy as
    # _lint_helpers.diff_changed_paths.
    out = subprocess.run(
        ["git", "-C", _REPO_ROOT, "ls-files", "-z"],
        capture_output=True, text=True, encoding="utf-8", errors="surrogateescape",
        timeout=60, check=True).stdout
    paths = sorted(p for p in out.split("\0") if p
                   and p.lower().endswith((".yaml", ".yml"))
                   and not (_SCAN_SKIP_PARTS & set(PurePosixPath(p).parts))
                   and p.startswith(_SHIPPED_ROOTS))
    if not paths:
        # ⛔ Same fail-closed guard as `_expected_rule_files`, and for the same
        # reason: an empty scan is indistinguishable from a clean one. Every
        # check downstream iterates this list, so returning `[]` when git
        # fails makes the coverage tripwire pass by examining nothing.
        # `raise`, not `assert`: `python -O` strips the latter.
        raise AssertionError(
            f"no tracked YAML under {_SHIPPED_ROOTS} in {_REPO_ROOT} — either "
            "the repo layout moved or `git ls-files` failed; an empty scan "
            "satisfies every contract that reads it")
    return paths


@functools.lru_cache(maxsize=1)
def _rule_containers() -> tuple:
    """Cached tuple form of _iter_rule_containers."""
    return tuple(_iter_rule_containers())


gen_re = re.compile(r"GENERATED from rule-packs/rule-pack-([A-Za-z0-9_-]+)\.ya?ml")

def _header_provenance(chunk: str):
    """The generator header, read ONLY from a document's leading comments.

    ⛔ Two things must both be narrow here, and the first version was
    narrow in neither. Searching the whole file's raw text means the string
    confers provenance from anywhere it appears — including inside an
    `annotations.summary` value, which is attacker- or tenant-writable
    data. And computing it once per FILE then applying it to every document
    means a `---` separator is the best hiding place in the repo: append a
    hand-authored ConfigMap to any generated one and it inherits the
    header. That is the escape hatch content-based discovery just closed,
    reopened one layer up and cheaper than before.

    So the header is read from the text before the file's first document and
    applies to the FIRST object of kubectl's reading only (`RuleFile.document
    == 1`, `_containers_of`) — for a `kind: List`, its first item.
    """
    for line in chunk.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped == "---":
            continue                 # document-start marker, not content
        if not stripped.startswith("#"):
            return None              # past the leading comment block
        m = gen_re.search(stripped)
        if m:
            return m.group(1)
    return None

def _iter_rule_containers():
    """Yield (where, rules_doc, provenance) for every rule file the repo ships.

    The files are the declaration's (`_expected_rule_files`), read by
    `_lib_consumer_read.rule_files_in` — every data value of a declared
    ConfigMap is a rule file (`only_declared=False`: the path declared it,
    and a value that is not one is a problem, not a skip) — then the
    registered chart values keys (`chart_values_rule_files`).

    `where` is `<file>:<data key>` for a ConfigMap's rule file, `<file>:<key
    path>` for a chart values key, and the file for a PrometheusRule or a
    plain rule file. `rules_doc` is the rule loader's reading; a rule file it
    refuses, or a file no reader reads, is `{"_problem": <reason>}` for the
    sentinel, never dropped.

    `provenance` is the source pack this artifact was generated FROM, or None
    when it is hand-authored. It is read from the generator's own header
    ("GENERATED from rule-packs/rule-pack-X.yaml … DO NOT EDIT", first object
    only — see `_header_provenance`), from a `da-rule-pack-X` object name, or
    from a plain rule file's own name `rule-pack-X`. None means "nobody
    generated this", which is what makes it a platform artifact — see
    _is_platform_cm_location.
    """
    yield from _containers_of(_REPO_ROOT, _declared_rule_files())
    for rf in _lib_consumer_read.chart_values_rule_files(_REPO_ROOT):
        yield f"{rf.manifest}:{rf.key}", _rule_doc(rf), None


def _rule_doc(rf) -> dict:
    """The rule loader's reading of *rf*, or the sentinel's marker."""
    if rf.problem is not None:
        return {"_problem": rf.problem}
    return rf.documents[0]


def _containers_of(root: str, rels: "list[str]"):
    """(where, rules_doc, provenance) for the rule files of the files *rels*
    (repo-relative) under *root*, in order — `rule_files_in`'s answer, with
    this module's provenance rules on top."""
    paths = [str(Path(root, rel)) for rel in rels]
    found = _lib_consumer_read.rule_files_in(paths, only_declared=False, keep_refusals=True)
    for rel, path in zip(rels, paths):
        try:
            text, _enc = _decode_manifest(Path(path).read_bytes())
        except OSError:
            text = None
        header = _header_provenance(text) if text is not None else None
        for rf in found[path]:
            if rf.unread:
                yield rel, {"_problem": rf.problem}, None
                continue
            first = header if rf.document == 1 else None
            if rf.kind == "ConfigMap":
                yield f"{rel}:{rf.key}", _rule_doc(rf), first
            elif rf.kind == "PrometheusRule":
                name = rf.object_name or ""
                prov = (name[len("da-rule-pack-"):]
                        if name.startswith("da-rule-pack-") else first)
                yield rel, _rule_doc(rf), prov
            else:
                # A plain rule file: the file itself is what Prometheus loads.
                stem = _strip_rules_ext(PurePosixPath(rel).name)
                prov = (stem[len(_RULE_PACK_PREFIX):]
                        if stem and stem.startswith(_RULE_PACK_PREFIX) else None)
                yield rel, _rule_doc(rf), prov


def _reset_caches() -> None:
    """Clear every memo that reads the tree, in one call.

    ⛔ Five functions here are `lru_cache`d and three of them read
    `_rule_containers()`. A test that swaps the container source and clears two
    of the three leaves the third holding values computed from the fake input —
    and because these caches live at MODULE scope rather than inside one
    pytest file, that staleness outlives the test. Enumerating them at the call
    site is how one gets missed; enumerating them here is how it stays fixed.
    """
    for fn in (_generated_pack_names, _platform_rule_locations,
               _source_pack_alerts, _expected_rule_files, _rule_containers,
               _rule_shaped_but_unparsed):
        fn.cache_clear()


def _iter_repo_alert_rules():
    """Yield (where, rule) for EVERY alerting rule the repo ships.

    Single scanner on purpose: the sentinel contract and the alert_source
    contract below are both "this discriminator is RESERVED" invariants, and a
    reserved-value claim is only as good as its coverage — two scanners would
    let one drift and silently narrow the other's guarantee.
    """
    for where, doc, _prov in _rule_containers():
        for group in (doc.get("groups") or []):
            if not isinstance(group, dict):
                continue
            for rule in (group.get("rules") or []):
                if isinstance(rule, dict) and "alert" in rule:
                    yield where, rule


@functools.lru_cache(maxsize=1)
def _rule_shaped_but_unparsed():
    """Declared rule files that yield no rules, or would not reach Prometheus.
    Must stay empty.

    ⛔ The silent-zero is the failure this exists for. A rule file that is
    refused contributes nothing, and every count-based floor is still
    satisfied by the others, so no assertion anywhere notices that a whole
    block of alerts is unguarded. Every declared file and every rule file in
    it is read (`_rule_containers`); this names each one that does not load,
    and each declared file whose bytes would not reach the cluster as read.

    YAML OUTSIDE the declaration is not this function's business: whether it
    looks like rules is `_rule_tripwire`'s alarm, which reads nothing into
    the contracts.
    """
    offenders = []
    for rel in _expected_rule_files():
        text, enc = _read_manifest(rel)
        # ⛔ A rules file is UTF-8 or it is a bug. Whether its rules reach the
        # cluster depends on the byte order: UTF-16 BE applies, UTF-16 LE dies
        # on `yaml.NewYAMLReader`'s `\n`-byte document split. `utf-8-sig` is
        # NOT an offender: kubectl applies a BOM-prefixed UTF-8 manifest
        # (measured), and `_decode_manifest` slices the BOM so it costs the
        # file no provenance.
        if text is None and enc != "unreadable":
            offenders.append((rel, f"no standard encoding decodes this file ({enc}); "
                                   "neither this reader nor kubectl can read it"))
        elif text is not None and enc not in ("utf-8", "utf-8-sig"):
            offenders.append((rel, f"a declared rule file that ships as {enc}, not UTF-8 "
                                   "— whether kubectl applies it depends on the byte "
                                   "order"))
    for where, doc, _prov in _rule_containers():
        if "_problem" in doc:
            offenders.append((where, f"its rules do not load: {doc['_problem']}"))
        elif "groups" not in doc:
            # ⛔ The empty container: a PrometheusRule with an empty spec, or a
            # data key whose body emptied out — the rule loader reads it, no
            # groups, zero rules contributed. `groups: []` is NOT this: a
            # generator with nothing to emit legitimately produces it.
            offenders.append((where, "rule container yields no rules at all — "
                                     "an empty PrometheusRule spec, or a data "
                                     "key whose body emptied out"))
    return tuple(offenders)
