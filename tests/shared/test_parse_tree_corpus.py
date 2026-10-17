"""Python half of tests/shared/parse_tree_corpus.json (#2897, ADR-036 §2).

`da-guard parse-tree` refuses a CLOSED LIST of shapes — the ones whose
meaning is ambiguous between YAML readers (the kinds of internal/parsetree;
owner decision on ADR-036 §2, option B). Everything else it reads as yaml.v3
reads it, including files today's route generator refuses only because
PyYAML does (a TAB separator, a duplicate anchor, deep nesting, …); the
migration audit (#2898) lists those per file as "previously refused, now
read as X". This corpus pins both directions, row by row, so neither drifts
silently.

Every row carries a written `category`:
- `unreadable:<kind>` — parse-tree must refuse it with exactly that kind.
  When today's generator reads it, the row's `note` starts "stricter than
  today" and says what changes.
- `pyyaml_only` — today's generator refuses it, parse-tree reads it; the
  `note` says what yaml.v3 reads.
- `both_read` — both read it.

The generator's verdict is frozen as data: the file alone in an empty
conf.d, run through `_grar_parse._parse_config_files` (the reader of
`generate-routes --validate --strict`: PyYAML via
`_lib_io.strict_load_exporter_keys`). A file it drops whole is booked in
`files_skipped` and refuses the run; `generator` is then "rejected", with
the reason, else "read". One drop is not a YAML verdict and does not count:
"'tenants' must be a mapping" (a semantic question). This test checks each
category against that verdict (`pyyaml_only` ⇒ rejected, `both_read` ⇒ read).

Rows:
- `rows`: hand-picked files — every text kind, the ADR §2 example, values
  whose meaning changes (yes / on / off, 0777, a `010` key), the examples
  of #2759, #2700, #2713 and #2674, and the PyYAML-only refusals.
- `merge_key_rows`: every row of merge_key_policy_corpus.json, by `id`
  (its file is not copied here), with the frozen generator verdict. Their
  categories are in parse_tree_merge_key_categories.json, written by the Go
  test (UPDATE_PARSE_TREE_CATEGORIES=1) from parse-tree itself and checked
  here against the generator verdict.

The Go half
(components/threshold-exporter/app/internal/parsetree/corpus_test.go) asserts parse-tree's verdict equals every row's category.

Regenerate (never edit by hand):

    REGEN_PARSE_TREE_CORPUS=1 python3 -m pytest \\
        tests/shared/test_parse_tree_corpus.py -q -p no:cacheprovider
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "tools"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "tools" / "ops"))
sys.path.insert(0, str(Path(__file__).parent))
from _grar_parse import _parse_config_files  # noqa: E402
from test_merge_key_policy_corpus import _encode, row_id  # noqa: E402

CORPUS_PATH = Path(__file__).parent / "parse_tree_corpus.json"
MERGE_KEY_CORPUS_PATH = Path(__file__).parent / "merge_key_policy_corpus.json"
MERGE_KEY_CATEGORIES_PATH = Path(__file__).parent / "parse_tree_merge_key_categories.json"
STRICTER = "stricter than today"

# The closed list: every kind parse-tree refuses a file for
# (internal/parsetree Kinds; the Go test asserts the two are one set), and
# the subset it can give a text (the other two are about the path, covered
# by da-guard's own tests).
CLOSED_LIST = ("read_failure", "not_regular_file", "invalid_utf8", "bom_not_at_start",
               "syntax_error", "multiple_documents", "non_specific_tag", "tag_not_allowed",
               "non_scalar_key", "null_key", "duplicate_key", "decode_error",
               "top_level_not_mapping")
TEXT_KINDS = ("invalid_utf8", "bom_not_at_start", "syntax_error", "multiple_documents",
              "non_specific_tag", "tag_not_allowed", "non_scalar_key", "null_key", "duplicate_key",
              "decode_error", "top_level_not_mapping")

# The one whole-file drop that is not about the YAML (see the docstring).
_NOT_A_YAML_VERDICT = "'tenants' must be a mapping"

_POLICY = ("domain_policies:\n  fin:\n    tenants: [t1]\n    constraints:\n"
           "      forbidden_receiver_types: [slack]\n")
_TENANT = ("tenants:\n  t1:\n    _routing:\n      receiver:\n        type: webhook\n"
           "        url: \"https://example.com/x\"\n")
_SECTION2 = ("tenants:\n  010:\n    _routing:\n      receiver:\n        type: slack\n"
             "        api_url: \"https://hooks.slack.com/x\"\n        send_resolved: yes\n"
             "      group_wait: 30s\n")


def _laughs(levels: int) -> str:
    out = ["l0: &l0 [x, x, x, x, x, x, x, x, x, x]\n"]
    for i in range(1, levels):
        out.append(f"l{i}: &l{i} [" + ", ".join([f"*l{i - 1}"] * 10) + "]\n")
    return "".join(out)


def _bom_at(start: int) -> str:
    """#2759 N: a BOM in a comment, its three bytes starting at byte `start`."""
    return "#" + "a" * (start - 1) + "\ufeff\n" + _POLICY


