---
section: Changed
topic: confd-family
issues: [2906]
created: 2026-10-17T14:40:00+00:00
---
- **⚠️ Breaking：`gitops-check local`、`diagnose --show-inheritance`、`backtest`、`policy-engine --policy`、`opa-evaluate`、`generate_tenant_mapping_rules.py`、`validate-config --policy-dsl` 與 `check_retire_drift`／`check_path_metadata_consistency`／`check_threshold_unit_sanity`／`check_threshold_reachability` 改經 `da-guard parse-tree` 讀 conf.d（tools；[#2906](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2906)）**：值是 yaml.v3 的讀法（未加引號的 `yes`／`on` 是字串、`0o17` 是 15、日期是原文），選哪些檔也是 exporter 的（子目錄、`.yml`、任何大小寫）。需要 da-guard（`$DA_GUARD_BINARY`，否則 `$PATH`）；沒有或執行失敗時結束碼 2（`check_path_metadata_consistency` 原本一律 0）。parse-tree 讀不了的檔（重複鍵、多份文件、`!!set` 等標籤、頂層不是 mapping）會被指名，不再略過或照 PyYAML 讀；`--policy`／`--policy-dsl` 的獨立檔也一樣。
