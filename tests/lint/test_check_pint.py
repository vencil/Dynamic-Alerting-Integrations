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
3. the platform ConfigMap must be matched by PREFIX + both YAML extensions, the
   same judgment the pytest-side platform-pack scanners make, so a split pack
   cannot end up visible to one side and invisible to the other; and
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

    The 16 GENERATED `configmap-rules-*.yaml` copies and operator-manifests/ stay
    out (they carry `DO NOT EDIT` and are synced by check_rulepack_sync.py); the
    hand-maintained `configmap-rules-platform*.y(a)ml` is IN — pint reads its
    ConfigMap-embedded rules via `parser.relaxed`, so the older "ConfigMap-wrapped →
    unparseable" framing no longer applies to it.
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
    block = re.search(r'parser\s*\{(.*?)\n\}', hcl, re.S)
    assert block, "expected a `parser { ... }` block in .pint.hcl"
    m = re.search(rf'^\s*{key}\s*=\s*\[(.*?)\]', block.group(1), re.S | re.M)
    assert m, f"parser.{key} not found in .pint.hcl"
    return re.findall(r'"((?:[^"\\]|\\.)*)"', m.group(1))


def test_platform_configmap_is_in_scope_by_prefix_and_both_extensions():
    """The platform pack's DEPLOYED ConfigMap must be linted, matched like pytest.

    Two failure modes this pins down, both silently GREEN:

    * dropping the ConfigMap from `parser.include` reverts 16 platform alerts to
      being invisible to pint (6 of them with real alerts/template label-flow risk);
    * matching it by EXACT filename instead of prefix makes a future
      `configmap-rules-platform-federation.yaml` invisible to pint while the
      pytest-side scanners — which key off a PREFIX plus `(".yaml", ".yml")` — keep
      counting it, so the two sides disagree about what "the platform pack" is.

    The prefix/extension judgment is read back out of the pytest module rather than
    restated here, so the two cannot drift.
    """
    hcl = _read(".pint.hcl")
    include = _hcl_list(hcl, "include")
    relaxed = _hcl_list(hcl, "relaxed")

    ops = _read("tests/ops/test_generate_routes_orchestration.py")
    m_prefix = re.search(r'_PLATFORM_CM_PREFIX\s*=\s*"([^"]+)"', ops)
    assert m_prefix, "_PLATFORM_CM_PREFIX not found in the platform-pack scanner"
    prefix = m_prefix.group(1)
    m_exts = re.search(r'_RULES_FILE_EXTS\s*=\s*\(([^)]*)\)', ops)
    assert m_exts, "_RULES_FILE_EXTS not found in the platform-pack scanner"
    exts = re.findall(r'"\.(\w+)"', m_exts.group(1))
    assert sorted(exts) == ["yaml", "yml"], exts

    cm_patterns = [p for p in include if prefix in p]
    assert cm_patterns, (
        f"parser.include must cover the platform ConfigMap ({prefix}*) — without it "
        f"pint cannot see the 16 platform alerts that have no tests/rulepacks extract"
    )
    assert cm_patterns == [p for p in relaxed if prefix in p], (
        "the platform ConfigMap must appear in BOTH parser.include and parser.relaxed "
        "with the same pattern: include-without-relaxed makes pint treat the ConfigMap "
        "as a malformed rule document and emit a Fatal parse problem (CI red), because "
        "`relaxed` is the per-path opt-in for rules embedded in a `data:` block"
    )

    # pint auto-anchors these regexps (`X` is compiled as `^X$`), so the pattern must
    # itself allow a suffix after the prefix and either extension.
    for pat in cm_patterns:
        compiled = re.compile("^" + pat.replace("\\\\", "\\") + "$")
        for suffix in ("", "-federation"):
            for ext in exts:
                candidate = f"k8s/03-monitoring/{prefix}{suffix}.{ext}"
                assert compiled.match(candidate), (
                    f"{pat!r} does not match {candidate!r} — use a PREFIX + both "
                    f"extensions, not an exact filename, to stay aligned with "
                    f"_PLATFORM_CM_PREFIX / _RULES_FILE_EXTS"
                )
        # …but must NOT swallow the 16 GENERATED `DO NOT EDIT` sibling ConfigMaps:
        # linting those double-reports every rule-pack finding onto a file developers
        # are told not to edit (measured 689 entries vs 393, findings duplicated).
        for generated in ("configmap-rules-mariadb", "configmap-rules-kubernetes"):
            assert not compiled.match(f"k8s/03-monitoring/{generated}.yaml"), (
                f"{pat!r} must not match the generated copy {generated}.yaml"
            )


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
    # Sizing window, measured with pint 0.86.0: 393 total, of which 42 come from
    # configmap-rules-platform.yaml. Below 352 the floor cannot notice the platform
    # pack falling out of scope (the −42 drop it exists to catch); at/above 393 every
    # routine rule ADDITION would need this number bumped.
    assert 352 <= floor < 393, (
        f"_MIN_PINT_ENTRIES={floor} is outside the useful window [352, 393): too low "
        f"and losing the whole platform ConfigMap stays green, too high and adding a "
        f"rule turns CI red. Re-measure and re-centre it on a PINT_VERSION bump."
    )
