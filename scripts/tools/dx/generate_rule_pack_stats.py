#!/usr/bin/env python3
"""generate_rule_pack_stats.py — Rule Pack 統計單一來源產生器

從 rule-packs/*.yaml 和 k8s/03-monitoring/configmap-rules-*.yaml
讀取實際規則數量，產生 Markdown include 片段供文件引用。

用法:
  python3 scripts/tools/generate_rule_pack_stats.py              # 印出統計
  python3 scripts/tools/generate_rule_pack_stats.py --check      # CI 模式 (exit 1 on drift)
  python3 scripts/tools/generate_rule_pack_stats.py --json       # JSON 輸出
  python3 scripts/tools/generate_rule_pack_stats.py --generate   # 產生 docs/includes/rule-pack-stats.md
"""
import argparse
import json
import os
import re
import stat
import sys
from pathlib import Path

import yaml

# Pull `try_utf8_stdout` from the shared compat lib at scripts/tools/.
# Migrated in #489 Phase B (was missing encoding setup → would crash on
# legacy Windows cp950/cp936 consoles when printing emoji to stdout).
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, str(_THIS_DIR))
sys.path.insert(0, os.path.join(str(_THIS_DIR), ".."))
from _atomic_write import atomic_write_text  # noqa: E402
from _lib_compat import try_utf8_stdout  # noqa: E402
from _lib_exitcodes import EXIT_VIOLATION  # noqa: E402

# ---------------------------------------------------------------------------
# Repo root detection
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent.parent

RULE_PACKS_DIR = REPO_ROOT / "rule-packs"
K8S_RULES_DIR = REPO_ROOT / "k8s" / "03-monitoring"
INCLUDE_DIR = REPO_ROOT / "docs" / "includes"

# Exporter name mapping (pack name → display name + exporter)
PACK_META = {
    "mariadb": ("MariaDB", "mysqld_exporter (Percona)"),
    "postgresql": ("PostgreSQL", "postgres_exporter"),
    "kubernetes": ("Kubernetes", "cAdvisor + kube-state-metrics"),
    "redis": ("Redis", "redis_exporter"),
    "mongodb": ("MongoDB", "mongodb_exporter"),
    "elasticsearch": ("Elasticsearch", "elasticsearch_exporter"),
    "oracle": ("Oracle", "oracledb_exporter"),
    "db2": ("DB2", "db2_exporter"),
    "clickhouse": ("ClickHouse", "clickhouse_exporter"),
    "kafka": ("Kafka", "kafka_exporter"),
    "rabbitmq": ("RabbitMQ", "rabbitmq_exporter"),
    "jvm": ("JVM", "jmx_exporter"),
    "nginx": ("Nginx", "nginx-prometheus-exporter"),
    # liveness 先前不在此表，exporter 欄位因此 fallback 成 pack 名 "liveness"。
    # #1267 把這張表搬進 architecture-and-design 後它會被讀者看到，故補上。
    # exporter 欄由 LANG_STRINGS["<lang>"]["liveness_exporter"] 覆寫（此處僅為 fallback）——
    # PACK_META 是跨語系共用的，直接放中文會讓英文表出現中文（外審抓到）。
    "liveness": ("Exporter Liveness", "threshold-exporter heartbeat"),
    "operational": ("Operational", "threshold-exporter 運營模式"),
    "platform": ("Platform", "threshold-exporter 自監控"),
}

# Bilingual table headers / totals
LANG_STRINGS = {
    "zh": {
        "header": "| 規則包 | Exporter | Recording | Alert |",
        "sep": "|--------|----------|-----------|-------|",
        "total": "合計",
        "operational_exporter": "threshold-exporter 運營模式",
        "platform_exporter": "threshold-exporter 自監控",
        "liveness_exporter": "threshold-exporter 心跳",
    },
    "en": {
        "header": "| Rule Pack | Exporter | Recording | Alert |",
        "sep": "|-----------|----------|-----------|-------|",
        "total": "Total",
        "operational_exporter": "threshold-exporter operational mode",
        "platform_exporter": "threshold-exporter self-monitoring",
        "liveness_exporter": "threshold-exporter heartbeat",
    },
}


