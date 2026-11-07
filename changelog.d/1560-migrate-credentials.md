---
section: Added
topic: tools
issues: [1560]
created: 2026-11-07T03:19:19+00:00
---
- **`da-tools migrate-credentials`：找出 conf.d 明文寫的 receiver 憑證並遷移成 Secret 參照，只報告不擋（[#1560](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1560)）**：依 owner 裁決的明文政策逐檔判定，把租戶 `_routing` 的明文改寫成 `{secret: {key}}`（預設印 diff；`--write` 經 `da-guard write` 寫回，須同時給 `--manifests-out`），為每個租戶寫 `da-<tenant>` Secret（0600、O_NOFOLLOW，在 git worktree 內、symlink、hard link 一律拒絕），並列出待輪替清單（不含值）。憑證值不會出現在任何輸出：含值的 diff 整檔不顯示；改寫後檔案仍含被遷出的值（anchor、merge key、註解）就整檔不改寫。平台位置、無人讀的位置、userinfo、代理憑證、不合法 tenant id 列給人決定。改寫後仍會被產生器擋下時 `--write` 什麼都不寫。沒讀的檔另列，結束碼至少 1。結束碼 0／1／2。下一步產生器會以同一判定改為拒收。
