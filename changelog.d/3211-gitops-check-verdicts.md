---
section: Changed
topic: da-tools
issues: [3211, 3241]
created: 2026-10-21T16:20:00+00:00
---
- **⚠️ 結束碼變更：`gitops-check` 的三處判定改照實際狀態（[#3211](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3211)）**：`repo --branch main` 先前會比到 `refs/heads/feature/main` 這類以 `/main` 結尾的分支並通過，現在照 git 的解析比對（`heads/x`、`refs/heads/x` 都指 `refs/heads/x`），分支不存在時 fail（rc 0 → 1）；`--branch refs/heads/x` 先前 fail，現在通過（rc 1 → 0）。`repo --path` 由伺服器明確回答路徑不在該分支時，由「仍通過、`config_path_verified: false`」改為 fail（rc 0 → 1）；沒給 `--path` 時檢查的是預設的 `configs/`，倉庫沒有這個目錄也會 fail；伺服器不支援 `git archive --remote` 時照舊通過並附說明。`local --dir` 指到的不是目錄時，由 fail rc 1 改為呼叫端錯誤 rc 2（`--json` 時印 `reason: dir_not_found` 的 envelope）；上層目錄進不去、判斷不了是不是目錄時，由 `unexpected internal error (PermissionError)` 改為具名的呼叫端錯誤 `reason: dir_not_accessible`（[#3241](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3241)，rc 2 不變）。
