---
title: "ADR-004: Federation 架構——中央 Exporter 優先"
tags: [adr, architecture]
audience: [platform-engineers]
version: v2.9.0
lang: zh
id: ADR-004
tracking_kind: adr
status: accepted
domain: exporter
created_at: 2026-04-07
updated_at: 2026-10-10
---
# ADR-004: Federation 架構——中央 Exporter 優先

> **Language / 語言：** **中文 (Current)** | [English](./004-federation-central-exporter-first.en.md)

<!-- Language switcher is provided by mkdocs-static-i18n header. -->

**決策摘要**：多叢集部署先支援「中央 Exporter + 邊緣 Prometheus」：一個 threshold-exporter 放在中央叢集，管理所有邊緣租戶的閾值，告警規則在中央評估。「每個邊緣叢集各放一個 exporter」的架構延後。

## 狀態

✅ **Accepted** (v1.12.0) → **Extended** (v2.3.0)

v2.3.0：新增邊緣評估——Rule Pack 的正規化部分可在邊緣 Prometheus 評估，threshold-exporter 與告警評估仍在中央。

## 名詞

- **Federation（聯邦）**：把多個 Prometheus 的資料彙整到一處。本文指平台內部的跨叢集部署：邊緣叢集收集指標，中央叢集統一管理閾值與告警。
- **中央叢集／邊緣叢集**：中央叢集負責統一的監控與告警；邊緣叢集是各自跑業務與資料庫的 Kubernetes 叢集。
- **Rule Pack**：平台隨附的一組 Prometheus 規則檔（recording rule 與告警規則），每個檔案對應一種資料庫或用途，例如 `rule-pack-mariadb.yaml`。
- **邊緣正規化**：Rule Pack 裡先把各資料庫 exporter 的原始指標整理成統一格式的 recording rule（例如 `tenant:mysql_threads_connected:max`）。放在邊緣叢集評估時，就叫邊緣正規化。

## 背景

企業通常把業務分散在多個 Kubernetes 叢集上，需要統一的告警管理。多叢集部署有兩種主要架構：

**中央 Exporter + 邊緣 Prometheus**

- 一個 threshold-exporter 部署在中央叢集，管理所有邊緣租戶的閾值。
- 資料流：邊緣 Prometheus 以聯邦抓取（federation）或 `remote_write` 把原始指標送到中央；中央 Prometheus 抓 exporter 的閾值，在中央評估全部 Rule Pack。

**邊緣 Exporter + 中央聚合**

- 每個邊緣叢集各自部署 threshold-exporter。
- 複雜度：N 個 exporter 實例、N 份設定，以及中央的協調邏輯。

### 決策標準

| 標準 | 中央 Exporter | 邊緣 Exporter |
|:-----|:-----:|:-----:|
| Exporter 部署數 | 1 | N |
| 設定管理複雜度 | 低 | 高 |
| 涵蓋的使用情境（決策時估計） | 約 80% | 約 20% |
| 實作時間 | 短 | 長 |

## 決策

**先實作「中央 Exporter + 邊緣 Prometheus」架構。**

依決策當時的估計，約八成的企業採用中央管理的監控架構（統一的告警策略、一個 exporter 就能應付多個叢集）。先做這個架構，能用較短的時間涵蓋大多數情境。

### 範例

**中央架構的最小設定**（設定片段，取自 [Federation 整合指南 §4.1](../integration/federation-integration.md#41-option-one-prometheus-federation)，未在本文實跑）：中央 Prometheus 抓本地的 threshold-exporter，並從邊緣 Prometheus 拉帶 `tenant` 標籤的指標。

```yaml
# prometheus.yml（中央叢集）
scrape_configs:
  - job_name: "threshold-exporter"
    static_configs:
      - targets: ["threshold-exporter:8080"]

  - job_name: "federation-edge-asia-1"
    honor_labels: true
    metrics_path: "/federate"
    params:
      "match[]":
        - '{tenant!=""}'
    static_configs:
      - targets: ["prometheus-edge-asia-1.example.com:9090"]
```

**邊緣評估（v2.3.0，exporter 仍在中央）**：`da-tools rule-pack-split` 把 Rule Pack 拆成邊緣與中央兩份。輸入目錄 `my-packs/` 只放 `rule-pack-mariadb.yaml`：

```bash
da-tools rule-pack-split --rule-packs-dir my-packs/ --output-dir split-output/
```

輸出（實跑）：

```
✓ Rule packs split successfully
  Edge rule groups: 1
  Central rule groups: 2
  Files processed: 1
```

`split-output/edge-rules/rule-pack-mariadb.yaml` 只含 `mariadb-normalization` 一組，部署到邊緣；`split-output/central-rules/rule-pack-mariadb.yaml` 含 `mariadb-threshold-normalization` 與 `mariadb-alerts` 兩組，部署到中央，與 threshold-exporter 的閾值比對後發出告警。

## 理由

### 架構簡單

**中央 Exporter**：設定集中管理。exporter 只有一份部署，以多副本做高可用，成本低。邊緣的 Prometheus 之間沒有相依，也不需要協調邏輯。

**邊緣 Exporter**：每個邊緣都要各自設定，中央要追蹤 N 個實例；N 個 exporter 升級版本時要彼此協調；中央彙整邊緣資料時，可能出現資料重複或遺漏。

### 時間與資源

決策當時估計，邊緣 Exporter 架構需要額外 6–8 週開發（實例管理框架、彙整邏輯、多層設定驗證）。

## 後果與已知限制

**得到的**

- 能較快推出多叢集支援，滿足多數情境。
- 降低初期的運維負擔。
- 為之後的邊緣 Exporter 架構先打好 API 與工具基礎。
- 客戶可以漸進採用：先用中央架構，之後再依需要升級。

**要承擔的**

- 若邊緣 Exporter 的需求很大，要面對部分重新設計。

## 考慮過的替代方案

| 方案 | 判斷 | 原因 |
|------|------|------|
| 同時實作兩種架構 | 不採用 | 時程延後、初期複雜度過高、難以測試 |
| 只實作邊緣架構 | 不採用 | 違背先交付最小可用版本的原則，也拖累客戶的時程 |

## 實作位置

- `da-tools federation-check` 可分別驗證邊緣叢集、中央叢集與端對端（`edge` / `central` / `e2e`）。
- `da-tools rule-pack-split` 把 Rule Pack 拆成邊緣正規化與中央兩部分，也能輸出 Prometheus Operator 使用的 PrometheusRule CRD（Kubernetes 自訂資源）。
- `da-tools operator-generate --kustomize` 產生列出所有 CRD 檔的 `kustomization.yaml`；`da-tools drift-detect --mode operator` 比對叢集上的 PrometheusRule CRD 與本地檔案。

## 相關

- [ADR-006: 租戶映射拓撲](./006-tenant-mapping-topologies.md) — 以中央 Exporter 搭配資料面的 Recording Rules 實現 1:N 映射
- [ADR-005: 投影卷掛載 Rule Pack](./005-projected-volume-for-rule-packs.md) — 多叢集部署中 Rule Pack 的掛載方式
- [Federation 整合指南](../integration/federation-integration.md) — 詳細的整合步驟
- [場景：多叢集聯邦架構](../scenarios/multi-cluster-federation.md) — 多叢集的部署案例
