"""Rule-tree scanner — what alerting rules this repo actually ships.

Content-based discovery over the shipped roots, plus the provenance rules that
decide which of those rules belong to the hand-authored PLATFORM tree rather
than to a generated copy of a rule pack.

⛔ This lives in a module, not in a test file, for one concrete reason: it used
to live in tests/ops/test_generate_routes_orchestration.py, where nothing else
could import it — so `_real_platform_label_sets` there and
`platform_alert_identities` in scripts/tools/ops/_grar_validate.py each grew
their OWN answer to "what is the platform rule tree", both by hard-coding the
single filename `configmap-rules-platform.yaml`. Three readers, no cross-check,
and a scanner whose whole point is that the filename is not the criterion.
The duplication was a consequence of the location.

The production reader keeps its own narrow path on purpose — it ships inside an
image that carries no repo tree and falls back to a constant when the ConfigMap
is unreachable, so it cannot depend on `git ls-files`. What it gains from this
module is a CROSS-CHECK: see
test_generate_routes_orchestration.py::TestPlatformReaderParity.
"""
from __future__ import annotations

import base64
import codecs
import functools
import json
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath

import yaml

# Guarded and deduped, unlike the bare double-insert most siblings use: this
# module is imported by test suites whose conftest has already added the same
# directory, and `check_rulepack_sync` below inserts one more on its own.
_THIS_DIR = str(Path(__file__).resolve().parent)
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)


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
# in-module caller by design: discovery is content-based and this is only used
# for HUMAN-READABLE messages. Deleting it as "unused" breaks that lockstep.
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
    """Pack names that HAVE a rule-packs/ source, by filename and shape.

    ⛔ NOT ON THE CLASSIFICATION PATH ANY MORE. This used to decide whether a
    ConfigMap was a generated copy, and its guards below were written against
    that: a `touch rule-packs/rule-pack-x.yaml` decoy reclassifying
    configmap-rules-x.yaml as generated and dropping every platform alert inside
    it out of all four contracts. That chain now runs through
    `_source_pack_alerts`, which compares CONTENT and never asks this function
    anything — so the decoy is priced by the subset test there, not here.

    What is left is a filename-and-shape oracle used only by the discovery tests
    (and by `_reset_caches`). Keep the guards honest anyway — the tests read it
    as ground truth about which packs exist, and an oracle that answers loosely
    makes the assertions that consult it loose in the same direction — but do
    not read the ⛔ below as a live defence:

    RECURSIVE, matching `_source_pack_alerts` over the same tree: a source pack
    moved into a subdirectory must not read as "source deleted".

    A name alone does not make a pack. The file must parse as YAML and actually
    declare `groups:`; a decoy is only convincing if it has to contain the thing
    it claims to be the source of.
    """
    packs_dir = Path(_REPO_ROOT) / "rule-packs"
    names = set()
    for path in packs_dir.rglob("*"):
        if not path.is_file():
            continue
        stem = _strip_rules_ext(path.name)
        if not (stem and stem.startswith(_RULE_PACK_PREFIX)):
            continue
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (yaml.YAMLError, UnicodeDecodeError, OSError):
            continue
        # ⛔ Test for the KEY, not for a truthy value. `rule-pack-custom-alerts`
        # is generated, and a conf.d tree that declares no `_custom_alerts` —
        # an ordinary, supported deployment — makes the generator emit
        # `groups: []`. Truthiness reads that empty artifact as "no source pack",
        # reclassifies its ConfigMap as hand-authored platform, and then demands
        # `alert_source: platform` on TENANT alerts: a reserved value that pipes
        # them into the NOC channel. Steering the maintainer into a broken state
        # is precisely why the filename criterion had to go; requiring a truthy
        # `groups` reinstalls it from the other side.
        if not isinstance(doc, dict) or "groups" not in doc:
            continue
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
    property of the artifact instead of a property of its name — and the
    matching DISCOVERY rewrite (see `_iter_rule_containers`) means membership
    no longer depends on filename or directory either. Scope today: 63
    containers across the four `_SHIPPED_ROOTS` / 408 rules, of which exactly
    one container is hand-authored. Pinned by test_platform_cm_discovery_is_content_based
    and test_unknown_provenance_defaults_to_platform.

    The last directory criterion is GONE. The bare `groups:` shape (a plain
    Prometheus rule file with no ConfigMap or PrometheusRule wrapper) used to be
    recognised only under rule-packs/, because it is indistinguishable from this
    repo's 19 `tests/rulepacks/**/*.rules.yaml` extracts. That reason expired
    when the scan narrowed to shipped roots — `tests/` is no longer read at all,
    so the path test was protecting against a collision that can no longer
    happen while leaving a real hole: a hand-authored `extra-platform-rules.yaml`
    under k8s/ was not discovered. Removing it costs nothing on today's tree
    (measured: identical containers, rules and platform set) and closes that.

    Two further soft spots, both currently harmless and both worth knowing:
    a PrometheusRule's provenance comes from its `da-rule-pack-<x>` object name
    alone, so the name attests to nothing about the CONTENT it wraps; and
    `groups: []` passes the shape test (a generator with nothing to emit
    legitimately produces one), so an unrelated ConfigMap with an empty
    `groups` key would be read as a rules container contributing no rules.
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
        if where.startswith("rule-packs/"):
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
    `rule-pack-<name>.{yaml,yml}` under rule-packs/, at ANY depth (the filename
    is the pack's identity, see below). Accepting any
    container under rule-packs/ that *claimed* a provenance reopened forgery on
    the source side: rule-packs/ is the one tree scanned recursively, so a file
    dropped into rule-packs/recipes/examples/conf.d/ carrying a fake
    `# GENERATED from rule-packs/rule-pack-redis.yaml` header could write its
    own alerts into redis's trusted set, and a manifest matching them then
    walked out of all four contracts. Fixing the consumer side while leaving
    the producer side credulous just moved where the lie had to be told.
    """
    out, seen_files = {}, {}
    for where, doc, prov in _rule_containers():
        if not prov:
            continue
        # RECURSIVE, matching `_generated_pack_names`'s rglob. Requiring the
        # pack to sit at the top of rule-packs/ made the two helpers disagree
        # about the same question: move a pack into a subdirectory and one still
        # knew it existed while the other did not, so its deploy copies were
        # reclassified as hand-authored platform trees — with, again, the
        # reserved-label misdiagnosis. The FILENAME is the pack's identity; its
        # depth is not.
        name = PurePosixPath(where).name
        expected = {f"{_RULE_PACK_PREFIX}{prov}{ext}" for ext in _RULES_FILE_EXTS}
        if where.startswith("rule-packs/") and name in expected:
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


# Directories whose YAML is deliberately NOT shipped alerting configuration:
# parser fixtures and build/vendor output. Everything else in the tree is in
# scope — the point of content-based discovery is that a real rules artifact
# cannot escape by being placed somewhere the scanner was never told about.
# ⛔ Deliberately EMPTY. Once the scan narrowed to k8s/ + operator-manifests/
# + rule-packs/, this list matched zero files in the tree — while remaining a
# working escape hatch: `mkdir k8s/03-monitoring/fixtures/` hid a rules
# ConfigMap from every contract at the cost of one directory name. A filter
# that protects nothing and hides something is pure attack surface. If a real
# fixture tree ever lands under a shipped root, exclude it by explicit path
# with a reason, not by a name any directory can adopt.
_SCAN_SKIP_PARTS = frozenset()


# ⛔ The SHIPPED roots. Anchored on this repo's own existing answer to "what is
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
# RECURSIVE, unlike `kubectl apply -f <dir>`: coverage should not depend on how
# a maintainer happens to invoke kubectl, and a rules file moved into a
# subdirectory must not leave every contract (CodeRabbit, PR #1270).
_SHIPPED_ROOTS = ("k8s/", "operator-manifests/", "rule-packs/", "helm/")


@functools.lru_cache(maxsize=1)
def _expected_rule_files() -> frozenset:
    """Files that MUST each yield at least one container, from `git ls-files`.

    ⛔ The anchor has to live OUTSIDE the scanner. Deriving a floor from
    `_source_pack_alerts()` — which reads the scanner — means a scanner that
    goes blind to a tree also shrinks the number it is checked against, and the
    check passes while the coverage evaporates. `git ls-files` knows what the
    repo ships without asking any of this module's opinions.

    Coverage is asserted PER FILE rather than as a count. A total floor is both
    too loose and too tight at once, measured on the tree as it stands (366
    non-platform alerts, old floor `>= 350`):
      too loose — 16 rules could vanish unnoticed; emptying
        rule-packs/rule-pack-liveness.yaml leaves 363 and the floor stays green
        while a whole shipped file has gone dark.
      too tight — retiring any of 13 of the 16 packs drops below it (redis
        -18 -> 348, mariadb -54 -> 312). The failure text then tells the
        maintainer their legitimate retirement broke a reserved-value contract.

    "Every shipped rules file still produces rules" says the thing actually
    meant, and it updates itself when a pack is retired: the retired file
    leaves `git ls-files`, so it leaves the expected set too.

    Three call sites depend on this: the discovery-scope test and the two
    reserved-value contracts (component="sentinel" and alert_source), each of
    which needs non-vacuity over the files it scans.
    """
    globs = ("rule-packs/rule-pack-*.yaml", "rule-packs/rule-pack-*.yml",
             "operator-manifests/da-rule-pack-*.yaml",
             "k8s/03-monitoring/configmap-rules-*.yaml",
             "k8s/03-monitoring/configmap-rules-*.yml")
    out = subprocess.run(["git", "-C", _REPO_ROOT, "ls-files", "-z", *globs],
                         capture_output=True, text=True, timeout=60,
                         check=True).stdout
    files = frozenset(p for p in out.split("\0") if p)
    if not files:
        # `raise`, not `assert` — this is a fail-closed check and `python -O`
        # strips asserts. An empty anchor silently satisfies every per-file
        # coverage assertion that reads it, which is the one outcome this
        # function must never produce quietly.
        raise AssertionError(
            f"no tracked rules files matched {globs} under {_REPO_ROOT} — "
            "either the repo layout moved or `git ls-files` failed; both make "
            "the per-file coverage floor vacuous")
    return files


def _tracked_yaml_paths():
    """Every tracked YAML file, minus fixture/vendor trees. Sorted, repo-relative."""
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
    out = subprocess.run(
        ["git", "-C", _REPO_ROOT, "ls-files", "-z"],
        capture_output=True, text=True, timeout=60, check=True).stdout
    return sorted(p for p in out.split("\0") if p
                  and p.lower().endswith((".yaml", ".yml"))
                  and not (_SCAN_SKIP_PARTS & set(PurePosixPath(p).parts))
                  and p.startswith(_SHIPPED_ROOTS))


@functools.lru_cache(maxsize=1)
def _rule_containers() -> tuple:
    """Cached tuple form of _iter_rule_containers — see F11 note there."""
    return tuple(_iter_rule_containers())


def _is_rule_groups(value) -> bool:
    """Prometheus rule-group shape, not merely a key spelled `groups`.

    ⛔ `groups:` is a popular word. k8s/04-tenant-api/configmap-rbac.yaml
    uses it for RBAC subject groups (name / tenants / permissions), and a
    scanner that accepted any `groups` key classified that file as a
    hand-authored PLATFORM alerting tree. A rule group always carries
    `rules`; an empty list is allowed because a generator with nothing to
    emit legitimately produces one (see the custom-alerts pack).
    """
    if not isinstance(value, list):
        return False
    return not value or any(
        isinstance(g, dict) and "rules" in g for g in value)

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

def _documents(text: str):
    """(is-first-document, parsed doc) per YAML document.

    ⛔ Let the YAML parser find document boundaries. Hand-splitting on
    `line == "---"` missed the legal spellings `--- ` (one trailing space)
    and `--- # comment`: the file then failed to split, `safe_load` raised
    ComposerError on the multi-document text, and the `except` swallowed
    the ENTIRE FILE — a whole rules ConfigMap gone, alert counts unchanged,
    so no floor noticed. Worse, `_rule_shaped_but_unparsed` uses
    `safe_load_all`, which parses that same file fine, so the one guard
    meant to catch silent-zero could not see it. Two parsers disagreeing
    about how many documents a file has WAS the hole; there is one now.

    The generator header belongs to the file's opening comments, so it
    applies to the first document only — a hand-authored document appended
    after a `---` does not inherit its neighbour's provenance.
    """
    try:
        for i, doc in enumerate(yaml.safe_load_all(text)):
            yield i == 0, doc
    except yaml.YAMLError:
        return


def _iter_rule_containers():
    """Yield (where, rules_doc, provenance) for every rules-bearing document.

    ⛔ DISCOVERY IS CONTENT-BASED. A file is in scope because of what it
    CONTAINS, never because of its name or its directory. The previous scanner
    walked `configmap-rules-*` under k8s/03-monitoring/ only, which meant the
    reserved-value guarantees below were really "…among files we happened to
    look at": a rules ConfigMap named differently, spelled .YAML, or placed in
    another directory was not misclassified, it was never seen. Worse, the
    repo's 16 `kind: PrometheusRule` manifests under operator-manifests/ — a
    first-class deployment path — were outside the scan entirely.

    Three container shapes, all recognised by structure:
      * `kind: ConfigMap`      → every data key whose body parses to a mapping
      * `kind: PrometheusRule` → `spec.groups`
      * a bare `groups:` doc   → a wrapper-less Prometheus rule file, ANYWHERE
        under the shipped roots. It used to be recognised only inside
        rule-packs/ and therefore only ever meant "a source pack"; that path
        test is gone, so `k8s/…/extra-platform-rules.yaml` is now discovered
        too — and, having no provenance, classified as platform.

    Multi-document YAML is walked in full; a rules doc hiding behind a `---`
    separator is exactly the kind of placement this is meant to stop mattering.

    `provenance` is the source pack this artifact was generated FROM, or None
    when it is hand-authored. It is read from the generator's own header
    ("GENERATED from rule-packs/rule-pack-X.yaml … DO NOT EDIT"), from a
    `da-rule-pack-X` object name, or from the file's own location under
    rule-packs/. None means "nobody generated this", which is what makes it a
    platform artifact — see _is_platform_cm_location.
    """
    for rel in _tracked_yaml_paths():
        text, _enc = _read_manifest(rel)
        if text is None:
            continue          # reported by _rule_shaped_but_unparsed, not dropped
        yield from _containers_from_text(rel, text)


def _containers_from_text(rel: str, text: str):
    """The per-file half of `_iter_rule_containers`, split out to be TESTABLE.

    ⛔ Every guard below — the widened prefilter, `kind: List` unwrapping,
    binaryData decoding, parser-driven document splitting, header locality —
    was added because a red-team run walked through it, and NONE of them had a
    regression test: reverting any one of them left the suite green, because
    every assertion in this file reads the live tree, and the live tree
    contains no attack. A guard with no fixture is a guard with a countdown.
    Keeping the parsing reachable without git or a repo checkout is what lets
    TestContainerDiscovery pin each one with the shape that motivated it.
    """
    # ⛔ NO raw-text prefilter. Every spelling tried was wrong in a new way —
    # a literal `"groups:"` missed `groups :` and the quoted key `"groups":`;
    # an anchored pattern missed the deployed tree, whose rules live in quoted
    # scalars; the bare word missed base64 binaryData; and even then it missed
    # `"\\x67roups:"`, a YAML escape the generated files' own double-quoted
    # form makes entirely natural. A prefilter the criterion can be smuggled
    # past is not an optimisation, it IS the criterion. `_is_rule_groups`
    # below is the real test; parsing ~90 files costs about a second.

    file_header = _header_provenance(text)
    for is_first, top_doc in _documents(text):
        header_prov = file_header if is_first else None
        docs_todo = [top_doc]
        while docs_todo:
            doc = docs_todo.pop(0)
            if not isinstance(doc, dict):
                continue
            kind = doc.get("kind")
            if isinstance(doc.get("items"), list):
                # ⛔ Mirror kubectl, which does NOT look at the kind name.
                # `apply` builds with `.Flatten()`, whose FlattenListVisitor
                # defers to `Unstructured.IsList()` — and that is literally
                # "is `items` a list". `decodeToList` then back-fills each
                # item's kind from the list's own (`ConfigMapList` → ConfigMap),
                # so `kubectl get cm -o yaml` output round-trips, and so does a
                # wrapper with a kind nobody has ever heard of. Matching the
                # bare `List` spelling left every named variant open, and the
                # worse face is SUBTRACTIVE: wrapping an EXISTING deployed file
                # removed 14 alerts from every contract while all five floors
                # still held.
                # `kind: List` is what `kubectl get -o yaml` emits for multiple
                # objects and what `kubectl apply -f` happily takes back. Three
                # hard-coded kinds meant a wrapper nobody had to invent made the
                # contents invisible — including, measured, a tenant alert
                # wearing the RESERVED `alert_source: platform`, which turned the
                # reserved-value contract off entirely.
                # ⛔ …and the outer document's OWN content is dropped, by
                # kubectl and therefore by us. Verified against
                # `unstructured.UnstructuredJSONScheme` (k8s 1.36 apimachinery),
                # which is what `apply` decodes with:
                #   ConfigMap + data                 -> 1 object, data kept
                #   ConfigMap + `items: []` + data   -> 0 objects
                #   items non-empty + own data       -> only the items
                # So skipping it is CORRECT — but it is a SILENT ZERO, and a
                # cheap accident: paste the `items:` key out of a
                # `kubectl get -o yaml` dump into a hand-authored ConfigMap, or
                # delete the wrapped objects and leave `items: []` behind, and
                # your platform rules stop deploying with nothing saying so.
                # Hand it to the sentinel rather than dropping it quietly.
                if (doc.get("data") or doc.get("binaryData")
                        or isinstance(doc.get("spec"), dict)):
                    yield (f"{rel}:{doc.get('metadata', {}).get('name', '?')}",
                           {"_own_content_shadowed_by_items": True}, header_prov)
                for item in (doc.get("items") or []):
                    if isinstance(item, dict):
                        docs_todo.append(item)
                continue
            if kind == "ConfigMap":
                prov = header_prov
                # ⛔ binaryData counts. kubelet's configmap MakePayload falls
                # back to BinaryData when a projected `items` key is absent from
                # Data (k8s pkg/volume/configmap/configmap.go), and this repo
                # mounts its rules exactly that way — so base64 is a real
                # delivery path, not an obfuscation. Reading only `data` left it
                # unscanned AND defeated the raw-text `- alert:` tripwire.
                entries = dict(doc.get("data") or {})
                for key, blob in (doc.get("binaryData") or {}).items():
                    try:
                        entries.setdefault(key, base64.b64decode(blob).decode("utf-8"))
                    except (ValueError, UnicodeDecodeError):
                        continue
                for key, body in entries.items():
                    try:
                        inner = yaml.safe_load(body) if isinstance(body, str) else body
                    except yaml.YAMLError:
                        # ⛔ Not silent — but only when the body was TRYING to be
                        # rules. The OUTER file parses, so the whole-file tripwire
                        # sees nothing wrong; an indentation slip inside one `|`
                        # block therefore deleted a whole block of alerts with no
                        # counter moving. A ConfigMap may legitimately carry
                        # non-YAML data keys (this repo ships a markdown
                        # Alertmanager template that way), so flag only bodies
                        # that visibly declare rules.
                        # The KEY's extension is the declaration of intent: a
                        # `.yml`/`.yaml` data key promises YAML, a `.md` one does
                        # not. (Matching on `- alert:` in the body was too loose —
                        # this repo's Alertmanager markdown template contains that
                        # exact string as a documentation example.)
                        if key.lower().endswith((".yml", ".yaml")):
                            yield f"{rel}:{key}", {"_unparsable_body": True}, prov
                        continue
                    if isinstance(inner, dict) and (
                            _is_rule_groups(inner.get("groups"))
                            or isinstance(inner.get("spec"), dict)):
                        yield f"{rel}:{key}", inner, prov
            elif kind == "PrometheusRule":
                # ⛔ `groups` at the TOP level of a PrometheusRule is the
                # commonest paste error and the tripwire below cannot see it:
                # that check receives `doc["spec"]`, so the misplaced key is
                # outside what it is handed. Normalise here, where both halves
                # are still visible, and let the tripwire flag the empty spec.
                if _is_rule_groups(doc.get("groups")) and not (
                        isinstance(doc.get("spec"), dict) and doc["spec"].get("groups")):
                    yield rel, {"_misplaced_groups": doc.get("groups")}, None
                    continue
                name = (doc.get("metadata") or {}).get("name") or ""
                prov = (name[len("da-rule-pack-"):]
                        if name.startswith("da-rule-pack-") else header_prov)
                yield rel, (doc.get("spec") or {}), prov
            elif _is_rule_groups(doc.get("groups")):
                stem = _strip_rules_ext(PurePosixPath(rel).name)
                prov = (stem[len(_RULE_PACK_PREFIX):]
                        if stem and stem.startswith(_RULE_PACK_PREFIX) else None)
                yield rel, doc, prov


def _reset_caches() -> None:
    """Clear every memo that reads the tree, in one call.

    ⛔ Six functions here are `lru_cache`d and three of them read
    `_rule_containers()`. A test that swaps the container source and clears two
    of the three leaves the third holding values computed from the fake input —
    and because these caches now live at MODULE scope rather than inside one
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
    """Containers that LOOK like they hold rules but yield none. Must stay empty.

    ⛔ The silent-zero is the failure this exists for. A ConfigMap data key whose
    body nests its rules under `spec.groups` (the PrometheusRule shape, easy to
    paste by mistake) parses fine, classifies fine, and contributes nothing —
    every count-based floor is still satisfied by the other keys, so no assertion
    anywhere notices that a whole block of alerts is unguarded. Yielding zero
    rules from something that is visibly rule-shaped is never correct; it is
    either a nesting mistake or a scanner that has stopped understanding a shape
    the repo now uses.
    """
    offenders = []
    # ⛔ A file that will not parse is the loudest silent-zero of all: it
    # contributes nothing and no branch above ever sees it. 68 tracked YAML in
    # this repo currently fail to parse (helm templates carrying Go actions);
    # none holds rules today, so only flag one that visibly tries to.
    for rel in _tracked_yaml_paths():
        text, enc = _read_manifest(rel)
        # ⛔ UNCONDITIONAL, and it cannot be conditioned on rule-shape because
        # nothing here can read the file to find out. A shipped .yaml that no
        # standard encoding decodes is not a manifest kubectl can apply either
        # (its YAML parser takes UTF-8/16/32 and nothing else), so this fires on
        # a real defect every time — measured: zero tracked YAML in this repo is
        # anything but plain UTF-8, so the floor starts at zero and stays there.
        if text is None and enc != "unreadable":
            offenders.append((rel, f"no standard encoding decodes this file "
                                   f"({enc}); the scanner cannot read it and "
                                   "neither can kubectl"))
            continue
        if text is None:
            continue
        # ⛔ Regex, not a literal. `-  alert:` (two spaces) is the same YAML and
        # evaded the substring, so the "declares alerts but will not parse"
        # tripwire could be stepped around with a whitespace edit.
        if not re.search(r"^\s*-\s+alert\s*:", text, re.M):
            continue
        # ⛔ A rules file is UTF-8 or it is a bug, even though the decoder above
        # now reads the others. Whether the rules reach the cluster depends on
        # the byte order: UTF-16 BE applies, UTF-16 LE dies on
        # `yaml.NewYAMLReader`'s `\n`-byte document split. "Deploys or silently
        # does not, depending on endianness" is not a state to leave shipped, and
        # every other reader in this repo opens manifests as UTF-8.
        # `utf-8-sig` is deliberately NOT an offender: kubectl applies a
        # BOM-prefixed UTF-8 manifest (measured), and the slice above means the
        # BOM no longer costs the file its provenance. It works, so saying it is
        # broken would be a false positive — and the message below would be a
        # lie, since endianness has nothing to do with it.
        if enc not in ("utf-8", "utf-8-sig"):
            offenders.append((rel, f"declares alerts but ships as {enc}, not "
                                   "UTF-8 — whether kubectl applies it depends "
                                   "on the byte order"))
            continue
        try:
            list(yaml.safe_load_all(text))
        except yaml.YAMLError as exc:
            offenders.append((rel, f"declares alerts but will not parse: {exc.__class__.__name__}"))
    for where, doc, _prov in _rule_containers():
        if doc.get("groups"):
            continue
        if "_unparsable_body" in doc:
            offenders.append((where, "ConfigMap data key does not parse as YAML"))
            continue
        if "_misplaced_groups" in doc:
            offenders.append((where, "PrometheusRule with groups: at the top "
                                     "level instead of under spec:"))
            continue
        if "_own_content_shadowed_by_items" in doc:
            offenders.append((where, "an `items:` key makes kubectl treat this "
                                     "document as a LIST and discard its own "
                                     "data/spec — the rules in it do not deploy"))
            continue
        nested = doc.get("spec")
        if isinstance(nested, dict) and nested.get("groups"):
            offenders.append((where, "rules nested under spec.groups"))
        elif isinstance(doc.get("rules"), list):
            offenders.append((where, "rules present but no enclosing groups:"))
    return tuple(offenders)
