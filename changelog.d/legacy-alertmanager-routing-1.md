---
section: Fixed
topic: alertmanager-routing
issues: [1423, 1460, 1556, 1616, 1617, 1641, 1649, 1650, 1651]
created: 2026-09-26T17:00:00+00:00
---
- **BREAKING — `generate-routes` 的旗標誤用與不可用輸入改為大聲失敗（tools、ops）**：`--base-config` 給了但不可用（路徑錯、空字串、非 mapping）時，先前會靜默改用平台內建佔位值、客戶的 SMTP／Slack 設定消失而流水線仍綠，現在 exit 2；空字串的旗標值在 `generate-routes`、`validate-config`、`lint` 一律視為「有給」。在不讀它的模式下使用 `-o`／`--dry-run`／`--namespace`／`--configmap`／`--yes`／`--base-config` 也是 exit 2（顯式傳入預設值也算）；租戶檔解析失敗不再印 `OK`，改 rc 1 並拒絕 render 與 apply；kubectl 失敗依文件回 2（[#1616](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1616)、[#1650](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1650)）。⚠️ 正確處置是修路徑而非拿掉旗標；CI／cron 裡的 `--apply` 請加 `--yes`。`da-tools init` 範本一併拿掉無效的 `-o … --validate` 組合。
