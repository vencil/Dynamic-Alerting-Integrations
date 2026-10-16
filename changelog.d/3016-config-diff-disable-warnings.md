---
section: Added
topic: da-tools
issues: [3016]
created: 2026-10-16T08:30:00+00:00
---
- **`config-diff` 列出這次變更新引入、沒有作用或只關到一部分的 `disable`（[ADR-038](docs/adr/038-unread-keys-judged-by-readers.md) 決定 8）**：noop 是拿掉這個 `disable` 後 exporter 送出的值完全不變（常見原因是 key 拼錯）；partial 是 key 本身關掉了，但它的 `_critical` 或維度鍵仍在送出。Markdown 報告新增「`disable` With No or Partial Effect」一節，JSON 新增 `disable_warnings`（每筆 `key`、`disable`、`served_family`、`tenants`）。base 那側已經是同樣狀態的不列；這一節只是提醒，不單獨影響結束碼。
