---
section: Fixed
topic: confd-family
issues: [2459, 2097]
created: 2026-09-30T22:20:00+08:00
---
- **`describe_tenant`／`tenant-verify` 對可解析但形狀不對的 `_defaults.yaml` 改與 exporter 同判（da-tools；[#2459](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2459)）**：`defaults:` 不是 mapping（例如 `defaults: [1, 2]`）時原本 traceback，現在指名檔案、以 rc 2 結束；整份文件不是 mapping 時該檔不提供任何 defaults，並在 stderr 指名。明寫 `!!bool` 只接受 yaml.v3 的 `true`／`True`／`TRUE`（與對應的 false），`!!binary` 改用嚴格 base64 解碼，`!!bool yes`、`!!binary "%%%"` 這類 exporter 不載入的檔不再照常給出答案。多文件的 `_defaults.yaml` 與 `--what-if` 檔改為只讀第一份文件，與 exporter 相同（[#2097](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2097) 第 3 點的殘餘）。`tenant-verify` 遇到無法解析的 `_defaults.yaml` 不再 traceback，改為指名檔案、以 exit 1 結束。
