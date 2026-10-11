---
section: Fixed
topic: dx
issues: [2852]
created: 2026-10-11T08:50:45+00:00
---
- **13 支開發期 lint／產生器只讀寫自己所在的那棵樹（[#2852](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2852)）**：`gen_am_duration_json`、`gen_recipe_status_json`、`gen_tenant_id_json`、`generate_rulepack_configmaps` 與 `check_cross_ns_url_consistency`、`check_ha_threshold_aggregation`、`check_ksm_version_allowlist`、`check_leftouterjoin_enrichment`、`check_portal_rulepack_claims`、`check_retire_drift`、`check_single_writer_invariant`、`check_unpinned_deps`、`check_workflow_git_push_permissions` 原本以「第一個帶 `.git` 的祖先」當根目錄，本樹沒有 `.git` 時會改讀外層專案（產生器也會寫進外層）。現在一律取包住工具的 `scripts/tools` 所在目錄（新增 `_lib_compat.require_project_root`），不在任何 `scripts/tools` 底下時回 rc 2，即使明確給了 `--root` 之類的路徑旗標也一樣（這些旗標的預設值由根目錄算出）。checkout 與 git worktree 的行為不變。
