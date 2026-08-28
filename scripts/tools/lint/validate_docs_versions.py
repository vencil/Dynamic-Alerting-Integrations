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

# Import version patterns from centralized registry
from _version_patterns import (
    REPO_ROOT,
    CHART_YAML,
    DA_TOOLS_VERSION,
    CLAUDE_MD,
    RULE_PACKS_DIR,
    K8S_RULES_DIR,
    DOCS_DIR,
    SCANNABLE_EXTENSIONS,
    SCAN_DIRECTORIES,
    ROOT_FILES,
    DA_TOOLS_TAG_PATTERN,
    EXPORTER_VERSION_PATTERNS,
    PLATFORM_VERSION_FRONTMATTER_PATTERN,
    BARE_TAG_PATTERN,
    TOOLS_RELEASE_TAG_PATTERN,
    DA_BINARY_VERSION_OUTPUT_PATTERN,
    SET_IMAGE_TAG_PATTERN,
    VERSION_HISTORICAL_LINE_MARKERS,
    VERSION_CURRENCY_IGNORE,
    RULE_PACK_COUNT_PATTERNS,
    TOOL_COUNT_PATTERNS,
    ADR_COUNT_PATTERNS,
    DOC_FILE_COUNT_PATTERNS,
    SCENARIO_COUNT_PATTERNS,
    BILINGUAL_PAIR_PATTERN,
    BILINGUAL_NUMBER_PATTERNS,
    TOOL_COUNT_SCOPE_ANCHOR,
    DA_TOOLS_VERSION_PATTERN,
    EXPORTER_VERSION_PATTERN,
    MKDOCS_EXTRA_CHECKS,
    SKIP_CI_INTERPOLATION_FILES,
    SKIP_RULE_PACK_FILES,
    SKIP_BILINGUAL_NUMBER_FILES,
    DOC_MAP_SKIP_DIRS,
    DOC_MAP_SKIP_NAMES,
    DOC_MAP_SKIP_NAME_PATTERNS,
    ROADMAP_SECTIONS,
    SKIP_FEATURE_HEADINGS,
    TOOL_COUNT_CHECK_FILES,
    ADR_COUNT_CHECK_FILES,
    RULE_PACK_COUNT_CHECK_FILES,
    BILINGUAL_BADGE_CHECK_FILES,
    AUTO_FIX_PATTERNS,
)

# Pull `try_utf8_stdout` from the shared compat lib at scripts/tools/.
# Migrated in #489 Phase B (was missing encoding setup → would crash on
# legacy Windows cp950/cp936 consoles when printing emoji to stdout).
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, str(_THIS_DIR))
sys.path.insert(0, os.path.join(str(_THIS_DIR), ".."))
from _lib_compat import try_utf8_stdout  # noqa: E402
from _lib_exitcodes import EXIT_VIOLATION  # noqa: E402
from _lib_toolcount import count_scope, is_tool_file  # noqa: E402
from _lib_versions import read_platform_version  # noqa: E402


# ---------------------------------------------------------------------------
# Read source of truth
# ---------------------------------------------------------------------------

# Which module-level constant each source of truth is read from.
#
# ⛔ The constant NAME, not a second copy of the path. Retyping the path
# gives one file two declarations that drift apart, and the failure message
# would then name a file the reader never opened — the exact class of stale
# description this change set exists to remove. (CodeRabbit raised this on
# #1493; the first draft did retype all three paths.)
#
# Resolved lazily through globals() so that tests which monkeypatch the
# constants still see the path they injected.
_SSOT_SOURCE_ATTRS = {
    "platform": "CLAUDE_MD",
    "tools": "DA_TOOLS_VERSION",
    "exporter": "CHART_YAML",
}


def _ssot_source_label(key: str) -> str:
    """Repo-relative path of the file *key* is read from, for the message."""
    path = globals().get(_SSOT_SOURCE_ATTRS.get(key, ""))
    if path is None:
        return "(unknown source)"
    try:
        return Path(path).resolve().relative_to(REPO_ROOT).as_posix()
    except (ValueError, OSError):
        return str(path)

def read_source_versions() -> Dict[str, str]:
    """Read version numbers from source-of-truth files."""
    versions = {}

    # da-tools version
    if DA_TOOLS_VERSION.exists():
        ver = DA_TOOLS_VERSION.read_text(encoding="utf-8").strip()
        if re.match(DA_TOOLS_VERSION_PATTERN, ver):
            versions["tools"] = ver

    # Exporter version from Chart.yaml
    if CHART_YAML.exists():
        content = CHART_YAML.read_text(encoding="utf-8")
        m = re.search(EXPORTER_VERSION_PATTERN, content, re.MULTILINE)
        if m:
            versions["exporter"] = m.group(1)

    # Platform version from CLAUDE.md.
    #
    # #1480: this used to be a fourth local regex (`PLATFORM_VERSION_PATTERN`,
    # anchored on `專案概覽 (vX.Y.Z)`). It went dead on 2026-04-09 when
    # `abe27478` — a chore commit about phantom locks, nothing to do with
    # version governance — rewrote that heading to a bare `## 專案概覽` and
    # moved the version into the bold lead-in line below it. Nothing said so:
    # both consumers were guarded by `if "platform" in versions`, so they
    # simply stopped running while this tool kept printing `platform: v???`
    # and exiting 0. That held across v2.7.0, v2.8.0, v2.8.1, v2.9.0 and
    # v2.9.1 — five tagged platform releases, roughly four and a half
    # months. Measured damage: `tests/e2e/package.json` froze at 2.6.0,
    # the exact version at which the gate stopped being able to see it.
    #
    # `_lib_versions.read_platform_version` already reads the current shape
    # and is what the doc-map / tool-map generators use, so this defers to it
    # instead of maintaining yet another copy of the same lookup. The explicit
    # empty default keeps "could not read it" distinguishable — its own
    # fallback would otherwise hand back a stale release string.
    platform = read_platform_version(default="", claude_md=CLAUDE_MD)
    if platform:
        versions["platform"] = platform.lstrip("v")

    return versions


