---
title: "跨租戶 ConfigMap 硬化基線（operator RBAC + off-cluster 稽核）"
tags: [governance, security, audit, rbac, hardening]
audience: [platform-engineer, sre, security]
version: v2.9.0
lang: zh
---
# 跨租戶 ConfigMap 硬化基線 — operator RBAC + off-cluster 稽核

> **Language / 語言：** | **中文（當前）** | [English](./cross-tenant-configmap-hardening.en.md)

> 相關文件：[治理與安全](governance-security.md) · [Tenant API Hardening](api/tenant-api-hardening.md) · [Architecture](architecture-and-design.md)

---

> **這份文件是「建議基線 + 驗證清單」，不是本平台部署的控制。**
> 平台把跨租戶的告警邏輯彙整成 `monitoring` namespace 內的 ConfigMap（`configmap-rules-*`），由 federation-gateway / vector / tenant-api 消費。能竄改這些 ConfigMap 的是**叢集內的 operator persona**——而 operator 是**客戶側**角色：本 repo **不出貨任何 operator-facing Role**（每個 Role / ClusterRole 都是某個 component 自己的 SA：kube-state-metrics / prometheus / assembler / tenant-api-federation / vector）。因此我們能 codify 的是**一份收斂的建議 Role + 一支驗證腳本 + 一段稽核 policy**，交給導入者套用到自己的叢集，而非一個我們替客戶部署的 admission/GitOps 控制。
>
> 本文是 [issue #903](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/903)（RFC，已關閉）三個自足後繼之一（另兩個：[#924](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/924) 撤銷儲存 tamper-evident、[#925](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/925) workload-spec 重導向），對應 [issue #926](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/926)。

## 1. 威脅面與定位

彙整後的跨租戶 ConfigMap 是**單一高價值標的**：一次竄改即可讓一個租戶的告警邏輯汙染到其他租戶（靜默、誤路由、偽造抑制）。攻擊有兩類，對應本文兩個 Part 之外還有第三個放大面：

| 攻擊類 | 手法 | 對應防線 |
|---|---|---|
| 直接竄改 | operator 直接 write 跨租戶 ConfigMap | Part A §2.1 |
| 有效設定竄改（config-integrity ≠ effective-config-integrity）| 不動 ConfigMap，改把消費端 Deployment 的 `volumes[].configMap.name` 指向別的 ConfigMap（[#925](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/925)）| Part A §2.1 第三條 |
| admission 層自我保護破口 | 部署了 ValidatingAdmissionPolicy 想擋竄改，但 operator 能改 VAP 的 Binding → 關掉自己的守門員 | Part A §2.1 第二條 |

**偵測面**：即使預防收斂到位，仍需要一層**從叢集內部無法竄改**的稽核跡（tamper-evident-from-inside）來滿足合規與事後鑑識——這是 Part B。

### 業界框架對映

Part A 與 Part B 分屬每個框架都刻意拆開的兩個控制族：

| 框架 | Part A（RBAC 最小權限）| Part B（稽核與可歸責）|
|---|---|---|
| CIS Kubernetes Benchmark | §5.1 RBAC least-privilege（避免萬用 verb / 資源）| §1.2 / §3.2 audit policy 與 log 匯出 |
| NIST SP 800-53 | AC-6 最小權限 | AU-2 / AU-3 / AU-12 稽核生成 + AU-6 稽核審視 |
| PCI-DSS v4.0 | Req 7 need-to-know | Req 10 logging & monitoring |
| SOC 2 | CC6.1 邏輯存取控制 | CC7.2 異常偵測 |

---

## Part A — operator RBAC 收斂基線（load-bearing prevention）

> ⛔ **先搞懂一件事：Kubernetes RBAC 只有 allow、沒有 deny rule。**
> 原生 RBAC 是**純加成**的——一個主體的有效權限＝所有綁定到它的 Role / ClusterRole 的 rule **聯集**，沒有任何「拒絕」語意（能表達 deny 的是 admission 層，如 ValidatingAdmissionPolicy / OPA / Kyverno，那是更重的 [#903](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/903) deferred 層）。
> 所以本文說的「拒絕 operator 做 X」實際意義是：**確保沒有任何綁定到 operator 的 Role 授予 X**。基線由兩件事組成——(1) 一份**只授予必要讀取**的窄 Role（§2.2）；(2) 一支驗證有效權限**確實不含**危險 grant 的腳本（§2.3）。

### 2.1 三類必須確保「未被授予」的寫入

#### 第一條 — 跨租戶 ConfigMap 的 `create` / `update` / `delete` / `deletecollection`

operator 不得對 `monitoring` namespace 的 `configmaps` 取得這四個寫入 verb。

**RBAC 真實限制（load-bearing）**：`update` / `patch` / `delete` **可以**用 `resourceNames` 縮限到特定物件；但 **`create`、`deletecollection`、`list`、`watch` 無法用 `resourceNames` 限制**——
- `create` 送出時物件還沒有名字，RBAC 無從 match；
- `deletecollection` 作用在整個 collection 上，`resourceNames` 對它無效。

因此若 operator 真的需要寫**自己的**某個 ConfigMap，正解是用 `resourceNames` 白名單那個特定名字的 `update` / `patch`（見 §2.2），而 **`create` 與 `deletecollection` 只能整個不授予**——否則一個 `deletecollection configmaps -n monitoring` 就能清掉全部跨租戶規則，且 RBAC 無法把它擋在「只能刪自己那個」。

#### 第二條 — `admissionregistration.k8s.io` 的寫入

operator 不得對 `validatingadmissionpolicies` 與 `validatingadmissionpolicybindings`（以及 mutating 對應物）取得 `create` / `update` / `patch` / `delete`。（這兩者是**叢集級**資源，故此類授予會落在 operator 的 ClusterRole，而非 §2.2 那份 namespaced Role；§2.3 的驗證用 `kubectl auth can-i` 跨 Role / ClusterRole 一併查有效權限，不受此差異影響。）

**為什麼這條是「讓任何 admission 層黏得住」的根基**：一個 ValidatingAdmissionPolicy **保護不了自己的 Binding**——能改 Binding 的人可以把 `validationActions` 從 `[Deny]` 改成 `[Warn]`、或直接刪掉 Binding，守門員就形同關閉。所以即使未來導入 VAP 來擋 ConfigMap 竄改，**這層 RBAC 才是真正讓它生效的前提**；少了它，VAP 只是一道可被繞過去的裝飾。

#### 第三條 — 消費端 Deployment 的 `patch`

operator 不得對消費跨租戶 ConfigMap 的 Deployment（`federation-gateway` / `vector` / `tenant-api`，實際名稱依 Helm release 而定）取得 `patch` / `update`。

**堵的是 [#925](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/925) 的 workload-ref 重導向**：config-integrity ≠ effective-config-integrity。就算跨租戶 ConfigMap 一個 byte 都沒被動，只要能 `patch` Deployment 的 `spec.template.spec.volumes[].configMap.name`，就能讓消費端**改掛到攻擊者控制的 ConfigMap**，達到等價於竄改的效果，且完全繞過任何只盯著 ConfigMap 物件本身的偵測。

### 2.2 建議的窄 operator Role（least-privilege 樣板）

以下是一份**只讀** ConfigMap 的最小 Role；operator 若還需管理自己的 CRD / 資源，在**另一條** rule 追加，切勿把 configmaps / admissionregistration / 消費端 deployments 的寫入混進來。

```yaml
# 建議基線：operator 對跨租戶 ConfigMap 只讀，不得寫。
# 客戶側套用；實際 CRD 資源依 operator 而定，在另一條 rule 追加。
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: tenant-operator-baseline
  namespace: monitoring   # 跨租戶 ConfigMap 所在的平台 namespace
rules:
  # ── 跨租戶 ConfigMap：只讀，不含任何寫入 verb ──
  - apiGroups: [""]
    resources: ["configmaps"]
    verbs: ["get", "list", "watch"]
  # ── 若 operator 必須寫「自己的」某個 ConfigMap，才加下面這條，
  #    並用 resourceNames 白名單那個特定名字（絕不放 create / deletecollection）──
  # - apiGroups: [""]
  #   resources: ["configmaps"]
  #   resourceNames: ["my-operator-own-config"]
  #   verbs: ["get", "update", "patch"]
  #
  # ⛔ 刻意「不出現」在本 Role（也不得由其他綁定補上）：
  #   - configmaps 的 create / update / patch / delete / deletecollection（未 resourceName 縮限者）
  #   - admissionregistration.k8s.io/* 的任何寫入
  #   - apps/deployments 對 federation-gateway / vector / tenant-api 的 patch / update
```

> **驗收方式不是讀這份 YAML，而是查有效權限**——因為權限是所有綁定的聯集，光看單一 Role 無法保證別處沒補上危險 grant。用 §2.3 的腳本對**活叢集**驗證。

### 2.3 驗證腳本 `scripts/ops/verify_operator_rbac.sh`

對一個活叢集查 operator ServiceAccount 的**有效權限**，逐條確認上述危險 grant 都回 `no`。底層是 `kubectl auth can-i --as=system:serviceaccount:<ns>:<sa>`（server-side 權限求值，會把所有綁定的聯集算進去，比人工讀 Role 可靠）。

```bash
# 針對某叢集的 operator SA 跑驗證（platform-ns 預設 monitoring）
scripts/ops/verify_operator_rbac.sh \
  --operator-sa my-operators:tenant-operator \
  --platform-ns monitoring \
  --deployments "federation-gateway vector tenant-api"
```

- 任一危險 grant 可執行（`can-i` 回 `yes`）→ 腳本印 `VIOLATION:` 並以 **exit 1** 收尾（可直接接 CI / 導入前檢查）。
- 全部收斂 → exit 0。
- **fail-closed**：若無法評估有效權限（叢集不可達／無 `--as` impersonation 權限）→ 明確 abort、exit 1，**不會誤報 PASS**。
- 需要 `kubectl` 對目標叢集有讀取 RBAC 的權限；腳本本身不改動叢集（唯讀）。

> **⚠️ 誠實邊界**：`kubectl auth can-i` 反映的是 **control-plane 的 RBAC 求值**。它抓得到 Role / Binding 授予的權限，但抓不到繞過 API server 的路徑（例如直接改 etcd、或有 node-level 存取）。那些不在 RBAC 的守備範圍，屬 Part B 稽核與更上游的叢集存取控制。

---

## Part B — off-cluster kube-apiserver 稽核（cheap detection）

Part A 收斂了「誰能改」，Part B 回答「改了有沒有人知道」。關鍵是**把稽核跡放到叢集外**——放在叢集內的任何 log 都可能被同一個提權者抹掉，唯有 off-cluster 的 kube-apiserver audit log 才是 tamper-evident-from-inside，也正是 PCI-DSS Req 10 / SOC 2 CC7.2 實際倚賴的東西。本 repo **今日無任何 audit policy**（已驗），故這是淨新增的一層。

> **稽核 vs GitOps self-heal 是兩件事**：[#903](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/903) 的設計 pass 曾把「稽核跡」與「GitOps 自癒」綁在一起一併 defer。它們**可分離**：off-cluster audit log 就是「沒有 ArgoCD 的 git-diff 等價物」——不需要那套重量級 reconcile，就先拿到 tamper-evident 的偵測層。

### 3.1 audit-policy 片段

記錄 `monitoring` namespace 內 `configmaps` 與 `admissionregistration.k8s.io` 的寫入，取 `Metadata` level（記 who / what / when，**不記** ConfigMap 內容——避免把可能敏感的告警設定寫進 audit log）。

```yaml
# kube-apiserver audit policy（節錄）：只針對平台 namespace 的高價值寫入。
# 放到 API server 的 --audit-policy-file，並配 --audit-log-path 或 webhook 匯出（見 §3.2）。
apiVersion: audit.k8s.io/v1
kind: Policy
omitStages:
  - RequestReceived
rules:
  # 跨租戶 ConfigMap 的寫入 → Metadata level（不含內容）
  - level: Metadata
    namespaces: ["monitoring"]
    verbs: ["create", "update", "patch", "delete", "deletecollection"]
    resources:
      - group: ""
        resources: ["configmaps"]
  # admission policy / binding 的任何寫入（叢集級資源，不綁 namespace）
  - level: Metadata
    verbs: ["create", "update", "patch", "delete"]
    resources:
      - group: "admissionregistration.k8s.io"
        resources: ["validatingadmissionpolicies", "validatingadmissionpolicybindings"]
  # 其餘一律不記（本 policy 只加這一層，不改動既有 audit 設定）
  - level: None
```

> **要不要記內容？** `Metadata` 只回答「誰在何時對哪個物件做了什麼」，足以偵測「有沒有 out-of-band 寫入」。若鑑識需要看「改了什麼」，可把第一條升成 `Request` 或 `RequestResponse`——代價是 audit log 會含 ConfigMap body（可能夾帶敏感設定），需相應的 log 保護。預設用 `Metadata` 是隱私與偵測的平衡點。

### 3.2 各雲商匯出指引（實際匯出因平台而異）

audit policy 定義「記什麼」，**「送到哪」是平台相依**的，且必須是叢集外的 sink：

| 平台 | 啟用方式 | Sink |
|---|---|---|
| EKS | Control plane logging 開 `audit`（EKS 用內建 audit policy，見注） | CloudWatch Logs |
| GKE | 預設開啟 admin activity；data-access 需顯式啟用 | Cloud Audit Logs（Cloud Logging）|
| AKS | Diagnostic settings 開 `kube-audit` / `kube-audit-admin` | Log Analytics / Event Hub |
| 自管 kubeadm / k3s | API server 加 `--audit-policy-file` + `--audit-log-path`（file）或 `--audit-webhook-config-file`（webhook）| 外部 log backend（VictoriaLogs / Loki / SIEM）經 log shipper |

> **注（受管平台的取捨）**：EKS / GKE 的 control-plane audit policy 多半**不可自訂**——你拿到的是平台預設全量 audit，本文的 policy 片段主要對**自管 control plane** 直接適用；受管平台則改用「全量 audit + 在 log backend / SIEM 端用查詢過濾出 §3.1 的那兩類事件」達到同樣效果（見 §3.3 的查詢範式）。

### 3.3 out-of-band 寫入告警範式

搭配一條告警：**平台 namespace 內，由「非平台身份」對 `configmaps` 的寫入**。所謂「平台身份」＝合法會寫這些 ConfigMap 的 SA（如 GitOps reconciler、CI deployer）的 allowlist；任何落在 allowlist 之外的寫入即可疑。

> **為什麼是「範式」而非可直接部署的 PrometheusRule**：告警的資料來源是 audit log，而 audit log 的匯出路徑 §3.2 已說明是平台相依的——沒有一條在所有叢集都成立的 metric 來源。硬出一條 PrometheusRule 只會變成到處都不會 fire 的死規則。因此這裡給**查詢範式**，導入者依自己的 §3.2 sink 落地。**本告警刻意不進 `rule-packs/`**（否則會觸發三副本 hard gate 與 platform-alert-count 連動），它是客戶側依環境落地的 guidance。

若你把 audit log 匯進本平台的 log backend（VictoriaLogs），示意查詢（LogQL 風格；欄位名依 audit event schema）：

```logql
# 平台 namespace 內、非 allowlist 身份對 configmaps 的寫入
{job="kube-audit"}
  | json
  | objectRef_namespace="monitoring"
  | objectRef_resource="configmaps"
  | verb=~"create|update|patch|delete|deletecollection"
  | user_username!~"system:serviceaccount:(gitops|ci-deployer):.*"
```

受管平台則把同一邏輯寫成該平台 log 查詢語言（CloudWatch Logs Insights / Cloud Logging query / KQL），並掛到其告警機制。告警語意：**穩態下平台 ConfigMap 只該由 GitOps / CI 這類已知身份寫入**；一旦冒出 allowlist 外的 writer，就是該人工介入的訊號。

---

## 4. 為什麼不改 `rbac-setup-wizard`

[issue #926](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/926) 提到「延伸 rbac-setup-wizard」作為可能掛點，但**經評估不採用**：該 wizard 產出的是 tenant-api 的 **app-layer `_rbac.yaml`**（group / permission / tenant 的 API 授權），與本文的 **K8s cluster-plane operator RBAC（ClusterRole / Role）是完全不同的 RBAC 層**。把 cluster-plane 的 operator 收斂塞進一個產 app-layer 設定的 wizard，只會**混淆兩層 RBAC**、增加誤用風險。兩者的正確關係是「並列的不同層」，故本文明確保持它們分開，不做 wizard 接線。

## 5. 與其他硬化層的關係

- **網路面**：[#962](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/962) header-trust netpol 收斂了跨 pod 的偽造 identity 路徑（L4 network plane），與本文的 RBAC plane 互補。
- **撤銷儲存**：[#924](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/924) 讓 federation token 撤銷儲存 tamper-evident（append-only + hashed）。
- **workload-ref**：[#925](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/925)（defer-with-trigger）的重導向向量，本文 §2.1 第三條先以 RBAC 收斂 patch 權限作為便宜的第一道。
- **重量級預防（deferred）**：VAP / OPA / Kyverno 的 admission-time 阻擋、與 GitOps self-heal，仍循 [#903](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/903) 的 activation triggers；本文兩層（RBAC 收斂 + off-cluster 稽核）是**趕在那些 trigger 前先落地的便宜基線**，且 Part A 是其餘一切預防所依賴的地基。

## 6. 相關文件 + issue

- [治理與安全](governance-security.md) — 平台整體治理 / 稽核 / 合規總覽（本文為其 operator-RBAC + 稽核的深水章）
- [Tenant API Hardening](api/tenant-api-hardening.md) — tenant-api **app-plane** 硬化（與本文 cluster-plane 分層）
- issue：[#926](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/926)（本文）· [#903](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/903)（母 RFC）· [#924](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/924) · [#925](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/925)
- 驗證腳本：`scripts/ops/verify_operator_rbac.sh`
