"""Coverage tripwire: rule-shaped YAML outside the rule-source declaration (#3438).

⛔ An ALARM, not a classifier. Which files hold rule files is declared by
path (`_lib_consumer_read.RULE_SOURCES` / `CHART_VALUES_RULES`) and read by
`_lib_consumer_read.rule_files_in`; nothing here decides what is read. This
module answers one question about every OTHER tracked YAML under the shipped
roots — "does anything in it look like Prometheus rules?" — and a yes is a
red test whose message asks for the file to be declared (a rule source, or a
registered chart values key) or declared not to ship rules
(`NOT_RULE_FILES`, or the `not_rule_files` of its directory's entry).

⛔ Its answer is messages, never a list of files, and no module under
scripts/tools imports it (tests/lint/test_rule_tripwire.py pins both): a
tripwire whose hits were fed to the readers would be the content-based
discovery the declaration replaced, back by another door.

"Looks like rules" is the criterion the repo already had, not a new one:
  * `_rules_shaped_at_any_depth` over the any-YAML reading of each document
    (a `groups:` holding a real rule at any depth, or a bare list of rules —
    the shape a chart's values hand to `toYaml`), and
  * the `- alert:` regex, over the text and over every string the any-YAML
    reading holds (a ConfigMap data value written as one quoted scalar, which
    the generated rules ConfigMaps are).
Known misses: rules in base64 (`binaryData`), a recording-rule-only file
spelled as a quoted string, rules a template builds from parts. A file no
standard encoding decodes is named (it cannot be judged at all).
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
# whitespace edit.
_ALERT_LINE = re.compile(r"^\s*-\s+alert\s*:", re.M)


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


def _strings(node, _depth: int = 0):
    """Every string *node* holds, keys and values, at any depth."""
    if _depth > 64:
        return
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for k, v in node.items():
            if isinstance(k, str):
                yield k
            yield from _strings(v, _depth + 1)
    elif isinstance(node, list):
        for v in node:
            yield from _strings(v, _depth + 1)


def _without(doc, key_path):
    """*doc* with the subtree at *key_path* removed (a registered chart
    values key is read as rules, so it is no alarm)."""
    if not key_path or not isinstance(doc, dict) or key_path[0] not in doc:
        return doc
    if len(key_path) == 1:
        return {k: v for k, v in doc.items() if k != key_path[0]}
    return {**doc, key_path[0]: _without(doc[key_path[0]], key_path[1:])}


def declared_not_rules() -> "frozenset[str]":
    """The YAML the declaration says ships no rules: every `RULE_SOURCES`
    entry's `not_rule_files`, and `NOT_RULE_FILES`."""
    beside = {f"{s.directory}/{n}" for s in _lib_consumer_read.RULE_SOURCES
              for n in s.not_rule_files}
    return frozenset(beside | {p for p, _why in _lib_consumer_read.NOT_RULE_FILES})


def rule_shaped_outside_declaration(repo_root: str, tracked_yaml: "list[str]",
                                    declared: "frozenset[str]") -> "tuple[str, ...]":
    """One message per tracked YAML in *tracked_yaml* (repo-relative) that is
    not a *declared* rule file, not declared to ship no rules
    (``declared_not_rules``), and looks like rules — the empty tuple when
    nothing does. A registered chart values file (`CHART_VALUES_RULES`) is
    judged without its registered keys. Raises `ConsumerReadError` when
    da-crdecode cannot be had (an any-YAML refusal is no error: such a file
    is judged on its text)."""
    skip = declared | declared_not_rules()
    registered: "dict[str, list[tuple[str, ...]]]" = {}
    for reg in _lib_consumer_read.CHART_VALUES_RULES:
        registered.setdefault(reg.values, []).append(reg.key_path)
    todo = [rel for rel in tracked_yaml if rel not in skip]
    readings = _lib_consumer_read.any_yaml_documents_of(
        [str(Path(repo_root, rel)) for rel in todo], keep_refusals=True)
    ask = ("declare it in _lib_consumer_read (a RULE_SOURCES entry, or a "
           "CHART_VALUES_RULES registration), or declare that it ships no rules "
           "(NOT_RULE_FILES, or the not_rule_files of its directory's entry)")
    alarms = []
    for rel, docs in zip(todo, readings.values()):
        try:
            raw = Path(repo_root, rel).read_bytes()
        except OSError as e:
            alarms.append(f"{rel}: cannot be read ({e.strerror}), so whether it holds "
                          f"rules is unknown — {ask}")
            continue
        text, enc = _decode_manifest(raw)
        if text is None:
            alarms.append(f"{rel}: no standard encoding decodes it ({enc}); neither this "
                          f"tripwire nor kubectl can read it — {ask}")
            continue
        why = None
        if isinstance(docs, _lib_consumer_read.ConsumerReadError):
            if _ALERT_LINE.search(text):
                why = "spells out `- alert:` (no YAML reading reads the file)"
        else:
            for doc in docs:
                for key_path in registered.get(rel, ()):
                    doc = _without(doc, key_path)
                at = _rules_shaped_at_any_depth(doc)
                if at is not None:
                    why = f"holds Prometheus rules at `{at or '(root)'}`"
                    break
                if any(_ALERT_LINE.search(s) for s in _strings(doc)):
                    why = "holds a string that spells out `- alert:`"
                    break
        if why is not None:
            alarms.append(f"{rel}: {why}, and no rule contract reads it — {ask}")
    return tuple(alarms)
