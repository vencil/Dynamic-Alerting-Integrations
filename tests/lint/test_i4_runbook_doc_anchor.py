"""The I-4 smoke test's hand-copied commands must still be in the checklist (#2142).

``scripts/tools/lint/smoke_test_i4_runbook.sh`` never reads
``docs/integration/troubleshooting-checklist{,.en}.md``: it runs copies of
the checklist's jq / promtool / amtool / yq snippets that were pasted into
the script by hand. Nothing tied the two together, so a checklist command
could be edited (or broken) while the script's copy stayed green.

This test ties them in the reverse direction. It parses every
``assert_jq`` / ``assert_promql`` / ``assert_amtool`` / ``assert_yaml``
call in the script and requires its payload to appear verbatim inside a
fenced block of the checklist section the call names (``§1.2.2`` → the
``1.2.2`` heading and its subsections), in both languages. jq / PromQL
payloads must appear as the whole single-quoted argument; YAML payloads as
a run of consecutive lines, ignoring ``#`` comments (the part of a block
that differs by language, and that yq ignores). The set of
payloads comes from the script itself; nothing is listed here by hand.

Two markers in the script record the deliberate gaps, each with a reason:

* ``# i4-doc-anchor: exempt — <reason>`` on the line right above an
  ``assert_*`` call whose payload is rewritten on purpose (placeholders
  filled in, a URL pointed at nothing). An exempt call that anchors in both
  languages is stale and fails.
* ``# i4-doc-anchor: unanchored-begin — <reason>`` …
  ``# i4-doc-anchor: unanchored-end`` around checks that use a tool
  outside the ``assert_*`` helpers. Outside the helper bodies and outside a
  region, a tool name may not appear in code at all — not only in command
  position, since ``env jq``, ``xargs jq``, ``bash -c "jq …"`` and
  ``T=jq; $T`` all run it. This catches a new inline check written the
  ordinary way; it is a text match on the source, so it cannot see a name
  the shell assembles (``j""q``, ``j\\q``, a name split by a ``\\``
  line continuation, ``eval``) or a tool run inside a heredoc fed to
  ``bash``.

Each checklist must use a section number once: a duplicated heading would
let a stale copy under the wrong heading satisfy the anchor.

What this does NOT cover: checklist commands the script never copied, and
whether a filter reads the right field (``jq '.typo // empty'`` exits 0).
"""
from __future__ import annotations

import re
import shlex
import textwrap
from collections import Counter
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "tools" / "lint" / "smoke_test_i4_runbook.sh"
DOCS = (
    REPO_ROOT / "docs" / "integration" / "troubleshooting-checklist.md",
    REPO_ROOT / "docs" / "integration" / "troubleshooting-checklist.en.md",
)

HELPERS = ("assert_jq", "assert_promql", "assert_amtool", "assert_yaml")
TOOLS = ("jq", "promtool", "amtool", "yq", "grep")
EXEMPT = "# i4-doc-anchor: exempt"
BEGIN = "# i4-doc-anchor: unanchored-begin"
END = "# i4-doc-anchor: unanchored-end"
AMTOOL_SUFFIX = " 2>&1 || true"

# A tool name as a word anywhere in code. Deliberately not "command
# position": `env jq`, `xargs jq`, `bash -c "jq"`, `T=jq; $T` and
# `/usr/bin/jq` all run it.
_TOOL_WORD = re.compile(r"(?<![\w.\-])(%s)(?![\w\-])" % "|".join(TOOLS))
_HELPER_NAME = re.compile(r"\b(%s)\b" % "|".join(HELPERS))
_HEREDOC = re.compile(r"<<-?\s*['\"]?(\w+)['\"]?")
_SECTION = re.compile(r"^§(\d+(?:\.\d+)*) ")


class Call:
    def __init__(self, line: int, helper: str, args: list[str], exempt: str | None):
        self.line, self.helper, self.args, self.exempt = line, helper, args, exempt

    @property
    def name(self) -> str:
        return self.args[0]

    @property
    def payload(self) -> str:
        p = self.args[-1]
        if self.helper == "assert_amtool" and p.endswith(AMTOOL_SUFFIX):
            p = p[: -len(AMTOOL_SUFFIX)]
        return p

    def __repr__(self) -> str:
        return f"{SCRIPT.name}:{self.line} {self.helper} {self.name!r}"


