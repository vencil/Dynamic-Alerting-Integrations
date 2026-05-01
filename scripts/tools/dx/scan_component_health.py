#!/usr/bin/env python3
"""scan_component_health.py — JSX 元件健康快照（v2.7.0 Phase .a A-1 首發）

掃描 tool-registry.yaml 中註冊的 JSX 工具，產出結構化健康資料：
Tier 分級（多訊號加權，DEC-08）、i18n 覆蓋、Design Token 遵循、
Token Density / Migration Group 自動分群（v2.7.0 Day 4 DEC-M 新增）、
Playwright 覆蓋、git 活躍度。

**純讀取，不修改任何檔案。**

用法:
  # 基本執行（產出 docs/internal/component-health-snapshot.json）
  python3 scripts/tools/dx/scan_component_health.py

  # 指定輸出路徑
  python3 scripts/tools/dx/scan_component_health.py --output /tmp/snapshot.json

  # 只印 summary，不寫檔
  python3 scripts/tools/dx/scan_component_health.py --summary-only

  # JSON 輸出到 stdout（CI / pipeline 用）
  python3 scripts/tools/dx/scan_component_health.py --stdout

Tier 判準（多訊號加權，見 v2.7.0-planning.md §10 DEC-08）:
  score = LOC(0-3) + Audience(0-2) + Phase(0-2) + Writer(0-2) + Recency(-1~+1)
  Tier 1: score ≥ 7   Tier 2: 4-6   Tier 3: ≤ 3
  deprecation_candidate override: LOC<100+stale 或 writer=0+audience=narrow

Archived (registry opt-in, v2.8.0 A-5b):
  若 tool-registry.yaml 項目含 `status: archived`，產生的 entry tier="Archived"、
  status="ARCHIVED"，並從 tier_distribution / token_group_distribution /
  playwright_coverage / i18n aggregates / hex|px offenders 分母中排除。
  scan 仍計算 LOC / i18n 作為 visibility 指標（非評分）。
  `archive_candidates` 列出自動建議（Tier3 deprecation_candidate + 長期未動 + 極小 LOC）。
"""
from __future__ import annotations
import argparse
import json
import re
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import yaml

# --- 自動偵測 repo 根目錄（從 script 位置往上找 .git） ---
def _find_repo_root(start: Path) -> Path:
    for p in [start, *start.parents]:
        if (p / ".git").exists():
            return p
    raise RuntimeError(f"Cannot locate repo root from {start}")

REPO = _find_repo_root(Path(__file__).resolve())
REGISTRY = REPO / "docs/assets/tool-registry.yaml"
E2E_DIR = REPO / "tests/e2e"
JSX_ROOT = REPO / "docs"
DEFAULT_OUTPUT = REPO / "docs/internal/component-health-snapshot.json"

# --- Regex / 常數 ---
_HEX_RE = re.compile(r"#[0-9a-fA-F]{3,8}\b")
_PX_RE = re.compile(r"\b([1-9]\d*)px\b")
_TOKEN_RE = re.compile(r"var\(\s*--da-[\w-]+\s*\)")
# Tailwind palette utilities (for measuring "unmigrated" Tailwind usage that
# competes with design tokens). Matches slate / blue / gray / neutral palette.
_TAILWIND_PALETTE_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:bg|text|border|ring|from|to|via|fill|stroke)-"
    r"(?:slate|gray|zinc|neutral|blue|sky|indigo|red|green|emerald|amber|yellow|orange)-"
    r"(?:50|100|200|300|400|500|600|700|800|900|950)"
    r"(?![A-Za-z0-9_-])"
)
_I18N_DECL_RE = re.compile(r"=\s*window\.__t\b")
_I18N_CALL_RE = re.compile(r"(?<![A-Za-z0-9_\.])t\(\s*['\"`]")
_CJK_PATTERNS = [
    re.compile(r'"[^"\n]*[\u4e00-\u9fff]+[^"\n]*"'),
    re.compile(r"'[^'\n]*[\u4e00-\u9fff]+[^'\n]*'"),
    re.compile(r"`[^`\n]*[\u4e00-\u9fff]+[^`\n]*`"),
]
_WRITER_NAME_RE = re.compile(
    r"\b(wizard|manager|editor|setup|generator|heatmap|playground)\b",
    re.IGNORECASE,
)
_WRITER_CONTENT_RE = re.compile(
    r"fetch\([^)]*method[^)]*(POST|PUT|PATCH|DELETE)"
    r"|onSubmit\s*="
    r"|\bapiCall\b"
    r"|window\.confirm\("
)

