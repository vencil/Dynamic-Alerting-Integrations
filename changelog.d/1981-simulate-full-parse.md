---
section: Changed
topic: exporter
issues: [1981]
created: 2026-09-28T11:15:50+00:00
---
- **`/simulate` 拒收 exporter 載入時會丟棄的內容（exporter；[#1981](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1981)）**：⚠️ API 行為變更：`POST /api/v1/tenants/simulate` 對這類 payload 從 200 改為 400。先前租戶檔與 chain 根層（L0）`_defaults.yaml` 只經輕量解碼，`defaults: {mysql_connections: abc}`、`max_metrics_per_tenant: abc` 仍回 200，但 commit 後 exporter 會把整份檔記進 `parse_failed`，同檔合法的 key 一起失效。現在兩者改用 exporter 自己的完整解析，`{error}` 點名 `tenant_yaml` 或 `defaults_chain_yaml[0]`，最多列 10 條錯誤並附總數。也包括原本會回 404（tenant 不在檔內）的請求：檔會被整份丟棄時，優先回 400。portal 的預覽只送一層，一律當 L0：把子目錄的 `_defaults.yaml`（例如 schedule 形式、或加了引號的數字）貼進「最外層 _defaults.yaml」欄位，現在會收到 400。L1 以下值的型別維持寬鬆（`"70"` 照常生效），與 exporter 相同。⚠️ `/effective` 在 L0 上仍寬鬆解析，這類輸入上兩者暫時不一致（[#2296](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2296)）。合法 payload 的回應不變。