# (name, file, doc, encoding, category, note, source)
_ROWS: tuple[tuple, ...] = (
    # ── ADR-036 §2 and the values whose meaning changes ──
    ("adr036-s2-example", "t010.yaml", _SECTION2, None, "both_read",
     None, "ADR-036 §2"),
    ("bool-words-are-strings", "t.yaml",
     "tenants:\n  t1:\n    _routing:\n      receiver: {type: slack, api_url: \"https://x\", "
     "send_resolved: on}\n    _severity_dedup: off\n    a: no\n    b: y\n", None, "both_read",
     None,
     "ADR-036 §8"),
    ("octal-value-and-keys", "t.yaml", "tenants:\n  010:\n    cpu: 0777\n  0777: {cpu: 1e3}\n",
     None, "both_read",
     None, "ADR-036 §2"),
    ("empty-file", "t.yaml", "", None, "both_read",
     None, None),
    ("comments-only", "t.yaml", "# nothing\n", None, "both_read",
     None, None),
    ("null-document", "t.yaml", "~\n", None, "both_read",
     None, None),
    ("bom-at-start", "t.yaml", "\ufeff" + _TENANT, None, "both_read",
     None, None),
    ("quoted-merge-key", "t.yaml", "x: {\"<<\": {a: 1}, b: 2}\n" + _TENANT, None, "both_read",
     None,
     "ADR-036 §2"),
    ("merge-expanded", "t.yaml",
     "x-base: &b {type: webhook, url: \"https://x\"}\ntenants:\n  t1:\n    _routing:\n"
     "      receiver: {<<: *b, type: slack}\n", None, "both_read",
     None, None),
    # ── invalid_utf8 ──
    ("latin-1", "t.yaml", "# é\n" + _TENANT, "latin-1", "unreadable:invalid_utf8",
     None, None),
    ("utf-16-bom", "_domain_policy.yaml", _POLICY, "utf-16-le-bom", "unreadable:invalid_utf8",
     None, None),
    # ── bom_not_at_start ──
    ("bom-in-comment", "t.yaml", "# \ufeff\n" + _TENANT, None, "unreadable:bom_not_at_start",
     "stricter than today: PyYAML reads a BOM inside a comment", None),
    ("bom-in-value", "t.yaml", "tenants:\n  t1:\n    a: \"x\ufeff\"\n", None, "unreadable:bom_not_at_start",
     "stricter than today: PyYAML keeps the BOM in the string", None),
    ("2759-j-two-boms", "_domain_policy.yaml", "\ufeff\ufeff" + _POLICY, None, "unreadable:bom_not_at_start",
     "stricter than today: PyYAML reads the second BOM into the first key", "#2759 J"),
    ("2759-n-bom-510", "_domain_policy.yaml", _bom_at(510), None, "unreadable:bom_not_at_start",
     "stricter than today: PyYAML enforces the policy; yaml.v3 eats the byte after the BOM",
     "#2759 N"),
    ("2759-n-bom-511", "_domain_policy.yaml", _bom_at(511), None, "unreadable:bom_not_at_start",
     "stricter than today: PyYAML enforces the policy; yaml.v3 eats the byte after the BOM",
     "#2759 N"),
    # ── syntax_error ──
    ("unclosed-flow", "t.yaml", "tenants: {t1: [\n", None, "unreadable:syntax_error",
     None, None),
    ("2759-b-flow-colon", "_domain_policy.yaml",
     "domain_policies: {fin: {tenants:[t1]}}\n", None, "unreadable:syntax_error",
     "stricter than today: PyYAML reads `tenants:[t1]` as a key; yaml.v3 refuses it", "#2759 B"),
    ("2759-d-surrogate", "_domain_policy.yaml",
     "domain_policies:\n  fin:\n    tenants: [\"\\ud800\"]\n", None, "unreadable:syntax_error",
     "stricter than today: PyYAML builds a lone surrogate; yaml.v3 refuses it",
     "#2759 D"),
    ("2759-h-yaml-directive", "_domain_policy.yaml", "%YAML 1.2\n---\n" + _POLICY, None,
     "unreadable:syntax_error",
     "stricter than today: yaml.v3 refuses a %YAML 1.2 directive", "#2759 H"),
    ("escaped-slash", "t.yaml", "tenants:\n  t1:\n    a: \"x\\/y\"\n", None, "unreadable:syntax_error",
     "stricter than today: PyYAML reads `\\/` as `/`; yaml.v3 refuses the escape",
     "ADR-036 table"),
    ("tab-indentation", "t.yaml", "tenants:\n\tt1:\n\t\tcpu: 1\n", None, "unreadable:syntax_error",
     None,
     None),
    # ── multiple_documents ──
    ("two-documents", "_domain_policy.yaml", "x: 1\n---\n" + _POLICY, None, "unreadable:multiple_documents",
     None, "ADR-036 §2"),
    ("empty-second-document", "t.yaml", _TENANT + "---\n", None, "unreadable:multiple_documents",
     None, None),
    # ── top_level_not_mapping ──
    ("top-level-list", "t.yaml", "- t1\n", None, "unreadable:top_level_not_mapping",
     None, None),
    ("top-level-scalar", "_domain_policy.yaml", "policies\n", None, "unreadable:top_level_not_mapping",
     None,
     None),
    # ── tag_not_allowed ──
    ("local-tag", "t.yaml", "tenants:\n  t1:\n    a: !foo x\n", None, "unreadable:tag_not_allowed",
     None,
     None),
    ("python-tag", "t.yaml", "tenants:\n  t1:\n    a: !!python/tuple [1, 2]\n", None,
     "unreadable:tag_not_allowed",
     None, None),
    ("binary", "t.yaml", "tenants:\n  t1:\n    a: !!binary aGk=\n", None, "unreadable:tag_not_allowed",
     "stricter than today: PyYAML builds bytes",
     None),
    ("set", "t.yaml", "tenants:\n  t1: !!set {a, b}\n", None, "unreadable:tag_not_allowed",
     "stricter than today: PyYAML builds a set", None),
    ("omap", "t.yaml", "tenants:\n  t1: !!omap [{a: 1}]\n", None, "unreadable:tag_not_allowed",
     "stricter than today: PyYAML builds an ordered list of pairs", None),
    ("2759-f-set-domain", "_domain_policy.yaml",
     "domain_policies:\n  fin: !!set\n    tenants: [t1]\n", None, "unreadable:tag_not_allowed",
     "stricter than today: PyYAML builds a set (no mapping)",
     "#2759 F"),
    ("2759-k2-omap-collection-key", "_domain_policy.yaml", _POLICY + "x: !!omap [{? [a] : 1}]\n",
     None, "unreadable:tag_not_allowed",
     "stricter than today: PyYAML builds the omap", "#2759 K2"),
    ("2700-merge-tagged-key", "t1.yaml",
     "tenants:\n  t1:\n    _routing:\n      !!merge q: {receiver: {type: slack, api_url: "
     "\"https://hooks.slack.com/x\"}}\n", None, "unreadable:tag_not_allowed",
     "stricter than today: PyYAML merges through `!!merge q`; yaml.v3 reads a key q", "#2700"),
    ("merge-tagged-merge-key", "t.yaml", "x: {!!merge <<: {a: 1}}\n", None, "unreadable:tag_not_allowed",
     "stricter than today: both merge; an explicit !!merge is outside the closed tag list", None),
    ("plain-merge-as-value", "t.yaml", "tenants:\n  t1:\n    a: <<\n", None, "unreadable:tag_not_allowed",
     None, None),
    # ── non_specific_tag ──
    ("nonspecific-plain", "t.yaml", "tenants:\n  t1:\n    a: ! 123\n", None, "unreadable:non_specific_tag",
     "stricter than today: PyYAML reads `! 123` as a string, yaml.v3 as an int", "ADR-036 §8"),
    ("nonspecific-quoted", "t.yaml", "tenants:\n  t1:\n    a: ! \"x\"\n", None,
     "unreadable:non_specific_tag",
     "stricter than today: the vendored patch keeps `!`, upstream yaml.v3 drops it", "ADR-036 §8"),
    ("nonspecific-merge-key-alias", "t.yaml",
     "x-b: &b {cpu: 1}\ntenants:\n  t1: {! \"<<\": *b}\n", None, "unreadable:non_specific_tag",
     "stricter than today: PyYAML and the vendored yaml.v3 merge, upstream yaml.v3 reads a key \"<<\"",
     "ADR-036 §8"),
    ("nonspecific-slo-objective", "t.yaml",
     "tenants:\n  t1:\n    _custom_alerts:\n    - name: x\n      slo:\n        objective: ! \"99.9\"\n",
     None, "unreadable:non_specific_tag",
     "stricter than today: the exporter refuses the objective today, upstream yaml.v3 would accept it", "ADR-036 §8"),
    ("nonspecific-mapping", "t.yaml", "tenants: !\n  t1: {a: 1}\n", None, "unreadable:non_specific_tag",
     "stricter than today: `!` on a mapping", None),
    ("nonspecific-verbatim", "t.yaml", "tenants:\n  t1:\n    a: !<!> x\n", None,
     "unreadable:non_specific_tag",
     "stricter than today: `!<!>` is `!`", None),
    ("nonspecific-after-anchor", "t.yaml", "tenants:\n  t1:\n    a: &a ! x\n", None,
     "unreadable:non_specific_tag",
     "stricter than today: PyYAML reads the string \"x\"; `!` after an anchor is still `!`",
     "#2897 review"),
    # ── non_scalar_key ──
    ("sequence-key", "t.yaml", "tenants:\n  ? [t1]\n  : {a: 1}\n", None, "unreadable:non_scalar_key",
     None,
     None),
    ("mapping-key", "t.yaml", "tenants:\n  ? {c: d}\n  : {a: 1}\n", None, "unreadable:non_scalar_key",
     None,
     None),
    ("collection-key-beside-merge", "t.yaml", "? [a]\n: 1\n<<: {a: 1}\n", None, "unreadable:non_scalar_key",
     None, "yaml.v3 decode panics (unhashable key)"),
    ("2759-k1-merge-collection-key", "_domain_policy.yaml", _POLICY + "x: {<<: {? [a] : 1}}\n",
     None, "unreadable:non_scalar_key",
     None, "#2759 K1"),
    # ── null_key ──
    ("tilde-key", "t.yaml", "tenants:\n  ~: {a: 1}\n", None, "unreadable:null_key",
     "stricter than today: PyYAML reads a None key; `~` and \"~\" could not be told apart", None),
    ("null-key", "t.yaml", "tenants:\n  null: {a: 1}\n  t1: {a: 1}\n", None, "unreadable:null_key",
     "stricter than today: PyYAML reads a None key",
     None),
    ("empty-key", "t.yaml", "tenants:\n  ? \n  : {a: 1}\n", None, "unreadable:null_key",
     "stricter than today: PyYAML reads a None key", None),
    # ── duplicate_key ──
    ("duplicate-key", "t.yaml", "tenants:\n  t1: {a: 1}\n  t1: {a: 2}\n", None, "unreadable:duplicate_key",
     None, None),
    ("duplicate-alias-key", "t.yaml", "x: {&k a: 1, *k : 2}\n", None, "unreadable:duplicate_key",
     None,
     "ADR-036 §2"),
    ("duplicate-quoted-key", "t.yaml", "tenants:\n  t1: {a: 1, \"a\": 2}\n", None,
     "unreadable:duplicate_key",
     None, None),
    # ── decode_error ──
    ("int-tag-mismatch", "t.yaml", "tenants:\n  t1:\n    cpu: !!int abc\n", None, "unreadable:decode_error",
     None, "ADR-036 §2"),
    ("bool-tag-yes", "t.yaml", "tenants:\n  t1:\n    a: !!bool yes\n", None, "unreadable:decode_error",
     "stricter than today: PyYAML reads True; yaml.v3 refuses `!!bool yes`",
     "ADR-036 §2"),
    ("merge-scalar", "t.yaml", "tenants:\n  t1:\n    <<: 1\n", None, "unreadable:decode_error",
     None,
     "ADR-036 §2"),
    ("merge-anchored-scalar-sequence", "t.yaml", "<<: &y [a, b]\n", None, "unreadable:decode_error",
     None,
     None),
    ("2713-overridden-merge-source", "_domain_policy.yaml",
     _POLICY + "x:\n  a: 1\n  <<: {a: !!bool y}\n", None, "unreadable:decode_error",
     None, "#2713"),
    ("2713-overridden-merge-source-int", "t.yaml",
     "tenants:\n  t1:\n    cpu: 1\n    <<: {cpu: !!int x}\n", None, "unreadable:decode_error",
     None, "#2713"),
    ("2674-self-alias", "_domain_policy.yaml",
     "domain_policies:\n  fin:\n    tenants: [t1]\n    constraints:\n"
     "      forbidden_receiver_types: [slack]\n      require_critical_escalation: &x [*x]\n",
     None, "unreadable:decode_error",
     "stricter than today: PyYAML builds the recursive list; yaml.v3 refuses it", "#2674"),
    ("alias-expansion-limit", "t.yaml", _laughs(9), None, "unreadable:decode_error",
     "stricter than today: PyYAML shares the lists; yaml.v3 refuses more alias expansion than its limit", None),
    # ── #2759 rows the generator reads ──
    ("2759-c-flow-colon-end", "_domain_policy.yaml",
     "domain_policies:\n  fin:\n    tenants: [t1, a:]\n", None, "both_read",
     None, "#2759 C"),
    ("2759-q-receiver-mapping-item", "_domain_policy.yaml",
     "domain_policies:\n  fin:\n    tenants: [t1]\n    constraints:\n"
     "      forbidden_receiver_types: [{a: 1}, [slack]]\n", None, "both_read",
     None, "#2759 Q"),
    # ── PyYAML-only refusals: parse-tree reads them as yaml.v3 does ──
    ("tab-after-colon", "t.yaml", "tenants:\n  t1:\n    cpu:\t80\n", None, "pyyaml_only",
     "yaml.v3 reads cpu = 80 (!!int); PyYAML refuses the TAB separator", "ADR-036 §2"),
    ("tab-in-flow-policy", "_domain_policy.yaml",
     "domain_policies: {\n\tfin: {\n\t\ttenants: [t1],\n\t\tconstraints: "
     "{forbidden_receiver_types: [slack]}}}\n", None, "pyyaml_only",
     "yaml.v3 reads the policy; PyYAML refuses TAB as white space in flow context", "ADR-036 §2"),
    ("2759-a-depth-600", "_domain_policy.yaml", _POLICY + "x: " + "[" * 600 + "]" * 600 + "\n",
     None, "pyyaml_only",
     "yaml.v3 reads the 600-deep list (its limit is 10000); PyYAML raises RecursionError", "#2759 A"),
    ("duplicate-anchor", "t.yaml", "x: {b: &x 1, c: &x 2}\n", None, "pyyaml_only",
     "yaml.v3 reads b = 1, c = 2 (a later anchor of the same name replaces the first); PyYAML "
     "refuses the second anchor", "#2897 review"),
    ("invalid-calendar-timestamp", "t.yaml", "a: 2001-13-45\n", None, "pyyaml_only",
     "yaml.v3 reads the string \"2001-13-45\" (!!str); PyYAML resolves !!timestamp and cannot "
     "build the date", "#2897 review"),
    ("flow-plain-ending-colon", "t.yaml", "a: {b: 1:}\n", None, "pyyaml_only",
     "yaml.v3 reads b = the string \"1:\"; PyYAML refuses the scalar", "#2897 review"),
    ("flow-merge-key-colon", "t.yaml", "x: {<<:, d: 1}\n", None, "pyyaml_only",
     "yaml.v3 reads a literal key \"<<:\" (null) beside d = 1, no merge; measured: it changes no "
     "routing or policy outcome in today's generator", "#2897 review"),
    ("single-quote-escape", "t.yaml", "h: \"q\\'\"\n", None, "pyyaml_only",
     "yaml.v3 reads the string \"q'\" (`\\'` escapes a single quote); PyYAML refuses the "
     "escape", "#2897 review"),
    ("block-header-comment", "t.yaml", "f: |#\n  text\n", None, "pyyaml_only",
     "yaml.v3 reads the string \"text\\n\"; PyYAML refuses a comment right after the `|` "
     "header", "#2897 review"),
    ("plain-equals", "_domain_policy.yaml", _POLICY + "x: =\n", None, "pyyaml_only",
     "yaml.v3 reads the string \"=\"; PyYAML resolves YAML 1.1's !!value tag, which it "
     "cannot build", None),
)

