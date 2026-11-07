---
section: Fixed
topic: tenant-api
issues: []
created: 2026-11-07T06:22:14+00:00
---
- **tenant-api 測試：寫完就執行的 stub 不再偶發「text file busy」**：測試用 `os.WriteFile` 寫出 stub git、hook 或 driver 再執行它時，若同時有平行測試在 fork，子行程會在 exec 之前持有該檔的寫入 fd，執行就失敗並回 ETXTBSY（[golang/go#22315](https://github.com/golang/go/issues/22315)），gitops 的 CI 因此偶發轉紅。新增 `testutil.WriteExecutable`，在開檔到關檔之間持有 `syscall.ForkLock` 的讀鎖；所有呼叫點改用它，並以 go/ast 架構測試擋下測試裡帶執行位元的 `os.WriteFile`／`os.OpenFile`。只影響測試，不影響執行檔。
