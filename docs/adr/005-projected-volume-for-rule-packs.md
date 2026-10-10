---
title: "ADR-005: 投影卷掛載 Rule Pack"
tags: [adr, architecture]
audience: [platform-engineers]
version: v2.9.0
lang: zh
id: ADR-005
tracking_kind: adr
status: accepted
domain: k8s
created_at: 2026-03-13
updated_at: 2026-10-10
---
# ADR-005: 投影卷掛載 Rule Pack

> **Language / 語言：** **中文 (Current)** | [English](./005-projected-volume-for-rule-packs.en.md)

**決策摘要**：每個 Rule Pack 各放在一個獨立的 ConfigMap，以 Kubernetes 的 projected volume 一起掛進 Prometheus 的規則目錄，每個來源都設 `optional: true`。刪掉某個 Rule Pack 的 ConfigMap，在 Prometheus 重新載入成功後就等於卸載它，Prometheus 不會因此啟動失敗。

## 狀態

✅ **Accepted**（v1.0.0）

2026-10-10：更正故障隔離的範圍：只涵蓋 ConfigMap 不存在；任一規則檔語法錯誤會影響全部 Rule Pack，改列為已知限制。

## 名詞

- **Rule Pack**：平台隨附的一組 Prometheus 規則檔（recording rule 與告警規則），每個檔案對應一種資料庫或用途，例如 `rule-pack-mariadb.yaml`。部署時每個 Rule Pack 對應一個名為 `prometheus-rules-<pack>` 的 ConfigMap。兩個例外：平台自我監控的 `prometheus-rules-platform` 在 `rule-packs/` 下沒有對應的檔案；租戶自訂告警的 `prometheus-rules-custom-alerts` 不在本文的 projected volume 裡。
- **Projected volume（投影卷）**：Kubernetes 的一種 volume，把多個來源（ConfigMap、Secret 等）合併掛載到同一個目錄。
- **`optional: true`**：projected volume 來源的設定。設了之後，該 ConfigMap 不存在時 Pod 照常啟動，只是目錄裡少了那幾個檔案。

## 背景

平台提供多個預先建好的 Rule Pack，涵蓋不同的基礎設施與應用場景（Kubernetes、JVM、Nginx、各種資料庫等）。

租戶應能選擇要啟用哪些 Rule Pack，而不是被迫全部接受：

- **按需啟用**：某些租戶只關心 Kubernetes 監控，不需要 JVM 或 Nginx 的規則。
- **效能**：載入所有 Rule Pack 會增加 Prometheus 的啟動時間與記憶體消耗。

### 候選方案比較

| 方案 | 做法 | 可選性 | 運維複雜度 |
|:-----|:-----|:-----:|:-----:|
| 單一大 ConfigMap | 所有規則放進一個 ConfigMap | ❌ 無 | 低 |
| 多個 ConfigMap + projected volume | 每個 Rule Pack 一個 ConfigMap，來源設 `optional: true` | ✅ 高 | 中 |
| 動態注入規則 | 自訂 controller 在執行時修改規則 | ✅ 高 | 高 |

## 決策

**採用 projected volume + `optional: true`：每個 Rule Pack 對應一個獨立的 ConfigMap，透過 projected volume 掛載到 Prometheus 的規則目錄，每個來源都設 `optional: true`。**

### 範例

`k8s/03-monitoring/deployment-prometheus.yaml` 的 `rules` volume（節錄）：

```yaml
volumes:
  - name: rules
    projected:
      sources:
        - configMap:
            name: prometheus-rules-mariadb
            optional: true
            items:
              - key: mariadb-recording.yml
                path: mariadb-recording.yml
              - key: mariadb-alert.yml
                path: mariadb-alert.yml
        - configMap:
            name: prometheus-rules-jvm
            optional: true
            items:
              - key: jvm-recording.yml
                path: jvm-recording.yml
              - key: jvm-alert.yml
                path: jvm-alert.yml
        # ... 其餘 Rule Pack 比照
```

這個 volume 掛在 `/etc/prometheus/rules`，Prometheus 設定以 `rule_files: ["/etc/prometheus/rules/*.yml"]` 讀取。

