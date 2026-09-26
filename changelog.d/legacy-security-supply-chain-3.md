---
section: Security
topic: security-supply-chain
issues: [903, 925, 926, 1018, 1364, 1538, 1582, 1583]
created: 2026-09-26T17:00:00+00:00
---
- **平台安全硬化：漏洞回報入口、PSS、跨租戶 RBAC 基線與注入修補（docs、k8s、helm、tools、portal）**：新增 `SECURITY.md`（GitHub private vulnerability reporting 入口、首次回應 7 天內、修補先進 release 再發 advisory）。四個 app-tier namespace 掛 Pod Security Standards `restricted` 的 warn+audit（尚未 enforce）；⚠️ Vector log agent 搬進專屬 privileged `vector` namespace，需依 runbook 先拆後裝。新增跨租戶告警 ConfigMap 的 operator RBAC 建議基線、驗證腳本 `verify_operator_rbac.sh` 與 audit-policy 指引。修補 GitHub Pages 工具頁 jsx-loader 的反射式 DOM XSS（`?component=`／`?flow=`／`?lang=`）；工具的 plain-text 輸出改跳脫不受信任的檔名與租戶名，防止偽造輸出行；workflow `run:` 改用 `env:` 綁定 GitHub context；PR 的 secret scan 只掃該 PR 分支並排除誤報的 Lob detector。詳 [#1018](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1018)、[#926](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/926)、[#1538](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1538)。
