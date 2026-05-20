---
title: "Platform Log Aggregation Runbook"
tags: [internal, runbook, observability, federation, logs]
audience: [platform-engineer, sre]
version: v2.8.1
lang: zh
---

# Platform Log Aggregation Runbook

#539 Phase 1。federation-gateway 把 audit log 用 JSON 寫到 stdout
（ADR-020 IV-2f）；本 pipeline 把它搬進中央 log store，讓「過去 6h 這
顆 token 碰過什麼」這種 incident query 有得問。

```
federation-gateway (stdout JSON + stderr operational)
    │
    ▼
node DaemonSet (Vector)   ── helm/vector chart
    │  VRL: parse_json on success → log_type=federation_audit
    │       parse_json on fail   → log_type=gateway_operational
    │
    ▼
VictoriaLogs (single pod) ── helm/victorialogs chart
    │
    ▼
Grafana datasource / LogsQL
```

**Hard rule**：producer 永遠不直接 HTTP push 到 log store。gateway →
stdout、delivery → shipper。log-store 掛掉時 federation gateway 不能
被連累。

## 1. Deploy 順序

```sh
# 1) Store first — Vector 啟動時會嘗試握手 sink。
helm install victorialogs ./helm/victorialogs -n monitoring \
  --set persistence.size=20Gi \
  --set retentionPeriod=30d

# 2) Shipper second。
helm install vector ./helm/vector -n monitoring \
  --set victorialogs.host=victorialogs.monitoring.svc

# 3) Grafana datasource：plugin 用 GF_INSTALL_PLUGINS 自動裝
#    （k8s/03-monitoring/deployment-grafana.yaml 已加好）；
#    datasource provisioning 已寫進 configmap-grafana.yaml。
kubectl rollout restart -n monitoring deploy/grafana
```

驗 datasource 載入完成：

```sh
kubectl logs -n monitoring deploy/grafana | grep -i 'victoriametrics-logs-datasource'
# 期待看到「plugin loaded」之類的訊息；CrashLoopBackOff 通常是
# Grafana 連不上 grafana.com 抓 plugin —— air-gapped cluster 必須
# pre-bake 一個含 plugin 的 image，把 GF_INSTALL_PLUGINS 拿掉。
```

## 2. Smoke-test LogsQL（#539 §4 AC5）

從 Grafana **Explore → VictoriaLogs** 跑這幾條。每條都應該有結果；
0 結果代表 pipeline 中間斷掉，往下看 §4 troubleshooting。

| 用途 | LogsQL |
|---|---|
| 「現在有 log 進來」 | `*` |
| 「每個 tenant 的 audit 量」 | `log_type:federation_audit \| stats by (tenant_id) count()` |
| 「過去 6h 5xx 是誰」 | `log_type:federation_audit AND status:~"5.."` （時間範圍：6h） |
| 「JWT 拒絕的攻擊掃描」 | `log_type:federation_audit AND status:401 \| stats by (token_id) count()` |
| 「Envoy 操作層異常」 | `log_type:gateway_operational AND ("error" OR "warning")` |

**注意**：JWT-fail request 在 audit JSON 裡 **沒有 tenant_id**（jwt_authn
在 claim injection 之前就拒了），所以 `stats by (tenant_id)` 不會把它
們算進去。要找它們改用 `token_id` 或 `path`。

從 CLI 跑同樣的 query：

```sh
kubectl exec -n monitoring deploy/victorialogs -- \
  wget -qO- 'http://localhost:9428/select/logsql/query?query=log_type:federation_audit&limit=10'
```

## 3. Stream-field schema（#539 §3 load-bearing table）

VictoriaLogs 把 fields 分兩類：**stream fields**（定義 logical stream，
基數爆炸會炸索引）vs **data fields**（per-line、可查、不入 stream
index）。本 pipeline 的選擇凍結在 `helm/vector/values.yaml` 的
`streamFields`：

| field | 類 | 為什麼 |
|---|---|---|
| `app` | stream | container_name，bounded |
| `k8s_namespace` | stream | bounded |
| `log_type` | stream | 2 值（federation_audit / gateway_operational） |
| `tenant_id` | stream | bounded（百量級 MVP） |
| `status` | stream | enum，~7 個 HTTP code |
| `pod_name` | **data** | HPA churn 會炸 stream index |
| `token_id` | data | 高基數、短命 |
| `query` (PromQL) | data | 每個 request 都不同 |
| `path` / `method` / `ts` / `duration_ms` | data | per-request payload |

**改 `streamFields` 是破壞性變更**：已 ingest 的資料的 stream 樹會
被切，需要 re-index。改之前先評估保留 vs 重灌。

## 4. Troubleshooting

### 4.1 「Grafana 查不到 log」

