---
section: Changed
topic: tools
issues: [4215]
created: 2026-11-25T02:00:00+08:00
---
- **第三方 promql-parser 退場，PromQL 只由 Prometheus 自己的 parser 判讀（tools；[#4215](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4215)）**：`da-tools migrate`（`migrate_rule.py`）、`onboard_platform` 的閾值候選、HA-max lint（`check_ha_threshold_aggregation`）與 absence guard 判定，改經 `da-read promql-shape` 讀語法樹；promql-parser（Rust/PyO3）從 da-tools 映像、CI、dev container 與 pre-commit 移除。遷移工具的門檻、指標、label matcher、聚合猜測與語義中斷函式都從語法樹讀，tenant label 依 selector 在原式中的位置注入：多行的舊規則不再一律判成無法解析；label 值裡的 `-`、`rate(` 不再影響判定；`x {…}`、`{__name__="x"}`、同一指標同時裸寫與帶標籤的寫法都會注入 tenant（regex 改寫時這幾種會靜默漏掉，讓 recording rule 讀到所有租戶）；改寫後 Prometheus 不接受即 rc 2 點名，不再退回原式。⚠️ Breaking：`migrate --no-ast` 移除，帶這個旗標 rc 2（`unrecognized arguments`）；遷移報告不再標示引擎；語義中斷清單移除部署的 Prometheus 不認得的 `holt_winters`（用到它的規則在 rule loader 就 rc 2）。
