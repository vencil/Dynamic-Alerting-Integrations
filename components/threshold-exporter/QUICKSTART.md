# threshold-exporter — Alerting thresholds as hot-reloadable config

> **把告警閾值寫成 YAML、SHA-256 熱重載的多租戶 exporter——改一個數字即生效，不重啟、不碰 Prometheus rule。**
> *A multi-tenant exporter that turns alerting thresholds into hot-reloadable config — change a YAML number, not a Prometheus rule.*

|  |  |
|---|---|
| **What / 是什麼** | config-driven 多租戶閾值 exporter（SHA-256 熱重載、目錄掃描）。*Config-driven multi-tenant threshold exporter.* |
| **Why / 為什麼** | 改一個 YAML 數字即生效：**不重啟、不改 rule、不重部署**。*Edit one YAML value and it takes effect live.* |
| **Who / 給誰** | Platform Engineer / SRE |
| **Try（≤5 min）** | 見下方 3 步 / *3 steps below* |
| **→ You'll see** | 你的 YAML 閾值**變成 live `user_threshold` metric**。*Your YAML thresholds appear as live Prometheus metrics.* |

> 🎯 **主要服務對象**：Platform Engineer / SRE（部署與閾值營運，見 [Platform Engineer 角色指南](../../docs/getting-started/for-platform-engineers.md)）。

**Prerequisite**：Go 1.26.3+（[安裝](https://go.dev/dl/)）。

## Try it（從本目錄 `components/threshold-exporter/` 執行）

```sh
# 1) build（main package 在 app/）
cd app && go build -o ../threshold-exporter . && cd ..

# 2) 用內附 sample conf.d 跑起來（含 db-a / db-b 兩個範例租戶）
./threshold-exporter --config-dir ./config/conf.d
#   → 監聽 :8080；--listen 可改 port

# 3) 另開一個 terminal：看你的閾值變成 metric
curl -s localhost:8080/metrics | grep user_threshold
```

**你會看到**（例）：

```
user_threshold{component="container",metric="cpu",severity="warning",tenant="db-a"} 70
user_threshold{component="container",metric="memory",severity="warning",tenant="db-a"} 85
```

`user_threshold` 的**值就是你在 `config/conf.d/db-a.yaml` / `_defaults.yaml` 設的閾值**。改那個數字、存檔，exporter 在 `--reload-interval`（預設 30s）內 SHA-256 偵測變更並熱重載——`/metrics` 立即反映新值，**不需重啟**。

> 想看「改閾值 → 告警紅燈」的完整連動，跑整套 stack（見下方 try-local）。

## Next
- ← **先玩整套**：[`try-local/`](../../try-local/)（exporter + tenant-api + portal + Prometheus + Alertmanager 一鍵）
- 📖 **深入配置 / 旗標參考**：[`README.md`](README.md)（四層繼承、熱重載、維度標籤、Cardinality Guard）
- ✍️ **不寫 PromQL 也能加告警**：租戶可用平台 recipe 宣告自訂告警 → [README §自訂告警](README.md#45-自訂告警-_custom_alerts)
- → **上 production**：[`helm/threshold-exporter/`](../../helm/threshold-exporter/)（Helm 部署、ConfigMap / Operator 雙路徑）
