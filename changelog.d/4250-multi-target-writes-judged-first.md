---
section: Fixed
topic: tools
issues: [4250]
created: 2026-11-26T19:10:00+00:00
---
- **一次改寫多個追蹤檔的產生器，寫第一個之前先確認每個目標都寫得進去（[#4250](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4250)）**：原本的預檢只擋非一般檔與標準串流，後面某個目標若是本行程不能寫的唯讀檔，或是要新建在不收新檔的目錄裡，會在前面的目標已改寫之後才以 rc 2 結束。`_lib_output` 新增 `check_writable_artifact`（已存在的檔與實際寫入用同一個 `os.access(W_OK)` 判斷；還不存在的檔看它要建在哪個目錄）與批次入口 `write_repo_artifacts`：先檢查整批、再寫第一個。只有寫的當下才遇得到的失敗（磁碟滿、配額、檢查之後權限才變）仍可能留下前面已寫的檔，這時錯誤會列出「already written, not rolled back」的檔名。本批遷移 `gen_tenant_id_json` 與 `generate_adr_index`；後者原本逐一「檢查→讀→寫」，後面的文件缺少 sentinel 時前面的已改寫，現在全部算好才寫。其餘多目標工具分批遷移。root 執行時 `os.access` 一律放行，量不到唯讀的情況。
