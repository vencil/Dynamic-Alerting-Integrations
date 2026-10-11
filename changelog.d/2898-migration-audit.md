---
section: Added
topic: da-tools
issues: [2898]
created: 2026-10-11T16:40:02+00:00
---
- **`da-tools migration-audit`：切換到單一解析器前的遷移稽核（ADR-036 §8；[#2898](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2898)）**：同一棵 conf.d 以今天產生器的 PyYAML 讀法與 `da-guard parse-tree` 各讀一次，只列差異、不做判定：讀法不同的值（檔案、行號、兩邊讀法、建議寫法，例如 `on` 要布林就寫 `true`）、只有一邊有的鍵、會變成讀不了的檔（附 `kind`）、過去拒收之後讀得到的檔（附 yaml.v3 讀成的內容）、兩邊選檔的差異，以及刪除 vendored yaml.v3「非特定 tag」patch 後 exporter 讀法改變的兩處（`! "<<"`、SLO `objective: ! "99.9"`）。結束碼 0 無差異、1 有差異、2 呼叫錯誤；`--json` 的 schema 為 `da-tools.migration-audit/v1`。需要 da-guard（da-tools 映像已內建），找不到時失敗並說明，不會自己解析。
