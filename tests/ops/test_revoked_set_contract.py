"""The revoked.txt line contract must read the same on all three sides
(#1235 / TRK-349).

`revoked.txt` is written by tenant-api and read by two independently
implemented consumers — the gateway Lua filter and the ADR-028 reconciler.
They used to apply different line/whitespace semantics, so the same file
could parse to different token sets on the two ends, and ADR-028's whole
detection argument assumes they see the same set.

The fix put an explicit pattern on each side. That immediately creates the
disease the issue is about: a contract copied by hand into three files. This
module is the cheap mechanical guard against those three copies drifting
apart:

  1. components/tenant-api/internal/federation/token/manager.go
     `tokenIDPattern` — the WRITER.
  2. helm/federation-gateway/values.yaml `revokedSet.tokenIdPattern` — the
     chart value templated into revoked_check.lua. The Lua deliberately does
     not hard-code it, so the chart is the gateway's copy.
  3. scripts/tools/ops/_federation_revocation_reconciler.py
     `TOKEN_ID_PATTERN` — the DETECTOR.

⛔ LIMIT OF THE THREE-LITERAL CHECK — read before trusting it.
Literal equality is NECESSARY, NOT SUFFICIENT: it catches a hand-copied
pattern drifting, and nothing else. Three regex dialects can spell the
same-looking pattern and not mean the same thing, and two readers can
disagree about what a line IS one layer below the pattern entirely — both
of which are the family #1235 belongs to.

The division of labour, no part of which substitutes for another:

  * the three-literal check (below)   — a hand-copied pattern drifting.
  * federation-e2e S11 (positive)     — PATTERN-DIALECT drift: one literal
    meaning different things to the three engines.
  * federation-e2e S10 (negative)     — READER-SEMANTICS drift: the two
    readers disagreeing about what a line IS. ⚠️ S11 has NO resistance to
    that class — both the old and the current reader pass S11, which is
    exactly why S10 exists.
  * the conformance matrix at the bottom of THIS module — the byte space,
    mechanically enumerated through the production entry point.

If the literal check ever goes red, fix the drift — but never treat it
going green as evidence the two readers agree.
"""
from __future__ import annotations

import os
import re
import sys

import pytest

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_TOOLS_OPS = os.path.join(_REPO_ROOT, "scripts", "tools", "ops")
sys.path.insert(0, _TOOLS_OPS)

import _federation_revocation_reconciler as rec  # noqa: E402

_MANAGER_GO = os.path.join(
    _REPO_ROOT, "components", "tenant-api", "internal", "federation", "token", "manager.go")
_GATEWAY_VALUES = os.path.join(_REPO_ROOT, "helm", "federation-gateway", "values.yaml")
_GATEWAY_LUA = os.path.join(
    _REPO_ROOT, "helm", "federation-gateway", "files", "revoked_check.lua")


def _go_pattern() -> str:
    """Resolve manager.go's `tokenIDPattern`, which is a const expression
    built from `tokenIDPrefix` — so the prefix is not hand-copied either."""
    src = open(_MANAGER_GO, encoding="utf-8").read()
    prefix_m = re.search(r'^const tokenIDPrefix = "([^"]*)"', src, re.MULTILINE)
    assert prefix_m, "tokenIDPrefix const not found in manager.go"
    expr_m = re.search(r'^const tokenIDPattern = (.+)$', src, re.MULTILINE)
    assert expr_m, "tokenIDPattern const not found in manager.go"

    # Evaluate the Go constant concatenation: string literals joined by `+`,
    # with the bare identifier `tokenIDPrefix` substituted. Tokenised rather
    # than split on "+", because the pattern literal itself contains one.
    expr = expr_m.group(1).strip()
    term = re.compile(r'\s*(?:"((?:[^"\\]|\\.)*)"|(tokenIDPrefix))\s*')
    out = []
    pos = 0
    while pos < len(expr):
        m = term.match(expr, pos)
        assert m, f"unhandled term in tokenIDPattern const expression at {expr[pos:]!r}"
        out.append(prefix_m.group(1) if m.group(2) else m.group(1).encode().decode("unicode_escape"))
        pos = m.end()
        if pos < len(expr):
            assert expr[pos] == "+", \
                f"unhandled operator in tokenIDPattern const expression: {expr[pos:]!r}"
            pos += 1
    return "".join(out)


