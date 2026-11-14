---
section: Removed
topic: da-tools
issues: [3873]
created: 2026-11-14T06:00:00+00:00
---
- **⚠️ breaking：validate-config 移除 `unread_keys` 列，共用鍵判決只在 `routes` 列（tools；[#3873](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3873)）**：`routes` 列就是 `generate-routes --validate`，它本來就報共用鍵判決（[ADR-038](docs/adr/038-unread-keys-judged-by-readers.md)），同一份報告因此判兩次，在 `expires`／時段邊界上兩列可能不一致。文字報告與 `--json` 都不再有 `unread_keys` 列，讀它的 CI 請改讀 `routes` 列（明細以 `key verdict:` 開頭的行）。該列的建議改由 `routes` 列給：判不了的閾值鍵（沒有根平台檔）、判決給不出來（da-guard）、平台什麼都沒宣告（⛔ 不要刪 key）各有專屬建議，同列另有路由問題時兩段建議都給。結束碼不變；`routes` 列沒判到鍵（樹先被拒收）時，`no_alert` 列自己判一次。
