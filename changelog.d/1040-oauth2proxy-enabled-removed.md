---
section: Changed
topic: tenant-api
issues: [1040]
created: 2026-11-13T15:30:00+00:00
---
- **⚠️ Breaking：tenant-api chart 移除 `oauth2Proxy.enabled`（ADR-027 §6；[#1040](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1040)）**：oauth2-proxy 是 pod 內唯一的 oauth2-proxy，也是到 API 的唯一入口（§5），原本設成 `false` 就已經 render 失敗，只剩 `true` 能用。這個開關與模板裡依它判斷的分支一併刪除，oauth2-proxy、它的設定檔與 Unix socket 一律渲染。`values.schema.json` 拒收這個 key：升級前從 values 檔刪掉 `oauth2Proxy.enabled`（不論設成什麼），否則 `helm upgrade` 以 `additional properties 'enabled' not allowed` 失敗。ADR-027 §9 的 5b 檢查（`tests/helm/test_tenant_api_machine_identity_removed.py`）已涵蓋這個 key。
