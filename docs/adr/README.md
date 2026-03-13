---
tags: [adr, architecture]
audience: [platform-engineers]
version: v1.13.0
lang: zh
---

# 架構決策記錄 (ADR)

本目錄收錄 Multi-Tenant Dynamic Alerting 平台的架構決策記錄 (Architecture Decision Records)。每份 ADR 記錄特定設計決策的背景、選項評估與長期影響。

## ADR 索引

| ID | 標題 | 狀態 | 摘要 |
|:---|:-----|:-----|:-----|
| [001](#001-severity-dedup-via-inhibit) | 嚴重度 Dedup 採用 Inhibit 規則 | ✅ Accepted | 使用 Alertmanager inhibit_rules 而非 PromQL 進行嚴重度去重，保留 TSDB 完整性 |
| [002](#002-oci-registry-over-chartmuseum) | OCI Registry 替代 ChartMuseum | ✅ Accepted | 選擇 ghcr.io OCI 統一分發 Helm charts 與 Docker images，簡化基礎設施 |
| [003](#003-sentinel-alert-pattern) | Sentinel Alert 模式 | ✅ Accepted | 利用哨兵告警 + inhibit 實現三態控制，取代直接 PromQL 抑制 |
| [004](#004-federation-scenario-a-first) | Federation 場景 A 優先 | ✅ Accepted | 優先實現中央 exporter + 邊緣 Prometheus 的聯邦模式 |
| [005](#005-projected-volume-for-rule-packs) | 投影卷掛載 Rule Pack | ✅ Accepted | 採用 Projected Volume 與 optional:true 實現可選 Rule Pack 卸載 |

---

## 001: 嚴重度 Dedup 採用 Inhibit 規則

**文件**: [`001-severity-dedup-via-inhibit.md`](./001-severity-dedup-via-inhibit.md)

使用 Alertmanager inhibit_rules 而非 PromQL 的 `absent()`/`unless()` 進行嚴重度去重。關鍵考量：保留 TSDB 完整性，同一指標的多個嚴重度級別都被記錄，Alertmanager 層級進行智慧抑制。

---

## 002: OCI Registry 替代 ChartMuseum

**文件**: [`002-oci-registry-over-chartmuseum.md`](./002-oci-registry-over-chartmuseum.md)

選擇 ghcr.io OCI registry 統一分發 Helm charts 與 Docker images，消除對獨立 ChartMuseum 的依賴。需要 Helm 3.8+，但簡化運維成本。

---

## 003: Sentinel Alert 模式

**文件**: [`003-sentinel-alert-pattern.md`](./003-sentinel-alert-pattern.md)

透過 exporter flag metric → recording rule → sentinel alert → inhibit 的流程實現三態模式 (Normal/Silent/Maintenance)。相比直接 PromQL 抑制，此模式組合性強且易於調試。

---

## 004: Federation 場景 A 優先

**文件**: [`004-federation-scenario-a-first.md`](./004-federation-scenario-a-first.md)

優先實現 Federation 場景 A：中央 exporter + 邊緣 Prometheus。此方案簡潔 (單一 exporter 部署)，涵蓋 80% 的聯邦用例；場景 B (邊緣 exporter) 延後至 P2。

---

## 005: 投影卷掛載 Rule Pack

**文件**: [`005-projected-volume-for-rule-packs.md`](./005-projected-volume-for-rule-packs.md)

採用 Projected Volume 與 `optional: true` 實現 15 個 Rule Pack 的可選卸載。租戶可刪除個別 ConfigMap 來禁用特定 Rule Pack，Prometheus 不會因缺失 pack 而失敗。

---

## 相關文件

- [`docs/architecture-and-design.md`](../architecture-and-design.md) — 完整架構設計
- [`docs/getting-started/for-platform-engineers.md`](../getting-started/for-platform-engineers.md) — 平台工程師快速入門
- [`CLAUDE.md`](../../CLAUDE.md) — 開發上下文指引