def count_rule_packs() -> Dict[str, object]:
    """Count Rule Packs and rules from actual YAML files.

    Returns dict with keys: pack_count, recording, alert, total,
    and per_pack detail list.
    """
    packs = {}

    # #741 S3b: custom-alerts is a TENANT-authored deployed pack, not platform
    # coverage — exclude it from the platform "Rule Packs / Alerts" counts that
    # gate the docs (mirrors generate_rule_pack_stats.py + PACK_ORDER).
    _EXCLUDE = {"custom-alerts"}

    # rule-packs/ directory (recording rules + operational alerts)
    for f in sorted(RULE_PACKS_DIR.glob("rule-pack-*.yaml")):
        name = f.stem.replace("rule-pack-", "")
        if name in _EXCLUDE:
            continue
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
        if name in _EXCLUDE:
            continue
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


def _collect_scannable_files(extensions: Tuple[str, ...] = SCANNABLE_EXTENSIONS,
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
    # Root READMEs + CLAUDE.md + mkdocs.yml
    for name in ROOT_FILES:
        p = REPO_ROOT / name
        if p.is_file():
            files.append(p)
    # Component READMEs (v2.4.0: 補全 components/ 目錄掃描範圍)
    components_dir = REPO_ROOT / "components"
    if components_dir.is_dir():
        files.extend(_cached_rglob(components_dir, "*.md"))
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
    """Check all da-tools image tag references match VERSION file.

    Skips historical planning docs (Category A per v2.7.0-planning §8.11.1),
    CHANGELOG entries, and test fixture/docstring bodies that intentionally
    record past-version strings. These are reference-style mentions that must
    not be rewritten on version bumps — doing so would rewrite history.
    """
    # Historical-reference exemptions. Align with check_bare_tags' skip_names
    # pattern. Keep the list narrow: each entry represents an SSOT decision
    # that the file's prior-version strings are load-bearing.
    skip_names = {
        "CHANGELOG.md", "CHANGELOG.en.md",
        "v2.5.0-v2.6.0-planning.md",
        "known-regressions.md",
    }
    issues = []

    for f in _collect_scannable_files():
        if f.name in skip_names:
            continue
        content = _read_cached(f)
        for i, line in enumerate(content.splitlines(), 1):
            for m in re.finditer(DA_TOOLS_TAG_PATTERN, line):
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

    for f in _collect_scannable_files():
        if not f.exists():
            continue
        if f.name in SKIP_CI_INTERPOLATION_FILES:
            continue
        content = _read_cached(f)
        for i, line in enumerate(content.splitlines(), 1):
            for pat, desc in EXPORTER_VERSION_PATTERNS:
                for m in re.finditer(pat, line):
                    found_ver = m.group(1)
                    if found_ver != expected:
                        rel = f.relative_to(REPO_ROOT)
                        issues.append(Issue(
                            "exporter-version", "error", str(rel), i,
                            f"{desc} {found_ver} should be {expected}",
                        ))
    return issues


def check_release_tag_currency(tools_expected: str,
                               exporter_expected: str = None) -> List[Issue]:
    """Flag stale RELEASE-TAG version references in docs (TB-F1 class).

    Covers the forms bump_docs' bold-only rewrite rule misses and no check
    previously caught: ``tools/vX`` (TAG= vars / release download URLs),
    ``da-* vX`` (--version expected output), and ``--set image.tag=vX``.
    Image *tags* themselves stay with check_da_tools_version /
    check_exporter_version. Lines citing a past version on purpose
    (historical markers) or with an explicit per-line ignore are skipped.

    Burned #141 Track B / TB-F1: stale ``tools/v2.7.0`` install examples
    drifted because the release-tag form had no currency check.
    """
    skip_names = {
        "CHANGELOG.md", "CHANGELOG.en.md",
        "v2.5.0-v2.6.0-planning.md", "known-regressions.md",
        # Release SOP: intentionally shows EXAMPLE tags for all 5 version
        # lines (tools/vX, exporter/vX, portal/vX, …) to teach the tag form.
        "github-release-playbook.md",
    }
    # (regex, expected_version, label_template)
    specs = [
        (TOOLS_RELEASE_TAG_PATTERN, tools_expected, "tools/v{found}"),
        (DA_BINARY_VERSION_OUTPUT_PATTERN, tools_expected, "da-* v{found}"),
    ]
    # The --set image.tag=vX form targets the exporter chart; only check it
    # when the exporter version resolved from its source of truth.
    if exporter_expected:
        specs.append(
            (SET_IMAGE_TAG_PATTERN, exporter_expected, "--set image.tag=v{found}"))
    markers = tuple(mk.lower() for mk in VERSION_HISTORICAL_LINE_MARKERS)
    issues: List[Issue] = []
    for f in _collect_scannable_files():
        if f.name in skip_names:
            continue
        content = _read_cached(f)
        for i, line in enumerate(content.splitlines(), 1):
            low = line.lower()
            if VERSION_CURRENCY_IGNORE in low:
                continue
            if any(mk in low for mk in markers):
                continue
            for pat, expected, label in specs:
                for m in re.finditer(pat, line):
                    found = m.group(1)
                    if found != expected:
                        rel = f.relative_to(REPO_ROOT)
                        issues.append(Issue(
                            "release-tag-version", "error", str(rel), i,
                            label.format(found=found) + f" should be v{expected}",
                        ))
    return issues


def check_platform_version(expected: str) -> List[Issue]:
    """Check the platform version in `docs/**/*.md` and `docs/**/*.jsx`
    frontmatter.

    ⚠️ The old one-liner ("and inline version references") over-stated
    the scope: both scans are `re.match` on
    `PLATFORM_VERSION_FRONTMATTER_PATTERN`, which is line-anchored to a
    `version:` key. Nothing here looks at prose. #1480 copied that stale
    sentence into a CHANGELOG entry before catching it, which is what a
    docstring describing an intention rather than the code costs.
    """
    issues = []

    # Scan all docs/**/*.md frontmatter
    for f in sorted(_cached_rglob(DOCS_DIR,"*.md")):
        content = _read_cached(f)
        lines = content.splitlines()

        # Check if file has frontmatter
        if lines and lines[0].strip() == "---":
            for i, line in enumerate(lines[1:], 2):
                if line.strip() == "---":
                    break
                m = re.match(PLATFORM_VERSION_FRONTMATTER_PATTERN, line)
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
            m = re.match(PLATFORM_VERSION_FRONTMATTER_PATTERN, line)
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
    alert_count = actual["alert"]

    files_to_scan = list(_cached_rglob(DOCS_DIR,"*.md"))
    files_to_scan.extend(RULE_PACK_COUNT_CHECK_FILES)

    for f in files_to_scan:
        if not f.exists():
            continue
        if f.name in SKIP_RULE_PACK_FILES:
            continue
        content = _read_cached(f)
        rel = str(f.relative_to(REPO_ROOT))

        for i, line in enumerate(content.splitlines(), 1):
            # Check pack count patterns
            for pat, grp, _, desc in RULE_PACK_COUNT_PATTERNS[:4]:
                for m in re.finditer(pat, line, re.IGNORECASE):
                    found = m.group(grp)
                    # Determine expected value based on pattern
                    if "alert" in desc.lower():
                        expected = str(alert_count)
                    else:
                        expected = str(pack_count)

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

    for f in BILINGUAL_BADGE_CHECK_FILES:
        if not f.exists():
            continue
        content = _read_cached(f)
        rel = str(f.relative_to(REPO_ROOT))
        for i, line in enumerate(content.splitlines(), 1):
            m = re.search(BILINGUAL_PAIR_PATTERN, line)
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
    for m in re.finditer(r"^### .+?([A-Z][A-Za-z][^\n]+)", content,
                         re.MULTILINE):
        feat = m.group(1).strip()
        if feat in SKIP_FEATURE_HEADINGS:
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

    for fpath, start_pattern, desc in ROADMAP_SECTIONS:
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

    # Find zh/en pairs
    pairs = []
    for zh_file in sorted(_cached_rglob(DOCS_DIR,"*.md")):
        if ".en." in zh_file.name:
            continue
        if zh_file.name in SKIP_BILINGUAL_NUMBER_FILES:
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
        try:
            zh_content = zh_file.read_text(encoding="utf-8")
            en_content = en_file.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError):
            continue  # phantom mount or missing file — skip pair

        for pat, desc in BILINGUAL_NUMBER_PATTERNS:
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

    for f in sorted(_cached_rglob(DOCS_DIR,"*.md")):
        if ".en." in f.name:
            continue
        rel = f.relative_to(REPO_ROOT)
        rel_str = str(rel).replace("\\", "/")

        # Skip includes/, adr/, design-reviews/ individual files, and known exclusions
        parts = rel.parts
        if any(d in parts for d in DOC_MAP_SKIP_DIRS):
            continue
        if f.name in DOC_MAP_SKIP_NAMES:
            continue
        # Skip gitignored planning / draft files (see .gitignore patterns)
        if any(p.match(f.name) for p in DOC_MAP_SKIP_NAME_PATTERNS):
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
    """Check that tool-map.md lists the repo-root scripts/tools/*.py files.

    Scans the `scripts/tools/` root — a flat glob, NOT the subdirectories —
    and asks `_lib_toolcount.is_tool_file`, then verifies each result is
    referenced in docs/internal/tool-map.md.

    ⛔ #1511: this used to spell the predicate out again right here, a
    fourth hand-written copy inside the very change that collapsed the
    other three — and this docstring already claimed it called the shared
    one. ⚠️ Switching is NOT behaviour-neutral. Measured, TWO classes of
    name are globbed here but rejected by `is_tool_file`:

      `B.PY` / `tool.PY`   uppercase suffix — `Path.suffix` is
                           case-sensitive, and a case-insensitive
                           filesystem still hands them to `glob("*.py")`.
      `.py` / `..py`       leading-dot names, whose `Path.suffix` is the
                           empty string. `Path.glob` does not exclude
                           dotfiles the way `glob.glob` does, so this
                           class is not filesystem-specific — ⚠️ measured
                           on this Windows host only, not on Linux.

    Today's tree contains neither (13 root `.py`, zero disagreement
    measured), so the swap changes nothing that ships. An earlier version
    of this note claimed "one cell" and that the swap made the two
    platforms agree; only the first class supports that.

    ⚠️ The docstring used to claim this scanned `scripts/tools/`, which
    read as all of it. It does not — and the subdirectories are covered by
    a different mechanism, not by nothing: `generate_tool_map --check
    --lang all` regenerates the whole document and compares. Measured with
    `ops/blast_radius.py`'s row deleted: this function reports nothing,
    while that command exits 1 with `(missing: blast_radius.py)`, and
    `ci.yml`'s `lint` job runs it as `pre-commit run tool-map-check
    --all-files` with no `if:`, `needs:` or `continue-on-error`.

    ⚠️ What is genuinely narrow here: this function reports at `warn`, so
    even the root-level gap it does check exits 0. Widening the scan
    changes what the gate reports, so it is tracked separately.
    """
    issues = []
    tool_map = DOCS_DIR / "internal" / "tool-map.md"
    tools_dir = REPO_ROOT / "scripts" / "tools"
    if not tool_map.exists() or not tools_dir.exists():
        return issues

    map_content = tool_map.read_text(encoding="utf-8").lower()

    for f in sorted(tools_dir.glob("*.py")):
        if not is_tool_file(f):
            continue

        lookup = f.name.lower()
        if lookup not in map_content:
            issues.append(Issue(
                "tool-map-coverage", "warn",
                "docs/internal/tool-map.md", 0,
                f"tool not listed in tool-map: {f.name}",
            ))

    return issues


