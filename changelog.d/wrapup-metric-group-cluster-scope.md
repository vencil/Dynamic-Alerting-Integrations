---
section: Changed
topic: tools
issues: [1200]
created: 2026-11-25T09:00:00+00:00
---
- **同一個 `metric_group` 的 warning／critical 必須有相同的叢集範圍；`check_cluster_identity` 的 baseline 與 `--generate` 移除（tools；[#1200](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1200)）**：Alertmanager 的嚴重度去重只把「兩則都帶 `da_cluster`」或「兩則都不帶」的告警配成一對（[ADR-001](docs/adr/001-severity-dedup-via-inhibit.md)、[ADR-006](docs/adr/006-tenant-mapping-topologies.md)）。`check_metric_group_pairs.py` 新增兩個判定：`SCOPE-MISMATCH`（同一個規則包、同一個 `metric_group` 的 warning 與 critical，結果一條逐叢集、一條不是，或一條告警的 `or` 兩側不同）與 `SCOPE-NOT-JUDGED`（範圍判不出來，同樣算失敗）；範圍取自 `check_cluster_identity` 對規則結果的判定，不讀 `# da_cluster: tenant-wide` 宣告。兩條都不帶 `da_cluster` 的一組允許。⚠️ **breaking**：`check_cluster_identity.py --generate` 與 `tests/rulepacks/cluster_identity_baseline.yaml` 已移除（清單早已是空的），帶 `--generate` 會以 argparse 的結束碼 `2` 拒絕；檢查改為零 finding——規則保留 `da_cluster`，或在規則正上方宣告刻意跨叢集。腳本裡呼叫過 `--generate` 的要拿掉。
