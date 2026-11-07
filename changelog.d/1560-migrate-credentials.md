---
section: Added
topic: tools
issues: [1560]
created: 2026-11-07T03:19:19+00:00
---
- **`da-tools migrate-credentials`：找出 conf.d 明文寫的 receiver 憑證並遷移成 Secret 參照，只報告不擋（[#1560](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1560)）**：依 owner 裁決的明文政策逐檔判定，把租戶 `_routing` 的明文改寫成 `{secret: {key}}`（預設印結構化的改寫計畫，不印任何取自檔案文字的內容；`--write` 經 `da-guard write` 寫回，之後以 `git diff` 檢視），`--manifests-out` 搭配 `--write` 為每個租戶寫 `da-<tenant>` Secret（0600、O_NOFOLLOW，git worktree 內、symlink、hard link 一律拒絕），與 conf.d 一起成敗，並列出待輪替清單（不含值）。工具只保證被改寫的憑證欄位本身不再是明文（改寫後與寫入後都讀回重判）；檔內其他副本、別的檔與 git 歷史裡的舊值不保證找得到，輪替清單上的每個值都必須輪替。平台位置、無人讀的位置、userinfo、代理憑證、不合法 tenant id 列給人決定。改寫後仍會被產生器擋下時 `--write` 什麼都不寫。沒讀的檔另列，結束碼至少 1。結束碼 0／1／2。下一步產生器會以同一判定改為拒收。