def _count_python_tools() -> int:
    """How many Python tools `scripts/tools/{ops,dx,lint}` holds.

    ⛔ #1483: this used to exist twice inside this module.
    `check_tool_count_in_docs` scanned the root plus `ops/`, `dx/`, `lint/`
    (220); `_auto_fix` scanned the root only (1), so `--fix` wrote
    "1 個 Python 工具" into README.md and printed `🔧 Fixed tool-count`.
    `tool-count` is a warning, so nothing went red afterwards.

    ⚠️ The first repair for that unified both halves on the **checker's**
    number — and that number is itself wrong. The scope is written into
    the sentence being checked: ``scripts/tools/{ops,dx,lint}`` 下 N 個
    Python 工具. The root is not in it. Unifying on the root-inclusive
    number would have made `--fix` write one value while
    `bump_docs --sync-counts` wrote another back — two tools fighting over
    one line, with the gate emitting a warning neither side could satisfy.

    ⛔ #1511: the scan itself lives in `scripts/tools/_lib_toolcount.py`
    now, shared with both writers, so that fight cannot restart from a
    third copy drifting. `count_scope` is deliberately NOT the scope
    `generate_tool_map` uses — read that module's docstring before
    changing either.
    """
    return len(count_scope(REPO_ROOT / "scripts" / "tools"))


