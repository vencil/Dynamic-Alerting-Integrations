---
section: Changed
topic: ci
issues: [3228]
created: 2026-11-12T23:57:55+00:00
---
- **tenant-api 發布前拒絕 image-pin 豁免（release；[#3228](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3228)）**：`release-tenant-api` 在建置 image 之前以 `check_image_pin_capability.py --chart helm/tenant-api --refuse-exemptions` 檢查 chart 預設的 da-tools／recipe-preview pin，已登記的豁免一律當 violation，所以預設安裝跑不起來的 chart 不會被發布。腳本新增 `--chart`（只查一個 chart，查不到任何 workload 回 2）與 `--refuse-exemptions`。在下一個帶 `generator-service` 與 recipe-preview image 的 `tools/v*` 發布、`bump_docs.py --tools` 移動 `generator.image.tag` 之前，tenant-api 發布會被這一步擋下：tenant-api 須排在 tools 之後發。
