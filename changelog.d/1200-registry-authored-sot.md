---
section: Changed
topic: threshold-registry
issues: [1200]
created: 2026-11-10T12:02:00+00:00
---
- **閾值改由 `rule-packs/threshold-registry.yaml` 手寫，`scaffold_tenant.RULE_PACKS` 的閾值半邊改為生成物（[#1200](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1200)）**：要改閾值、單位、說明、`metric_class`、`chart_default` 或 deprecated alias，一律改 registry 再跑 `check_threshold_registry.py --regen`；`scripts/tools/ops/_rule_packs_generated.py` 與其他生成面一起重生，da-tools 映像隨附它。`RULE_PACKS` 的 import API 不變，但每個鍵都帶 `metric_class`（只有 `saturation` 會影響輸出，集合不變）。閘門改驗 registry 是自身抽取的不動點（`critical_of` 錯、optional 鍵掛 `chart_default`、alias 指向不存在的鍵 → 紅），並拒收重複鍵。
