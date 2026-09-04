"""_version_patterns.py — Version pattern registry for validate_docs_versions.py

This module centralizes all version pattern definitions, file paths, and scan
configurations that were previously embedded in validate_docs_versions.py.

Extracted in v2.4.0 Phase B to:
  - Improve maintainability by separating data from logic
  - Enable reuse across multiple validation tools
  - Reduce cognitive load in the main checker module
"""
import re
from pathlib import Path
from typing import Tuple, List, Dict, Any

# ============================================================================
# Repo root detection
# ============================================================================
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent.parent

# ============================================================================
# Source-of-truth files (paths to read version info and counts from)
# ============================================================================
CHART_YAML = REPO_ROOT / "helm" / "threshold-exporter" / "Chart.yaml"
DA_TOOLS_VERSION = REPO_ROOT / "components" / "da-tools" / "app" / "VERSION"
CLAUDE_MD = REPO_ROOT / "CLAUDE.md"
RULE_PACKS_DIR = REPO_ROOT / "rule-packs"
K8S_RULES_DIR = REPO_ROOT / "k8s" / "03-monitoring"
DOCS_DIR = REPO_ROOT / "docs"

# ============================================================================
# File scan configuration: which directories and patterns to scan
# ============================================================================

# Extensions to scan for version references
SCANNABLE_EXTENSIONS: Tuple[str, ...] = (".md", ".jsx", ".json")

# Directories to scan (used by _collect_scannable_files)
SCAN_DIRECTORIES = {
    "docs": DOCS_DIR,
    "root": REPO_ROOT,
    "components": REPO_ROOT / "components",
    "ci": [
        REPO_ROOT / ".github",
        REPO_ROOT / ".gitlab",
    ],
    "k8s": REPO_ROOT / "k8s",
}

# Root files to include in scans
ROOT_FILES = ("README.md", "README.en.md", "CLAUDE.md", "mkdocs.yml")

# E2E and JSX version checks: additional files to scan for version references
E2E_PACKAGE_JSON = REPO_ROOT / "tests" / "e2e" / "package.json"
JSX_VERSION_FILES = list((DOCS_DIR / "interactive" / "tools").glob("*.jsx")) if (DOCS_DIR / "interactive" / "tools").exists() else []

# ============================================================================
# Pattern definitions for each type of version/count check
# ============================================================================

# da-tools image tag pattern
DA_TOOLS_TAG_PATTERN = r"da-tools:v?([0-9]+\.[0-9]+\.[0-9]+)"

# Exporter version patterns: (regex, description)
EXPORTER_VERSION_PATTERNS: List[Tuple[str, str]] = [
    (r"threshold-exporter:v?([0-9]+\.[0-9]+\.[0-9]+)", "image tag"),
    (r"charts/threshold-exporter --version ([0-9]+\.[0-9]+\.[0-9]+)",
     "OCI chart version"),
    (r"charts/threshold-exporter:([0-9]+\.[0-9]+\.[0-9]+)",
     "OCI chart inline version"),
]

# Platform version in frontmatter
PLATFORM_VERSION_FRONTMATTER_PATTERN = r"^version:\s*v?([0-9]+\.[0-9]+[^\s]*)"

# Docker image bare tag pattern (missing v-prefix).
# Covers all 4 published component images (da-tools / threshold-exporter /
# tenant-api / da-portal). The `(?<!charts/)` lookbehind excludes OCI *chart*
# refs (`charts/<name>:<ver>`), which correctly use bare SemVer.
BARE_TAG_PATTERN = (
    r"(?<!charts/)(?:da-tools|threshold-exporter|tenant-api|da-portal)"
    r":(\d+\.\d+\.\d+)"
)

# Rule Pack count patterns: (regex, group_index, expected_value, description)
# Note: group_index=None means special handling (multi-group)
RULE_PACK_COUNT_PATTERNS: List[Tuple[str, Any, Any, str]] = [
    (r"(\d+)\s*個\s*Rule\s*Pack", 1, None, "Rule Pack count (zh)"),
    (r"(\d+)\s*Rule\s*Pack\s*ConfigMap", 1, None,
     "Rule Pack ConfigMap count"),
    (r"rule%20packs-(\d+)-", 1, None, "Rule Pack badge"),
    (r"alerts-(\d+)-", 1, None, "Alert badge"),
    (r"\*\*合計\*\*.*\*\*(\d+)\*\*.*\*\*(\d+)\*\*", None, None,
     "Rule Pack total row"),
]

