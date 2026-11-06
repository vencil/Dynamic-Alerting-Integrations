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

## 0. 現況：實驗叢集全部 enforce

每個 app-tier namespace（`db-a`、`db-b`、`monitoring`、`tenant-api`）
**enforce `restricted`**；`vector` 是 carve-out，enforce `privileged`（Vector
DaemonSet 的 3 個 hostPath 連 baseline 都禁，見
`k8s/00-namespaces/namespace-vector.yaml` 與
[platform-log-aggregation-runbook §1.1](platform-log-aggregation-runbook.md)）。

⛔ **不要在這裡維護一張 namespace 對等級的表**：權威是
`k8s/00-namespaces/*.yaml` 本身，`tests/pss` 的 `TestLabNamespacesEnforce`
守住三件事：每個 Namespace 都有 `enforce`；等級等於同名 chart 在
`Chart.yaml` 宣告的等級（沒有宣告就是 `restricted`）；`enforce-version`
等於 kind node image 的 minor（`scripts/ops/kind-node-image.env`）。

`warn`／`audit` 刻意維持不帶版本的 latest 形式：它們要在新標準被 enforce
之前先回報偏移。

`check_k8s_manifests.py`（Container SAST L4）另有一條 informational 規則，
檢查每個 Namespace 都帶 `warn` 與 `audit`；它不擋 merge，enforce 由上面的
測試守。

### 0.1 CI 閘門：每個出貨的 pod 都過得了它宣告的等級

namespace label 只保護本 repo 自己的實驗叢集；客戶的 namespace 由客戶自己
label。對客戶有意義的保證是「我們出貨的每個 pod 都能在 `restricted` 下
跑」，這由 `tests/pss` 機械化守住（CI 的 `Go Tests — Pod Security gate`，
併入必要檢查 `Go Tests`）：

- **判準是上游程式碼**：以 `k8s.io/pod-security-admission`（API server 的
  PodSecurity 外掛就是用它建的）判定，不自寫規則。版本取自
  `scripts/ops/kind-node-image.env` 的 minor，go.mod 的函式庫版本由測試釘住
  與它一致。
- **判定範圍**：每個 chart 以預設值（能渲染時）與 `helm/<chart>/ci/*.yaml`
  各渲染一次；`k8s/` 底下每個帶 pod spec 的 manifest。
- **例外只在一個地方宣告**：chart 的 `Chart.yaml` 加
  `annotations: {dynamic-alerting.io/pod-security-level: <level>}`；沒有就是
  `restricted`。目前只有 `helm/vector` 宣告 `privileged`。
- **chart 提供、但跑不了 restricted 的設定**：在 `ci/` 放一個檔名以
  `pss-needs-privileged-` 開頭的值檔，以 privileged 判定，而且測試會要求它
  **確實**違反 restricted；哪天這個設定變得合規或被拿掉，閘門轉紅、檔案要刪。
  目前只有一個：tenant-api 的 `confDir.type=hostPath`（文件寫明是 kind 開發
  用），它連 baseline 都過不了，不能裝在 enforce restricted 的 namespace。
- **條件分支要被打開**：workload template 與 `.tpl` helper 裡每個 `if`／
  `with`／`range`（以及 `$x := …` 指派）讀到的 `.Values` 路徑，必須在某個
  受判定的值組裡是開的；寫在 `not` 後面的，還要有一組是關的。`fail`／
  `required` 防呆的否定分支不算（它不渲染任何 pod）。例外值檔不算覆蓋。
- **預設值渲染不出來**：只有列在 `defaultsDoNotRender` 的 chart 可以（目前
  tenant-api，`writeBack.mode` 刻意沒有預設），而且錯誤訊息要對得上；清單
  裡的 chart 若預設值又能渲染，測試要求把它移出清單。

本地跑：`make dc-go-test MOD=pss`（或在有 Go 與 helm 的環境 `cd tests/pss &&
PSS_REQUIRE_HELM=1 go test ./...`）。

**判定不到的範圍**：`eq .Values.mode "x"` 這類列舉分支只會判到值檔選中的
那幾個；`.Values` 經 `$var :=` 以外的方式取別名、或在 `with` 裡以 `.` 讀
的分支；客戶叢集的 mutating webhook（在 admission 之前改 pod，例如 Istio
的 init container）；以變數組出的指令（`K=kubectl; $K run …`）；以 `printf`
等方式產生、無法對到 `kind:` 行數的 manifest 會讓測試轉紅而不是放行。

**執行期才建的 pod 也有判定**（`tests/pss/lab_test.go`）：`scripts/**/*.sh`
heredoc 裡的 manifest（帶 pod 的 `kind:` 行數必須等於判定筆數）；
`docs/internal/**`、`components/*/README.md` 裡的 `kubectl run`（套用
`--overrides` 後判定）與 `kubectl debug`（必須 `--profile=restricted`）；
`docs/internal/examples/**` 的 manifest。

**`ci/` 不出貨**：四個出貨 chart 的 `.helmignore` 排除 `ci/`；
`tests/lint/test_chart_package_contents_at_pr.py` 在 PR 階段就 `helm package`
並跑發版時那支內容檢查。

## 1. Step 0 — 探測（非破壞，隨時可跑）

```sh
# server-side dry-run 全 ns 掃 violator：不落任何 label，apiserver 回
# admission 對每個 ns 現存 workload 的 would-warn 清單。
kubectl label --dry-run=server --overwrite ns --all \
  pod-security.kubernetes.io/enforce=restricted
```

預期輸出：只有 Vector DaemonSet（hostPath ×3、root、DAC_READ_SEARCH）。出現**預期外** violator → 先記
issue、修掉或 carve，**不要**急著開豁免。

## 2. Step 1 — warn+audit soak（歷程；新 namespace 仍可沿用）

**soak 窗口 ≥ 8 天**，理由：violation 只在 **pod 建立時**被 admission
評估，長駐 Deployment 不重排就不觸發——窗口必須涵蓋所有「會建新 pod 的
排程」完整跑過一輪：

- 8 天下限原本來自週跑的 `threshold-govern` CronJob；它已依 [ADR-027](../adr/027-tenant-api-caller-identity.md) §4 移除，下限沿用，涵蓋週期性 deploy 與手動作業
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

## 3. Step 2→3 — flip enforce（已完成，#1018）

實驗叢集已全部 flip（§0）。依據：Step 0 在 v1.37.0 apiserver 上部署
`k8s/**` 與全部 chart 後，唯一違規是 `vector` ns 的 Vector（宣告內的
carve-out），且負向對照（monitoring 放一個 `NET_RAW` pod）有被抓到；另外
§0.1 的 CI 閘門以同一份 admission 程式碼持續判定每個 chart。

flip 時一併處理、之後仍適用的規則：

1. ⛔ **不可 blanket-exempt 整個 monitoring**，會連 prometheus／grafana／
   KSM 一起解保護。需要更寬等級的 workload 依 Vector 前例 carve 到專屬
   ns，並在它的 chart `Chart.yaml` 宣告等級（§0.1）。
2. **`enforce-version` 釘版**，確保 k8s 升版讓 restricted 定義收緊時不會
   在升版當下無預警開始 reject；版本跟著 kind node image 走（§0）。
3. **執行期才建的 pod 也要符合**：`scripts/run_load.sh` 的 Job／Pod 與
   `docs/internal/**` 的 `kubectl run` debug pod 都帶 restricted
   securityContext（debug pod 用 `--override-type=strategic`，以免取代掉
   `--` 之後的參數），由 `tests/pss` 判定。

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