def _marker_reason(comment: str, marker: str) -> str:
    return comment.strip()[len(marker):].strip(" —-:")


def parse_script(text: str) -> tuple[list[Call], list[str]]:
    """Return the ``assert_*`` calls and every structural problem found."""
    lines = text.split("\n")
    calls: list[Call] = []
    problems: list[str] = []
    defined = set()
    in_helper = False
    region: int | None = None
    region_hits = 0
    prev_comment = ""
    i = 0
    while i < len(lines):
        raw = lines[i]
        stripped = raw.strip()
        m = re.match(r"^(\w+)\(\)\s*\{", raw)
        if m:
            defined.add(m.group(1))
            in_helper = True
            i += 1
            continue
        if in_helper:
            in_helper = raw != "}"
            i += 1
            continue
        if stripped.startswith("#"):
            if stripped.startswith(BEGIN):
                if region is not None:
                    problems.append(f"line {i + 1}: nested {BEGIN}")
                if not _marker_reason(stripped, BEGIN):
                    problems.append(f"line {i + 1}: {BEGIN} without a reason")
                region, region_hits = i + 1, 0
            elif stripped.startswith(END):
                if region is None:
                    problems.append(f"line {i + 1}: {END} without a begin")
                elif region_hits == 0:
                    problems.append(
                        f"line {region}: unanchored region invokes no tool — remove it")
                region = None
            prev_comment = stripped
            i += 1
            continue
        if not stripped:
            prev_comment = ""
            i += 1
            continue
        # Assemble one logical statement: extend until the quotes balance.
        start, buf = i, raw
        while True:
            try:
                toks = shlex.split(buf, comments=True)
                break
            except ValueError:
                i += 1
                if i >= len(lines):
                    raise AssertionError(f"unbalanced quotes from line {start + 1}")
                buf += "\n" + lines[i]
        i += 1
        hd = _HEREDOC.search(buf)
        if hd:
            while i < len(lines) and lines[i].strip() != hd.group(1):
                i += 1
            i += 1
        if toks and toks[0] in HELPERS:
            exempt = None
            if prev_comment.startswith(EXEMPT):
                exempt = _marker_reason(prev_comment, EXEMPT)
                if not exempt:
                    problems.append(f"line {start + 1}: {EXEMPT} without a reason")
            if region is not None:
                problems.append(
                    f"line {start + 1}: {toks[0]} call inside an unanchored region")
            calls.append(Call(start + 1, toks[0], toks[1:], exempt))
        else:
            if _HELPER_NAME.search(buf):
                problems.append(
                    f"line {start + 1}: helper called outside statement position "
                    "— this test cannot see that call")
            code = "\n".join(l.split(" #", 1)[0] for l in buf.split("\n"))
            if _TOOL_WORD.search(code):
                if region is None:
                    problems.append(
                        f"line {start + 1}: uses {'/'.join(TOOLS)} outside the "
                        f"assert_* helpers and outside an unanchored region")
                else:
                    region_hits += 1
        prev_comment = ""
    if region is not None:
        problems.append(f"line {region}: {BEGIN} never closed")
    unknown = sorted(n for n in defined if n.startswith("assert_") and n not in HELPERS)
    if unknown:
        problems.append(f"helper(s) this test does not know how to anchor: {unknown}")
    missing = sorted(set(HELPERS) - defined)
    if missing:
        problems.append(f"helper(s) no longer defined: {missing}")
    return calls, problems


def section_blocks(md: str) -> dict[str, list[str]]:
    """Map each numbered heading (``1.2.2``) to the fenced blocks under it,
    subsections included."""
    own: dict[str, list[str]] = {}
    numbers: list[str] = []
    current = None
    fence: list[str] | None = None
    for line in md.split("\n"):
        if line.strip().startswith("```"):
            if fence is None:
                fence = []
            else:
                if current is not None:
                    own.setdefault(current, []).append("\n".join(fence))
                fence = None
            continue
        if fence is not None:
            fence.append(line)
            continue
        m = re.match(r"^#{2,6} (\d+(?:\.\d+)*)\b", line)
        if m:
            current = m.group(1)
            numbers.append(current)
            own.setdefault(current, [])
    assert fence is None, "unterminated fenced block"
    dup = sorted(n for n, c in Counter(numbers).items() if c > 1)
    assert not dup, f"section number(s) used by more than one heading: {dup}"
    return {
        num: [b for sub, bs in own.items()
              if sub == num or sub.startswith(num + ".") for b in bs]
        for num in own
    }


