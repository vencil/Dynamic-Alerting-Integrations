---
section: Changed
topic: confd-family
issues: [2115]
created: 2026-10-06T18:00:00+00:00
---
- **`validate-config` 的 profiles 列改讀 exporter 的答案（[#2115](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2115)）**：⚠️ 行為變更：租戶的 `_profile` 有沒有綁到 profile 改看 `da-guard effective`，不再自己讀檔。profile 定義在任何根目錄平台檔的 `profiles:` 都算數（以前只認 `_profiles.yaml`，定義在 `_defaults.yaml` 的會被誤報 unknown）；寫在根目錄平台檔 `tenants:` 條目的 `_profile` 也檢查；從子目錄 `_defaults.yaml` 繼承來的 `_profile` 改報「exporter 不從 defaults 檔綁 profile」並點名該檔。沒有 `tenants:` 的檔不再當租戶（印 `WARN`）。exporter 丟掉或讀不到的檔（例如根 `defaults:` 裡寫了 `_profile`，整份 `_defaults.yaml` 被丟掉）、被拒收的樹，這一列 FAIL 並附 da-guard 的原因；找不到 da-guard 時這一列 FAIL、結束碼 2——`validate-config` 從此每次都需要 da-guard（da-tools 映像內建）。profile 本身的結構檢查仍只讀 `_profiles.yaml`。
