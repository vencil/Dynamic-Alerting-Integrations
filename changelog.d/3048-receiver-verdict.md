---
section: Changed
topic: da-tools
issues: [3048]
created: 2026-10-16T09:06:05+00:00
---
- **`test-notification` 的 receiver 合格判定改採路由產生器的判定，並檢查 port 範圍（[#3048](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3048)）**：先前工具自己檢查必填欄位與 URL，路由產生器拒收、根本不會進 Alertmanager 的 receiver 仍被拿去測或在 `--dry-run` 報 `DRY_RUN`。現在 receiver 本身的設定被產生器（`build_receiver_config`）拒收的不送出：URL 欄位（email 為 `smarthost`）有給但格式不合標 `INVALID_URL`，其他原因（例如 URL 欄位缺鍵、空字串或 null，email `to: [""]`、`send_resolved: "maybe"`、`http_config` 寫成字串）標 `INVALID_CONFIG`。目前不涵蓋產生器在路由層的略過（override 缺 `alertname`／`metric_group`、`routes[i]` 有不支援的 key，以及主 receiver 被拒時該租戶的 `overrides`／`routes` 整組不產出），這些 receiver 照常測試，另開 [#3076](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3076) 追蹤。port 須在 1–65535（含 email `smarthost`），先前 `:99999` 會被系統折算成別的 port 送出。送出時主機名無法編碼（IDNA，例如 label 超過 63 字元）標 `INVALID_URL`；`--dry-run` 不檢查這一項。結束碼變化：上述 receiver 在 `--ci` 下由 `0` 變 `1`；`http://[::1/`、`http://[1:2]/`、無法編碼的主機名先前 traceback 退出 `1`，現在無 `--ci` 時為 `0`。
