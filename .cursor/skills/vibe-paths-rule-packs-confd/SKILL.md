---
name: vibe-paths-rule-packs-confd
description: "Vibe 路徑觸發指引：動到 rule-packs/**、components/threshold-exporter/config/conf.d/**、try-local/seed/conf.d/** 之前先讀的章節與一句約束。"
paths:
  - "rule-packs/**"
  - "components/threshold-exporter/config/conf.d/**"
  - "try-local/seed/conf.d/**"
---
<!-- 此檔為產生物，來源 .agents/paths-map.json —— 請改那份 SSOT，再跑 `make agent-adapters`；不要直接編輯這份複本。 -->
# vibe-paths-rule-packs-confd

先讀：
- `docs/internal/testing-playbook.md` §conf.d/ YAML 格式陷阱

約束：conf.d 是 wrapped format（`tenants:` 包一層）；rule-pack 改動後 `make platform-data` 重生數據，`rule-pack-stats-check` 與 `platform-data-check` 會擋 drift；Go／PromQL／fixture 不得 hardcode tenant id。
