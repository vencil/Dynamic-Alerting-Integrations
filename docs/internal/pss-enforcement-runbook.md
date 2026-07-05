---
title: "Pod Security Standards Enforcement Runbook"
tags: [internal, runbook, security, k8s, admission]
audience: [platform-engineer, sre, maintainers]
version: v2.9.0
lang: zh
---

# Pod Security Standards（PSS）Enforcement Runbook

[#1018](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1018)
的 phased rollout 操作手冊。背景：#962 信任硬化 arc 發現 machine-identity
token 以明文走 CNI，同節點 `CAP_NET_RAW` / `hostNetwork` pod 可嗅探；root
mitigation 不是 app-TLS，而是用 PSS admission 讓「rogue tenant deployment
要求 NET_RAW / hostNetwork」直接進不來（研究 grounding 見 PR #1009 討論串）。

**前置需求**：PSA（Pod Security admission）需 **K8s ≥1.23**（beta，預設開）
／**≥1.25**（GA）；victorialogs netpol 的 `namespaceSelector` 依賴 **≥1.21**
的 `kubernetes.io/metadata.name` auto-label——PSA 前提已涵蓋，ns manifest
無需（也不應）手寫該 label。

## 0. 現況（PR-1 已落地 = 本 repo 的 soak 基線）

| Namespace | enforce | warn / audit | 說明 |
|---|---|---|---|
| `db-a` / `db-b` | —（無） | `restricted` | mariadb chart 0.1.2 已補 `runAsNonRoot` + `runAsUser/Group: 999`（restricted-ready） |
| `monitoring` | —（無） | `restricted` | app-tier（prometheus / grafana / alertmanager / KSM / da-portal / threshold-exporter / victorialogs / chargeback…）；soak 期蒐集 warn |
| `tenant-api` | —（無） | `restricted` | tenant-api workload 已達 restricted（drop ALL + runAsNonRoot + seccomp） |
| `vector` | **`privileged`** | `privileged` | **carve-out**：Vector DaemonSet 3 個 hostPath 連 baseline 都禁；獨立 ns 隔離爆炸半徑（見 `k8s/00-namespaces/namespace-vector.yaml` 註解與 [platform-log-aggregation-runbook §1.1](platform-log-aggregation-runbook.md) 遷移步驟） |

配套 guardrail：`check_k8s_manifests.py`（Container SAST L4）內建
**informational** 規則——`k8s/` 下所有 `kind: Namespace` 必帶
`pod-security.kubernetes.io/warn` + `audit` 兩個 label、值 ∈
{privileged, baseline, restricted}；`enforce` 選配（帶了就驗值）。WARN
級不擋 merge（soak 紀律：不回溯攔在飛 PR；enforce flip 時再議升級）。

⛔ **warn/audit 故意用不帶版本的 latest 形式**——soak 期要抓的是「對最新
標準的偏移」；**pin minor 版是 enforce flip 那一步才做的事**（見 §3）。

## 1. Step 0 — 探測（非破壞，隨時可跑）

```sh
# server-side dry-run 全 ns 掃 violator：不落任何 label，apiserver 回
# admission 對每個 ns 現存 workload 的 would-warn 清單。
kubectl label --dry-run=server --overwrite ns --all \
  pod-security.kubernetes.io/enforce=restricted
```

預期輸出：只有 Vector DaemonSet（hostPath ×3、root、DAC_READ_SEARCH）
與尚未修完的 monitoring app-tier 元件。出現**預期外** violator → 先記
issue、修掉或 carve，**不要**急著開豁免。

## 2. Step 1 — warn+audit soak（本 PR 起算）

**soak 窗口 ≥ 8 天**，理由：violation 只在 **pod 建立時**被 admission
評估，長駐 Deployment 不重排就不觸發——窗口必須涵蓋所有「會建新 pod 的
排程」完整跑過一輪：

- `threshold-govern` CronJob：**weekly**（`0 3 * * 1`，週一 03:00）→ 8 天下限的來源
- `maintenance-scheduler` CronJob：`*/5 * * * *`（頭一小時就覆蓋）
- 至少一次正常 deploy / rollout（CI 出版節奏即可）

**觀察點**：

```sh
# a) 即時 warn（操作者視角）：任何 kubectl apply/rollout 時 stderr 的
#    "Warning: would violate PodSecurity ..." —— CI deploy log 也會有。
# b) audit annotations（叢集視角）：apiserver audit log 中
#    pod-security.kubernetes.io/audit-violations annotation。
# c) 每 ns 快速重掃（等價 Step 0，但 per-ns 好讀）：
kubectl label --dry-run=server --overwrite ns monitoring \
  pod-security.kubernetes.io/enforce=restricted
```

**soak 通過條件**：整個窗口內零**非預期** warn/audit 事件（Vector ns 的
privileged 是宣告內、不算）。

## 3. Step 2→3 — 修 violator、flip enforce（follow-up PR，不在本 PR）

1. 修掉 soak 抓到的 violator（或依 Vector 前例 carve 到專屬 ns——⛔
   **不可 blanket-exempt 整個 monitoring**，會連 prometheus/grafana/KSM
   一起解保護）。
2. **flip 時才 pin 版本**：`enforce=restricted` + `enforce-version:
   v1.<叢集當前 minor>`——確保未來 k8s 升版讓 restricted 定義收緊時，
   不會在升版當下無預警開始 reject；warn/audit 維持 latest 繼續抓偏移。
3. 順序：**app ns 先**（`db-a` / `db-b` / `tenant-api`），`monitoring`
   確認 Vector 已完成搬遷（§0 表格）+ 自身 app-tier 清乾淨後最後上。
4. L4 lint 的 PSS 規則屆時同步把 `enforce` 從 optional 改 required
   （對已 flip 的 ns），severity 是否升級同回合再議。

### Flip 後的日常操作注意

- **RS-level rejection 觀察點**：enforce 後 violation 的 reject 發生在
  **ReplicaSet 建 Pod** 那一步——`kubectl apply` 一個違規 Deployment
  **會成功**（物件寫入 etcd），rollout 卻永遠不前進。查法：
  `kubectl get events -n <ns> --field-selector reason=FailedCreate` +
  `kubectl describe rs <rs>`（訊息含 "violates PodSecurity"）。**別只盯
  deploy status**，那裡只看得到 progressing timeout。
- **`kubectl debug` 檢修**：restricted ns 內 ephemeral debug container
  預設 profile 會被擋 → 用 `kubectl debug --profile=restricted`；需要
  privileged 檢修（strace / 網路抓包）→ 到 node 層或 `vector` ns 做，
  不要臨時解 app ns 的 label（真不得已要解，記 audit + 事後復原）。

## 4. Cluster-default（self-managed 叢集的 operator 選配）

`AdmissionConfiguration` 可讓**未來新建而忘了 label 的 ns** fail 到
baseline 而非全開：

```yaml
# ⚠️ 這是 kube-apiserver 的 --admission-control-config-file，不是叢集內
# 資源 —— 不能 kubectl apply、不隨本 repo manifests 出貨；managed
# cluster（EKS/GKE/AKS）根本碰不到 apiserver flags。
apiVersion: apiserver.config.k8s.io/v1
kind: AdmissionConfiguration
plugins:
  - name: PodSecurity
    configuration:
      apiVersion: pod-security.admission.config.k8s.io/v1
      kind: PodSecurityConfiguration
      defaults:
        enforce: baseline
        audit: restricted
        warn: restricted
      exemptions:
        namespaces: [kube-system]
```

本 repo 的替代防線（managed cluster 也有效）：L4 lint 的「所有
Namespace manifest 必帶 warn+audit label」規則（§0）——新 ns 沒 label
進不了 review 而不自知。

## 5. 誠實殘留（scope 界線，抄自 #1018，flip 後仍成立）

namespace-scoped PSS 是 **admission guardrail、非 node/network 控制**：
未 label / exempted ns 的 pod 仍可排到同節點嗅探；cluster-admin 與
privileged controller 不受限。PSS 可靠擋住「rogue tenant deployment
請求 NET_RAW / hostNetwork」（正是 #962 的目標威脅），但**不保證**節點
上不存在任何 NET_RAW pod——全封同節點竊聽需 node-pool 隔離或不傳明文
（encryption-in-transit defer-with-trigger 的範疇，見 #1009 討論）。
