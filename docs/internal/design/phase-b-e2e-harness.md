---
title: "Phase 2 E2E Harness — Alert Fire-through Latency Design"
tags: [design, internal, phase-b, benchmark]
audience: [maintainers, platform-engineers]
version: v2.7.0
status: draft
lang: zh
---

# Phase 2 E2E Harness — Alert Fire-through Latency Design

> **受眾**：Maintainers、Platform Engineers
> **版本**：v2.7.0（草案，目標 land 在 v2.8.0 phase .b）
> **狀態**：`draft` — 設計文件先行；implementation 採 **calibration gate** 模型（見 §6.5），不以 customer sample 為硬阻擋
>
> **相關文件**：[Benchmark Playbook](../benchmark-playbook.md) · [Architecture & Design](../../architecture-and-design.md) · [PR #59 Phase 1 baseline](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/59)

本文件描述 Phase 2 alert fire-through 端到端 (e2e) latency harness 的設計。Phase 1（PR #59，merge `f1f14e7`，2026-04-25）已完成 1000/2000/5000-tenant hierarchical baseline 對「reload 自身」的測量，但**沒有**涵蓋從 config change 到 alert dispatched 的完整路徑。本文件先行 land，使 implementation PR 在 design contract 確立後可立即推進。

---

## 1. Background — Phase 1 vs Phase 2 framing

### 1.1 Phase 1 已測量項目（infrastructure SLO）

Phase 1（B-1 Phase 1 + B-8，PR #59）量測的是 threshold-exporter **reload 路徑本身**的耗時，全部停在 exporter 內部：

| Benchmark | 量測對象 | Phase 1 結果（1000-tenant 中位數） |
|-----------|---------|--------------------------------|
| `BenchmarkScanDirHierarchical` | bare scan（只走目錄、無解析） | 32 ms |
| `BenchmarkFullDirLoad_Hierarchical` | parse + merge `_defaults.yaml` 鏈 | 146 ms |
| `BenchmarkDiffAndReload_Hierarchical_NoChange` | 每 tick reload（quiet path） | 189 ms |
| `BenchmarkBlastRadius_DefaultsChange_Hierarchical` | defaults 改變 → affected count（21） | 212 ms |

詳見 [`benchmark-playbook.md` § v2.8.0 1000-Tenant Hierarchical Baseline](../benchmark-playbook.md)。

### 1.2 Phase 1 缺口

Phase 1 **沒有**測量 alert fire-through。客戶 (end customer) 真正關心的 SLO 是：

> **「從 operator 改 config → 對應 alert 從 webhook 收到」**有多快？

而**不是**「從 config change → exporter `/effective` endpoint 反映新值」。完整路徑橫跨四個獨立服務：exporter、Prometheus、Alertmanager、webhook receiver。Phase 1 只覆蓋到 exporter 出口；剩下三段是 Phase 2 範圍。

Phase 2 的目的是把這個盲區量化：**operator-perceived latency = T4 − T0**（5-anchor 模型，見 §2）。

### 1.3 Why design doc lands first

Phase 2 implementation 涉及四個獨立 service 的 wiring + 量測協定，在沒有確立 design contract（time anchors / output schema / trigger model）前直接寫 code 風險高。先把 design doc land，可以：

- 把 architecture choice (docker-compose vs k3d) 的論證固化，避免實作中重開戰場。
- 鎖住 measurement model，避免實作者各自詮釋 7 種「scrape timestamp」。
- 讓 follow-up implementation PR 的 acceptance criteria 早早可審。

---

## 2. Goal — 5-Anchor Measurement Model

### 2.1 設計原則

原 7-anchor 模型（v1 草稿）的根本問題是把 **interval-quantized observation** 當作 **point-in-time event**。Prometheus 的 scrape interval 與 evaluation interval 是兩個**獨立**的離散時間網格 — 兩者間 0 到 1× scrape_interval 的「相位差」是隨機的、不是 stage。

新 5-anchor 模型遵守三條規則：

1. **能由 emitter 自己記時的，不靠 observer**（exporter 自己 emit timestamp gauge，不靠 Prometheus scrape time）
2. **interval-quantized 的東西塌成單一 stage**（不假裝 scrape 與 eval 是兩個獨立 stage）
3. **所有 anchor 在同一 kernel clock domain**（driver 進 compose，不在 host 端）

### 2.2 5-Anchor 圖

```
   ┌─────────────────────┐  T0 ← driver wall-clock (config write begins)
   │  driver (Python)    │  T4 ← driver polls receiver, gets wall-clock
   └──────────┬──────────┘
              │ write file
              ▼
   ┌─────────────────────┐  T1 ← exporter wall-clock, exposed as gauge:
   │  threshold-exporter │       last_scan_complete_unixtime_seconds
   │                     │  T2 ← exporter wall-clock, exposed as gauge:
   │                     │       last_reload_complete_unixtime_seconds
   │  /metrics exposes:  │
   │    last_scan_*      │
   │    last_reload_*    │
   │    tenant_threshold_value{...}
   │    actual_metric_value{...}  ← from pushgateway
   └──────────┬──────────┘
              │ Prometheus scrapes (5s)
              ▼
   ┌─────────────────────┐  T3 ← driver polls /api/v1/alerts
   │  Prometheus         │       reads `activeAt` from response
   │  rule eval (5s)     │       (alert 第一次進 firing 的時點)
   └──────────┬──────────┘
              │ POST alert
              ▼
   ┌─────────────────────┐
   │  Alertmanager       │  (無獨立 anchor; AM→webhook 併入 stage D)
   │  group_wait: 0s     │
   └──────────┬──────────┘
              │ POST webhook
              ▼
   ┌─────────────────────┐  T4 ← receiver wall-clock
   │  receiver           │       (driver 接著 GET /last-post 取得)
   └─────────────────────┘

   所有 service 共用 host kernel clock（同一 docker daemon）→ 無 skew。
```

### 2.3 Stage 分解

| Stage | 範圍 | 量測對象 | 預期數量級 | Regression 訊號 |
|-------|------|---------|-----------|---------------|
| **A** | T1 − T0 | fsnotify pickup（OS 通知 exporter 檔案變動） | ms | OS / mount 層問題 |
| **B** | T2 − T1 | parse + merge `_defaults.yaml` + `diffAndReload` | 100 ms 級（**Phase 1 baseline 領域**） | exporter 內部 regression |
| **C** | T3 − T2 | Prometheus dispatch（**scrape boundary + eval boundary 糾纏**；0–2× scrape_interval） | 0–10 s（5s scrape × 2） | scrape_interval 設定議題，**非** exporter |
| **D** | T4 − T3 | Alertmanager 收 alert + dispatch + webhook 抵達 | ms | AM / network |

> **Stage C 為何不拆**：scrape interval 與 evaluation interval 獨立 — alert 從「Prometheus 有資料」到「alert 進 firing」之間是 0 到 1× evaluation_interval 的隨機 phase。把它拆兩 stage 不會比合併資訊量更高，反而讓 P95 數字不可解釋。誠實併。

### 2.4 Fire + Resolve 對稱量測

每 run 跑一次 fire path + 一次 resolve path，**共用 5 anchors 的鏡像**：

| 方向 | T0 含義 | T3 anchor | T4 anchor |
|------|---------|----------|-----------|
| **Fire** | 寫 trigger config（actual > threshold） | `ALERTS{alertstate="firing",...}` 出現的 `activeAt` | webhook payload `status="firing"` |
| **Resolve** | 還原 config（actual < threshold） | `ALERTS{alertstate="firing",...}` 不再 match 的時點 | webhook payload `status="resolved"` |

→ Alertmanager config 必須 `send_resolved: true`（見 §4.4）。

### 2.5 Output JSON Schema

每 run 產一筆，包含 fire 與 resolve 兩段：

```json
{
  "run_id": 7,
  "warm_up": false,
  "fixture_kind": "synthetic-v2",
  "gate_status": "pending",
  "fire": {
    "T0_unix_ns": 1714032001000000000,
    "T1_unix_ns": 1714032001050000000,
    "T2_unix_ns": 1714032001195000000,
    "T3_unix_ns": 1714032005120000000,
    "T4_unix_ns": 1714032005165000000,
    "stage_ms": {"A": 50, "B": 145, "C": 3925, "D": 45},
    "e2e_ms": 4165
  },
  "resolve": {
    "T0_unix_ns": 1714032015000000000,
    "T1_unix_ns": 1714032015048000000,
    "T2_unix_ns": 1714032015190000000,
    "T3_unix_ns": 1714032020110000000,
    "T4_unix_ns": 1714032020155000000,
    "stage_ms": {"A": 48, "B": 142, "C": 4920, "D": 45},
    "e2e_ms": 5155
  }
}
```

聚合（**n ≥ 30**，第 1 run 標 `warm_up: true` 不入聚合）→ fire 與 resolve **分別**報 P50 / P95 / P99，並對 stage C 顯示直方圖（不只 percentile，因 quantization noise 為 stage C 主導項）。

`fixture_kind ∈ {synthetic-v1, synthetic-v2, customer-anon}`、`gate_status ∈ {pending, passed, failed, voided}` — 由 §6.5 calibration gate 機制填入。

### 2.6 Hidden prerequisite — exporter 改動

5-anchor 模型要求 exporter 新增兩個 gauge（**約 10 行 Go**），與 harness 同 PR：

```go
// components/threshold-exporter/app/metrics.go (additions)
var (
    lastScanCompleteUnixSeconds = prometheus.NewGauge(prometheus.GaugeOpts{
        Name: "last_scan_complete_unixtime_seconds",
        Help: "Wall-clock unix seconds at the most recent successful scanDirHierarchical completion.",
    })
    lastReloadCompleteUnixSeconds = prometheus.NewGauge(prometheus.GaugeOpts{
        Name: "last_reload_complete_unixtime_seconds",
        Help: "Wall-clock unix seconds at the most recent successful diffAndReload completion.",
    })
)

// 於 scanDirHierarchical / diffAndReload 結尾各 .Set(float64(time.Now().Unix()))。
```

值得 export 的副效益：production 也能用這兩個 gauge 監控「exporter 是否 stuck」（age = `time() - last_scan_complete_unixtime_seconds`）。

---

## 3. Architecture choice — docker-compose, NOT k3d

### 3.1 決策

選用 **docker-compose**，**不**用 k3d / kind / minikube。下述論證為 design 重點，避免日後 reviewer 重提。

### 3.2 Rationale

**(a) k3d 帶入的 K8s 網路雜訊在 5s scrape quantization 解析度下不可區分**

k3d 內含完整 K8s data plane：CNI（flannel）、kube-proxy iptables、Service VIP、CoreDNS。這些在 alert fire-through path 上引入的 jitter 通常 1–2 ms（busy cluster 可能尖峰至幾十 ms），但 stage C 的 quantization 本身就是 0–10 s 級。**k3d 的 K8s noise 落在我們的量測解析度以下**，加它不會讓數字更精準，只會讓重現條件更脆弱。

**(b) ConfigMap-symlink rotation 已被 unit test 覆蓋**

K8s 把 ConfigMap mount 到 Pod 時用的 atomic-rename-via-symlink 行為，已在 PR #54 的 A-8b unit test (`TestScanDirHierarchical_K8sSymlinkLayout`) 涵蓋。e2e harness 若再測同一件事，是 redundant retest at higher cost。

**(c) docker-compose 的 concrete 優勢**

| 維度 | docker-compose | k3d |
|------|----------------|-----|
| 啟動時間（cold） | ~10 s | 30–60 s（含 cluster bootstrap） |
| Service ordering | `depends_on` + `healthcheck` 直接表達 | 需 Pod readiness probe + init container 模擬 |
| Volume 模型 | host bind-mount（FUSE/Cowork 上 native） | hostPath 或 PVC，FUSE 環境會踩 inode quirks |
| Cleanup | `docker compose down -v` | `k3d cluster delete` + 殘留 docker network |
| Network | bridge，single-host | overlay + iptables，多層除錯 |
| Repro on dev laptop | ✓ | ✓（但較重） |

**(d) docker-compose CANNOT validate（誠實標註）**

| 不被驗證的行為 | 是否影響 SLO？ | 替代覆蓋 |
|--------------|-----------|---------|
| K8s Service discovery / endpoint slice 更新延遲 | 在 5s quantization 解析度下不可區分 | Phase 3（如有）可接 k3d 補強 |
| Rolling pod restart 期間 scrape 失敗率 | 否，與 config-change SLO 正交 | 屬 deployment SLO，另案 |
| ConfigMap update semantics（projected volume） | 否，已被 A-8b unit test 覆蓋 | A-8b |
| K8s-native RBAC / NetworkPolicy 路徑 | 否，與 alert fire-through 正交 | governance-security.md |

→ 結論：docker-compose 為 Phase 2 SLO 量測的**正確**選擇；k3d 不是「更高保真」，而是「換個維度的 noise」。

---

## 4. Stack

### 4.1 Components

| Service | 來源 | 角色 |
|---------|------|------|
| **threshold-exporter** | `build: ../../components/threshold-exporter`（current branch + §2.6 gauges） | 暴露 `/metrics`；watch `conf.d/`；emit timestamp gauges |
| **prometheus** | `prom/prometheus:v2.55.0`（pinned，不用 `latest`） | scrape exporter + pushgateway / 跑 alert rules |
| **pushgateway** | `prom/pushgateway:v1.10.0`（pinned） | 接收 driver 注入的 `actual_metric_value` |
| **alertmanager** | `prom/alertmanager:v0.27.0`（pinned） | 收 firings → route to webhook（fire + resolve 都送） |
| **receiver** | `build: ./receiver`（custom Go ~80 行） | log 每筆 POST + ring buffer + ts 查詢 endpoint |
| **driver** | `build: ./driver`（Python on alpine） | 在 compose **內部**跑量測腳本，避 clock skew |

> **為何 receiver 自寫不用現成 image**：commonly-used `webhook-logger` 第三方 image 無固定維護者；80 行 Go server 為最低風險選項，且能直接寫 NDJSON 到 stdout 便於解析。

> **為何 driver 進 compose**：消除 host wall-clock vs container wall-clock 之間的 skew（Docker Desktop / Cowork VM 上 host 與容器 VM 是兩個 kernel clock domain）。driver-as-container 後，T0 / T4 與 exporter / Prometheus / receiver 的 timestamp 全部來自同一 host kernel。

### 4.2 Compose 骨架

```yaml
# tests/e2e-bench/docker-compose.yml
version: "3.9"
services:
  threshold-exporter:
    build: { context: ../../components/threshold-exporter }
    volumes:
      - ./fixture/active:/etc/threshold-exporter/conf.d:rw   # driver writes here
    expose: ["9090"]
    healthcheck:
      test: ["CMD", "wget", "-qO-", "http://localhost:9090/-/ready"]
      interval: 2s

  pushgateway:
    image: prom/pushgateway:v1.10.0
    expose: ["9091"]

  prometheus:
    image: prom/prometheus:v2.55.0
    volumes:
      - ./prometheus.yml:/etc/prometheus/prometheus.yml:ro
      - ./alert-rules.yml:/etc/prometheus/alert-rules.yml:ro
    depends_on:
      threshold-exporter: { condition: service_healthy }

  alertmanager:
    image: prom/alertmanager:v0.27.0
    volumes:
      - ./alertmanager.yml:/etc/alertmanager/alertmanager.yml:ro
    depends_on:
      prometheus: { condition: service_healthy }

  receiver:
    build: { context: ./receiver }
    expose: ["5001"]

  driver:
    build: { context: ./driver }
    volumes:
      - ./fixture:/fixture:rw          # mutates fixture/active/*
      - ./bench-results:/results:rw    # writes per-run JSON
    depends_on:
      alertmanager: { condition: service_started }
      receiver:     { condition: service_started }
    command: ["python3", "/app/driver.py", "--count", "30"]
```

### 4.3 Pre-canned alert rule（actual vs threshold 雙 metric 模型）

`tenant_threshold_value` 是 exporter 由 config 計算出的閾值；`actual_metric_value` 由 pushgateway 注入模擬「真實業務 metric」。Alert 條件 = 真實值跨越閾值，這是生產真實樣態（v1 草稿用單一 metric 自比 100，邏輯不通）。

```yaml
# tests/e2e-bench/alert-rules.yml
groups:
  - name: e2e-bench
    interval: 5s
    rules:
      - alert: TenantThresholdExceeded
        # actual 與 threshold 透過 tenant_id label 對齊
        expr: |
          actual_metric_value
            > on(tenant_id) group_left
          tenant_threshold_value{metric="bench_trigger"}
        for: 0s
        labels:
          severity: critical
          run_kind: e2e-bench
        annotations:
          summary: "E2E bench: actual exceeded threshold for {{ $labels.tenant_id }}"
```

> **Run isolation**：每 run 用**獨立** `tenant_id`（`bench-run-1` … `bench-run-30`）→ alert label set 全部不同 → Alertmanager 不 dedup。第 i run 的 fixture 寫 `bench-run-{i}` tenant，pushgateway push `actual_metric_value{tenant_id="bench-run-{i}"}=200` 配合 threshold 100 → fire；resolve 階段 push `=50` → resolve。

### 4.4 Alertmanager → webhook

```yaml
# tests/e2e-bench/alertmanager.yml
route:
  receiver: e2e-webhook
  group_wait: 0s
  group_interval: 1s
  repeat_interval: 5m
  group_by: [tenant_id]   # 各 run 不同 tenant_id 自然不混群

receivers:
  - name: e2e-webhook
    webhook_configs:
      - url: http://receiver:5001/hook
        send_resolved: true   # 量 resolve path 必要
```

`group_wait: 0s` + `group_by: [tenant_id]` 為刻意設定，避免 batching latency 計入量測。

---

## 5. Measurement protocol

### 5.1 Pre-flight：fixture pre-creation

第一次 run 之前 driver 跑 setup phase：把 `fixture/active/conf.d/` 預先放入 30 個 placeholder tenant 檔案（`bench-run-1.yaml` … `bench-run-30.yaml`），每個內容為 `bench_trigger: 100`（未觸發狀態）。**Run loop 中只 modify 既有檔案的內容，從不 create 新檔案**——避 fsnotify 對 create vs modify 的 event 路徑差異。

### 5.2 Single-run protocol（fire + resolve 對稱）

```text
=== Run i, fire phase ===
T0 ← now()
  write fixture/active/conf.d/bench-run-{i}.yaml with bench_trigger threshold = 100
  POST pushgateway: actual_metric_value{tenant_id="bench-run-{i}"} = 200
T1 ← poll exporter /metrics, read last_scan_complete_unixtime_seconds gauge value
T2 ← poll exporter /metrics, read last_reload_complete_unixtime_seconds gauge value
T3 ← poll Prometheus /api/v1/alerts, find ALERTS{tenant_id=bench-run-{i},alertstate=firing}
     → use response.activeAt (Prometheus's internal time, same kernel clock)
T4 ← poll receiver /posts?since={T0}, find first POST with status=firing & matching tenant
     → use receiver's recorded ts

=== Run i, resolve phase ===
T0' ← now()
  POST pushgateway: actual_metric_value{tenant_id="bench-run-{i}"} = 50
  (fixture 不動;  threshold 維持 100;  actual 50 < 100 = resolve)
T1', T2' ← (likely 與 fire 同值, 因 fixture 沒 change; 但量測仍記錄)
   實作上若 reload 因無 fixture 變動而沒觸發, T1'/T2' 取 fire 階段的最後值並標 stage_ab_skipped=true
T3' ← poll Prometheus /api/v1/alerts, find ALERTS{tenant_id=bench-run-{i}} 不再 match
T4' ← poll receiver /posts?since={T0'}, find POST with status=resolved & matching tenant
```

> **Resolve 量測注意**：resolve 路徑的 stage A/B 通常 trivially 0（fixture 沒 change，不觸發 reload）。stage A/B 若標 `skipped`，aggregator 對 resolve path 只報 stage C+D 與 e2e。客戶看的 resolve SLO 主要是 stage C+D，這仍有意義。

### 5.3 Run-loop with warm-up handling

```bash
#!/usr/bin/env bash
# tests/e2e-bench/run.sh
set -euo pipefail

COUNT="${COUNT:-30}"
OUT="bench-results/e2e-$(date -u +%Y%m%dT%H%M%SZ).json"

docker compose up -d --wait
mkdir -p bench-results

# driver 本身在 compose 內跑;  此處等其完成
docker compose logs -f driver &
LOGS_PID=$!
docker compose wait driver
kill $LOGS_PID || true

# driver 寫到 bench-results/per-run-*.json;  此處彙整
python3 aggregate.py bench-results/per-run-*.json > "$OUT"
docker compose down -v
```

driver 本身內部跑 `for i in range(COUNT+1)`（多一輪做 warm-up，標 `warm_up: true`）。aggregator 過濾 `warm_up=false` 後計 P50/P95/P99。

### 5.4 為什麼 stage C 要顯示直方圖

stage C（T3 − T2）量測值受 scrape interval 對齊 phase 影響：T2 落在 scrape window 早段或晚段會差到接近一個完整 interval。30 runs 的 stage C 分布**不應**是窄峰，而應呈現 0–scrape_interval 區間的近似 uniform — 若不是 uniform，代表觸發時點與 scrape 對齊有系統偏差，需要排查 driver 寫入時序。

---

## 6. Customer sample protocol

> **Phase 2 implementation 不以 customer sample 為硬阻擋**——採 §6.5 calibration gate 模型。本節列 sample 抵達後的處理規格。

### 6.1 為何要 customer sample

real-world 的 `_defaults.yaml` 鏈深度、tenant overlay 命中率、metric 種類分布，與合成 fixture 之間有顯著差異。沒有客戶 sample 直接做 final SLO sign-off，數字會偏離客戶真實環境。

但**沒有 sample 不代表完全不能做** — synthetic fixture 可以做到「數量級正確」，作為 v2.8.0 phase .b 的 calibrated baseline；customer sample 作 final calibration gate（§6.5）。

### 6.2 Sample 需求（要傳達給客戶）

| 項目 | 要求 | 備註 |
|------|------|------|
| Tenant ID anonymization | 用 deterministic hash（同 real ID → 同 anon ID across files） | 保留 ID 之間的關係（同 prefix 暗示同 domain） |
| Threshold value redaction | 商業敏感閾值替換為合理範圍的隨機值 | 保留量級即可，不需精確 |
| Subset size | 最少 1000 tenant；建議 2000–5000 | 對齊 Phase 1 baseline 點 |
| Defaults 鏈深度 | 完整保留（不簡化） | 鏈深度本身是 fixture calibration 的關鍵變數 |
| Metric 種類 | 完整保留 | 影響 export cardinality |

### 6.3 Repository convention

```
tests/e2e-bench/fixture/
├── synthetic-v1/conf.d/      # 由 buildDirConfigHierarchical 產生（Phase 1 reuse;  uniform 分布）
├── synthetic-v2/conf.d/      # 加 zipfian tenant size + power-law overlay depth（更逼真合成）
├── customer-anon/            # 客戶 anonymized sample
│   ├── .gitignore            # 忽略所有實際檔案
│   └── README.md             # 解釋如何取得 sample
└── active/                   # run-time 工作區, driver 寫入此處（內容由 fixture-kind 選擇複製）
```

`customer-anon/` 整個目錄 gitignored；CI 不依賴此 fixture。

### 6.4 Fallback 行為

當 customer fixture 缺席時，run.sh 自動 fallback 到 `synthetic-v2/`，並在輸出 JSON 加 `"fixture_kind": "synthetic-v2"` 欄位。aggregator 依此渲染對應 banner（見 §6.5）。

### 6.5 Calibration gate（取代「hard blocker」模型）

| 項目 | 規格 |
|------|------|
| **預設狀態** | 任何 run 預設 `gate_status: "pending"` |
| **Gate 通過條件** | `customer-anon` ≥1000 tenants × ≥30 runs × P95 與最近一次 `synthetic-v2` P95 差距 ≤30%（30% 為 v2.8.0 placeholder，可由 implementation PR 微調） |
| **Gate 通過後** | 該批 customer-anon run 標 `gate_status: "passed"`；最近一次 synthetic-v2 P95 retroactively 標 `gate_status: "passed"`（calibration confirmed） |
| **Gate 失敗條件** | 差距 >30% → customer-anon 標 `gate_status: "failed"`；對應 synthetic-v2 baseline 標 `voided`，benchmark-playbook 該 section 加紅色 banner，外部 reference 全部須 review |
| **Kill switch** | v2.9.0 cut 前若 customer sample 未抵達，**強制 go/no-go review**：要嘛 explicit 接受 synthetic-v2 為定案 baseline（含假設文件化），要嘛 rescope phase 2 |
| **Aggregator 渲染** | output JSON `fixture_kind` × `gate_status` 矩陣決定 banner 文字： `synthetic-*` + `pending` → 黃框「pending customer validation」；`customer-anon` + `passed` → 綠勾；任何 `failed` / `voided` → 紅框 |

設計理由：把 customer sample 從 phase blocker 重新定義為 **calibration gate** — Phase 2 永遠可 land，但數字永遠帶 gate 狀態 metadata。失敗模式（「內部 baseline 變外部 SLA 的滑坡」）由 banner 機制 + kill switch 強制可見性與時限決定。

---

## 7. Implementation outline — contract not code

implementation PR 可選 Go testcontainers / bash+Python / 純 Go test 任一實作路徑，**只要符合下列 contract**：

### 7.1 Input contract

- Fixture 來源：`tests/e2e-bench/fixture/{synthetic-v1,synthetic-v2,customer-anon}/conf.d/`，由 env var `E2E_FIXTURE_KIND` 選擇（缺省 `synthetic-v2`）
- Trigger 注入：pushgateway HTTP API
- Run count：env var `COUNT`（預設 30）

### 7.2 Output contract

- Per-run JSON 寫到 `bench-results/per-run-{run_id}.json`，schema 見 §2.5
- 聚合輸出 `bench-results/e2e-{ISO8601}.json`，含 fire / resolve P50/P95/P99 + stage 直方圖 + gate banner
- Last line of stdout = single-line JSON summary（與 A-15 `bench_wrapper.sh` convention 一致）

### 7.3 Reference directory layout（建議，不強制）

```
tests/e2e-bench/
├── docker-compose.yml          # §4.2
├── prometheus.yml              # 5s scrape, exporter + pushgateway targets
├── alertmanager.yml            # §4.4 (send_resolved: true)
├── alert-rules.yml             # §4.3 (actual vs threshold)
├── fixture/                    # §6.3
├── receiver/{Dockerfile,main.go}    # §7.4
├── driver/{Dockerfile,driver.py}    # §7.5
├── aggregate.py                # P50/P95/P99 + gate banner 渲染
└── run.sh                      # §5.3 orchestration（可省，driver 自走 loop）
```

### 7.4 `receiver/main.go` reference（~90 行）

關鍵：ring buffer + `/posts?since={unix_ns}` query endpoint（不只 `/last-post`，避 cross-run 干擾）。

```go
type Post struct {
    ReceivedUnixNs int64           `json:"received_unix_ns"`
    Status         string          `json:"status"`   // "firing" / "resolved"
    TenantID       string          `json:"tenant_id"`
    Body           json.RawMessage `json:"body"`
}

// ring buffer of last 200 posts;  GET /posts?since=N&tenant_id=X filters
```

### 7.5 `driver/driver.py` reference（~150 行）

每 run 跑一次 fire phase 跑一次 resolve phase（§5.2），全部在 container 內執行。timestamp 一律來自 `time.time_ns()` 或 service-internal API（Prometheus `activeAt`、receiver `received_unix_ns`、exporter gauge value）。

---

## 8. Open questions

### 8.1 Should the harness run in CI or local-only?

**Initial position：local-only**。理由：

- e2e harness cold start（compose up + healthcheck + 30 runs × 2 phases + teardown）≈ 5–8 min，在 GitHub Actions runner 是顯著 wall-clock 成本。
- 若 harness 在 CI flake，會把 PR pipeline 拖回到 Phase 1 之前的「unstable」感受。
- 客戶 baseline 要的是「跑 30 次取 P95 + CI」，不是「每 PR 跑 1 次」。

**Phase 3 升級條件**：當 implementation stable + customer fixture 確定後，可 gate 到 nightly job（不阻擋 PR），輸出寫到 `bench-results/` 由 release pipeline 採集。詳 §11。

### 8.2 Trigger metric injection — pushgateway 確認

§4.3 已採 actual vs threshold 雙 metric 模型，pushgateway 為注入點。其他選項（custom exporter、node_exporter）在新模型下優勢消失。

> 注意：pushgateway 不是 stale-state 友善，若 driver 忘記 cleanup 上一 run 的 push 值，會干擾下一 run。driver `finally:` 區塊必須對 pushgateway 做 `DELETE /metrics/job/e2e-driver/...`。

### 8.3 Webhook receiver authentication

- **Local docker-compose 環境**：不需要。docker bridge 隔離。
- **若未來移到共用 CI runner**：建議加 shared bearer token。

**Initial position：no auth**。在開發者文件記：harness 不可暴露於 host network。

### 8.4 Run baseline 5000-tenant fixture too?

是。§9 acceptance criteria 要 1000 + 5000 兩個 fixture size 各 30 runs，比對兩組 stage breakdown 確認 stage B 是否在 5000 scale 變 dominant。

### 8.5 P95 confidence interval 怎麼報？

n=30 對 P95 的 empirical estimator 方差仍大。aggregator 應跑 bootstrap（resample 1000 次取 percentile of percentile）報 95% CI。若 CI 寬度 > median 的 50%，視為 inconclusive，需更多 runs。

---

## 9. Acceptance criteria for follow-up implementation PR

當本 design doc land 後，implementation PR 應滿足：

### 9.1 Code 改動

- [ ] threshold-exporter 新增兩個 timestamp gauges（§2.6）
- [ ] `tests/e2e-bench/` 完整 scaffolding（§7.3 為 reference layout）
- [ ] receiver 採 ring buffer + query endpoint（§7.4），不只 `/last-post`
- [ ] driver 在 compose 內部跑（§4.1, §4.2），不在 host
- [ ] alert rule 採 actual vs threshold 雙 metric model（§4.3）
- [ ] alertmanager `send_resolved: true`（§4.4）

### 9.2 量測協定

- [ ] fixture pre-creation：30 個 placeholder tenant 檔案（§5.1）
- [ ] Run i 用獨立 `tenant_id=bench-run-{i}`（§4.3 Run isolation）
- [ ] 第 1 run 標 `warm_up: true` 不入聚合（§5.3）
- [ ] fire + resolve 對稱量測（§2.4, §5.2）
- [ ] n ≥ 30 runs；aggregator 報 P50/P95/P99 + 95% CI via bootstrap（§8.5）

### 9.3 Output 與 CI

- [ ] 每 run JSON schema 含 `fixture_kind` + `gate_status` + `warm_up`（§2.5）
- [ ] aggregator 渲染 §6.5 gate banner
- [ ] `make e2e-bench` Makefile target（內部呼叫 `run.sh`）
- [ ] 1000-tenant + 5000-tenant 各 30 runs 報告（§8.4）
- [ ] 所有 docker images pin 到具體版本（不 `latest`）

### 9.4 文件

- [ ] [`benchmark-playbook.md`](../benchmark-playbook.md) 加 section「Phase 2 alert fire-through baseline」，含 stage 直方圖 + gate banner
- [ ] CHANGELOG `[Unreleased]` entry：「v2.8.0 alert fire-through e2e baseline (calibration gate model)」
- [ ] 本 design doc 在 implementation 後升格為 benchmark-playbook 章節（§10），檔案 archive 至 `docs/internal/archive/design/`

### 9.5 品質閘門

- [ ] Pre-commit clean
- [ ] customer-anon 缺席時 fallback 到 synthetic-v2，輸出 banner 正確標 pending

---

## 10. Relation to existing infrastructure

| 既有項目 | 與本 harness 的關係 |
|---------|------------------|
| **A-8b unit test** (`TestScanDirHierarchical_K8sSymlinkLayout`，PR #54) | 已覆蓋 K8s ConfigMap-symlink 行為；e2e **不**重做。e2e 假設 scan 路徑正確，只測 fire-through latency |
| **A-15 `bench_wrapper.sh`** | 確立的 clean stdout 輸出 convention；e2e harness 的 `aggregate.py` 應遵循同一格式（最後一行為 single-line JSON summary） |
| **B-1 Phase 1**（PR #59） | 提供 `buildDirConfigHierarchical` synthetic fixture builder；`fixture/synthetic-v1/` 直接 reuse；synthetic-v2 在其上加 zipfian + power-law |
| **B-8 BlastRadius metric**（PR #59） | 提供 `affected-tenants` ReportMetric；e2e 可 cross-check：trigger 一個 defaults change 後，e2e 觀察到的 alert 數應接近 `affected-tenants` 計數 |
| **`docs/internal/benchmark-playbook.md`** | Phase 2 結果落腳處；implementation PR 後本 design doc 升格至 playbook 章節，原檔 archive |
| **`CLAUDE.md` § Pre-commit 品質閘門** | e2e harness 不進 pre-commit auto hooks（成本太高）；保留為 manual-stage 候選 |
| **production exporter** | §2.6 新增的 `last_scan_complete_unixtime_seconds` / `last_reload_complete_unixtime_seconds` gauges 在 production 也有 monitoring 價值（exporter stuck detection） |

---

## 11. Phase 2.5 / Phase 3 follow-up（out of scope）

實作完 Phase 2 後可立即看見的 follow-up（**不**列入本 PR）：

| Phase | 主題 | 描述 |
|-------|------|------|
| **2.5** | Failure-mode harness | 注入 exporter SIGTERM mid-reload / Prometheus scrape timeout / AM 暫停 30s / docker network 5% packet drop。每 case 量「time-to-recovery」。客戶 P99 tail behavior 主要由此類事件決定 |
| **2.5** | Blast-radius axis | 不只「1 tenant 變動 → 1 alert」，也測「defaults 變動 → 100 tenants 變動 → 100 alerts」，量 `T4_last - T4_first`（dispatch fan-out duration），補 Phase 1 B-8 的 e2e 對照 |
| **3** | Regression gate in release pipeline | 每 release candidate 跑 e2e，對比上一 release 的 P50/P95；若 regression > 20% 則 fail release。把 e2e 從一次性 baseline 升級為 ongoing sentinel |
| **3** | OpenTelemetry spans | exporter 開 OTel span `scan` / `reload`；driver 收 OTLP 直接拿 raw span data（無 quantization）。本 doc 的 timestamp gauges 是 Phase 2 解，OTel 是 Phase 3 解 |
| **3** | k3d 補充驗證（如客戶實際 K8s pattern 出 anomaly） | 僅當 Phase 2 + production telemetry 顯示 K8s-specific regression 時才接 |

---

## 相關資源

| 資源 | 說明 |
|------|------|
| [Benchmark Playbook](../benchmark-playbook.md) | Phase 1 1000-tenant baseline 方法論 + 量測踩坑；Phase 2 結果未來落腳處 |
| [Architecture & Design](../../architecture-and-design.md) | 9 個核心設計概念與 4 層路由 — 解釋 alert fire-through 在系統內的位置 |
| [Config-Driven 架構設計](../../design/config-driven.md) | `_defaults.yaml` 鏈與 hot-reload 行為；fixture calibration 的概念基礎 |
| [PR #59 — Phase 1 Hierarchical Baseline](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/59) | Phase 1 implementation 與本文件的 prerequisite |
| [PR #54 — A-8b K8s symlink unit test](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/54) | 已覆蓋 ConfigMap-symlink 行為，e2e 不重做的依據 |
| [Prometheus alerting rules docs](https://prometheus.io/docs/prometheus/latest/configuration/alerting_rules/) | alert-rules.yml 撰寫參考 |
| [Alertmanager configuration](https://prometheus.io/docs/alerting/latest/configuration/) | alertmanager.yml routing 設定參考 |
| [Doc Template Spec](../doc-template.md) | 本 design doc 遵循的 frontmatter 與結構規範 |
