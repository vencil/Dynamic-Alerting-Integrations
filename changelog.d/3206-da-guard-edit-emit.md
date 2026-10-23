---
section: Added
topic: confd-family
issues: [3206]
created: 2026-10-23T12:50:00+00:00
---
- **`da-guard write`：conf.d 只有 Go 一個寫者（[#3206](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3206)，[ADR-036](docs/adr/036-single-parser-effective-config.md) §2 步驟 10）**：一次呼叫是一批檔（JSON `{"files": [...]}`，路徑不可重複），每一項是改寫既有檔（`ops`：set／unset／ensure-map，`set []` 換掉整個頂層；編輯以外的節點與註解照原樣寫回）、新建檔（`create`：有序的 map，鍵與檔首可帶註解，註解是版面、每行寫成 `# …`、不讀回）或逐位元組搬移（`from`，來源之後不存在）。整批放進樹裡以 `parse-tree` 的整棵樹模式判一次，內容須等於預期，一個檔被拒就都不寫、逐檔列出原因；可以修好樹、不能讓樹變壞：批外的檔失去讀取端、任何路徑新出現 verdict、驗證新出現錯誤（如租戶重複）時整批拒寫（`worsens_tree`）；原檔被驗證點名不構成拒絕，但寫出或留在原位的文字不得被點名。寫到一半失敗時新建的檔移除、搬移搬回，已 rename 的編輯標為已寫入（`partial_write`）。`--json` 為 `da-guard.write/v3`；CRLF、BOM 與擁有者保留，不是一般檔的路徑不讀。tenant-api 的批次 patch 與 custom-alerts 合併改用同一個共用套件 `pkg/confedit`，行為不變，唯一例外是批次 `unset` 移除租戶區塊最後一個鍵時，現在照常寫成 `t1: {}`（以前誤判為 `not_round_trippable` 拒收）。其他產生新檔的 Python 工具尚未切換（後續步驟）。
