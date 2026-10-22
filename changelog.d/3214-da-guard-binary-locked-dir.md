---
section: Fixed
topic: da-tools
issues: [3214]
created: 2026-10-21T16:00:00+00:00
---
- **`$DA_GUARD_BINARY` 在進不去的目錄時改報具名錯誤（[#3214](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3214)）**：`$DA_GUARD_BINARY`／`--da-guard-binary` 指向的檔案位在執行者無法進入的目錄（例如 root 建的 0700 暫存目錄，由其他 uid 執行）時，經 da-guard 讀 conf.d 的工具與 `da-tools guard`／`batch-pr`／`parser` 先前以 `unexpected internal error (PermissionError)` 結束；現在改為 `da-guard could not be run (<路徑>): Permission denied`，與檔案本身不可執行時相同。結束碼不變（2）。`generator-service` 先前在這種情況下以誤導的 `cannot listen on '<socket>': Permission denied` 拒絕啟動（rc 2）；現在與 binary 本身不可執行時相同：照常啟動，每次判定時才報 da-guard 無法執行。
