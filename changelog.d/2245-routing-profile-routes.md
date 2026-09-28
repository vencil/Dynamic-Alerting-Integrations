---
section: Added
topic: alertmanager-routing
issues: [2245, 2232]
created: 2026-09-28T06:30:00+00:00
---
- **routing profile 與 tenant `_routing` 的 `routes:` 開始生效（ADR-007；[#2245](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2245)）**：`match: {label: value}` 只做等值比對，每條產出 tenant 主路由底下的一條子路由，排在 overrides 之後；沒寫的 timing / `group_by` 沿用主路由，receiver 名為 `tenant-<t>-route-<i>`。先前產生器靜默丟掉 `routes`，`explain_route` 卻印成已合併，現在改列實際產出的子路由與被略過的條目。不合法的條目（regex、`continue`、空 `match`、非字串值）以 `WARN … skipping` 略過，`--validate` 會失敗；domain policy 與 `--policy` 網域檢查也涵蓋這些 receiver。`_routing_defaults` 寫 `routes` 會被略過並報錯。`routing-profiles.schema.json` 改為引用 tenant 的 `routing` 定義，`check_confd_schema` 與 `validate-config` 的引號檢查開始涵蓋 `_routing_profiles.yaml`（[#2232](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2232)）。