def _pack_order() -> list:
    """Canonical 16-pack ordering, imported rather than re-declared (#1277).

    `generate_platform_data.PACK_ORDER` is the single place this order lives;
    re-typing it here would create the 7th hand-maintained copy of rule-pack
    metadata — the exact class of defect #1267/#1277 exist to remove.
    """
    from generate_platform_data import PACK_ORDER  # noqa: E402  (lazy: avoids import cycle)
    return list(PACK_ORDER)


def _pack_label(key: str) -> str:
    """Display name, from the same SoT `platform-data.json` serialises as `label`."""
    from generate_platform_data import PACK_LABELS  # noqa: E402
    return PACK_LABELS.get(key, key)


def _get_stats_file(lang: str) -> Path:
    """Return output path for a given language."""
    if lang == "en":
        return INCLUDE_DIR / "rule-pack-stats.en.md"
    return INCLUDE_DIR / "rule-pack-stats.md"


ARCH_SENTINEL_START = "<!-- RULE_PACK_STATS_START -->"
ARCH_SENTINEL_END = "<!-- RULE_PACK_STATS_END -->"

# #1277: the §3.1 inventory table in docs/design/rule-packs.{md,en.md}.
DESIGN_SENTINEL_START = "<!-- RULE_PACK_INVENTORY_START -->"
DESIGN_SENTINEL_END = "<!-- RULE_PACK_INVENTORY_END -->"

DESIGN_LANG = {
    "zh": {
        "header": "| Rule Pack | ConfigMap 名稱 | Recording Rules | Alert Rules |",
        "sep": "|-----------|-----------------|-----------------|-------------|",
        "total": "總計",
        "unit": "rules",
    },
    "en": {
        "header": "| Rule Pack | ConfigMap Name | Recording Rules | Alert Rules |",
        "sep": "|-----------|----------------|-----------------|-------------|",
        "total": "Total",
        "unit": "rules",
    },
}


def _design_doc(lang: str) -> Path:
    """docs/design/rule-packs.{md,en.md} — the §3.1 inventory table (#1277)."""
    name = "rule-packs.en.md" if lang == "en" else "rule-packs.md"
    return REPO_ROOT / "docs" / "design" / name


