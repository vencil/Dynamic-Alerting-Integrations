---
section: Fixed
topic: confd-family
issues: [2179]
created: 2026-09-28T00:47:23+08:00
---
- **`da-guard` 不再對 exporter 會丟掉的檔回 0「safe to merge」；`offboard` 的 pre-check 失敗改回非 0（tools／exporter；[#2179](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2179)）**：exit 3 的檔案集合改為 exporter 自己載入時會整份丟掉的檔（與 exporter 同一判定），包括根目錄的 `_defaults.yaml`（過去不帶 `--cardinality-limit` 回 2、帶了回 0）、`_platform.yaml`、`_profiles.yaml`（過去回 0 且不點名）；與 `--cardinality-limit`、`--scope` 無關，定義見 [cli-reference §guard](docs/cli-reference.md#guard)。`da-guard -h`／`--help` 現在會印出用法。`da-tools offboard` 讀不開的設定檔會被點名並讓 pre-check 失敗；pre-check 失敗時不論有沒有 `--execute` 都回 1（過去不帶 `--execute` 回 0）。
