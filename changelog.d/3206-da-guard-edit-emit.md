---
section: Added
topic: confd-family
issues: [3206]
created: 2026-10-23T12:50:00+00:00
---
- **`da-guard edit`／`da-guard emit`：conf.d 只有 Go 一個寫者（[#3206](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3206)，[ADR-036](docs/adr/036-single-parser-effective-config.md) §2 步驟 10a）**：`edit` 對既有檔做 set／unset／ensure-map，編輯以外的節點與註解照原樣寫回；`emit` 依呼叫端給的鍵順序寫新檔。輸入是 JSON，mapping 寫成 `[key, value]` 的有序清單、純量自帶型別（`{"str": …}`、`{"int": …}` 等）。寫入前以 `parse-tree` 單檔模式在目標路徑讀回，內容必須等於「原檔解析結果套上這次修改」，原檔讀不了、沒有讀取端讀那個路徑、或重新輸出會改到編輯以外的節點時一律不寫（結束碼 1，`--json` 點名原因）；`--dry-run` 只印出要寫的內容。新增的鍵只有識別字形式、又不是 YAML 1.1 布林／null 字時才寫成 plain；不是一般檔的路徑（FIFO、裝置）不讀、直接拒絕；CRLF、BOM 與擁有者保留。tenant-api 的批次 patch 與 custom-alerts 合併改用同一個共用套件 `pkg/confedit`，行為不變。寫 conf.d 的 Python 工具尚未切換（後續步驟）。