def render_design_table(stats: dict, lang: str = "zh") -> str:
    """§3.1 的庫存表：顯示名 / ConfigMap 名 / 兩個規則數，全部可推導。

    ⚠️ 這張表**不含**「擁有團隊」欄（#1277）。那一欄自 2026-04-06 引入後值從未
    變動過（期間 kubernetes 規則數 7→30 也沒人回頭看過歸誰），而它給的
    **逐 pack 對應**在 repo 內沒有任何機器可讀來源；修改前它還與當時
    `.github/CODEOWNERS` 的歸屬相互矛盾（redis 寫 Cache／CODEOWNERS 是
    `@dba-team`，mongodb 同）。注意那套**詞彙本身**並非零命中——
    `rule-packs/README.md` 與 `configmap-prometheus.yaml` 的散文都提過
    「DBA / K8s Infra / Search」，前者甚至是硬寫在 generate_rule_pack_readme.py
    裡每次重生成的；`Messaging` / `Analytics` 更是 pack **category** 的正式
    顯示名。零命中的只有 AppDev / AppData。
    治理面的擁有權正本是 CODEOWNERS；本表只放「查得證的事實」，示意性的團隊
    切分改放在表格下方明確標示的區塊，讓讀者分得出哪些是庫存、哪些是舉例。

    來源刻意是 `PACK_LABELS` / `PACK_ORDER`（import 自 generate_platform_data）
    ＋ `gather_stats()`，而不是已生成的 platform-data.json——避免「生成物依賴
    另一個生成物」的重生順序問題。實測三者對 16 個 pack 逐筆一致。
    """
    s = DESIGN_LANG[lang]
    per_pack = stats["per_pack"]
    order = _pack_order()

    # ⚠️ FAIL LOUD on a set mismatch. The rows come from PACK_ORDER while the
    # totals come from gather_stats(); if those two disagree the table
    # CONTRADICTS ITSELF — a pack silently gets no row while its counts still
    # land in the total — and `--check` stays green because both sides are
    # internally consistent with their own source. Measured: adding a
    # rule-pack YAML without touching PACK_ORDER produced 16 rows summing to
    # 166/160 under a total row reading 167/161, with rc=0. Nothing else
    # enforces PACK_ORDER completeness (generate_platform_data.py iterates the
    # same list, so it is green too). Same class the sibling
    # generate_byo_rulepack_table.py fails loud on; found by adversarial review.
    missing = [k for k in order if k not in per_pack]
    extra = [k for k in per_pack if k not in order]
    if missing or extra:
        raise ValueError(
            "PACK_ORDER 與實際掃到的 Rule Pack 不一致，表格會靜默漏列且總計對不上各列。\n"
            f"  只在 PACK_ORDER（無來源檔）：{sorted(missing)}\n"
            f"  只在來源檔（未列入 PACK_ORDER）：{sorted(extra)}\n"
            "新增/移除 rule pack 時要同步 generate_platform_data.PACK_ORDER。"
        )

    lines = [s["header"], s["sep"]]
    for key in order:
        counts = per_pack[key]
        lines.append(
            f"| {_pack_label(key)} | `prometheus-rules-{key}` "
            f"| {counts['recording']} | {counts['alert']} |"
        )
    lines.append(
        f"| **{s['total']}** | | **{stats['recording']}** "
        f"| **{stats['alert']}** (= **{stats['total']}** {s['unit']}) |"
    )
    return "\n".join(lines) + "\n"


def _arch_doc(lang: str) -> Path:
    """The doc the table is injected into (#1267)."""
    name = "architecture-and-design.en.md" if lang == "en" else "architecture-and-design.md"
    return REPO_ROOT / "docs" / name


def _table_body(table: str) -> str:
    """Strip the fragment's `<!-- ... -->` header lines — inline injection needs
    only the table itself (the DO-NOT-EDIT notice lives beside the sentinels)."""
    lines = [ln for ln in table.split("\n") if not ln.startswith("<!--")]
    return "\n".join(lines).strip("\n") + "\n"


def _replace_block(content: str, body: str, rel: str, start: str, end: str) -> str:
    """Swap a sentinel block's contents. The end sentinel deliberately does NOT
    absorb a newline: `body` already ends with one, so letting both claim it adds
    a blank line per run and `--check` would never converge (burned once)."""
    pattern = re.compile(
        r"(" + re.escape(start) + r"\n)" r".*?" r"(" + re.escape(end) + r")",
        re.DOTALL,
    )
    if not pattern.search(content):
        raise ValueError(
            f"{rel} 內找不到 sentinel 區塊。請加上這兩行：\n  {start}\n  {end}"
        )
    return pattern.sub(lambda m: m.group(1) + body + m.group(2), content)


def _replace_arch_block(content: str, body: str, rel: str) -> str:
    """architecture-and-design.{md,en.md} 的統計表區塊。"""
    return _replace_block(content, body, rel, ARCH_SENTINEL_START, ARCH_SENTINEL_END)


def count_rules_in_yaml(filepath: Path) -> tuple:
    """Count recording and alert rules in a YAML file.

    Returns (recording_count, alert_count).
    """
    data = yaml.safe_load(filepath.read_text(encoding="utf-8"))
    rec = alert = 0
    if data and "groups" in data:
        for g in data["groups"]:
            for r in g.get("rules", []):
                if "alert" in r:
                    alert += 1
                elif "record" in r:
                    rec += 1
    return rec, alert


