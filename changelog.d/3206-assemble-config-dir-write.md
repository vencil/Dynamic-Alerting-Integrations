---
section: Changed
topic: confd-family
issues: [3206]
created: 2026-10-27T17:42:36+00:00
---
- **`assemble_config_dir.py` 的組裝改由 `da-guard write` 寫（[#3206](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3206)，[ADR-036](docs/adr/036-single-parser-effective-config.md) §2 步驟 10）**：不再以 `shutil.copy2` 複製、`chmod 0644`，要組裝的檔一次以 `copy` 項目交給 Go：每個來源檔的位元組原樣放進 `--output`，同名的既有檔整份取代，整棵樹判一次，一個被拒就一個都不寫、逐檔點名、結束碼 2；這一輪建立的 `--output` 被拒時再移除。原有的衝突檢查（同名載體、跨來源重複租戶，結束碼 1）照舊在前。⚠️ 可見變化：新檔權限是 0600（先前 0644），取代的既有檔保留原權限——exporter 以別的 uid 讀 `--output` 時，部署端要自行放寬（例如 `chmod -R a+rX`）；`--check` 改問同一批的 dry run，會被拒的組裝（例如 `--output` 裡同名的是 symlink）在 `--check` 就結束碼 2；來源裡是 symlink 的檔，複製的是它指向的那個檔。