def anchors(call: Call, blocks: list[str]) -> bool:
    if call.helper in ("assert_jq", "assert_promql"):
        needle = "'" + call.payload + "'"
        return any(needle in b for b in blocks)
    if call.helper == "assert_amtool":
        return any(call.payload in b for b in blocks)
    body = _yaml_lines(call.payload)
    assert body, f"{call}: empty YAML payload"
    for b in blocks:
        lines = _yaml_lines(b)
        if any(lines[k:k + len(body)] == body for k in range(len(lines) - len(body) + 1)):
            return True
    return False


def _yaml_lines(text: str) -> list[str]:
    """YAML lines with comments dropped, dedented. Comments are the one part
    of a checklist YAML block that differs by language, and yq ignores them."""
    kept = [re.sub(r"\s+#.*$", "", l).rstrip() for l in text.split("\n")
            if not l.lstrip().startswith("#")]
    kept = [l for l in kept if l]
    return textwrap.dedent("\n".join(kept)).split("\n") if kept else []


def check(script_text: str, docs: dict[str, str]) -> list[str]:
    calls, problems = parse_script(script_text)
    sections = {name: section_blocks(md) for name, md in docs.items()}
    for call in calls:
        m = _SECTION.match(call.name)
        if not m:
            problems.append(f"{call}: name does not start with '§<section> '")
            continue
        results = {}
        for name, secs in sections.items():
            if m.group(1) not in secs:
                problems.append(f"{call}: {name} has no section {m.group(1)}")
                continue
            results[name] = anchors(call, secs[m.group(1)])
        if call.exempt is None:
            for name, ok in results.items():
                if not ok:
                    problems.append(
                        f"{call}: payload not found verbatim in {name} §{m.group(1)} "
                        f"— the checklist changed or the copy drifted: {call.payload[:80]!r}")
        elif results and all(results.values()):
            problems.append(f"{call}: exempt, but anchors in every language — remove the marker")
    return problems


def _repo_docs() -> dict[str, str]:
    return {p.name: p.read_text(encoding="utf-8") for p in DOCS}


def test_every_smoke_copy_is_anchored_in_the_checklist() -> None:
    problems = check(SCRIPT.read_text(encoding="utf-8"), _repo_docs())
    assert not problems, "\n".join(problems)


def test_parser_sees_every_helper_call() -> None:
    """Lower and upper bound: every helper name used outside its definition
    and outside comments is one parsed call — no more, no fewer."""
    text = SCRIPT.read_text(encoding="utf-8")
    calls, _ = parse_script(text)
    code = [l for l in text.split("\n") if not l.lstrip().startswith("#")]
    uses = sum(len(_HELPER_NAME.findall(l)) for l in code
               if not re.match(r"^\w+\(\)\s*\{", l))
    assert calls, "parsed no assert_* call at all"
    assert len(calls) == uses


# --- counterfactuals on synthetic inputs ------------------------------------

_HEAD = "\n".join(f"{h}() {{\n    :\n}}" for h in HELPERS) + "\n"
_DOC = textwrap.dedent("""\
    ## 1.1 One
    ```bash
    curl -s x | jq '.data.a // empty'
    amtool silence query -o json --alertmanager.url=http://<am>:9093
    ```
    #### 1.1.1 Sub
    ```yaml
    spec:
      ingress:
        - port: 80
    ```
    ## 1.2 Two
    ```bash
    jq '.other'
    ```
    """)


def _run(body: str, doc: str = _DOC) -> list[str]:
    return check(_HEAD + body, {"zh": doc, "en": doc})


def test_anchored_copy_passes() -> None:
    assert _run("assert_jq \"§1.1 a\" \"$F\" '.data.a // empty'\n") == []


def test_subsection_block_counts_for_parent_section() -> None:
    body = "assert_yaml \"§1.1 np\" '\nspec:\n  ingress:\n    - port: 80'\n"
    assert _run(body) == []


