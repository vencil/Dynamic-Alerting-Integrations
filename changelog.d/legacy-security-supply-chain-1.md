---
section: Security
topic: security-supply-chain
issues: [902, 1058, 1243, 1337, 1933]
created: 2026-09-26T17:00:00+00:00
---
- **映像供應鏈閉環：digest pin、自動 bump 與每晚 CVE 掃描（ci、helm、k8s）**：chart／manifest 拉取的 14 個第三方映像全部改為 `@sha256:` digest pin，由 self-hosted Renovate 每週重解 digest 並 bump tag；CI 以 `curl` 下載的工具 binary 先驗 SHA-256，lint 的 docker fallback 也改 digest pin。新增每晚對 `main` 的 Trivy 掃描，分自建、第三方、以及 `da-tools init` 寫進客戶 repo 的映像三桶，各自開 deduped tracking issue，只在出現新 finding 或覆蓋率倒退時通知；PR 期另檢查部署與交付映像 ref 在 registry 解析得到。先前不在任何掃描矩陣的兩顆自建映像（`federation-audit-sidecar`、`vector-projection-gate`）已納入，並由檢查確保每個 Dockerfile 都被掃。Trivy 豁免條目必須帶到期日與 justification。詳 [#902](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/902)、[#1337](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1337)、[#1933](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1933)。
