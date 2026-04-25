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
CHART_YAML = REPO_ROOT / "components" / "threshold-exporter" / "Chart.yaml"
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

# Docker image bare tag pattern (missing v-prefix)
BARE_TAG_PATTERN = r"(?<!charts/)(?:da-tools|threshold-exporter):(\d+\.\d+\.\d+)"

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

# Tool count patterns: (regex, description)
TOOL_COUNT_PATTERNS: List[Tuple[str, str]] = [
    (r"(\d+)\s*個\s*Python\s*工具", "Python tool count (zh)"),
    (r"(\d+)\s*Python\s*tools?(?:\s*[\(（])", "Python tool count (en)"),
    (r"(\d+)\s*Python\s*tools?(?:\s*in)", "Python tool count (en-in)"),
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

# Platform version extraction from CLAUDE.md
PLATFORM_VERSION_PATTERN = r"專案概覽 \(v([0-9]+\.[0-9]+[^)]+)\)"

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

# Tool map coverage check skips these filename prefixes
TOOL_MAP_SKIP_PREFIXES = ("_lib", "__init__", "__pycache__")

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
    "tool-count": {
        "pattern": r"(\d+)(\s*個\s*Python\s*工具)",
        "replacement_template": "{value}\\2",
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
