---
section: Fixed
topic: dx
issues: [2640, 2641]
created: 2026-10-03T06:37:24+00:00
---
- **`subprocess-timeout-audit` 遇到不存在的掃描路徑改為 exit 2；`verify_diff.py` 不再漏選帶 BOM 的測試檔（dx；[#2640](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2640)、[#2641](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2641)）**：`check_subprocess_timeout.py` 收到不存在的路徑（命令列指定的，或未指定時的任一預設根）會在 stderr 指名該路徑並以 exit 2 結束，任何模式皆然；先前它被靜默略過，打錯字或改名的目錄掃到零個檔仍回 exit 0，與乾淨的樹無從區分（與 `open-encoding-audit` 同一規則）。`verify_diff.py` 讀測試檔改用與直譯器相同的解碼（剝掉一個 UTF-8 BOM、依 PEP 263 cookie 選編碼）：帶 BOM 的測試檔先前會從映射消失，`--dry-run`／`--run` 漏選它、`--check` 誤報「AST 解析失敗」；真正的語法錯誤仍照舊報出。
