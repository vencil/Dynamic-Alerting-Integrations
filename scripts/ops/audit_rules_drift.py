#!/usr/bin/env python3
"""audit_rules_drift.py — 季度 rule-corpus drift 稽核 (TRK-307).

掃 dev-rules / pre-commit hooks / vibe skills / memory feedback cards，產一份
markdown drift report，標出：count 切分漂移、重複候選、feedback cross-ref /
orphan、hook↔dev-rule 覆蓋缺口、stale feedback。

定位
----
MANUAL 季度工具（不入 CI；out-of-scope：自動刪除 / 合併 — 只產 report，人工決定）。
與 `anthropic-skills:consolidate-memory` 互補：後者只掃 ~/.claude memory，本工具
補 repo 內規則語料（dev-rules / hooks / skills）。

DIY 理由（lint-adoption-policy）
--------------------------------
此為 Vibe-specific 異質規則語料 drift 偵測，無對應 open-source engine；相似度用
stdlib difflib 即足。policy 規定「DIY only when meaningful divergence」——此處成立。
本工具非 lint（不 gate commit），是 audit report generator。

用法
----
    python3 scripts/ops/audit_rules_drift.py            # 寫 report 到 audit-reports/
    python3 scripts/ops/audit_rules_drift.py --stdout   # 只印到 stdout，不寫檔
    python3 scripts/ops/audit_rules_drift.py --memory-dir <path>  # 指定 memory 目錄

memory feedback 目錄預設在 ~/.claude/...（user-local，不在 repo / CI）；不存在時
跳過 feedback 相關檢查並於 report 註記（CI 環境的正常行為）。
"""
from __future__ import annotations

import argparse
import difflib
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

# Reuse shared atomic-write helper (LF-forcing + sibling-tmp + os.replace).
# Same import pattern as scripts/dx/generate_planning_index.py.
_TOOLS_DX = Path(__file__).resolve().parent.parent / "tools" / "dx"
sys.path.insert(0, str(_TOOLS_DX))
from _atomic_write import atomic_write_text  # noqa: E402

# Make stdout tolerate non-ASCII on legacy Windows consoles.
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

REPO_ROOT = Path(__file__).resolve().parents[2]
DEV_RULES = REPO_ROOT / "docs" / "internal" / "dev-rules.md"
PRECOMMIT = REPO_ROOT / ".pre-commit-config.yaml"
SKILLS_DIR = REPO_ROOT / ".claude" / "skills"
CLAUDE_MD = REPO_ROOT / "CLAUDE.md"
MEMORY_INDEX_NAME = "MEMORY.md"
DEFAULT_MEMORY = (
    Path.home()
    / ".claude"
    / "projects"
    / "C--Users-vencs-vibe-k8s-lab"
    / "memory"
)
DEFAULT_REPORT_DIR = REPO_ROOT / "docs" / "internal" / "audit-reports"

SIM_THRESHOLD = 0.60  # difflib ratio above which two texts are a duplication candidate
STALE_DAYS = 120  # feedback card untouched longer than this = stale candidate


