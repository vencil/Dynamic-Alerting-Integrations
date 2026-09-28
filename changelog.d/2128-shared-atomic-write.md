---
section: Fixed
topic: dx
issues: [2128]
created: 2026-09-28T12:10:12+00:00
---
- **共用的 `_atomic_write` 不再弄壞 symlink、hard link 與使用者的 `.tmp`（dx；[#2128](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2128)）**：`generate_doc_map`／`generate_tool_map`（`--safe`）、`generate_rule_pack_stats`、`generate_byo_rulepack_table`、`generate_adr_index`、`generate_planning_index`、`audit_rules_drift` 與 `generate_tenant_metadata` 改用同一份 #2082 的寫法。symlink 會寫進它指向的目標、本身保留（刻意不同於 renameio／python-atomicwrites）；hard link 每個名字都會更新；使用者的 `<目標>.tmp` 不再被刪。行為變更：目錄不可寫但檔案可寫時，從 rc 1 加 traceback 改為 rc 0，印 WARN 後就地寫入（失去原子性）；別人擁有的檔案寫入成功但無法 chmod 時只印 WARN；唯讀（0444）檔案從「經由可寫目錄被取代、rc 0」改為 rc 2 拒絕，以尊重檔案本身的唯讀權限；指向不存在目錄的 dangling symlink 從「整個被換成一般檔」改為 rc 2 報錯、symlink 保留。寫入失敗一律 rc 2，只印一行指名目標檔；路徑來自 `--out`／`--target` 時點出該 flag。