def check_tool_count_in_docs() -> List[Issue]:
    """Check that CLAUDE.md and README tool counts match actual scripts/tools/*.py.

    Compares the "XX 個 Python 工具" / "XX Python tools" counts in CLAUDE.md
    and README files against `count_scope` — the tools under
    `scripts/tools/{ops,dx,lint}`.

    ⛔ TOOL_COUNT_CHECK_FILES is a list of files whose tool-count sentence
    declares THAT scope. It is not "every file that mentions a tool count",
    and the difference is load-bearing: `docs/README.{md,en.md}` also carry a
    Python-tool number, and they are deliberately absent, because their
    sentence declares a different scope — ``scripts/tools/`` whole-tree,
    "in total" in the English half. Comparing a `{ops,dx,lint}` number
    against a whole-tree claim would make the sentence wrong in a new way
    rather than right; which scope those two files should declare is a
    documentation decision, tracked in #1540 rather than settled here.

    ⚠️ "Deferred" is not "harmless": both of those files say **73**, and no
    reading of the tree produces that — `{ops,dx,lint}` is 221, the whole
    tree 238. The number has not moved since v2.1.0 (`827ee07e`), no
    `bump_docs` rule points at either file, and nothing checks them. This
    check staying out of their way is a decision about SCOPE, not a statement
    that they are currently right.

    ⚠️ `CLAUDE.md` carries no matching sentence today, so it contributes no
    findings. It is kept in the list because an unmatched FILE costs one read,
    unlike an unmatched RULE, which would be a defect (see
    `bump_docs.apply_count_updates`'s DEAD diagnosis).
    ⛔ Do NOT read that as "it would be covered if it regained one". Blind
    review checked the only tool-count sentence that file has ever had
    (`2a9078ee`): 「完整工具表（42 個 Python 工具…）見 tool-map.md」 — that
    declares the TOOL-MAP scope, root-inclusive, and carries no anchor, so
    this check would skip it in silence. `CLAUDE.md` also has no `bump_docs`
    count rule, so the writer's DEAD diagnosis would not catch it either.
    That is a blind spot the anchor introduces, disclosed rather than guessed
    at; closing it means deciding which scope `CLAUDE.md` should state, which
    is the same documentation decision as #1540's gap 1.
    """
    issues = []
    if not (REPO_ROOT / "scripts" / "tools").exists():
        return issues
    actual_count = _count_python_tools()

    for fpath in TOOL_COUNT_CHECK_FILES:
        if not fpath.exists():
            continue
        content = _read_cached(fpath)
        rel = str(fpath.relative_to(REPO_ROOT))

        for i, line in enumerate(content.splitlines(), 1):
            # ⛔ The scope is the anchor; the number is the payload. A line
            # that says "N Python tools" WITHOUT naming this scope is a claim
            # about something else, and reading it as this count is how a true
            # sentence gets rewritten into a false one — see
            # TOOL_COUNT_SCOPE_ANCHOR for the measured case.
            if TOOL_COUNT_SCOPE_ANCHOR not in line:
                continue
            occurrences = _tool_count_occurrences(line)
            if len(occurrences) > 1:
                # ⛔ Ambiguous: the anchor says this line is about the counted
                # scope, but it states more than one count, and nothing here
                # can tell which one the scope governs. Guessing is what makes
                # this dangerous — measured on the shipped README, a line
                # reading "221 Python tools under <scope>, incl. 105 Python
                # tools in lint/" had the true 105 reported as a drift and
                # then rewritten to 221 by `--fix`, which then printed
                # "All version references and counts are consistent."
                issues.append(Issue(
                    "tool-count", "warn", rel, i,
                    f"line states {len(occurrences)} counts "
                    f"({', '.join(str(n) for _d, n in occurrences)}) while "
                    f"naming the counted scope, so this check cannot tell "
                    f"which one the scope governs and will not touch it. "
                    f"Put the scope count on its own line."))
                continue
            for desc, found in occurrences:
                if found != actual_count:
                    issues.append(Issue(
                        "tool-count", "warn", rel, i,
                        f"{desc}: found {found}, actual is {actual_count}",
                    ))

    return issues