```sh
# a) Vector 在每個 node 都跑？
kubectl get pods -n monitoring -l app.kubernetes.io/name=vector -o wide

# b) Vector 有沒有看到 gateway 的 pod？
kubectl logs -n monitoring -l app.kubernetes.io/name=vector --tail=50 \
  | grep -i 'federation-gateway\|added file\|matched'

# c) Vector → VictoriaLogs 連得到嗎？
kubectl exec -n monitoring deploy/victorialogs -- \
  wget -qO- 'http://localhost:9428/metrics' | grep -E 'vl_(rows_ingested|free_disk)'
```

### 4.2 「VictoriaLogs pod 起不來」

```sh
# 99% 是 PVC 沒 bind。
kubectl get pvc -n monitoring victorialogs-data
kubectl describe pvc -n monitoring victorialogs-data
```

storageClass 不對 → `--set persistence.storageClass=<your-class>` 重裝。
測試環境可以 `--set persistence.enabled=false`（資料隨 pod 蒸發，
NOTES.txt 會印警告）。

### 4.3 「Vector pod 在 read 失敗」

`kubernetes_logs` 讀 `/var/log/pods/...` 需要能讀 root-owned file。
本 chart 預設 `runAsUser: 0` + `DAC_READ_SEARCH` cap（其他 cap 全掉）。
如果叢集有 PSP/PSA 把 root 擋掉，需要：

- 切到 `--set containerSecurityContext.runAsUser=472` 並讓 host 把
  `/var/log/pods` group-readable，或
- 換 image 到 `timberio/vector:0.55.0-distroless-static`（有 `nobody`
  user），同上需 host 端配合，或
- 在該 namespace 例外放行 root。

### 4.4 升 Vector 版本時的 VRL 編譯爆炸

Vector ≥ 0.55 把 `unnecessary error coalescing operation` 從 warning
升成編譯錯，pod 直接 crashloop（首跑 #539 smoke-test 中招）。VRL
的 infallible 運算（例如 `merge(object!, object!, deep:true)`、
`to_string(string)`）後面**不要**接 `??` fallback —— 編譯器會說
「this expression can't fail」。升版前先 `helm template vector
./helm/vector | yq '... | .data."vector.yaml"' | vector validate
--config-yaml /dev/stdin` 本地驗一次。

### 4.5 Vector `data_dir` vs `readOnlyRootFilesystem`

Vector 預設 `data_dir: /var/lib/vector`（容器 root 下）；本 chart 開
`containerSecurityContext.readOnlyRootFilesystem: true`，兩者直接撞
「Could not create subdirectory ... Read-only file system」。所以
configmap.yaml 顯式設 `data_dir: /vector-data-dir`，對齊 daemonset.yaml
hostPath 掛載點。**改掛載路徑時兩處要同步**，否則 pod 起不來。

## 5. Capacity / Retention

預設 `retentionPeriod=30d` + `persistence.size=10Gi`，依 federation
audit 預估流量（~50 RPS × 1KB/line × 30d ≈ 130GB）**不夠長期跑**——
應該按環境調整 size。Re-estimate 規則：

```
size ≈ (audit_RPS × 1 KB × retention_seconds × 1.3 overhead) / compression_ratio
```

VictoriaLogs 平均壓縮比 ~10x，所以實務上 130GB raw ≈ 13GB on disk。
保守起見 `size = 30 GiB` 給 30d。

#552 chargeback query log 進來時（consumer #2）流量會翻倍以上，
那時要重新估。

## 6. Phase 2 — chargeback（#552 consumer #2）

啟用步驟（在 Phase 1 已就位的前提下）：

```sh
# 1) Prometheus 開 query log（k8s/03-monitoring/configmap-prometheus.yaml
#    global section 已加 query_log_file: /dev/stderr）。
kubectl apply -f k8s/03-monitoring/configmap-prometheus.yaml
kubectl rollout restart -n monitoring deploy/prometheus

# 2) Vector 加 prometheus 為 additionalSource。
helm upgrade vector ./helm/vector -n monitoring --reuse-values \
  --set 'additionalSources[0].name=prometheus_query_log' \
  --set 'additionalSources[0].extraLabelSelector=app=prometheus'

# 3) 裝 chargeback-aggregator（daily CronJob）。
# ⚠️ 不要加 `--wait` —— PVC 是 WaitForFirstConsumer（local-path / 多數
#    雲端 storage class 預設行為），install 過程沒 consumer pod 觸發 →
#    PVC 永遠 Pending → `--wait` timeout。manualJob hook 是
#    post-install，要 install 完才會被建立去 bind PVC。
helm install chargeback ./helm/chargeback-aggregator -n monitoring
```

驗 query log 真的進去 store：

```sh
kubectl run vl-q --rm -i --restart=Never --image=busybox:1.36 --quiet -- \
  wget -qO- 'http://victorialogs.monitoring.svc:9428/select/logsql/query?query=log_type%3Aprometheus_query_log&limit=2'
