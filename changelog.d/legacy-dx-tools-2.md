---
section: Fixed
topic: dx-tools
issues: [1511, 1542, 1696, 1815, 1929]
created: 2026-09-26T17:00:00+00:00
---
- **內部生成與建置工具修正（internal、dx）**：`make platform-data` 不再必定 exit 2，tenant metadata 載入失敗改為 fail-loud、不寫出殘缺的 `platform-data.json`；Makefile 補齊 `.PHONY`，help 與 recipe 對齊實際可轉送的旗標；regen 工具在 Windows 上不再把整檔改寫成 CRLF（⚠️ 連帶 `migrate_rule.py`／`validate_migration.py` 的 CSV 在 Windows 上改輸出 LF）；「Python 工具數」的檢查端與寫入端改用同一份計數述詞；`generate_tool_map` 遇到讀不進來的檔不再 traceback、`--lang` 預設改 `all`；編輯器 schema 綁定與 CI 的 conf.d 檔案集合對齊。
