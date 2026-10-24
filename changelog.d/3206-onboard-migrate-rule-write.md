---
section: Changed
topic: confd-family
issues: [3206]
created: 2026-10-24T02:14:12+00:00
---
- **`onboard_platform.py`、`migrate_rule.py` 的 conf.d 檔改由 `da-guard write` 寫（[#3206](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3206)，[ADR-036](docs/adr/036-single-parser-effective-config.md) §2 步驟 10c）**：onboard 的 `phase1-routing/<tenant>.yaml`、`phase2-rules/_defaults-suggestion.yaml` 與 migrate 的 `tenant-config.yaml`、`defaults-snippet.yaml`，資料（帶型別的值、檔首／鍵上方／檔尾的註解）在寫其他輸出之前一次交給 `da-guard write`，文字由 Go 產生、整棵樹判一次，一個檔被拒就一個都不寫（rc 2、逐檔點名原因）；這四個檔不再有 PyYAML 的寫出路徑。重跑同一個 `-o` 照舊覆寫（整份取代）。migrate 的兩個檔同進退：先前 `defaults-snippet.yaml` 最後才寫。要寫進註解的外部輸入（alert 名、`--prefix` 帶出的 key、租戶 id 等）含註解寫不了的字元時 rc 2、點名欄位。CSV、rule pack、報告、Phase 3 片段與 hints 照舊由 Python 寫。⚠️ 可見變化：字串一律雙引號（`group_wait: "30s"`）；先前被拒的 `'1e3'` 寫成 `"1e3"`，`--prefix "team #"` 的鍵寫成 `"team #app_queue_depth"`（都照常寫出）；超過 18 位的整數以浮點寫出（`1e20` 寫成 `100000000000000000000.0`）；非有限的閾值（`1e999`）改為 rc 2、點名 key（先前寫成 `.inf`）；只有註解的檔（onboard 只有 critical 層的建議、migrate 沒有任何 key 的 tenant-config）頂層寫成 `{}`；縮排 2 格，鍵之間、規則之間的空行不再保留，檔尾註解與內容之間空一行。讀起來的內容不變。