def generator_verdict(data: bytes, fname: str) -> tuple[str, str]:
    """("rejected" | "read", the drop's reason) — today's generator reader on
    `data` written as `fname`, alone in a conf.d."""
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / fname).write_bytes(data)
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            res = _parse_config_files(d)
    for f, reason in res.get("files_skipped", []):
        if f == fname and not reason.startswith(_NOT_A_YAML_VERDICT):
            # The temporary directory is no part of the verdict.
            return "rejected", " ".join(reason.replace(str(Path(d)) + os.sep, "").split())
    return "read", ""


def _rows() -> list[dict]:
    out = []
    for name, fname, doc, encoding, category, note, source in _ROWS:
        verdict, reason = generator_verdict(_encode(doc, encoding), fname)
        row = {"name": name, "file": fname, "doc": doc, "id": row_id(doc, encoding),
               "category": category, "generator": verdict}
        if encoding is not None:
            row["encoding"] = encoding
        if reason:
            row["generator_reason"] = reason[:200]
        if note is not None:
            row["note"] = note
        if source is not None:
            row["source"] = source
        out.append(row)
    return out


def _merge_key_rows() -> list[dict]:
    rows = json.loads(MERGE_KEY_CORPUS_PATH.read_text(encoding="utf-8"))["rows"]
    out = []
    for r in rows:
        verdict, reason = generator_verdict(_encode(r["doc"], r.get("encoding")),
                                            "_domain_policy.yaml")
        row = {"id": r["id"], "generator": verdict}
        if reason:
            row["generator_reason"] = reason[:200]
        out.append(row)
    return out


