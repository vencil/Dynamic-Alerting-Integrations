#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Generate rule-packs/README.md from actual YAML rule pack files.

This tool auto-generates the Rule Pack table by scanning YAML files,
extracting rule counts, and generating markdown documentation.

Usage:
    python3 generate_rule_pack_readme.py                  # Dry-run (stdout)
    python3 generate_rule_pack_readme.py --update         # Write to rule-packs/README.md
    python3 generate_rule_pack_readme.py --check          # CI mode (exit 1 if drift)
"""

import argparse
import sys
from pathlib import Path
from typing import Dict, List, Tuple
import yaml


def extract_rule_counts(yaml_file: Path) -> Tuple[int, int]:
    """
    Extract recording rules and alert rules from a YAML rule pack file.

    Returns:
        (recording_rules_count, alert_rules_count)
    """
    try:
        with open(yaml_file, encoding="utf-8") as f:
            content = yaml.safe_load(f)
    except (yaml.YAMLError, IOError) as e:
        print(f"Warning: Failed to parse {yaml_file}: {e}", file=sys.stderr)
        return 0, 0

    if not content or "groups" not in content:
        return 0, 0

    record_count = 0
    alert_count = 0

    for group in content.get("groups", []):
        for rule in group.get("rules", []):
            if "record" in rule:
                record_count += 1
            elif "alert" in rule:
                alert_count += 1

    return record_count, alert_count


def generate_table_rows(rule_packs_dir: Path) -> Tuple[List[Dict], int, int]:
    """
    Scan rule-packs directory and generate table rows.

    Returns:
        (list_of_rows, total_records, total_alerts)
    """
    yaml_files = sorted(rule_packs_dir.glob("rule-pack-*.yaml"))

    if not yaml_files:
        raise FileNotFoundError(f"No rule pack YAML files found in {rule_packs_dir}")

    rows = []
    total_records = 0
    total_alerts = 0

    for yaml_file in yaml_files:
        # Extract name from filename: rule-pack-mariadb.yaml -> MariaDB
        pack_name = yaml_file.stem.replace("rule-pack-", "")
        display_name = pack_name.replace("-", " ").title()

        # Special cases for acronyms
        display_name = display_name.replace("Jvm", "JVM").replace("Db2", "DB2")

        records, alerts = extract_rule_counts(yaml_file)
        total = records + alerts

        total_records += records
        total_alerts += alerts

        rows.append({
            "name": display_name,
            "file": yaml_file.name,
            "records": records,
            "alerts": alerts,
            "total": total,
        })

    return rows, total_records, total_alerts


def generate_readme_content(rows: List[Dict], total_records: int, total_alerts: int) -> str:
    """
    Generate the full README.md content with header, table, and footer.
    """
    # Build table header
    lines = []
    lines.append("---")
    lines.append('title: "Rule Packs — 模組化 Prometheus 規則"')
    lines.append("tags: [overview, introduction]")
    lines.append("audience: [all]")
    lines.append("version: v1.12.0")
    lines.append("lang: zh")
    lines.append("---")
    lines.append("# Rule Packs — 模組化 Prometheus 規則")
    lines.append("")
    lines.append("> 每個 Rule Pack 包含完整的三件套：Normalization Recording Rules + Threshold Normalization + Alert Rules。")
    lines.append("> **所有 15 個 Rule Pack 已透過 Projected Volume 架構預載入 Prometheus 中** (分散於 `configmap-rules-*.yaml`)。")
    lines.append("> 未部署 exporter 的 pack 不會產生 metrics，因此 alert 不會誤觸發 (near-zero cost)。")
    lines.append(">")
    lines.append("> **其他文件：** [README](../README.md) (概覽) · [Migration Guide](../docs/migration-guide.md) (遷移指南) · [Architecture & Design](../docs/architecture-and-design.md) (技術深度)")
    lines.append("")
    lines.append("## 支援的整合 (Supported Integrations)")
    lines.append("")

    # Table header
    lines.append("| Rule Pack | File | Recording Rules | Alert Rules | Total |")
    lines.append("|-----------|------|-----------------|-------------|-------|")

    # Table rows
    for row in rows:
        lines.append(
            f"| {row['name']} | {row['file']} | {row['records']} | {row['alerts']} | {row['total']} |"
        )

    # Summary row
    lines.append(f"| **TOTAL** | | **{total_records}** | **{total_alerts}** | **{total_records + total_alerts}** |")
    lines.append("")

    # Architecture section
    lines.append("## 架構說明")
    lines.append("")
    lines.append("每個 Rule Pack 擁有獨立的 ConfigMap (`k8s/03-monitoring/configmap-rules-*.yaml`)，")
    lines.append("透過 Kubernetes **Projected Volume** 統一掛載至 Prometheus 的 `/etc/prometheus/rules/`。")
    lines.append("各團隊 (DBA, K8s Infra, Search) 可獨立維護自己的 ConfigMap，不會產生 PR 衝突。")
    lines.append("此目錄 (`rule-packs/`) 保留各 pack 的獨立 YAML 作為**權威參考 (canonical source)**，")
    lines.append("方便查閱各 pack 的完整結構和 PromQL 表達式。")
    lines.append("")
    lines.append("### 為什麼全部預載？")
    lines.append("")
    lines.append("- **成本**: 沒有對應 metric 的 recording rule 會回傳空結果集，CPU 額外開銷 < 0.1%，evaluation 時間幾乎無增長。")
    lines.append("- **簡化**: 新增 exporter 後只需配置 `_defaults.yaml` + tenant YAML，不需修改 Prometheus 設定。")
    lines.append("- **安全**: 唯一的風險是 `absent()` — 目前只有 mariadb (已部署) 使用 `absent(mysql_up)`，其他 pack 都不含 `absent()`。")
    lines.append("")
    lines.append("### 動態卸載 (optional: true)")
    lines.append("")
    lines.append("所有 Rule Pack 在 Projected Volume 中均設定 `optional: true`，這代表：")
    lines.append("")
    lines.append("- **卸載不崩潰**: 刪除任何 Rule Pack 的 ConfigMap（`kubectl delete cm prometheus-rules-<type> -n monitoring`）後，Prometheus **不會 Crash**，只是對應的規則消失。")
    lines.append("- **適用場景**: 大型客戶可能有自己的規則體系，需要關閉平台的黃金標準 Rule Pack，改用 `custom_` 前綴的遷移規則或完全自訂的規則。")
    lines.append("- **重新載入**: 重新 `kubectl apply` 對應的 ConfigMap YAML 即可恢復。Prometheus 的 `--web.enable-lifecycle` 端點或 SHA-256 hash 偵測會自動觸發重載。")
    lines.append("")
    lines.append("```bash")
    lines.append("# 卸載 MongoDB Rule Pack（不影響其他 pack 和 Prometheus 運行）")
    lines.append("kubectl delete cm prometheus-rules-mongodb -n monitoring")
    lines.append("")
    lines.append("# 驗證 Prometheus 正常")
    lines.append("kubectl logs -n monitoring deploy/prometheus --tail=5")
    lines.append("")
    lines.append("# 恢復")
    lines.append("kubectl apply -f k8s/03-monitoring/configmap-rules-mongodb.yaml")
    lines.append("```")
    lines.append("")
    lines.append("## 自訂 Rule Pack")
    lines.append("")
    lines.append("每個 Rule Pack 遵循統一結構：")
    lines.append("")
    lines.append("```yaml")
    lines.append("groups:")
    lines.append("  # 1. Normalization Recording Rules")
    lines.append("  - name: <db>-normalization")
    lines.append("    rules:")
    lines.append("      - record: tenant:<metric>:<function>   # sum/max/rate5m")
    lines.append("        expr: ...")
    lines.append("")
    lines.append("  # 2. Threshold Normalization")
    lines.append("  - name: <db>-threshold-normalization")
    lines.append("    rules:")
    lines.append("      - record: tenant:alert_threshold:<metric>")
    lines.append("        expr: max by(tenant) (user_threshold{metric=\"<metric>\", severity=\"warning\"})")
    lines.append("")
    lines.append("  # 3. Alert Rules (使用 group_left + unless maintenance + runbook injection)")
    lines.append("  - name: <db>-alerts")
    lines.append("    rules:")
    lines.append("      - alert: <AlertName>")
    lines.append("        expr: |")
    lines.append("          (")
    lines.append("            tenant:<metric>:<function> > on(tenant) group_left tenant:alert_threshold:<metric>")
    lines.append("          )")
    lines.append("          * on(tenant) group_left(runbook_url, owner, tier) tenant_metadata_info")
    lines.append("          unless on(tenant) (user_state_filter{filter=\"maintenance\"} == 1)")
    lines.append("        annotations:")
    lines.append("          runbook_url: \"{{ $labels.runbook_url }}\"")
    lines.append("          owner: \"{{ $labels.owner }}\"")
    lines.append("          tier: \"{{ $labels.tier }}\"")
    lines.append("```")
    lines.append("")
    lines.append("### Dynamic Runbook Injection (v1.11.0)")
    lines.append("")
    lines.append("Alert Rules 透過 `* on(tenant) group_left(runbook_url, owner, tier) tenant_metadata_info` 將租戶 metadata 注入 alert labels，再由 annotations 引用。`tenant_metadata_info` 由 threshold-exporter 根據租戶 `_metadata` 配置自動輸出（值永遠為 1），保證 `group_left` join 不會漏掉任何 tenant。")
    lines.append("")
    lines.append("若租戶未設定 `_metadata`，`tenant_metadata_info` 不存在，`group_left` 回傳空向量。因此已內建的 11 個 Rule Pack 均已加入此 join，但 **自訂 Rule Pack 建議同步採用此 pattern** 以確保 runbook URL 與 owner 資訊可自動傳遞至通知。")
    lines.append("")
    lines.append("## Exporter 文件連結")
    lines.append("")
    lines.append("- **mysqld_exporter**: https://github.com/prometheus/mysqld_exporter")
    lines.append("- **redis_exporter**: https://github.com/oliver006/redis_exporter")
    lines.append("- **mongodb_exporter**: https://github.com/percona/mongodb_exporter")
    lines.append("- **elasticsearch_exporter**: https://github.com/prometheus-community/elasticsearch_exporter")
    lines.append("- **oracledb_exporter**: https://github.com/iamseth/oracledb_exporter")
    lines.append("- **ibm_db2_exporter**: https://github.com/IBM/db2-prometheus-exporter (community)")
    lines.append("- **clickhouse_exporter**: https://github.com/ClickHouse/clickhouse_exporter (或 ClickHouse 內建 /metrics)")
    lines.append("- **kafka_exporter**: https://github.com/danielqsj/kafka-exporter")
    lines.append("- **rabbitmq_exporter**: https://github.com/kbudde/rabbitmq_exporter")
    lines.append("- **kube-state-metrics**: https://github.com/kubernetes/kube-state-metrics")

    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(
        description="Auto-generate rule-packs/README.md from YAML files"
    )
    parser.add_argument(
        "--update",
        action="store_true",
        help="Write to rule-packs/README.md (default: dry-run to stdout)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="CI mode: compare current vs generated, exit 1 if drift",
    )
    parser.add_argument(
        "--rule-packs-dir",
        type=Path,
        default=Path(__file__).parent.parent.parent / "rule-packs",
        help="Path to rule-packs directory",
    )

    args = parser.parse_args()

    try:
        rows, total_records, total_alerts = generate_table_rows(args.rule_packs_dir)
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    generated_content = generate_readme_content(rows, total_records, total_alerts)
    readme_path = args.rule_packs_dir / "README.md"

    if args.check:
        # Read current README and compare
        try:
            with open(readme_path, encoding="utf-8") as f:
                current_content = f.read()
        except IOError as e:
            print(f"Error: Failed to read {readme_path}: {e}", file=sys.stderr)
            sys.exit(1)

        if current_content != generated_content:
            print(
                f"Error: {readme_path} is out of sync with rule pack YAML files.",
                file=sys.stderr,
            )
            print(
                "Run: python3 scripts/tools/generate_rule_pack_readme.py --update",
                file=sys.stderr,
            )
            sys.exit(1)
        else:
            print(f"OK: {readme_path} is in sync.", file=sys.stderr)
            sys.exit(0)

    elif args.update:
        # Write to file
        try:
            with open(readme_path, "w", encoding="utf-8") as f:
                f.write(generated_content)
            print(f"Updated {readme_path}", file=sys.stderr)
        except IOError as e:
            print(f"Error: Failed to write {readme_path}: {e}", file=sys.stderr)
            sys.exit(1)

    else:
        # Default: dry-run (print to stdout)
        print(generated_content)


if __name__ == "__main__":
    main()
