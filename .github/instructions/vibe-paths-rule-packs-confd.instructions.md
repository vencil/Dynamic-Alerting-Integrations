---
applyTo: "rule-packs/**,components/threshold-exporter/config/conf.d/**,try-local/seed/conf.d/**"
---
<!-- 此檔為產生物，來源 .agents/paths-map.json —— 請改那份 SSOT，再跑 `make agent-adapters`；不要直接編輯這份複本。 -->
# vibe-paths-rule-packs-confd

先讀：
- `docs/internal/testing-playbook.md` §conf.d/ YAML 格式陷阱

約束：conf.d 是 wrapped format（`tenants:` 包一層）；rule-pack 改動後的產物各有各的重生指令與 drift hook——`python3 scripts/tools/dx/generate_rule_pack_stats.py --generate --lang all`（`rule-pack-stats-check`）、`make rulepack-configmaps`（副本 drift 由 `rulepack-configmap-drift`／`rulepack-3copy-drift` 擋）、`make platform-data`（`platform-data-check`）——一支跑完不代表另幾道會綠；Go／PromQL／fixture 不得 hardcode tenant id。
