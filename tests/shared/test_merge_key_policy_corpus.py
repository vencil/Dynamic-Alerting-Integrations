"""Python half of tests/shared/merge_key_policy_corpus.json (#2677).

The corpus is `_domain_policy.yaml` documents built from the YAML features
that decide what a merge key is and what it supplies: anchors and aliases
(as a key, as a value, as a merge source), merge-key spellings (`<<` plain,
quoted, under `!!merge`, `!<tag:yaml.org,2002:merge>`, a `%TAG` handle,
the non-specific `!`; a tagged `!!merge q`), merge sequences, own keys
beside merges, and merges nested in merge values, at every level of a
policy (the top, `domain_policies`, a domain, its `constraints`); also
merge-tagged `tenants` items (the generator reads those as source text),
refused `require_critical_escalation` values outside the one place
tenant-api reads them leniently, and repeated keys where no policy reader
looks. Rows with nothing enforced are thinned to one in four. Each row
holds what the route generator's own reader (`_parse_config_files`, PyYAML)
makes of it: `unusable` (the file or its domain_policies block is dropped)
or, per domain with a non-empty `forbidden_receiver_types`, its tenants and
forbidden types.

The Go halves read the same rows:
- components/threshold-exporter/app/pkg/routingpolicy/merge_key_corpus_test.go:
  da-guard's ParseDomainPolicies says exactly what PyYAML says.
- components/tenant-api/internal/policy/merge_key_corpus_test.go: tenant-api's
  parseConfig says what PyYAML says, or refuses the file — never a policy
  PyYAML does not read.

The rows are generated from a fixed seed, never hand-edited:

    REGEN_MERGE_KEY_CORPUS=1 python3 -m pytest \
        tests/shared/test_merge_key_policy_corpus.py -q -p no:cacheprovider
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import random
import sys
import tempfile
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "tools"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "tools" / "ops"))
from _grar_parse import _parse_config_files  # noqa: E402

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
# file). Written in place under domain_policies.<d>.constraints tenant-api
# reads them leniently (#2325), a known difference this corpus leaves out;
# everywhere else (the top, a domain, an unread key, an anchored value also
# aliased elsewhere) both must refuse.
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
# and the one a yaml.Node cannot tell from a quoted "<<" (GO_BLIND).
Q_SPELLINGS = ("!!merge {q}", "!<tag:yaml.org,2002:merge> {q}", "!y!merge {q}")
BLIND_SPELLING = '! "<<"'
# Anchored key spellings an alias key may name (x-keys).
KEY_ANCHOR_SPELLINGS = ("<<", "<<", "!!merge <<", "!!merge qk", '"<<"', "! <<", "!!str <<")

GO_BLIND = "yaml.v3 drops the non-specific tag `!` on a quoted `<<`"


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
        self.anchored_escalation = ""  # a refused escalation anchored in constraints

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
            s, text = BLIND_SPELLING, "<<"
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
            where = r.randrange(4)
            if where == 0:
                self.extra.append(f"require_critical_escalation: {v}")
            elif where == 1:
                self.extra.append(f"x-junk: {{require_critical_escalation: {v}}}")
            elif where == 2:
                self.domain_escalation = v
            else:
                self.anchored_escalation = f"&e0 {v}"
                self.extra.append("x-use-e: *e0")
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


def _verdict(doc: str, root: Path) -> object:
    (root / "_domain_policy.yaml").write_text(doc, encoding="utf-8")
    with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
        res = _parse_config_files(str(root))
    if res["policy_file_errors"]:
        return "unusable"
    out = {}
    for name, body in res["domain_policies"].items():
        if not isinstance(body, dict):
            continue
        cons = body.get("constraints")
        forbidden = cons.get("forbidden_receiver_types") if isinstance(cons, dict) else None
        if not isinstance(forbidden, list) or not forbidden:
            continue
        tenants = body.get("tenants")
        out[str(name)] = {"tenants": [str(t) for t in tenants] if isinstance(tenants, list) else [],
                          "forbidden": sorted(str(f) for f in forbidden)}
    return out


def _rows() -> list[dict]:
    rows = []
    empty = 0
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        for doc in _documents():
            verdict = _verdict(doc, root)
            if verdict == {}:  # nothing enforced: one in four kept
                empty += 1
                if empty % 4:
                    continue
            row: dict = {"doc": doc, "pyyaml": verdict}
            if BLIND_SPELLING in doc:
                row["go_blind"] = GO_BLIND
            rows.append(row)
            if len(rows) == DOCS:
                return rows
    return rows


def _render(rows: list[dict]) -> str:
    comment = [
        "What the route generator's reader (_parse_config_files, PyYAML) makes",
        "of `_domain_policy.yaml` documents built around YAML merge keys",
        "(#2677). GENERATED by tests/shared/test_merge_key_policy_corpus.py",
        "from a fixed seed — regenerate with REGEN_MERGE_KEY_CORPUS=1; never",
        "edit by hand. `pyyaml`: \"unusable\" (the file or its domain_policies",
        "block is dropped), else each domain with a non-empty",
        "forbidden_receiver_types: its tenants and its forbidden types, sorted.",
        "Go: da-guard's ParseDomainPolicies must say the same; tenant-api's",
        "parseConfig the same or refuse the file. `go_blind`: yaml.v3 hands Go",
        "nothing that tells the row apart from another; Go skips it.",
    ]
    body = ",\n".join("    " + json.dumps(r, ensure_ascii=False, sort_keys=True) for r in rows)
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
