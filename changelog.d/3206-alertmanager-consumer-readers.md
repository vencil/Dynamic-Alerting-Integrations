---
section: Changed
topic: confd-family
issues: [3206]
created: 2026-10-29T00:52:00+00:00
---
- **⚠️ Breaking：讀 Alertmanager 設定的工具改以 Alertmanager 自己的載入函式讀（[#3206](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3206)，[ADR-036](docs/adr/036-single-parser-effective-config.md) §12）**：`generate-routes --base-config` 經 `da-read alertmanager` 讀，須是 Alertmanager 收得下的完整設定（至少 `route:` 與它指名的 `receivers:`；`global:`、`inhibit_rules:` 仍可省略），缺 `route:` 不再補內建的、結束碼 2、訊息是載入函式的原話。`--output-configmap`、`--validate`、`--apply` 組出的 ConfigMap 經 `da-crdecode` 讀回，設定經 `da-read`；沒有 amtool 時載入函式拒收即結束碼 1、不寫不套用。`explain-route --trace`、`config-diff` 的路由面與 `byo-check` 的 inhibit 檢查都是 Alertmanager 的讀法：matcher 依它排序、`99s` 顯示 `1m39s`、只是寫法不同的時長不算變更、`.nan` 是字串。沒有 `da-read`／`da-crdecode` 時結束碼 2，不退回 PyYAML（da-tools 映像已內建；repo 內 `make da-read-build da-crdecode-build`）。