# --- 工具函式 ---
def git_log(fmt: str, path: Path, reverse: bool = False) -> str:
    rel = path.relative_to(REPO)
    args = ["git", "log"]
    if reverse:
        args.append("--reverse")
    else:
        args.append("-1")
    args += [f"--format={fmt}", "--", str(rel)]
    try:
        out = subprocess.check_output(
            args, cwd=REPO, text=True, stderr=subprocess.DEVNULL, timeout=30,
        ).strip()
        if reverse and out:
            return out.splitlines()[0]
        return out
    except subprocess.CalledProcessError:
        return ""


def build_git_mtime_cache(paths: list[Path]) -> tuple[dict, dict]:
    """Batch-fetch first/last modified date for every path in a single
    ``git log --name-only`` pass. Much faster than N per-file calls
    on FUSE mounts (where each process spawn costs ~2-3s).

    Returns (last_mtime_map, first_mtime_map) keyed by posix path.
    """
    wanted = {p.relative_to(REPO).as_posix() for p in paths}
    last_map: dict[str, str] = {}
    first_map: dict[str, str] = {}
    try:
        out = subprocess.check_output(
            ["git", "log", "--name-only", "--format=__COMMIT__%x00%ai"],
            cwd=REPO, text=True, stderr=subprocess.DEVNULL, timeout=120,
        )
    except subprocess.CalledProcessError:
        return last_map, first_map

    current_date = ""
    for line in out.splitlines():
        if line.startswith("__COMMIT__"):
            # line format: __COMMIT__\x002026-04-16 12:34:56 +0800
            parts = line.split("\x00", 1)
            current_date = parts[1] if len(parts) > 1 else ""
            continue
        if not line or line not in wanted:
            continue
        # git log iterates newest → oldest, so first hit = last_mtime,
        # subsequent hits keep overwriting first_mtime until we reach the end.
        if line not in last_map:
            last_map[line] = current_date
        first_map[line] = current_date
    return last_map, first_map


def count_hex_colors(content: str) -> tuple[int, int]:
    total = hardcoded = 0
    for line in content.splitlines():
        matches = _HEX_RE.findall(line)
        if not matches:
            continue
        total += len(matches)
        stripped = line.strip()
        if stripped.startswith("*") or stripped.startswith("//"):
            continue
        hardcoded += len(matches)
    return total, hardcoded


def count_cjk_strings(content: str) -> int:
    return sum(len(p.findall(content)) for p in _CJK_PATTERNS)


def count_design_tokens(content: str) -> int:
    """Count `var(--da-*)` token occurrences (Phase .a0 migration progress)."""
    return len(_TOKEN_RE.findall(content))


def count_tailwind_palette(content: str) -> int:
    """Count hardcoded Tailwind palette utility classes (unmigrated legacy)."""
    return len(_TAILWIND_PALETTE_RE.findall(content))


def classify_token_group(
    tokens: int, tailwind_palette: int, hex_hardcoded: int
) -> str:
    """Classify into design-token migration groups (DEC-J Day 3 finding).

    Group A: mature / model tool — high token density, no competing Tailwind palette.
    Group B: migration in progress — some tokens AND some Tailwind or hex.
    Group C: not started — zero or near-zero tokens.
    N/A:     content too small or no styling signals.
    """
    if tokens >= 80 and tailwind_palette <= 5 and hex_hardcoded <= 5:
        return "A"  # mature
    if tokens >= 20:
        return "B"  # in-progress
    if tailwind_palette == 0 and hex_hardcoded == 0 and tokens == 0:
        return "N/A"  # stylistically trivial
    return "C"  # unmigrated


# --- Tier 評分 ---
def _audience_score(audience: list[str]) -> int:
    s = set(audience or [])
    has_tenant = "tenant" in s
    non_tenant = s - {"tenant"}
    if has_tenant and non_tenant:
        return 2
    if len(s) >= 2:
        return 1
    return 0