def _tool_count_occurrences(line: str) -> List[Tuple[str, int]]:
    """Every counted-tools claim on one line, as `(description, number)`.

    ⛔ Shared by the check and the repair on purpose. They used to ask this
    question in two different ways — the check per line via `finditer`, the
    repair per FILE via `re.sub` — and `\\s*` matches a newline, so the repair
    reached occurrences the check could not report and never would.
    """
    found: List[Tuple[str, int]] = []
    for pat, desc in TOOL_COUNT_PATTERNS:
        for m in re.finditer(pat, line, re.IGNORECASE):
            found.append((desc, int(m.group(1))))
    return found


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

    files_to_check = ADR_COUNT_CHECK_FILES.copy()
    files_to_check.extend([
        adr_dir / "README.md",
        adr_dir / "README.en.md",
    ])

    for fpath in files_to_check:
        if not fpath.exists():
            continue
        content = _read_cached(fpath)
        rel = str(fpath.relative_to(REPO_ROOT))

        for i, line in enumerate(content.splitlines(), 1):
            for pat, desc in ADR_COUNT_PATTERNS:
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

    files_to_check = [REPO_ROOT / "CLAUDE.md"]

    for fpath in files_to_check:
        if not fpath.exists():
            continue
        content = _read_cached(fpath)
        rel = str(fpath.relative_to(REPO_ROOT))

        for i, line in enumerate(content.splitlines(), 1):
            for pat, desc in DOC_FILE_COUNT_PATTERNS:
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

    files_to_check = [REPO_ROOT / "CLAUDE.md"]

    for fpath in files_to_check:
        if not fpath.exists():
            continue
        content = _read_cached(fpath)
        rel = str(fpath.relative_to(REPO_ROOT))

        for i, line in enumerate(content.splitlines(), 1):
            for pat, desc in SCENARIO_COUNT_PATTERNS:
                for m in re.finditer(pat, line):
                    found = int(m.group(1))
                    if found != actual_count:
                        issues.append(Issue(
                            "scenario-count", "warn", rel, i,
                            f"{desc}: found {found}, actual is {actual_count}",
                        ))

    return issues


