---
section: Fixed
topic: tenant-api
issues: [2124]
created: 2026-09-27T11:49:22+08:00
---
- **`POST /tenants/{id}/validate` 改用 PUT 在同一寫入模式下的驗證函式（tenant-api；[#2124](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2124)）**：dry-run 過去手抄部分檢查，對重複鍵、YAML 語法錯、第二份 YAML 文件、夾帶其他 tenant 區塊、tenant 檔歧義或已被其他 conf.d 檔宣告等輸入回 `valid: true`，PUT 卻拒收。現在 direct 模式跑寫入前的完整驗證；PR 模式只跑 PR 建立前的 body 驗證，依 base 樹判定的檢查仍在建 PR 時才做。授權與 domain policy 不在 dry-run 內。⚠️ 行為變化：遇到結構性錯誤即停，`warnings` 只列第一個；錯誤訊息不再帶伺服器檔案路徑。回應欄位不變。