def count_rules_in_configmap(filepath: Path) -> tuple:
    """Count rules inside a Kubernetes ConfigMap YAML.

    Returns (recording_count, alert_count).
    """
    data = yaml.safe_load(filepath.read_text(encoding="utf-8"))
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
    return rec, alert


def gather_stats() -> dict:
    """Gather Rule Pack statistics from source YAML files.

    Returns dict with pack_count, recording, alert, total, per_pack.
    """
    packs = {}

    # #741 S3b: custom-alerts is a TENANT-authored deployed pack, not platform
    # coverage — exclude it from the platform "Rule Packs / Alerts" stats badge
    # (mirrors its omission from generate_platform_data.py's PACK_ORDER).
    _EXCLUDE = {"custom-alerts"}

    # rule-packs/ directory
    for f in sorted(RULE_PACKS_DIR.glob("rule-pack-*.yaml")):
        name = f.stem.replace("rule-pack-", "")
        if name in _EXCLUDE:
            continue
        rec, alert = count_rules_in_yaml(f)
        packs[name] = {"recording": rec, "alert": alert}

    # k8s ConfigMaps (may have additional alert rules)
    for f in sorted(K8S_RULES_DIR.glob("configmap-rules-*.yaml")):
        name = f.stem.replace("configmap-rules-", "")
        if name in _EXCLUDE:
            continue
        rec, alert = count_rules_in_configmap(f)
        if name not in packs:
            packs[name] = {"recording": rec, "alert": alert}
        else:
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


def generate_markdown_table(stats: dict, lang: str = "zh") -> str:
    """Generate a Markdown table from Rule Pack stats."""
    s = LANG_STRINGS[lang]
    # Bilingual nav marker satisfies check_bilingual_structure.py nav-link check.
    pair_target = "rule-pack-stats.md" if lang == "en" else "rule-pack-stats.en.md"
    lines = [
        f"<!-- Bilingual pair: {pair_target} -->",
        "<!-- Auto-generated by generate_rule_pack_stats.py — DO NOT EDIT -->",
        "",
        s["header"],
        s["sep"],
    ]

    for name, counts in stats["per_pack"].items():
        display, exporter = PACK_META.get(name, (name, name))
        # Language-specific exporter names
        if name == "operational":
            exporter = s["operational_exporter"]
        elif name == "platform":
            exporter = s["platform_exporter"]
        elif name == "liveness":
            exporter = s["liveness_exporter"]
        lines.append(
            f"| {display.lower() if display == name else name} "
            f"| {exporter} "
            f"| {counts['recording']} "
            f"| {counts['alert']} |"
        )

    lines.append(
        f"| **{s['total']}** | "
        f"| **{stats['recording']}** "
        f"| **{stats['alert']}** |"
    )
    lines.append("")

    return "\n".join(lines)


def format_summary(stats: dict) -> str:
    """Format a badge-like one-line summary of Rule Pack statistics.

    Output: "15 packs | 139R 99A | 238 rules"
    """
    return (f"{stats['pack_count']} packs | "
            f"{stats['recording']}R {stats['alert']}A | "
            f"{stats['total']} rules")


