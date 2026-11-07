---
section: Changed
topic: rule-packs
issues: [2566, 1200]
created: 2026-11-07T00:54:46+00:00
---
- **⚠️ BREAKING：租戶 metadata annotation 改由生成器寫入，所有租戶告警都帶（[#2566](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2566)）**：凡宣告 `tenant` label、且不是 `alert_source: platform` 的告警，`generate_rulepack_derived.py` 都會寫入 `runbook_url`／`owner`／`tier` 三個 annotation（模板沿用 `check_leftouterjoin_enrichment.METADATA_ANNOTATIONS`），pre-commit 會擋下手改與遺漏。停機、exporter 缺席、哨兵與 `CustomRecipeSilent` 等 31 條原本沒帶的告警也補上了；租戶沒有 metadata 時值為空字串，告警照常觸發。升級注意：`VersionAwareThresholdInert`、`CustomRecipeDiskInert` 原本指向平台文件的 `runbook_url` 改名為 `platform_runbook_url`；一次性腳本 `inject_metadata_join.py` 移除。
