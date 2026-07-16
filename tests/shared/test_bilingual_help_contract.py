"""DA_LANG bilingual ``--help`` behavioral contract gate.

THE CONTRACT
------------
``docs/internal/dev-rules.md`` §9（i18n 三層架構）Layer 3 mandates: Python CLI
tools (``scripts/tools/**``) switch their argparse help strings via
``detect_cli_lang()`` (env order ``DA_LANG`` > ``LC_ALL`` > ``LANG``; explicit
``DA_LANG`` always wins — pinned by tests/shared/test_property_tools.py).
The observable form of that contract, per bilingual tool:

    DA_LANG=zh_TW.UTF-8 <tool> --help   → exit 0, output contains CJK
    DA_LANG=en_US.UTF-8 <tool> --help   → exit 0, output != the zh output

WHY THIS FILE EXISTS
--------------------
The dev-rules "da-tools 契約" family has three enforceable contracts. Two are
already behavioral gates: exit codes → ``test_tool_exit_codes.py``; ``--json``
stdout → ``test_json_stdout_contract.py`` (both dev-rules §13). The bilingual
help contract (§9 L3) had ZERO behavioral enforcement: the nominal check,
``scripts/tools/lint/check_i18n_coverage.py``, is a manual-stage soft-warn
COVERAGE REPORT — a string heuristic (does the source mention
``detect_cli_lang`` / ``_HELP``?) that never runs a tool, so a tool whose
wiring silently stopped switching stays "covered". This file is the
enforcement half; check_i18n_coverage stays as the complementary coverage
report (deliberately untouched).

SCOPE — corpus and its four-way partition
-----------------------------------------
Corpus = ``collect_tools()`` imported from ``test_tool_exit_codes.py``
(single source of truth — ops/dx/lint ``*.py``, no ``_``-prefixed libs).
Every tool is in exactly ONE bucket; a NEW tool that names itself in no
allowlist below lands in the BILINGUAL bucket by default and must therefore
either really implement bilingual help or add itself (with a reason) to an
allowlist — there is no silent escape (``test_partition_is_exact``).

* BILINGUAL (derived complement, 26 incl. 1 known-broken) — the behavioral
  assertions above run per tool.
* ``ENGLISH_ONLY`` (136) — dx convention: non-customer-facing internal tools
  may ship English-only help. RATCHET: shrink-only — the gate runs each one
  under ``DA_LANG=zh`` and turns RED the moment its help gains CJK, forcing
  the entry OUT of the allowlist and INTO the bilingual contract.
* ``CHINESE_ONLY_HELP`` (26) — help text is Chinese(-mixed) with NO
  ``detect_cli_lang`` wiring at all: single-language by construction, legal
  under the ZH-primary SSOT policy (dev-rules §9b) for internal tooling.
  RATCHET: the gate asserts the wiring stays absent — the moment one of
  these adopts ``detect_cli_lang`` it must graduate to BILINGUAL.
* ``KNOWN_BROKEN_BILINGUAL`` (1) — has the wiring but ``--help`` does not
  respond to it. Fixing is a behavior change owned by the PM, NOT this
  gate-only wave; entries are xfail(strict) so a fix forces de-listing.

COST DESIGN (why not a blind full-matrix sweep)
-----------------------------------------------
Subprocess budget = 2×|BILINGUAL| + 1×|ENGLISH_ONLY| + 1×|CHINESE_ONLY|
= 2×26 + 136 + 26 = 214 (vs 376 already spent by test_tool_exit_codes).
The allowlists are known-conclusion sets: one zh-help run suffices to verify
"still no CJK" / "still has CJK" — an en-side run there would prove nothing
this gate asserts. The CHINESE_ONLY wiring ratchet is a source-text check
(zero subprocesses).

HONEST BOUNDARIES
-----------------
* The en output is asserted *different from zh*, NOT CJK-free — bilingual
  content purity is a different lint's jurisdiction, not this gate's.
* Only ``--help`` is gated. Runtime-message i18n (several ENGLISH_ONLY tools
  wire ``detect_cli_lang`` for report strings only, e.g. operator_check.py,
  check_doc_template.py) is out of scope here.
* Windows hosts: children print CJK safely because tests/conftest.py forces
  ``PYTHONIOENCODING=utf-8`` into the inherited env (session autouse).
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

import pytest

from test_tool_exit_codes import collect_tools

ALL_TOOLS = collect_tools()
BY_NAME = {t.name: t for t in ALL_TOOLS}

# CJK Unified Ideographs (U+4E00–U+9FFF) — presence marks a zh help string.
CJK_RE = re.compile(r"[一-鿿]")

TIMEOUT_S = 20

ZH = "zh_TW.UTF-8"
EN = "en_US.UTF-8"


def _run_help(tool_name: str, lang: str) -> subprocess.CompletedProcess:
    """Run ``<tool> --help`` with DA_LANG=<lang>.

    ``detect_cli_lang`` early-returns on an explicit zh/en ``DA_LANG``
    prefix (it beats LC_ALL/LANG), so the host locale needs no scrubbing.
    PYTHONIOENCODING=utf-8 is inherited from the conftest session fixture.
    """
    env = dict(os.environ)
    env["DA_LANG"] = lang
    return subprocess.run(
        [sys.executable, str(BY_NAME[tool_name]), "--help"],
        capture_output=True, timeout=TIMEOUT_S, env=env,
    )


def _stdout(proc: subprocess.CompletedProcess) -> str:
    return proc.stdout.decode("utf-8", "replace")


# ═══════════════════════════════════════════════════════════════════════════
# ENGLISH_ONLY allowlist — name → one-line reason. Shrink-only ratchet.
# ═══════════════════════════════════════════════════════════════════════════
_R_OPS = ("ops tool authored with English-only help; bilingual wiring never "
          "added (coverage tracked by check_i18n_coverage soft-warn)")
_R_OPS_RT = ("detect_cli_lang wired for RUNTIME report strings only; "
             "--help body is English-only")
_R_DX = "dx internal tool (not customer-facing) — English-only per dx convention"
_R_LINT = "lint/CI gate (dev-internal) — English-only per dx convention"
_R_LINT_RT = ("lint tool with detect_cli_lang wired for runtime messages "
              "only; --help body is English-only")
_R_SELF = ("check_i18n_coverage itself — source contains 'detect_cli_lang' "
           "only as its scan pattern; its own help is English")

ENGLISH_ONLY: dict[str, str] = {
    # ── scripts/tools/ops ──────────────────────────────────────────────
    "analyze_rule_pack_gaps.py": _R_OPS,
    "assemble_config_dir.py": _R_OPS,
    "backtest_threshold.py": _R_OPS,
    "batch_diagnose.py": _R_OPS,
    "blast_radius.py": _R_OPS,
    "blind_spot_discovery.py": _R_OPS,
    "byo_check.py": _R_OPS,
    "config_diff.py": _R_OPS,
    "cutover_tenant.py": _R_OPS,
    "da_assembler.py": _R_OPS,
    "federation_check.py": _R_OPS,
    "federation_keygen.py": _R_OPS,
    "generate_alertmanager_routes.py": _R_OPS,
    "grafana_import.py": _R_OPS,
    "inject_metadata_join.py": _R_OPS,
    "lint_custom_rules.py": _R_OPS,
    "maintenance_scheduler.py": _R_OPS,
    "onboard_platform.py": _R_OPS,
    "operator_check.py": _R_OPS_RT,
    "patch_config.py": _R_OPS,
    "rule_pack_diff.py": _R_OPS,
    "shadow_verify.py": _R_OPS,
    "silencer_drift_check.py": _R_OPS,
    "state_reconcile.py": _R_OPS,
    # ── scripts/tools/dx ───────────────────────────────────────────────
    "add_frontmatter.py": _R_DX,
    "analyze_bench_history.py": _R_DX,
    "analyze_tier1_fp_rate.py": _R_DX,
    "axe_lite_static.py": _R_DX,
    "bump_playbook_versions.py": _R_DX,
    "check_aria_references.py": _R_DX,
    "compile_custom_alerts.py": _R_DX,
    "coverage_delta.py": _R_DX,
    "coverage_gap_analysis.py": _R_DX,
    "describe_tenant.py": _R_DX,
    "diag_pr_ci.py": _R_DX,
    "doc_coverage.py": _R_DX,
    "gen_recipe_status_json.py": _R_DX,
    "generate_alert_reference.py": _R_DX,
    "generate_changelog.py": _R_DX,
    "generate_nav.py": _R_DX,
    "generate_platform_data.py": _R_DX,
    "generate_rule_pack_readme.py": _R_DX,
    "generate_rulepack_configmaps.py": _R_DX,
    "generate_tenant_fixture.py": _R_DX,
    "generate_tenant_metadata.py": _R_DX,
    "migrate_conf_d.py": _R_DX,
    "render_soak_diff.py": _R_DX,
    "reword_chain.py": _R_DX,
    "run_chaos_soak.py": _R_DX,
    "scaffold_jsx_dep.py": _R_DX,
    "scaffold_lint.py": _R_DX,
    "suggest_related.py": _R_DX,
    "sync_glossary_abbr.py": _R_DX,
    "sync_schema.py": _R_DX,
    "sync_tool_registry.py": _R_DX,
    "tenant_verify.py": _R_DX,
    # ── scripts/tools/lint ─────────────────────────────────────────────
    "check_account_registry_monotonic.py": _R_LINT,
    "check_ad_hoc_git_scripts.py": _R_LINT,
    "check_admin_config_schema.py": _R_LINT,
    "check_bat_ascii_purity.py": _R_LINT,
    "check_bilingual_annotations.py": _R_LINT,
    "check_bilingual_content.py": _R_LINT,
    "check_changelog_no_tbd.py": _R_LINT,
    "check_cli_coverage.py": _R_LINT,
    "check_codename_gate.py": _R_LINT,
    "check_codename_leak.py": _R_LINT,
    "check_commit_scope_doc.py": _R_LINT,
    "check_confd_schema.py": _R_LINT,
    "check_configmap_mount_completeness.py": _R_LINT,
    "check_cross_ns_url_consistency.py": _R_LINT,
    "check_dev_bypass_manifest.py": _R_LINT,
    "check_dev_rules_enforcement.py": _R_LINT,
    "check_devrules_size.py": _R_LINT,
    "check_dist_source_consistency.py": _R_LINT,
    "check_doc_datools_cmds.py": _R_LINT,
    "check_doc_k8s_refs.py": _R_LINT,
    "check_doc_links.py": _R_LINT,
    "check_doc_reading_time.py": _R_LINT_RT,
    "check_doc_template.py": _R_LINT_RT,
    "check_flaky_registry.py": _R_LINT,
    "check_frontmatter_versions.py": _R_LINT,
    "check_glossary_coverage.py": _R_LINT,
    "check_ha_threshold_aggregation.py": _R_LINT,
    "check_hardcode_tenant.py": _R_LINT,
    "check_head_blob_hygiene.py": _R_LINT,
    "check_helm_values_secrets.py": _R_LINT,
    "check_hub_badge_drift.py": _R_LINT,
    "check_i18n_coverage.py": _R_SELF,
    "check_iac_helm.py": _R_LINT,
    "check_iac_vibe_rules.py": _R_LINT,
    "check_includes_sync.py": _R_LINT,
    "check_jsx_loader_compat.py": _R_LINT,
    "check_k8s_manifests.py": _R_LINT,
    "check_ksm_version_allowlist.py": _R_LINT,
    "check_leftouterjoin_enrichment.py": _R_LINT,
    "check_lint_toolchain_fit.py": _R_LINT,
    "check_log_egress_policy.py": _R_LINT,
    "check_maintenance_symmetry.py": _R_LINT,
    "check_md_yaml_drift.py": _R_LINT,
    "check_open_encoding.py": _R_LINT,
    "check_orphan_docs.py": _R_LINT,
    "check_orphan_lint.py": _R_LINT,
    "check_path_metadata_consistency.py": _R_LINT,
    "check_pint.py": _R_LINT,
    "check_planning_status_sync.py": _R_LINT,
    "check_playwright_rtl_drift.py": _R_LINT,
    "check_portal_audience_enum.py": _R_LINT,
    "check_portal_bundle_size.py": _R_LINT,
    "check_portal_i18n.py": _R_LINT,
    "check_pr_scope_drift.py": _R_LINT,
    "check_property_coverage.py": _R_LINT,
    "check_repo_name.py": _R_LINT,
    "check_retire_drift.py": _R_LINT,
    "check_routing_profiles.py": _R_LINT_RT,
    "check_rulepack_sync.py": _R_LINT,
    "check_session_guard_liveness.py": _R_LINT,
    "check_single_writer_invariant.py": _R_LINT,
    "check_skip_a11y_justification.py": _R_LINT,
    "check_structure.py": _R_LINT,
    "check_subprocess_timeout.py": _R_LINT,
    "check_threshold_observed_map.py": _R_LINT,
    "check_tool_registry_jsx_parity.py": _R_LINT,
    "check_translation.py": _R_LINT,
    "check_undefined_tokens.py": _R_LINT,
    "check_vmalert_coverage.py": _R_LINT,
    "check_window_x_no_fallback.py": _R_LINT,
    "check_workflow_git_push_permissions.py": _R_LINT,
    "detect_sed_damage.py": _R_LINT,
    "fix_doc_links.py": _R_LINT,
    "fix_file_hygiene.py": _R_LINT,
    "lint_html_doc_links.py": _R_LINT,
    "lint_jsx_babel.py": _R_LINT,
    "lint_tool_consistency.py": _R_LINT,
    "trufflehog_to_sarif.py": _R_LINT,
    "validate_mermaid.py": _R_LINT,
    "validate_planning_session_row.py": _R_LINT,
}

# ═══════════════════════════════════════════════════════════════════════════
# CHINESE_ONLY_HELP — CJK help, NO detect_cli_lang wiring (single-language by
# construction; legal for internal tooling under ZH-primary SSOT, §9b).
# Graduation path: adopting detect_cli_lang turns the wiring ratchet red →
# remove the entry here, the tool then falls into BILINGUAL enforcement.
# ═══════════════════════════════════════════════════════════════════════════
_R_ZH = ("Chinese-only help, no detect_cli_lang wiring — internal tool under "
         "ZH-primary SSOT (dev-rules §9b)")

CHINESE_ONLY_HELP: dict[str, str] = {
    # ── scripts/tools/ops ──────────────────────────────────────────────
    "baseline_discovery.py": _R_ZH,
    "check_alert.py": _R_ZH,
    "deprecate_rule.py": _R_ZH,
    "migrate_rule.py": _R_ZH,
    "offboard_tenant.py": _R_ZH,
    "validate_migration.py": _R_ZH,
    # ── scripts/tools/dx ───────────────────────────────────────────────
    "bump_docs.py": _R_ZH,
    "doc_impact.py": _R_ZH,
    "generate_doc_map.py": _R_ZH,
    "generate_rule_pack_stats.py": _R_ZH,
    "generate_tool_map.py": _R_ZH,
    "inject_related_docs.py": _R_ZH,
    "inject_waveform.py": _R_ZH,
    "migrate_ssot_language.py": _R_ZH,
    "pr_preflight.py": _R_ZH,
    "scan_component_health.py": _R_ZH,
    "waveform_compile.py": _R_ZH,
    "waveform_score.py": _R_ZH,
    # ── scripts/tools/lint ─────────────────────────────────────────────
    "check_bilingual_structure.py": _R_ZH,
    "check_build_completeness.py": _R_ZH,
    "check_design_token_usage.py": _R_ZH,
    "check_jsx_i18n.py": _R_ZH,
    "check_makefile_targets.py": _R_ZH,
    "check_metric_dictionary.py": _R_ZH,
    "check_playbook_freshness.py": _R_ZH,
    "validate_docs_versions.py": _R_ZH,
}

# ═══════════════════════════════════════════════════════════════════════════
# KNOWN_BROKEN_BILINGUAL — real wiring bugs, DELIBERATELY NOT FIXED in the
# gate-landing wave (fixing help output is a behavior change owned by the
# PM). xfail(strict): once fixed, the XPASS turns this gate red and forces
# the entry's removal — the list can only shrink.
# ═══════════════════════════════════════════════════════════════════════════
KNOWN_BROKEN_BILINGUAL: dict[str, str] = {
    "runtime_audit.py": (
        "has detect_cli_lang wiring, but only for RUNTIME report strings "
        "(runtime_audit.py:306 self.lang, :372-395 i18n(...) prints); the "
        "argparse help strings are hardcoded zh/en-mixed, so "
        "DA_LANG=zh_TW.UTF-8 and DA_LANG=en_US.UTF-8 --help outputs are "
        "byte-identical (and CJK-bearing). TODO: route the help strings "
        "through i18n() — behavior change, needs an owner decision; on fix, "
        "delete this entry."
    ),
}

# Derived complement: everything not explicitly allowlisted must behave
# bilingually. A brand-new tool lands here by default — no silent escape.
BILINGUAL = sorted(
    n for n in BY_NAME
    if n not in ENGLISH_ONLY and n not in CHINESE_ONLY_HELP
)

_BILINGUAL_PARAMS = [
    pytest.param(
        n,
        marks=pytest.mark.xfail(reason=KNOWN_BROKEN_BILINGUAL[n], strict=True),
    ) if n in KNOWN_BROKEN_BILINGUAL else n
    for n in BILINGUAL
]


# ═══════════════════════════════════════════════════════════════════════════
# Meta-tests — keep the partition honest
# ═══════════════════════════════════════════════════════════════════════════
def test_no_duplicate_tool_basenames():
    """The partition is keyed by basename; a collision would make it ambiguous."""
    names = [t.name for t in ALL_TOOLS]
    dupes = sorted({n for n in names if names.count(n) > 1})
    assert not dupes, (
        f"duplicate tool basenames across ops/dx/lint break name-keyed "
        f"classification: {dupes}"
    )


def test_partition_is_exact():
    """allowlists ∪ derived-bilingual == corpus, pairwise disjoint, no stale names.

    This is the no-silent-escape guarantee: a new tool that is in no
    allowlist is BILINGUAL by definition and immediately subject to the
    behavioral assertions below.
    """
    corpus = set(BY_NAME)
    for label, listed in (
        ("ENGLISH_ONLY", ENGLISH_ONLY),
        ("CHINESE_ONLY_HELP", CHINESE_ONLY_HELP),
        ("KNOWN_BROKEN_BILINGUAL", KNOWN_BROKEN_BILINGUAL),
    ):
        stale = sorted(set(listed) - corpus)
        assert not stale, (
            f"{label} names tool(s) that no longer exist "
            f"(renamed/removed — clean the list): {stale}"
        )

    overlap = sorted(set(ENGLISH_ONLY) & set(CHINESE_ONLY_HELP))
    assert not overlap, f"tool(s) in BOTH allowlists (pick one): {overlap}"

    misplaced = sorted(
        set(KNOWN_BROKEN_BILINGUAL)
        & (set(ENGLISH_ONLY) | set(CHINESE_ONLY_HELP))
    )
    assert not misplaced, (
        f"KNOWN_BROKEN_BILINGUAL entries must NOT also be allowlisted "
        f"(they are broken *bilingual* tools): {misplaced}"
    )

    assert BILINGUAL, "derived bilingual set is empty — partition is broken"
    # Set identity: derived ∪ allowlists == corpus (tautological given the
    # derivation, asserted anyway as a guard against future refactors).
    assert set(BILINGUAL) | set(ENGLISH_ONLY) | set(CHINESE_ONLY_HELP) == corpus


def test_allowlists_shrink_only_count_pin():
    """Count pin：讓「shrink-only」成為機械保證而非慣例（W5 盲審 F1）。

    沒有這個 pin，新工具作者把純英文 help 的工具直接加進 ENGLISH_ONLY
    就能靜默繞過雙語 enforcement（gate 全綠、名單無聲成長）。pin 之後
    任何成長都必須顯式 bump 這裡的數字——這正是想要的 review 摩擦；
    縮減（工具畢業成雙語）不需 bump。
    """
    assert len(ENGLISH_ONLY) <= 136, (
        f"ENGLISH_ONLY grew to {len(ENGLISH_ONLY)} (pin=136). Adding an "
        "English-only tool is allowed but must be an explicit, reviewed "
        "decision — bump this pin in the same commit and justify it."
    )
    assert len(CHINESE_ONLY_HELP) <= 26, (
        f"CHINESE_ONLY_HELP grew to {len(CHINESE_ONLY_HELP)} (pin=26). "
        "New tools should be bilingual (dev-rules §9); bump only with "
        "explicit justification."
    )
    assert len(KNOWN_BROKEN_BILINGUAL) <= 1, (
        f"KNOWN_BROKEN_BILINGUAL grew to {len(KNOWN_BROKEN_BILINGUAL)} "
        "(pin=1). This list is a repair queue, not an escape hatch — "
        "fix the tool instead of listing it."
    )


# ═══════════════════════════════════════════════════════════════════════════
# The gate — bilingual tools must actually switch on DA_LANG
# ═══════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("tool_name", _BILINGUAL_PARAMS)
def test_bilingual_help_responds_to_da_lang(tool_name):
    """DA_LANG=zh help carries CJK; DA_LANG=en help differs from it."""
    zh_proc = _run_help(tool_name, ZH)
    assert zh_proc.returncode == 0, (
        f"{tool_name}: --help under DA_LANG={ZH} exited "
        f"{zh_proc.returncode}\nstderr: "
        f"{zh_proc.stderr.decode('utf-8', 'replace')[:300]}"
    )
    zh_out = _stdout(zh_proc)
    assert CJK_RE.search(zh_out), (
        f"{tool_name}: DA_LANG={ZH} --help contains no CJK — the bilingual "
        f"contract (dev-rules §9 L3) is not implemented. Either wire "
        f"detect_cli_lang() into the help strings, or explicitly allowlist "
        f"the tool in ENGLISH_ONLY with a one-line reason.\n"
        f"stdout[:200]: {zh_out[:200]!r}"
    )

    en_proc = _run_help(tool_name, EN)
    assert en_proc.returncode == 0, (
        f"{tool_name}: --help under DA_LANG={EN} exited "
        f"{en_proc.returncode}\nstderr: "
        f"{en_proc.stderr.decode('utf-8', 'replace')[:300]}"
    )
    en_out = _stdout(en_proc)
    assert en_out != zh_out, (
        f"{tool_name}: --help output is IDENTICAL under DA_LANG=zh and "
        f"DA_LANG=en — detect_cli_lang() wiring exists in name only (or was "
        f"silently disconnected). If this is a newly-discovered wiring bug, "
        f"register it in KNOWN_BROKEN_BILINGUAL with a phenomenon "
        f"description; do not allowlist a bilingual tool as English-only."
    )


@pytest.mark.parametrize("tool_name", sorted(ENGLISH_ONLY))
def test_english_only_allowlist_ratchet(tool_name):
    """Shrink-only ratchet: an allowlisted tool whose help gains CJK must
    graduate OUT of ENGLISH_ONLY and into the bilingual contract."""
    proc = _run_help(tool_name, ZH)
    assert proc.returncode == 0, (
        f"{tool_name}: --help under DA_LANG={ZH} exited {proc.returncode}\n"
        f"stderr: {proc.stderr.decode('utf-8', 'replace')[:300]}"
    )
    out = _stdout(proc)
    assert not CJK_RE.search(out), (
        f"{tool_name} is allowlisted ENGLISH_ONLY but its zh --help now "
        f"contains CJK — it has (at least partially) gone bilingual. Remove "
        f"it from ENGLISH_ONLY so the behavioral contract applies "
        f"(allowlist only ever shrinks).\n"
        f"first CJK context: "
        f"{out[max(0, CJK_RE.search(out).start() - 40):CJK_RE.search(out).start() + 40]!r}"
    )


@pytest.mark.parametrize("tool_name", sorted(CHINESE_ONLY_HELP))
def test_chinese_only_help_stays_unwired(tool_name):
    """Chinese-only tools: help really is CJK, and the tool stays wiring-free.

    The wiring check is the graduation tripwire: the moment one of these
    imports detect_cli_lang it claims to be bilingual — move it to the
    BILINGUAL bucket (delete its entry here) so the behavioral contract
    applies. Source check costs zero subprocesses.
    """
    source = BY_NAME[tool_name].read_text(encoding="utf-8")
    assert "detect_cli_lang" not in source, (
        f"{tool_name} is registered CHINESE_ONLY_HELP but now references "
        f"detect_cli_lang — it claims bilingual support. Remove it from "
        f"CHINESE_ONLY_HELP so test_bilingual_help_responds_to_da_lang "
        f"enforces the real behavior."
    )
    proc = _run_help(tool_name, ZH)
    assert proc.returncode == 0, (
        f"{tool_name}: --help under DA_LANG={ZH} exited {proc.returncode}\n"
        f"stderr: {proc.stderr.decode('utf-8', 'replace')[:300]}"
    )
    assert CJK_RE.search(_stdout(proc)), (
        f"{tool_name} is registered CHINESE_ONLY_HELP but its help contains "
        f"no CJK anymore — reclassify it (ENGLISH_ONLY with a reason, or "
        f"bilingual)."
    )