def _auto_fix(issues: List[Issue], bilingual_pairs: int,
              rule_counts: dict, quiet: bool = False) -> List[Issue]:
    """Repair what can be repaired; return the issues actually repaired.

    *quiet* suppresses the per-repair prose line. ⛔ It exists because this
    function writes to the same stdout the `--json` document goes to (#1506):
    one `🔧 Fixed …` line ahead of the document is enough to make the whole
    output unparseable, and the repair itself is reported in the document's
    `repaired` key instead. It does NOT suppress anything else — a failure
    to write still raises.

    ⛔ Returns the issues, not a count. #1483's first attempt decided the exit
    code from "is this check id in a list of fixable ids", which is a proxy
    for "was this issue handled" and not the same thing: `rule-pack-count` is
    in that list, but the repair only rewrites two badge patterns, so the
    same id occurring in prose is never touched. `--ci --fix` then dropped a
    real error out of the exit-code decision AND out of the printed report —
    measured: a `99 個 Rule Pack` line in a doc gave `--ci` rc=1 and
    `--ci --fix` rc=0 with the message reading "0 error(s)".

    ⚠️ Reads each file fresh rather than through `_read_cached`. The cache is
    never invalidated on write, so a second issue on the same file used to
    read the pre-repair text and write it back — silently reverting the first
    repair while printing `🔧 Fixed` for both.
    """
    import stat
    repaired: List[Issue] = []

    for issue in issues:
        fpath = REPO_ROOT / issue.file
        if not fpath.exists():
            continue

        content = fpath.read_text(encoding="utf-8")
        new_content = content

        if issue.check == "bilingual-count":
            # Fix badge count using pattern from AUTO_FIX_PATTERNS
            pattern = AUTO_FIX_PATTERNS["bilingual-count"]["pattern"]
            replacement = AUTO_FIX_PATTERNS["bilingual-count"]["replacement_template"].format(value=bilingual_pairs)
            new_content = re.sub(pattern, replacement, new_content)

        elif issue.check == "tool-count":
            # Fix the counted sentence in either language — ON THE LINE THE
            # CHECKER NAMED, and only if that line still carries the scope.
            #
            # ⛔ This used to `re.sub` the whole file, and `\s*` matches a
            # newline, so it rewrote occurrences the checker never reported
            # and never could: measured, a wrapped sentence
            # (`removed 40\nPython tools that had no callers`) became
            # `removed 221\n…` while the per-line checker stayed silent about
            # it before AND after — a falsehood written under a green light.
            # A repair keyed to the finding cannot reach past it.
            #
            # ⚠️ `re.IGNORECASE` on purpose, matching `check_tool_count_in_docs`:
            # a repair that is stricter than its checker reports a form it
            # cannot rewrite, which is a warning no tool can clear (#1504).
            actual_count = _count_python_tools()
            lines = new_content.splitlines(keepends=True)
            idx = issue.line - 1
            # ⛔ Re-derived here rather than trusted from the issue: the repair
            # must not rewrite a line the check would refuse to judge. Exactly
            # one occurrence, on the line the check named, carrying the scope.
            if (0 <= idx < len(lines)
                    and TOOL_COUNT_SCOPE_ANCHOR in lines[idx]
                    and len(_tool_count_occurrences(lines[idx])) == 1):
                fixed = lines[idx]
                for pat, repl in AUTO_FIX_PATTERNS["tool-count"]["patterns"]:
                    fixed = re.sub(pat, repl.format(value=actual_count),
                                   fixed, flags=re.IGNORECASE)
                lines[idx] = fixed
                new_content = "".join(lines)

        elif issue.check == "doc-file-count":
            # Fix "XX 個文件" count from doc-map.md row count
            doc_map = REPO_ROOT / "docs" / "internal" / "doc-map.md"
            if doc_map.exists():
                map_text = doc_map.read_text(encoding="utf-8")
                rows = sum(1 for ln in map_text.splitlines()
                           if ln.startswith("|"))
                doc_count = max(0, rows - 2)
                pattern = AUTO_FIX_PATTERNS["doc-file-count"]["pattern"]
                replacement = AUTO_FIX_PATTERNS["doc-file-count"]["replacement_template"].format(value=doc_count)
                new_content = re.sub(pattern, replacement, new_content)

        elif issue.check == "rule-pack-count":
            # These are trickier — only fix clear badge patterns
            # (avoid modifying prose where context might differ)
            pack_count = rule_counts["pack_count"]
            alert_count = rule_counts["alert"]
            # Fix badge patterns using AUTO_FIX_PATTERNS
            for pat, repl_template in AUTO_FIX_PATTERNS["rule-pack-count"]["patterns"]:
                if "pack_count" in repl_template:
                    repl = repl_template.format(pack_count=pack_count)
                else:
                    repl = repl_template.format(alert_count=alert_count)
                new_content = re.sub(pat, repl, new_content)

        if new_content != content:
            fpath.write_text(new_content, encoding="utf-8", newline="\n")
            # `_auto_fix` reads fresh now, so the cache no longer causes the
            # revert bug — but leaving pre-repair text in it would hand
            # stale content to any re-verify pass added later in-process.
            _CONTENT_CACHE.pop(fpath, None)
            os.chmod(fpath,
                     stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP
                     | stat.S_IROTH)
            if not quiet:
                print(f"  🔧 Fixed {issue.check} in {issue.file}")
            repaired.append(issue)

    return repaired


def check_image_tag_v_prefix() -> List[Issue]:
    """Ensure Docker image tags use v-prefix convention consistently.

    Convention (aligned with CI release.yaml):
      - Docker images: da-tools:v<ver>, threshold-exporter:v<ver> (v-prefixed)
      - Helm OCI chart: charts/threshold-exporter:<ver> (no v, SemVer)

    Detects bare version tags (e.g. da-tools:2.0.0) that should be v-prefixed.
    Skips CI release.yaml (uses variable interpolation) and CHANGELOG (historical).
    """
    issues = []

    skip_names = {"release.yaml", "CHANGELOG.md", "CHANGELOG.en.md"}

    for f in _collect_scannable_files():
        if not f.exists() or f.name in skip_names:
            continue
        content = _read_cached(f)
        for i, line in enumerate(content.splitlines(), 1):
            for m in re.finditer(BARE_TAG_PATTERN, line):
                rel = f.relative_to(REPO_ROOT)
                ver = m.group(1)
                # Image name is the matched text left of the ':' (works for all
                # 4 component images without brittle context sniffing).
                img = m.group(0).rsplit(":", 1)[0]
                issues.append(Issue(
                    "image-tag-v-prefix", "error", str(rel), i,
                    f"{img}:{ver} missing v prefix, should be {img}:v{ver}",
                ))
    return issues


