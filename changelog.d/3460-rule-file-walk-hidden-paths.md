---
section: Changed
topic: confd-family
issues: [3460]
created: 2026-10-31T16:50:00+00:00
---
- **⚠️ Breaking：rule file 工具走訪目錄時不讀隱藏路徑，ConfigMap 掛載目錄不再重複讀（[#3460](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3460)）**：`lint`、`silencer-drift-check --rule-source`、`onboard --rule-files` 給目錄時，目錄底下名稱以 `.` 開頭的檔與目錄不讀，與 exporter 讀 conf.d 的規則相同；所給的目錄本身與直接給的檔不受此限。Kubernetes ConfigMap 掛載目錄因此只讀一次、以使用者看到的 `r.yaml` 回報，不再連 `..2024_01_01/r.yaml` 也讀。隱藏路徑下沒有經其他路徑讀到的 YAML 檔（例如 `.hidden/x.yaml`）改為點名拒收，要讀就直接給成目標，否則以 `--exclude` 排除；目標經過隱藏路段的目錄 symlink（`team-a -> ..data/team-a`）與指向樹外的隱藏目錄 symlink 也點名。
