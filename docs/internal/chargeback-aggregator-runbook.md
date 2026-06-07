---
title: "Federation Chargeback Aggregator Runbook"
tags: [internal, runbook, observability, federation, chargeback]
audience: [platform-engineer, sre, finance]
version: v2.9.0
lang: zh
---

# Federation Chargeback Aggregator Runbook

#539 Phase 2 / #552 consumer #2。Prometheus query log → Vector demux → VictoriaLogs `log_type=prometheus_query_log` stream → daily CronJob 算 per-tenant cost CSV。

> **架構脈絡**：本 runbook 接續 [`platform-log-aggregation-runbook.md`](platform-log-aggregation-runbook.md) Phase 1 之後。先把那邊的 victorialogs + vector 起好，本 runbook 的步驟才有意義。

## 1. 為什麼不能用 gateway access log 算 chargeback

`tenant_federation_requests_total{tenant,status}` 數的是「請求次數」不是「成本」。同一個租戶打一條重 query（掃 10 萬筆 series）跟一條輕 query（10 筆 series）對這個 metric 各 +1，無法區分 —— 計費會把輕重 query 收同樣的錢。

IV-2f（#511）刻意**沒**在 Envoy 加 `series_returned` 欄位 —— Envoy 要去 buffer + 解壓 + 解析 Prometheus response body 才能數 series，成本太高；blast-radius 強制執行交給 storage 的 `--query.max-samples` cap 已經夠了。

正確的 cost signal 在 Prometheus 自己的 `query_log_file`，每條 query 一筆 JSON 帶 `stats.samples.totalQueryableSamples` + `stats.timings.execTotalTime`，這是商用級帳單的真實來源。詳 #552。

## 2. 啟用步驟

前提：Phase 1（helm/victorialogs + helm/vector）已就位。

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
# 內含 params.query, samples_scanned, exec_time_s 等欄位
# （Prometheus 原生輸出 *Total*Time 單位為「秒」）
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
# exec_time_s 單位是「秒」（float，如 0.000052 = 52μs），不是 ms。
# finance pipeline 算錢時別把它當 ms。
# 沒 tenant_id 的 row 會 bucket 成 tenant=platform（rule eval / alert eval，不可計費）
```

## 2.1 Tamper-evident verification（#566 T2-4）

每筆 CSV 旁邊都會寫一個 `.csv.sha256` sidecar（`sha256sum -c` 格式）+ append 一筆到 `manifest.jsonl`。Finance pipeline / audit verifier：

```sh
# 方法 1：cron job 本身印出 sha256 prefix（最快的健檢）
kubectl logs -n monitoring job/chargeback-aggregator-manual | grep sha256

# 方法 2：操作員端用 sha256sum -c 驗證單一 CSV（標準 GNU coreutils）
kubectl run vfy --rm -i --restart=Never --image=busybox:1.36 \
  --overrides='{"spec":{"containers":[{"name":"v","image":"busybox:1.36","workingDir":"/reports","command":["sh","-c","sha256sum -c chargeback-'$(date -u +%F)'.csv.sha256"],"volumeMounts":[{"name":"r","mountPath":"/reports"}]}],"volumes":[{"name":"r","persistentVolumeClaim":{"claimName":"chargeback-aggregator-reports"}}]}}' \
  -n monitoring
# 期望：chargeback-YYYY-MM-DD.csv: OK
# 若 FAILED → CSV 被改動過、跟 sidecar 對不上 → 對照 manifest.jsonl 看是否有
# 多筆 entry（重跑）或單筆 entry hash 跟 sidecar 不符（tamper）

# 方法 3：append-only manifest 看 30 天 hash 序列
kubectl exec -n monitoring deploy/<reader-pod> -- cat /reports/manifest.jsonl | jq .
# 每天一筆（重跑會多筆同 date）；hash 跟對應 .csv.sha256 必須一致
```

**重要**：這是 **tamper-evident**（有 operator 改動的訊號），**不是
tamper-proof**（operator with PVC write 仍可同時改 CSV + .sha256 +
manifest 三檔造假）。compliance-grade WORM 是 #566 X-2 / SIEM 端的
責任，見 [`platform-log-aggregation-runbook.md`](platform-log-aggregation-runbook.md) §7.3。

## 3. Tenant attribution

Prometheus query log 不會原生帶 `tenant_id`。federation-proxy（IV-2a）在 query 進 Prometheus **之前**注入 `{tenant="X"}`，所以 logged query string 帶有 `tenant="X"` selector。VRL 用 regex 抽出來。

- federation 查詢 → query string 有 `tenant="X"` → row 的 `.tenant_id` field 是 X
- 平台自身的 rule / alert eval → 帶 `ruleGroup` field → VRL 強制砍 `.tenant_id` → aggregator bucket 成 `tenant=platform`
- 平台自身的 ad-hoc query（沒 tenant selector 也沒 ruleGroup）→ regex 無 match → `.tenant_id` 缺 → bucket 成 `tenant=platform`

`platform` row 反映平台自身負載，**不可**入客戶帳單。

## 4. Failure modes

| 症狀 | 原因 | 救法 |
|---|---|---|
| CSV 全是 `tenant=platform` row | federation-proxy 沒在線，所有查詢都是平台自己的 | 先確認 federation-gateway + federation-proxy 在跑 |
| `prometheus_query_log` stream 在 VictoriaLogs 是空 | Prometheus 沒重啟 / `query_log_file` 沒生效 | `kubectl logs -n monitoring deploy/prometheus \| grep query_log` 看有沒有 query log 出 stderr |
| CronJob 一直跑失敗 | VictoriaLogs URL 改了 / network policy 擋 | `helm get values chargeback -n monitoring` 確認 URL；`kubectl logs job/chargeback-...` 看 Python stderr |
| CSV 重複出現舊日期 | retention 沒清乾淨 | 手動 `kubectl exec ... rm /reports/chargeback-OLD-DATE.csv`；或調 `output.retentionDays` |
| 升 chart 版本後 `exec_time_s` 一陣子是 NaN | 24h window 內混合舊 `exec_time_ms` 跟新 `exec_time_s` row | aggregator 內建 dual-field coalesce，等舊 row 隨 VictoriaLogs retention rolls 過後自然消失（通常 24h） |

## 5. Capacity / scaling

預設 daily 02:00 UTC，24h window，per-tenant aggregate。流量上去（>100 tenant、>10k req/s）時：

- `WINDOW_HOURS` 可降到 1h 跑 hourly aggregate（finance 端要對得起來）
- `output.retentionDays=90` 預設，financial dispute window 通常 30-90d；可調
- PVC 預設 `output.pvcSize=5Gi`，~100 byte/row × 平均日均 row 數 × retention days 估

## Refs

- 源 issue：[#539 Phase 2](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/539)
- Scoping ticket：[#552](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/552)
- 上游 pipeline runbook：[`platform-log-aggregation-runbook.md`](platform-log-aggregation-runbook.md)
- Chart：`helm/chargeback-aggregator/README.md`（repo 內路徑、不入 mkdocs 站台）
