---
section: Fixed
topic: confd-family
issues: [2588]
created: 2026-10-01T22:12:19+00:00
---
- **`da-guard` 遇到讀不到的設定檔改為 exit 3 並點名（exporter；[#2588](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2588)）**：exporter 載入時 stat 或讀取失敗的檔（例如權限不足、懸空 symlink）與列不出內容的子目錄，過去被 Dangling Defaults Guard 靜默略過：其中的租戶不受檢查、讀不到的 `_defaults.yaml` 的值不算進有效設定，仍可 exit 0；`--scope` 下只剩讀不到的檔時還會報「vacuously safe」。現在與 `--scope` 有關者（檔案的判定規則同 `parse_failed`；列不出的目錄則在它就是 scope、位於 scope 之下或包含 scope 時才算）以 exit 3 結束，報告另列「Files the exporter cannot read」，JSON 報告帶 `unreadable`（與 `served-values` 同一份 `{file, reason}`）；指向目錄的 symlink 照舊只跳過。`--config-dir` 本身列不出內容時改為 exit 2（過去 exit 0），與 `served-values`、`effective` 一致。`--scope` 的說明改正：相對路徑以目前工作目錄解析（與 `--config-dir` 相同），不是相對於 `--config-dir`；行為不變。