def main():
    """CLI entry point: Rule Pack 統計單一來源產生器."""
    try_utf8_stdout()
    parser = argparse.ArgumentParser(
        description="Generate Rule Pack statistics from source YAML files",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--check", action="store_true",
                        help="CI mode: exit 1 if include file is outdated")
    parser.add_argument("--json", action="store_true",
                        help="Output stats as JSON")
    parser.add_argument("--generate", action="store_true",
                        help="Generate docs/includes/rule-pack-stats[.en].md")
    parser.add_argument("--lang", choices=["zh", "en", "all"], default="zh",
                        help="Language: zh (default), en, or all")
    parser.add_argument("--format", choices=["table", "summary"],
                        default="table", dest="output_format",
                        help="Output format: table (default) or summary (one-line badge)")

    args = parser.parse_args()
    stats = gather_stats()
    langs = ["zh", "en"] if args.lang == "all" else [args.lang]

    if args.json:
        print(json.dumps(stats, indent=2, ensure_ascii=False))
        return

    if args.output_format == "summary":
        print(format_summary(stats))
        return

    if not args.json and not args.check and not args.generate:
        print(f"Rule Pack Stats (from source YAML):")
        print(f"  Packs:     {stats['pack_count']}")
        print(f"  Recording: {stats['recording']}")
        print(f"  Alert:     {stats['alert']}")
        print(f"  Total:     {stats['total']}")
        print()
        for name, counts in stats["per_pack"].items():
            print(f"  {name:20s} {counts['recording']:3d}R  {counts['alert']:3d}A")
        return

    # ⚠️ 兩階段：先把**所有**語言 × 所有輸出面算完，再開始寫。若邊算邊寫，
    # 第二個語言算到一半失敗（例如某個 pack 的來源檔被移除 → KeyError）就會
    #留下「ZH 已更新、EN 還是舊值」的分岔樹，而且 exit code 是 Python 預設的
    # 1——與「有 drift，跑 --generate 就好」同碼，但 --generate 同樣會炸。
    # 姊妹生成器 generate_byo_rulepack_table.py 在 #1267 已建立「非零退出 ⇒
    # 什麼都沒改」的性質，這支當時漏了。外審實測抓到。
    plan = []          # (kind, path, rel, current, updated)
    try:
        for lang in langs:
            table = generate_markdown_table(stats, lang=lang)
            sf = _get_stats_file(lang)
            plan.append(("fragment", sf, sf.relative_to(REPO_ROOT).as_posix(),
                         sf.read_text(encoding="utf-8") if sf.exists() else None, table))

            arch = _arch_doc(lang)
            arch_rel = arch.relative_to(REPO_ROOT).as_posix()
            a_cur = arch.read_text(encoding="utf-8")
            plan.append(("inject", arch, arch_rel, a_cur,
                         _replace_arch_block(a_cur, _table_body(table), arch_rel)))

            design = _design_doc(lang)
            d_rel = design.relative_to(REPO_ROOT).as_posix()
            d_cur = design.read_text(encoding="utf-8")
            plan.append(("inject", design, d_rel, d_cur,
                         _replace_block(d_cur, render_design_table(stats, lang=lang),
                                        d_rel, DESIGN_SENTINEL_START, DESIGN_SENTINEL_END)))
    except ValueError as exc:
        # 缺 sentinel／PACK_ORDER 與來源不一致 —— caller error，不是 drift。
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except KeyError as exc:
        print(f"ERROR: Rule Pack {exc} 在 PACK_ORDER 內但找不到來源規則檔——"
              f"新增/移除 pack 時要同步 generate_platform_data.PACK_ORDER。", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"ERROR: 讀取目標文件失敗：{exc}", file=sys.stderr)
        return 2

    has_drift = False
    for kind, path, rel, current, updated in plan:
        if args.generate:
            if kind == "fragment":
                INCLUDE_DIR.mkdir(parents=True, exist_ok=True)
                atomic_write_text(path, updated)
                print(f"✅ Generated {rel}")
            elif updated != current:
                atomic_write_text(path, updated)
                print(f"✅ Injected table into {rel}")
        elif args.check:
            if current is None:
                print(f"❌ {rel} does not exist. Run with --generate first.")
                has_drift = True
            elif updated.strip() != current.strip():
                print(f"❌ {rel} 已過期。Run with --generate --lang all to update.")
                has_drift = True
            else:
                print(f"✅ {rel} is up to date.")

    if args.check and has_drift:
        sys.exit(EXIT_VIOLATION)


if __name__ == "__main__":
    # sys.exit(main()) — main() 現在用 return 回 caller-error(2)，
    # 裸呼叫會把退出碼丟掉（訊息有印、rc 卻是 0）。
    sys.exit(main())