def _phase_score(phase: str) -> int:
    if phase in {"deploy", "configure"}:
        return 2
    if phase in {"monitor", "troubleshoot"}:
        return 1
    return 0


def _loc_score(loc: int) -> int:
    if loc >= 800:
        return 3
    if loc >= 400:
        return 2
    if loc >= 200:
        return 1
    return 0


def _recency_score(last_modified_iso: str, today: datetime) -> int:
    if not last_modified_iso:
        return 0
    try:
        dt = datetime.strptime(last_modified_iso, "%Y-%m-%d %H:%M:%S %z")
    except ValueError:
        return 0
    delta_days = (today - dt).days
    if delta_days <= 90:
        return 1
    if delta_days <= 180:
        return 0
    return -1


def _writer_score(key: str, content: str) -> int:
    if _WRITER_NAME_RE.search(key) or _WRITER_CONTENT_RE.search(content):
        return 2
    return 0


def derive_tier(
    tool: dict, content: str, loc: int, last_modified: str, today: datetime
) -> tuple[str, int, dict]:
    breakdown = {
        "loc": _loc_score(loc),
        "audience": _audience_score(tool.get("audience", []) or []),
        "phase": _phase_score(tool.get("journey_phase", "")),
        "writer": _writer_score(tool["key"], content),
        "recency": _recency_score(last_modified, today),
    }
    score = sum(breakdown.values())
    breakdown["_total"] = score

    is_stub_stale = loc < 100 and breakdown["recency"] < 0
    if is_stub_stale:
        return "Tier 3 (deprecation_candidate)", score, breakdown
    if score >= 7:
        return "Tier 1", score, breakdown
    if score >= 4:
        return "Tier 2", score, breakdown
    if breakdown["writer"] == 0 and breakdown["audience"] == 0:
        return "Tier 3 (deprecation_candidate)", score, breakdown
    return "Tier 3", score, breakdown


def compute_i18n_coverage(i18n_calls: int, cjk_hardcoded: int) -> float | None:
    if i18n_calls == 0 and cjk_hardcoded == 0:
        return None
    total = i18n_calls + cjk_hardcoded
    return round(i18n_calls / total, 3) if total else None


def _is_archive_candidate(entry: dict, today: datetime) -> tuple[bool, str]:
    """Suggest archival for Tier 3 deprecation_candidates that look truly dead.

    Criteria (all required):
      - Tier == "Tier 3 (deprecation_candidate)"
      - LOC < 50 (stub-sized)
      - tier_breakdown.recency == -1 (>180 days since last modified)
      - tier_breakdown.writer == 0 (no mutation surface)
      - not playwright_spec
      - first_commit older than 365 days (not a WIP that just got parked)

    Returns (True, human-readable reason) or (False, "").
    """
    if entry.get("tier") != "Tier 3 (deprecation_candidate)":
        return False, ""
    loc = entry.get("loc") or 0
    breakdown = entry.get("tier_breakdown") or {}
    if loc >= 50:
        return False, ""
    if breakdown.get("recency", 0) != -1:
        return False, ""
    if breakdown.get("writer", 0) != 0:
        return False, ""
    if entry.get("playwright_spec"):
        return False, ""
    first_commit = entry.get("first_commit") or ""
    try:
        dt = datetime.strptime(first_commit, "%Y-%m-%d %H:%M:%S %z")
    except ValueError:
        return False, ""
    if (today - dt).days <= 365:
        return False, ""
    return True, (
        f"LOC={loc}, recency>180d, writer=0, no-spec, first_commit>365d"
    )