# The scope the counted sentence states, verbatim and in both languages.
#
# ⛔ #1540: this is the anchor, and the number is only the payload. A tool
# count is a claim ABOUT A SCOPE; `check_tool_count_in_docs` therefore only
# reads lines that carry this string, and `_auto_fix` only rewrites the line
# the checker named.
#
# Blind review earned this the hard way. Without the anchor, the checker asks
# "is there an `N Python tools` on this line", which is true of sentences that
# are about something else entirely — measured on this tree, adding the true
# sentence `The try-local/ showcase bundle ships 2 Python tools of its own.`
# to README.en.md produced `found 2, actual is 221`, and `--fix` rewrote it to
# `ships 221 Python tools of its own` and then printed
# `✅ All version references and counts are consistent.` A repair that turns a
# true sentence into a false one, and reports success.
#
# ⚠️ That hazard is older than the English half: the Chinese pattern has
# always been noun-anchored, and the same probe in Chinese
# (`另外附 2 個 Python 工具`) was rewritten to 221 on `origin/main` too. The
# anchor closes both, plus the whole-file/per-line split below.
TOOL_COUNT_SCOPE_ANCHOR = "`scripts/tools/{ops,dx,lint}`"

# Tool count patterns: (regex, description)
#
# ⛔ #1540: the English half used to be two patterns that each demanded a
# particular word right after the noun — `tools(` and `tools in`.
# `README.en.md` says "221 Python tools **under** `scripts/tools/{ops,dx,lint}`",
# so both matched nothing. Measured on `5b7f6c35`: each scored 0 hits across
# all three of TOOL_COUNT_CHECK_FILES, while `bump_docs` kept writing that
# number via its own rule — so rewriting the English number to 999 produced 0
# `tool-count` findings. (⚠️ rc stayed 0 either way: `tool-count` is a
# warning, so the exit code was never the signal here.)
#
# ⛔ The repair is NOT a third spelling with `under` in it; the next
# preposition would drift out the same way. A preposition carries no counting
# information, so it cannot be what identifies the claim — the SCOPE is, and
# that is now matched separately by TOOL_COUNT_SCOPE_ANCHOR.
#
# ⚠️ `check_tool_count_in_docs` matches these case-insensitively, and
# `AUTO_FIX_PATTERNS["tool-count"]` has to stay in step in both spelling and
# flags: a form the checker sees but the repair cannot rewrite is a warning
# nobody can clear (#1504).
TOOL_COUNT_PATTERNS: List[Tuple[str, str]] = [
    (r"(\d+)\s*個\s*Python\s*工具", "Python tool count (zh)"),
    (r"(\d+)\s*Python\s*tools?\b", "Python tool count (en)"),
]

# ADR count pattern
ADR_COUNT_PATTERNS: List[Tuple[str, str]] = [
    (r"(\d+)\s*ADRs?\b", "ADR count"),
]

# Document file count pattern
DOC_FILE_COUNT_PATTERNS: List[Tuple[str, str]] = [
    (r"(\d+)\s*個文件", "doc file count (zh)"),
]

# Scenario count pattern
SCENARIO_COUNT_PATTERNS: List[Tuple[str, str]] = [
    (r"(\d+)\s*場景", "scenario count (zh)"),
]

# Bilingual pair detection
BILINGUAL_PAIR_PATTERN = r"bilingual-(\d+)%20pairs"