卸載 JVM 的 Rule Pack：

```bash
kubectl delete cm prometheus-rules-jvm -n monitoring
```

結果：`jvm-recording.yml` 與 `jvm-alert.yml` 從規則目錄消失，其他 Rule Pack 的規則照常運作；Prometheus 不會因為缺少這個 ConfigMap 而啟動失敗。

### 為什麼選 projected volume

- **可選**：`optional: true` 讓 ConfigMap 不存在或被刪除時，Prometheus 仍能啟動。卸載一個 Rule Pack 只要刪掉它的 ConfigMap。
- **改了自動生效**：Prometheus 本身不監看規則檔。同一個 Pod 裡的 config-reloader sidecar（與 Prometheus 放在同一個 Pod 的輔助容器）監看規則目錄的檔案內容，有變更就呼叫 Prometheus 的 `/-/reload`，調整 Rule Pack 組合不必重啟 Prometheus。這只涵蓋 Rule Pack：主設定 `prometheus.yml` 以 `subPath` 掛載，而 Kubernetes 不會把 ConfigMap 的更新傳進以 `subPath` 掛載的檔案，改它仍須重啟 Pod。
- **運維簡單**：不需要自訂 controller 或複雜的初始化邏輯，只用 Kubernetes 原生功能。

### 為什麼不用單一大 ConfigMap

- **全有或全無**：無法只卸載其中一個，租戶被迫接受所有 Rule Pack。
- **版本管理困難**：各 Rule Pack 的更新週期不同，放在一起很難統一管理版本。

## 後果與已知限制

**得到的**

- 租戶可以自由選擇 Rule Pack 組合，減少不必要的計算開銷。
- Rule Pack 可以各自更新，版本管理有彈性。
- 每個 Rule Pack 可以單獨用 `promtool check rules` 驗證。
- 第三方或自訂的 Rule Pack 可以照同樣的方式加進來。

**要承擔的**

- Kubernetes manifest 變複雜：projected volume 要逐一列出每個 Rule Pack 的 ConfigMap 來源。
- 租戶需要了解 `optional: true` 的意義，避免誤刪 ConfigMap。

**已知限制**

- 隔離只涵蓋「ConfigMap 不存在」。任何一個 Rule Pack 的規則檔有語法錯誤時，Prometheus 啟動會失敗；執行中的 reload 則整批拒絕，所有 Rule Pack 都停在上一次成功載入的版本。

**運維建議**

- 提供 Helm chart 自動生成 projected volume 設定，免去手寫。
- 文件要寫清楚「刪除 ConfigMap = 卸載 Rule Pack」。
- 監控工具應能列出目前啟用的 Rule Pack。
- CI 應檢查至少有一個 Rule Pack ConfigMap 存在，否則 Prometheus 沒有任何規則。

## 考慮過的替代方案

### 單一大 ConfigMap（不採用）

設定簡單、部署快，但無法選擇性卸載，版本也難管理。

### 動態注入規則的 controller（考慮過，不採用）

更有彈性，可以在執行時調整 Rule Pack。但要引入並維護一個自訂 controller，複雜度高。

### 每個 Rule Pack 一個 Helm subchart（考慮過，不採用）

每個 Rule Pack 可以是獨立的 chart，但 Helm release 會變得零碎，chart 之間的依賴也難管理。

## 相關

- [ADR-001: 嚴重度 Dedup 採用 Inhibit 規則](./001-severity-dedup-via-inhibit.md) — 抑制規則可以作為 Rule Pack 的一部分
- [ADR-003: Sentinel Alert 模式](./003-sentinel-alert-pattern.md) — sentinel 告警規則隨 Rule Pack 分發
- [`rule-packs/README.md`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/rule-packs/README.md) — Rule Pack 清單與卸載方式
- [`docs/getting-started/for-platform-engineers.md`](../getting-started/for-platform-engineers.md) — 自訂 Rule Pack 指南
- [Kubernetes Projected Volume 官方文件](https://kubernetes.io/docs/concepts/storage/projected-volumes/)
