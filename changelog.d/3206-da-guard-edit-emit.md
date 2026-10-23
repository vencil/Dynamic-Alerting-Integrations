---
section: Added
topic: confd-family
issues: [3206]
created: 2026-10-23T12:50:00+00:00
---
- **`da-guard edit`／`da-guard emit`：conf.d 只有 Go 一個寫者（[#3206](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3206)，[ADR-036](docs/adr/036-single-parser-effective-config.md) §2 步驟 10a）**：`edit` 對既有檔做 set／unset／ensure-map（`set` 的路徑為 `[]` 時整個頂層換成給的 map），編輯以外的節點與註解照原樣寫回；`emit` 依呼叫端給的鍵順序寫新檔。一次呼叫是一批檔（JSON `{"files": [{"path": …, "ops": […]}, …]}`，emit 為 `"content"`，路徑不可重複；純量自帶型別，mapping 是有序清單），整批放進樹裡以 `parse-tree` 的整棵樹模式判一次，內容須等於「原檔解析結果套上這次修改」，一個檔被拒就都不寫、逐檔列出原因；原檔讀不了、路徑沒有讀取端、重新輸出會改到編輯以外的節點也不寫；寫到一半失敗時 emit 的新檔會移除，edit 已 rename 的檔標為已寫入（`partial_write`）；`--json` 為 `da-guard.write/v2`；原檔被驗證點名（exporter 讀不進去）不構成拒絕——編輯可以修好它，但寫出（或沒有改變、留在原位）的文字不得被點名，否則拒寫（結束碼 1，`--json` 點名原因）；只有註解的檔當成空檔改；`--dry-run` 只印出要寫的內容。新增的鍵只有識別字形式、又不是 YAML 1.1 布林／null 字時才寫成 plain；不是一般檔的路徑（FIFO、裝置）不讀、直接拒絕；CRLF、BOM 與擁有者保留。tenant-api 的批次 patch 與 custom-alerts 合併改用同一個共用套件 `pkg/confedit`，行為不變，唯一例外是批次 `unset` 移除租戶區塊最後一個鍵時，現在照常寫成 `t1: {}`（以前誤判為 `not_round_trippable` 拒收）。產生新檔的 Python 工具尚未切換（後續步驟）。