# Bilingual number consistency patterns: (regex, description)
#
# ⛔ These are matched with `re.IGNORECASE` against WHOLE FILES, so a pattern
# that is merely "reasonable" collects section numbers, version numbers and
# PromQL identifiers as if they were counts. Measured on `a807a41c` over the
# 94 bilingual pairs (564 cells), the `Alert rule count` entry below used to
# read `(\d+)\s*Alert(?:\s+rule)?` and produced:
# ⚠️ Cell totals below are quoted only for the Alert pattern. The
#    whole-table figures an earlier revision also quoted (519 SILENT /
#    30 AGREE) are a WINDOWS reading: `docs/README-root.{md,en.md}` are
#    mode-120000 symlinks, so a checkout without symlink support sees a
#    path string with no numbers in it and four cells read SILENT that
#    read AGREE on Linux/CI (515/34 there). The numbers this comment
#    actually rests on — 94 pairs, 564 cells, MISMATCH 4, AGREE 5 — are
#    identical under both.
# ⚠️ 94 pairs / 564 cells is the population AS MEASURED ON `a807a41c`, not
#    the population today: this same change adds `README-root.md` to
#    SKIP_BILINGUAL_NUMBER_FILES, so the loop now walks 92 pairs (552
#    cells). The number is left as measured and anchored rather than
#    rewritten — rewriting it would silently re-date a measurement nobody
#    re-ran.
#
#   MISMATCH 4  — four permanent warnings nobody could clear. What made
#                 each cell DIFFER, measured per cell rather than listed
#                 from memory (an earlier revision of this comment named
#                 four fragments that map onto only three of them):
#                   cli-reference          zh `v2 alert` vs en `122 alerts`
#                   synthetic-probe        en-only `ADR-025 Alerting-Plane`
#                   troubleshooting-check  zh-only `v1 alertname`
#                   multi-system-playbook  en-only `7 alerts`
#                 ⚠️ `### 1.2 Alert` matched BOTH halves and so drove no
#                 warning at all; and the last cell's `7 alerts` is a REAL
#                 count — what was noise there was the Chinese half's
#                 `Phase 2 ALERTS{}`, which is why the lookahead below is
#                 load bearing.
#   AGREE    5  — of which exactly ONE was a real count comparison
#                 (`docs/design/rule-packs.md`: 4 / 8 / 9 alerts). The other
#                 four agreed by coincidence on section numbers (`2.9 Alert
#                 Routing`, `2.3 Alertmanager`) and version numbers
#                 (`v2.9.0 alert-quality`, `v2 alertname`).
#
# The narrowed form keeps that one real comparison and drops all four false
# warnings, adding no new ones. Each element earns its place by measurement:
#   * PLURAL noun — `alerts`, never bare `Alert`. Kills the section headings,
#     `alertname`, and `v2.9.0 alert-quality` in one move.
#   * `(?!\s*\{)` — the PromQL series selector `ALERTS{...}` is an identifier,
#     not a count. Without this exception one MISMATCH survives, so it is load
#     bearing, and it must apply to BOTH halves: an earlier attempt hung it on
#     the English side only and manufactured a regression.
#   * `(?<![\d.])` — refuses the `2` in `### 1.2 Alerts don't fire`: the digit
#     is preceded by a `.`, so a DOTTED section number cannot be read as a
#     count.
#     ⛔ It does NOT refuse an INTEGER heading. `## 1 Alerts` matches, because
#     the `1` is preceded by a space — measured, and reported by review after
#     an earlier revision of this line described the guard as refusing
#     "headings", which is wider than what it does. A pair such as
#     `## 1 Alerts` / `## 2 Alerts` would produce a false mismatch.
#     ⚠️ Left open deliberately: the corpus has ZERO integer headings of that
#     shape today, and closing it means teaching the CONSUMER to skip heading
#     lines (it currently matches against whole files), which is a bigger
#     change than the false positive it would prevent. Disclosed, not fixed.
#
#   * the `[個條支]` branch — Chinese states the same count as `N 條 alert` /
#     `N 個 告警`, mirroring the `個?` the sister `Rule Pack count` pattern
#     above has carried for far longer. Without it the Chinese half is the
#     empty set on every such line, and a check that needs BOTH halves cannot
#     speak. Measured: it takes the live comparisons from 1 to 3 (adding
#     `cli-reference` 122 and `multi-system-migration-playbook` 7) with zero
#     new mismatches. ⛔ The measure word is REQUIRED on that branch — making
#     it optional pulls `v2 alert` back in.
#     ⛔ A `告警` (the Chinese noun) branch was implemented and then
#     REMOVED. Measured over the whole corpus it added exactly ONE hit,
#     and that hit was an ORDINAL: `第 4 條告警 FederationRevo…` in
#     `docs/adr/028-…` names the 4th rule, it does not count four of
#     them. There is no test for that branch that is not also a test for
#     that false positive — which is the same shape as the `[個條筆則項]`
#     widening rejected earlier on this ticket.
#
# ⚠️ Honest boundary, measured rather than assumed. This check only speaks when
# BOTH halves match, so a count only one half states stays silent.
# ⛔ An earlier revision of this comment said the four one-sided cells were
# "a real asymmetry in the prose, not a drift", and called fixing them a
# product decision. That was FALSE and it is worth recording why, because the
# wording talked the next reader out of the actual fix: all four Chinese halves
# DID state the same number, as `122 條 alert`, `7 條 alert`, `其餘 40 條`,
# `40 條平台告警裡有 37 條`. The prose was symmetric; the PATTERN was not.
# Measured at the time: injecting a real drift into the Chinese half of any of
# those four produced zero warnings under BOTH the old and the new pattern.
# ⚠️ Two of the four are still silent after the branch above, and deliberately
# so: `其餘 **40 條**落在…` and `40 條平台告警` put a modifier between the
# measure word and the noun, and widening far enough to cross that is how the
# false positives this narrowing removed would come back. That is a disclosed
# gap, not a settled question.
BILINGUAL_NUMBER_PATTERNS: List[Tuple[str, str]] = [
    (r"(\d+)\s*個?\s*Rule\s*Pack", "Rule Pack count"),
    (r"(\d+)\s*Recording", "Recording rule count"),
    (r"(?<![\d.])(\d+)(?:\s+alert(?:\s+rule)?s"
     r"|\s*[個條支]\s*alert(?:\s+rule)?s?"
     r")\b(?!\s*\{)", "Alert rule count"),
    (r"rule%20packs-(\d+)-", "Rule Pack badge"),
    (r"alerts-(\d+)-", "Alert badge"),
    (r"bilingual-(\d+)", "Bilingual badge"),
]