def _norm(text: str) -> str:
    """Normalize markdown text for fuzzy similarity: drop code/links/punct, fold ws."""
    text = re.sub(r"`[^`]*`", " ", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"[#*_>|\-]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip().lower()


def _ratio(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, _norm(a), _norm(b)).ratio()


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _frontmatter(text: str) -> tuple[dict, str]:
    """Split a `---\\n...\\n---\\n` frontmatter block; return (meta, body)."""
    m = re.match(r"\A---\s*\n(.*?)\n---\s*\n(.*)\Z", text, re.DOTALL)
    if not m:
        return {}, text
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        meta = {}
    return (meta if isinstance(meta, dict) else {}), m.group(2)


# --------------------------------------------------------------------------- loaders


def load_dev_rules() -> list[dict]:
    """Extract `### ` headings + bodies from dev-rules.md."""
    text = _read(DEV_RULES)
    rules: list[dict] = []
    parts = re.split(r"^### (.+)$", text, flags=re.MULTILINE)
    # parts[0] = preamble; then (heading, body) pairs
    for i in range(1, len(parts), 2):
        heading = parts[i].strip()
        body = parts[i + 1] if i + 1 < len(parts) else ""
        rules.append({"heading": heading, "body": body.strip()})
    return rules


def load_hooks() -> list[dict]:
    """Parse .pre-commit-config.yaml → [{id, name, stage}]. stage in auto|manual|pre-push."""
    data = yaml.safe_load(_read(PRECOMMIT)) or {}
    hooks: list[dict] = []
    for repo in data.get("repos", []):
        for hook in repo.get("hooks", []) or []:
            stages = hook.get("stages") or []
            if "manual" in stages:
                stage = "manual"
            elif "pre-push" in stages:
                stage = "pre-push"
            else:
                stage = "auto"
            hooks.append(
                {
                    "id": hook.get("id", "?"),
                    "name": str(hook.get("name", hook.get("id", "?"))),
                    "stage": stage,
                }
            )
    return hooks


def load_skills() -> list[dict]:
    """Read .claude/skills/*/SKILL.md → [{name, description}]."""
    skills: list[dict] = []
    if not SKILLS_DIR.is_dir():
        return skills
    for skill_md in sorted(SKILLS_DIR.glob("*/SKILL.md")):
        meta, _ = _frontmatter(_read(skill_md))
        skills.append(
            {
                "name": meta.get("name", skill_md.parent.name),
                "description": str(meta.get("description", "")),
            }
        )
    return skills


def load_feedback(memory_dir: Path) -> list[dict]:
    """Read memory/feedback_*.md → [{file, name, body, mtime_days, promoted}]."""
    cards: list[dict] = []
    if not memory_dir.is_dir():
        return cards
    now = datetime.now(timezone.utc)
    for card in sorted(memory_dir.glob("feedback_*.md")):
        meta, body = _frontmatter(_read(card))
        mtime = datetime.fromtimestamp(card.stat().st_mtime, timezone.utc)
        cards.append(
            {
                "file": card.name,
                "name": str(meta.get("name", card.stem)),
                "body": body,
                "mtime_days": (now - mtime).days,
                "promoted": bool(meta.get("promoted_to_claude_md", False)),
            }
        )
    return cards


# --------------------------------------------------------------------------- checks


def check_count_reconciliation(hooks: list[dict]) -> list[str]:
    """Compare measured hook stage split vs CLAUDE.md's stated 'N auto + M manual + K pre-push'."""
    measured = {"auto": 0, "manual": 0, "pre-push": 0}
    for h in hooks:
        measured[h["stage"]] += 1
    findings: list[str] = []
    claude = _read(CLAUDE_MD)
    m = re.search(
        r"(\d+)\s*auto-run\s*\+\s*(\d+)\s*manual-stage\s*\+\s*(\d+)\s*pre-push",
        claude,
    )
    measured_str = (
        f"{measured['auto']} auto + {measured['manual']} manual + "
        f"{measured['pre-push']} pre-push"
    )
    if not m:
        findings.append(
            f"⚠️ 找不到 CLAUDE.md 的 hook 計數宣告字串；實測 {measured_str}。"
        )
        return findings
    claimed = {"auto": int(m.group(1)), "manual": int(m.group(2)), "pre-push": int(m.group(3))}
    claimed_str = (
        f"{claimed['auto']} auto + {claimed['manual']} manual + "
        f"{claimed['pre-push']} pre-push"
    )
    if claimed == measured:
        findings.append(f"✅ hook 計數一致：CLAUDE.md 宣告 = 實測 = {measured_str}。")
    else:
        findings.append(
            f"🕳️ **hook 計數漂移**：CLAUDE.md 宣告 `{claimed_str}`，實測 `{measured_str}`。"
            f"（總數 {sum(claimed.values())} vs {sum(measured.values())}）— 無 lint 攔此 inline split，建議校正 CLAUDE.md。"
        )
    return findings


def check_duplication(feedback: list[dict], dev_rules: list[dict]) -> list[str]:
    """Pairwise similarity within feedback cards + feedback↔dev-rule, ≥ SIM_THRESHOLD."""
    findings: list[str] = []
    for i in range(len(feedback)):
        for j in range(i + 1, len(feedback)):
            r = _ratio(feedback[i]["body"], feedback[j]["body"])
            if r >= SIM_THRESHOLD:
                findings.append(
                    f"🔁 feedback `{feedback[i]['file']}` ↔ `{feedback[j]['file']}` "
                    f"相似度 {r:.2f} — 重複候選，考慮合併。"
                )
    for card in feedback:
        for rule in dev_rules:
            r = _ratio(card["body"][:1500], rule["body"][:1500])
            if r >= SIM_THRESHOLD:
                findings.append(
                    f"🔁 feedback `{card['file']}` ↔ dev-rule 「{rule['heading']}」 "
                    f"相似度 {r:.2f} — feedback 內容或已 codify 進 dev-rules，考慮收斂。"
                )
    if not findings:
        findings.append(f"✅ 無相似度 ≥ {SIM_THRESHOLD:.2f} 的重複候選。")
    return findings


def check_feedback_xref(feedback: list[dict], memory_dir: Path) -> list[str]:
    """Flag feedback cards missing from MEMORY.md index + broken feedback_X references."""
    findings: list[str] = []
    index_path = memory_dir / MEMORY_INDEX_NAME
    if not index_path.is_file():
        findings.append(f"⚠️ 找不到 {MEMORY_INDEX_NAME} index，跳過 cross-ref 檢查。")
        return findings
    index_text = _read(index_path)
    known = {c["file"] for c in feedback}
    for card in feedback:
        stem = card["file"][:-3] if card["file"].endswith(".md") else card["file"]
        if stem not in index_text and card["file"] not in index_text:
            findings.append(f"🕳️ feedback `{card['file']}` 未在 {MEMORY_INDEX_NAME} index 中 — orphan。")
    referenced = set(re.findall(r"feedback_[a-z0-9_]+", index_text))
    for ref in sorted(referenced):
        if f"{ref}.md" not in known:
            findings.append(f"🕳️ {MEMORY_INDEX_NAME} 引用 `{ref}` 但找不到對應卡 — broken ref。")
    if len(findings) == 0:
        findings.append("✅ 所有 feedback 卡均在 index 中、無 broken ref。")
    return findings


def check_hook_devrule_gap(dev_rules: list[dict], hooks: list[dict]) -> list[str]:
    """Flag numbered dev-rules (### N.) whose body mentions no hook id / 'hook' keyword."""
    findings: list[str] = []
    hook_ids = {h["id"] for h in hooks}
    for rule in dev_rules:
        if not re.match(r"^\d+[a-z]?\.", rule["heading"]):
            continue  # only the numbered 12 rules, not §P/§S/§T/§A
        body = rule["body"]
        low = body.lower()
        # 顯式 reviewer-only 訊號優先（即使 body 提到 hook，也是在說「沒有 hook」）。
        reviewer_only = (
            "reviewer convention" in low
            or "reviewer-only" in low
            or "未由 pre-commit" in body
            or "未由 hook" in body
        )
        # 機械防線訊號：hook keyword / hook id / lint / code-driven / 引用 .py 腳本。
        mechanical = (
            "hook" in low
            or "lint" in low
            or "code-driven" in low
            or ".py" in body
            or any(hid in body for hid in hook_ids)
        )
        if reviewer_only:
            findings.append(
                f"👁️ dev-rule 「{rule['heading']}」 顯式標為 reviewer convention "
                f"（無機械防線）— 確認 hook-vs-skill-coverage.md §7 漏接已收錄。"
            )
        elif not mechanical:
            findings.append(
                f"🕳️ dev-rule 「{rule['heading']}」 body 未提及任何 hook / lint — "
                f"可能 reviewer-only，對照 hook-vs-skill-coverage.md 確認。"
            )
    if not findings:
        findings.append("✅ 所有編號 dev-rule 至少提及一個 hook / 機械防線。")
    return findings


def check_staleness(feedback: list[dict]) -> list[str]:
    """Flag feedback cards untouched > STALE_DAYS (mtime proxy for 'last hit')."""
    findings: list[str] = []
    for card in sorted(feedback, key=lambda c: -c["mtime_days"]):
        if card["mtime_days"] > STALE_DAYS:
            promoted = " [已升 CLAUDE.md root]" if card["promoted"] else ""
            findings.append(
                f"⏳ `{card['file']}` {card['mtime_days']} 天未更新{promoted} — "
                f"確認是否仍適用 / 可下放。"
            )
    if not findings:
        findings.append(f"✅ 無超過 {STALE_DAYS} 天未更新的 feedback 卡。")
    return findings


# --------------------------------------------------------------------------- render


def render_report(
    dev_rules: list[dict],
    hooks: list[dict],
    skills: list[dict],
    feedback: list[dict],
    memory_dir: Path,
    memory_available: bool,
) -> str:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    counts = {"auto": 0, "manual": 0, "pre-push": 0}
    for h in hooks:
        counts[h["stage"]] += 1
    lines: list[str] = []
    lines.append(f"# Rule-corpus drift audit — {today}")
    lines.append("")
    lines.append(
        "> 由 `scripts/ops/audit_rules_drift.py`（TRK-307）產生。MANUAL 季度稽核；"
        "只標 drift 候選，**不自動修改**。處理 SOP 見 "
        "[`quarterly-audit-sop.md`](../quarterly-audit-sop.md)。"
    )
    lines.append("")
    lines.append("## 語料盤點")
    lines.append("")
    lines.append("| 來源 | 數量 |")
    lines.append("|---|---|")
    lines.append(f"| dev-rules `### ` 條目 | {len(dev_rules)} |")
    lines.append(
        f"| pre-commit hooks | {len(hooks)}"
        f"（{counts['auto']} auto / {counts['manual']} manual / {counts['pre-push']} pre-push）|"
    )
    lines.append(f"| 本地 skills | {len(skills)} |")
    if memory_available:
        lines.append(f"| memory feedback 卡 | {len(feedback)} |")
    else:
        lines.append("| memory feedback 卡 | （memory 目錄不存在 — 已跳過）|")
    lines.append("")

    sections = [
        ("1. Count reconciliation（hook 切分 vs CLAUDE.md 宣告）", check_count_reconciliation(hooks)),
        ("2. Hook ↔ dev-rule 覆蓋缺口", check_hook_devrule_gap(dev_rules, hooks)),
    ]
    if memory_available:
        sections += [
            ("3. 重複候選（相似度 ≥ %.2f）" % SIM_THRESHOLD, check_duplication(feedback, dev_rules)),
            ("4. Feedback cross-ref / orphan", check_feedback_xref(feedback, memory_dir)),
            ("5. Stale feedback（> %d 天未更新）" % STALE_DAYS, check_staleness(feedback)),
        ]
    else:
        sections += [
            (
                "3-5. Feedback 相關檢查",
                ["⚠️ memory 目錄不存在（CI 環境正常）— 重複 / cross-ref / staleness 檢查需在 maintainer 機器上跑。"],
            ),
        ]

    for title, findings in sections:
        lines.append(f"## {title}")
        lines.append("")
        for f in findings:
            lines.append(f"- {f}")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(
        "_重新產生：`make audit-rules`。本 report 為時間點快照，不代表 live state。_"
    )
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- main


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="季度 rule-corpus drift 稽核 (TRK-307)")
    ap.add_argument(
        "--memory-dir",
        type=Path,
        default=None,
        help="memory feedback 卡目錄（預設 ~/.claude/.../memory）",
    )
    ap.add_argument(
        "--out",
        type=Path,
        default=None,
        help="report 輸出路徑（預設 docs/internal/audit-reports/rules-drift-YYYY-MM.md）",
    )
    ap.add_argument("--stdout", action="store_true", help="只印到 stdout，不寫檔")
    args = ap.parse_args(argv)

    # 顯式 --memory-dir typo 要報錯，而非靜默當「memory 不存在」(lens 8)。
    # 預設路徑不存在 = CI / 非 maintainer 機器的正常情形 → graceful skip。
    explicit_memory = args.memory_dir is not None
    memory_dir = args.memory_dir if explicit_memory else DEFAULT_MEMORY
    memory_available = memory_dir.is_dir()
    if explicit_memory and not memory_available:
        sys.stderr.write(
            f"error: --memory-dir 指定的路徑不存在：{memory_dir}（typo？）。\n"
        )
        return 2

    dev_rules = load_dev_rules()
    hooks = load_hooks()
    skills = load_skills()
    feedback = load_feedback(memory_dir) if memory_available else []

    report = render_report(
        dev_rules, hooks, skills, feedback, memory_dir, memory_available
    )

    if args.stdout:
        sys.stdout.write(report)
        return 0

    out = args.out
    if out is None:
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        out = DEFAULT_REPORT_DIR / f"rules-drift-{month}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(out, report)
    sys.stdout.write(f"wrote drift report: {out.relative_to(REPO_ROOT)}\n")
    if not memory_available:
        sys.stderr.write(
            "note: memory 目錄不存在，feedback 檢查已跳過（CI 環境正常；"
            "完整稽核請在 maintainer 機器上跑）。\n"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
