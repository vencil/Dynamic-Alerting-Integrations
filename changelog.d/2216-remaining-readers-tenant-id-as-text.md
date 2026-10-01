---
section: Fixed
topic: confd-reader-consistency
issues: [2216]
created: 2026-10-01T22:23:42+00:00
---
- **其餘讀租戶 id 的工具也改以原始文字讀取，與 exporter 一致（tools；[#2216](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2216)）**：下列工具過去以 YAML 1.1 型別讀 `tenants:` 的 key，不加引號的 `010` 被讀成 `8`：`generate_tenant_mapping_rules.py`、`generate_tenant_metadata.py`（原本依樹的內容不同，可能 traceback 或把租戶列成 `8`）、`backtest_threshold.py` 的 custom alert 租戶清單、custom alert 編譯器的租戶 recipe、`migrate_conf_d.py`、`check_path_metadata_consistency.py`、`check_retire_drift.py`、`threshold_govern.py` 寫前驗收（原本誤報租戶消失而拒寫）、`offboard_tenant.py` 的 pre-check（原本找不到該租戶的指標與跨檔引用），以及 `check_md_yaml_drift.py` 的 schema 檢查（同一區塊的 `010:` 與 `8:` 原本被併成一個租戶，只檢查到其中一份設定）。以值寫出的租戶 id 也改以原始文字讀取：`onboard_platform.py` 讀 Alertmanager 設定時 `match:` 裡的租戶 label、`_instance_mapping.yaml` 的 `tenant:`（原本 traceback）、`_groups.yaml` 的 `members:`、`check_retire_drift.py` 讀 Namespace 的 `instance` label，以及 `offboard_tenant.py` 找跨檔引用時的 `exclude_tenants:`、domain policy `tenants:`、`members:` 清單。加引號的寫法結果不變。
