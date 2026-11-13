---
section: Added
topic: helm
issues: [1295]
created: 2026-11-06T01:40:00+00:00
---
- **⚠️ Breaking：VictoriaLogs 前面加上 vmauth，消費端一律帶帳密、預設強制（victorialogs chart 0.5.0，Secret 指名方式為 0.7.0；[#1295](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1295)）**：VictoriaLogs 只監聽 pod 內 loopback，Service 的 :9428 由 vmauth 接手。`auth.consumers` 是誰能連、能用哪些路徑的單一清單（Vector 只能寫入，其他只能查詢）。預設 `auth.mode: enforce`：沒帶帳密只剩 `/health`、`/ping`、`/metrics`，而且每個消費端都必須在 `auth.existingSecrets`（以消費端為鍵的 map）指名 Secret，否則拒絕 render 並列出缺的消費端；升級時若指名的 Secret 不存在或缺 `password` key，`helm upgrade` 也會拒絕（部署身分不能讀 Secret 時設 `auth.checkSecretsOnUpgrade: false`）。vector、chargeback-aggregator、federation-gateway、federation-reconciler 四個 chart 以 `victorialogs.auth.existingSecret` 帶上帳密（0.12.0／0.3.0／0.7.0／0.4.0），Grafana 的 datasource 改用 basic auth、密碼取自 Secret `victorialogs-grafana`。render 只檢查 store 這一側，消費端沒帶帳密會收到 401（Vector 會直接丟資料，新增 critical 告警 `VectorStoreSinkDroppingEvents`）。升級時先明寫 `auth.mode: observe`、接上並確認每個消費端再拿掉（`--reuse-values` 會沿用舊版的 observe）；步驟見 `docs/internal/platform-log-aggregation-runbook.md` §1.2。
