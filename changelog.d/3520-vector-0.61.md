---
section: Security
topic: security-supply-chain
issues: [3520]
created: 2026-11-01T23:23:20+00:00
---
- **Vector 升到 0.61.0，清掉 libssl3 CVE-2026-84782（[#3520](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3520)）**：`helm/vector`（chart 0.10.3）與 `helm/federation-gateway` 的 audit-metrics sidecar（chart 0.6.2）都改用 `timberio/vector:0.61.0-distroless-libc`，CI 與 dev container 的 Vector CLI 同步升級。0.58–0.60 仍帶這個漏洞，只有 0.61 修掉。0.58–0.61 移除的元件與選項（`http` source 別名、`logdna`、`azure_monitor_logs` 等），chart 都沒有用到。⚠️ 自行在 `additionalSinks` 加 sink 的部署要注意：0.58 起 Vector 不再輸出 `buffer_events`／`buffer_byte_size` 這兩個 gauge（改用 `buffer_size_events`／`buffer_size_bytes`），scheme 省略的 sink endpoint 預設改為 `https`。