def _values_pattern() -> str:
    # ⛔ A hard import, NOT importorskip. This module is a drift gate; a gate
    # that can skip itself out of existence on a machine missing a dependency
    # reports "no drift" for the one reason that has nothing to do with drift.
    # CI installs pyyaml explicitly, so a missing module here means the
    # environment is wrong and should say so.
    import yaml
    with open(_GATEWAY_VALUES, encoding="utf-8") as fh:
        return yaml.safe_load(fh)["revokedSet"]["tokenIdPattern"]


def test_all_three_sides_declare_the_same_pattern():
    go = _go_pattern()
    values = _values_pattern()
    python = rec.TOKEN_ID_PATTERN
    assert go == values == python, (
        "revoked.txt line contract has drifted between its three copies — "
        f"manager.go={go!r} values.yaml={values!r} reconciler={python!r}"
    )


def test_gateway_lua_takes_the_pattern_from_the_chart():
    """The Lua must consume the templated value, not carry its own copy —
    otherwise the values.yaml side of the test above checks nothing."""
    lua = open(_GATEWAY_LUA, encoding="utf-8").read()
    assert ".Values.revokedSet.tokenIdPattern" in lua, \
        "revoked_check.lua no longer sources the contract from the chart"
    assert "TOKEN_ID_PATTERN" in lua
    # A hard-coded prefix literal next to the pattern would mean a fourth copy.
    assert 'local TOKEN_ID_PATTERN = "^ftk_' not in lua, \
        "revoked_check.lua hard-codes the token-id contract again"


def test_pattern_is_anchored_at_both_ends():
    """An unanchored contract would accept a line with anything around a valid
    id — the readers would then be free to disagree about the rest of it."""
    p = rec.TOKEN_ID_PATTERN
    assert p.startswith("^") and p.endswith("$"), p


@pytest.mark.parametrize("token_id", ["ftk_1", "ftk_deadbeefdeadbeef", "ftk_0a1b"])
def test_conforming_ids_are_accepted(token_id):
    assert rec.parse_revoked_file(token_id + "\n") == ({token_id}, 0)


# ── Conformance matrix ───────────────────────────────────────────────
#
# The cases below are MECHANICALLY ENUMERATED, not hand-listed, and that is
# deliberate on two counts.
#
#   * Strength. A hand-written list of "inputs that once broke it" only
#     covers what somebody thought of. #1235 is a whole-class defect — two
#     readers disagreeing about what a line IS — so the guard has to be the
#     class, not a sample of it. The enumeration below covers every byte in
#     every position it can occupy relative to a conforming id.
#   * Shape. A payload-shaped fixture reads as an instruction; a
#     cross-product reads as a specification table. Same information, and
#     the table is the stronger of the two. (Go took the same route for the
#     CVE-2023-24534 parser fix: an invariant, not a proof-of-concept.)
#
# ⛔ Never "simplify" this back into a literal list of interesting inputs.
# The one input a hand-written list reliably omits is the one nobody wanted
# to write down — which is exactly the one an attacker starts from.
_SEPARATOR = b"\n"
_CONFORMING = "ftk_a1"


def _oracle(raw: bytes) -> tuple[set[str], int]:
    """Independent expectation, computed over BYTES.

    A line is what lies between separator bytes. At most one trailing CR is
    dropped (the single tolerated deviation, so a CRLF-written file reads
    the same on both ends). A non-empty line is an id iff it is exactly the
    prefix followed by lower-case hex.

    Written over bytes on purpose: an oracle that went through the same
    text-decoding path as the implementation would inherit that path's
    behaviour and could not detect it. The decode layer reinterpreting
    bytes IS one of the defects this module exists to pin.
    """
    ids: set[str] = set()
    rejected = 0
    for chunk in raw.split(_SEPARATOR):
        if chunk.endswith(b"\r"):
            chunk = chunk[:-1]
        if not chunk:
            continue
        try:
            text = chunk.decode("ascii")
        except UnicodeDecodeError:
            rejected += 1
            continue
        if re.fullmatch(r"ftk_[0-9a-f]+", text):
            ids.add(text)
        else:
            rejected += 1
    return ids, rejected