# --- 主流程 ---
def scan(today: datetime | None = None) -> dict:
    today = today or datetime.now(timezone.utc)
    registry = yaml.safe_load(REGISTRY.read_text(encoding="utf-8"))
    tools = registry["tools"]
    spec_names = {p.stem.replace(".spec", "") for p in E2E_DIR.glob("*.spec.ts")}

    # Batch git log (single subprocess) — orders of magnitude faster than
    # per-file calls on FUSE mounts.
    tool_paths = [JSX_ROOT / t["file"] for t in tools if (JSX_ROOT / t["file"]).exists()]
    last_mtime_map, first_mtime_map = build_git_mtime_cache(tool_paths)

    results = []
    for tool in tools:
        file_path = JSX_ROOT / tool["file"]
        entry = {
            "key": tool["key"],
            "file": tool["file"],
            "title_en": tool.get("title", {}).get("en", ""),
            "audience": tool.get("audience", []) or [],
            "journey_phase": tool.get("journey_phase", ""),
            "hub_section": tool.get("hub_section", ""),
            "appears_in": tool.get("appears_in", []) or [],
        }
        if not file_path.exists():
            entry.update({"status": "MISSING", "tier": "N/A"})
            results.append(entry)
            continue

        content = file_path.read_text(encoding="utf-8", errors="replace")
        loc = content.count("\n") + 1
        i18n_enabled = bool(_I18N_DECL_RE.search(content))
        i18n_calls = len(_I18N_CALL_RE.findall(content))
        cjk_strings = count_cjk_strings(content)
        cjk_hardcoded = max(0, cjk_strings - i18n_calls)
        hex_total, hex_hardcoded = count_hex_colors(content)
        px_count = len(_PX_RE.findall(content))
        design_tokens = count_design_tokens(content)
        tailwind_palette = count_tailwind_palette(content)
        token_density_per_100_loc = (
            round(design_tokens / loc * 100, 1) if loc > 0 else 0.0
        )
        has_spec = tool["key"] in spec_names
        rel_posix = file_path.relative_to(REPO).as_posix()
        last_modified = last_mtime_map.get(rel_posix, "")
        first_commit = first_mtime_map.get(rel_posix, "")

        # A-5b: registry-opt-in archive. Archived tools still show LOC / i18n
        # for visibility but are excluded from tier / token / coverage aggregates.
        if tool.get("status") == "archived":
            entry.update({
                "status": "ARCHIVED",
                "loc": loc,
                "tier": "Archived",
                "i18n_enabled": i18n_enabled,
                "i18n_calls": i18n_calls,
                "cjk_strings_total": cjk_strings,
                "cjk_hardcoded_strings": cjk_hardcoded,
                "i18n_coverage_ratio": compute_i18n_coverage(i18n_calls, cjk_hardcoded),
                "hex_colors_total": hex_total,
                "hex_colors_hardcoded": hex_hardcoded,
                "px_hardcoded": px_count,
                "design_tokens": design_tokens,
                "tailwind_palette": tailwind_palette,
                "token_density_per_100_loc": token_density_per_100_loc,
                "playwright_spec": has_spec,
                "last_modified": last_modified,
                "first_commit": first_commit,
                "archived_reason": tool.get("archived_reason", ""),
            })
            results.append(entry)
            continue

        token_group = classify_token_group(design_tokens, tailwind_palette, hex_hardcoded)
        tier, tier_score, tier_breakdown = derive_tier(
            tool, content, loc, last_modified, today
        )

        entry.update({
            "status": "OK",
            "loc": loc,
            "tier": tier,
            "tier_score": tier_score,
            "tier_breakdown": tier_breakdown,
            "i18n_enabled": i18n_enabled,
            "i18n_calls": i18n_calls,
            "cjk_strings_total": cjk_strings,
            "cjk_hardcoded_strings": cjk_hardcoded,
            "i18n_coverage_ratio": compute_i18n_coverage(i18n_calls, cjk_hardcoded),
            "hex_colors_total": hex_total,
            "hex_colors_hardcoded": hex_hardcoded,
            "px_hardcoded": px_count,
            "design_tokens": design_tokens,
            "tailwind_palette": tailwind_palette,
            "token_density_per_100_loc": token_density_per_100_loc,
            "token_group": token_group,
            "playwright_spec": has_spec,
            "last_modified": last_modified,
            "first_commit": first_commit,
        })
        results.append(entry)

    registered = {t["file"] for t in tools}
    all_jsx = [p.relative_to(JSX_ROOT).as_posix() for p in JSX_ROOT.rglob("*.jsx")]
    unregistered = sorted(set(all_jsx) - registered)

    # A-5b: split active vs archived. Archived excluded from all derived metrics
    # so tier/token/coverage denominators reflect the *live* surface area.
    active_results = [r for r in results if r["status"] == "OK"]
    archived_results = [r for r in results if r["status"] == "ARCHIVED"]
    archived_tools = sorted(r["key"] for r in archived_results)

    tier_dist = Counter(r["tier"] for r in active_results)
    with_spec = sum(1 for r in active_results if r.get("playwright_spec"))
    tier1_nospec = sum(
        1 for r in active_results
        if r.get("tier") == "Tier 1" and not r.get("playwright_spec")
    )
    hex_offenders = sum(
        1 for r in active_results if r.get("hex_colors_hardcoded", 0) > 0
    )
    px_offenders = sum(1 for r in active_results if r.get("px_hardcoded", 0) > 0)
    token_group_dist = Counter(r.get("token_group", "N/A") for r in active_results)
    tier1_group_c = [
        r["key"]
        for r in active_results
        if r.get("tier") == "Tier 1" and r.get("token_group") == "C"
    ]
    tier1_group_a = [
        r["key"]
        for r in active_results
        if r.get("tier") == "Tier 1" and r.get("token_group") == "A"
    ]
    i18n_vals = [
        r["i18n_coverage_ratio"]
        for r in active_results
        if r.get("i18n_coverage_ratio") is not None
    ]
    low_i18n = sorted(
        [r for r in active_results if r.get("i18n_coverage_ratio") is not None],
        key=lambda r: (r["i18n_coverage_ratio"], -r["cjk_hardcoded_strings"]),
    )[:5]

    # A-5b: auto-suggestion — flag deprecation_candidates that look truly dead.
    # Warning-only (Q2 policy); maintainers decide whether to opt in to `status: archived`.
    archive_candidates = []
    for r in active_results:
        is_candidate, reason = _is_archive_candidate(r, today)
        if is_candidate:
            archive_candidates.append({"key": r["key"], "reason": reason})
    archive_candidates.sort(key=lambda c: c["key"])

    summary = {
        "generated_at": today.strftime("%Y-%m-%d"),
        "phase": "v2.7.0 Phase .a A-1",
        "total_registered_tools": len(tools),
        "total_active_tools": len(active_results),
        "archived_count": len(archived_results),
        "archived_tools": archived_tools,
        "archive_candidates": archive_candidates,
        "total_jsx_files_on_disk": len(all_jsx),
        "unregistered_jsx_files": unregistered,
        "tier_distribution": dict(tier_dist),
        "playwright_coverage": f"{with_spec}/{len(active_results)}",
        "tier1_without_spec": tier1_nospec,
        "tools_with_hardcoded_hex": hex_offenders,
        "tools_with_hardcoded_px": px_offenders,
        "token_group_distribution": dict(token_group_dist),
        "tier1_token_group_a_mature": sorted(tier1_group_a),
        "tier1_token_group_c_unmigrated": sorted(tier1_group_c),
        "i18n_coverage_distribution": {
            "samples": len(i18n_vals),
            "min": min(i18n_vals) if i18n_vals else None,
            "max": max(i18n_vals) if i18n_vals else None,
            "avg": round(sum(i18n_vals) / len(i18n_vals), 3) if i18n_vals else None,
        },
        "low_i18n_coverage_top5": [
            {
                "key": r["key"],
                "coverage": r["i18n_coverage_ratio"],
                "i18n_calls": r["i18n_calls"],
                "cjk_hardcoded": r["cjk_hardcoded_strings"],
            }
            for r in low_i18n
        ],
    }
    return {"summary": summary, "tools": results}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--output", type=Path, default=DEFAULT_OUTPUT,
        help=f"Output JSON path (default: {DEFAULT_OUTPUT.relative_to(REPO)})",
    )
    parser.add_argument("--summary-only", action="store_true", help="Skip writing JSON file")
    parser.add_argument("--stdout", action="store_true", help="Emit full JSON to stdout instead of file")
    parser.add_argument("--today", type=str, help="Override today's date (ISO, for deterministic testing)")
    args = parser.parse_args()

    today = datetime.now(timezone.utc)
    if args.today:
        today = datetime.fromisoformat(args.today).replace(tzinfo=timezone.utc)

    data = scan(today=today)

    if args.stdout:
        json.dump(data, sys.stdout, indent=2, ensure_ascii=False)
        print()
        return 0

    print(json.dumps(data["summary"], indent=2, ensure_ascii=False))

    if not args.summary_only:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"\nWrote: {args.output.relative_to(REPO)}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
