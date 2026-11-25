---
section: Fixed
topic: ci
issues: [3981]
created: 2026-11-25T09:59:17+00:00
---
- **共用寫入入口 `write_repo_artifact` 收 bytes，三支以 bytes 改寫檔案的工具改經它（[#3981](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3981)）**：bytes 照原樣寫出（不轉編碼、不轉換行，CRLF 與非 UTF-8 位元組都保留）。`fix_file_hygiene`（pre-commit hook）、`check_head_blob_hygiene --fix`、`bump_playbook_versions` 改為所有檔都算完、判斷過才開始寫，寫入改經入口：原子取代，hard link、別人的檔、目錄不可寫時改為就地寫入並在 stderr 多一行 WARN。兩支 hygiene 工具保留檔案原本的權限；`bump_playbook_versions` 照 checkout（`0644`，可執行檔 `0755`）。寫入被拒或失敗時，原本 `fix_file_hygiene` 與 `bump_playbook_versions` 是 `unexpected internal error`，`check_head_blob_hygiene --fix` 只印 `write failed`、結束碼 `0`／`1`；現在都是結束碼 `2` 加一行。被拒的若是這個行程的 stdout，不寫任何檔；若是不可寫的檔，在它之前的檔可能已寫出（#4250），已寫出的檔會在寫出時逐一列出（`fix_file_hygiene` 多印 `fixed <path>`，`bump_playbook_versions` 多印 `wrote <path>`）。遇到 FIFO 原本卡住：兩支 hygiene 工具現在略過它；`bump_playbook_versions` 有任何一份讀不到、非 UTF-8 或是 FIFO 時，結束碼 `2`，不寫任何一份。`check_head_blob_hygiene --fix` 的逐檔結果改以 `--fix:` 區塊列出。
