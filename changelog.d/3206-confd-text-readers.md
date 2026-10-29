---
section: Changed
topic: da-tools
issues: [3206]
created: 2026-10-29T17:28:27+00:00
---
- **conf.d 形狀的文字一律改經 `da-guard parse-tree` 讀（[#3206](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3206)，[ADR-036](docs/adr/036-single-parser-effective-config.md) §12）**：`check_threshold_unit_sanity` 的 portal 範本與文件 yaml 區塊、`check_threshold_reachability` 讀 `da-tools init` 產生的 `_defaults.yaml`，都以 exporter 的讀法（yaml.v3）讀；沒有 da-guard 就 rc 2，不退回 PyYAML。⚠️ exporter 拒收的範本改為點名、rc 2（原本略過）；拒收的 `_defaults.yaml` 產生器是 floor breach（`--ci` rc 1）。文件區塊依 `# <檔名>.yaml` 拆成各檔再讀，原本被重複的 `tenants:` 蓋掉的租戶值也會檢查；整段被拒收的區塊略過（拒收的 conf.d 範例由 md-yaml-drift 點名）。⚠️ `check_md_yaml_drift` 判斷「被拒收的區塊是不是 conf.d 範例」不再用 PyYAML：文字裡出現任何 conf.d 鍵名（或反斜線跳脫）就當成範例、報 UNREADABLE。
