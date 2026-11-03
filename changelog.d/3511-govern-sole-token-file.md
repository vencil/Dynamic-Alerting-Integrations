---
section: Changed
topic: tools
issues: [3511]
created: 2026-11-03T15:42:53+00:00
---
- **⚠️ Breaking：threshold-govern 的 token 檔是唯一憑證時讀不到改為 rc 2（[#3511](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3511)）**：`--apply` 只帶 `--auth-token-file`（或 `$DA_GOVERN_TOKEN_FILE`）、沒有 `--identity-groups` 也沒有字面 token（oauth2-proxy 模式）時，token 檔不存在、無法存取或不是 UTF-8，以前只印 warning、不帶任何憑證繼續送出請求，要到 proxy 才被拒；現在在任何寫入前以 rc 2 結束，訊息點名旗標與路徑（`--json` reason `file_not_found`／`file_not_accessible`／`file_not_utf8`）。有 identity header 時 Bearer 只供稽核，仍維持 warning 後繼續。
