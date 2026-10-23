---
section: Changed
topic: confd-family
issues: [3206]
created: 2026-10-23T22:00:00+00:00
---
- **`generate_tenant_fixture.py` 改由 `da-guard write` 寫檔（[#3206](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3206)，[ADR-036](docs/adr/036-single-parser-effective-config.md) §2 步驟 10c-2）**：整個合成 fixture 的資料（帶型別的值、鍵順序照產生的）一次交給 `da-guard write`，文字由 Go 產生、整棵樹判一次，一個檔被拒就一個都不寫（rc 2）；不再有 Python 自己的 YAML 輸出。⚠️ 可見變化：字串一律寫成雙引號（`"disable"`、`"gold"`；維度鍵 `metric{label="x"}` 也寫成雙引號），數字以十進位展開寫出（`1e-05` 寫成 `0.00001`）；讀起來的內容不變。產生時間隨租戶數線性增加，1000 個租戶約 7–8 秒（先前約 1 秒）。值經新的 `_lib_confedit.data_value` 轉成帶型別的值；沒有契約型別的值（`--extra-defaults k=inf`、不是字串的鍵）rc 2、點名該鍵、什麼都不寫。
