---
section: Changed
topic: confd-family
issues: [3206]
created: 2026-10-24T12:00:00+00:00
---
- **`scaffold_tenant.py` 與 `da-tools init` 的 conf.d 檔改由 `da-guard write` 寫（[#3206](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3206)，[ADR-036](docs/adr/036-single-parser-effective-config.md) §2 步驟 10c-3）**：這次要寫的 conf.d 檔（`_defaults.yaml`、`<tenant>.yaml`、`_instance_mapping.yaml`、`_profiles.yaml`）連同註解一次交給 `da-guard write`，整棵樹判一次，一個被拒就都不寫；原本會覆寫的檔改為整份取代。拒寫一律結束碼 2（`init` 原為 1），訊息點名檔與原因。`scaffold --from-onboard` 改為只寫一份 `_defaults.yaml`，含所有租戶選的 pack（原本最後一個租戶的那份蓋掉其他租戶的）。⚠️ 可見變化：字串一律雙引號；序列項目縮排兩格；檔首註解之後、檔尾「已宣告 key」清單之前各多一個空行；反例註解依新縮排重新折行；新建的 conf.d 檔權限為 0644（原為 0600；取代既有檔時沿用原權限）。讀起來的內容不變。
