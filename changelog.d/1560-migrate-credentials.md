---
section: Added
topic: tools
issues: [1560]
created: 2026-11-07T03:19:19+00:00
---
- **`da-tools migrate-credentials`：找出 conf.d 明文寫的 receiver 憑證並遷移成 Secret 參照，只報告不擋（[#1560](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1560)）**：依 owner 裁決的明文政策逐檔判定（只收參照的欄位、webhook `url` 帶 userinfo、四處代理憑證；email `headers` 等允許明文），把租戶 `_routing` 裡的明文改寫成 `{secret: {key}}`（預設印 diff、值一律遮罩；`--write` 經 `da-guard write` 寫回，須同時給 `--manifests-out`），`--manifests-out` 為每個租戶寫 `da-<tenant>` Secret（建檔即 0600，目的地在 git worktree 內就拒絕），並列出待輪替清單（檔名、行號、欄位、租戶，不含值）。平台位置、userinfo、代理憑證列給人決定；改寫後仍會被綁定判定擋下的參照另列，不算完成。結束碼 0 無明文／1 有發現／2 錯誤。目前沒有任何工具因明文而拒收；下一步產生器會以同一判定改為拒收。
