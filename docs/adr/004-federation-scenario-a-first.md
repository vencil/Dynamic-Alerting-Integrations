---
tags: [adr, architecture]
audience: [platform-engineers]
version: v2.4.0
lang: zh
---

# ADR-004: Federation 場景 A 優先實現

## 狀態

✅ **Accepted** (v1.12.0)

## 背景

Multi-Tenant Dynamic Alerting 平台面臨多叢集監控需求。企業通常在多個 Kubernetes 叢集上運行業務，需要統一的告警管理。

### Federation 的兩個主要場景

**場景 A：中央 Exporter + 邊緣 Prometheus**
- 單一 threshold-exporter 部署在中央，服務所有邊緣叢集
- 邊緣 Prometheus 透過 `prometheus.yml` 的 `remote_read` 從中央 exporter 讀取指標
- 邊緣 Prometheus 只需本地 rules；告警由本地或中央 Alertmanager 處理

**場景 B：邊緣 Exporter + 中央聚合**
- 每個邊緣叢集部署獨立的 threshold-exporter
- 中央 Prometheus 透過聯邦抓取或 remote_write 聚合邊緣資料
- 複雜度：N 個 exporter 實例、N 個配置、中央協調邏輯

### 決策標準

| 標準 | 場景 A | 場景 B |
|:-----|:-----:|:-----:|
| Exporter 部署數 | 1 | N |
| 配置管理複雜度 | 低 | 高 |
| 覆蓋用例百分比 | ~80% | ~20% |
| 實施時間 | 短 | 長 |

## 決策

**優先實現 Federation 場景 A（中央 Exporter + 邊緣 Prometheus）。場景 B 延後至 P2 (Roadmap)。**

## 基本原理

### 80-20 法則

根據客戶調研，大多數企業採用中央管理的監控架構：

- 80%：統一的告警策略、單一 exporter 實例足以應對多叢集
- 20%：邊緣自主運營、需要邊緣 exporter 實例 (雲邊協同、自治程度高)

場景 A 可覆蓋多數用例，優先完成以快速 go-to-market。

### 架構簡潔性

**場景 A**：
- 配置集中：租戶配置在中央管理，所有 Prometheus 同步拉取
- Exporter HA：單個 exporter 部署 HA (多副本)，成本低
- 無協調邏輯：邊緣 Prometheus 之間無依賴

**場景 B**：
- 配置分散：每個邊緣需獨立配置，中央需追蹤 N 個實例
- Exporter 管理：N 個 exporter 版本、補丁、升級需協調
- 同步複雜：中央需聚合邊緣資料，可能出現資料重複或遺漏

### 時間與資源考量

場景 A 的核心開發工作已完成 (v1.12.0)：
- `remote_read` 整合測試完成
- 文件已記錄 ([`docs/federation-integration.md`](../federation-integration.md))
- 典型部署時間：2-3 小時

場景 B 需額外的開發工作：
- 邊緣 exporter 實例管理框架
- 中央聚合邏輯 (dedup、ordering)
- 多層次配置驗證
- 預估開發時間：6-8 週

## 後果

### 正面影響

✅ 快速推出 Federation 支援，滿足 80% 用例
✅ 簡化初期運維負擔
✅ 為後續場景 B 打下 API/工具基礎
✅ 客戶可漸進式採用，先用 A 後升級至 B

### 負面影響

⚠️ 邊緣自主運營的用例需延期
⚠️ 場景 B 若後續需求旺盛，會面臨重設計風險
⚠️ 某些「邊緣 exporter 自主配置」的場景無法在 v1.x 內支援

### 遷移路徑

場景 A 的使用者未來升級至場景 B 時可平滑遷移：
- API 相容性保證：無需修改現有的 A 型部署
- 工具支援：`scaffold_tenant.py` 擴展以支援邊緣 exporter 配置
- 文件指引：明確切換至場景 B 的步驟

## 替代方案考量

### 方案 A：同時實現 A 與 B (已拒絕)
- 優點：面面俱到
- 缺點：時間表延期、初期複雜度過高、難以測試

### 方案 B：只實現場景 B (已拒絕)
- 優點：更強大
- 缺點：違背最小可行產品 (MVP) 原則、挫傷客戶時間表

## 相關決策

- [ADR-006: 1:N 租戶映射拓撲](./006-tenant-mapping-topologies.md) — 基於場景 A 的 data-plane Recording Rules 實現 1:N 映射
- [ADR-005: 投影卷掛載 Rule Pack](./005-projected-volume-for-rule-packs.md) — Federation 場景中 Rule Pack 的掛載機制

## 現況與後續方向

- **v1.12.0**（已完成）：場景 A 核心實現、`remote_read` 整合測試、文件記錄
- **v2.1.0**（已完成）：`federation_check.py` 遷移至共用 `query_prometheus_instant`，支援邊緣/中央雙模驗證
- **後續**：場景 B（Rule Pack 分層）保留為未來方向，見 [`architecture-and-design.md` §5.1](../architecture-and-design.md)

## 參考資料

- [`docs/federation-integration.md`](../federation-integration.md) — 場景 A 詳細集成指南
- [`docs/scenarios/multi-cluster-federation.md`](../scenarios/multi-cluster-federation.md) — 多叢集場景案例
- `CHANGELOG.md` — v1.12.0 Federation 初始實現記錄

## 相關資源

| 資源 | 相關性 |
|------|--------|
| [001-severity-dedup-via-inhibit](001-severity-dedup-via-inhibit.md) | ⭐⭐⭐ |
| [002-oci-registry-over-chartmuseum](002-oci-registry-over-chartmuseum.md) | ⭐⭐⭐ |
| [003-sentinel-alert-pattern](003-sentinel-alert-pattern.md) | ⭐⭐⭐ |
| [004-federation-scenario-a-first](004-federation-scenario-a-first.md) | ⭐⭐⭐ |
| [005-projected-volume-for-rule-packs](005-projected-volume-for-rule-packs.md) | ⭐⭐⭐ |
| [README](README.md) | ⭐⭐⭐ |
| ["架構與設計 — 動態多租戶警報平台技術白皮書"](../architecture-and-design.md) | ⭐⭐ |
| ["架構與設計 — 附錄 A"](../architecture-and-design.md#附錄-a角色與工具速查) | ⭐⭐ |
