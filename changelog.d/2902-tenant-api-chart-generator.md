---
section: Added
topic: tenant-api
issues: [2902, 2826, 2858]
created: 2026-10-15T02:26:01+00:00
---
- **tenant-api chart：產生器容器（ADR-036 §3／§5；[#2902](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2902)、[#2903](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2903)）**：pod 多一個 da-tools 容器跑 `da-tools generator-service`，tenant-api 每筆寫入前問它（`--generator-socket`）；必備、沒有開關。socket 放在只有它與 tenant-api 掛載的記憶體 emptyDir，以唯讀方式讀同一個 conf.d repo、與 tenant-api 同 uid，資源 request＝limit（512Mi／1 CPU，不相等時 render 失敗），repo 設 `gc.auto=0`；conf.d 沒有 git repo（emptyDir 又沒設 `gitRepoUrl`）時 render 失敗。`generator.policy.existingConfigMap`／`key` 把部署端 policy 檔唯讀掛進兩個容器的同一路徑（產生器 `--policy`、tenant-api `--deploy-policy`；[#2826](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2826)）。移除 `policy.unavailableOpen`。⚠️ 預設映像 `tools/v2.9.0` 沒有 `generator-service`，要把 `generator.image` 指到含它的映像（設了 `digest` 時以 digest 為準，tag 與 digest 都要指向含它的映像），下一個 tools 版本會一起調升。chart 版本 2.9.24。
