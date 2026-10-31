"""Coverage tripwire: rule-shaped YAML outside the rule-source declaration (#3438).

⛔ An ALARM, not a classifier. Which files hold rule files is declared by
path (`_lib_consumer_read.RULE_SOURCES` / `CHART_VALUES_RULES`) and read by
`_lib_consumer_read.rule_files_in`; nothing here decides what is read. This
module answers one question about every OTHER tracked YAML under the shipped
roots — "does anything in it look like Prometheus rules?" — and a yes is a
red test whose message asks for the file to be declared (a rule source, or a
registered chart values key) or declared not to ship rules
(`NOT_RULE_FILES`, with the reason).

⛔ Its answer is messages, never a list of files, and no module under
scripts/tools imports it (tests/lint/test_rule_tripwire.py pins both): a
tripwire whose hits were fed to the readers would be the content-based
discovery the declaration replaced, back by another door.

"Looks like rules" is `_rules_shaped_at_any_depth` — a `groups:` holding a
real rule (`alert` or `record`) at any depth, or a bare list of rules with
`expr` (the shape a chart's values hand to `toYaml`) — asked of:
  * the any-YAML reading of each document of the file;
  * the any-YAML reading of every string value in it that contains a `:`
    (a ConfigMap data value — `|` block, quoted scalar or one-line JSON — is
    such a string; text without a `:` reads to no mapping, so to no rule),
    and of every `binaryData` value, base64-decoded;
and, where a reading refuses the text (a Helm template, a broken value), a
regex for a rule key (`alert` / `record`) written in any of YAML's spellings
— a block sequence entry (`- alert:`), a flow mapping (`{alert: …`,
`, record: …`) or a JSON key (`"alert":`) — on that text and, for a whole
file no reading reads, on every base64-shaped scalar in it, decoded (a
`binaryData` value of a templated ConfigMap).
Known misses: a string inside a string (a rule file nested in a value of a
value), rules a template builds from parts (a key spelled through `{{ }}`),
a rule key that is neither first in its block entry nor after `{` / `,`
(in a block mapping below `- expr: …`, say) when no reading reads the text,
and a binaryData value that is not base64 of UTF-8 text (named nowhere: it
is no rule file either). A file no standard encoding decodes is named (it
cannot be judged at all).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

_THIS_DIR = str(Path(__file__).resolve().parent)
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)
_TOOLS_DIR = str(Path(__file__).resolve().parent.parent)
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)
import _lib_consumer_read  # noqa: E402
from _rule_tree import _decode_manifest  # noqa: E402

# ⛔ Regex, not a literal. `-  alert:` (two spaces) is the same YAML and
# evaded the substring, so the tripwire could be stepped around with a
# whitespace edit. Three spellings of a rule key, for text no reading reads:
# a block sequence entry (`- alert:`), a key after `{` or `,` (a flow
# mapping, or JSON), each with the key bare, single- or double-quoted.
_ALERT_LINE = re.compile(
    r"""(?:^[ \t]*-[ \t]+|[{,]\s*)(?:alert|record|"alert"|"record"|'alert'|'record')\s*:""",
    re.M)
#: A scalar that may be base64 (a `binaryData` value) in a text no reading reads.
_BASE64_SCALAR = re.compile(r"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/]{16,}={0,2}(?![A-Za-z0-9+/=])")


def _spells_out_rules(text: str) -> bool:
    """Whether *text*, which no reading reads, spells out a rule key — in
    itself, or in a base64-shaped scalar of it decoded (a templated
    ConfigMap's `binaryData`)."""
    if _ALERT_LINE.search(text):
        return True
    for m in _BASE64_SCALAR.finditer(text):
        try:
            decoded = _lib_consumer_read.binary_data_bytes(m.group(0)).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            continue
        if _ALERT_LINE.search(decoded):
            return True
    return False


def _is_rule_groups(value) -> bool:
    """Prometheus rule-group shape, not merely a key spelled `groups`.

    ⛔ `groups:` is a popular word. k8s/04-tenant-api/configmap-rbac.yaml
    uses it for RBAC subject groups (name / tenants / permissions). A rule
    group always carries `rules`; an empty list is allowed because a
    generator with nothing to emit legitimately produces one.
    """
    if not isinstance(value, list):
        return False
    return not value or any(
        isinstance(g, dict) and "rules" in g for g in value)


def _rules_shaped_at_any_depth(node, _depth: int = 0):
    """First path at which *node* nests something that is a Prometheus rule set.

    Returns a dotted path string, or None. Two shapes, because a chart can
    supply either half and let the template supply the other:
      * a mapping with a `groups:` key holding at least one real rule, and
      * a bare list of rules, which a template wraps in `groups:` itself.

    `_is_rule_groups` alone is too loose for a tripwire — it accepts `[]`,
    which is a legitimate empty generator artifact. A tripwire that fires on
    nothing gets deleted, so require an actual rule.
    """
    if _depth > 12:                       # charts are not this deep; stop anyway
        return None
    if isinstance(node, dict):
        groups = node.get("groups")
        if _is_rule_groups(groups) and any(
                isinstance(r, dict) and ("alert" in r or "record" in r)
                for g in (groups or []) if isinstance(g, dict)
                for r in (g.get("rules") or [])):
            return "groups"
        for key, value in node.items():
            found = _rules_shaped_at_any_depth(value, _depth + 1)
            if found is not None:
                return f"{key}.{found}" if found else str(key)
        return None
    if isinstance(node, list):
        if any(isinstance(r, dict) and ("alert" in r or "record" in r) and "expr" in r
               for r in node):
            return ""                     # a bare rule list; caller names it
        for i, item in enumerate(node):
            found = _rules_shaped_at_any_depth(item, _depth + 1)
            if found is not None:
                return f"[{i}].{found}" if found else f"[{i}]"
    return None


def _texts_inside(node, _depth: int = 0):
    """The texts *node* holds that a rule file could be written in: every
    string value with a `:` in it, and every `binaryData` value decoded
    (base64 of UTF-8; one that is not is skipped — it is no text)."""
    if _depth > 64:
        return
    if isinstance(node, str):
        if ":" in node:
            yield node
    elif isinstance(node, dict):
        for k, v in node.items():
            if k == "binaryData" and isinstance(v, dict):
                for blob in v.values():
                    try:
                        yield _lib_consumer_read.binary_data_bytes(blob).decode("utf-8")
                    except (ValueError, UnicodeDecodeError):
                        continue
            else:
                yield from _texts_inside(v, _depth + 1)
    elif isinstance(node, list):
        for v in node:
            yield from _texts_inside(v, _depth + 1)


def _without(doc, key_path):
    """*doc* with the subtree at *key_path* removed (a registered chart
    values key is read as rules, so it is no alarm)."""
    if not key_path or not isinstance(doc, dict) or key_path[0] not in doc:
        return doc
    if len(key_path) == 1:
        return {k: v for k, v in doc.items() if k != key_path[0]}
    return {**doc, key_path[0]: _without(doc[key_path[0]], key_path[1:])}


def declared_not_rules() -> "frozenset[str]":
    """The YAML the declaration says looks like rules and ships none:
    `NOT_RULE_FILES`, each with its reason. (A `RULE_SOURCES` entry's
    `not_rule_files` only says which YAML beside the rule files is accounted
    for; the tripwire still judges them.)"""
    return frozenset(p for p, _why in _lib_consumer_read.NOT_RULE_FILES)


def rule_shaped_outside_declaration(repo_root: str, tracked_yaml: "list[str]",
                                    declared: "frozenset[str]", *,
                                    honour_exemptions: bool = True) -> "tuple[str, ...]":
    """One message per tracked YAML in *tracked_yaml* (repo-relative) that is
    not a *declared* rule file, not declared to ship no rules
    (``declared_not_rules``), and looks like rules — the empty tuple when
    nothing does. A registered chart values file (`CHART_VALUES_RULES`) is
    judged without its registered keys. Raises `ConsumerReadError` when
    da-crdecode cannot be had (an any-YAML refusal is no error: such a file
    is judged on its text). With *honour_exemptions* False, `NOT_RULE_FILES`
    is judged too — the check that an exemption is still needed
    (`_lib_consumer_read.stale_rule_source_entries`)."""
    skip = declared | (declared_not_rules() if honour_exemptions else frozenset())
    registered: "dict[str, list[tuple[str, ...]]]" = {}
    for reg in _lib_consumer_read.CHART_VALUES_RULES:
        registered.setdefault(reg.values, []).append(reg.key_path)
    todo = [rel for rel in tracked_yaml if rel not in skip]
    readings = _lib_consumer_read.any_yaml_documents_of(
        [str(Path(repo_root, rel)) for rel in todo], keep_refusals=True)
    ask = ("declare it in _lib_consumer_read (a RULE_SOURCES entry, or a "
           "CHART_VALUES_RULES registration), or declare that it ships no rules "
           "(NOT_RULE_FILES, with the reason)")
    alarms: "dict[str, str]" = {}
    inner: "list[tuple[str, str]]" = []          # (rel, a text inside it)
    for rel, docs in zip(todo, readings.values()):
        try:
            raw = Path(repo_root, rel).read_bytes()
        except OSError as e:
            alarms[rel] = (f"cannot be read ({e.strerror}), so whether it holds "
                           "rules is unknown")
            continue
        text, enc = _decode_manifest(raw)
        if text is None:
            alarms[rel] = (f"no standard encoding decodes it ({enc}); neither this "
                           "tripwire nor kubectl can read it")
            continue
        if isinstance(docs, _lib_consumer_read.ConsumerReadError):
            if _spells_out_rules(text):
                alarms[rel] = ("spells out a rule key (`alert` / `record`; no YAML "
                               "reading reads the file)")
            continue
        for doc in docs:
            for key_path in registered.get(rel, ()):
                doc = _without(doc, key_path)
            at = _rules_shaped_at_any_depth(doc)
            if at is not None:
                alarms[rel] = f"holds Prometheus rules at `{at or '(root)'}`"
                break
            inner += [(rel, t) for t in _texts_inside(doc)]
    inner = [(rel, t) for rel, t in inner if rel not in alarms]
    for (rel, t), docs in zip(inner, _lib_consumer_read.any_yaml_text_documents_each(
            [t for _r, t in inner], keep_refusals=True)):
        if rel in alarms:
            continue
        if isinstance(docs, _lib_consumer_read.ConsumerReadError):
            if _ALERT_LINE.search(t):
                alarms[rel] = ("holds a text that spells out a rule key (`alert` / "
                               "`record`; no YAML reading reads it)")
        elif any(_rules_shaped_at_any_depth(d) is not None for d in docs):
            alarms[rel] = "holds a text (a data value, say) that reads as Prometheus rules"
    return tuple(f"{rel}: {alarms[rel]}, and no rule contract reads it — {ask}"
                 for rel in todo if rel in alarms)