def _positions(payload: bytes) -> list[tuple[str, bytes]]:
    """`payload` placed in each position it can occupy relative to a
    conforming id, plus the two-line form that a reader which merges or
    truncates lines would resolve differently from one that does not."""
    body = _CONFORMING.encode()
    head, tail = body[:4], body[4:]
    return [
        ("leading", payload + body + _SEPARATOR),
        ("trailing", body + payload + _SEPARATOR),
        ("interior", head + payload + tail + _SEPARATOR),
        ("split", head + payload + _SEPARATOR + tail + _SEPARATOR),
        ("before-a-clean-line", payload + _SEPARATOR + body + _SEPARATOR),
        ("after-a-clean-line", body + _SEPARATOR + payload + _SEPARATOR),
    ]


def test_file_level_matrix_over_every_byte(tmp_path):
    """Every byte, every position, through the PRODUCTION entry point.

    `read_live_set` — not `parse_revoked_file` — because the file-to-text
    step is itself a place where bytes can be reinterpreted, and a
    string-level test is structurally blind to it.

    Bytes that are not valid UTF-8 must make the read FAIL rather than
    resolve to something: the reconciler is fail-closed, and a half-decoded
    revoked set is the one answer that must never be produced.
    """
    p = tmp_path / "revoked.txt"
    mismatches = []
    for b in range(0x100):
        if bytes([b]) == _SEPARATOR:
            continue
        for label, raw in _positions(bytes([b])):
            p.write_bytes(raw)
            expected_decodable = True
            try:
                raw.decode("utf-8")
            except UnicodeDecodeError:
                expected_decodable = False
            try:
                got = rec.read_live_set(str(p))
            except UnicodeDecodeError:
                if expected_decodable:
                    mismatches.append((hex(b), label, "unexpected decode failure"))
                continue
            if not expected_decodable:
                mismatches.append((hex(b), label, f"undecodable bytes resolved to {got!r}"))
                continue
            want = _oracle(raw)
            if got != want:
                mismatches.append((hex(b), label, f"got {got!r} want {want!r}"))
    assert not mismatches, (
        f"{len(mismatches)} byte/position combinations resolve to something other "
        f"than their verbatim reading (first 10: {mismatches[:10]})")


def test_string_level_matrix_over_a_codepoint_sweep():
    """The same table widened past one byte.

    The file layer is pinned exhaustively above; this sweep widens the
    PATTERN half over multi-byte encodings, where the risk is a decision
    about what a character is rather than about where a line ends.
    """
    mismatches = []
    for cp in range(0x20, 0x3100):
        payload = chr(cp).encode("utf-8")
        for label, raw in _positions(payload):
            got = rec.parse_revoked_file(raw.decode("utf-8"))
            want = _oracle(raw)
            if got != want:
                mismatches.append((hex(cp), label, f"got {got!r} want {want!r}"))
    assert not mismatches, (
        f"{len(mismatches)} codepoint/position combinations resolve to something "
        f"other than their verbatim reading (first 10: {mismatches[:10]})")


def test_the_matrix_can_actually_fail(tmp_path):
    """Meta-guard: prove the oracle is not vacuously agreeing with the
    implementation. A reader that normalised its input — the behaviour
    #1235 removed — must be reported as a mismatch by the same comparison
    the two tests above use."""
    def _normalising_reader(text: str) -> tuple[set[str], int]:
        return {ln.strip() for ln in text.splitlines() if ln.strip()}, 0

    disagreements = 0
    for cp in range(0x20, 0x3100):
        raw = _positions(chr(cp).encode("utf-8"))[1][1]  # trailing position
        if _normalising_reader(raw.decode("utf-8")) != _oracle(raw):
            disagreements += 1
    assert disagreements > 0, (
        "the oracle agrees with a normalising reader everywhere — it cannot "
        "be detecting the class of defect it exists to detect")
