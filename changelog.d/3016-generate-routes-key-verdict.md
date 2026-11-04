---
section: Added
topic: da-tools
issues: [3016]
created: 2026-10-28T23:51:55+00:00
---
- **`generate-routes --validate` 擋下沒有讀取端讀的鍵（da-tools；ADR-038 決定 5）**：與 validate-config `unread_keys` 列同一份共用判決、同樣的行，每行以 `key verdict:` 開頭印在 stderr；`--findings-json` 新增 kind `key_unread`、`key_disable_noop`、`key_disable_partial`、`key_undecidable`、`key_verdict_incomplete`。error 等級的 finding、判不了的閾值鍵與判決失敗都是 `blocks: validate`，讓 `--validate` 結束碼 1；warning 與 notice 只報。只跑 `generate-routes --validate` 的 CI 從此也擋得到拼錯的鍵。產生器容器不在產生器裡再判一次：它在產生器放行之後、於同一次判定的期限內判。