def check_e2e_and_jsx_versions(expected_platform: str) -> List[Issue]:
    """Check e2e/package.json and JSX frontmatter versions match platform version.

    v2.6.0: Expanded coverage per v2.5.0 Lesson Learned #4 (version sync gaps).
    Checks:
      - tests/e2e/package.json "version" field
      - JSX tool frontmatter "version:" field
    """
    issues = []
    e2e_pkg = REPO_ROOT / "tests" / "e2e" / "package.json"

    # Check e2e/package.json
    #
    # ⛔ #1484: every way this file can fail to yield a version is an ERROR
    # that names it. Measured on the old code, six shapes returned no issue
    # at all or killed the run: a missing `version` key, an empty string,
    # malformed JSON, `"version": null`, a non-string version, and the file
    # being absent entirely. Of the two checks #1480 brought back to
    # life this is the one that caught real damage, and any of those
    # shapes switched it off. ⚠️ Not "the only check in the gate that
    # ever caught drift" — an earlier draft said that and it is false:
    # `bilingual-count`, `tool-count` and `bilingual-numbers` were
    # between them reporting six real drifts on the same tree.
    #
    # ⚠️ `_UNREAD` rather than `None` as the "not parsed yet" sentinel:
    # `null` is legal JSON, so `json.loads` returning `None` is a *result*.
    # Using `None` for both collapsed them and let a file containing just
    # `null` pass silently — a bug introduced by the first attempt at this
    # very fix, and found by blind review.
    rel = str(e2e_pkg.relative_to(REPO_ROOT)) if e2e_pkg.is_absolute() \
        else str(e2e_pkg)
    if not e2e_pkg.exists():
        issues.append(Issue(
            "e2e-package-version", "error", rel, 0,
            "is missing, so the version it pins was never compared against "
            "the platform version; restore it or drop this check",
        ))
    else:
        _UNREAD = object()
        pkg = _UNREAD
        try:
            pkg = json.loads(e2e_pkg.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
            issues.append(Issue(
                "e2e-package-version", "error", rel, 0,
                f"could not be read, so its version was never compared "
                f"against the platform version: {exc}",
            ))
        if pkg is not _UNREAD:
            found = pkg.get("version") if isinstance(pkg, dict) else None
            if not isinstance(pkg, dict):
                issues.append(Issue(
                    "e2e-package-version", "error", rel, 0,
                    f"top level is {type(pkg).__name__}, not an object, so "
                    f"it carries no version to compare",
                ))
            elif not isinstance(found, str) or not found.strip():
                issues.append(Issue(
                    "e2e-package-version", "error", rel, 0,
                    f"has no usable version string to compare against the "
                    f"platform version (found {found!r})",
                ))
            else:
                # Normalize: remove leading 'v' for comparison
                norm_expected = expected_platform.lstrip("v")
                norm_found = found.lstrip("v")
                if norm_found != norm_expected:
                    issues.append(Issue(
                        "e2e-package-version", "error", rel, 0,
                        f"version {found} does not match the platform "
                        f"version {expected_platform}",
                    ))

    # Check JSX frontmatter versions
    #
    # ⛔ The directory this scanned — `docs/interactive/tools` — stopped
    # existing on 2026-05-07 (`b439c427`, the portal monorepo restructure,
    # #279/TD-042), which moved the sources to `tools/portal/src`. The
    # `if jsx_dir.exists():` then made three and a half months of drift
    # invisible: measured on this tree, 68 `.jsx` files, 49 of them carrying
    # a `version:`, one of them (`getting-started/wizard.jsx`) still at
    # 2.7.0. Nothing else covered it — `lint_tool_consistency` checks
    # `related:` not `version:`, and `check_frontmatter_versions` only walks
    # `docs/**/*.md`.
    #
    # ⚠️ Missing directory is now an error, not a skip: "the source tree
    # moved again" and "there is nothing to check" are the same picture
    # otherwise, and that is exactly how this half died the first time.
    # ⚠️ Severity is `error`, not `warn`: as a warning it could never make
    # `--ci` non-zero, so even after the path was corrected nothing would
    # have stopped the drift from shipping.
    jsx_dir = REPO_ROOT / "tools" / "portal" / "src"
    if not jsx_dir.is_dir():
        issues.append(Issue(
            "jsx-frontmatter-version", "error",
            str(jsx_dir.relative_to(REPO_ROOT)), 0,
            "the JSX source tree is not where this check looks, so no JSX "
            "frontmatter version was compared; update the path here rather "
            "than letting the check disappear",
        ))
    else:
        for jsx_file in sorted(jsx_dir.rglob("*.jsx")):
            # The e2e half turns an unreadable file into a named Issue; this
            # half used to let it raise. A file that is unreadable, or that
            # disappears between the rglob and the read, would abort the
            # whole validator with a traceback and no Issue at all — the
            # same "the check went quiet" class this block argues against,
            # in crash form.
            try:
                content = jsx_file.read_text(encoding="utf-8",
                                             errors="replace")
            except OSError as exc:
                issues.append(Issue(
                    "jsx-frontmatter-version", "error",
                    str(jsx_file.relative_to(REPO_ROOT)), 0,
                    f"could not be read, so its frontmatter version was "
                    f"never compared: {exc}",
                ))
                continue
            # JSX frontmatter is between --- delimiters
            if not content.startswith("---"):
                continue
            end = content.find("---", 3)
            if end <= 0:
                continue
            m = re.search(r"^version:\s*v?(\S+)", content[3:end], re.MULTILINE)
            if not m:
                continue
            found = m.group(1)
            norm = expected_platform.lstrip("v")
            if found != norm and found != expected_platform:
                issues.append(Issue(
                    "jsx-frontmatter-version", "error",
                    str(jsx_file.relative_to(REPO_ROOT)), 0,
                    f"version {found} does not match the platform version "
                    f"{expected_platform}",
                ))
    return issues


def check_mkdocs_extra_versions(versions: Dict[str, str]) -> List[Issue]:
    """Check mkdocs.yml extra vars match source-of-truth versions.

    v2.4.0 新增：mkdocs.yml extra.platform_version / exporter_version / tools_version
    是文件網站的版號來源，必須與 VERSION / Chart.yaml 一致。
    """
    issues = []
    mkdocs_path = REPO_ROOT / "mkdocs.yml"
    if not mkdocs_path.exists():
        return issues

    content = _read_cached(mkdocs_path)

    for i, line in enumerate(content.splitlines(), 1):
        for key, source_key in MKDOCS_EXTRA_CHECKS:
            expected = versions.get(source_key)
            if expected is None:
                continue
            m = re.match(rf'\s+{key}:\s*"([^"]+)"', line)
            if m:
                found = m.group(1)
                if found != expected:
                    issues.append(Issue(
                        "mkdocs-extra-version", "error", "mkdocs.yml", i,
                        f'{key}: "{found}" should be "{expected}"',
                    ))
    return issues


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    """CLI entry point: 文件版號與計數一致性檢查."""
    try_utf8_stdout()
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
    if "tools" in versions:
        all_issues.extend(check_release_tag_currency(
            versions["tools"], versions.get("exporter")))
    if "platform" in versions:
        all_issues.extend(check_platform_version(versions["platform"]))
    # ⛔ Fail-closed on EVERY source of truth, not just the one #1480 named.
    #
    # The key set is DERIVED from `MKDOCS_EXTRA_CHECKS` (the existing
    # declaration of "these are the versions this tool knows about") rather
    # than retyped here, so a fourth SSOT added there is covered on the day
    # it lands instead of quietly joining the fail-open set.
    #
    # Measured before this loop existed: writing `v2.9.0` instead of `2.9.0`
    # into `components/da-tools/app/VERSION` — one extra character, a
    # release-day typo — made `read_source_versions()` drop the `tools` key,
    # which silently switched off `check_da_tools_version`,
    # `check_release_tag_currency` and the mkdocs `tools_version` row: **161
    # errors became 0 and the tool exited 0**. Only `platform` had a guard,
    # because `platform` was the key the issue happened to name.
    #
    # ⛔ The message deliberately does NOT list which checks were skipped.
    # An earlier version did, named two of them, and was already wrong — the
    # platform version has three consumers, not two. A list of dependants is
    # a description that rots silently every time someone adds one; "every
    # check that compares against it" cannot.
    for _ssot_key in sorted({_key for _, _key in MKDOCS_EXTRA_CHECKS}):
        if _ssot_key in versions:
            continue
        all_issues.append(Issue(
            f"{_ssot_key}-version-source", "error",
            _ssot_source_label(_ssot_key), 0,
            f"could not read the {_ssot_key} version from its source of "
            f"truth, so every check that compares against it was skipped "
            f"rather than run. Fix the reader; do not remove this error."))

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
    all_issues.extend(check_mkdocs_extra_versions(versions))
    if "platform" in versions:
        all_issues.extend(check_e2e_and_jsx_versions(versions["platform"]))

    # --fix mode: auto-fix fixable issues.
    #
    # ⛔ #1506: this used to be its own exit — repair, print, `return` —
    # sitting BEFORE the report block, which is the only place `args.json` is
    # ever read. So `--json` was silently inert whenever `--fix` was passed
    # too: a caller asking for JSON got `🔧 Fixed …` prose and a parse error
    # on line 1. #1483 had already repaired the other half of the same early
    # return (the exit code); the format half outlived it because the two
    # halves are decided in two different places. Repair now feeds the SAME
    # report path as every other run, so there is one exit and exactly one
    # place that knows what `--json` means.
    repaired: List[Issue] = []
    if args.fix and all_issues:
        repaired = _auto_fix(all_issues, bilingual_pairs, rule_counts,
                             quiet=args.json)
        if not args.json:
            if repaired:
                print(f"Auto-fixed {len(repaired)} issue(s). Re-run to verify.")
            else:
                print("No auto-fixable issues found.")

    # What the report and the exit code are both about.
    #
    # ⛔ `not in` here is an IDENTITY test on purpose: `Issue` defines no
    # `__eq__`, and `_auto_fix` hands back the very objects it repaired. Two
    # issues that merely look alike (one check id on two lines of one file)
    # must not cancel each other out.
    standing = [i for i in all_issues if i not in repaired]
    errors = [i for i in standing if i.severity == "error"]
    warnings = [i for i in standing if i.severity == "warn"]

    if args.json:
        result = {
            "source_of_truth": {
                "platform": versions.get("platform"),
                "exporter": versions.get("exporter"),
                "tools": versions.get("tools"),
                "rule_packs": rule_counts,
                "bilingual_pairs": bilingual_pairs,
            },
            # Post-repair state, so `issues` and `summary` describe the tree as
            # it stands when the process exits — the same thing the exit code
            # describes. What `--fix` changed is reported in its own key rather
            # than by making `issues` mean two things depending on a flag.
            "issues": [i.to_dict() for i in standing],
            "repaired": [i.to_dict() for i in repaired],
            "summary": {
                "errors": len(errors),
                "warnings": len(warnings),
                "repaired": len(repaired),
            },
        }
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        if standing:
            if repaired:
                print()
                print(f"  {len(standing)} issue(s) still standing after --fix "
                      f"({len(errors)} error(s)):")
            for issue in standing:
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
        sys.exit(EXIT_VIOLATION)


if __name__ == "__main__":
    main()
