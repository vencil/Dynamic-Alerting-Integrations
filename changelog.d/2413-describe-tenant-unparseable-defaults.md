---
section: Fixed
topic: confd-family
issues: [2413]
created: 2026-09-29T22:05:00+08:00
---
- **`describe_tenant` 遇到無法解析的 `_defaults.yaml` 不再 traceback（da-tools；[#2413](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2413)）**：任一層（含子目錄）被選用的 `_defaults.yaml` 語法錯誤（例如 `defaults: [`）、不是 UTF-8、或含 `"\udfff"` 這類 surrogate 跳脫時，三種模式都印出 Traceback、以 rc 1 結束，且不指出是哪個檔。現在印出 `❌ <檔案路徑> does not parse: <YAML 錯誤與位置>`，以 rc 2（caller error）結束、stdout 不輸出。與租戶檔不同，壞掉的 defaults 層不會被略過——略過會讓其下所有租戶的有效設定缺一層卻回 rc 0。`--what-if` 檔無法解析時的錯誤訊息現在也帶上檔案路徑。正常的 `_defaults.yaml` 與壞租戶檔的既有行為不變。
