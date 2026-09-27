---
section: Fixed
topic: tenant-api
issues: [2124]
created: 2026-09-27T11:49:22+08:00
---
- **`POST /tenants/{id}/validate` 的判定改由寫入路徑的同一支驗證產生（tenant-api；[#2124](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2124)）**：dry-run 過去手抄寫入路徑的部分檢查，對重複鍵、YAML 語法錯、第二份 YAML 文件、夾帶其他 tenant 區塊、以及 tenant 檔歧義或已被其他 conf.d 檔宣告等輸入回 `valid: true`，PUT 卻拒收。現在同一份輸入 dry-run 與 PUT 結論一致。⚠️ 行為變化：驗證遇到結構性錯誤即停，同時有多種錯誤時 `warnings` 只列第一個（過去可能同時列出 root key 與 key 錯誤）；會依現有 tenant 檔判定的檢查也出現在 dry-run。回應欄位不變。
