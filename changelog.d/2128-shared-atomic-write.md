---
section: Fixed
topic: dx
issues: [2128]
created: 2026-09-28T12:10:12+00:00
---
- **共用的 `_atomic_write` 不再弄壞 symlink、hard link 與使用者的 `.tmp`（dx；[#2128](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2128)）**：`generate_doc_map`／`generate_tool_map`（`--safe`）、`generate_rule_pack_stats`、`generate_byo_rulepack_table`、`generate_adr_index`、`generate_planning_index`、`audit_rules_drift` 共用的原子寫入改用 #2082 的做法，`generate_tenant_metadata` 也改呼叫同一份實作。三種退化已修：symlink 輸出會寫進它指向的目標、symlink 本身保留（刻意不同於 renameio／python-atomicwrites 直接換掉 symlink 的預設）；hard link 的每個名字都會更新（就地寫入並印 WARN）；使用者自己的 `<目標>.tmp` 不再被刪。行為變更：目錄不可寫但檔案可寫時，原本是 rc 1 加 traceback，現在改為 rc 0，在 stderr 印 WARN 後就地寫入，這一次寫入沒有原子性。寫入失敗一律 rc 2，只印一行指名目標檔（不再指名暫存檔）；路徑來自 `--out`／`--target` 時會點出該 flag。
