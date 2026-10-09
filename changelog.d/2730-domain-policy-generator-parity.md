---
section: Fixed
topic: alertmanager-routing
issues: [2730, 2486]
created: 2026-10-08T15:34:20+00:00
---
- **da-guard 與 tenant-api 讀 `_domain_policy.yaml` 與 route generator 對齊（exporter、tenant-api；[#2730](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2730)）**：產生器整份丟棄的檔——第二份 YAML 文件、最上層 `tenants:` 不是 mapping、值的位置有 `!!merge` 集合、會被建構卻無 constructor 或建構失敗的值（`=`、`!custom x`、`!!int x`）、引號／註解／block scalar 以外的 TAB（`key:<TAB>v`、行尾 TAB、`---<TAB>`）、非 UTF-8（含 UTF-16）——da-guard 改報 `domain_policy_unusable`，tenant-api 改為拒收。`tenants` 項目照原文讀，`!!null x` 與名為 null 的 domain key 照產生器讀。vendored yaml.v3 加兩段 patch：保留非特定 tag `!`（`! "true"` 是布林、`! "<<"` 是 merge key），以及只在讀 policy 時開啟、照 PyYAML 只認空格分隔的 scanner 開關。forbidden 中非字串的 receiver type 不禁止任何 type，`allowed_receiver_types` 的非字串項目不允許任何 type。帶 `%YAML 1.2` 或未知 directive 的檔 da-guard 判不可用、tenant-api 拒收（比產生器嚴）；巢狀深度、flow 內以冒號結尾的 plain、`!!set`、`!!omap`／`!!pairs` key 仍比產生器寬（[#2759](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2759)）。
