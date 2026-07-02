---
title: "Log aggregation smoke-test fixtures (#539)"
tags: [internal, examples, observability]
audience: [platform-engineer]
version: v2.9.1
lang: zh
---

# Log aggregation smoke-test fixtures

#539 三 phase 各做 runtime smoke-test 時用的 fixture。**不**部署到生產 cluster。

> 完整 runtime 驗證步驟見 [`docs/internal/platform-log-aggregation-runbook.md`](../../platform-log-aggregation-runbook.md) §6 + §7；本目錄只放 fixture 本體。

## 內容

| 檔案 | 用途 |
|---|---|
| `mock-siem.yaml` | Phase 3 fan-out smoke-test 的「假 SIEM」—— 一個 stdlib Python HTTP server，把 Vector POST 過來的 ndjson 解析後印一行到 stdout。`kubectl logs -l app.kubernetes.io/name=mock-siem` 看是否收到 |
| `vector-phase3-values.yaml` | helm/vector 用的 values overlay，把 source 鎖到 prometheus pod，配 mock-siem 為 `additionalSinks[0]`（測 fan-out + §2 back-pressure isolation） |

## 跑法（再現紅隊 T2-2 + Phase 3 fan-out）

```sh
# 1) 確保 Phase 1+2 已就位（helm/victorialogs + helm/vector + helm/chargeback-aggregator）
helm list -n monitoring | grep -E "victorialogs|vector|chargeback"

# 2) 啟 mock-siem
kubectl apply -f docs/internal/examples/log-aggregation-smoketest/mock-siem.yaml
kubectl wait -n monitoring --for=condition=ready pod -l app.kubernetes.io/name=mock-siem --timeout=60s

# 3) Vector 加上 fan-out 指向 mock-siem
helm upgrade vector ./helm/vector -n monitoring \
  -f docs/internal/examples/log-aggregation-smoketest/vector-phase3-values.yaml

# 4) 驗 fan-out 兩邊都到貨
kubectl logs -n monitoring -l app.kubernetes.io/name=mock-siem --tail=5 \
  | grep SIEM_RECV   # 期望看到 log_type=prometheus_query_log tenant_id=... msg_len=...

# 5) 驗 §2 back-pressure isolation —— SIEM down，VictoriaLogs 不能受影響
kubectl scale -n monitoring deploy/mock-siem --replicas=0
sleep 30
kubectl get pod -n monitoring -l app.kubernetes.io/name=vector    # 應該 Running, RESTARTS=0
# VictoriaLogs row count 應持續成長：
kubectl run vlq --rm -i --restart=Never --image=busybox:1.36 -n monitoring \
  --labels='app.kubernetes.io/name=vector' \
  -- wget -qO- 'http://victorialogs.monitoring.svc:9428/select/logsql/query?query=%2A+%7C+stats+count%28%29+as+n&limit=1'

# 6) 清乾淨
kubectl scale -n monitoring deploy/mock-siem --replicas=1
kubectl delete -f docs/internal/examples/log-aggregation-smoketest/mock-siem.yaml
helm upgrade vector ./helm/vector -n monitoring --reuse-values \
  --set 'additionalSinks=null'   # 移掉 fan-out，回 Phase 1 + 2 設定
```

## 為什麼 mock-siem 用 Python stdlib

- **零 dependency**：直接 `python:3.13-slim` + 一個 inline command，不用維護自家 image
- **可讀**：30 行 Python 比 100 行 syslog-ng config 容易理解
- **故意 unauthenticated**：純粹 smoke-test，**絕不**部署到 production —— 真實 SIEM 走 mTLS / token