def _render() -> str:
    comment = [
        "TODAY's route generator reader (_grar_parse._parse_config_files, PyYAML) on",
        "each file, alone in a conf.d: `generator` is \"rejected\" when it drops the",
        "file whole (which refuses `generate-routes --validate --strict`), else",
        "\"read\". GENERATED by tests/shared/test_parse_tree_corpus.py — regenerate with",
        "REGEN_PARSE_TREE_CORPUS=1; never edit by hand. `rows`: hand-picked files,",
        "`doc` written as UTF-8 or in `encoding`; `category`: unreadable:<kind>",
        "(parse-tree refuses it with that kind), pyyaml_only (the generator refuses",
        "it today, parse-tree reads it) or both_read; `note`: what changes. Exactly",
        "the kinds in `closed_list` are refused (ADR-036 §2, owner option B).",
        "`merge_key_rows`: every row of merge_key_policy_corpus.json by `id` (the",
        "file is that corpus's `doc`, as `_domain_policy.yaml`); their categories",
        "are in parse_tree_merge_key_categories.json. `id`: the first 16 hex",
        "digits of the sha256 of the file. Go half:",
        "components/threshold-exporter/app/internal/parsetree/corpus_test.go.",
    ]
    doc = {"_comment": comment, "pyyaml": yaml.__version__, "closed_list": list(CLOSED_LIST),
           "text_kinds": list(TEXT_KINDS)}
    head = json.dumps(doc, ensure_ascii=False, indent=2)[:-2]

    def body(rows: list[dict]) -> str:
        text = ",\n".join("    " + json.dumps(r, ensure_ascii=False, sort_keys=True) for r in rows)
        for ch in ("\u2028", "\u2029", "\x85", "\ufeff"):
            text = text.replace(ch, "\\u%04x" % ord(ch))
        return text

    return (head + ",\n  \"rows\": [\n" + body(_rows()) + "\n  ],\n  \"merge_key_rows\": [\n"
            + body(_merge_key_rows()) + "\n  ]\n}\n")


