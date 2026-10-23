---
section: Changed
topic: confd-family
issues: [3206]
created: 2026-10-23T16:21:00+00:00
---
- **`patch-config`、`threshold-govern`、`deprecate` 改由 `da-guard write` 寫檔（[#3206](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3206)，[ADR-036](docs/adr/036-single-parser-effective-config.md) §2 步驟 10b）**：三支工具只送「改什麼」，文字由 Go 產生並讀回確認。⚠️ 可見變化：被改的值以外的註解、flow 與每個純量的寫法都保留，但版面重排（縮排 2 格、行尾註解前一格空白、空行可能消失）；`patch-config` 打的值是標準十進位時寫成數字（`50`，先前 `'50'`），其他寫成雙引號字串（`"70:critical"`）；`deprecate` 對 symlink 的檔拒寫，多檔一次送進同一次 `da-guard write`、整批判定，一個被拒就都不寫（rc 1），寫到一半失敗是 rc 2 並列出已寫入的檔；`threshold-govern` 第一次經它寫的檔 PR diff 會含版面變化。