# ============================================================================
# Source-of-truth pattern extraction
# ============================================================================

# Platform version extraction from CLAUDE.md — REMOVED in #1480.
#
# This was a second copy of the lookup, pinned to the pre-v2.6.0 inline
# spelling; it silently stopped matching for five releases while its only
# consumer kept exiting 0. ⛔ Do not reintroduce it: the reader is
# `_lib_versions.read_platform_version`, which the doc-map / tool-map
# generators and this module now share. Details in issue #1480.

# da-tools version extraction from VERSION file
DA_TOOLS_VERSION_PATTERN = r"^[0-9]+\.[0-9]+\.[0-9]+"

# Exporter version extraction from Chart.yaml
EXPORTER_VERSION_PATTERN = r'^appVersion:\s*"([0-9]+\.[0-9]+\.[0-9]+[^"]*)"'

# mkdocs.yml extra version checks: (key_name, version_source_key)
MKDOCS_EXTRA_CHECKS: List[Tuple[str, str]] = [
    ("platform_version", "platform"),
    ("exporter_version", "exporter"),
    ("tools_version", "tools"),
]

# ============================================================================
# Files to skip in various scans
# ============================================================================

# Release workflows with CI variable interpolation
SKIP_CI_INTERPOLATION_FILES = {"release.yaml"}

# Rule pack count checks skip these files (historical references)
SKIP_RULE_PACK_FILES = {"CHANGELOG.md", "CHANGELOG.en.md", "benchmarks.md",
                        "benchmarks.en.md"}

# ⛔ These three `docs/` entries are mode-120000 symlinks to files OUTSIDE
# `docs/` (`git ls-files -s docs/` shows the modes). They are ALIASES, not
# documents. Any reader that COUNTS or PAIRS documents must skip them or the
# same document is seen twice.
#
# ⛔ Do NOT bolt this exclusion onto a reader that merely scans text (links,
# frontmatter, templates, reading time) just because it visits an alias twice:
# there, visiting twice has no consequence and narrowing the scan would remove
# real coverage. A text-side reader that skips these anyway needs its own
# reason stated at its own use site — `dx/add_frontmatter.py` has one (never
# write THROUGH a symlink; dev-rules #11), which is why it keeps a separate
# list instead of importing from here. Two lists with two different reasons
# are not the duplication this constant exists to remove.
#
# ⚠️ TWO VIEWS, ONE LITERAL. The `docs/`-relative paths are primary because a
# path identifies an alias and a bare NAME does not: `CHANGELOG.md` is also a
# real document at the repo root, so a reader whose scan set includes the root
# (`dx/doc_coverage.py`, whose `root_md_files` carries it) would exclude the
# genuine article if it matched on names — which is why the name view is the
# derived one and why "just compare names" is the wrong fix here.
# Readers that only ever walk `docs/` (`bump_docs._count_docs`,
# `validate_docs_versions.count_bilingual_pairs`) take the name view.
#
# ⚠️ Deliberately a literal set rather than `Path.is_symlink()`: a checkout
# without symlink support materialises these as 12-byte path stubs, so the
# derived test answers differently per platform — which is the exact platform
# dependence this set exists to remove (a Windows census read all-SILENT where
# CI read four AGREE cells).
#
# ⚠️ `DOC_MAP_SKIP_NAMES` below lists `README-root.md` but NOT
# `README-root.en.md`. Measured: `check_doc_map_coverage` reports 0 issues
# either way today, so that asymmetry is latent, not active — left alone
# rather than "tidied", because adding a name there narrows a scan with no
# measured defect behind it.
DOCS_TREE_SYMLINK_ALIAS_PATHS = {"docs/CHANGELOG.md", "docs/README-root.md",
                                 "docs/README-root.en.md"}
