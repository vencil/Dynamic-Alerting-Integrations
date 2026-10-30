---
section: Added
topic: da-tools
issues: [3016]
created: 2026-10-28T23:51:55+00:00
---
- **`generate-routes --validate` 報出沒有讀取端讀的鍵（da-tools；ADR-038 決定 5）**：與 validate-config `unread_keys` 列同一份共用判決、同樣的行，每行以 `key verdict:` 開頭印在 stderr；`--findings-json` 新增 kind `key_unread`、`key_disable_noop`、`key_disable_partial`、`key_undecidable`、`key_verdict_incomplete`，`blocks` 一律 `never`。**只報不擋**：結束碼不變，da-guard 失敗只有一行 NOTICE，被拒收的樹不判。只跑 `generate-routes --validate` 的 CI（例如沒有另跑 validate-config 的 pipeline）從此也看得到拼錯的鍵。產生器容器不在產生器裡再判一次：它在產生器放行之後、於同一次判定的期限內判。
