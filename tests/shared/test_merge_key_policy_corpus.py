"""Python half of tests/shared/merge_key_policy_corpus.json (#2677).

The corpus is `_domain_policy.yaml` documents built from the YAML features
that decide what a merge key is and what it supplies: anchors and aliases
(as a key, as a value, as a merge source), merge-key spellings (`<<` plain,
quoted, under `!!merge`, `!<tag:yaml.org,2002:merge>`, a `%TAG` handle,
the non-specific `!`; a tagged `!!merge q`), merge sequences, own keys
beside merges, and merges nested in merge values, at every level of a
policy (the top, `domain_policies`, a domain, its `constraints`); also
merge-tagged `tenants` items (the generator reads those as source text),
refused `require_critical_escalation` values wherever written (in place
under constraints included: hub #2486 PR-7c round 2, B1), and repeated keys
where no policy reader looks. Rows with nothing enforced are thinned to one
in four. Each row holds what the route generator's own reader
(`_parse_config_files`, PyYAML) makes of it: `unusable` (the file or its
domain_policies block is dropped) or, per domain with a non-empty
`forbidden_receiver_types` or `allowed_receiver_types`, its tenants and
forbidden types, its allowed types when that list is non-empty (`allowed`:
the string entries; a list of none still restricts), and `escalation: true`
when its require_critical_escalation is True (the generator's `is True`; any
other value it reads is not enforced).

After the seeded rows come deterministic `shape` rows (hub #2486, PR-7c/7d):
one per single-file divergence measured on main — merge-tagged collections
as values (#2730 §4), non-string `tenants` items (§2), the non-specific
`! "<<"` key and `! "true"`-style values (§6), multi-document streams (§1),
a top-level `tenants:` (§5), `<<: *previous` chains of 50 and 200, one
anchored list aliased by a domain (#2715), and the PR-7c round 2 shapes:
receiver-type items PyYAML cannot build or builds as no string (F1),
receiver-type items it builds as a collection — a mapping, a list, a
`!!set` / `!!omap` / `!!pairs`, an aliased list (#2758),
require_critical_escalation in place (B1), values with no constructor
wherever built, and shapes da-guard used to refuse that the generator reads
(`!!null x`, a null domain key; not the directives it still refuses, #2759),
and round 3's directive then `---` and a tab, which the generator drops;
then the final blind review's (N1) directive lines with a tab in them and
`---` at column 0 then a tab, with controls that write `%` and `---` and a
tab inside the document, which the generator reads; then the TAB family
(round 6): policies the generator reads with one space written as a TAB, a
TAB at a line's end or right after an indicator (PyYAML takes a TAB as a
separator only inside quotes, comments and block scalars), NEL / LS / PS
as the line break, and `encoding` rows — a policy written as UTF-16 with its
byte order mark, or as Latin-1, which the generator (UTF-8 only) cannot
read and yaml.v3 can. Their verdicts are `_verdict()`'s
like any other row's, and Go skips none of them (`nonspecific_tag` marks
the rows that carry the non-specific tag `!` on a quoted scalar, which Go
reads through the vendored yaml.v3's patch — #2730 §6). The shapes also
hold the divergences #2759 lists as Go-looser that no row measured before (ADR-036
step 2): a nesting depth of 600 (A), `!!set` at domain_policies / a domain
/ constraints (F), a merge value holding a mapping with a collection key, in
each spelling (K1), and a UTF-8 BOM in a comment straddling byte 512 (N),
each with a control every reader reads alike.

Every row has an `id`: the first 16 hex digits of the sha256 of the file it
stands for (its `doc` as UTF-8, or written in its `encoding`). A row whose
file the generator reads but blocks on as production runs it (`--validate
--strict`) carries `pyyaml_strict_blocks`: the generator's own blocking
findings for the file (kind, policy, field), taken from the structured
findings load_tenant_tree returns — the verdict alone (what it enforces)
cannot tell a blocked file from one that enforces nothing. The Go halves
hold such a row to "unusable": a Go reader that neither refuses it nor
reports a blocking problem is looser.

The Go halves read the same rows:
- components/threshold-exporter/app/pkg/routingpolicy/merge_key_corpus_test.go:
  da-guard's ParseDomainPolicies says what PyYAML says, or what
  tests/shared/merge_key_go_verdicts.json records for that row.
- components/tenant-api/internal/policy/merge_key_corpus_test.go: tenant-api's
  parseConfig says what PyYAML says or what that snapshot records, or
  refuses the file (refusals are not recorded yet: ADR-036 step 2, PR-C).
The snapshot holds only the rows where a Go reader differs; each is matched
to an entry of tests/shared/reader_divergence_catalog.yaml, and its direction
computed, by tests/shared/test_reader_divergence_catalog.py.

The rows are generated from a fixed seed, never hand-edited:

    REGEN_MERGE_KEY_CORPUS=1 python3 -m pytest \
        tests/shared/test_merge_key_policy_corpus.py -q -p no:cacheprovider
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import random
import re
import sys
import tempfile
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "tools"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "tools" / "ops"))
from _grar_parse import _parse_config_files, load_tenant_tree  # noqa: E402

CORPUS_PATH = Path(__file__).parent / "merge_key_policy_corpus.json"
SEED = 2677
DOCS = 500

# The keys each level of a policy reads, by depth: the top, domain_policies,
# a domain, its constraints.
LEVEL_KEYS = (("domain_policies",), ("fin", "ops"), ("tenants", "constraints"),
              ("forbidden_receiver_types",))
TENANT_LISTS = ("[t1]", "[t2]", "[t1, t2]")
RECEIVER_LISTS = ("[slack]", "[email]", "[slack, email]")
# Tenant ids a `tenants:` list may hold besides t1 / t2 ("full" documents):
# the generator reads a `tenants` list's scalar items as their source text
# (_lib_yaml_keys.ExporterKeyLoader, raw_text_sequences), so a merge-tagged
# item is a tenant id there — unless the list is also built as a value
# elsewhere (written in x-lists, aliased from x-use), where PyYAML has no
# constructor for it and drops the file.
ODD_TENANTS = ("<<", "!!merge x")
# require_critical_escalation values the generator refuses (it drops the
# file): written at the top, in a domain, under an unread key, anchored in
# constraints and aliased elsewhere, or in place under
# domain_policies.<d>.constraints — every reader must refuse each (hub #2486
# PR-7c round 2, B1: tenant-api no longer reads the in-place one leniently).
REFUSED_ESCALATIONS = ("<<", "!!merge x", "{<<: 5}", "{a: 1, a: 2}", "{x: [<<]}")
# Repeated keys the generator refuses, under keys no policy reader reads
# (yaml.v3's struct decode never looks there): one spelling twice, or an
# alias key beside its anchor.
DUPLICATES = ("{a: 1, a: 2}", "{&z0 a: 1, *z0 : 2}", "{tenants: [t1], tenants: [t2]}")

# Merge-key spellings written as a key. "<<"-text ones share yaml.v3's key
# text, so a mapping gets at most one of them (yaml.v3 refuses a repeat).
# These the generator and yaml.v3 read alike (a merge, or a plain "<<" key):
LT_SPELLINGS = ("<<", "<<", "!!merge <<", "!<tag:yaml.org,2002:merge> <<", "! <<",
                "!y!merge <<", '"<<"', "'<<'", "!!str <<")
# These only the generator reads as a merge — half the documents ("full")
# use them: a tagged `!!merge q`, an alias key, an anchored `<<` as a value,
# and the non-specific `! "<<"` (NONSPECIFIC: a yaml.Node tells it from a
# quoted "<<" only through the vendored yaml.v3's patch).
Q_SPELLINGS = ("!!merge {q}", "!<tag:yaml.org,2002:merge> {q}", "!y!merge {q}")
NONSPECIFIC_SPELLING = '! "<<"'
# Anchored key spellings an alias key may name (x-keys).
KEY_ANCHOR_SPELLINGS = ("<<", "<<", "!!merge <<", "!!merge qk", '"<<"', "! <<", "!!str <<")

NONSPECIFIC = ("the non-specific tag `!` on a quoted scalar: PyYAML resolves it as if plain; "
               "upstream yaml.v3 drops the tag, the vendored copy keeps it (#2730 §6)")
# A non-specific tag `!` on a quoted scalar, key or value, an anchor on
# either side of it (`! "<<"`, `&a ! '<<'`, `! &a "true"`): PyYAML resolves
# the scalar as if it were plain (`! "true"` is True, `! "<<"` a merge);
# the vendored yaml.v3 keeps Tag "!" on the node so Go can too.
_NONSPECIFIC_RE = re.compile(r"""(?:^|[\s\[{,?\ufeff\u2028\x85])(?:&\w+ )?! (?:&\w+ )?["']""")


class _Gen:
    def __init__(self, r: random.Random, full: bool) -> None:
        self.r = r
        self.full = full
        self.defs: list[tuple[int, str]] = []  # (level, rendered body), anchored &d<i>
        self.lists: list[tuple[str, str]] = []  # (kind, rendered), anchored &l<i>
        self.keys: list[str] = []  # anchored key spellings, &k<i>
        self.value_lt = False  # an anchored `<<` written as a value (&v0)
        self.tag_handle = False
        self.q = 0
        self.tenant_anchors = 0  # &a<i>: tenants lists anchored where written
        self.extra: list[str] = []  # top-level lines (x-use, x-junk, escalation)
        self.domain_escalation = ""  # a refused escalation written in a domain
        self.anchored_escalation = ""  # a refused escalation written in constraints

    def tenant_item(self) -> str:
        r = self.r
        if self.full and r.random() < 0.3:
            lt = [i for i, sp in enumerate(self.keys) if sp.split()[-1] == "<<" and "!!str" not in sp
                  and '"' not in sp]
            if lt and r.random() < 0.4:
                return f"*k{r.choice(lt)}"
            return r.choice(ODD_TENANTS)
        return r.choice(("t1", "t2"))

    def tenant_list(self) -> str:
        r = self.r
        body = "[" + ", ".join(self.tenant_item() for _ in range(r.randint(1, 2))) + "]"
        if self.full and r.random() < 0.2:
            i = self.tenant_anchors
            self.tenant_anchors += 1
            if r.random() < 0.5:
                self.extra.append(f"x-use{i}: [*a{i}]")
            return f"&a{i} {body}"
        if self.tenant_anchors and r.random() < 0.2:
            return f"*a{r.randrange(self.tenant_anchors)}"
        return body

    # A value of an own key of level `level`.
    def own_value(self, level: int, key: str, depth: int) -> str:
        r = self.r
        if level == 3 and key == "require_critical_escalation":
            if self.anchored_escalation:
                v, self.anchored_escalation = self.anchored_escalation, ""
                return v
            return r.choice(("true", "false"))
        if level == 2 and key == "require_critical_escalation":
            v, self.domain_escalation = self.domain_escalation, ""
            return v
        if level == 2 and key == "note":
            return r.choice(DUPLICATES)
        if level == 2 and key == "tenants" or level == 3:
            kind = "t" if level == 2 else "f"
            mine = [i for i, (k, _) in enumerate(self.lists) if k == kind]
            if mine and r.random() < 0.3:
                return f"*l{r.choice(mine)}"
            return self.tenant_list() if kind == "t" else r.choice(RECEIVER_LISTS)
        if level == 0 and r.random() < 0.05:
            return "null"
        return self.mapping_value(level + 1, depth)

    # A mapping of level `level`: inline, or an alias to one defined earlier.
    def mapping_value(self, level: int, depth: int) -> str:
        mine = [i for i, (lv, _) in enumerate(self.defs) if lv == level]
        if mine and self.r.random() < 0.3:
            return f"*d{self.r.choice(mine)}"
        return self.mapping(level, depth)

    def merge_value(self, level: int, depth: int) -> str:
        r = self.r
        if r.random() < 0.25:
            items = [self.mapping_value(level, depth + 1) for _ in range(r.randint(1, 2))]
            return "[" + ", ".join(items) + "]"
        return self.mapping_value(level, depth + 1)

    # A merge-ish key for one mapping, or None. `texts` holds the key texts
    # the mapping already has: PyYAML's strict loader and yaml.v3 both refuse
    # a mapping with two keys of one text (two `<<`, or `<<` beside an alias
    # naming an anchored `<<`), so a repeat is written only now and then.
    def merge_key(self, texts: set) -> str | None:
        r = self.r
        roll = r.random() if self.full else 0.3
        if roll < 0.02:
            s, text = NONSPECIFIC_SPELLING, "<<"
        elif roll < 0.5:
            s, text = r.choice(LT_SPELLINGS), "<<"
        elif roll < 0.75:
            text = f"q{self.q}"
            s = r.choice(Q_SPELLINGS).format(q=text)
            self.q += 1
        elif self.value_lt and roll < 0.8:
            s, text = "*v0 ", "<<"
        elif self.keys:
            i = r.randrange(len(self.keys))
            s, text = f"*k{i} ", self.keys[i].split()[-1].strip("'\"")
        else:
            return None
        if text in texts and r.random() > 0.005:
            return None
        texts.add(text)
        self.tag_handle |= s.startswith("!y!")
        return s

    def mapping(self, level: int, depth: int) -> str:
        return "{" + ", ".join(self.entries(level, depth)) + "}"

    def entries(self, level: int, depth: int) -> list[str]:
        r = self.r
        keys = list(LEVEL_KEYS[level])
        if level == 3 and (self.anchored_escalation or r.random() < 0.1):
            keys.append("require_critical_escalation")
        if level == 2 and self.domain_escalation:
            keys.append("require_critical_escalation")
        if level == 2 and r.random() < 0.03:
            keys.append("note")
        r.shuffle(keys)
        own = [k for k in keys if r.random() < 0.6 or k in ("require_critical_escalation", "note")]
        entries = [f"{k}: {self.own_value(level, k, depth)}" for k in own]
        texts: set = set(own)
        if depth < 2:
            for _ in range(r.choice((0, 0, 1, 1, 2))):
                k = self.merge_key(texts)
                if k is None:
                    continue
                entries.insert(r.randint(0, len(entries)), f"{k}: {self.merge_value(level, depth)}")
        return entries

    def document(self) -> str:
        r = self.r
        lines = []
        for _ in range(r.randint(0, 3) if self.full else 0):
            self.keys.append(r.choice(KEY_ANCHOR_SPELLINGS))
        for _ in range(r.randint(0, 3)):
            kind = r.choice("tf")
            body = ("[" + ", ".join(self.tenant_item() for _ in range(r.randint(1, 2))) + "]"
                    if kind == "t" else r.choice(RECEIVER_LISTS))
            self.lists.append((kind, body))
        if self.full and r.random() < 0.2:
            v = r.choice(REFUSED_ESCALATIONS)
            where = r.randrange(5)
            if where == 0:
                self.extra.append(f"require_critical_escalation: {v}")
            elif where == 1:
                self.extra.append(f"x-junk: {{require_critical_escalation: {v}}}")
            elif where == 2:
                self.domain_escalation = v
            elif where == 3:
                self.anchored_escalation = f"&e0 {v}"
                self.extra.append("x-use-e: *e0")
            else:
                self.anchored_escalation = v  # in place, no anchor
        if r.random() < 0.08:
            self.extra.append(f"x-dup: {r.choice(DUPLICATES)}")
        self.value_lt = self.full and r.random() < 0.08
        for _ in range(r.randint(0, 2)):
            level = r.randint(0, 3)
            body = self.mapping(level, 1)
            self.defs.append((level, body))
        if self.keys or self.value_lt:
            items = [f"{{&k{i} {s}: {{}}}}" for i, s in enumerate(self.keys)]
            if self.value_lt:
                items.append("&v0 <<")
            lines.append("x-keys: [" + ", ".join(items) + "]")
        if self.lists:
            lines.append("x-lists: [" + ", ".join(f"&l{i} {v}" for i, (_, v) in enumerate(self.lists)) + "]")
        if self.defs:
            lines.append("x-defs: [" + ", ".join(f"&d{i} {b}" for i, (_, b) in enumerate(self.defs)) + "]")
        lines += self.entries(0, 0) or ["domain_policies: {}"]
        if "*e0" in "".join(self.extra) and "&e0" not in "\n".join(lines):
            self.extra = [x for x in self.extra if "*e0" not in x]  # the anchor was never written
        lines += self.extra
        doc = "\n".join(lines) + "\n"
        if self.tag_handle:
            doc = "%TAG !y! tag:yaml.org,2002:\n---\n" + doc
        return doc


def _block_document(r: random.Random) -> str:
    """A policy written in block style, its tenants a block sequence."""
    items = ["t1"] + [r.choice(("t2",) + ODD_TENANTS) for _ in range(r.randint(1, 2))]
    anchor = r.random() < 0.3
    doc = ("x: {&m <<: {}}\n" if r.random() < 0.3 else "") + "domain_policies:\n  fin:\n    tenants:"
    doc += " &bt\n" if anchor else "\n"
    doc += "".join(f"    - {i}\n" for i in items)
    if "&m" in doc and r.random() < 0.5:
        doc += "    - *m\n"
    doc += "    constraints: {forbidden_receiver_types: [slack]}\n"
    if anchor:
        doc += r.choice(("  ops: {tenants: *bt, constraints: {forbidden_receiver_types: [email]}}\n",
                         "x-use: *bt\n"))
    return doc


def _documents():
    """Documents in a fixed order, endless; _rows keeps what it needs."""
    r = random.Random(SEED)
    seen: set = set()
    while True:
        doc = _block_document(r) if r.random() < 0.08 else _Gen(r, full=r.random() < 0.5).document()
        if doc not in seen:
            seen.add(doc)
            yield doc


# --- Shapes: deterministic rows after the seeded ones (hub #2486, PR-7c/7d) --
#
# One `_domain_policy.yaml` per shape, each tagged with the section of the
# ticket it pins (`shape`). The verdict is the same `_verdict()`: these rows
# are not hand-judged. Every shape is a single-file divergence measured on
# main (#2730, #2715); the Go halves hold them to the generator like any
# other row.

_POLICY = ("domain_policies:\n  fin:\n    tenants: [t1]\n"
           "    constraints:\n      forbidden_receiver_types: [slack]\n")
# What a merge supplies a domain, and a domain's own body, flow style.
_FIN_BODY = "{tenants: [t1], constraints: {forbidden_receiver_types: [slack]}}"


def _policy(tenants: str = "[t1]", domain_extra: str = "", constraints_extra: str = "") -> str:
    return ("domain_policies:\n  fin:\n    tenants: " + tenants + "\n" + domain_extra
            + "    constraints:\n      forbidden_receiver_types: [slack]\n" + constraints_extra)


def _s4_merge_tagged_collections() -> list[str]:
    """§4: a `!!merge`-tagged collection where a value goes."""
    values = ("!!merge [a]", "!!merge {a: 1}", "[!!merge {a: 1}]", "!!merge []", "!!merge {}",
              "{k: !!merge [a]}")
    docs = []
    for v in values:
        docs.append(_POLICY + f"x: {v}\n")
        docs.append(f"x: {v}\n" + _POLICY)
        docs.append(_policy(domain_extra=f"    description: {v}\n"))
        docs.append(_policy(constraints_extra=f"      x: {v}\n"))
        docs.append(f"domain_policies:\n  x: {v}\n  fin: {_FIN_BODY}\n")
    for item in ("!!merge [a]", "!!merge {a: 1}"):
        docs.append(_policy(tenants=f"[t1, {item}]"))
        docs.append(_policy(tenants=f"[{item}]"))
        docs.append(_policy(tenants=f"\n    - t1\n    - {item}"))
    return docs


def _s2_tenant_items() -> list[str]:
    """§2: a `tenants` item that is not a string."""
    docs = []
    for item in ("!!binary aGk=", "~", "!!null x", "!!int 7", "010", "''"):
        docs.append(_policy(tenants=f"[t1, {item}]"))
        docs.append(_policy(tenants=f"[{item}]"))
        docs.append(_policy(tenants=f"\n    - t1\n    - {item}"))
    return docs


def _s6_nonspecific_keys() -> list[str]:
    """§6 / N4: the key `! "<<"` (non-specific tag on a quoted `<<`)."""
    docs = []
    for key in ('! "<<"', "! '<<'"):
        # The top level: a merge supplying domain_policies.
        docs.append(f"{key}:\n  domain_policies:\n    fin: {_FIN_BODY}\n")
        docs.append(f"x-b: &b {{domain_policies: {{fin: {_FIN_BODY}}}}}\n{key}: *b\n")
        # A domain: supplied whole, and beside an own key.
        dom = f"x-b: &b {_FIN_BODY}\ndomain_policies:\n  fin:\n    {key}: *b\n"
        docs.append(dom)
        docs.append(f"x-b: &b {_FIN_BODY}\ndomain_policies:\n  fin:\n    tenants: [t2]\n    {key}: *b\n")
        # Constraints: supplied whole, and beside an own allowlist.
        docs.append("domain_policies:\n  fin:\n    tenants: [t1]\n    constraints:\n"
                    f"      {key}: {{forbidden_receiver_types: [slack]}}\n")
        docs.append("domain_policies:\n  fin:\n    tenants: [t1]\n    constraints:\n"
                    "      allowed_receiver_types: [email]\n"
                    f"      {key}: {{forbidden_receiver_types: [slack]}}\n")
        # domain_policies itself: a merge supplying a domain.
        docs.append(f"x-b: &b {{fin: {_FIN_BODY}}}\ndomain_policies:\n  {key}: *b\n")
        # Spellings of the same key on the domain shape.
        docs.append("﻿" + dom)
        docs.append(dom.replace("\n", "\r"))
        docs.append(dom.replace("\n    " + key, "     " + key))
        docs.append(dom.replace("\n    " + key, "\x85    " + key))
        docs.append(" " + dom)
        docs.append("\x85" + dom)
        docs.append(f"x-b: &b {_FIN_BODY}\ndomain_policies:\n  fin: {{{key}: *b}}\n")
        docs.append(f"x-b: &b {_FIN_BODY}\ndomain_policies: {{fin: {{{key}: *b}}}}\n")
        docs.append(f"x-b: &b {_FIN_BODY}\ndomain_policies:\n  fin:\n    ? {key}\n    : *b\n")
        docs.append(f"x-b: &b {_FIN_BODY}\ndomain_policies:\n  fin:\n    &a {key}: *b\n")
        docs.append(f"x-b: &b {_FIN_BODY}\ndomain_policies:\n  fin:\n    {key[:2]}&a {key[2:]}: *b\n")
        # Constraints spellings.
        con = ("domain_policies:\n  fin:\n    tenants: [t1]\n    constraints:\n"
               f"      {key}: {{forbidden_receiver_types: [slack]}}\n")
        docs.append("﻿" + con)
        docs.append(con.replace("\n", "\r"))
        docs.append("domain_policies:\n  fin:\n    tenants: [t1]\n"
                    f"    constraints: {{{key}: {{forbidden_receiver_types: [slack]}}}}\n")
    return docs


def _s6_nonspecific_values() -> list[str]:
    """§6: a non-specific tag on a quoted value (PyYAML: `! "true"` is True)."""
    docs = []
    for q in ('"', "'"):
        for v in ("true", "false", "yes", "null", "~", "7"):
            docs.append(_policy(constraints_extra=f"      require_critical_escalation: ! {q}{v}{q}\n"))
        for v in ("slack", "null", "~", "7", "true"):
            docs.append("domain_policies:\n  fin:\n    tenants: [t1]\n"
                        f"    constraints:\n      forbidden_receiver_types: [! {q}{v}{q}]\n")
            docs.append(_policy(tenants=f"[t1, ! {q}{v}{q}]"))
        docs.append("domain_policies:\n  fin:\n    tenants: [t1]\n"
                    f"    constraints:\n      forbidden_receiver_types: ! {q}[slack]{q}\n")
        docs.append(f"domain_policies: ! {q}null{q}\n")
        docs.append(f"domain_policies:\n  fin:\n    tenants: [t1]\n    constraints: ! {q}null{q}\n")
        docs.append(f"domain_policies:\n  fin: ! {q}~{q}\n  ops: {_FIN_BODY}\n")
        docs.append(_policy(tenants=f"! {q}null{q}"))
        docs.append(_policy(constraints_extra=f"      require_critical_escalation: &e ! {q}true{q}\n")
                    + "x-use: *e\n")
    return docs


def _s1_multi_document() -> list[str]:
    """§1: a stream of more than one document."""
    return [
        _POLICY + "---\nfoo: 1\n",
        "{}\n---\n" + _POLICY,
        "domain_policies: {}\n---\n" + _POLICY,
        _POLICY + "---\n" + _POLICY.replace("[slack]", "[email]"),
        _POLICY + "...\n---\nfoo: 1\n",
        _POLICY + "---\n",
        "---\n" + _POLICY + "...\n",  # one document, explicit markers
    ]


def _s5_top_level_tenants() -> list[str]:
    """§5: a top-level `tenants:` sequence."""
    return [
        "tenants: [t1]\n" + _POLICY,
        _POLICY + "tenants: [t1]\n",
        "tenants: [t1]\n",
        "tenants: [t1]\ndomain_policies: {}\n",
        "tenants:\n  - t1\n" + _POLICY,
    ]


def _constraints(*lines: str) -> str:
    """Domain `fin` (tenants [t1]) with these constraint lines, block style."""
    return ("domain_policies:\n  fin:\n    tenants: [t1]\n    constraints:\n"
            + "".join(f"      {line}\n" for line in lines))


# Scalars PyYAML's SafeConstructor refuses to build (the generator drops the
# file): a tag whose constructor rejects the text, a tag it has no
# constructor for (`!!value`, `!!yaml`, a local `!custom`).
_UNBUILT_SCALARS = ("!!int x", "!!bool x", "!!float x", "!!timestamp x", "!!timestamp 2001-13-01",
                    "!!int ''", "!!value x", "!!yaml x", "!custom x")


def _f1_receiver_type_items() -> list[str]:
    """F1 (hub #2486 PR-7c round 2): receiver-type items PyYAML cannot build."""
    docs = []
    for item in _UNBUILT_SCALARS:
        docs.append(_constraints(f"forbidden_receiver_types: [slack, {item}]"))
        docs.append(_constraints(f"forbidden_receiver_types: [{item}]"))
        docs.append(_constraints("forbidden_receiver_types:", "- slack", f"- {item}"))
        docs.append(_constraints("forbidden_receiver_types: [slack]", f"allowed_receiver_types: [email, {item}]"))
        docs.append(_constraints(f"allowed_receiver_types: [{item}]"))
    return docs


def _f1_allowed_not_a_string() -> list[str]:
    """F1: allowed items PyYAML builds as no string, their text a type name."""
    docs = []
    for allowed in ("[!!null webhook]", "[!!null slack, email]", "[slack, !!null webhook]", "[!!null x]",
                    '[! "null"]', "[~]", "[yes]", "[7]", "[!!bool true]", "[!!int 7]"):
        docs.append(_constraints(f"allowed_receiver_types: {allowed}"))
        docs.append(_constraints("forbidden_receiver_types: [pagerduty]", f"allowed_receiver_types: {allowed}"))
    for forbidden in ("[slack, !!null webhook]", "[!!null slack]"):
        docs.append(_constraints(f"forbidden_receiver_types: {forbidden}"))
    return docs


def _collection_receiver_types() -> list[str]:
    """#2758: a receiver-type entry PyYAML builds as a collection (unhashable).

    It names no receiver type: the generator skips it and still enforces the
    string entries; a non-empty allowed list of none still restricts.
    """
    return [
        _constraints("forbidden_receiver_types: [slack, {a: 1}]"),
        _constraints("forbidden_receiver_types: [slack, [a]]"),
        _constraints("forbidden_receiver_types: [{a: 1}]"),
        _constraints("forbidden_receiver_types: [slack, !!set {a}]"),
        _constraints("forbidden_receiver_types: [slack, !!omap [{a: 1}]]"),
        _constraints("forbidden_receiver_types: [slack, !!pairs [{a: 1}]]"),
        _constraints("forbidden_receiver_types:", "- slack", "- {a: 1}"),
        _constraints("forbidden_receiver_types: [slack]", "allowed_receiver_types: [email, {a: 1}]"),
        _constraints("allowed_receiver_types: [{a: 1}]"),
        _constraints("allowed_receiver_types: [email, [a]]"),
        _constraints("x: &l [a]", "forbidden_receiver_types: [slack, *l]"),
    ]


def _b1_escalation_in_place() -> list[str]:
    """B1 (owner P3): require_critical_escalation written in place in constraints."""
    docs = []
    # PyYAML refuses these (the generator drops the file), then values it
    # reads that are no boolean (only --strict reports those: not enforced).
    for v in ("!!null {}", "!!bool maybe", "!!int x", "{<<: 1}", "!!bool [true]", "<<", "!!merge x",
              '"true"', "1", "[true]", "!!null x", "yes"):
        docs.append(_policy(constraints_extra=f"      require_critical_escalation: {v}\n"))
        docs.append("domain_policies:\n  fin:\n    tenants: [t1]\n    constraints: "
                    f"{{forbidden_receiver_types: [slack], require_critical_escalation: {v}}}\n")
    return docs


# Values PyYAML has no constructor for: `=` (!!value), `! "!"` / `! "*"` /
# `! "&"` (!!yaml), a local tag.
_NO_CONSTRUCTOR = ('! "="', '! "!"', '! "*"', '! "&"', "=", "!custom x", "!!value x", "!!yaml x")


def _e_no_constructor() -> list[str]:
    """F2 (pre-existing): a value or item with no constructor, wherever built."""
    docs = []
    for v in _NO_CONSTRUCTOR:
        docs.append(_POLICY + f"x: {v}\n")
        docs.append(_POLICY + f"x: [{v}]\n")
        docs.append(_policy(domain_extra=f"    description: {v}\n"))
        docs.append(_policy(constraints_extra=f"      x: {v}\n"))
        docs.append(_constraints(f"forbidden_receiver_types: [slack, {v}]"))
        docs.append(_policy(tenants=f"[t1, {v}]"))  # tenants items: source text, never built
        docs.append(_policy(tenants=f"&a [t1, {v}]") + "x: *a\n")  # ... unless aliased where built
        docs.append(_POLICY + f"~: {v}\n")  # a null key's value: never built
    docs.append(f"!!merge {{domain_policies: {{fin: {_FIN_BODY}}}}}\n")
    docs.append(f"--- !!merge\ndomain_policies: {{fin: {_FIN_BODY}}}\n")
    docs.append(f"--- !custom\ndomain_policies: {{fin: {_FIN_BODY}}}\n")
    for item in ("! |\n        true\n", "! |-\n        true\n", "! >\n        true\n"):
        docs.append(_constraints("forbidden_receiver_types:", "- slack", f"- {item}"))
    return docs


def _e_reader_stricter() -> list[str]:
    """F2 (pre-existing): shapes the generator reads that da-guard refused."""
    return [
        _policy(tenants="&a [!!null x]"),
        _policy(tenants="&a [t1, !!null x]"),
        _policy(tenants="&a [t1, !!null x]") + "x: *a\n",
        "tenants: !!null x\n" + _POLICY,
        _POLICY + "tenants: !!null x\n",
        _constraints("forbidden_receiver_types: [slack]", "x: !!null x"),
        # Not generated: a `%YAML 1.x` other than 1.1, or a directive named
        # other than YAML / TAG — the generator reads it, da-guard refuses the
        # file (a known gap, #2759). The two below every reader reads alike.
        "%YAML 1.1\n---\n" + _POLICY,
        "%YAML 2.0\n---\n" + _POLICY,  # refused by both
        f"domain_policies:\n  ~: {_FIN_BODY}\n",
        f"domain_policies:\n  null: {_FIN_BODY}\n",
        f'domain_policies:\n  ! "null": {_FIN_BODY}\n',
        f"domain_policies:\n  ~: {_FIN_BODY}\n  ops: {_FIN_BODY}\n",
        f"domain_policies: {{~: {_FIN_BODY}}}\n",
        # A null key's value is never built, an unconstructible one included
        # (#2763 review): `! "null"` is as null as `~` here.
        f'domain_policies:\n  ! "null": =\n  fin: {_FIN_BODY}\n',
        f'domain_policies:\n  ! "null": {{x: =}}\n  fin: {_FIN_BODY}\n',
        f"domain_policies:\n  null: =\n  fin: {_FIN_BODY}\n",
        f"domain_policies:\n  ~: {{x: =}}\n  fin: {_FIN_BODY}\n",
        # ...unless an alias names a node in it: then it is built there.
        f"domain_policies:\n  ~: &m {{a: =}}\n  fin: {_FIN_BODY}\nx: *m\n",
        f"domain_policies:\n  ~: {{y: &m {{a: =}}}}\n  fin: {_FIN_BODY}\nx: {{<<: *m}}\n",
        f'domain_policies:\n  ! "null": {{y: &m [=]}}\n  fin: {_FIN_BODY}\nx: *m\n',
        f"domain_policies:\n  ~: &m {{~: =}}\n  fin: {_FIN_BODY}\nx: *m\n",
        # An `!!omap` / `!!pairs` item is not built by construct_mapping: its
        # pair's value is built whatever the key, a null one included.
        f"domain_policies:\n  fin: {_FIN_BODY}\nx: !!omap [{{~: {{a: =}}}}]\n",
        f'domain_policies:\n  fin: {_FIN_BODY}\nx: !!omap [{{! "null": =}}]\n',
        f'domain_policies:\n  fin: {_FIN_BODY}\nx: !!omap [{{! "null": {{a: =}}}}]\n',
        f"domain_policies:\n  fin: {_FIN_BODY}\nx: !!pairs [{{~: {{a: =}}}}]\n",
        f'domain_policies:\n  fin: {_FIN_BODY}\nx: !!pairs [{{! "null": =}}]\n',
        f"domain_policies:\n  fin: {_FIN_BODY}\nx: !!omap [{{null: {{a: !!int x}}}}]\n",
        f"domain_policies:\n  ~: &o {{~: {{a: =}}}}\n  fin: {_FIN_BODY}\nx: !!omap [*o]\n",
        f"domain_policies:\n  fin: {_FIN_BODY}\nx: !!omap [{{~: =}}]\n",
        f"domain_policies:\n  fin: {_FIN_BODY}\nx: !!pairs [{{~: [=]}}]\n",
        f"domain_policies:\n  fin: {_FIN_BODY}\nx: !!omap [{{~: 1}}]\n",
        # ...only where it is built as one: a `tenants` value is built item by
        # item through construct_mapping, a merge value is flattened, its tag
        # and its items' tags unread (#2763 round 3).
        f"domain_policies:\n  fin: {_FIN_BODY}\nx: {{tenants: !!omap [{{~: =}}]}}\n",
        f"domain_policies:\n  fin: {_FIN_BODY}\nx: {{tenants: !!pairs [{{~: =}}]}}\n",
        "domain_policies:\n  fin: {tenants: !!omap [{~: =}], constraints: {forbidden_receiver_types: [slack]}}\n",
        "domain_policies:\n  fin: {tenants: !!pairs [t1, {~: =}], constraints: {forbidden_receiver_types: [slack]}}\n",
        f"domain_policies:\n  fin: {_FIN_BODY}\nx: {{tenants: !!omap [&m {{~: =}}]}}\ny: *m\n",
        f"domain_policies:\n  fin: {_FIN_BODY}\nx: {{<<: !!omap [{{~: =}}]}}\n",
        f"domain_policies:\n  fin: {_FIN_BODY}\nx: {{<<: !!pairs [{{~: {{a: !!int z}}}}]}}\n",
        "domain_policies:\n  fin: {<<: !!omap [{~: =}], tenants: [t1], constraints: {forbidden_receiver_types: [slack]}}\n",
        f"domain_policies:\n  <<: !!pairs [{{~: =}}]\n  fin: {_FIN_BODY}\n",
        f"domain_policies:\n  fin: {_FIN_BODY}\nx: {{<<: [!custom {{a: 1}}]}}\n",
        f"domain_policies:\n  fin: {_FIN_BODY}\nx: {{tenants: &s !!omap [{{~: =}}]}}\ny: *s\n",
        f"domain_policies:\n  fin: {_FIN_BODY}\nx: {{tenants: !!omap [&m {{~: =}}]}}\ny: !!omap [*m]\n",
        f"domain_policies:\n  fin: {_FIN_BODY}\nx: {{<<: !!omap [{{a: !!int z}}]}}\n",
        f"domain_policies:\n  fin: {_FIN_BODY}\nx: {{tenants: !!omap [!custom {{a: 1}}]}}\n",
        # ...but the same mapping reached as a built mapping too is built by
        # construct_mapping there: its `tenants` is raw.
        f"domain_policies:\n  fin: {_FIN_BODY}\nx: !!omap [&m {{tenants: [!custom {{a: 1}}]}}]\ny: *m\n",
        f"domain_policies:\n  fin: {_FIN_BODY}\ny: &m {{tenants: [!custom {{a: 1}}]}}\nx: !!omap [*m]\n",
        f"domain_policies:\n  fin: {_FIN_BODY}\nx: !!omap [&m {{tenants: !!omap [!custom {{a: 1}}]}}]\ny: {{<<: *m}}\n",
        # A `tenants` with an unconstructible item under a null key of an
        # `!!omap` / `!!pairs` item: the generator builds the pair's value,
        # so it drops the file, and so must da-guard.
        f"domain_policies:\n  fin: {_FIN_BODY}\nx: !!omap [{{~: {{tenants: [!custom {{a: 1}}]}}}}]\n",
        f"domain_policies:\n  fin: {_FIN_BODY}\nx: !!pairs [{{~: {{tenants: [!custom {{a: 1}}]}}}}]\n",
        f"domain_policies:\n  fin: {_FIN_BODY}\nx: !!omap [{{~: {{tenants: !!omap [!custom {{a: 1}}]}}}}]\n",
        f"domain_policies:\n  fin: {_FIN_BODY}\nn: {{~: &v {{tenants: [!custom {{a: 1}}]}}}}\nx: !!omap [{{~: *v}}]\n",
        f"domain_policies:\n  fin: {_FIN_BODY}\nx: {{tenants: [!!omap [{{~: {{tenants: [!custom {{a: 1}}]}}}}]]}}\n",
        f"domain_policies:\n  fin: {_FIN_BODY}\na: &k ~\nx: !!omap [{{*k : {{tenants: [!custom {{a: 1}}]}}}}]\n",
        f"domain_policies:\n  fin: {_FIN_BODY}\nx: !!omap [{{~: {{tenants: [!!merge {{a: 1}}]}}}}]\n",
        f"domain_policies:\n  fin: {_FIN_BODY}\nx: !!pairs [{{~: !!pairs [{{~: {{tenants: [!custom {{a: 1}}]}}}}]}}]\n",
        # A built omap/pairs item's KEY is built too (construct_object): `<<`
        # there is no merge, `=` has no constructor (looser-only fuzz, #2763).
        f"domain_policies:\n  fin: {_FIN_BODY}\nx: !!omap [{{<<: {{a: 1}}}}]\n",
        f"domain_policies:\n  fin: {_FIN_BODY}\nx: !!pairs [{{<<: {{a: 1}}}}]\n",
        f"domain_policies:\n  fin: {_FIN_BODY}\nx: !!omap [{{<<: [{{a: 1}}]}}]\n",
        f"domain_policies:\n  fin: {_FIN_BODY}\nx: [!!pairs [{{<<: {{a: 1}}}}]]\n",
        f"domain_policies:\n  fin: {_FIN_BODY}\n  ops: !!omap [{{<<: {{a: 1}}}}]\n",
        f"domain_policies:\n  fin: {_FIN_BODY}\nx: !!omap [{{=: 1}}]\n",
        f"domain_policies:\n  fin: {_FIN_BODY}\nx: {{<<: !!omap [{{a: 1}}]}}\n",
        f"domain_policies:\n  fin: {_FIN_BODY}\nx: {{tenants: !!omap [{{<<: {{a: 1}}}}]}}\n",
        # ...a sequence key's items included (#2763 final review).
        f"domain_policies:\n  fin: {_FIN_BODY}\nx: !!omap [{{? [=] : 1}}]\n",
        f"domain_policies:\n  fin: {_FIN_BODY}\nx: !!omap [{{? [!custom x] : 1}}]\n",
        f"domain_policies:\n  fin: {_FIN_BODY}\nx: !!pairs [{{? !!omap [{{<<: {{a: 1}}}}] : 1}}]\n",
        "domain_policies:\n  fin: {tenants: [t1], constraints: {forbidden_receiver_types: [slack]}, extra: !!omap [{? [=] : 1}]}\n",
    ]


def _known_stricter_2759() -> list[str]:
    """Known stricter (#2759): the generator reads these, da-guard refuses.

    da-guard's load.go is the 2eb4181b1 version: hand-modelling how PyYAML
    builds `!!omap` / `!!pairs` items kept opening new divergences, so
    da-guard's refusals of these rows stay recorded in
    merge_key_go_verdicts.json and catalogued in
    reader_divergence_catalog.yaml (#2759) until the root fix (#2766) lands.
    """
    return [
        # An `!!omap` / `!!pairs` item is taken apart, not built by
        # construct_mapping: its `tenants` is no raw-text sequence and its own
        # tag is never read (#2763 round 4).
        f"domain_policies:\n  fin: {_FIN_BODY}\nx: !!omap [{{tenants: !!omap [!custom {{a: 1}}]}}]\n",
        f"domain_policies:\n  fin: {_FIN_BODY}\nx: !!pairs [{{tenants: !!pairs [!custom {{a: 1}}]}}]\n",
        f"domain_policies:\n  fin: {_FIN_BODY}\n  ops: !!omap [{{tenants: !!omap [!custom {{a: 1}}]}}]\n",
        f"domain_policies:\n  fin: {_FIN_BODY}\nx: !!omap [{{tenants: !!omap [!!str {{a: 1}}]}}]\n",
        f"domain_policies:\n  fin: {_FIN_BODY}\nx: [!!omap [{{tenants: !!omap [!custom {{a: 1}}]}}]]\n",
        f"domain_policies:\n  fin: {_FIN_BODY}\nx: {{tenants: [!!omap [{{tenants: !!omap [!custom {{a: 1}}]}}]]}}\n",
        f"domain_policies:\n  fin: {_FIN_BODY}\nx: !!omap [!!merge {{a: 1}}]\n",
        f"domain_policies:\n  fin: {_FIN_BODY}\nx: !!pairs [{{tenants: !!omap [!!merge {{a: 1}}]}}]\n",
        # An anchored mapping PyYAML builds as a mapping first is rewritten
        # in place (flatten_mapping drops `<<`, retags `=`), so the same node
        # named later as an omap/pairs item no longer holds what da-guard
        # judges: which comes first is build order, not modelled (#2763).
        f"domain_policies:\n  fin: {_FIN_BODY}\ny: &m {{<<: {{a: 1}}}}\nx: !!omap [*m]\n",
        f"domain_policies:\n  fin: {_FIN_BODY}\ny: &m {{=: 1}}\nx: !!omap [*m]\n",
        f"domain_policies:\n  fin: {_FIN_BODY}\ny: &m {{<<: {{a: 1}}}}\nx: !!pairs [*m]\n",
        f"domain_policies:\n  fin: {_FIN_BODY}\nx: {{tenants: [&m {{<<: {{a: 1}}}}]}}\nz: !!omap [*m]\n",
        f"domain_policies:\n  fin: {_FIN_BODY}\ny: &m {{! \"<<\": {{a: 1}}}}\nx: !!omap [*m]\n",
    ]


def _r3_directive_tab_start() -> list[str]:
    """Hub #2486 PR-7c round 3 (R3-B1): a directive, then `---` and a tab.
    PyYAML cannot start a token at the tab and the generator drops the file;
    a reader that rewrote the directive away read a policy from it."""
    return [
        "%FOO x\n---\t\n" + _POLICY,
        "%YAML 1.2\n---\t\n" + _POLICY,
    ]


# A `%TAG` directive's handle and prefix, as the seeded documents write it.
_TAG_DIRECTIVE = "%TAG !y! tag:yaml.org,2002:"


def _n1_directive_tab() -> list[str]:
    """Hub #2486 PR-7c final blind review (N1): a directive line with a tab in
    it, or `%YAML 1.1` / `%TAG` then `---` and a tab. The generator drops the
    file; on main da-guard and tenant-api read a policy from it."""
    lines = ("%YAML\t1.1", "%YAML 1.1\t# c", "%YAML 1.1\t",
             "%TAG\t!y! tag:yaml.org,2002:", "%TAG !y!\ttag:yaml.org,2002:",
             _TAG_DIRECTIVE + "\t# c", _TAG_DIRECTIVE + "\t")
    docs = [line + "\n---\n" + _POLICY for line in lines]
    docs += ["%YAML 1.1\n---\t\n" + _POLICY, _TAG_DIRECTIVE + "\n---\t\n" + _POLICY,
             "# c\n\n  \n%YAML\t1.1\n---\n" + _POLICY,
             "%YAML\t1.1\r\n---\r\n" + _POLICY.replace("\n", "\r\n"),
             "%YAML\t1.1\r---\r" + _POLICY.replace("\n", "\r")]
    return docs


def _n1_dash_tab() -> list[str]:
    """N1 without a directive: `---` at column 0 then a tab, before, as or
    after the first document. The generator drops every one."""
    return [
        "---\t\n" + _POLICY,
        f"---\tdomain_policies: {{fin: {_FIN_BODY}}}\n",
        f"---\t{{domain_policies: {{fin: {_FIN_BODY}}}}}\n",
        "---\t# c\n" + _POLICY,
        "---\t&a\n" + _POLICY,
        "---\t\r\n" + _POLICY.replace("\n", "\r\n"),
        "---\t\r" + _POLICY.replace("\n", "\r"),
        _POLICY + "---\t\n",
    ]


def _n1_not_a_directive() -> list[str]:
    """Controls for N1: `%` and `---` and a tab inside the first document,
    never at column 0 — the generator reads each; so must every reader."""
    return [
        _policy(domain_extra="    description: |\n      %YAML\t1.1\n      ---\tx\n"),
        _policy(domain_extra='    description: "a ---\tb"\n'),
        _policy(domain_extra='    description: "a\n      ---\tb %TAG\tc"\n'),
        _policy(domain_extra="    description: '%YAML\t1.1'\n"),
    ]


# Policies the generator reads, one per way of writing one (TAB family, hub
# #2486 PR-7c round 6): block, flow with commas, a tagged merge, block
# sequences, explicit `---` / `...`, comments, quotes and a block scalar,
# an explicit `?` key.
_TAB_BASES = (
    _POLICY,
    "domain_policies: {fin: " + _FIN_BODY + ", ops: {tenants: [t1, t2], "
    "constraints: {forbidden_receiver_types: [slack, email]}}}\n",
    "x-b: &b " + _FIN_BODY + "\ndomain_policies:\n  fin:\n    !!merge <<: *b\n",
    "domain_policies:\n  fin:\n    tenants:\n    - t1\n    - t2\n    constraints:\n"
    "      forbidden_receiver_types:\n      - slack\n",
    "---\n" + _POLICY + "...\n",
    "# c\ndomain_policies:  # c\n  fin:\n    description: |\n      a b\n"
    "    tenants: ['t1', \"t2\"]\n    constraints:\n      forbidden_receiver_types: [slack]\n"
    "      require_critical_escalation: true\n",
    "domain_policies:\n  ? fin\n  : tenants: [t1]\n    constraints: {forbidden_receiver_types: [slack]}\n",
)
# Where a TAB goes after an indicator: these characters, and `---` / `...`.
_TAB_INDICATORS = (":", ",", "-", "{", "[", "---", "...")
# At most this many rows per TAB class, drawn with a fixed seed.
_TAB_ROWS = 80


def _tab_sample(docs: list[str], salt: int) -> list[str]:
    unique = list(dict.fromkeys(docs))
    if len(unique) <= _TAB_ROWS:
        return unique
    picked = sorted(random.Random(SEED * 10 + salt).sample(range(len(unique)), _TAB_ROWS))
    return [unique[i] for i in picked]


def _tab_space() -> list[str]:
    """TAB family: one space of a policy the generator reads written as a TAB.
    PyYAML takes a TAB as a separator only inside quotes, comments and block
    scalars (scan_to_next_token skips spaces only); yaml.v3 skipped it in
    flow context and after a token on the line."""
    return _tab_sample([b[:i] + "\t" + b[i + 1:] for b in _TAB_BASES
                        for i, ch in enumerate(b) if ch == " "], 1)


def _tab_trailing() -> list[str]:
    """TAB family: a TAB at the end of a line."""
    return _tab_sample([b[:i] + "\t" + b[i:] for b in _TAB_BASES
                        for i, ch in enumerate(b) if ch == "\n"], 2)


def _tab_after_indicator() -> list[str]:
    """TAB family: a TAB right after an indicator (`:`, `,`, `-`, `{`, `[`,
    `---`, `...`)."""
    docs = []
    for b in _TAB_BASES:
        for ind in _TAB_INDICATORS:
            start = b.find(ind)
            while start >= 0:
                i = start + len(ind)
                docs.append(b[:i] + "\t" + b[i:])
                start = b.find(ind, start + 1)
    return _tab_sample(docs, 3)


def _line_breaks() -> list[str]:
    """NEL / LS / PS as the line break (both readers take them as one), alone
    and with a TAB before or after one."""
    docs = []
    for brk in ("\x85", "\u2028", "\u2029"):
        for b in _TAB_BASES:
            docs.append(b.replace("\n", brk))
        docs.append(_POLICY.replace("\n", brk, 1).replace("\n", "\t" + brk, 1))
        docs.append(_POLICY.replace("\n", "\t" + brk, 1))
        docs.append(_POLICY.replace("\n", brk + "\t", 1))
        docs.append(_POLICY.replace(": [t1]\n", ":" + brk + "\t[t1]\n"))
    return docs


# How an `encoding` row's file is written (its `doc` is the text): UTF-16
# with its byte order mark, which yaml.v3 decodes and the generator (UTF-8
# only) cannot read; Latin-1, which is no UTF-8 to either.
ENCODINGS = {"utf-16-le-bom": ("utf-16-le", b"\xff\xfe"), "utf-16-be-bom": ("utf-16-be", b"\xfe\xff"),
             "latin-1": ("latin-1", b"")}


def _encoded() -> list[tuple[str, str]]:
    """(encoding, document): a policy the generator reads as UTF-8, written in
    another encoding."""
    out = []
    for enc in ("utf-16-le-bom", "utf-16-be-bom"):
        out += [(enc, _POLICY), (enc, _TAB_BASES[1]), (enc, "# é\n" + _POLICY)]
    out.append(("latin-1", "# é\n" + _POLICY))
    return out


def _encode(doc: str, encoding: str | None) -> bytes:
    if encoding is None:
        return doc.encode("utf-8")
    codec, bom = ENCODINGS[encoding]
    return bom + doc.encode(codec)


def _merge_chains() -> list[tuple[str, str]]:
    """Domains (or anchors) chained by `<<: *previous`, lengths 50 and 200."""
    out = []
    for n in (50, 200):
        lines = ["domain_policies:", f"  d0: &c0 {_FIN_BODY}"]
        lines += [f"  d{i}: &c{i} {{<<: *c{i - 1}}}" for i in range(1, n)]
        out.append((f"merge-chain-domains-{n}", "\n".join(lines) + "\n"))
        lines = [f"x-c0: &c0 {_FIN_BODY}"]
        lines += [f"x-c{i}: &c{i} {{<<: *c{i - 1}}}" for i in range(1, n)]
        lines += ["domain_policies:", f"  fin: {{<<: *c{n - 1}}}"]
        out.append((f"merge-chain-anchors-{n}", "\n".join(lines) + "\n"))
    return out


def _many_aliases() -> list[tuple[str, str]]:
    """#2715: no merge key, one anchored list of n tenants that a domain aliases."""
    out = []
    for n in (989, 990):
        items = ", ".join(f"tenant-{i}" for i in range(n))
        doc = (f"x-anchors:\n  all: &all [{items}]\ndomain_policies:\n"
               "  dom0: {tenants: *all, constraints: {forbidden_receiver_types: [slack]}}\n")
        out.append((f"2715-aliased-tenants-{n}", doc))
    return out


# --- Looser-only fuzz (#2759) -----------------------------------------------
#
# Documents mixing the features the shapes above are about — null keys,
# `!!omap` / `!!pairs` / `!!set`, merge keys, `tenants` nested in those,
# explicit tags, `=` values, anchors and aliases across them — from a fixed
# seed. Each row where a Go reader differs is recorded in
# merge_key_go_verdicts.json, and reader_divergence_catalog.yaml accepts only
# refusals here (go_stricter): a read of a file the generator drops, or of
# other values, has no entry and is red (#2759).

_FUZZ_SEED = 2759
_FUZZ_DOCS = 400
_FUZZ_NULL_KEYS = ("~", "null", '! "null"', "!!null x", "*n0 ")
# Leaves every reader builds, and leaves PyYAML refuses wherever it builds them.
_FUZZ_LEAVES = ("1", "t1", "~", "[t1]", "!!str t2", "{a: 1}", "[{a: 1}]")
_FUZZ_BAD_LEAVES = ("=", "!!int z", "!custom {a: 1}", "!!str {a: 1}", "!!merge {a: 1}", "!custom x")
_FUZZ_KEYS = ("tenants", "tenants", "<<", "<<", "a", "constraints", "forbidden_receiver_types")


class _Fuzz:
    """One fuzz document; anchors are aliased only once their node is done."""

    def __init__(self, r: random.Random) -> None:
        self.r = r
        self.done: list[str] = []
        self.next_anchor = 0

    def key(self) -> str:
        r = self.r
        return r.choice(_FUZZ_NULL_KEYS) if r.random() < 0.45 else r.choice(_FUZZ_KEYS)

    def node(self, depth: int) -> str:
        r = self.r
        if self.done and r.random() < 0.15:
            return "*" + r.choice(self.done)
        anchor = None
        if r.random() < 0.2:
            anchor = f"a{self.next_anchor}"
            self.next_anchor += 1
        text = self.body(depth)
        if anchor is None:
            return text
        self.done.append(anchor)
        return f"&{anchor} {text}"

    def body(self, depth: int) -> str:
        r = self.r
        kind = "leaf" if depth >= 3 else r.choice(
            ("leaf", "map", "map", "omap", "omap", "pairs", "set", "seq", "tenants", "merge"))
        if kind == "leaf":
            return r.choice(_FUZZ_BAD_LEAVES if r.random() < 0.1 else _FUZZ_LEAVES)
        if kind == "map":
            entries = []
            for _ in range(r.randint(1, 3)):
                k = self.key()
                entries.append(f"{k}: {self.merge_value(depth + 1) if k == '<<' else self.node(depth + 1)}")
            return "{" + ", ".join(entries) + "}"
        if kind in ("omap", "pairs"):
            items = []
            for _ in range(r.randint(1, 2)):
                if r.random() < 0.85:
                    items.append(f"{{{self.key()}: {self.node(depth + 1)}}}")
                else:  # an item that is no single-pair mapping: an alias, a tagged node, ...
                    items.append(self.node(depth + 1))
            return f"!!{kind} [" + ", ".join(items) + "]"
        if kind == "set":
            keys = [r.choice(_FUZZ_NULL_KEYS + ("a", "t1", "tenants")) for _ in range(r.randint(1, 2))]
            return "!!set {" + ", ".join(f"{k}: ~" for k in keys) + "}"
        if kind == "seq":
            return "[" + ", ".join(self.node(depth + 1) for _ in range(r.randint(1, 2))) + "]"
        if kind == "tenants":
            items = ", ".join(self.node(depth + 1) for _ in range(r.randint(1, 2)))
            return "{tenants: " + r.choice(("[", "!!omap [", "!!pairs [")) + items + "]}"
        return f"{{<<: {self.merge_value(depth + 1)}, a: 1}}"

    def merge_value(self, depth: int) -> str:
        """A merge key's value: mostly a mapping or a sequence of them."""
        r = self.r
        c = r.random()
        if c < 0.15 or depth >= 3:
            return self.node(depth)  # anything: an alias, a tagged collection, a scalar
        if c < 0.6:
            return self.mapping(depth)
        return "[" + ", ".join(self.mapping(depth) for _ in range(r.randint(1, 2))) + "]"

    def mapping(self, depth: int) -> str:
        """A flow mapping, maybe anchored, or an alias."""
        r = self.r
        if self.done and r.random() < 0.2:
            return "*" + r.choice(self.done)
        text = "{" + f"{self.key()}: {self.node(depth + 1)}" + "}"
        if r.random() < 0.2:
            anchor = f"a{self.next_anchor}"
            self.next_anchor += 1
            self.done.append(anchor)
            return f"&{anchor} {text}"
        return text

    def document(self) -> str:
        r = self.r
        fin = _FIN_BODY
        if r.random() < 0.25:
            fin = fin[:-1] + f", {self.key()}: {self.node(1)}}}"
        doc = f"domain_policies:\n  fin: {fin}\n"
        if r.random() < 0.25:
            doc += f"  {r.choice(('ops', '~', 'null'))}: {self.node(1)}\n"
        for i in range(r.randint(1, 3)):
            doc += f"x{i}: {self.node(0)}\n"
        if "*n0" in doc:
            doc = "nul: &n0 ~\n" + doc
        return doc


def _fuzz_looser_only() -> list[str]:
    r = random.Random(_FUZZ_SEED)
    out: list[str] = []
    seen: set = set()
    while len(out) < _FUZZ_DOCS:
        doc = _Fuzz(r).document()
        if doc not in seen:
            seen.add(doc)
            out.append(doc)
    return out


# --- #2759's Go-looser classes (ADR-036 step 2) -------------------------------
#
# Measured on main (2026-10-09) and listed in #2759 as Go reading what the
# generator drops or missing what it enforces; no row held them before. They
# are here so the Go halves record them and the catalog pins them as still
# looser, row by row: each is a blocker for ADR-036's phase 2.

def _2759_a_depth() -> list[str]:
    """A: a nesting depth of 600 — PyYAML's recursive constructor gives up
    and the generator drops the file; Go reads the policy. Depth 300 is the
    control every reader reads (well below the limit even under pytest's own
    stack frames)."""
    docs = []
    for n in (600, 300):
        docs.append(_POLICY + "x: " + "[" * n + "]" * n + "\n")
        docs.append(_POLICY + "x: " + "{a: " * n + "1" + "}" * n + "\n")
    return docs


def _2759_f_set() -> list[str]:
    """F: `!!set` where domain_policies, a domain or its constraints go.
    PyYAML builds a set of the keys (no mapping): the generator drops the
    file (domain_policies) or enforces nothing there; Go reads a mapping."""
    return [
        "domain_policies: !!set\n  fin:\n    tenants: [t1]\n    constraints:\n"
        "      forbidden_receiver_types: [slack]\n",
        "domain_policies:\n  fin: !!set\n    tenants: [t1]\n    constraints:\n"
        "      forbidden_receiver_types: [slack]\n",
        "domain_policies:\n  fin:\n    tenants: [t1]\n    constraints: !!set\n"
        "      forbidden_receiver_types: [slack]\n",
        f"domain_policies: !!set {{fin: {_FIN_BODY}}}\n",
        "domain_policies: {fin: !!set {tenants: [t1], constraints: {forbidden_receiver_types: [slack]}}}\n",
        "domain_policies: {fin: {tenants: [t1], constraints: !!set {forbidden_receiver_types: [slack]}}}\n",
    ]


def _2759_k1_merge_collection_key() -> list[str]:
    """K1: a merge value holding a mapping with a collection key — PyYAML
    cannot hash the key and the generator drops the file; spelled as a
    mapping, a merge sequence and a `!!omap`."""
    return [
        _POLICY + "x: {<<: {? [a] : 1}}\n",
        _POLICY + "x: {<<: [{? [a] : 1}]}\n",
        _POLICY + "x: {<<: !!omap [{? [a] : 1}]}\n",
    ]


def _2759_n_bom_512() -> list[str]:
    """N: a UTF-8 BOM inside a comment, its three bytes straddling byte 512
    (starting at byte 510 or 511). The generator reads and enforces the
    policy; both Go readers read none. Starting at byte 509 is the control."""
    return ["#" + "a" * (start - 1) + "﻿\n" + _POLICY for start in (510, 511, 509)]


_KNOWN_STRICTER_2759 = "known-stricter-2759"
_FUZZ_LOOSER_ONLY = "fuzz-looser-only"


def _shapes() -> list[tuple[str, str]]:
    """(shape, document), fixed order, no repeats."""
    out = []
    for shape, docs in (("s4-merge-tagged-collection", _s4_merge_tagged_collections()),
                        ("s2-tenant-item-not-a-string", _s2_tenant_items()),
                        ("s6-nonspecific-key", _s6_nonspecific_keys()),
                        ("s6-nonspecific-value", _s6_nonspecific_values()),
                        ("s1-multi-document", _s1_multi_document()),
                        ("s5-top-level-tenants", _s5_top_level_tenants()),
                        ("f1-receiver-type-unbuilt", _f1_receiver_type_items()),
                        ("f1-receiver-type-not-a-string", _f1_allowed_not_a_string()),
                        ("2758-collection-receiver-type", _collection_receiver_types()),
                        ("b1-escalation-in-place", _b1_escalation_in_place()),
                        ("e-no-constructor", _e_no_constructor()),
                        # Directive shapes da-guard refuses but the generator reads: #2759.
                        ("e-reader-stricter", _e_reader_stricter()),
                        (_KNOWN_STRICTER_2759, _known_stricter_2759()),
                        ("r3-directive-tab-start", _r3_directive_tab_start()),
                        ("n1-directive-tab", _n1_directive_tab()),
                        ("n1-dash-tab", _n1_dash_tab()),
                        ("n1-not-a-directive", _n1_not_a_directive()),
                        ("tab-space", _tab_space()),
                        ("tab-trailing", _tab_trailing()),
                        ("tab-after-indicator", _tab_after_indicator()),
                        ("line-break-nel-ls-ps", _line_breaks()),
                        (_FUZZ_LOOSER_ONLY, _fuzz_looser_only()),
                        ("2759-a-depth", _2759_a_depth()),
                        ("2759-f-set", _2759_f_set()),
                        ("2759-k1-merge-collection-key", _2759_k1_merge_collection_key()),
                        ("2759-n-bom-512", _2759_n_bom_512())):
        out += [(shape, d) for d in docs]
    out += _merge_chains() + _many_aliases()
    seen: set = set()
    unique = []
    for shape, doc in out:
        if doc not in seen:
            seen.add(doc)
            unique.append((shape, doc))
    return unique


def _verdict(doc: str, root: Path, encoding: str | None = None) -> object:
    (root / "_domain_policy.yaml").write_bytes(_encode(doc, encoding))
    with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
        res = _parse_config_files(str(root))
    if res["policy_file_errors"]:
        return "unusable"
    out = {}
    for name, body in res["domain_policies"].items():
        if not isinstance(body, dict):
            continue
        # As check_domain_policies (_grar_validate) reads a policy: `tenants`
        # present and not a list skips the policy (a finding of its own),
        # and a `tenants` entry that is not a string names no tenant.
        tenants = body.get("tenants", [])
        if not isinstance(tenants, list):
            continue
        cons = body.get("constraints")
        if not isinstance(cons, dict):
            continue
        forbidden = cons.get("forbidden_receiver_types")
        forbidden = forbidden if isinstance(forbidden, list) else []
        allowed = cons.get("allowed_receiver_types")
        allowed = allowed if isinstance(allowed, list) else []
        # Only a string entry can forbid anything: a non-string one (`! "null"`
        # is None, `! "true"` True to PyYAML) equals only a non-string
        # receiver type, which the generator already refuses on its own
        # ("missing required 'receiver.type'", blocking). So `forbidden` is
        # the string entries, and a list of none forbids nothing (#2730 §6).
        forbidden = sorted(f for f in forbidden if isinstance(f, str))
        if not forbidden and not allowed:
            continue
        entry = {"tenants": [t for t in tenants if isinstance(t, str)], "forbidden": forbidden}
        # allowed_receiver_types, when a non-empty list: it restricts, and only
        # its string entries are types it allows (a list of only non-string
        # entries — `[!!null webhook]` is [None] — allows no type at all).
        if allowed:
            entry["allowed"] = sorted(a for a in allowed if isinstance(a, str))
        # require_critical_escalation as the generator enforces it (`is True`,
        # _grar_validate): `! "true"` is True to PyYAML (#2730 §6).
        if cons.get("require_critical_escalation") is True:
            entry["escalation"] = True
        out[str(name)] = entry
    return out


def row_id(doc: str, encoding: str | None = None) -> str:
    """A row's stable id: the first 16 hex digits of the sha256 of the file
    it stands for. The Go halves compute the same from their own bytes."""
    return hashlib.sha256(_encode(doc, encoding)).hexdigest()[:16]


# Finding.blocks values that block the generator as production runs it
# (`generate-routes --validate --strict`): all but "never" (_grar_merge).
_PRODUCTION_BLOCKS = ("always", "strict", "validate")


def _strict_blocks(root: Path) -> list[dict]:
    """The generator's own blocking findings on the policy file in `root`,
    run as production runs it (strict policies): load_tenant_tree's
    structured findings (`Finding.kind` / `.blocks`, _grar_merge) whose
    `blocks` stops `--validate --strict`. The tree holds the policy file
    alone, so every finding is about it — and none depends on a tenant's
    routing (a violation by a tenant is the tenant's, not the file's)."""
    with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
        tree = load_tenant_tree(str(root), strict_policies=True)
    found = {(f.kind, f.policy or "", f.field or "") for f in tree.schema_warnings
             if getattr(f, "blocks", None) in _PRODUCTION_BLOCKS}
    return [{"kind": k, "policy": p, "field": fld} for k, p, fld in sorted(found)]


def _row(doc: str, root: Path, encoding: str | None = None, **extra) -> dict:
    row: dict = {"doc": doc, "id": row_id(doc, encoding), "pyyaml": _verdict(doc, root, encoding)}
    if encoding is not None:
        row["encoding"] = encoding
    # A file the generator reads but blocks on under --strict: the verdict
    # alone (what it enforces) cannot say so. An unusable file blocks anyway.
    if row["pyyaml"] != "unusable":
        blocks = _strict_blocks(root)
        if blocks:
            row["pyyaml_strict_blocks"] = blocks
    row.update(extra)
    return row


def _rows() -> list[dict]:
    rows = []
    empty = 0
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        for doc in _documents():
            row = _row(doc, root)
            if row["pyyaml"] == {}:  # nothing enforced: one in four kept
                empty += 1
                if empty % 4:
                    continue
            if NONSPECIFIC_SPELLING in doc:
                row["nonspecific_tag"] = NONSPECIFIC
            rows.append(row)
            if len(rows) == DOCS:
                break
        for shape, doc in _shapes():
            row = _row(doc, root, shape=shape)
            if _NONSPECIFIC_RE.search(doc):
                row["nonspecific_tag"] = NONSPECIFIC
            rows.append(row)
        for encoding, doc in _encoded():
            rows.append(_row(doc, root, encoding, shape="encoding-not-utf8"))
    return rows


def _render(rows: list[dict]) -> str:
    comment = [
        "What the route generator's reader (_parse_config_files, PyYAML) makes",
        "of `_domain_policy.yaml` documents built around YAML merge keys",
        "(#2677). GENERATED by tests/shared/test_merge_key_policy_corpus.py",
        "from a fixed seed — regenerate with REGEN_MERGE_KEY_CORPUS=1; never",
        "edit by hand. `pyyaml`: \"unusable\" (the file or its domain_policies",
        "block is dropped), else each domain the generator enforces (a list",
        "`tenants`) with string forbidden_receiver_types or a non-empty",
        "allowed_receiver_types list: its string tenants and those forbidden",
        "types, sorted (a non-string entry forbids nothing a usable config",
        "has), `allowed` (its string entries, sorted) when the allowed list is",
        "non-empty, and `escalation: true` when require_critical_escalation",
        "is True.",
        "Go: da-guard's ParseDomainPolicies must say the same, or what",
        "merge_key_go_verdicts.json records for the row; tenant-api's",
        "parseConfig the same, or what it records, or refuse the file.",
        "`id`: the first 16 hex digits of the sha256 of the file the row",
        "stands for (the key of merge_key_go_verdicts.json and of",
        "reader_divergence_catalog.yaml). `pyyaml_strict_blocks`: the file is",
        "read, but the generator run as in production (--validate --strict)",
        "blocks on it — its own blocking findings (kind, policy, field); the",
        "Go halves hold such a row to \"unusable\" (refused, or a blocking",
        "problem). `nonspecific_tag`: the row",
        "carries the non-specific tag `!` on a quoted scalar, which Go reads",
        "through the vendored yaml.v3's patch (#2730 §6). `encoding`: the",
        "file is `doc` written in that encoding (utf-16-le-bom, utf-16-be-bom:",
        "the byte order mark, then UTF-16; latin-1), not UTF-8. `shape`: a",
        "deterministic row pinning one ticket's shape",
        "(hub #2486, PR-7c/7d; #2759 A, F, K1, N), after the seeded ones.",
    ]
    body = ",\n".join("    " + json.dumps(r, ensure_ascii=False, sort_keys=True) for r in rows)
    # Line and byte-order characters stay escaped: the fixture is one row a
    # line, and editors and hooks treat these as breaks or a file header.
    for ch in ("\u2028", "\u2029", "\x85", "\ufeff"):
        body = body.replace(ch, "\\u%04x" % ord(ch))
    return ("{\n  \"_comment\": " + json.dumps(comment, ensure_ascii=False, indent=4).replace("\n", "\n  ")
            + ",\n  \"pyyaml\": \"" + yaml.__version__ + "\",\n  \"rows\": [\n" + body + "\n  ]\n}\n")


def test_corpus_is_what_the_generator_reads() -> None:
    fresh = _render(_rows())
    if os.environ.get("REGEN_MERGE_KEY_CORPUS"):
        CORPUS_PATH.write_text(fresh, encoding="utf-8")
    on_disk = CORPUS_PATH.read_text(encoding="utf-8")
    assert on_disk == fresh, (
        "merge_key_policy_corpus.json is not what the generator's reader says; "
        "regenerate with REGEN_MERGE_KEY_CORPUS=1 and review the diff")


def test_corpus_is_not_vacuous() -> None:
    rows = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))["rows"]
    unusable = [r for r in rows if r["pyyaml"] == "unusable"]
    enforced = [r for r in rows if isinstance(r["pyyaml"], dict) and r["pyyaml"]]
    assert len(unusable) > 40 and len(enforced) > 150, (len(unusable), len(enforced))
    docs = "".join(r["doc"] for r in rows)
    for needle in ("*k", "*d", "*l", "*v0", "!!merge q", "!y!merge", "! <<", '"<<"',
                   "!<tag:yaml.org,2002:merge> <<", ": [*d"):
        assert needle in docs, needle
    shapes = {r.get("shape") for r in rows} - {None}
    for shape in ("s4-merge-tagged-collection", "s2-tenant-item-not-a-string", "s6-nonspecific-key",
                  "s6-nonspecific-value", "s1-multi-document", "s5-top-level-tenants",
                  "f1-receiver-type-unbuilt", "f1-receiver-type-not-a-string",
                  "2758-collection-receiver-type", "b1-escalation-in-place",
                  "e-no-constructor", "e-reader-stricter", "r3-directive-tab-start",
                  "n1-directive-tab", "n1-dash-tab", "n1-not-a-directive",
                  "tab-space", "tab-trailing", "tab-after-indicator", "line-break-nel-ls-ps",
                  "encoding-not-utf8", "known-stricter-2759", "fuzz-looser-only",
                  "merge-chain-domains-200", "merge-chain-anchors-200", "2715-aliased-tenants-990",
                  "2759-a-depth", "2759-f-set", "2759-k1-merge-collection-key", "2759-n-bom-512"):
        assert shape in shapes, shape


def test_row_ids_are_unique_and_are_the_file_hash() -> None:
    """The snapshot and the catalog key rows by `id`: two rows sharing one
    would let one row's entry excuse the other."""
    rows = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))["rows"]
    ids = [r["id"] for r in rows]
    assert len(set(ids)) == len(ids), "two corpus rows share an id"
    for r in rows:
        assert r["id"] == row_id(r["doc"], r.get("encoding")), r["id"]