def test_corpus_is_what_the_generator_reads() -> None:
    fresh = _render()
    if os.environ.get("REGEN_PARSE_TREE_CORPUS"):
        CORPUS_PATH.write_text(fresh, encoding="utf-8")
    on_disk = CORPUS_PATH.read_text(encoding="utf-8")
    assert on_disk == fresh, (
        "parse_tree_corpus.json is not what today's generator reader says; regenerate with "
        "REGEN_PARSE_TREE_CORPUS=1 and review the diff")


def test_every_text_kind_has_a_row_and_rows_are_unique() -> None:
    corpus = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    cats = {r["category"] for r in corpus["rows"]}
    for kind in TEXT_KINDS:
        assert f"unreadable:{kind}" in cats, kind
    assert set(TEXT_KINDS) <= set(CLOSED_LIST)
    names = [r["name"] for r in corpus["rows"]]
    assert len(set(names)) == len(names)
    ids = [r["id"] for r in corpus["rows"]]
    assert len(set(ids)) == len(ids), "two rows are the same file"
    for issue in ("#2759", "#2700", "#2713", "#2674"):
        assert any((r.get("source") or "").startswith(issue) for r in corpus["rows"]), issue


def _check_category(where: str, category: str, generator: str) -> None:
    if category == "pyyaml_only":
        assert generator == "rejected", f"{where}: pyyaml_only, but today's generator reads it"
    elif category == "both_read":
        assert generator == "read", f"{where}: both_read, but today's generator refuses it"
    else:
        kind = category.removeprefix("unreadable:")
        assert category.startswith("unreadable:") and kind in CLOSED_LIST, f"{where}: {category}"


