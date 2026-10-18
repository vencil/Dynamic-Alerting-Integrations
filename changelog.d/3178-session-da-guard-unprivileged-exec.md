---
section: Fixed
topic: dx
issues: [3178]
created: 2026-10-18T11:33:19+00:00
---
- **以 root 跑測試時，降權成 `nobody` 的 lint 測試不再因執行不了 da-guard 而紅（測試基礎設施；[#3178](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3178)）**：`tests/conftest.py` 建的 session da-guard 目錄與執行檔改為所有 uid 可讀可執行（0755，其他 uid 仍不可寫）。`check_path_metadata_consistency` 那支以 `setpriv` 降權的測試，執行前也會以同一個 uid 試跑 `$DA_GUARD_BINARY`，跑不了時以寫明原因的 skip 收場，不再是只帶 `PermissionError` 的紅；另有一支測試釘住 session da-guard 的權限，讓修法被拿掉時轉紅而不是悄悄變成 skip。
