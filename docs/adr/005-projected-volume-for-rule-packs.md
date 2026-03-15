---
tags: [adr, architecture]
audience: [platform-engineers]
version: v2.0.0
lang: zh
---

# ADR-005: 投影卷掛載 Rule Pack

## 狀態

✅ **Accepted** (v1.0.0)

## 背景

平台提供 15 個預構建的 Rule Pack，涵蓋不同的基礎設施與應用場景 (Kubernetes、JVM、Nginx、Database、等)。

### Rule Pack 分發的挑戰

租戶應能選擇性啟用 Rule Pack，而不是被迫接受所有 pack：

- **靜態場景**：某些租戶只關心 Kubernetes 監控，不需 JVM 或 Nginx rules
- **性能考量**：加載所有 Rule Pack 會增加 Prometheus 的啟動時間與記憶體消耗
- **可靠性**：若某個 Rule Pack 配置有誤，應不影響其他 pack 或核心系統

### 候選方案對比

| 方案 | 實現方式 | 可選性 | Prometheus 失敗模式 | 運維複雜度 |
|:-----|:--------|:-----:|:-----:|:-----:|
| 單一大 ConfigMap | 所有 rules → 一個 ConfigMap | ❌ 無 | 全部失敗 | 低 |
| N 個 ConfigMap + Projected Volume | 每個 pack → 獨立 ConfigMap + optional:true | ✅ 強 | 隔離失敗 | 中 |
| 動態規則注入 | Controller 動態修改 rules | ✅ 強 | 複雜 | 高 |

## 決策

**採用 Projected Volume + optional: true 架構：每個 Rule Pack 對應一個獨立的 ConfigMap，透過 Kubernetes Projected Volume 掛載至 Prometheus 的 rules 目錄，並設置 optional: true。**

```yaml
# Prometheus StatefulSet 部份配置示例
volumes:
  - name: rule-packs
    projected:
      sources:
        - configMap:
            name: rule-pack-kubernetes
            optional: true
            items:
              - key: rules.yaml
                path: kubernetes-rules.yaml
        - configMap:
            name: rule-pack-jvm
            optional: true
            items:
              - key: rules.yaml
                path: jvm-rules.yaml
        # ... 其他 15 個 rule pack
```

## 基本原理

### 為何選擇 Projected Volume

**可選性（Optionality）**：`optional: true` 表示若 ConfigMap 不存在或被刪除，Prometheus 不會因缺失而啟動失敗。租戶可透過 `kubectl delete configmap rule-pack-jvm` 簡單地卸載 Rule Pack。

**隔離與容錯**：每個 Rule Pack 獨立受控。若 Rule Pack-JVM 的配置有誤，只影響 JVM rules，不波及其他 pack 或核心 rules。

**動態管理**：Prometheus configmap-reload sidecar 監視 ConfigMap 變更，自動 reload rules。租戶可快速調整 Rule Pack 組合。

**運維簡潔**：無需自定義 controller 或複雜的初始化邏輯。純 K8s 原生功能，易於理解與維護。

### 為何拒絕單一大 ConfigMap

- **All-or-nothing**：無法選擇卸載，租戶被迫接受所有 pack
- **版本管理困難**：Rule Pack 的更新週期不同 (K8s pack 頻繁，Database pack 穩定)，難以統一版本
- **故障放大**：單一 ConfigMap 包含 15 個 pack，若其中一個有錯誤，整個系統啟動失敗

## 後果

### 正面影響

✅ 租戶可自由選擇 Rule Pack 組合，減少不必要的計算開銷
✅ Rule Pack 獨立更新，版本管理靈活
✅ 故障隔離：一個 pack 出問題不影響其他 pack
✅ 簡化 Prometheus 配置驗證：可為單個 pack 執行 `promtool check rules`
✅ 支援第三方或自定義 Rule Pack 的無縫擴展

### 負面影響

⚠️ Kubernetes manifests 變複雜 (Projected Volume + 15 個 ConfigMap source)
⚠️ 需維護 15 個 ConfigMap，初期佈署時間增加
⚠️ 租戶需瞭解 `optional: true` 的語意，避免誤刪

### 運維考量

- 提供 Helm chart 自動化生成 Projected Volume 配置，無需手工編寫
- 文件清楚說明「刪除 ConfigMap = 卸載 Rule Pack」的機制
- 監控工具 (e.g., `check_alert.py`) 應支援檢視「當前啟用的 Rule Pack 清單」
- CI 流程驗證：必須至少有一個 Rule Pack ConfigMap 存在，否則 Prometheus 規則為空

## 替代方案考量

### 方案 A：單一大 ConfigMap (已拒絕)
- 優點：配置簡單、部署快速
- 缺點：無選擇性、故障放大、版本管理困難

### 方案 B：動態規則注入 Controller (已考量)
- 優點：更靈活、可支援執行時 Rule Pack 變更
- 缺點：引入自定義 controller、複雜度高、難於維護

### 方案 C：Helm subcharts (已考量)
- 優點：每個 pack 可獨立 chart
- 缺點：Helm release 碎片化、依賴管理複雜

## 實施檢查清單

- [x] Rule Pack YAML 拆分為 15 個獨立 ConfigMap
- [x] Helm chart 配置 Projected Volume + optional:true
- [x] 測試卸載單個 Rule Pack 不造成 Prometheus 啟動失敗
- [x] 文件說明租戶如何禁用特定 Rule Pack
- [x] `check_alert.py` 新增「顯示啟用的 Rule Pack」功能

## 相關決策

- [ADR-001: 嚴重度 Dedup 採用 Inhibit 規則](./001-severity-dedup-via-inhibit.md) — inhibit rules 可作為 Rule Pack 一部分
- [ADR-003: Sentinel Alert 模式](./003-sentinel-alert-pattern.md) — sentinel rules 作為 Rule Pack 分發

## 參考資料

- [`../rule-packs/README.md`](../rule-packs/README.md) — Rule Pack 目錄結構與清單
- [`docs/getting-started/for-platform-engineers.md`](../getting-started/for-platform-engineers.md) §Rule Pack 配置 — 自定義 Rule Pack 指南
- [Kubernetes Projected Volume 官方文件](https://kubernetes.io/docs/concepts/storage/projected-volumes/)

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
| ["專案 Context 圖：角色、工具與產品互動關係"](../context-diagram.md) | ⭐⭐ |
