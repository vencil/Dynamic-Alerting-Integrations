---
section: Added
topic: tenant-api
issues: [2902, 2826, 2858]
created: 2026-10-15T02:26:01+00:00
---
- **tenant-api chart：可選的產生器容器（`generator.enabled`，預設關；ADR-036 §3／§5；[#2902](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2902)）**：開啟後 pod 多一個 da-tools 容器跑 `da-tools generator-service`，socket 放在只有它與 tenant-api 掛載的記憶體 emptyDir，以唯讀方式讀同一個 conf.d repo、與 tenant-api 同 uid，資源 request＝limit（512Mi／1 CPU，不相等時 render 失敗），repo 設 `gc.auto=0`。`generator.policy.existingConfigMap`／`key` 把部署端 policy 檔（`allowed_domains`）唯讀掛進兩個容器的同一路徑並帶 `--policy`（[#2826](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2826)）。tenant-api 調大 body 上限時，產生器的上限跟著放大。目前預設關：已發布的 `tools/v2.9.0` 映像沒有 `generator-service`，要搭配含它的 `generator.image.tag` 才能開；tenant-api 改呼叫它（[#2903](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2903)）時移除這個開關。chart 版本 2.9.23。
