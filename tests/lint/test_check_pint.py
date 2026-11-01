"""Tests for the pint linter wrapper (check_pint.py).

Codifies the hand-maintained sync points the wrapper depends on — every one of
them a place where two halves of a single decision live in different files and
can drift APART while CI stays green:

1. the pint version pinned in `check_pint.py` (docker fallback tag) must match the
   version the CI step installs as a binary — otherwise CI runs a mixed setup (the
   binary-on-PATH path wins, so the wrapper's pinned version may never execute and
   a bump to one side silently drifts);
2. `.pint.hcl`'s `parser.include` must be reachable from `_PINT_ARGS` — pint only
   walks the directories it is handed on the CLI, so an include path with no CLI
   root above it is dead config that lints nothing;
3. the declared platform packs (`_lib_consumer_read.PLATFORM_PACKS`) must be
   linted at their rule-pack source and no generated ConfigMap copy may be
   (#1595 D1a: the platform ConfigMap is generated now); and
4. the `entries=N` non-vacuity floor must survive pint's ANSI-coloured output —
   the earlier `entries == 0` guard did not, and was inert in CI as a result.
"""
import importlib.util
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _read(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


def _wrapper_module():
    """Import check_pint.py by path (scripts/ is not an importable package)."""
    path = REPO_ROOT / "scripts" / "tools" / "lint" / "check_pint.py"
    spec = importlib.util.spec_from_file_location("_check_pint_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_pint_version_sync_between_wrapper_and_ci():
    wrapper = _read("scripts/tools/lint/check_pint.py")
    ci = _read(".github/workflows/ci.yml")

    m_wrapper = re.search(r'PINT_VERSION\s*=\s*"([\d.]+)"', wrapper)
    m_ci = re.search(r'\bPINT=([\d.]+)', ci)

    assert m_wrapper, "PINT_VERSION not found in check_pint.py"
    assert m_ci, "PINT=<version> not found in the ci.yml pint install step"
    assert m_wrapper.group(1) == m_ci.group(1), (
        f"pint version drift: check_pint.py PINT_VERSION={m_wrapper.group(1)} "
        f"vs ci.yml PINT={m_ci.group(1)} — keep them in sync "
        f"(docker tag has no `v`; the GitHub release tag the curl uses does)."
    )


def test_pint_config_scopes_to_rule_pack_sources():
    """The gate must scope to the canonical rule-pack sources.

    The GENERATED `configmap-rules-*.yaml` copies stay out (they carry
    `DO NOT EDIT` and are synced by check_rulepack_sync.py) — the platform one
    included since #1595 D1a.
    """
    hcl = _read(".pint.hcl")
    assert re.search(r'include\s*=\s*\[[^\]]*rule-packs/rule-pack-', hcl, re.S), (
        "parser.include must scope to rule-packs/rule-pack-*.yaml"
    )
    # alerts/template (the killer check) must stay enabled globally — only the
    # match-all idiom disables (comparison/impossible/dependency) belong there.
    global_disable = re.search(r'rule\s*\{\s*disable\s*=\s*\[([^\]]*)\]', hcl)
    assert global_disable, "expected a global match-all `rule { disable = [...] }`"
    assert "alerts/template" not in global_disable.group(1), (
        "alerts/template must NOT be globally disabled — it is the gate's reason to exist"
    )


def _hcl_list(hcl: str, key: str) -> list[str]:
    """Extract a `parser` block string-list (`include` / `relaxed`) from .pint.hcl."""
    # ⛔ Strip comments FIRST. pint ignores a commented-out list entry; a plain
    # `"..."` findall does not, so `# "k8s/.../configmap-rules-platform.*"`
    # reads as still-included here while pint silently drops 42 entries. One
    # `# ` is all it takes, and the lockstep this helper exists to enforce is
    # then enforcing nothing. Quoted `#` inside a path would be a false strip,
    # so only treat `#`/`//` as a comment when it starts a line (after indent),
    # which is the only form this file uses.
    # `/* */` too: HCL accepts it, so commenting an include entry out that way
    # drops the same 42 entries from pint while a `#`-only strip still reads
    # the path as live. Block comments first, then line comments.
    # ⛔ LINE comments first. .pint.hcl already contains an unpaired `/*` inside
    # a `#` comment (a glob, `tests/rulepacks/*`); stripping block comments
    # first lets that `/*` pair with a later `*/` and swallow real config,
    # producing a misleading "parser.include NOT FOUND" instead of a drift
    # report. Removing line comments first makes the stray `/*` disappear with
    # the comment that contains it.
    hcl = re.sub(r'^[ \t]*(?:#|//).*$', '', hcl, flags=re.M)
    hcl = re.sub(r'/\*.*?\*/', '', hcl, flags=re.S)
    block = re.search(r'parser\s*\{(.*?)\n\}', hcl, re.S)
    assert block, "expected a `parser { ... }` block in .pint.hcl"
    m = re.search(rf'^\s*{key}\s*=\s*\[(.*?)\]', block.group(1), re.S | re.M)
    assert m, f"parser.{key} not found in .pint.hcl"
    return re.findall(r'"((?:[^"\\]|\\.)*)"', m.group(1))


def _include_matches(include: "list[str]", path: str) -> bool:
    """Whether pint's include list admits *path* (pint auto-anchors: `^X$`)."""
    return any(re.match("^" + p.replace("\\\\", "\\") + "$", path) for p in include)


def test_platform_pack_is_linted_in_its_source_form_and_no_copy_is():
    """#1595 D1a: the platform rules are a rule pack — linted at the source.

    Two failure modes this pins down, both silently GREEN:

    * the platform pack falling out of `parser.include` (a narrower rule-pack
      pattern) takes every platform alert out of pint; and
    * a generated `configmap-rules-*` copy back in scope reports every finding
      twice, half of them on a `DO NOT EDIT` file — the platform ConfigMap was
      the one hand-maintained copy, read through `parser.relaxed`, and is now
      generated like the rest, so neither it nor `relaxed` belongs here.

    Which packs are platform packs, and what their files and copies are named,
    is read from the declaration (`_lib_consumer_read`), not restated here.
    """
    import _lib_consumer_read as cr

    hcl = _read(".pint.hcl")
    include = _hcl_list(hcl, "include")
    assert cr.PLATFORM_PACKS, "no platform pack declared — nothing to pin"
    for pack in sorted(cr.PLATFORM_PACKS):
        source = f"{cr.RULE_PACKS.directory}/{cr.RULE_PACKS.file_name(pack)}"
        assert _include_matches(include, source), (
            f"parser.include does not admit the platform pack source {source}")
    for pack in sorted(cr.PLATFORM_PACKS) + ["mariadb", "kubernetes"]:
        copy = f"{cr.CONFIGMAP_RULES.directory}/{cr.CONFIGMAP_RULES.file_name(pack)}"
        assert not _include_matches(include, copy), (
            f"parser.include admits the generated copy {copy}: every finding would "
            f"be reported twice, once on a file developers must not edit")
    stripped = re.sub(r'^[ \t]*(?:#|//).*$', '', hcl, flags=re.M)
    assert not re.search(r'^\s*relaxed\s*=', stripped, re.M), (
        "parser.relaxed existed only for the hand-maintained platform ConfigMap; "
        "no rule file in scope is ConfigMap-wrapped any more")


def test_cli_roots_cover_every_parser_include_path():
    """`parser.include` and `_PINT_ARGS` are two halves of one decision.

    pint only walks the directories handed to it on the CLI, so an include pattern
    with no CLI root above it lints nothing while CI stays green — measured: adding
    the ConfigMap to `parser.include` alone left entries at 351 (unchanged) and
    exit 0; adding `k8s/03-monitoring/` to `_PINT_ARGS` took it to 393.
    """
    mod = _wrapper_module()
    roots = [a for a in mod._PINT_ARGS if a.endswith("/")]
    assert roots, "_PINT_ARGS should hand pint at least one directory root"

    for pat in _hcl_list(_read(".pint.hcl"), "include"):
        # literal directory prefix of the regexp, up to the first metacharacter
        literal = re.split(r'[.*+?\[\](){}|^$\\]', pat)[0]
        assert any(literal.startswith(r) for r in roots), (
            f"parser.include has {pat!r} but no _PINT_ARGS root covers {literal!r} — "
            f"pint never walks there, so that include is dead config. Roots: {roots}"
        )


def test_entries_floor_survives_pints_ansi_coloured_output():
    """The non-vacuity guard must be a FLOOR, and must actually parse pint's output.

    pint colours its logs unconditionally (it does not test for a TTY), so under
    `capture_output=True` the count arrives wrapped in SGR escapes. A bare
    `entries=(\\d+)` never matches that — which is exactly how the earlier
    `entries == 0` guard ended up inert in CI. Regression-pin both the ANSI
    tolerance and the floor's sizing window.
    """
    mod = _wrapper_module()
    real_log_line = (
        '\x1b[2mlevel=\x1b[0m\x1b[97mINFO\x1b[0m \x1b[2mmsg=\x1b[0m\x1b[97m'
        '"Checking Prometheus rules"\x1b[0m \x1b[2mentries=\x1b[0m\x1b[94m393\x1b[0m '
        '\x1b[2mworkers=\x1b[0m\x1b[94m10\x1b[0m\n'
    )
    m = mod._ENTRIES_RE.search(mod._ANSI_SGR_RE.sub("", real_log_line))
    assert m and m.group(1) == "393", (
        "the entries= probe must survive pint's ANSI colouring — otherwise the "
        "non-vacuity floor silently never runs"
    )
    # …and it must not depend on the colouring being there.
    plain = mod._ENTRIES_RE.search(
        mod._ANSI_SGR_RE.sub("", 'msg="Checking Prometheus rules" entries=393\n'))
    assert plain and plain.group(1) == "393"

    floor = mod._MIN_PINT_ENTRIES
    assert isinstance(floor, int)
    # ⛔ DERIVE the window; do not snapshot it. Hard-coding `< 393` deadlocked
    # the two guards against each other: the runtime staleness tripwire only
    # fires once entries reach measure+pack, and every value it can suggest is
    # then above the frozen upper bound — so obeying CI's own instruction turned
    # this test red, with a message about PINT_VERSION that describes a different
    # situation entirely. Reading both constants makes a ratchet a two-line edit
    # that stays consistent by construction.
    total, pack = mod._PINT_ENTRIES_AT_MEASURE, mod._PLATFORM_PACK_ENTRIES

    # ⛔ The lockstep the constant's own comment claims. It did NOT exist:
    # changing _PLATFORM_PACK_ENTRIES from 42 to 30 left every test green while
    # the comment asserted the sibling value was read back "so the two cannot
    # drift". Claiming a guard is worse than not having one — it is why nobody
    # adds the guard. pint entries and platform alert count are the same
    # quantity (the platform pack holds only alerting rules, no recording rules).
    ops = _read("tests/ops/test_generate_routes_orchestration.py")
    m_alerts = re.search(r'_MIN_PLATFORM_ALERTS\s*=\s*(\d+)', ops)
    assert m_alerts, "_MIN_PLATFORM_ALERTS not found in the platform-pack scanner"
    assert pack == int(m_alerts.group(1)), (
        f"_PLATFORM_PACK_ENTRIES={pack} but _MIN_PLATFORM_ALERTS="
        f"{m_alerts.group(1)}. These count the same rules; letting them drift "
        "makes the pint staleness window size itself against a stale number.")

    # `0 <` as well as `< pack`: floor == total leaves NO room for a legitimate
    # small deletion, which contradicts the sizing note's own "8 of slack", and
    # `total := floor` would otherwise satisfy the window trivially.
    assert 0 < total - floor < pack, (
        f"_MIN_PINT_ENTRIES={floor} is outside the useful window "
        f"({total - pack}, {total}]: at or below it, losing the whole platform "
        f"pack ({pack} entries) stays green; above it, the measured total "
        f"itself would fail. Ratchet _MIN_PINT_ENTRIES and _PINT_ENTRIES_AT_MEASURE "
        f"together — check_pint.py's own failure message prints both values."
    )
