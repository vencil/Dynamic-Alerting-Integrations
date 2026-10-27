---
section: Changed
topic: confd-family
issues: [3206]
created: 2026-10-27T21:30:00+00:00
---
- **⚠️ Breaking：`check_threshold_registry --regen` 的兩份 conf.d `_defaults.yaml` 改由 `da-guard write` 寫（tools；[#3206](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3206)，[ADR-036](docs/adr/036-single-parser-effective-config.md) §2 步驟 10）**：拼好生成區塊的檔各自整份取代，每個 conf.d 根目錄一批，寫任何檔前先以同一批 dry run 判定；被拒時逐檔點名，registry 與生成面一個檔都不寫，結束碼 2。Python 的讀回比對拿掉，改為 da-guard 的讀回與驗證（`inf`、原檔重複鍵都是 `read_back_mismatch`）；生成區塊不在 spec 指定的 key 底下（例如 `optional_overrides:` 被手改名）照舊拒寫。⚠️ 可見變化：檔的權限保留（先前改成 0600）；conf.d 檔是指向 repo 內的 symlink 時由 da-guard write 的 dry run 拒寫（reason `symlink`），先前跟著連結寫進目標檔；registry 改在生成面之後才寫；寫到一半停下時點名失敗的檔與已寫入、不會還原的檔（結束碼 2）。檢查模式不變。
