---
section: Fixed
topic: ci
issues: [2909]
created: 2026-10-11T11:56:08+00:00
---
- **try-local nightly smoke 會在 recipe-preview 壞掉時轉紅（[#2909](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2909)）**：#2873 的映像起不來 6 週，nightly 一直是綠的。`try-local/smoke.sh` 新增第 5 項：`GET /healthz` 要回 200 且帶 promtool，再對 `/api/v1/tenants` 的第一個租戶送兩次 `POST /preview`，超過門檻要回 `firing`、低於門檻要回 `inactive`。workflow 的等待步驟改由 `scripts/ops/trylocal_wait_ready.sh` 執行：長駐服務沒有容器、不在 running、或重啟過就判失敗；`restart: "no"` 的一次性服務（從 compose 推導）要以 0 結束。原本只看 `.Health`，沒有 healthcheck、已退出或正在重啟的容器都會被放行。
