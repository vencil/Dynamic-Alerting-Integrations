---
title: "IaC Lint Baseline — Container/k8s SAST residual findings"
tags: [internal, lint, security, iac, ci]
audience: [contributors, maintainers]
version: v2.9.1
lang: zh
---

# IaC Lint Baseline — Container/k8s SAST 殘留 findings

> 本文件登記 [epic #448](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/448) Container/k8s IaC SAST 各層的**非阻擋（WARN / High）findings** + rationale + 預計修補時程。
>
> **為什麼需要 baseline 表**：SAST 嚴格度分兩級——**Critical → BLOCK PR**（必須 0）；**High / WARN → 不擋 merge，但須列管**（AC 7「任何 High 都需在 iac-lint-baseline.md 列入 + rationale」）。沒有 baseline 表，warning 會無限累積成無人看的雜訊；列管後每筆都有 owner 與退場條件。
>
> consolidated 全 4 層總帳見下節「Consolidated baseline（全 4 層總帳）」（[TRK-315](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/596) 收斂，epic [#448](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/448) closure）。各層細節隨各層 PR 增補於下方分節。

## Severity → action 對照（Layer 1 採 BLOCK/WARN reduction）

| Wrapper action | 來源 | 行為 |
|---|---|---|
| **BLOCK** | hadolint `error` level、Vibe 規則 V0/V1/V2/V3 違反 | 擋 commit / PR（須 0；epic AC 7 Critical=0 必須）|
| **WARN** | hadolint `warning` level | 記入本表（High），不擋 merge |
| **INFO** | hadolint `info` / `style` level | 僅 log，不列管 |

> 全 4 層共用的 Critical/High/Medium/Low → BLOCK/WARN/INFO 統一表已於 [TRK-314](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/595) 收斂 —— 見下節「統一 Severity → Action（全 4 層 SSOT）」；Layer 1 的 hadolint level → BLOCK/WARN reduction 是上表的特化。

## 統一 Severity → Action（全 4 層 SSOT，TRK-314 收斂）

本表是 epic #448 四層 SAST 共用的 severity → action 對照（AC 5）。各層 detector 把 finding 映到一個 severity tier，wrapper 依本表決定 action；escape 機制依層別不同（見最右欄）。

| Severity | Action | 來源（依層）| Escape 機制 |
|---|---|---|---|
| **Critical** | **BLOCK PR**（required check 擋 merge；epic AC 7 要求 Critical=0）| L1 Vibe V0–V3 / hadolint `error`；**L2 / L4** `privileged-container`·`privilege-escalation-container`·`host-network`·`host-pid`·`host-ipc`·`docker-sock`；L2 Mode A `ALLOW_EMPTY_*`/`INSECURE_*=true` | **無 escape**（必修） |
| **Secret 字面（L3）** | **BLOCK**（lint ship 在 0）| L3 key 名像 secret 但值為非空硬編字面 | PR body `bypass-lint: helm-values-secrets` + `reason: <≥30 words>` |
| **High** | **WARN**（不擋 merge，但**須列管**於本文件 + rationale）| **L2 / L4** `run-as-non-root`·`no-read-only-root-fs`·`unset-cpu·memory-requirements`·`capabilities-add`（wrapper rule）；L1 hadolint `warning` | **L2 / L4**：中央 `EXEMPTIONS` registry（`check_iac_helm.py` / `check_k8s_manifests.py`）+ 本表 rationale，否則未登記 High → BLOCK；L1 hadolint warning 自動入本表 |
| **Medium / Low** | **INFO**（僅 log，不列管）| 其餘所有 kube-linter check；hadolint `info`/`style` | n/a |

> **CVE image scan 維持 informational**（AC 5）：release.yaml 的既有 trivy image-CVE scan **不**升為 BLOCK —— upstream CVE 隨時爆，不應無預警卡 release（與本表的「IaC misconfig」是不同關注點）。

### Branch protection required checks（AC 5，**owner action**）

Critical → BLOCK 的最後一哩是 GitHub branch protection 的 required status checks：CI job 紅 → PR 無法 merge。Claude **無權**改 branch protection（人工操作），owner 須在 **repo Settings → Branches → `main` → Require status checks to pass** 勾選下列 check 為 required：

- [ ] **`Lint`** —— 含 L1 `iac-sast-check`（hadolint）+ L3 `helm-values-secrets-check`（secret-shape，含 raw k8s/）
- [ ] **`Container SAST (Helm L2 + raw k8s L4)`** —— L2 + L4 kube-linter 的 Critical → BLOCK gate（本 job 在 TRK-314 由「L2 (Helm)」更名，涵蓋兩層）
- [ ] 既有：`Lint Rule Packs` / `Python Tests` / `Go Tests` / `Lint Documentation`

> 勾選前先確認該 check 名稱在最近一次 CI run 出現過（rename 後舊名 `Container SAST L2 (Helm)` 不再回報，勿誤設為 required → 會卡在等不到的 check）。

## Consolidated baseline（全 4 層總帳，TRK-315 — epic #448 closure）

epic #448 的 hybrid policy：**既有 open-source engine 優先 + Vibe wrapper 疊專案政策**（severity / 中央 exemption / scope），取代 DIY-only `check_*.py` 的 reactive whack-a-mole（**僅 greenfield 套用**，不回頭遷移既有 ~50 支）。規範同步於 [`dev-rules.md` §安全紀律](dev-rules.md) + `CLAUDE.md`。

**總帳（against `main` HEAD，2026-05-24）—— AC 7：0 Critical（必須）達成 ✅**：

| Layer | 工具 | engine | scope | Critical | baseline-High | INFO |
|---|---|---|---|---:|---:|---:|
| L1 Dockerfile（TRK-311）| `check_iac_vibe_rules.py` | hadolint | 7 Dockerfile | **0** | 5 | 1 |
| L2 Helm template（TRK-312）| `check_iac_helm.py` | kube-linter | 9 chart | **0** | 5 | 3 |
| L3 values/manifest secret-shape（TRK-313）| `check_helm_values_secrets.py` | —（純 Vibe，無對應 engine）| 111 檔 | **0** | 0 | 0 |
| L4 raw k8s manifest（TRK-314）| `check_k8s_manifests.py` | kube-linter | 42 manifest | **0** | 1 | 0 |
| **總計** | | | | **0** ✅ | **11** | 4 |

11 筆 baseline-High **全數**有 rationale + 退場/修補欄（見各層分節表），其中 L2 `tenant-api`、L4 `tenant-api` 為同一架構事實（git workdir 需可寫）；L1 5 筆為政策性 `--no-cache`/pip-self DL3018/DL3013；L2 mariadb×2 + vector×2（root log-collector）為架構必需。**0 Critical** 由 branch-protection required checks（`Lint` + `Container SAST (Helm L2 + raw k8s L4)`）真擋 merge。

> **Critical → BLOCK 已真正上鎖**（非裝飾）：`main` branch protection 已將 `Lint`（含 L1+L3）與 `Container SAST (Helm L2 + raw k8s L4)`（L2+L4）列為 required status checks（owner 已設，2026-05-24）。驗證：`gh api repos/vencil/Dynamic-Alerting-Integrations/branches/main/protection/required_status_checks --jq '.checks[].context'`。

## Layer 1 — Dockerfile（TRK-311，hadolint + Vibe wrapper）

跑法：`python3 scripts/tools/lint/check_iac_vibe_rules.py`（CI hook `iac-sast-check`）。

**Baseline 截至 2026-05-23**：7 個 Dockerfile，**0 BLOCK** ✅ / 5 WARN（High，列管如下）/ 1 INFO。

| # | File:line | Code | 說明 | Rationale | 退場 / 修補 |
|---|---|---|---|---|---|
| 1 | `components/tenant-api/Dockerfile:44` | DL3018 | `apk add --no-cache git ca-certificates tzdata` 未 pin 版本 | 平台刻意採 `--no-cache` + 月度 rebuild + Trivy CRITICAL/HIGH gate，而非脆弱的 Alpine 版本 pin（Alpine repo 很快丟棄舊版本，pin 會把 build 變硬中斷）。同 `components/da-portal/Dockerfile` L30-31 註解的策略。 | 政策性 deferred；除非改採 pinned-base 策略，否則保留 |
| 2 | `helm/federation-gateway/audit-sidecar/Dockerfile:22` | DL3018 | fetch stage `apk add --no-cache curl` 未 pin | 同 #1（build stage，只為下載 + checksum 驗證 mtail，不入 runtime image） | 同 #1 |
| 3 | `helm/federation-gateway/audit-sidecar/Dockerfile:34` | DL3018 | runtime `apk add --no-cache logrotate` 未 pin | 同 #1 | 同 #1 |
| 4 | `helm/federation-gateway/audit-sidecar/Dockerfile:22` | DL4006 | `echo "<sha>  file" \| sha256sum -c -` 的 pipe 前未設 `-o pipefail` | 該 RUN 以 `&&` 串接，且 pipe 的**最後一個指令就是安全關鍵的 `sha256sum -c`**——其 exit code 正確傳播（pipefail 缺席不影響 checksum 驗證的把關效果）；busybox sh。 | **修補候選**（非急）：可加 `SHELL ["/bin/ash","-o","pipefail","-c"]`；留待 sidecar Dockerfile 下次動到時順手 |
| 5 | `components/da-tools/app/Dockerfile:15` | DL3013 | `pip install --upgrade pip` 未 pin pip 版本 | 升級 pip 自身到最新是標準且刻意的；應用相依套件**已 pin**（`PyYAML==6.0.3` / `promql-parser==0.7.0` / `croniter==6.0.0`）。DL3013 命中的是 `pip` 自身那一段。 | 低價值；可選擇性 pin，不列為待辦 |

INFO（不列管，僅記錄）：`components/da-tools/app/Dockerfile:12` DL3059（multiple consecutive RUN）。

## Layer 2 — Helm template（TRK-312，kube-linter + Vibe wrapper）

跑法：`python3 scripts/tools/lint/check_iac_helm.py`（CI job「Container SAST L2 (Helm)」；本地 on-demand：`pre-commit run iac-helm-sast-check --hook-stage manual --all-files`）。引擎：**單一 kube-linter**（render-then-lint）+ Mode A 源碼掃描 + wrapper `capabilities.add` 規則。**trivy-config 不採用**（與 kube-linter 對 K8s misconfig 高度重疊、雙引擎會 desync；trivy 仍是既有的 image-CVE informational scan，不同關注點）。

**例外採中央註冊表**（`check_iac_helm.py` 的 `EXEMPTIONS` dict，非 in-chart 註解——`helm template` 會剝掉註解，且集中式給 SecOps 單一稽核面）。

**Baseline 截至 2026-05-23**：9 個 chart × values variants，**0 Critical** ✅ / 5 baseline-High（中央豁免）/ 3 INFO。

| # | Chart:check | severity | Rationale（= EXEMPTIONS 登記） | 退場 / 修補 |
|---|---|---|---|---|
| 1 | `mariadb-instance:run-as-non-root` | High | mariadb 官方 image 啟動需 root 以 chown data dir 後降權 | 改 rootless mariadb image 才可解；政策性保留 |
| 2 | `mariadb-instance:no-read-only-root-fs` | High | mariadb-server 需可寫 `/var/lib/mysql` data dir | 同 #1 |
| 3 | `tenant-api:no-read-only-root-fs` | High | tenant-api gitops writer shells out to git，需可寫工作區 | **修補候選**：把 git workdir 移到 writable volume + readOnlyRootFilesystem:true |
| 4 | `vector:run-as-non-root` | High | log-collector DaemonSet 需 root 讀 `/var/log/pods` host log | 架構事實；保留 |
| 5 | `vector:capabilities-add` | High | `DAC_READ_SEARCH` — 讀其他 UID 擁有的 host log（配 root 需求） | 架構事實；保留 |

INFO（不列管）：`federation-gateway` / `federation-proxy` / `threshold-exporter` 的 `pdb-unhealthy-pod-eviction-policy`（PDB best-practice，非急；可於 PDB 補 `unhealthyPodEvictionPolicy: AlwaysAllow`）。

**附帶修復（L2 catch）**：`helm/da-portal/.helmignore` 用了 Helm 不支援的 `**/` glob（`**/README.md`），導致 `helm template` / `helm install` 直接中止（helm 3 + 4 皆然）——da-portal chart 原本無法 render，CI 也沒任何地方 render 它故長期未爆。已改為 bare `README.md`（任意深度仍 match）。

> **嚴重度門檻**：Critical（`privileged-container` / `privilege-escalation-container` / `host-network` / `host-pid` / `host-ipc` / `docker-sock`）→ BLOCK 無 escape；High（`run-as-non-root` / `no-read-only-root-fs` / `unset-cpu·memory-requirements` / wrapper `capabilities-add`）→ 須登記 EXEMPTIONS 才豁免，否則 BLOCK；其餘 kube-linter check → INFO。

## Layer 3 — Helm values secret-shape（TRK-313，純 Vibe wrapper）

跑法：`python3 scripts/tools/lint/check_helm_values_secrets.py`（hook `helm-values-secrets-check`，default stage，diff-only；`--full-scan` 做週期 audit）。**無 open-source engine**——YAML-shape 檢查 kube-linter/trivy 無對應。

抓「key 名像 secret（`password`/`token`/`apiKey`/`secret`/`clientSecret`…）但值是非空字面字串」。class (b)（negative pattern + false-positive escape），diff-only + PR-body bypass（`bypass-lint: helm-values-secrets`）。

**與 [#445](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/445) trufflehog 互補不衝突**：trufflehog 抓**高熵值**；本 lint 抓 **YAML shape**（連低熵 `password: hunter2` 都抓，那是 entropy detector 漏的）。兩者不雙重 fire——本 lint **ship 在 0**（所有現存 match 皆白名單），只對**新**硬編字面才響。

**白名單（合法、非硬編 secret）**：空值 `""` / `${VAR}` 環境插值 / `{{ .Values.* }}` template ref / placeholder（`<changeme>`/`REPLACE_WITH_*`/`PLACEHOLDER`/`YOUR_*`）/ bool / numeric / Go-duration（`4h`/`30s`，給 `tokenTTL` 類）/ `valueFrom`·`secretKeyRef`。**Key-allowlist**（名含 secret 但為 ref/flag）：`createSecret`/`secretName`/`existingSecret`/`secretRef`/`secretKeyRef`/`secretKey`/`tokenTTL`。

**Scope（self + Gemini 對抗式 review 後擴大;TRK-314 再納 raw k8s/）**：`helm/*/values*.yaml` + `helm/values*.yaml`（top-level overlay）+ `helm/*/templates/*.yaml`（**所有 template,含 ConfigMap** —— secret 誤置於 ConfigMap 是最常見外洩;key-name 語意掃描適用所有 manifest）+ `k8s/**/*.yaml`（**raw manifest**;secret-shape 檢查 manifest-agnostic,raw Secret 否則無任何一層掃低熵硬編 —— L4 kube-linter 無對應 check、#445 trufflehog 漏低熵）。key match 採 **endswith**（holder 結尾是 secret 字;`passwordPolicy`/`tokenTTL` 等 config 不誤報）；白名單另含 **YAML alias/anchor**（`*x`/`&x`）。**已知限制（accepted residual risk）**：line-based KEY:VALUE 掃描不解析 YAML AST,故 block scalar（`key: |`/`>`）與 list item（`- "literal"`）內的硬編值**不掃**,交 #445 trufflehog 高熵捕捉。

**Baseline 截至 2026-05-24（TRK-314 更新）**：scope **111 檔**（69 helm + 42 raw k8s）,**0 findings** ✅。現存全為白名單命中（mariadb `rootPassword: ""`、oauth2proxy `REPLACE_WITH_*`、`{{ .Values.* | quote }}`、`${SPLUNK_TOKEN}` 註解、`secretKey: *.pem` ref、`tokenTTL: 4h` duration 等）。**附帶修復**：擴 scope 到 k8s/ 時，唯一命中是 `k8s/03-monitoring/secret-grafana.yaml` 的 `admin-password: admin`（committed 弱憑證、可 `kubectl apply` 直接上線;trufflehog 因低熵漏抓）—— 已改為 `REPLACE_WITH_STRONG_PASSWORD` placeholder，對齊同 repo 的 `secret-oauth2proxy.yaml`（修真陽性而非消音;ship 在 0 且零白名單）。

## Layer 4 — k8s raw manifest（TRK-314，kube-linter + Vibe wrapper）

跑法：`python3 scripts/tools/lint/check_k8s_manifests.py [--ci]`（hook `k8s-manifests-sast-check`，**manual stage**，需 engine;CI 在「Container SAST (Helm L2 + raw k8s L4)」job hard-gate）。引擎：**與 L2 同一個 kube-linter**，但直接掃 raw manifest tree —— 不需 helm render（檔案已是 concrete k8s objects）。重用 L2 的 severity 分類（`classify_check`/`CRITICAL_CHECKS`/`HIGH_CHECKS`）與 `.kube-linter.yaml` config。

> **ticket premise 已過時（pre-flight grep 修正）**：原 ticket 假設「repo 無 raw manifest → stub」，實際 `k8s/` 有 **42 個**真 manifest（prometheus / grafana / alertmanager Deployment、tenant-api Deployment、CronJob、ConfigMap、RBAC、NetworkPolicy、raw Secret）。故 Layer 4 是**真層**，非 stub。

**例外採中央註冊表**（`check_k8s_manifests.py` 的 `EXEMPTIONS` dict，key 為 `(repo-relative path, check)` —— raw manifest 以**檔案**為稽核單位，不像 L2 以 chart 為單位）。CRITICAL 永不可豁免（同 L2）。**無 Mode A**：L2 的 `ALLOW_EMPTY`/`INSECURE_*` regex 會誤報 raw manifest 的合法 key（如 Prometheus scrape `insecure_skip_verify: true`，已實測命中），且 raw manifest 無 `{{ if }}` 分支需 pre-render 掃描，故 kube-linter pass 即 L4 全部;raw Secret 的硬編字面值由 **L3**（scope 已含 k8s/）負責。

**Baseline 截至 2026-05-24**：scope **42 manifest**，**0 Critical** ✅ / 1 baseline-High（中央豁免;原始 2 findings，tenant-api 的 2 個 container 同 `(path, check)` 去重為 1）/ 0 INFO。

> **變更（TRK-314 follow-up，PR #600 後續）**：maintenance-scheduler CronJob 原列 2 筆 FIX CANDIDATE（`run-as-non-root` / `no-read-only-root-fs`），已加固 securityContext 解除（pod `runAsNonRoot:true`/`runAsUser:65534`/`seccompProfile:RuntimeDefault` + container `readOnlyRootFilesystem:true`/`allowPrivilegeEscalation:false`/`capabilities.drop:[ALL]` + `/tmp` emptyDir + `PYTHONDONTWRITEBYTECODE=1`）。已 runtime-test：用發行 image 在 `--read-only --tmpfs /tmp --user 65534` 下跑 maintenance-scheduler，report-only 模式 exit 0、HTTP 路徑僅見連線錯誤（無 EROFS）。EXEMPTIONS 兩筆 key 已移除。

| # | Path:check | severity | Rationale（= EXEMPTIONS 登記） | 退場 / 修補 |
|---|---|---|---|---|
| 1 | `k8s/04-tenant-api/deployment.yaml:no-read-only-root-fs` | High | git-clone init + tenant-api container 需可寫工作區 clone/commit conf.d（同 L2 tenant-api chart 豁免;oauth2-proxy sidecar 已 `readOnlyRootFilesystem:true`） | 架構事實;同 L2 #3「修補候選」（git workdir 移到 writable volume） |

> **與其他層的關係**：L4 抓 raw manifest 的 **container misconfig**（kube-linter）;raw Secret 的**硬編字面**由 L3 抓（scope 含 k8s/）;**高熵值**由 #445 trufflehog 抓 —— 三者互補不雙重 fire（kube-linter 無 hardcoded-secret-value check）。

## 關聯

- [epic #448](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/448) — Container/k8s SAST 4-layer
- [`lint-policy.md`](lint-policy.md) — lint class / bypass / allowlist 治理
- [`dev-rules.md` §安全紀律](dev-rules.md) — IaC SAST 起手 pointer
- `.hadolint.yaml`（repo root）— Layer 1 engine config