def test_edited_checklist_is_red() -> None:
    doc = _DOC.replace(".data.a // empty", ".data.b // empty")
    assert _run("assert_jq \"§1.1 a\" \"$F\" '.data.a // empty'\n", doc)


def test_only_one_language_edited_is_red() -> None:
    body = _HEAD + "assert_jq \"§1.1 a\" \"$F\" '.data.a // empty'\n"
    en = _DOC.replace(".data.a // empty", ".data.b // empty")
    assert check(body, {"zh": _DOC, "en": en})


def test_copy_found_only_in_another_section_is_red() -> None:
    assert _run("assert_jq \"§1.1 o\" \"$F\" '.other'\n")


def test_substring_of_a_longer_filter_is_red() -> None:
    assert _run("assert_jq \"§1.1 a\" \"$F\" '.data.a'\n")


def test_exempt_needs_a_reason_and_goes_stale() -> None:
    call = "assert_amtool \"§1.1 q\" \"amtool silence query -o json --alertmanager.url=http://n:9093 2>&1 || true\"\n"
    assert _run(call)
    assert _run(f"{EXEMPT}\n{call}")
    assert _run(f"{EXEMPT} — URL points at nothing\n{call}") == []
    anchored = "assert_jq \"§1.1 a\" \"$F\" '.data.a // empty'\n"
    assert _run(f"{EXEMPT} — stale\n{anchored}")


def test_inline_tool_outside_region_is_red() -> None:
    assert _run("echo '{}' | jq '.x'\n")
    assert _run("if promtool check rules f; then :; fi\n")
    assert _run(f"{BEGIN} — no placeholder-free copy\necho '{{}}' | jq '.x'\n{END}\n") == []


def test_region_rules() -> None:
    assert _run(f"{BEGIN} — why\necho hi\n{END}\n"), "empty region"
    assert _run(f"{BEGIN}\necho '{{}}' | jq '.x'\n{END}\n"), "no reason"
    assert _run(f"{BEGIN} — why\necho '{{}}' | jq .x\n"), "never closed"
    call = "assert_jq \"§1.1 a\" \"$F\" '.data.a // empty'\n"
    assert _run(f"{BEGIN} — why\njq .x f\n{call}{END}\n"), "call hidden in region"


@pytest.mark.parametrize("line", [
    "env jq '.x' f",
    "command jq '.x' f",
    "OUT=`jq -n '.x'`",
    "bash -c \"jq -n '.x'\"",
    "T=jq; $T '.x' f",
    "echo '{}' | xargs jq -n '.x'",
    "/usr/bin/jq '.x' f",
    "./yq '.x' f",
    'echo "  ok §2.1.1 promtool --experimental promql format"',
])
def test_tool_word_outside_region_is_red(line: str) -> None:
    assert _run(line + "\n")


def test_tool_name_inside_a_longer_word_is_not_a_use() -> None:
    assert _run('cat > "$T/alertmanager.yml" <<\'EOF\'\nx: 1\nEOF\necho jqx myjq\n') == []


def test_duplicate_section_number_is_red() -> None:
    doc = _DOC + "#### 1.1.1 Again\n```bash\njq '.data.a // empty'\n```\n"
    with pytest.raises(AssertionError, match="more than one heading"):
        _run("assert_jq \"§1.1 a\" \"$F\" '.data.a // empty'\n", doc)


def test_call_the_parser_cannot_see_is_red() -> None:
    assert _run("if assert_jq \"§1.1 a\" \"$F\" '.data.a // empty'; then :; fi\n")


def test_unknown_helper_is_red() -> None:
    assert _run("assert_curl() {\n    :\n}\n")


def test_yaml_comments_are_ignored_but_values_are_not() -> None:
    doc = _DOC.replace("    - port: 80\n", "    - port: 80   # comment only in the doc\n")
    body = "assert_yaml \"§1.1 np\" '\n# comment only in the script\nspec:\n  ingress:\n    - port: 80'\n"
    assert _run(body, doc) == []
    assert _run(body.replace("port: 80", "port: 81"), doc)


def test_yaml_lines_must_be_consecutive() -> None:
    assert _run("assert_yaml \"§1.1 np\" '\nspec:\n    - port: 80'\n")


def test_name_without_section_is_red() -> None:
    assert _run("assert_jq \"no section\" \"$F\" '.data.a // empty'\n")
