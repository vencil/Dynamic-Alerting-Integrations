"""Python half of tests/shared/parse_tree_corpus.json (#2897, ADR-036 §2).

ADR-036 moves every conf.d read onto `da-guard parse-tree`, and one of its
non-negotiable acceptance conditions is that the route generator, reading
that JSON, is not looser than it is today: a file the generator refuses
today under `--strict` must be one parse-tree calls unreadable, unless the
row says why not.

This test freezes TODAY's verdict. For every row it writes the file alone
into an empty conf.d and runs the generator's own reader over it —
`_grar_parse._parse_config_files`, the function `generate-routes
--validate --strict` reads the tree with (PyYAML through
`_lib_io.strict_load_exporter_keys`, `open(..., encoding="utf-8")`). A file
it drops whole is booked in `files_skipped`, and every such drop refuses
the run (`tenant_file_errors` → the #1460 refusal; a policy file's
`policy_file_errors` → a `--strict` ERROR). `generator` is "rejected" for
such a row, else "read". One drop is not a YAML verdict and does not count:
"'tenants' must be a mapping" (the file parses; its `tenants:` has the
wrong shape — a question for the semantic layer, not for parse-tree).

Rows:
- `rows`: hand-picked files — one or more per unreadable kind parse-tree
  can give a text (read_failure and not_regular_file are about the path,
  not the text; da-guard's own tests cover them), the ADR §2 example, values
  whose meaning changes when yaml.v3 reads them (yes / on / off, 0777, a
  `010` key), the examples of #2759, #2700, #2713 and #2674, and the known
  exceptions. Each carries `expect`: the kind parse-tree gives it, or
  "read" — the Go test pins every row to it.
- `merge_key_rows`: every row of merge_key_policy_corpus.json, by `id`
  (its file is not copied here), with the same frozen verdict.

`exception` is the written reason a row the generator rejects is still
read by parse-tree; the Go test fails on a rejected row without one, and on
an exception parse-tree no longer needs. The Go half:
components/threshold-exporter/app/internal/parsetree/corpus_test.go.

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

# The kinds parse-tree gives a text (internal/parsetree Kinds, minus the two
# about the path). The Go test asserts this list is that set.
TEXT_KINDS = ("invalid_utf8", "bom_not_at_start", "syntax_error", "multiple_documents",
              "non_specific_tag", "tag_not_allowed", "non_scalar_key", "null_key", "duplicate_key",
              "decode_error", "top_level_not_mapping")

# The one whole-file drop that is not about the YAML (see the docstring).
_NOT_A_YAML_VERDICT = "'tenants' must be a mapping"

# Why each class of rejected-but-read row is read. Keyed by an exception
# name a row (or a merge_key_policy_corpus.json `shape`) refers to.
EXCEPTIONS = {
    "tab-separator": (
        "a TAB where YAML allows white space (after a `:` or another indicator, inside flow "
        "context, at a line's end): yaml.v3 reads the same values as with a space; PyYAML "
        "takes a TAB as a separator only inside quotes, comments and block scalars and refuses "
        "the file. The file is read, and its policy enforced, from then on — not looser."),
    "yaml11-value-tag": (
        "a plain `=`: PyYAML resolves it to YAML 1.1's !!value tag, which it cannot construct, "
        "and drops the file; yaml.v3 reads the string \"=\" (YAML 1.2 has no such tag). The "
        "file is read, and its policy enforced, from then on — not looser."),
    "deep-nesting": (
        "nesting about 600 levels deep: PyYAML's recursive constructor raises RecursionError "
        "and the generator drops the file; yaml.v3 (limit 10000) reads it. The file is read, "
        "and its policy enforced, from then on — not looser."),
}

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


# (name, file, doc, encoding, expect, exception, source)
_ROWS: tuple[tuple, ...] = (
    # ── ADR-036 §2 and the values whose meaning changes ──
    ("adr036-s2-example", "t010.yaml", _SECTION2, None, "read", None, "ADR-036 §2"),
    ("bool-words-are-strings", "t.yaml",
     "tenants:\n  t1:\n    _routing:\n      receiver: {type: slack, api_url: \"https://x\", "
     "send_resolved: on}\n    _severity_dedup: off\n    a: no\n    b: y\n", None, "read", None,
     "ADR-036 §8"),
    ("octal-value-and-keys", "t.yaml", "tenants:\n  010:\n    cpu: 0777\n  0777: {cpu: 1e3}\n",
     None, "read", None, "ADR-036 §2"),
    ("empty-file", "t.yaml", "", None, "read", None, None),
    ("comments-only", "t.yaml", "# nothing\n", None, "read", None, None),
    ("null-document", "t.yaml", "~\n", None, "read", None, None),
    ("bom-at-start", "t.yaml", "\ufeff" + _TENANT, None, "read", None, None),
    ("quoted-merge-key", "t.yaml", "x: {\"<<\": {a: 1}, b: 2}\n" + _TENANT, None, "read", None,
     "ADR-036 §2"),
    ("merge-expanded", "t.yaml",
     "x-base: &b {type: webhook, url: \"https://x\"}\ntenants:\n  t1:\n    _routing:\n"
     "      receiver: {<<: *b, type: slack}\n", None, "read", None, None),
    # ── invalid_utf8 ──
    ("latin-1", "t.yaml", "# é\n" + _TENANT, "latin-1", "invalid_utf8", None, None),
    ("utf-16-bom", "_domain_policy.yaml", _POLICY, "utf-16-le-bom", "invalid_utf8", None, None),
    # ── bom_not_at_start ──
    ("bom-in-comment", "t.yaml", "# \ufeff\n" + _TENANT, None, "bom_not_at_start", None, None),
    ("bom-in-value", "t.yaml", "tenants:\n  t1:\n    a: \"x\ufeff\"\n", None, "bom_not_at_start",
     None, None),
    ("2759-j-two-boms", "_domain_policy.yaml", "\ufeff\ufeff" + _POLICY, None, "bom_not_at_start",
     None, "#2759 J"),
    ("2759-n-bom-510", "_domain_policy.yaml", _bom_at(510), None, "bom_not_at_start", None,
     "#2759 N"),
    ("2759-n-bom-511", "_domain_policy.yaml", _bom_at(511), None, "bom_not_at_start", None,
     "#2759 N"),
    # ── syntax_error ──
    ("unclosed-flow", "t.yaml", "tenants: {t1: [\n", None, "syntax_error", None, None),
    ("2759-b-flow-colon", "_domain_policy.yaml",
     "domain_policies: {fin: {tenants:[t1]}}\n", None, "syntax_error", None, "#2759 B"),
    ("2759-d-surrogate", "_domain_policy.yaml",
     "domain_policies:\n  fin:\n    tenants: [\"\\ud800\"]\n", None, "syntax_error", None,
     "#2759 D"),
    ("2759-h-yaml-directive", "_domain_policy.yaml", "%YAML 1.2\n---\n" + _POLICY, None,
     "syntax_error", None, "#2759 H"),
    ("escaped-slash", "t.yaml", "tenants:\n  t1:\n    a: \"x\\/y\"\n", None, "syntax_error", None,
     "ADR-036 table"),
    ("tab-indentation", "t.yaml", "tenants:\n\tt1:\n\t\tcpu: 1\n", None, "syntax_error", None,
     None),
    # ── multiple_documents ──
    ("two-documents", "_domain_policy.yaml", "x: 1\n---\n" + _POLICY, None, "multiple_documents",
     None, "ADR-036 §2"),
    ("empty-second-document", "t.yaml", _TENANT + "---\n", None, "multiple_documents", None, None),
    # ── top_level_not_mapping ──
    ("top-level-list", "t.yaml", "- t1\n", None, "top_level_not_mapping", None, None),
    ("top-level-scalar", "_domain_policy.yaml", "policies\n", None, "top_level_not_mapping", None,
     None),
    # ── tag_not_allowed ──
    ("local-tag", "t.yaml", "tenants:\n  t1:\n    a: !foo x\n", None, "tag_not_allowed", None,
     None),
    ("python-tag", "t.yaml", "tenants:\n  t1:\n    a: !!python/tuple [1, 2]\n", None,
     "tag_not_allowed", None, None),
    ("binary", "t.yaml", "tenants:\n  t1:\n    a: !!binary aGk=\n", None, "tag_not_allowed", None,
     None),
    ("set", "t.yaml", "tenants:\n  t1: !!set {a, b}\n", None, "tag_not_allowed", None, None),
    ("omap", "t.yaml", "tenants:\n  t1: !!omap [{a: 1}]\n", None, "tag_not_allowed", None, None),
    ("2759-f-set-domain", "_domain_policy.yaml",
     "domain_policies:\n  fin: !!set\n    tenants: [t1]\n", None, "tag_not_allowed", None,
     "#2759 F"),
    ("2759-k2-omap-collection-key", "_domain_policy.yaml", _POLICY + "x: !!omap [{? [a] : 1}]\n",
     None, "tag_not_allowed", None, "#2759 K2"),
    ("2700-merge-tagged-key", "t1.yaml",
     "tenants:\n  t1:\n    _routing:\n      !!merge q: {receiver: {type: slack, api_url: "
     "\"https://hooks.slack.com/x\"}}\n", None, "tag_not_allowed", None, "#2700"),
    ("merge-tagged-merge-key", "t.yaml", "x: {!!merge <<: {a: 1}}\n", None, "tag_not_allowed",
     None, None),
    ("plain-merge-as-value", "t.yaml", "tenants:\n  t1:\n    a: <<\n", None, "tag_not_allowed",
     None, None),
    # ── non_specific_tag ──
    ("nonspecific-plain", "t.yaml", "tenants:\n  t1:\n    a: ! 123\n", None, "non_specific_tag",
     None, "ADR-036 §8"),
    ("nonspecific-quoted", "t.yaml", "tenants:\n  t1:\n    a: ! \"x\"\n", None,
     "non_specific_tag", None, "ADR-036 §8"),
    ("nonspecific-merge-key-alias", "t.yaml",
     "x-b: &b {cpu: 1}\ntenants:\n  t1: {! \"<<\": *b}\n", None, "non_specific_tag", None,
     "ADR-036 §8"),
    ("nonspecific-slo-objective", "t.yaml",
     "tenants:\n  t1:\n    _custom_alerts:\n    - name: x\n      slo:\n        objective: ! \"99.9\"\n",
     None, "non_specific_tag", None, "ADR-036 §8"),
    ("nonspecific-mapping", "t.yaml", "tenants: !\n  t1: {a: 1}\n", None, "non_specific_tag",
     None, None),
    ("nonspecific-verbatim", "t.yaml", "tenants:\n  t1:\n    a: !<!> x\n", None,
     "non_specific_tag", None, None),
    # ── non_scalar_key ──
    ("sequence-key", "t.yaml", "tenants:\n  ? [t1]\n  : {a: 1}\n", None, "non_scalar_key", None,
     None),
    ("mapping-key", "t.yaml", "tenants:\n  ? {c: d}\n  : {a: 1}\n", None, "non_scalar_key", None,
     None),
    ("collection-key-beside-merge", "t.yaml", "? [a]\n: 1\n<<: {a: 1}\n", None, "non_scalar_key",
     None, "yaml.v3 decode panics (unhashable key)"),
    ("2759-k1-merge-collection-key", "_domain_policy.yaml", _POLICY + "x: {<<: {? [a] : 1}}\n",
     None, "non_scalar_key", None, "#2759 K1"),
    # ── null_key ──
    ("tilde-key", "t.yaml", "tenants:\n  ~: {a: 1}\n", None, "null_key", None, None),
    ("null-key", "t.yaml", "tenants:\n  null: {a: 1}\n  t1: {a: 1}\n", None, "null_key", None,
     None),
    ("empty-key", "t.yaml", "tenants:\n  ? \n  : {a: 1}\n", None, "null_key", None, None),
    # ── duplicate_key ──
    ("duplicate-key", "t.yaml", "tenants:\n  t1: {a: 1}\n  t1: {a: 2}\n", None, "duplicate_key",
     None, None),
    ("duplicate-alias-key", "t.yaml", "x: {&k a: 1, *k : 2}\n", None, "duplicate_key", None,
     "ADR-036 §2"),
    ("duplicate-quoted-key", "t.yaml", "tenants:\n  t1: {a: 1, \"a\": 2}\n", None,
     "duplicate_key", None, None),
    # ── decode_error ──
    ("int-tag-mismatch", "t.yaml", "tenants:\n  t1:\n    cpu: !!int abc\n", None, "decode_error",
     None, "ADR-036 §2"),
    ("bool-tag-yes", "t.yaml", "tenants:\n  t1:\n    a: !!bool yes\n", None, "decode_error", None,
     "ADR-036 §2"),
    ("merge-scalar", "t.yaml", "tenants:\n  t1:\n    <<: 1\n", None, "decode_error", None,
     "ADR-036 §2"),
    ("merge-anchored-scalar-sequence", "t.yaml", "<<: &y [a, b]\n", None, "decode_error", None,
     None),
    ("2713-overridden-merge-source", "_domain_policy.yaml",
     _POLICY + "x:\n  a: 1\n  <<: {a: !!bool y}\n", None, "decode_error", None, "#2713"),
    ("2713-overridden-merge-source-int", "t.yaml",
     "tenants:\n  t1:\n    cpu: 1\n    <<: {cpu: !!int x}\n", None, "decode_error", None, "#2713"),
    ("2674-self-alias", "_domain_policy.yaml",
     "domain_policies:\n  fin:\n    tenants: [t1]\n    constraints:\n"
     "      forbidden_receiver_types: [slack]\n      require_critical_escalation: &x [*x]\n",
     None, "decode_error", None, "#2674"),
    ("alias-expansion-limit", "t.yaml", _laughs(9), None, "decode_error", None, None),
    # ── #2759 rows the generator reads ──
    ("2759-c-flow-colon-end", "_domain_policy.yaml",
     "domain_policies:\n  fin:\n    tenants: [t1, a:]\n", None, "read", None, "#2759 C"),
    ("2759-q-receiver-mapping-item", "_domain_policy.yaml",
     "domain_policies:\n  fin:\n    tenants: [t1]\n    constraints:\n"
     "      forbidden_receiver_types: [{a: 1}, [slack]]\n", None, "read", None, "#2759 Q"),
    # ── known exceptions ──
    ("tab-after-colon", "t.yaml", "tenants:\n  t1:\n    cpu:\t80\n", None, "read",
     "tab-separator", "ADR-036 §2"),
    ("tab-in-flow-policy", "_domain_policy.yaml",
     "domain_policies: {\n\tfin: {\n\t\ttenants: [t1],\n\t\tconstraints: "
     "{forbidden_receiver_types: [slack]}}}\n", None, "read", "tab-separator", "ADR-036 §2"),
    ("2759-a-depth-600", "_domain_policy.yaml", _POLICY + "x: " + "[" * 600 + "]" * 600 + "\n",
     None, "read", "deep-nesting", "#2759 A"),
)

# A merge_key_policy_corpus.json row the generator rejects carries an
# exception when the generator's own reason is the excused one (a TAB, the
# !!value tag, the recursion limit) — never by shape alone, so a row of the
# same shape rejected for another reason stays held to "unreadable".
MERGE_KEY_EXCEPTIONS = (
    ("found character '\\t'", "tab-separator"),
    ("but found '\\t'", "tab-separator"),
    ("tag 'tag:yaml.org,2002:value'", "yaml11-value-tag"),
    ("RecursionError", "deep-nesting"),
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
    for name, fname, doc, encoding, expect, exception, source in _ROWS:
        verdict, reason = generator_verdict(_encode(doc, encoding), fname)
        row = {"name": name, "file": fname, "doc": doc, "id": row_id(doc, encoding),
               "generator": verdict, "expect": expect}
        if encoding is not None:
            row["encoding"] = encoding
        if reason:
            row["generator_reason"] = reason[:200]
        if exception is not None:
            row["exception"] = exception
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
        for needle, exception in MERGE_KEY_EXCEPTIONS:
            if verdict == "rejected" and needle in reason:
                row["exception"] = exception
                break
        out.append(row)
    return out


def _render() -> str:
    comment = [
        "TODAY's route generator reader (_grar_parse._parse_config_files, PyYAML) on",
        "each file, alone in a conf.d: `generator` is \"rejected\" when it drops the",
        "file whole (which refuses `generate-routes --validate --strict`), else",
        "\"read\". GENERATED by tests/shared/test_parse_tree_corpus.py — regenerate with",
        "REGEN_PARSE_TREE_CORPUS=1; never edit by hand. `rows`: hand-picked files,",
        "`doc` written as UTF-8 or in `encoding`; `expect`: the kind `da-guard",
        "parse-tree` gives the file, or \"read\". `merge_key_rows`: every row of",
        "merge_key_policy_corpus.json by `id` (the file is that corpus's `doc`, as",
        "`_domain_policy.yaml`). `exception`: a key of `exceptions`, the written",
        "reason a rejected row is still read by parse-tree (ADR-036 §2). `id`: the",
        "first 16 hex digits of the sha256 of the file. Go half:",
        "components/threshold-exporter/app/internal/parsetree/corpus_test.go.",
    ]
    doc = {"_comment": comment, "pyyaml": yaml.__version__, "text_kinds": list(TEXT_KINDS),
           "exceptions": EXCEPTIONS}
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
    expects = {r["expect"] for r in corpus["rows"]}
    for kind in TEXT_KINDS:
        assert kind in expects, kind
    names = [r["name"] for r in corpus["rows"]]
    assert len(set(names)) == len(names)
    ids = [r["id"] for r in corpus["rows"]]
    assert len(set(ids)) == len(ids), "two rows are the same file"
    for issue in ("#2759", "#2700", "#2713", "#2674"):
        assert any((r.get("source") or "").startswith(issue) for r in corpus["rows"]), issue


def test_exceptions_are_written_and_used() -> None:
    corpus = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    used = {r["exception"] for r in corpus["rows"] + corpus["merge_key_rows"] if "exception" in r}
    assert used == set(EXCEPTIONS), (used, set(EXCEPTIONS))
    for r in corpus["rows"] + corpus["merge_key_rows"]:
        if "exception" in r:
            assert r["generator"] == "rejected", r


def test_merge_key_rows_are_that_corpus() -> None:
    corpus = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    mk = json.loads(MERGE_KEY_CORPUS_PATH.read_text(encoding="utf-8"))["rows"]
    assert [r["id"] for r in corpus["merge_key_rows"]] == [r["id"] for r in mk]
