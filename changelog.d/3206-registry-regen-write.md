---
section: Changed
topic: confd-family
issues: [3206]
created: 2026-10-27T21:30:00+00:00
---
- **⚠️ Breaking：`check_threshold_registry --regen` 的兩份 conf.d `_defaults.yaml` 改由 `da-guard write` 寫（tools；[#3206](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3206)，[ADR-036](docs/adr/036-single-parser-effective-config.md) §2 步驟 10）**：dev 範本與 try-local seed 拼好生成區塊的檔各自整份取代，每個 conf.d 根目錄一批，寫任何檔之前先以同一批 dry run 判定；被拒時兩個根目錄都判完、逐檔點名，registry 與所有生成面一個檔都不寫，結束碼 2。判定改由 Go 做：「讀回須等於原檔換上生成項目」的 Python 比對拿掉，改為 da-guard 的讀回與驗證（`inf` 與原檔的重複鍵都是 `read_back_mismatch`）。⚠️ 可見變化：檔的權限保留（先前改成 0600）；conf.d 檔是 symlink 時拒寫（先前寫進它指向的檔）；registry 改在生成面之後才寫；寫到一半停下時點名已寫入、不會還原的檔（結束碼 2）。檢查模式不變、不需要 da-guard。
