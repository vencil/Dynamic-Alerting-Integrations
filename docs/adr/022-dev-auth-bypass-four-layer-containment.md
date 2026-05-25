---
title: "ADR-022: tenant-api Dev-Auth Bypass — Local-Dev Identity Substitute, Four-Layer Containment"
tags: [adr, tenant-api, security, developer-experience, try-local]
audience: [platform-engineers, contributors]
version: v2.8.1
lang: zh
id: ADR-022
tracking_kind: adr
status: accepted
domain: tenant-api
created_at: 2026-05-25
updated_at: 2026-05-25
---
# ADR-022: tenant-api Dev-Auth Bypass — Local-Dev Identity Substitute, Four-Layer Containment

## 狀態

✅ **Accepted**（v2.9.0 起草，2026-05-25）。實作 [#464](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/464)（epic #449 原 onboarding-ADR 子題，重定位為本 ADR 的 tracker）。由 epic [#449](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/449) 的 try-local Mode 0（portal + tenant-api 核心雙星）浮現需求。設計經 brainstorm + 自審（loopback layer 衝突修正）。

## 背景

tenant-api 信任 oauth2-proxy 注入的 `X-Forwarded-Email` / `X-Forwarded-Groups` header 作為身分來源（`rbac/middleware.go`：無 email → 401；group 比對 `_rbac.yaml`）。production 由 oauth2-proxy 在前面注入。

但 try-local 的 **Mode 0 核心雙星**（`docker compose up da-portal tenant-api`，showcase 的中心）**沒有 oauth2-proxy**：portal 容器經 compose 網路打 tenant-api，瀏覽器無法注入 `X-Forwarded-*` → `/api/v1/me` 回 401、RBAC 全拒 → 旗艦 Tenant Manager 開不出來。

需要一個「本機 dev 的身分替身」。但**在 production binary 裡放 auth bypass 本身就是資安風險**——這正是本 ADR 要謹慎處理的。

## 決策

新增 `--dev-bypass-auth` / `TA_DEV_BYPASS_AUTH` flag（**預設 off**）。啟用時，一個外層 middleware 在請求**缺 `X-Forwarded-Email` 時**注入 dev 身分（email + groups，預設 `dev@local` / `demo-admins`）；**有真實 forwarded 身分時絕不覆蓋**。

**identity-only，非 RBAC 繞過**：注入身分後 **RBAC 照常 enforce**（針對注入的 group），不給 god-mode——注入 group 的權限仍來自 `_rbac.yaml`（try-local seed 提供）。即使被啟用，blast radius 也只限注入 group 的 RBAC 範圍。

把「prod binary 帶 auth-bypass」的風險用**四層防線**圍住（對齊專案既有四層防線文化）：

| 層 | 機制 | 防什麼 |
|---|---|---|
| **L1 預設 off** | flag 預設 false；middleware 只在 flag on 時 mount（off = 零行為改變、零開銷） | 不主動啟用 |
| **L2 可觀測 tripwire** | 每個 response 帶 `X-Dev-Auth-Bypass: active` header + `/metrics` gauge `tenant_api_dev_auth_bypass_active 1` + startup loud WARN | **任何環境**的監控/proxy/curl 都能偵測「bypass 在 prod 開著」——包括 L3 偵測不到的非 k8s prod |
| **L3 runtime poison pill** | startup 若 flag on 且偵測 `KUBERNETES_SERVICE_HOST` 或 `/var/run/secrets/kubernetes.io` → **panic**（fail-closed） | k8s production（auth bypass 絕不該在叢集內跑） |
| **L4 deploy-time SAST** | `check_dev_bypass_manifest.py` HARD block `TA_DEV_BYPASS_AUTH` / `--dev-bypass-auth` 出現在 helm/ k8s/ operator-manifests（pre-commit + CI `Lint` job） | 在引入它的 PR 階段就擋下；涵蓋 L3 看不到的非 k8s manifest |

L1-2 防君子與意外（含非 k8s prod 由監控抓）、L3-4 防小人與誤佈署。

## 為什麼不用其他方案

- **為什麼不用 nginx header-injection（掛載 nginx.conf 注 header）？** Windows/WSL2 單檔掛載易觸發 `invalid mount config` / 檔案鎖定（try-local 的 Windows 用戶會踩）。dev-bypass 由 tenant-api 自己消化，不需 portal 端掛載。
- **為什麼 L2 不是「loopback bind only」？**（原始四層設計曾用此。）實作時自審發現它**與 Mode 0 compose 自相矛盾**：portal 容器需經 compose 網路（非 loopback）連 tenant-api，且 `-p` port-forward 也要求 bind `0.0.0.0`——loopback-only 會讓 dev-bypass 服務不了它唯一的用途。改用**可觀測 tripwire**：不衝突 compose，且補上 L3「只擋 k8s」的非 k8s-prod gap（監控可在任何環境抓到）。
- **為什麼 identity-only 而非完整 RBAC 繞過？** 縮小 blast radius——即使啟用，也只拿到注入 group 的 RBAC-scoped 權限，不是 god-mode。bypass 的是「上游身分 proxy」，不是「授權」。

## 實作

- middleware + k8s guard：`internal/rbac/devbypass.go`（`DevBypassMiddleware` / `DevBypassK8sGuard` / `InKubernetes`）。
- flag + L3 guard + L2 warn + 掛載：`cmd/server/main.go`。
- L2 `/metrics` gauge：`internal/handler/metrics.go`（`SetDevBypassActive`）。
- L4 SAST：`scripts/tools/lint/check_dev_bypass_manifest.py` + pre-commit `dev-bypass-manifest-guard` + ci.yml `Lint`。
- 測試：`internal/rbac/devbypass_test.go`（注入 / 不覆蓋真身分 / tripwire header / k8s panic）+ `tests/lint/test_check_dev_bypass_manifest.py`。

## 後果

### 正面
- try-local **Mode 0 portal-live（showcase 中心）解鎖**——portal 在 compose 用 published image 即活。
- 唯一須進 `docs/adr/` 的重大平台決策落地、可追溯。

### 負面 / 取捨
- **production binary 帶一個 auth-bypass flag**——以四層防線圍住（L3 panic 在 k8s 不可繞、L4 SAST 擋 manifest）。仍是須持續審視的 surface。
- **L3 只擋 k8s**：非 k8s prod（VM/bare-metal）L3 不觸發、L4（manifest）也不經過 → 由 **L2 tripwire（監控）** backstop；本平台 prod 以 k8s 為主，殘留風險低。

## 關聯
- [#464](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/464)（本 ADR 的 tracker）；epic [#449](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/449) Mode 0；[#448](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/448) IaC SAST（L4 之家）。
- try-local Mode 0 / tenant-api QUICKSTART（[#466](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/466)）寫回 demo 以此 flag + seed `_rbac.yaml` 驅動。
