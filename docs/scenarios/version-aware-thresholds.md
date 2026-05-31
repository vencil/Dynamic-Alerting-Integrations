---
title: "Version-Aware Thresholds — 版本感知閾值使用攻略"
tags: [scenarios, version-aware, threshold, rolling-upgrade, kubernetes, lifecycle]
audience: [tenant-admins, platform-engineers, sre]
version: v2.8.1
lang: zh
---

# Version-Aware Thresholds — 版本感知閾值使用攻略

> **這份在講什麼**：app 做滾動升版時，新舊版本會同時在跑，但你想給 **v2 設更嚴的閾值**、v1 維持原樣，且升版完成後 cutover 自動發生。本攻略教租戶怎麼宣告、教平台維運怎麼確保它真的生效。
>
> 設計決策與取捨見 [ADR-024](../adr/024-version-aware-threshold-via-dimensional-label.md)。

## 這份是寫給誰的？（先對號入座）

| 你是 | 你要看 |
|---|---|
| **租戶 / 應用團隊**（要設版本專屬閾值） | §1–§4：宣告語法、滾動行為、動態降級、⛔ 適用範圍 |
| **平台維運 / SRE**（收到 `VersionAwareThresholdInert` 告警，或要讓功能生效） | [§For Platform Operators](#for-platform-operators-ksm-allowlist-remediation)：KSM allowlist 搶修 |

---

## 1. 宣告一個版本專屬閾值（租戶）

在你**平常管理閾值的地方**（da-portal / `PUT /api/v1/tenants/{id}` / GitOps，最終落到 `conf.d/<tenant>.yaml`），用 **dimensional `version` label** 語法新增閾值 key——寫法和既有閾值一樣，只是 key 帶上 `{version="..."}`。下例：v1 維持 80、v2 收緊到 60：

```yaml
tenants:
  db-a:
    container_cpu{version="v1"}: "80"
    container_cpu{version="v2"}: "60"      # 新版收緊
    container_memory{version="v2"}: "50:critical"   # 也支援 值:severity
```

- 透過 tenant-api 寫入時，無效的 `version`（大寫、regex、空、保留字 `default`、非-pilot metric）會在**寫入當下被拒絕並回錯誤**（不是事後才發現）。
- 字元集：`^[a-z0-9][a-z0-9._-]*$`（對齊 `app.kubernetes.io/version` 實務值）。
- **怎麼知道生效**：v2 pod 超標時，告警會帶上你宣告的 `version="v2"` label，on-call 一眼可辨版本。⚠ 前提是平台已正確設定 KSM（見 §4 與平台搶修段）——否則閾值靜默失效。

## 2. 滾動升版時會怎樣（emergent cutover）

cutover 不是你按一個鈕，而是 **emergent**：pod 升到 v2 後，它的 metric 自動帶 `version="v2"`，就 join 到 v2 的閾值。滾動期間 v1/v2 pod 並存，**各自比各自版本的閾值**——v1 pod 比 80、v2 pod 比 60，互不干擾。升完版 v1 pod 消失，v1 閾值自然不再觸發。你不需要在升版那一刻同步改 config。

## 3. 動態降級（沒設版號的版本怎麼辦）

如果某個跑著的版本（例如 v3）你**沒有**宣告專屬閾值，它**自動 fallback 到未版號 / `version="default"` 的閾值**——不會出現「新版上線但沒人盯」的 silent gap。而且告警會**保留 metric 的真實版號**（`version="v3"`），on-call 一眼看出是哪個版本在燒。

## 4. ⛔ 適用範圍（Phase 1 Pilot — 很重要，先看）

版本感知目前是 **Kubernetes pilot**，**只有兩個 metric 吃 `version` label**：

- ✅ `container_cpu`、`container_memory`
- ❌ 其他任何 metric（`redis_*`、`pg_*`、`mysql_*`…）寫 `{version="..."}` 會被 **da-guard 直接拒絕**（非-pilot metric，避免跨 pack 基數污染）。

> **silent-inert 風險（務必知道）**：版本感知靠 kube-state-metrics 暴露 pod 的版號 label。**若平台的 KSM 沒開對應 allowlist，你宣告的版本閾值會靜默失效**（所有 pod 被當成 `default`、你的 v2 嚴閾值不生效），而**你這端不會收到直接回饋**。所以：
>
> 1. 第一次採用版本閾值前，**先與平台團隊確認 KSM 已設定**（見下方平台段）。
> 2. 平台側有 runtime 安全網 `VersionAwareThresholdInert` 告警會在誤配時通知平台團隊。

---

## For Platform Operators: KSM Allowlist Remediation

> **此段為平台維運 / SRE。** 若你是被 `VersionAwareThresholdInert` 告警導到這裡：**這不是租戶的 YAML 寫錯，是 kube-state-metrics 沒暴露 pod 版號 label**，導致全平台的版本感知閾值靜默失效。以下是搶修步驟。標題保持英文 + 穩定，因為告警的 `runbook_url` 直連此 anchor（勿改名）。

### 根因

ADR-024 (0a) 的版號注入靠 `kube_pod_labels{label_app_kubernetes_io_version="..."}`。但 **kube-state-metrics 預設不暴露任何 pod label**（`kube_pod_labels` 連 series 都沒有），必須用 `--metric-labels-allowlist` 顯式開啟。真叢集實證：預設 KSM → 零 version label → (0a) join 匹配空集 → 每個 pod 靜默 fallback `default`。

### 搶修（k8s）

確認並修正 KSM 部署的 args：

```bash
# 1. 看 KSM 現在有沒有 allowlist
kubectl -n monitoring get deploy kube-state-metrics -o jsonpath='{.spec.template.spec.containers[0].args}'

# 2. 沒有 app.kubernetes.io/version 的話，補上（本 repo 的 deployment 已內建此 arg）
#    args 應包含：
#    --metric-labels-allowlist=pods=[app.kubernetes.io/version]

# 3. 驗證修好（應看到 label_app_kubernetes_io_version="vN"）
kubectl -n monitoring port-forward svc/kube-state-metrics 8080:8080 &
curl -s localhost:8080/metrics | grep -E 'kube_pod_labels\{.*label_app_kubernetes_io_version'
```

repo 內 `k8s/03-monitoring/deployment-kube-state-metrics.yaml` 已內建此 arg，並有 static lint `check_ksm_version_allowlist.py` 在 CI 攔截誤配。可重現的真叢集驗證腳本見 repo 的 `test/rulepack-e2e/run.sh`。

### 已知邊界

`VersionAwareThresholdInert` 抓的是「KSM 完全沒暴露 pod label」。若 allowlist 設了但**只含別的 label**（沒有 `app.kubernetes.io/version`），runtime sentinel 不會 fire——這個 partial-misconfig 由 CI 的 static lint 負責攔（部署前）。
