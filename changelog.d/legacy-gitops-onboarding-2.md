---
section: Fixed
topic: gitops-onboarding
issues: [902, 1218, 1337, 1347, 1356, 1357, 1358, 1361, 1380, 1383, 1408, 1417, 1418, 1454, 1473, 1791, 1911, 1942]
created: 2026-09-26T17:00:00+00:00
---
- **`da-tools init` 產出的 CI／部署管線改為真的能跑（tools）**：先前多個預設組合交出的管線無法運作——GitHub workflow 因 `apply` 縮排錯位被整份拒絕載入、`validate-config` 被傳入不存在的 `--ci` 使 Stage 1 必定失敗、GitLab pipeline 放在不會被自動載入的位置（[#1347](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1347)、[#1357](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1357)）。現已修正並以 `actionlint` 與 GitLab schema 驗證全組合產物；GitLab 的 production `apply` 限定在預設分支手動觸發、驗證 job 不再允許失敗、部署映像改釘具體版本，交付的 `actions/checkout` 升到 `@v6`。`--deploy helm` 補產 `environments/prod/values.yaml` 骨架（⚠️ helm 模式上線的是這份 values 而非 `conf.d/`，填寫前只有 chart 預設），`-o <子目錄>` 的路徑前綴一併套用，也不再於既有租戶檔旁另造重複載體。⚠️ 產出的 `_defaults.yaml` 曾把 16 個 `*_critical` 放進 `defaults:`，critical 分級因此從未生效，現改寫進各 `<tenant>.yaml`；已跑過 init 的客戶需手動把這些 key 移到租戶檔（[#1218](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1218)）。
