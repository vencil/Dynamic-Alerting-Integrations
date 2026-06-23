---
name: vibe-subagent-review
description: IaC-aware 兩階段 review — code 走 spec→quality、IaC 走 blast-radius。Use after a multi-file PR or an `Agent` implementation run, before commit — 特別是改動含 Helm values / .gotmpl / Prometheus rules / VRL transforms（這類「爆炸半徑優先」非單純 code quality）。補 #448 機械 SAST 抓不到的 cross-file cascade（改 selector 連動 NetworkPolicy / ServiceMonitor / ConfigMap 等）。SKIP if change is single-file doc-only or single-file test-only.
---

# vibe-subagent-review — IaC-aware blast-radius review

兩階段 review 的副檔名路由：**code 走 spec→quality，IaC 走 blast-radius**。

機械層的單檔 SAST 由 [#448](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/448)（hadolint / kube-linter / trivy config）+ pre-commit 顧；本 skill 顧**機械 lint 抓不到的跨檔語義 cascade**——「改 A 檔讓 B 檔語義錯位」這類需語境推理的問題。owner 分類見 [`hook-vs-skill-coverage.md`](../../../docs/internal/hook-vs-skill-coverage.md)。

## 何時觸發 / 何時跳過

- **觸發**：multi-file PR、或 `Agent` 跑完 implementation 後、`git commit` 前。
- **跳過**：單檔 doc-only / 單檔 test-only（無 cascade 風險，直接走一般 review）。

## 副檔名路由

| 改動檔 | review lens | 核心問題 |
|---|---|---|
| `.go` / `.py` | **Spec → Quality**（兩階段） | (1) 符合 issue spec？(2) 錯誤處理 / 邊界 / 測試覆蓋？ |
| `values.yaml` / `*.gotmpl` / `Chart.yaml` | **Blast Radius** | selector / RBAC / NetworkPolicy / ConfigMap 連動？ |
| `.vrl` / Vector transform | **Schema cascade** | 下游 SIEM payload field 改了哪些？接收端要通知？ |
| Prometheus rules（recording / alerting） | **Cardinality + Severity** | cardinality 暴增？severity 動到 dedup / Sentinel / 四層路由？ |

## Spec → Quality（`.go` / `.py`）

兩階段，分開跑（避免「code 漂亮但沒做對事」）：

1. **Spec 符合度**：對照 issue / ticket，做的是不是「要做的事」？範圍有無 over/under？
2. **Code quality**：錯誤處理、邊界、並發、測試 seam（用對 `freshMetrics` / FakeClock，見 [`test-map.md`](../../../docs/internal/test-map.md)）、tenant-agnostic（dev-rule #2）。

> **Go `Close()` 讀/寫不對稱**（review 必查，errcheck 分不出）：`defer func(){ _ = x.Close() }()` 只對 **read-closer** 安全（`resp.Body` / `sql.Rows` / `os.Open` 唯讀檔——關閉只釋放資源）。**write-closer**（`os.Create` / `gzip.Writer` / 自訂 `io.WriteCloser`）的 `Close()` error **不可吞**——寫入的 disk-flush 常延到 `Close()` 才發生，吞掉 = silent data loss。
>
> 盲區：自訂介面（如 `GetStorage() TenantStateStorage`，內嵌 `io.WriteCloser`）AI/review 缺全域 context 判不出讀/寫，易把 `_ = store.Close()` 誤當資源釋放放行。**正規防禦 = named return + defer 捕捉**（一眼可辨、且擋 panic / early-return 漏判，不靠判斷讀/寫）：
>
> ```go
> func WriteTenant() (err error) {
>     f, err := os.Create(p)
>     if err != nil { return err }
>     defer func() { err = errors.Join(err, f.Close()) }() // disk-flush 錯誤必上傳
>     // ... 寫入 ...
>     return nil
> }
> ```
>
> （來源：#912 + #914 對抗 review）

## Blast-radius checklist（`values.yaml` / template）

改 Helm values / template 時逐項問：

- [ ] **label / selector 改了** → 哪些 Service / ServiceMonitor / NetworkPolicy / Prometheus relabel 跟著要改？（漏改 = metric 靜默斷採集）
- [ ] **resource / replica / PVC 改了** → 容量 / scheduling / PDB / HPA 影響？
- [ ] **securityContext / capabilities.add 改了** → 有 rationale 註解嗎？（#448 Mode B 要求）
- [ ] **新增 ConfigMap / Secret key** → consumer 端 mount / envFrom 對齊？
- [ ] **subchart enabled flag** → RBAC / CRD / namespace / 依賴 chart 連動？

## Schema-cascade checklist（`.vrl` / transform）

- [ ] **改 / 刪 field** → 下游消費者（SIEM / dashboard / alert rule）哪些依賴它？
- [ ] **rename** → 有無相容過渡（雙寫 / alias），或需同步改下游？
- [ ] **型別變更**（string→int、scalar→array 等）→ 下游 parser / schema 會不會炸？

## Cardinality + Severity checklist（Prometheus rules）

- [ ] **新增 label / 動 label 來源** → cardinality 估算，需不需 Cardinality Guard opt-in（dev-rule #8）？
- [ ] **改 severity** → 動到 Severity Dedup / Sentinel / 四層路由 哪一層？（見 architecture-and-design 設計概念）
- [ ] **改 recording rule 名** → 下游 alerting rule / dashboard 引用是否同步？

## Worked examples（範式；具體案例隨真實觸發累積）

> 初版列**結構範式**而非 fabricated PR 引用（避免假造）；真實 worked example 會隨 skill 觸發逐步補入。

- **`.go`**：tenant-api 新增 handler → Spec：是否含 tenant-scoped authz？Quality：async 路徑是否用 `pollUntilTerminal` 取代 blind sleep（TRK-224 pattern）？
- **Helm values**：改 `victorialogs` pod label → Blast：對應 ServiceMonitor 的 selector 還命中嗎？Prometheus relabel 規則？
- **VRL**：chargeback transform 改輸出 field → Schema cascade：chargeback CSV schema + 下游 finance pipeline 的 `sha256sum -c` 驗證是否受影響。

## 與既有體系關係

- **[#448](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/448)**（container/k8s SAST lint）：機械層單檔 violation（runAsNonRoot / hostNetwork / ALLOW_EMPTY_PASSWORD…）。本 skill 是 AI 跨檔語義層——**互補不重做機械 lint**。
- **vibe-dev-rules**：commit / branch / trailer 紀律仍以 dev-rules 為準（本 skill 不重做）。
- 優先級仲裁見 [CLAUDE.md §Skill 優先級宣告](../../../CLAUDE.md)；衝突時 `vibe-*` supersede 環境層 `engineering:code-review`。
