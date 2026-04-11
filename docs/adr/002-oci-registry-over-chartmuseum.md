---
title: "ADR-002: OCI Registry 替代 ChartMuseum"
tags: [adr, architecture]
audience: [platform-engineers]
version: v2.6.0
lang: zh
---

# ADR-002: OCI Registry 替代 ChartMuseum

## 狀態

✅ **Accepted** (v1.12.0)

## 背景

Multi-Tenant Dynamic Alerting 平台需要分發多種製品：

1. **Helm Chart**：threshold-exporter 與 dynamic-alerting 的部署配置
2. **Docker Images**：threshold-exporter、da-tools CLI、Prometheus rules 等容器映像

傳統方案使用 ChartMuseum 作為獨立的 Helm 圖表儲存庫，Docker images 存放在容器映像倉庫 (e.g., ghcr.io)，形成兩套獨立的基礎設施。

## 決策

**統一採用 OCI 容器映像倉庫 (ghcr.io)，同時存放 Helm Charts 與 Docker Images。消除對 ChartMuseum 的依賴。**

## 基本原理

### OCI 規範支援

Helm 3.8+ 原生支援 OCI 層級的製品分發標準。Helm charts 可以直接推送到容器映像倉庫，視為 OCI 製品進行版本管理、存取控制、簽名驗證。

### 統一基礎設施的優勢

- **單一真理來源**：所有製品 (charts + images) 在同一倉庫，統一的 RBAC、簽名、稽核日誌
- **簡化運維**：無需維護獨立的 ChartMuseum 實例、備份策略、高可用配置
- **降低成本**：ghcr.io 免費額度充足，無額外基礎設施費用
- **版本追蹤一致**：所有製品使用相同的語義版本 (semantic versioning)

### 客戶側改動最小

- Helm 3.8+ 廣泛採用，大多數企業已升級
- 切換命令簡單：`helm repo add` 改為 `helm pull oci://ghcr.io/...`
- Chart 內容本身無需改動，只改佈署方式

## 後果

### 正面影響

✅ 單一倉庫管理，降低營運成本與複雜度
✅ 原生 OCI 簽名驗證，安全性提升
✅ 統一的 RBAC 與稽核追蹤
✅ CI/CD 流程簡化 (push once → artifacts distributed)

### 負面影響

⚠️ 需要 Helm 3.8+ (大多數環境已滿足)
⚠️ 企業內如有舊版 Helm，需協調升級計畫
⚠️ 某些 Helm plugin (e.g., helm-diff) 需驗證 OCI 相容性

### 遷移策略

- Chart `v1.12.0` 開始採用 OCI 發佈
- 文件記錄並行維護期：3 個月內仍保留 ChartMuseum 作為過渡
- 舊版本仍在 ChartMuseum 可用，新安裝推薦 OCI 方式

## 替代方案考量

### 方案 A：保留 ChartMuseum + ghcr.io 雙軌 (已拒絕)
- 優點：相容所有舊版 Helm
- 缺點：維護兩套基礎設施，複雜度倍增

### 方案 B：使用 Artifactory / Nexus (已考量但拒絕)
- 優點：企業級功能豐富
- 缺點：需自建/付費、與 ghcr.io 競爭、額外學習曲線

## 相關決策

- [ADR-005: 投影卷掛載 Rule Pack](./005-projected-volume-for-rule-packs.md) — Rule Pack 透過 OCI registry 分發後，以 Projected Volume 掛載至 Prometheus

## 實施檢查清單

- [x] 驗證 Helm 3.8+ OCI 相容性
- [x] 配置 ghcr.io OCI push 流程
- [x] 更新安裝文件與快速入門指南
- [x] 為過渡期維護 ChartMuseum 備份 (可選，3 個月過期)
- [x] 發佈變更日誌 (CHANGELOG.md)

## 參考資料

- [Helm 官方 — OCI Support](https://helm.sh/docs/topics/registries/)
- [`docs/getting-started/for-platform-engineers.md`](../getting-started/for-platform-engineers.md) — 安裝步驟
- `CHANGELOG.md` — 分發方式變更記錄

## 相關資源

| 資源 | 相關性 |
|------|--------|
| [001-severity-dedup-via-inhibit](001-severity-dedup-via-inhibit.md) | ⭐⭐⭐ |
| [002-oci-registry-over-chartmuseum](002-oci-registry-over-chartmuseum.md) | ⭐⭐⭐ |
| [003-sentinel-alert-pattern](003-sentinel-alert-pattern.md) | ⭐⭐⭐ |
| [004-federation-central-exporter-first](004-federation-central-exporter-first.md) | ⭐⭐⭐ |
| [005-projected-volume-for-rule-packs](005-projected-volume-for-rule-packs.md) | ⭐⭐⭐ |
| [README](README.md) | ⭐⭐⭐ |
| ["架構與設計 — 動態多租戶警報平台技術白皮書"](../architecture-and-design.md) | ⭐⭐ |
| ["架構與設計 — 附錄 A"](../architecture-and-design.md#附錄角色與工具速查) | ⭐⭐ |