DOCS_TREE_SYMLINK_ALIASES = {p.rsplit("/", 1)[-1]
                             for p in DOCS_TREE_SYMLINK_ALIAS_PATHS}

# Bilingual number consistency skips the aliases (see above) plus
# `benchmarks.md`, which is skipped for an unrelated reason: it is a table of
# raw measurements where the two halves legitimately carry different numbers.
# ⚠️ Measured: folding in the alias set is inert for this check —
# `README-root.en.md` is never a zh-side file, so the scanned set is 92 zh
# documents either way. The point is to stop spelling the alias list twice.
SKIP_BILINGUAL_NUMBER_FILES = {"benchmarks.md"} | DOCS_TREE_SYMLINK_ALIASES

# doc-map coverage check skips these directories and files.
#
# `internal` is skipped (issue #66 follow-up; mirrors generate_doc_map.py
# SKIP_DIRS): docs/internal/** are explicitly out-of-scope for the public
# doc catalog. Validator must not flag missing-from-doc-map for internal
# files; otherwise generator and validator disagree and produce false
# drift (the v2.8.0-{planning-archive,tech-debt-decomposition}.md cases
# observed during PR #72 review).
DOC_MAP_SKIP_DIRS = {"includes", "adr", "design-reviews", "internal"}
DOC_MAP_SKIP_NAMES = {"tags.md", "CHANGELOG.md", "README-root.md",
                      "doc-map.md", "tool-map.md",
                      "known-regressions.md"}

# doc-map coverage also skips gitignored planning / draft files (see .gitignore).
# These patterns match filenames (not full paths) to keep the lint fast.
DOC_MAP_SKIP_NAME_PATTERNS = (
    re.compile(r"^v[0-9][^/]*-planning\.md$"),
    re.compile(r"^v[0-9][^/]*-day[0-9]+-.*\.md$"),
    re.compile(r".*-plan-draft\.md$"),
    re.compile(r"^_project-structure-audit-.*\.md$"),
)

# ⛔ #1511: "which files are tools" and "which subdirectories the counted
# sentence covers" used to live here as `TOOL_MAP_SKIP_PREFIXES` and
# `TOOL_COUNT_SUBDIRS`, with two more copies in `bump_docs` and
# `generate_tool_map`. They now have one home — `scripts/tools/_lib_toolcount.py`
# — because this is a pattern registry for one checker, and the predicate is
# shared by a checker and two writers.

# ============================================================================
# Roadmap/changelog overlap detection
# ============================================================================

# Roadmap sections to scan: (filepath, section_start_pattern, description)
ROADMAP_SECTIONS: List[Tuple[Path, str, str]] = [
    (DOCS_DIR / "architecture-and-design.md",
     r"^## 5\.\s*未來擴展路線",
     "architecture-and-design.md §5"),
    (DOCS_DIR / "architecture-and-design.en.md",
     r"^## 5\.\s*Future",
     "architecture-and-design.en.md §5"),
    (REPO_ROOT / "CLAUDE.md",
     r"^## 長期展望",
     "CLAUDE.md 長期展望"),
]

# Feature headings to skip when extracting completed items
SKIP_FEATURE_HEADINGS = {"版號", "Breaking Changes", "Key Changes",
                         "Documentation Overhaul", "文件大重構"}

# ============================================================================
# File collections for various checks
# ============================================================================

