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
BILINGUAL_NUMBER_PATTERNS: List[Tuple[str, str]] = [
    (r"(\d+)\s*個?\s*Rule\s*Pack", "Rule Pack count"),
    (r"(\d+)\s*Recording", "Recording rule count"),
    (r"(\d+)\s*Alert(?:\s+rule)?", "Alert rule count"),
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

# Bilingual number consistency skips these
SKIP_BILINGUAL_NUMBER_FILES = {"benchmarks.md", "CHANGELOG.md"}

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
