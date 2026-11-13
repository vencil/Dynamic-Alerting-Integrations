---
section: Changed
topic: log-aggregation
issues: [1295]
created: 2026-11-12T23:59:00+00:00
---
- **⚠️ Breaking：VictoriaLogs 的 NetworkPolicy 改由 `auth.consumers` 渲染（victorialogs chart 0.6.0；[#1295](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1295)）**：每個 consumer 以 `from`（`namespace`、`podLabels` 或兩者並用）宣告網路身分，store 的 ingress 就由這份清單產生，誰能連線與能做什麼不再分寫兩處；Prometheus 不是 consumer，改由 `networkPolicy.scrapers`（預設 `app: prometheus`）放行 scrape。`networkPolicy.allowedPodSelectors`／`allowedNamespaces` 已移除，仍設定就拒絕 render；`helm upgrade --reuse-values` 會帶著它們，改用 `--reset-then-reuse-values`。values 檔裡若整份重寫了 `auth.consumers`（enforce 模式要指名 Secret 時通常如此），每個 consumer 都要補上 `from`，可從 chart 的 values.yaml 照抄。Vector 改為「`vector` namespace 且帶 `app.kubernetes.io/name: vector`」，同 namespace 的舊 Vector 列（#1018 之前的安裝）移除。