def test_hand_rows_categories_agree_with_the_generator() -> None:
    """Every row's written category against today's frozen verdict; a row
    parse-tree refuses and the generator reads says so in its note."""
    corpus = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    for r in corpus["rows"]:
        _check_category(r["name"], r["category"], r["generator"])
        stricter = r["category"].startswith("unreadable:") and r["generator"] == "read"
        note = r.get("note", "")
        assert stricter == note.startswith(STRICTER), (r["name"], note)
        assert bool(note) == (stricter or r["category"] == "pyyaml_only"), (r["name"], note)
    assert {r["category"] for r in corpus["rows"]} >= {"pyyaml_only", "both_read"}


def test_merge_key_rows_are_that_corpus_and_categorised() -> None:
    corpus = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    mk = json.loads(MERGE_KEY_CORPUS_PATH.read_text(encoding="utf-8"))["rows"]
    assert [r["id"] for r in corpus["merge_key_rows"]] == [r["id"] for r in mk]
    cats = json.loads(MERGE_KEY_CATEGORIES_PATH.read_text(encoding="utf-8"))["categories"]
    assert set(cats) == {r["id"] for r in mk}, "a merge_key row has no category, or a stale one"
    for r in corpus["merge_key_rows"]:
        _check_category("merge_key row " + r["id"], cats[r["id"]], r["generator"])


