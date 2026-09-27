---
section: Fixed
topic: confd-family
issues: [2093]
created: 2026-09-27T07:35:11+08:00
---
- **`tenant-verify` 拒絕驗證被多個檔宣告的 tenant（dx；[#2093](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2093)）**：同一 tenant 出現在兩個以上的 conf.d 檔時，過去會依檔名排序靜默挑一份來算 hash——多餘檔排在前面時，rollback checklist 第 6 項會 exit 0 假通過。現在單一 tenant 模式一律 exit 2，JSON 帶 `error: "duplicate"` 與排序後的 `files`，human 輸出逐行列出 `declared in:`。⚠️ `--all` 契約改變：有重複宣告的 tenant 列成 error 條目（沒有 `merged_hash`）、其餘照常輸出，最後 exit 2；沒有重複時維持 exit 0。處置是刪除多餘宣告後重跑。
