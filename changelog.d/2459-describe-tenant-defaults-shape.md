---
section: Fixed
topic: confd-family
issues: [2459, 2097]
created: 2026-09-30T22:20:00+08:00
---
- **`describe_tenant`／`tenant-verify` 對形狀不對的 `_defaults.yaml` 不再 traceback，並與 exporter 的讀法對齊（da-tools；[#2459](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2459)）**：`defaults:` 不是 mapping（例如 `defaults: [1, 2]`）或整份文件不是 mapping（list、純量）時原本 traceback，現在指名檔案、說明形狀不支援，以 rc 2 拒收。值位置明寫的 `!!bool` 只接受 yaml.v3 的 `true`／`True`／`TRUE`（與對應的 false），`!!binary` 改用嚴格 base64 解碼：`!!bool yes`、`!!binary "%%%"` 這類 exporter 不載入的檔不再照常給出答案；租戶檔同樣適用（該檔被略過並在 stderr 指名，其中的租戶回 rc 2）。多文件的 `_defaults.yaml` 與 `--what-if` 檔改為只讀第一份文件，與 exporter 相同（[#2097](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2097) 第 3 點的殘餘）。`tenant-verify` 遇到上述 `_defaults.yaml` 時改為指名檔案、以 exit 1 結束。