# ── An oracle that does not come from parse-tree ──
#
# The merge-key categories are a snapshot of parse-tree's own output, so a
# parse-tree that stopped refusing something would bless its own regression
# on UPDATE_PARSE_TREE_CATEGORIES=1 (a review did exactly that: `&a ! x` read
# once the `!` scan gave up at the anchor). This oracle reads every row
# parse-tree READS (both_read, pyyaml_only) with PyYAML's parser and composer
# — not its constructor — and requires none of the closed list's
# PyYAML-visible shapes there. A row PyYAML cannot compose is skipped: that is
# what pyyaml_only means.
_Y = "tag:yaml.org,2002:"
_SCALAR_TAGS = {_Y + t for t in ("str", "int", "float", "bool", "null", "timestamp")}
# What PyYAML's YAML 1.1 resolver gives a plain `=` and a plain `<<`; yaml.v3
# reads a string and a merge key (the `x: =` / `<<` rows).
_IMPLICIT_ONLY = {_Y + "value", _Y + "merge"}
# A cap per row, so a row of deep aliases cannot stall the walk. Reaching it
# is a failure of the row, never a silent pass over a truncated walk.
_ORACLE_NODES = 200_000

# The merge-key snapshot's categories, counted and pinned: any movement, in
# either direction (a row parse-tree used to refuse is read now, or the
# reverse, or one refused for another kind), needs an explicit edit here.
MERGE_KEY_HISTOGRAM = {
    "both_read": 127,
    "pyyaml_only": 119,
    "unreadable:bom_not_at_start": 3,
    "unreadable:decode_error": 83,
    "unreadable:duplicate_key": 40,
    "unreadable:invalid_utf8": 7,
    "unreadable:multiple_documents": 7,
    "unreadable:non_scalar_key": 2,
    "unreadable:non_specific_tag": 233,
    "unreadable:null_key": 128,
    "unreadable:syntax_error": 82,
    "unreadable:tag_not_allowed": 755,
}


def _oracle_problems(text: str) -> "list[str] | None":
    """The closed-list shapes PyYAML sees in `text` (None: it cannot compose)."""
    problems = []
    try:
        events = list(yaml.parse(text, Loader=yaml.SafeLoader))
        docs = list(yaml.compose_all(text, Loader=yaml.SafeLoader))
    except (yaml.YAMLError, RecursionError):
        return None
    for ev in events:
        tag = getattr(ev, "tag", None)
        if tag is None:
            continue
        if tag == "!":
            problems.append("non-specific tag `!`")
        elif isinstance(ev, yaml.ScalarEvent) and tag not in _SCALAR_TAGS:
            problems.append(f"scalar tag {tag}")
        elif isinstance(ev, yaml.MappingStartEvent) and tag != _Y + "map":
            problems.append(f"mapping tag {tag}")
        elif isinstance(ev, yaml.SequenceStartEvent) and tag != _Y + "seq":
            problems.append(f"sequence tag {tag}")
    if len(docs) > 1:
        problems.append("a second document")
    seen: set[int] = set()
    stack = list(docs)
    while stack:
        if len(seen) >= _ORACLE_NODES:
            problems.append(f"oracle node cap ({_ORACLE_NODES}) reached; the row was not fully checked")
            break
        node = stack.pop()
        if node is None or id(node) in seen:
            continue
        seen.add(id(node))
        if isinstance(node, yaml.ScalarNode):
            if node.tag not in _SCALAR_TAGS | _IMPLICIT_ONLY:
                problems.append(f"scalar resolves to {node.tag}")
        elif isinstance(node, yaml.SequenceNode):
            stack.extend(node.value)
        elif isinstance(node, yaml.MappingNode):
            keys: set[str] = set()
            for k, v in node.value:
                if not isinstance(k, yaml.ScalarNode):
                    problems.append("a non-scalar key")
                elif k.tag == _Y + "null":
                    problems.append("a null key")
                elif k.value in keys:
                    problems.append(f"duplicate key {k.value!r}")
                else:
                    keys.add(k.value)
                stack.extend((k, v))
    return problems


def _read_rows():
    """(where, text) of every row parse-tree reads."""
    corpus = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    for r in corpus["rows"]:
        if r["category"] in ("both_read", "pyyaml_only"):
            yield "row " + r["name"], _encode(r["doc"], r.get("encoding")).decode("utf-8")
    cats = json.loads(MERGE_KEY_CATEGORIES_PATH.read_text(encoding="utf-8"))["categories"]
    for r in json.loads(MERGE_KEY_CORPUS_PATH.read_text(encoding="utf-8"))["rows"]:
        if cats[r["id"]] in ("both_read", "pyyaml_only"):
            yield "merge_key row " + r["id"], _encode(r["doc"], r.get("encoding")).decode("utf-8")


