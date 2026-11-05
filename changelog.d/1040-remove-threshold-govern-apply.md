---
section: Removed
topic: tools
issues: [1040]
created: 2026-11-05T02:54:53+00:00
---
- **⚠️ Breaking：移除 threshold-govern 的寫入路徑（[ADR-027](docs/adr/027-tenant-api-caller-identity.md) §4；[#1040](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1040)）**：`da-tools threshold-govern` 只剩唯讀報告——跑推薦、過閘門（`--min-delta-pct`、信心等級），列出該調的腐敗閾值與要改的租戶檔，不呼叫 tenant-api、不開 PR。`--apply`、`--tenant-api-url`、`--identity-email`、`--identity-groups`、`--auth-token`、`--auth-token-file`、`--throttle-seconds`、`--max-prs`、`--timeout` 不再接受（結束碼 2）；`--json` 拿掉 `applied`、`outcomes` 與寫入計數。一併移除 `k8s/03-monitoring/cronjob-threshold-govern.yaml`、它的 NetworkPolicy 放行（raw egress 與 tenant-api chart 的 `networkPolicy.internalPortAllow.thresholdGovern`）、告警 `ThresholdGovernanceStale`，以及 tenant-api 的 `X-DA-Write-Source` header：單租戶 PR 一律標為 tenant-manager UI。