# Files to check for tool counts
TOOL_COUNT_CHECK_FILES = [
    REPO_ROOT / "CLAUDE.md",
    REPO_ROOT / "README.md",
    REPO_ROOT / "README.en.md",
]

# Files to check for ADR counts
ADR_COUNT_CHECK_FILES = [
    REPO_ROOT / "CLAUDE.md",
    REPO_ROOT / "README.md",
    REPO_ROOT / "README.en.md",
    REPO_ROOT / "docs" / "adr" / "README.md",
    REPO_ROOT / "docs" / "adr" / "README.en.md",
]

# Files to check for rule pack counts
RULE_PACK_COUNT_CHECK_FILES = [
    REPO_ROOT / "README.md",
    REPO_ROOT / "README.en.md",
]

# Bilingual badge files
BILINGUAL_BADGE_CHECK_FILES = [
    REPO_ROOT / "README.md",
    REPO_ROOT / "README.en.md",
]

# ============================================================================
# Auto-fix patterns (for --fix mode)
# ============================================================================

# Auto-fixable issue types and their fix patterns
AUTO_FIX_PATTERNS: Dict[str, Dict[str, Any]] = {
    "bilingual-count": {
        "pattern": r"bilingual-\d+%20pairs",
        "replacement_template": "bilingual-{value}%20pairs",
    },
    # ⛔ #1540: one entry per TOOL_COUNT_PATTERNS entry. The English half had
    # no repair at all, so even once the checker could see a drifted English
    # number, `--fix` could not rewrite it: the file would be reported every
    # run and repaired never. Both sides are matched case-insensitively, the
    # same way the checker matches them.
    "tool-count": {
        "patterns": [
            (r"(\d+)(\s*個\s*Python\s*工具)", "{value}\\2"),
            (r"(\d+)(\s*Python\s*tools?\b)", "{value}\\2"),
        ],
    },
    "doc-file-count": {
        "pattern": r"(\d+)(\s*個文件)",
        "replacement_template": "{value}\\2",
    },
    "rule-pack-count": {
        "patterns": [
            (r"rule%20packs-\d+-", "rule%20packs-{pack_count}-"),
            (r"alerts-\d+-", "alerts-{alert_count}-"),
        ],
    },
}

# ============================================================================
# Release-tag currency patterns (TB-F1 class — RELEASE tags, not image tags)
# ============================================================================
# bump_docs' auto-rewrite rule only matches the bold ``**`tools/vX`**`` form,
# so the code-block release-tag forms below drifted unsynced and shipped stale
# (burned #141 Track B / TB-F1: `tools/v2.7.0` install examples while latest was
# `tools/v2.8.0`). Image tags (`da-tools:vX`, `threshold-exporter:vX`) are
# covered by DA_TOOLS_TAG_PATTERN / EXPORTER_VERSION_PATTERNS; these are the
# release-tag-reference forms that had NO check.

# `tools/v<X.Y.Z>` in TAG= vars, `releases/download/.../tools/vX/` URLs, etc.
# Compare to the current da-tools (tools-line) version.
TOOLS_RELEASE_TAG_PATTERN = r"tools/v([0-9]+\.[0-9]+\.[0-9]+)"

# `da-guard|da-tools|da-batchpr|da-parser <whitespace> v<X.Y.Z>` — the expected
# output of a `--version` invocation. Compare to the tools-line version.
DA_BINARY_VERSION_OUTPUT_PATTERN = r"da-(?:guard|tools|batchpr|parser)\s+v([0-9]+\.[0-9]+\.[0-9]+)"

# helm `--set image.tag=v<X.Y.Z>` (threshold-exporter charts in docs). Compare
# to the exporter-line version.
SET_IMAGE_TAG_PATTERN = r"--set\s+image\.tag=v([0-9]+\.[0-9]+\.[0-9]+)"

# Per-line historical markers: a line that legitimately cites a PAST version
# (e.g. "older releases (≤ tools/v2.7.0)…"). These must NOT be flagged. Narrow
# and auditable; matched case-insensitively.
VERSION_HISTORICAL_LINE_MARKERS: Tuple[str, ...] = (
    "≤",
    "older release",
    "早期版本",
)

# Explicit per-line opt-out (repo noqa convention). Put it in a comment on the
# offending line to suppress a release-tag-currency finding with a rationale.
VERSION_CURRENCY_IGNORE = "version-currency-ignore"