def test_rows_parse_tree_reads_hold_no_closed_list_shape() -> None:
    checked = skipped = 0
    bad = []
    for where, text in _read_rows():
        problems = _oracle_problems(text)
        if problems is None:
            skipped += 1
            continue
        checked += 1
        if problems:
            bad.append(f"{where}: {sorted(set(problems))}")
    assert not bad, "parse-tree reads rows with a closed-list shape:\n" + "\n".join(bad[:20])
    assert checked > 100, (checked, skipped)


def test_oracle_bites() -> None:
    """The oracle sees each shape it is for (anti-vacuity)."""
    for text in ("a: ! x\n", "a: &a ! x\n", "a: !!set {b}\n", "? [a]\n: 1\n", "~: 1\n",
                 "a: 1\na: 2\n", "&k a: 1\n*k : 2\n", "a: 1\n---\nb: 2\n", "a: !!binary aGk=\n"):
        assert _oracle_problems(text), text
    assert _oracle_problems("a: =\nb: {<<: {c: 1}}\n") == []


def test_merge_key_histogram_is_pinned() -> None:
    cats = json.loads(MERGE_KEY_CATEGORIES_PATH.read_text(encoding="utf-8"))["categories"]
    got: dict[str, int] = {}
    for c in cats.values():
        got[c] = got.get(c, 0) + 1
    assert got == MERGE_KEY_HISTOGRAM, (
        "the merge_key categories moved — review the snapshot diff, then edit "
        f"MERGE_KEY_HISTOGRAM: got {dict(sorted(got.items()))}")


def test_oracle_cap_fails_loudly(monkeypatch) -> None:
    monkeypatch.setattr(sys.modules[__name__], "_ORACLE_NODES", 3)
    assert any("cap" in p for p in _oracle_problems("a: [1, 2, 3, 4]\n"))



# The ADR-036 §9 window before step 4 (#2906): files only PyYAML refuses,
# in a tenant file of a tree with no routing key. da-guard's default run
# passes such a tree (exit 0, nothing undetermined), so CI must still run the
# generator. The window is defined by the corpus, not by a hand-copied list:
# category pyyaml_only and a file name not starting with `_` (an `_` file the
# generator reads is undetermined evidence by name). The Go half,
# TestPyYAMLOnlyReject_WindowBeforeStep4 in
# components/threshold-exporter/app/cmd/da-guard/pyyaml_only_window_test.go,
# MEASURES the rows da-guard passes and asserts they are exactly this set;
# this half asserts the generator refuses each of them. Once step 4 makes the
# generator read parse-tree's JSON, both halves flip or go.
_WINDOW_DEFAULTS = "defaults:\n  mysql_connections: 80\n"
_WINDOW_TENANT = "tenants:\n  tx:\n    mysql_connections: \"50\"\n"
_GENERATE_ROUTES = REPO_ROOT / "scripts" / "tools" / "ops" / "generate_alertmanager_routes.py"


def in_pyyaml_only_window(row: dict) -> bool:
    """The corpus's definition of the window (same predicate as the Go half)."""
    return row["category"] == "pyyaml_only" and not row["file"].startswith("_")


def _generate_routes_strict(extra: dict[str, bytes] | None) -> tuple[int, str]:
    with tempfile.TemporaryDirectory() as d:
        confd = Path(d) / "conf.d"
        confd.mkdir()
        (confd / "_defaults.yaml").write_text(_WINDOW_DEFAULTS, encoding="utf-8")
        (confd / "tenant-x.yaml").write_text(_WINDOW_TENANT, encoding="utf-8")
        for name, data in (extra or {}).items():
            (confd / name).write_bytes(data)
        p = subprocess.run([sys.executable, str(_GENERATE_ROUTES), "--config-dir", str(confd),
                            "--validate", "--strict"], capture_output=True, text=True,
                           encoding="utf-8", timeout=120, check=False)
        return p.returncode, p.stdout + p.stderr


def test_pyyaml_only_window_is_refused_by_generate_routes() -> None:
    rows = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))["rows"]
    window = [r for r in rows if in_pyyaml_only_window(r)]
    # Known members, so an emptied or renamed window cannot pass vacuously.
    assert {"duplicate-anchor", "tab-after-colon"} <= {r["name"] for r in window}
    code, out = _generate_routes_strict(None)
    assert code == 0, f"control tree (no corpus file) refused, rc {code}:\n{out}"
    for r in window:
        assert r["generator"] == "rejected", r["name"]
        assert r["file"] not in ("_defaults.yaml", "tenant-x.yaml"), r["name"]
        code, out = _generate_routes_strict({r["file"]: _encode(r["doc"], r.get("encoding"))})
        assert code != 0, f"{r['name']}: generate-routes --validate --strict passed (rc 0):\n{out}"
        assert r["file"] in out, f"{r['name']}: the refusal does not name {r['file']}:\n{out}"