# 期望看到 _stream={app="prometheus",k8s_namespace="monitoring",log_type="prometheus_query_log",...}
# 內含 params.query, samples_scanned, exec_time_s 等欄位（Prometheus 原生輸出 *Total*Time 單位為**秒**）
```

跑一次 manual aggregation 看 CSV：

```sh
helm upgrade chargeback ./helm/chargeback-aggregator -n monitoring \
  --reuse-values --set manualJob.enabled=true
kubectl wait -n monitoring --for=condition=complete job/chargeback-aggregator-manual --timeout=120s
kubectl logs -n monitoring job/chargeback-aggregator-manual
# 期望最後一行：OK: wrote /reports/chargeback-YYYY-MM-DD.csv (N tenants); pruned 0 files older than ...
```

讀今天的 CSV：

```sh
kubectl exec -n monitoring job/chargeback-aggregator-manual -- cat /reports/chargeback-$(date -u +%F).csv
# CSV columns: tenant_id,samples_scanned,exec_time_s,queries,window_hours,generated_at
# exec_time_s 單位是「秒」（float，如 0.000052 = 52μs），不是 ms。finance pipeline 算錢時別把它當 ms。
# 沒 tenant_id 的 row 會 bucket 成 tenant=platform（rule eval / alert eval，不可計費）
```

### 6.1 為什麼不能用 gateway access log 算 chargeback

`tenant_federation_requests_total{tenant,status}` 數的是「請求次數」不是「成本」。同一個租戶打一條重 query（掃 10 萬筆 series）跟一條輕 query（10 筆 series）對這個 metric 各 +1，無法區分 —— 計費會把輕重 query 收同樣的錢。

IV-2f（#511）刻意**沒**在 Envoy 加 `series_returned` 欄位 —— Envoy 要去 buffer + 解壓 + 解析 Prometheus response body 才能數 series，成本太高；blast-radius 強制執行交給 storage 的 `--query.max-samples` cap 已經夠了。

正確的 cost signal 在 Prometheus 自己的 `query_log_file`，每條 query 一筆 JSON 帶 `stats.samples.totalQueryableSamples` + `stats.timings.execTotalTime`，這是商用級帳單的真實來源。詳 #552。

### 6.2 Tenant attribution

Prometheus query log 不會原生帶 `tenant_id`。federation-proxy（IV-2a）在 query 進 Prometheus **之前**注入 `{tenant_id="X"}`，所以 logged query string 帶有 `tenant_id="X"` selector。VRL 用 regex 抽出來。

- federation 查詢 → query string 有 `tenant_id="X"` → row 的 `tenant_id` field 是 X
- 平台自身的 rule / alert eval → 沒有 tenant_id selector → row 沒有 tenant_id field → aggregator bucket 成 `tenant=platform`

`platform` row 反映平台自身負載（recording rule、alert eval），**不可**入客戶帳單。

### 6.3 Failure modes

| 症狀 | 原因 | 救法 |
|---|---|---|
| CSV 全是 `tenant=platform` row | federation-proxy 沒在線，所有查詢都是平台自己的 | 先確認 federation-gateway + federation-proxy 在跑 |
| `prometheus_query_log` stream 在 VictoriaLogs 是空 | Prometheus 沒重啟 / `query_log_file` 沒生效 | `kubectl logs -n monitoring deploy/prometheus | grep query_log` 看有沒有 query log 出 stderr |
| CronJob 一直跑失敗 | VictoriaLogs URL 改了 / network policy 擋 | `helm get values chargeback -n monitoring` 確認 URL；`kubectl logs job/chargeback-...` 看 Python stderr |
| CSV 重複出現舊日期 | retention 沒清乾淨 | 手動 `kubectl exec ... rm /reports/chargeback-OLD-DATE.csv`；或調 `output.retentionDays` |

## 7. 跟 Phase 3（compliance / SIEM fan-out）的關係

VictoriaLogs 跟 Loki 一樣**不是 tamper-evident / WORM**。如果客戶帶
strict compliance requirement 進來（legal hold, immutable retention），
不要把 VictoriaLogs 換掉 —— **在 Vector sink 加一條 fan-out 到 SIEM**
就好。pipeline 上游沒變（Phase 2 加的 prometheus_query_log stream 也
跟著一起 fan-out，不需要二次配線）。詳 #539 §4 Phase 3。

## Refs

- 源 issue：[#539](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/539)
- 產生 audit JSON 的 Envoy access_log：`helm/federation-gateway/files/envoy.yaml`
- ADR-020 §Audit log
- Consumer #2：[#552](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/552)
