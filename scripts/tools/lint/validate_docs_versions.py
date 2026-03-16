#!/usr/bin/env python3
"""validate_docs_versions.py — 文件版號與計數一致性檢查

從 source-of-truth 檔案讀取實際版號與規則數量，掃描所有文件中的引用，
回報不一致之處。

檢查項目:
  1. da-tools image tag 是否與 VERSION 檔一致
  2. exporter image tag / OCI chart version 是否與 Chart.yaml 一致
  3. 平台版號（frontmatter、header、footer）是否與 CLAUDE.md 一致
  4. Rule Pack 計數（pack 數量、recording/alert 數量）是否與實際 YAML 一致
  5. 雙語文件配對數量是否與 badge 一致

用法:
  python3 scripts/tools/validate_docs_versions.py          # 互動報告
  python3 scripts/tools/validate_docs_versions.py --ci     # CI 模式 (exit 1 on fail)
  python3 scripts/tools/validate_docs_versions.py --json   # JSON 輸出
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Tuple

import yaml

# ---------------------------------------------------------------------------
# Repo root detection
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent.parent

# ---------------------------------------------------------------------------
# Source-of-truth files
# ---------------------------------------------------------------------------
CHART_YAML = REPO_ROOT / "components" / "threshold-exporter" / "Chart.yaml"
DA_TOOLS_VERSION = REPO_ROOT / "components" / "da-tools" / "app" / "VERSION"
CLAUDE_MD = REPO_ROOT / "CLAUDE.md"
RULE_PACKS_DIR = REPO_ROOT / "rule-packs"
K8S_RULES_DIR = REPO_ROOT / "k8s" / "03-monitoring"
DOCS_DIR = REPO_ROOT / "docs"


# ---------------------------------------------------------------------------
# Read source of truth
# ---------------------------------------------------------------------------

def read_source_versions() -> Dict[str, str]:
    """Read version numbers from source-of-truth files."""
    versions = {}

    # da-tools version
    if DA_TOOLS_VERSION.exists():
        ver = DA_TOOLS_VERSION.read_text(encoding="utf-8").strip()
        if re.match(r"^[0-9]+\.[0-9]+\.[0-9]+", ver):
            versions["tools"] = ver

    # Exporter version from Chart.yaml
    if CHART_YAML.exists():
        content = CHART_YAML.read_text(encoding="utf-8")
        m = re.search(r'^appVersion:\s*"([0-9]+\.[0-9]+\.[0-9]+[^"]*)"',
                       content, re.MULTILINE)
        if m:
            versions["exporter"] = m.group(1)

    # Platform version from CLAUDE.md
    if CLAUDE_MD.exists():
        content = CLAUDE_MD.read_text(encoding="utf-8")
        m = re.search(r"專案概覽 \(v([0-9]+\.[0-9]+[^)]+)\)", content)
        if m:
            versions["platform"] = m.group(1)

    return versions


def count_rule_packs() -> Dict[str, object]:
    """Count Rule Packs and rules from actual YAML files.

    Returns dict with keys: pack_count, recording, alert, total,
    and per_pack detail list.
    """
    packs = {}

    # rule-packs/ directory (recording rules + operational alerts)
    for f in sorted(RULE_PACKS_DIR.glob("rule-pack-*.yaml")):
        name = f.stem.replace("rule-pack-", "")
        data = yaml.safe_load(f.read_text(encoding="utf-8"))
        rec = alert = 0
        if data and "groups" in data:
            for g in data["groups"]:
                for r in g.get("rules", []):
                    if "alert" in r:
                        alert += 1
                    elif "record" in r:
                        rec += 1
        packs[name] = {"recording": rec, "alert": alert}

    # k8s ConfigMaps (may have alert rules not in rule-packs/ source)
    for f in sorted(K8S_RULES_DIR.glob("configmap-rules-*.yaml")):
        name = f.stem.replace("configmap-rules-", "")
        data = yaml.safe_load(f.read_text(encoding="utf-8"))
        rec = alert = 0
        if data and data.get("kind") == "ConfigMap":
            for _key, inner_yaml in data.get("data", {}).items():
                inner = yaml.safe_load(inner_yaml)
                if inner and "groups" in inner:
                    for g in inner["groups"]:
                        for r in g.get("rules", []):
                            if "alert" in r:
                                alert += 1
                            elif "record" in r:
                                rec += 1
        if name not in packs:
            packs[name] = {"recording": rec, "alert": alert}
        else:
            # Take max of both sources per pack
            packs[name]["recording"] = max(packs[name]["recording"], rec)
            packs[name]["alert"] = max(packs[name]["alert"], alert)

    total_rec = sum(p["recording"] for p in packs.values())
    total_alert = sum(p["alert"] for p in packs.values())

    return {
        "pack_count": len(packs),
        "recording": total_rec,
        "alert": total_alert,
        "total": total_rec + total_alert,
        "per_pack": packs,
    }


def count_bilingual_pairs() -> int:
    """Count .en.md files across the repo (each is one bilingual pair)."""
    count = 0
    # docs/ tree
    for f in _cached_rglob(DOCS_DIR,"*.en.md"):
        if f.is_file():
            count += 1
    # rule-packs/ tree
    for f in (REPO_ROOT / "rule-packs").rglob("*.en.md"):
        if f.is_file():
            count += 1
    # Root-level README.en.md
    root_en = REPO_ROOT / "README.en.md"
    if root_en.exists():
        count += 1
    return count


# ---------------------------------------------------------------------------
# Checkers
# ---------------------------------------------------------------------------

class Issue:
    """A single validation issue."""
    def __init__(self, check: str, severity: str, file: str,
                 line: int, message: str):
        self.check = check
        self.severity = severity  # "error" or "warn"
        self.file = file
        self.line = line
        self.message = message

    def to_dict(self):
        return {
            "check": self.check,
            "severity": self.severity,
            "file": self.file,
            "line": self.line,
            "message": self.message,
        }


def _scan_file(filepath: Path, pattern: str, flags: int = 0) -> List[Tuple[int, str]]:
    """Scan a file for regex pattern matches. Returns [(line_num, match_text)]."""
    if not filepath.exists():
        return []
    matches = []
    content = filepath.read_text(encoding="utf-8")
    for i, line in enumerate(content.splitlines(), 1):
        if re.search(pattern, line, flags):
            matches.append((i, line.strip()))
    return matches


# ---------------------------------------------------------------------------
# File collection cache — avoids repeated rglob + read_text across checks
# ---------------------------------------------------------------------------
_FILE_CACHE: Dict[str, List[Path]] = {}
_CONTENT_CACHE: Dict[Path, str] = {}
_RGLOB_CACHE: Dict[str, List[Path]] = {}


def _cached_rglob(base_dir: Path, pattern: str) -> List[Path]:
    """Cached rglob to avoid repeated filesystem walks."""
    cache_key = f"{base_dir}|{pattern}"
    if cache_key not in _RGLOB_CACHE:
        _RGLOB_CACHE[cache_key] = list(base_dir.rglob(pattern))
    return _RGLOB_CACHE[cache_key]


def _collect_scannable_files(extensions: Tuple[str, ...] = (".md", ".jsx"),
                             include_ci: bool = True) -> List[Path]:
    """Collect files to scan across docs, CI workflows, and K8s manifests.

    Results are cached to avoid repeated rglob calls across check functions.
    """
    cache_key = f"{extensions}|{include_ci}"
    if cache_key in _FILE_CACHE:
        return _FILE_CACHE[cache_key]

    files: List[Path] = []
    # Docs
    for ext in extensions:
        files.extend(_cached_rglob(DOCS_DIR,f"*{ext}"))
    # Root READMEs
    for name in ("README.md", "README.en.md"):
        p = REPO_ROOT / name
        if p.is_file():
            files.append(p)
    if include_ci:
        # CI workflows + K8s manifests
        for scan_dir in (REPO_ROOT / ".github",
                         REPO_ROOT / ".gitlab",
                         REPO_ROOT / "k8s"):
            if scan_dir.is_dir():
                files.extend(_cached_rglob(scan_dir, "*.yaml"))
                files.extend(_cached_rglob(scan_dir, "*.yml"))

    _FILE_CACHE[cache_key] = files
    return files


def _read_cached(filepath: Path) -> str:
    """Read file content with caching to avoid duplicate reads."""
    if filepath not in _CONTENT_CACHE:
        _CONTENT_CACHE[filepath] = filepath.read_text(encoding="utf-8")
    return _CONTENT_CACHE[filepath]


def check_da_tools_version(expected: str) -> List[Issue]:
    """Check all da-tools image tag references match VERSION file."""
    issues = []
    tag_pattern = r"da-tools:v?([0-9]+\.[0-9]+\.[0-9]+)"

    for f in _collect_scannable_files():
        content = _read_cached(f)
        for i, line in enumerate(content.splitlines(), 1):
            for m in re.finditer(tag_pattern, line):
                found_ver = m.group(1)
                if found_ver != expected:
                    rel = f.relative_to(REPO_ROOT)
                    issues.append(Issue(
                        "da-tools-version", "error", str(rel), i,
                        f"da-tools:{found_ver} should be da-tools:{expected}",
                    ))
    return issues


def check_exporter_version(expected: str) -> List[Issue]:
    """Check exporter image tags and OCI chart version references."""
    issues = []
    patterns = [
        (r"threshold-exporter:v?([0-9]+\.[0-9]+\.[0-9]+)", "image tag"),
        (r"charts/threshold-exporter --version ([0-9]+\.[0-9]+\.[0-9]+)",
         "OCI chart version"),
        (r"charts/threshold-exporter:([0-9]+\.[0-9]+\.[0-9]+)",
         "OCI chart inline version"),
    ]

    # Skip release.yaml — it uses CI variable interpolation, not literal tags
    skip_names = {"release.yaml"}

    for f in _collect_scannable_files():
        if not f.exists():
            continue
        if f.name in skip_names:
            continue
        content = _read_cached(f)
        for i, line in enumerate(content.splitlines(), 1):
            for pat, desc in patterns:
                for m in re.finditer(pat, line):
                    found_ver = m.group(1)
                    if found_ver != expected:
                        rel = f.relative_to(REPO_ROOT)
                        issues.append(Issue(
                            "exporter-version", "error", str(rel), i,
                            f"{desc} {found_ver} should be {expected}",
                        ))
    return issues


def check_platform_version(expected: str) -> List[Issue]:
    """Check frontmatter version: fields and inline version references."""
    issues = []
    fm_pattern = r"^version:\s*v?([0-9]+\.[0-9]+[^\s]*)"

    # Scan all docs/**/*.md frontmatter
    for f in sorted(_cached_rglob(DOCS_DIR,"*.md")):
        content = _read_cached(f)
        lines = content.splitlines()

        # Check if file has frontmatter
        if lines and lines[0].strip() == "---":
            for i, line in enumerate(lines[1:], 2):
                if line.strip() == "---":
                    break
                m = re.match(fm_pattern, line)
                if m:
                    found_ver = m.group(1)
                    if found_ver != expected and f"v{found_ver}" != f"v{expected}":
                        rel = f.relative_to(REPO_ROOT)
                        issues.append(Issue(
                            "platform-version", "error", str(rel), i,
                            f"frontmatter version {found_ver} should be {expected}",
                        ))

    # Also scan .jsx files
    for f in sorted(_cached_rglob(DOCS_DIR,"*.jsx")):
        content = _read_cached(f)
        for i, line in enumerate(content.splitlines(), 1):
            m = re.match(fm_pattern, line)
            if m:
                found_ver = m.group(1)
                if found_ver != expected:
                    rel = f.relative_to(REPO_ROOT)
                    issues.append(Issue(
                        "platform-version", "error", str(rel), i,
                        f"frontmatter version {found_ver} should be {expected}",
                    ))

    return issues


def check_rule_pack_counts(actual: Dict) -> List[Issue]:
    """Check Rule Pack counts in documentation match actual YAML counts."""
    issues = []
    pack_count = actual["pack_count"]
    rec_count = actual["recording"]
    alert_count = actual["alert"]
    total_count = actual["total"]

    # Patterns to check in docs
    checks = [
        # (pattern, extract_group_index, expected_value, description)
        (r"(\d+)\s*個\s*Rule\s*Pack", 1, str(pack_count), "Rule Pack count (zh)"),
        (r"(\d+)\s*Rule\s*Pack\s*ConfigMap", 1, str(pack_count),
         "Rule Pack ConfigMap count"),
        (r"rule%20packs-(\d+)-", 1, str(pack_count), "Rule Pack badge"),
        (r"alerts-(\d+)-", 1, str(alert_count), "Alert badge"),
        (r"\*\*合計\*\*.*\*\*(\d+)\*\*.*\*\*(\d+)\*\*", None, None,
         "Rule Pack total row"),
    ]

    # Files to skip (contain historical references that are correct at time of writing)
    skip_basenames = {"CHANGELOG.md", "CHANGELOG.en.md", "benchmarks.md",
                      "benchmarks.en.md"}

    files_to_scan = list(_cached_rglob(DOCS_DIR,"*.md"))
    files_to_scan.extend([
        REPO_ROOT / "README.md",
        REPO_ROOT / "README.en.md",
    ])

    for f in files_to_scan:
        if not f.exists():
            continue
        if f.name in skip_basenames:
            continue
        content = _read_cached(f)
        rel = str(f.relative_to(REPO_ROOT))

        for i, line in enumerate(content.splitlines(), 1):
            # Check pack count patterns
            for pat, grp, expected, desc in checks[:4]:
                for m in re.finditer(pat, line, re.IGNORECASE):
                    found = m.group(grp)
                    if found != expected:
                        # Skip historical references (v1.x.y context)
                        if re.search(r"v1\.[0-9]+\.[0-9]+", line):
                            continue
                        issues.append(Issue(
                            "rule-pack-count", "error", rel, i,
                            f"{desc}: found {found}, expected {expected}",
                        ))

    return issues


def check_bilingual_badge(actual_pairs: int) -> List[Issue]:
    """Check bilingual badge count matches actual .en.md pairs."""
    issues = []

    for f in [REPO_ROOT / "README.md", REPO_ROOT / "README.en.md"]:
        if not f.exists():
            continue
        content = _read_cached(f)
        rel = str(f.relative_to(REPO_ROOT))
        for i, line in enumerate(content.splitlines(), 1):
            m = re.search(r"bilingual-(\d+)%20pairs", line)
            if m:
                found = int(m.group(1))
                if found != actual_pairs:
                    issues.append(Issue(
                        "bilingual-count", "warn", rel, i,
                        f"badge says {found} pairs, actual is {actual_pairs}",
                    ))
    return issues


def _extract_changelog_completed_keywords() -> List[str]:
    """Extract feature keywords from completed CHANGELOG entries.

    Looks for section headers (### lines) and key feature names
    in the latest CHANGELOG versions. Returns normalised lowercase
    keywords that can be matched against roadmap text.
    """
    changelog = REPO_ROOT / "CHANGELOG.md"
    if not changelog.exists():
        return []

    content = changelog.read_text(encoding="utf-8")

    # Extract feature keywords from ### headings and bold items
    keywords = []
    # Match bold feature names like **`shadow_verify.py`** or **Shadow Monitoring**
    for m in re.finditer(r"\*\*`?([^*`]+)`?\*\*", content):
        kw = m.group(1).strip().lower()
        if len(kw) > 3 and not re.match(r"^v?\d+\.\d+", kw):
            keywords.append(kw)

    return keywords


def check_roadmap_changelog_overlap() -> List[Issue]:
    """Detect completed items that still appear in roadmap sections.

    Scans architecture-and-design.md §5 and CLAUDE.md 長期展望 for
    references to features already listed as completed in CHANGELOG.md.
    """
    issues = []

    # Roadmap files and their section markers
    roadmap_files = [
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

    # Known completed features (from CHANGELOG section headers)
    changelog = REPO_ROOT / "CHANGELOG.md"
    if not changelog.exists():
        return issues

    content = changelog.read_text(encoding="utf-8")

    # Extract completed feature *phrases* from ### headings.
    # e.g. "### 🏷️ Dual-Perspective Annotation" → "dual-perspective annotation"
    # We build regex patterns that require the phrase to appear as a
    # contiguous substring (case-insensitive), which avoids false positives
    # from individual words appearing in unrelated contexts.
    completed_phrases: List[str] = []
    skip_headings = {"版號", "Breaking Changes", "Key Changes",
                     "Documentation Overhaul", "文件大重構"}
    for m in re.finditer(r"^### .+?([A-Z][A-Za-z][^\n]+)", content,
                         re.MULTILINE):
        feat = m.group(1).strip()
        if feat in skip_headings:
            continue
        # Skip short phrases (< 8 chars) — too generic to match reliably
        if len(feat) < 8:
            continue
        completed_phrases.append(feat)

    if not completed_phrases:
        return issues

    # Build phrase patterns — match the exact multi-word phrase
    phrase_patterns = []
    for phrase in completed_phrases:
        # Escape for regex and allow flexible whitespace
        escaped = re.escape(phrase)
        escaped = re.sub(r"\\ ", r"\\s+", escaped)
        phrase_patterns.append((re.compile(escaped, re.IGNORECASE), phrase))

    for fpath, start_pattern, desc in roadmap_files:
        if not fpath.exists():
            continue
        fcontent = fpath.read_text(encoding="utf-8")
        lines = fcontent.splitlines()

        # Find roadmap section start
        in_roadmap = False
        for i, line in enumerate(lines, 1):
            if re.match(start_pattern, line):
                in_roadmap = True
                continue
            if in_roadmap and re.match(r"^## ", line) and \
                    not re.match(start_pattern, line):
                break  # Next top-level section
            if not in_roadmap:
                continue

            # Skip "已完成" reference lines and section-header lines
            if "已完成" in line or "completed" in line.lower():
                continue
            if line.startswith("#"):
                continue

            # Check if any completed feature *phrase* appears verbatim
            for pat, phrase in phrase_patterns:
                if pat.search(line):
                    rel = str(fpath.relative_to(REPO_ROOT))
                    issues.append(Issue(
                        "roadmap-stale", "warn", rel, i,
                        f"roadmap may reference completed feature: "
                        f"'{phrase}'",
                    ))
                    break  # One issue per line is enough

    return issues


def check_bilingual_number_consistency() -> List[Issue]:
    """Check that zh and en doc pairs have matching technical numbers.

    Compares numeric values in paired zh/en documents to detect
    translation drift (e.g. zh says 15 Rule Packs but en says 13).
    """
    issues = []

    # Patterns for technical numbers that should match across languages
    number_patterns = [
        (r"(\d+)\s*個?\s*Rule\s*Pack", "Rule Pack count"),
        (r"(\d+)\s*Recording", "Recording rule count"),
        (r"(\d+)\s*Alert(?:\s+rule)?", "Alert rule count"),
        (r"rule%20packs-(\d+)-", "Rule Pack badge"),
        (r"alerts-(\d+)-", "Alert badge"),
        (r"bilingual-(\d+)", "Bilingual badge"),
    ]

    # Files with legitimate historical number references
    skip_basenames = {"benchmarks.md", "CHANGELOG.md"}

    # Find zh/en pairs
    pairs = []
    for zh_file in sorted(_cached_rglob(DOCS_DIR,"*.md")):
        if ".en." in zh_file.name:
            continue
        if zh_file.name in skip_basenames:
            continue
        en_file = zh_file.with_name(
            zh_file.name.replace(".md", ".en.md"))
        if en_file.exists():
            pairs.append((zh_file, en_file))

    # Root README pair
    zh_root = REPO_ROOT / "README.md"
    en_root = REPO_ROOT / "README.en.md"
    if zh_root.exists() and en_root.exists():
        pairs.append((zh_root, en_root))

    for zh_file, en_file in pairs:
        zh_content = zh_file.read_text(encoding="utf-8")
        en_content = en_file.read_text(encoding="utf-8")

        for pat, desc in number_patterns:
            zh_nums = sorted(set(re.findall(pat, zh_content, re.IGNORECASE)))
            en_nums = sorted(set(re.findall(pat, en_content, re.IGNORECASE)))
            if zh_nums and en_nums and zh_nums != en_nums:
                rel_zh = str(zh_file.relative_to(REPO_ROOT))
                rel_en = str(en_file.relative_to(REPO_ROOT))
                issues.append(Issue(
                    "bilingual-numbers", "warn", rel_zh, 0,
                    f"{desc} mismatch: zh={zh_nums} vs en={en_nums} "
                    f"({rel_en})",
                ))

    return issues


def check_doc_map_coverage() -> List[Issue]:
    """Check that doc-map.md lists all docs/*.md files.

    Scans actual docs/ tree for .md files (excluding .en.md) and verifies
    each is referenced in docs/internal/doc-map.md.
    """
    issues = []
    doc_map = DOCS_DIR / "internal" / "doc-map.md"
    if not doc_map.exists():
        return issues

    map_content = doc_map.read_text(encoding="utf-8").lower()

    # Collect all zh doc files (skip .en.md)
    # Skip directories that are intentionally not in the doc-map table
    skip_dirs = {"includes", "adr"}
    skip_names = {"tags.md", "CHANGELOG.md", "README-root.md", "doc-map.md",
                   "tool-map.md"}

    for f in sorted(_cached_rglob(DOCS_DIR,"*.md")):
        if ".en." in f.name:
            continue
        rel = f.relative_to(REPO_ROOT)
        rel_str = str(rel).replace("\\", "/")

        # Skip includes/, adr/ individual files, and known exclusions
        parts = rel.parts
        if any(d in parts for d in skip_dirs):
            continue
        if f.name in skip_names:
            continue

        # doc-map uses backtick-quoted paths or plain filenames
        lookup = f.name.lower()

        if lookup not in map_content:
            issues.append(Issue(
                "doc-map-coverage", "warn",
                "docs/internal/doc-map.md", 0,
                f"doc file not listed in doc-map: {rel_str}",
            ))

    return issues


def check_tool_map_coverage() -> List[Issue]:
    """Check that tool-map.md lists all scripts/tools/*.py files.

    Scans actual scripts/tools/ for .py files (excluding __pycache__,
    _lib_*, and __init__) and verifies each is referenced in
    docs/internal/tool-map.md.
    """
    issues = []
    tool_map = DOCS_DIR / "internal" / "tool-map.md"
    tools_dir = REPO_ROOT / "scripts" / "tools"
    if not tool_map.exists() or not tools_dir.exists():
        return issues

    map_content = tool_map.read_text(encoding="utf-8").lower()

    skip_prefixes = ("_lib", "__init__", "__pycache__")
    for f in sorted(tools_dir.glob("*.py")):
        if any(f.name.startswith(p) for p in skip_prefixes):
            continue

        lookup = f.name.lower()
        if lookup not in map_content:
            issues.append(Issue(
                "tool-map-coverage", "warn",
                "docs/internal/tool-map.md", 0,
                f"tool not listed in tool-map: {f.name}",
            ))

    return issues


def check_tool_count_in_docs() -> List[Issue]:
    """Check that CLAUDE.md and README tool counts match actual scripts/tools/*.py.

    Compares the "XX 個 Python 工具" / "XX Python tools" counts in CLAUDE.md
    and README files against the actual number of .py files in scripts/tools/
    (excluding _lib_*, __init__, __pycache__).
    """
    issues = []
    tools_dir = REPO_ROOT / "scripts" / "tools"
    if not tools_dir.exists():
        return issues

    skip_prefixes = ("_lib", "__init__", "__pycache__")
    # Scan all subdirectories (ops/, dx/, lint/) + root
    all_py_files = list(tools_dir.glob("*.py"))
    for subdir in ("ops", "dx", "lint"):
        sub_path = tools_dir / subdir
        if sub_path.is_dir():
            all_py_files.extend(sub_path.glob("*.py"))
    actual_count = sum(
        1 for f in all_py_files
        if not any(f.name.startswith(p) for p in skip_prefixes)
    )

    # Patterns to detect tool count references
    count_patterns = [
        (r"(\d+)\s*個\s*Python\s*工具", "Python tool count (zh)"),
        (r"(\d+)\s*Python\s*tools?(?:\s*[\(（])", "Python tool count (en)"),
        (r"(\d+)\s*Python\s*tools?(?:\s*in)", "Python tool count (en-in)"),
    ]

    files_to_check = [
        REPO_ROOT / "CLAUDE.md",
        REPO_ROOT / "README.md",
        REPO_ROOT / "README.en.md",
    ]

    for fpath in files_to_check:
        if not fpath.exists():
            continue
        content = _read_cached(fpath)
        rel = str(fpath.relative_to(REPO_ROOT))

        for i, line in enumerate(content.splitlines(), 1):
            for pat, desc in count_patterns:
                for m in re.finditer(pat, line, re.IGNORECASE):
                    found = int(m.group(1))
                    if found != actual_count:
                        issues.append(Issue(
                            "tool-count", "warn", rel, i,
                            f"{desc}: found {found}, actual is {actual_count}",
                        ))

    return issues


def check_adr_count_in_docs() -> List[Issue]:
    """Check that ADR count references in docs match actual docs/adr/ files.

    Scans CLAUDE.md and README files for patterns like '5 ADRs' and
    compares against the actual number of ADR .md files (excluding README).
    """
    issues = []
    adr_dir = REPO_ROOT / "docs" / "adr"
    if not adr_dir.exists():
        return issues

    actual_count = sum(
        1 for f in adr_dir.glob("*.md")
        if f.name != "README.md" and not f.name.endswith(".en.md")
    )

    # Pattern: "5 ADRs" or "(5 ADRs)"
    count_patterns = [
        (r"(\d+)\s*ADRs?\b", "ADR count"),
    ]

    files_to_check = [
        REPO_ROOT / "CLAUDE.md",
        REPO_ROOT / "README.md",
        REPO_ROOT / "README.en.md",
        adr_dir / "README.md",
        adr_dir / "README.en.md",
    ]

    for fpath in files_to_check:
        if not fpath.exists():
            continue
        content = _read_cached(fpath)
        rel = str(fpath.relative_to(REPO_ROOT))

        for i, line in enumerate(content.splitlines(), 1):
            for pat, desc in count_patterns:
                for m in re.finditer(pat, line, re.IGNORECASE):
                    found = int(m.group(1))
                    if found != actual_count:
                        issues.append(Issue(
                            "adr-count", "warn", rel, i,
                            f"{desc}: found {found}, actual is {actual_count}",
                        ))

    return issues


def check_doc_file_count_in_docs() -> List[Issue]:
    """Check that doc file count in CLAUDE.md matches doc-map.md row count.

    CLAUDE.md references '43 個文件' — this must match the actual entry
    count in docs/internal/doc-map.md (table rows minus header/separator).
    """
    issues = []
    doc_map = REPO_ROOT / "docs" / "internal" / "doc-map.md"
    if not doc_map.exists():
        return issues

    # Count actual entries: table rows starting with | minus header + separator
    map_content = doc_map.read_text(encoding="utf-8")
    table_rows = sum(1 for line in map_content.splitlines()
                     if line.startswith("|"))
    actual_count = max(0, table_rows - 2)  # subtract header + separator

    # Pattern: "XX 個文件" in CLAUDE.md
    count_patterns = [
        (r"(\d+)\s*個文件", "doc file count (zh)"),
    ]

    files_to_check = [REPO_ROOT / "CLAUDE.md"]

    for fpath in files_to_check:
        if not fpath.exists():
            continue
        content = _read_cached(fpath)
        rel = str(fpath.relative_to(REPO_ROOT))

        for i, line in enumerate(content.splitlines(), 1):
            for pat, desc in count_patterns:
                for m in re.finditer(pat, line):
                    found = int(m.group(1))
                    if found != actual_count:
                        issues.append(Issue(
                            "doc-file-count", "warn", rel, i,
                            f"{desc}: found {found}, actual is "
                            f"{actual_count}",
                        ))

    return issues


def check_scenario_count_in_docs() -> List[Issue]:
    """Check that scenario count references match actual docs/scenarios/ files."""
    issues = []
    scenarios_dir = REPO_ROOT / "docs" / "scenarios"
    if not scenarios_dir.exists():
        return issues

    actual_count = sum(
        1 for f in scenarios_dir.glob("*.md")
        if not f.name.endswith(".en.md")
    )

    count_patterns = [
        (r"(\d+)\s*場景", "scenario count (zh)"),
    ]

    files_to_check = [REPO_ROOT / "CLAUDE.md"]

    for fpath in files_to_check:
        if not fpath.exists():
            continue
        content = _read_cached(fpath)
        rel = str(fpath.relative_to(REPO_ROOT))

        for i, line in enumerate(content.splitlines(), 1):
            for pat, desc in count_patterns:
                for m in re.finditer(pat, line):
                    found = int(m.group(1))
                    if found != actual_count:
                        issues.append(Issue(
                            "scenario-count", "warn", rel, i,
                            f"{desc}: found {found}, actual is {actual_count}",
                        ))

    return issues


def _auto_fix(issues: List[Issue], bilingual_pairs: int,
              rule_counts: dict) -> int:
    """Auto-fix fixable issues. Returns count of fixes applied."""
    import stat
    fixed = 0

    for issue in issues:
        fpath = REPO_ROOT / issue.file
        if not fpath.exists():
            continue

        content = _read_cached(fpath)
        new_content = content

        if issue.check == "bilingual-count":
            # Fix badge count: bilingual-XX%20pairs → bilingual-{actual}%20pairs
            new_content = re.sub(
                r"bilingual-\d+%20pairs",
                f"bilingual-{bilingual_pairs}%20pairs",
                new_content,
            )

        elif issue.check == "tool-count":
            # Fix "XX 個 Python 工具" count
            tools_dir = REPO_ROOT / "scripts" / "tools"
            skip_prefixes = ("_lib", "__init__", "__pycache__")
            actual_count = sum(
                1 for f in tools_dir.glob("*.py")
                if not any(f.name.startswith(p) for p in skip_prefixes)
            )
            new_content = re.sub(
                r"(\d+)(\s*個\s*Python\s*工具)",
                f"{actual_count}\\2",
                new_content,
            )

        elif issue.check == "doc-file-count":
            # Fix "XX 個文件" count from doc-map.md row count
            doc_map = REPO_ROOT / "docs" / "internal" / "doc-map.md"
            if doc_map.exists():
                map_text = doc_map.read_text(encoding="utf-8")
                rows = sum(1 for ln in map_text.splitlines()
                           if ln.startswith("|"))
                doc_count = max(0, rows - 2)
                new_content = re.sub(
                    r"(\d+)(\s*個文件)",
                    f"{doc_count}\\2",
                    new_content,
                )

        elif issue.check == "rule-pack-count":
            # These are trickier — only fix clear badge patterns
            # (avoid modifying prose where context might differ)
            pack_count = rule_counts["pack_count"]
            alert_count = rule_counts["alert"]
            # Fix badge patterns
            new_content = re.sub(
                r"rule%20packs-\d+-",
                f"rule%20packs-{pack_count}-",
                new_content,
            )
            new_content = re.sub(
                r"alerts-\d+-",
                f"alerts-{alert_count}-",
                new_content,
            )

        if new_content != content:
            fpath.write_text(new_content, encoding="utf-8")
            os.chmod(fpath,
                     stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP
                     | stat.S_IROTH)
            print(f"  🔧 Fixed {issue.check} in {issue.file}")
            fixed += 1

    return fixed


def check_image_tag_v_prefix() -> List[Issue]:
    """Ensure Docker image tags use v-prefix convention consistently.

    Convention (aligned with CI release.yaml):
      - Docker images: da-tools:v<ver>, threshold-exporter:v<ver> (v-prefixed)
      - Helm OCI chart: charts/threshold-exporter:<ver> (no v, SemVer)

    Detects bare version tags (e.g. da-tools:2.0.0) that should be v-prefixed.
    Skips CI release.yaml (uses variable interpolation) and CHANGELOG (historical).
    """
    issues = []
    # Match image:VERSION without v prefix (negative lookbehind for 'charts/')
    bare_tag_pattern = r"(?<!charts/)(?:da-tools|threshold-exporter):(\d+\.\d+\.\d+)"

    skip_names = {"release.yaml", "CHANGELOG.md", "CHANGELOG.en.md"}

    for f in _collect_scannable_files():
        if not f.exists() or f.name in skip_names:
            continue
        content = _read_cached(f)
        for i, line in enumerate(content.splitlines(), 1):
            for m in re.finditer(bare_tag_pattern, line):
                rel = f.relative_to(REPO_ROOT)
                ver = m.group(1)
                # Find which image it is
                start = max(0, m.start() - 30)
                context = line[start:m.end()]
                if "da-tools" in context:
                    img = "da-tools"
                else:
                    img = "threshold-exporter"
                issues.append(Issue(
                    "image-tag-v-prefix", "error", str(rel), i,
                    f"{img}:{ver} missing v prefix, should be {img}:v{ver}",
                ))
    return issues


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    """CLI entry point: 文件版號與計數一致性檢查."""
    parser = argparse.ArgumentParser(
        description="Validate version numbers and counts across documentation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--ci", action="store_true",
                        help="CI mode: exit 1 on any error")
    parser.add_argument("--json", action="store_true",
                        help="Output results as JSON")
    parser.add_argument("--fix", action="store_true",
                        help="Auto-fix bilingual badge and rule-pack count "
                             "issues (delegates version fixes to bump_docs.py)")

    args = parser.parse_args()

    # Read source of truth
    versions = read_source_versions()
    rule_counts = count_rule_packs()
    bilingual_pairs = count_bilingual_pairs()

    if not args.json:
        print("Source of truth:")
        print(f"  platform:  v{versions.get('platform', '???')}")
        print(f"  exporter:  v{versions.get('exporter', '???')}")
        print(f"  da-tools:  v{versions.get('tools', '???')}")
        print(f"  Rule Packs: {rule_counts['pack_count']} packs, "
              f"{rule_counts['recording']}R + {rule_counts['alert']}A "
              f"= {rule_counts['total']}")
        print(f"  Bilingual:  {bilingual_pairs} pairs")
        print()

    # Run all checks
    all_issues: List[Issue] = []

    if "tools" in versions:
        all_issues.extend(check_da_tools_version(versions["tools"]))
    if "exporter" in versions:
        all_issues.extend(check_exporter_version(versions["exporter"]))
    if "platform" in versions:
        all_issues.extend(check_platform_version(versions["platform"]))
    all_issues.extend(check_rule_pack_counts(rule_counts))
    all_issues.extend(check_bilingual_badge(bilingual_pairs))
    all_issues.extend(check_roadmap_changelog_overlap())
    all_issues.extend(check_bilingual_number_consistency())
    all_issues.extend(check_doc_map_coverage())
    all_issues.extend(check_tool_map_coverage())
    all_issues.extend(check_tool_count_in_docs())
    all_issues.extend(check_adr_count_in_docs())
    all_issues.extend(check_doc_file_count_in_docs())
    all_issues.extend(check_scenario_count_in_docs())
    all_issues.extend(check_image_tag_v_prefix())

    # --fix mode: auto-fix fixable issues
    if args.fix and all_issues:
        fixed = _auto_fix(all_issues, bilingual_pairs, rule_counts)
        if fixed:
            print(f"🔧 Auto-fixed {fixed} issue(s). Re-run to verify.")
        else:
            print("No auto-fixable issues found.")
            unfixable = [i for i in all_issues
                         if i.check in ("platform-version", "da-tools-version",
                                        "exporter-version")]
            if unfixable:
                print("  ℹ️  Version issues: run bump_docs.py to fix")
        return

    errors = [i for i in all_issues if i.severity == "error"]
    warnings = [i for i in all_issues if i.severity == "warn"]

    if args.json:
        result = {
            "source_of_truth": {
                "platform": versions.get("platform"),
                "exporter": versions.get("exporter"),
                "tools": versions.get("tools"),
                "rule_packs": rule_counts,
                "bilingual_pairs": bilingual_pairs,
            },
            "issues": [i.to_dict() for i in all_issues],
            "summary": {
                "errors": len(errors),
                "warnings": len(warnings),
            },
        }
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        if all_issues:
            for issue in all_issues:
                icon = "❌" if issue.severity == "error" else "⚠️"
                print(f"  {icon} [{issue.check}] {issue.file}:{issue.line} "
                      f"— {issue.message}")
            print()

        if errors:
            print(f"❌ {len(errors)} error(s), {len(warnings)} warning(s)")
        elif warnings:
            print(f"⚠️  {len(warnings)} warning(s), 0 errors")
        else:
            print("✅ All version references and counts are consistent.")

    if args.ci and errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
