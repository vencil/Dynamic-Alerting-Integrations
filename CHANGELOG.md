---
title: "Changelog"
tags: [changelog, releases]
audience: [all]
version: v2.8.1
lang: zh
---
# Changelog

All notable changes to the **Dynamic Alerting Integrations** project will be documented in this file.

## [Unreleased]

<!-- 下一版 in-flight 工作暫存區。每筆 entry 目標 3-6 行使用者重點 + 一行指回內部 artifact；session 過程 / FUSE trap / 完整 commit list 不入此處。release 收尾時做最終 condensation 並切正式 `## [vX.Y.Z]` heading。 -->

### Added

- **新增 [ADR-021](docs/adr/021-tenant-log-query-federation.md)：Tenant Log Query — Authorization-Plane-Only, Ingestion-Decoupled（TRK-316）** — 新架構決策：tenant 在平台上**就地查**自己的 log（query-in-place，非拉回；ADR-020 的姊妹件、方向相反）。平台**只 own 授權平面**，複用既有 `helm/federation-gateway` 新增第三個 `victorialogs` mode；隔離 100% 來自 VictoriaLogs 原生 `(AccountID, ProjectID)` 租戶模型 + JWT claim→header 注入（**非** prom-label-proxy —— LogsQL ≠ PromQL）。ingestion 蓋章解耦為**顯式可驗證契約**（零信任 payload + node-edge 強蓋章 + AccountID 單調配發永不回收）。3-layer blast radius 對 LogsQL 重校（無 sample cap，改靠 time-range 上限）。**Phase 1 — (b) 平台營運 log → targets v2.10.0**；**Phase 2 — (a) 租戶應用 log → defer-with-trigger**。ZH-primary、依語言政策不另製 `.en.md`（同 ADR-019 / ADR-020）。
- **threshold-exporter reload-pressure 記憶體 lever（issue [#459](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/459) Track 2+3）**：v2.8.0 closure soak 發現 sustained reload 下 `go_memstats_sys_bytes` / `heap_idle` high-water creep（`heap_objects` 持平 → 是 Go runtime GC pacing，**非 code leak**）。本 PR 交付**兩個 opt-in lever（皆 default off，不改既有行為）**：(1) Helm `exporter.goMemLimit` 注入 `GOMEMLIMIT` env（Go 1.19+ runtime 原生 soft heap ceiling，首選）；(2) `-free-os-mem-after-reload` flag（Helm `exporter.freeOsMemAfterReload`）每次 reload 後呼 `runtime/debug.FreeOSMemory()`，新 metric `da_config_free_os_memory_total` 計次。soak harness（`run_chaos_soak.py`）加追 `go_memstats_heap_released_bytes`（return-to-OS 直接訊號）。文件：[benchmark-playbook](docs/internal/benchmark-playbook.md#memory-characteristics-under-reload-pressure-459) 新增「Memory characteristics under reload pressure」段 + 新增雙語 [`deployment-sizing.md`](docs/integration/deployment-sizing.md)（reload-interval × uptime ≈ 成長 proxy + 記憶體 sizing 指引）。**Track 1（4h soak / reload-interval sweep / GOMEMLIMIT 實驗）未含本 PR**——先把 lever + 量測訊號到位讓實驗可跑。詳 [#459](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/459)。
- **threshold-exporter chart 補上 generic `extraEnv` / `extraEnvFrom`（issue [#607](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/607)）**：對齊 `helm/vector` / `helm/victorialogs` / `helm/chargeback-aggregator`（#565/#566）的平台慣例，讓 operator 跨 chart 心智模型統一 —— 注入 chart 未模型化的 env（`HTTP_PROXY` / APM agent 變數 / 自訂憑證路徑等，支援 `valueFrom` Secret ref）。採**混合模式（hybrid）**：保留 #459 的 dedicated `exporter.goMemLimit`（一等公民、可發現、可型別化、與 `freeOsMemAfterReload` 成對）為記憶體調校首選，`extraEnv` 為逃生艙（escape hatch）。**碰撞防呆**：template 先渲染 dedicated 欄位、後渲染 `extraEnv`，故同名 key（如 `GOMEMLIMIT`）依 K8s env「後蓋前（last-one-wins）」由 `extraEnv` 取得最終覆寫權，無需 Helm 端比對 key。兩者皆 default `[]`（no-op，不改既有 deploy）。chart 版本 2.8.0→2.8.1（appVersion 不變，無 Go binary 變更）。詳 [#607](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/607)。
- **Container/k8s IaC SAST Layer 4 — raw k8s manifest（kube-linter + Vibe wrapper，epic [#448](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/448) / TRK-314）** — 新 `scripts/tools/lint/check_k8s_manifests.py`：以 L2 同一個 kube-linter 直掃 `k8s/**/*.yaml`（不需 helm render，檔案已是 concrete objects），重用 L2 的 severity 分類 + `.kube-linter.yaml`，並用自有 `(path, check)` 中央 `EXEMPTIONS` registry。**ticket 原假設「0 raw manifest → stub」已過時**：pre-flight grep 發現 `k8s/` 有 42 個真 manifest，故為**真層**。Baseline：42 manifest，**0 Critical** ✅ / 1 baseline-High（tenant-api 可寫工作區；原 maintenance-scheduler CronJob 的 2 筆同 PR 加固 securityContext—runAsNonRoot/readOnlyRootFilesystem/drop-ALL + /tmp emptyDir + PYTHONDONTWRITEBYTECODE，runtime-test 過—故解除豁免而非列管）。hook `k8s-manifests-sast-check`（manual stage，需 engine），CI 併入「Container SAST (Helm L2 + raw k8s L4)」job。**AC5 CI integration**：於 `docs/internal/iac-lint-baseline.md` 收斂全 4 層共用的 **Severity → Action SSOT 表** + branch-protection required-check checklist（owner action）；trivy image-CVE scan 維持 informational。**附帶修復**：擴 L3 secret-shape scope 到 `k8s/`（69→111 檔），唯一命中 `k8s/03-monitoring/secret-grafana.yaml` 的 `admin-password: admin`（committed 弱憑證、trufflehog 低熵漏抓）已改為 `REPLACE_WITH_STRONG_PASSWORD` placeholder。
- **Container/k8s IaC SAST Layer 3 — Helm values secret-shape（純 Vibe wrapper，epic [#448](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/448) / TRK-313）**：4-layer 第三層、最輕（純 Python，無 engine）。`scripts/tools/lint/check_helm_values_secrets.py` 抓「key 名像 secret（`password`/`token`/`apiKey`/`secret`/`clientSecret`…）卻設成非空字面字串」於 `helm/*/values*.yaml` + `helm/values*.yaml` + `helm/*/templates/*.yaml`（**含 ConfigMap 等所有 template**——secret 誤置於 ConfigMap 是最常見外洩,key-name 語意掃描適用所有 manifest）。key 採 **endswith**（`passwordPolicy`/`tokenTTL` 等 config 不誤報）。class (b)（negative pattern + escape）：diff-only + PR-body bypass（`bypass-lint: helm-values-secrets`）+ 雙語錯誤。**與 [#445](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/445) trufflehog 互補**：trufflehog 抓高熵、本 lint 抓 YAML shape（連低熵 `password: hunter2` 都抓）；兩者不雙重 fire（本 lint ship 在 0，只對新硬編才響）。**白名單**：空值 / `${VAR}` / `{{ .Values.* }}` / placeholder（`<changeme>`/`REPLACE_WITH_*`）/ bool / numeric / Go-duration（給 `tokenTTL: 4h`）/ `valueFrom`·`secretKeyRef` / YAML alias（`*anchor`）；**key-allowlist**：`createSecret`/`secretName`/`secretRef`/`secretKeyRef`/`secretKey`/`tokenTTL`。**已知限制**：block scalar（`|`/`>`）與 list item（`-`）內的硬編值不掃,交 trufflehog 高熵捕捉。Baseline：**69 檔 0 findings**（self + Gemini 對抗式 review 後擴 scope + 修 endswith/placeholder/YAML-alias，仍 0）。pre-commit hook `helm-values-secrets-check`（default stage，diff-only）+ CI Lint job。詳 [`iac-lint-baseline.md`](docs/internal/iac-lint-baseline.md) §Layer 3。

- **Container/k8s IaC SAST Layer 2 — Helm template security（kube-linter + Vibe wrapper，epic [#448](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/448) / TRK-312）**：4-layer 的第二層。引擎為 **單一 kube-linter**（render-then-lint），wrapper `scripts/tools/lint/check_iac_helm.py` 跑兩模式 + 套 severity→action：**Mode A** 源碼文字掃 `ALLOW_EMPTY_*`/`INSECURE_*`（在 render 前抓、連 `{{ if }}` 包住的也抓，ERROR 無 escape）；**Mode B** 對每個 chart `helm template --namespace=lint-test`（含 `values-tier*.yaml` 變體）→ kube-linter，外加 wrapper 自己 parse 渲染 YAML 抓 `capabilities.add`。**嚴重度**：Critical（privileged / privilege-escalation / host-network / host-pid / host-ipc / docker-sock）→ BLOCK 無 escape；High（run-as-non-root / no-read-only-root-fs / unset-cpu·memory-requirements / capabilities-add）→ 須登記**中央 EXEMPTIONS 註冊表**才豁免（否則 BLOCK，逼 review），其餘 → INFO。**設計決策（偏離初始 AC，已記錄）**：(1) **廢 trivy-config**——與 kube-linter 對 K8s misconfig 高度重疊、雙引擎會 desync；trivy 仍為既有 image-CVE informational scan（不同關注點）。(2) **例外採中央註冊表而非 in-chart 註解**——`helm template` 會剝掉註解（values 經 `toYaml` 渲染尤甚），且集中式給 SecOps 單一稽核面。(3) `runAsNonRoot:false` 由 Critical 改 **High + 中央豁免**——否則會硬擋合法的 vector log-collector DaemonSet（需 root 讀 host log）。**Baseline**：9 chart **0 Critical**；5 baseline-High（mariadb ×2 / tenant-api ×1 / vector root 三件套含 `DAC_READ_SEARCH` ×2）+ 3 INFO（pdb），全列管於 [`iac-lint-baseline.md`](docs/internal/iac-lint-baseline.md)。**附帶修復**：L2 抓到 `helm/da-portal/.helmignore` 用了 Helm 不支援的 `**/` glob → `helm template`/`install` 直接中止（chart 原本無法部署、CI 無處 render 故長期未爆），改為 bare `README.md`。CI 走獨立 parallel job「Container SAST L2 (Helm)」（setup-helm + kube-linter binary，Docker fallback）；pre-commit hook `iac-helm-sast-check` 為 manual stage（render 9 chart ~1-2 min,不拖慢每次 commit）。詳 [epic #448](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/448)。

- **Container/k8s IaC SAST Layer 1 — Dockerfile（hadolint + Vibe wrapper，epic [#448](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/448) / TRK-311）**：4-layer IaC SAST 的第一層落地，並**試點 hybrid policy**（open-source engine + Vibe wrapper，取代過去「DIY `check_*.py` 看到就打」的路線）。hadolint v2.12.0 當引擎（containerized 或 binary，`.hadolint.yaml` config），Vibe wrapper `scripts/tools/lint/check_iac_vibe_rules.py` 聚合 hadolint JSON、套 severity→action（error=BLOCK / warning=baseline-High / info=INFO）並補三條 hadolint 無法表達的規則：每個 Dockerfile 須有 **HEALTHCHECK 或 `# rationale:` 註解**（distroless 自動豁免）、**禁過寬 `COPY`/`ADD`**（source 為裸 `.`/`./`/`*`，正是 v2.7.0 silent-break 的成因）、**`.dockerignore` baseline**（在每個 build-context root；用 `pathspec` 正規化等價 glob）。pre-commit hook `iac-sast-check` + CI `Lint` job hard-gate。**Pre-flight 校正**：repo 已成長至 7 個 Dockerfile（非 6）、9 個 Helm chart（非 ~4）；`.dockerignore` 從 1/7 補到全 context 覆蓋——且 da-portal / tenant-api 是從 **repo root** build（`context: .`），故補一份 repo-root `.dockerignore`（含 `!docs/...` re-include，本地實測兩者 image build 正常），而非各 component 目錄各放（會是 no-op）。issue AC1 的 DL3025 標號修正（真 DL3025 是 CMD/ENTRYPOINT JSON，over-broad COPY 改由 wrapper rule 攔）。非阻擋 High findings（5 筆，多為刻意的 `--no-cache` 策略）列管於 [`iac-lint-baseline.md`](docs/internal/iac-lint-baseline.md)。Layer 2-4（Helm template / values / k8s manifest）+ 完整 hybrid policy 段見 TRK-312~315。詳 [epic #448](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/448)。

- **#566 batch D — egress allowlist gate + GitOps-write-boundary doc（T4-1/T4-2 + T3-2/T3-3，issue [#566](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/566)）**：紅隊 backlog 最後一批可動工項。**經 Gemini 對抗式 review 修正方向後定案**（take/reframe：Gemini 假設 GitOps self-heal 已覆蓋平台 chart —— pre-flight grep 打臉，repo 的 GitOps scope 只到 `conf.d/` 租戶配置，故 self-heal 改標**條件式建議**；Gemini 建議的 extraEnv 扁平 key-blacklist 會打死合法 `secretKeyRef` 用途 —— 改為**區分 literal-value vs valueFrom**）。**T4 egress gate**（`scripts/tools/lint/check_log_egress_policy.py` + `make lint-egress`）：PR/pre-deploy 階段渲染 log-aggregation charts，擋 (1) `additionalSinks[].{endpoints,uri}` host 不在 allowlist、(2) 覆寫 `VECTOR_*` 保留 env（非 downward-API fieldRef，避免誤殺 chart 自己的 `VECTOR_SELF_*`）、(3) sensitive-named env（`*TOKEN*`/`*KEY*`/`*SECRET*`…）用字面 `value:` 而非 `valueFrom`。資料流照 multi-doc YAML caveat：`helm template` → `safe_load_all` → 過濾 None → 迭代 manifest，Vector ConfigMap 額外解析內嵌 `vector.yaml` 取結構化 sinks。**選型**：repo 的 `policies/examples/*.rego` 是 illustrative（grep 證實無 CI/Makefile/pre-commit 引用），真 gate 是 ~50 個 `check_*.py` + `opa` binary 不在 dev/CI（外部下載也被擋）—— 故 live gate 走 Python lint，政策同時鏡像為 `policies/examples/log-egress.rego`（modular core rules，為未來 OPA Gatekeeper runtime admission 留無痛遷移 seam）。**T3 doc-only**（runbook §7.5.2 production checklist）：人類 RoleBinding 不得有 `update/patch/delete` on deploy/cm/secret（只 GitOps SA 能寫）+ 把平台 Helm release 納入 ArgoCD/Flux self-heal（篡改 window 壓到 sync interval；明標目前 GitOps scope 只到 conf.d、需擴）。**CI gap 修補（review 抓到的真問題）**：`Python Tests` job **原本沒裝 helm** → 整個 session 加的 helm-gated chart test（NetworkPolicy / VRL / buffer guard / egress 共 34 個）在 CI **靜默 skip**、只在本地驗過 —— 加 `azure/setup-helm@v4` 讓它們從本地 safety net 升級成真 CI gate。chart test 28 → 34。詳 runbook §7.5 / `policies/examples/log-egress.rego`。

- **#566 batch C — CSV tamper-evident + extraEnv platform 慣例對齊（T2-4 + Q5，issue [#566](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/566)）**：紅隊 backlog C 批。**T2-4 chargeback CSV integrity**（`helm/chargeback-aggregator` `0.1.2`→`0.2.0`）：每筆 `chargeback-YYYY-MM-DD.csv` 旁邊寫 `.csv.sha256` sidecar（`sha256sum -c` 格式，finance pipeline 可單檔驗證）+ append-only `manifest.jsonl`（每次跑寫一行 `{date, sha256, generated_at, rows}`）。tamperer 要同時改 CSV + .sha256 + manifest 三檔才能不留痕跡，門檻拉高（vs 原本 free-edit）。retention prune 連 .sha256 sidecar 一起砍（避免 orphan hash 誤導），但 manifest **永不 prune** —— 它就是長期 audit trail。**邊界明示**（runbook §2.1）：這是 **tamper-evident**（有改動的訊號）不是 **tamper-proof**（PVC write 權還是能造假三檔）—— 真正 compliance-grade WORM 是 #566 X-2 / SIEM 端責任。**Q5 extraEnv/extraEnvFrom 平台慣例對齊**：把 #565 給 helm/vector 加的 `extraEnv` + `extraEnvFrom` 同形狀加到 `helm/victorialogs` （chart `0.1.3`→`0.1.4`）+ `helm/chargeback-aggregator`（chart 同上版本 bump）—— 三個 chart 現在 cred 注入 shape 一致。VictoriaLogs 未來加 `-httpAuthKey` 走 Secret 透過 extraEnvFrom 餵；chargeback 對 multi-tenant VictoriaLogs `ACCOUNT_ID` 也能走 Secret。**chart test**：23 → 28 case（sha256 sidecar 格式 / manifest append-only / sidecar prune-with-csv / chargeback + vlogs extraEnv 渲染）。runtime kind 驗證：CronJob 跑出 chargeback-2026-05-21.csv + .sha256 + manifest.jsonl 三檔，busybox `sha256sum -c` 對 sidecar 回 `OK` ✓。手動驗證指令文件化進 chargeback runbook §2.1。詳 [`chargeback-aggregator-runbook.md`](docs/internal/chargeback-aggregator-runbook.md) §2.1。

- **#566 batch B — compliance-strict path（X-3 + T3-1 + X-2 schema reserve，issue [#566](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/566)）**：紅隊 backlog B 批，三條互相依賴的 compliance hardening 一 PR 走完。**X-3 Vector disk buffer mode**（`helm/vector` `0.4.0`→`0.5.0`）：`additionalSinks` 新增 `_buffer_type: memory|disk`（預設 memory，無 regression）。disk mode 寫進 daemonset 既有的 hostPath data_dir（`/var/lib/vector`），Vector pod 重啟 / OOM-kill 都不丟 in-flight events（修了 X-3 audit timeline 洞）。`max_size` 預設 1 GiB（covers ~1h SIEM outage 中等流量）；分支 render `memory → max_events` / `disk → max_size`，Vector 對錯欄位名會 reject 啟動所以模板要選對。未知 `_buffer_type` 走 template `{{ fail }}` 同 §2 護欄一樣 fail-loud。**runtime 抓到的細節**：helm/sprig 對大整數會 round-trip 成 float scientific notation（`268435488` 渲染成 `2.68435488e+08`），Vector BufferConfig parser 直接 reject 「untagged enum」error。Template 加 `| int64` cast 強制整數渲染；新增 regression test 鎖死字面值。Vector 自帶 disk buffer 最小 256 MiB 限制，低於這個值 runtime 拒，doc 內提及。**T3-1 §2 vs compliance trade-off 文件**（runbook §7.3.1）：明寫 §2 hard rule（drop_newest）跟 compliance（不可漏 row）根本衝突 —— 時機性 attacker 趁 SIEM-down window 動手，VictoriaLogs 仍有 row 但 SIEM 端 forensic timeline 真空。三條設計路徑表格：**availability-first**（memory + drop_newest，預設，pod 重啟丟 row）/ **compliance-degraded**（disk + drop_newest，§2 仍守、SIEM downtime cover 到 max_size 滿）/ **compliance-strict**（disk + block，VictoriaLogs 必須 disable，產品定位變更等級）。預設不變、operator 按 compliance SLA 選層。**X-2 audit-signing schema seam**（VRL doc-only）：comment block 在 VRL demux 後標出 `audit_signature` / `audit_signed_at` field shape 未來會由 federation-gateway HMAC 簽。今日 no-op（`merge` 已自動 pass-through 任何 source JSON 欄位到 sink），但 schema 預留 + chain-of-custody primitive 的整體論述就位 —— 真做 producer-side signing 時不必動 Vector。**chart test**：22 → 23 case（新增 buffer memory/disk/invalid-type render + max_size 字面值 integer-shape 反 regression）。runtime kind 驗證：vector pod 用 disk buffer config Ready=True、`max_size: 268435488` 渲染成 bare integer（不再 float），既有 VictoriaLogs ingest + chargeback CronJob 不受影響。詳 [`platform-log-aggregation-runbook.md`](docs/internal/platform-log-aggregation-runbook.md) §7.3.1。

- **#566 batch A+E — log-aggregation hardening cheap wins（issue [#566](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/566)）**：#539 closure 後紅隊 + quality review 留下 17 條 backlog 拆批處理的第一批；都不阻 production-flow，但每條補一道目前單薄的縱深。**T5 image digest knob**：`helm/{victorialogs,vector,chargeback-aggregator}` 各新增 `image.digest`（預設空字串、render 出仍是 `repo:tag`，set 後變 `repo:tag@sha256:...`，kubelet pull-by-digest 不再 trusts upstream tag）。chart versions vlogs `0.1.2`→`0.1.3` / vector `0.3.2`→`0.4.0` / chargeback `0.1.1`→`0.1.2`。**T2-1 VRL gateway-origin filter**：`helm/vector` 新增 `audit.gatewayPodOwnerPrefix`（預設 `federation-gateway-`，空字串停用） —— 若 `log_type=federation_audit` 但 `kubernetes.pod_owner` 不符 prefix，VRL 強制改 `log_type=suspicious_audit` 分流；防 spoofer pod 用 gateway label 推假 audit row（紅隊 T2-1，最終 fix 仍須 RBAC，本層是 defense-in-depth + 留可 alert 的訊號）。**T1-4 + X-4 Prometheus alerts**（`k8s/03-monitoring/configmap-rules-platform.yaml`）：(a) `FederationAuditPipelineSilent`（severity: critical，5m）—— `absent_over_time(tenant_federation_requests_total[10m])` 一旦消失代表 audit pipeline 斷掉，解決原本「CSV 上某 tenant 0 row 不知是真沒用還是 pipeline 死掉」的歧義；(b) `VectorBufferEventsDropped`（severity: warning）—— `rate(vector_buffer_discarded_events_total[5m])>0` 偵測 SIEM fan-out buffer 溢流（§2 預設 drop_newest 不擋上游、但會丟 SIEM-bound row，這個 alert 把「靜默丟 row」變成 actionable）。runtime 在 kind 驗證：rules 已 load、`FederationAuditPipelineSilent` 因 gateway 本來就沒部進 kind 正確 fire pending。**Q4 chargeback runbook 拆檔**：把 `platform-log-aggregation-runbook.md` §6 整段（80 行）抽出成獨立 [`chargeback-aggregator-runbook.md`](docs/internal/chargeback-aggregator-runbook.md)，主 runbook 留 5 行摘要 + pointer；對齊 `federation-key-rotation-runbook.md` 體積。**Q6 VRL refactor watermark**：在 VRL demux 前加架構註解標出「現在 4 branches 還可讀，加第 5 個 log_type 該拆 Vector `route` transform」的決策點（doc-only、不改 VRL）。**chart test**：`tests/shared/test_helm_chart_log_aggregation.py` 從 15 case 補到 **19** case（image digest knob 兩種 render path、VRL origin filter enable/disable/**nil-audit-block-safe**）。**Pre-merge review 抓到的兩個 issue**：(a) VRL origin-filter 用 `.Values.audit.gatewayPodOwnerPrefix` 直接 access，operator 用 `helm upgrade --reuse-values` 從 0.3.x 升到 0.4.0 時，舊 stored values 沒有 `audit:` block → template **nil pointer panic**（自己 dogfood 升 chart 時撞到的問題，當下 work around 沒進 fix）；改用 `(default dict .Values.audit).gatewayPodOwnerPrefix` nil-safe lookup，補一條 unit test 鎖死這個升級路徑。(b) 之前 self-review 為了過 `bump_docs --check` 把 `docs/internal/known-regressions.md` v2.8.0 改 v2.8.1 寫到本地 disk —— 但該檔 gitignored，never ships。Revert 為原狀，避免 dev 環境跟 git tree drift。kind cluster runtime 驗證：Prometheus 重啟後新 alerts 都 `health=ok` 載入 / Vector helm upgrade 後 VRL 含 `suspicious_audit` 分流 + 正確 prefix / 既有 fan-out + chargeback CronJob 不受影響。

- **Platform 日誌彙整 Phase 3 — SIEM fan-out compliance branch（issue [#539](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/539) Phase 3）**：`helm/vector` chart `0.2.1`→`0.3.1`，values 新增 `additionalSinks: []` 列表，每筆 entry 是 free-form Vector sink config，跟 primary VictoriaLogs sink 共用 demux transform 輸出 —— 當 strict compliance 客戶到（tamper-evidence / WORM / legal hold），加一筆 sink 指向 SIEM 就好，上游 source/transform 不動（同一份 demuxed stream 包含 Phase 1 federation_audit/gateway_operational + Phase 2 prometheus_query_log 三條 stream 一起 fan-out，不需要二次配線）。**§2 hard rule 落地**：chart template 自動把每筆 entry 包 `buffer: {type: memory, when_full: drop_newest, max_events: 10000}`，意思是 SIEM 慢/掛時對該 sink **丟新事件**而不是 back-pressure 上游，primary VictoriaLogs 永不被連累。Entry 自帶 `buffer:` block 就跳過自動 wrap（給要 disk buffer 的 advanced operator）。**fail-loud 護欄**：template 偵測 `_buffer_when_full: block` 直接 `{{ fail }}` 中止 render，配 helpful error message（明寫違反 §2 + 給可用值 + 真要 block 該怎麼做）——self-review 預防 operator foot-gun。**SIEM 認證**：新增 `extraEnv` / `extraEnvFrom` values + daemonset 對應 render，operator 能透過 `secretRef` 把 SPLUNK_TOKEN 等 cred 注成 env var 讓 Vector `${VAR}` 展開（self-review 抓到原本文件範例引用 `${SPLUNK_TOKEN}` 但 chart 沒對應 knob 的 cold-start 斷層）。**架構釐清**（runbook §7.3）：加 fan-out **不會**把 VictoriaLogs 變 tamper-evident —— 平台只負責「同一份 demuxed stream 餵給 SIEM」，三件事完全在 SIEM 端（tamper-evidence / legal hold / immutable retention）；SIEM owner（通常 SecOps）扛 chain-of-custody，平台只負責 delivery 不漏 row（dropped-events metric 該被告警）。**runtime smoke-test 在 kind cluster 驗證**：起 mock-siem deployment（Python stdlib HTTP server 印收到的 JSON），helm/vector 加 additionalSinks 指向 mock-siem → 同時收到 prometheus_query_log row；scale mock-siem 到 0 模擬 SIEM outage 60s，Vector pod RESTARTS=0、VictoriaLogs row count 72366→73039（+673 in 30s，primary 完全沒被 back-pressure）；restore mock-siem replicas → 立即恢復收 row。runbook §7 + §7.1/7.2/7.3/7.4 涵蓋啟用步驟、4 條鐵則（drop_newest 預設 / 不可 block / inputs 用 demux 不用 raw / 不可叫 victorialogs）、compliance 責任落點對照表、4 個 failure mode。Phase 3 完成 → #539 三 phase 全交付，issue 關 umbrella。**紅隊 review 兩條 critical 同步補進**：(a) `helm/victorialogs` `0.1.1`→`0.1.2`：NetworkPolicy 預設 `enabled: true` + 預設 `allowedPodSelectors` 鎖 :9428 只給 vector / chargeback-aggregator / grafana —— 之前 VictoriaLogs HTTP API 無 built-in auth、同 ns 任何 pod 可 `wget DELETE` 砍歷史 audit（紅隊 T2-2）。(b) `helm/vector` `0.3.1`→`0.3.2`：`_buffer_when_full: block` 護欄補完 —— 之前只擋 shorthand，operator 自帶 `buffer: {when_full: block}` 直接 override 會繞過、違反 §2 hard rule；新規則 template 偵測自備 buffer block 內的 `when_full: block` 也 fail-loud（紅隊 T4-4）。runtime kind smoke：NetworkPolicy 套用後 vector + vlogs 正常 ingest（row count 持續爬升 ~108k），`describe networkpolicy` 確認三條 podSelector 規則就位（kindnet 不 enforce，production CNI 如 Calico/Cilium 才生效）；buffer guard 兩種繞法（shorthand + 自帶 block）都 fail template render。其餘紅隊 finding（producer-side audit signing / sink URL allowlist / Vector disk buffer / image digest pinning / SIEM-down evidence gap 等共 9 條）開一張 follow-up issue 統一收，本 PR 不擴 scope。**Quality review 兩條補進**：(c) 新增 `tests/shared/test_helm_chart_log_aggregation.py` 15 case —— pattern 抄 `test_helm_portal.py`，gate 在 helm CLI 可用（match `test_federation_keygen.py` 的 openssl-skipif 慣例）；驗 vlogs NetworkPolicy 三條 consumer / vector VRL `ruleGroup` override + `tenant=` regex + `_s` 單位 / `_buffer_when_full=block` 兩條繞法都 fail / extraEnv 進 daemonset / 內嵌 Python aggregate.py `py_compile` 通過。(d) Smoke-test fixture 進 repo：`docs/internal/examples/log-aggregation-smoketest/{mock-siem.yaml, vector-phase3-values.yaml, README.md}`，runbook §7.1 加 pointer —— red-team T2-2 / Phase 3 fan-out 在 main 從此可重現，不再是「我 host 上的 tmp file」口頭證據。詳 #539 §4 Phase 3 / [`platform-log-aggregation-runbook.md`](docs/internal/platform-log-aggregation-runbook.md) §7。

- **Platform 日誌彙整 Phase 2 — federation chargeback aggregation（issue [#539](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/539) / [#552](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/552)）**：把 #552 scoping ticket 指明的「正確 chargeback 來源」端到端接起來 —— Prometheus query log → Vector → VictoriaLogs `log_type=prometheus_query_log` stream → daily CronJob 算 per-tenant `samples_scanned` + `exec_time_s` CSV。**為什麼**：`tenant_federation_requests_total{tenant,status}`（gateway mtail sidecar 產出）只能數**請求次數**，無法分辨重 query（掃 10 萬 series）跟輕 query（10 series）—— 計費把兩者收同樣錢、不可行（#552）。IV-2f（#511）刻意不讓 Envoy 加 `series_returned` 欄位（要 buffer + 解壓 response body、成本太高；blast-radius 交給 storage `--query.max-samples` cap），正確 cost signal 在 Prometheus 自己 `query_log_file` 每條 query 的 `stats.samples.totalQueryableSamples` + `stats.timings.execTotalTime`。**改了什麼**：(1) `k8s/03-monitoring/configmap-prometheus.yaml` global 加 `query_log_file: /dev/stderr` —— 每條 PromQL query 變成 stderr 上的一行 JSON，跟 Prom 自己的 log 交織但可由 JSON-shape 區分；(2) `helm/vector` chart `0.1.2`→`0.2.0`，values 新增 `additionalSources` list（per-entry kubernetes_logs source；理由：gateway 用 `app.kubernetes.io/name` label，Prometheus 用 `app` label，單一 selector 無法同時 match —— 不引入 OR 邏輯反而展開成多個 source 比較乾淨），configmap.yaml 對應 range 渲染、demux transform inputs 拼進去；VRL demux 加第三 branch：parse 成功且有 `params.query` field → `log_type=prometheus_query_log`，用 regex 從 `params.query` 抽出 federation-proxy 注入的 `tenant="X"`（label 名為 `tenant` 不是 `tenant_id`，per PR #505 / federation-label-enrichment-audit；輸出 field 名仍叫 `.tenant_id` 對齊 §3 stream-field schema）；同時偵測 entry 是否有 `ruleGroup` field —— 有則屬於 Prometheus rule / alert evaluation、強制砍掉 `.tenant_id` bucket 成 `platform`（不可計費，防止規則表達式內部 `{tenant="X"}` selector 被誤分租戶），hoist `samples_scanned` / `exec_time_s` / `eval_time_s` 三個 cost 欄位到 top-level 讓 LogsQL `stats sum()` 不必走 nested path（**單位明示**：Prometheus 原生 `execTotalTime` / `evalTotalTime` 輸出單位是**秒**，self-review 在 push 前抓到原本欄位名誤標 `_ms`、會讓下游 finance pipeline 少收 1000× 帳，已於 amend 統一改為 `_s`；aggregator 查詢同時 sum 兩個欄位名以平滑 chart 升級期間混合舊／新 row 的 24h overlap window，VictoriaLogs retention rolls 過後 legacy `_ms` slot 自然消失）。(3) 新增 `helm/chargeback-aggregator` chart（v0.1.0）—— CronJob daily 02:00 跑 Python script（純 stdlib，無 pip）查 VictoriaLogs LogsQL `_time:24h log_type:prometheus_query_log | stats by (tenant_id) sum(samples_scanned), sum(exec_time_s), count()`，寫 `chargeback-YYYY-MM-DD.csv` 到 PVC（`output.retentionDays=90` 預設、舊檔自動刪），含 `manualJob.enabled=true` 一鍵跑 bootstrap / smoke-test Job；security 同 Phase 1（distroless 不可用就用 python:3.13-slim + non-root + readOnlyRootFS + drop all caps + automountServiceAccountToken: false）。**架構邊界**：chargeback 算的是 raw cost dimension（samples、exec time、query 數），**不**算錢（unit pricing 是 finance 的事）、**不** push metric（沒拉 pushgateway dependency；要 Prometheus 看的話 wrap 一個 textfile-collector 在外層）、**不**做 real-time 聚合（CronJob 才能保證慢的 chargeback run 永不拖累 federation 熱路徑、#552 hard rule）。runbook 新章 §6 + §6.1/6.2/6.3 涵蓋 deploy 順序、tenant attribution 邏輯（platform-bucket 不可計費）、4 個常見 failure mode；§7 Phase 3 連結改為「Phase 2 stream 自動跟著 fan-out」明確不需要二次配線。**Runtime smoke-test 在 kind cluster 驗證**：手動 inject `up{tenant="db-a"}` → VictoriaLogs row `_stream{...,tenant_id="db-a"}` ✓；platform rule eval row 沒 tenant_id ✓；CronJob 一次跑出 CSV 兩 bucket（platform=1055 samples / 6050 queries / 24h，db-a=1 sample / 1 query）正確。Python 3.13 `datetime.utcnow()` deprecation 一併修為 timezone-aware `datetime.now(timezone.utc)`。詳 #539 §4 Phase 2 / [`platform-log-aggregation-runbook.md`](docs/internal/platform-log-aggregation-runbook.md) §6。

- **Platform 日誌彙整 Phase 1 — Vector + VictoriaLogs（issue [#539](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/539)）**：兩個新 chart + Grafana 接線，把 federation-gateway audit log 從「跟著 HPA 死亡的 pod stdout」搬進中央 log store，讓 incident query「過去 6h 這顆 token 碰過什麼」有得問。新增 `helm/victorialogs`（單 pod + PVC + `-retentionPeriod=30d`，#539 §3 operational-simplicity 選的 single-binary store）+ `helm/vector`（DaemonSet + ClusterRole list/watch pods/namespaces/nodes + ConfigMap 帶 VRL 設定）；DaemonSet 走 `VECTOR_SELF_NODE_NAME` 讓 `kubernetes_logs` 只看本節點 pod（不必 cluster-wide list 翻全部 pod、API server 負擔線性可控）；source `extra_label_selector` 預設 `app.kubernetes.io/name=federation-gateway` 把 Phase 1 範圍鎖在這條 audit pipeline（§7 非目標：不做平台級通吃，後續每個 consumer 各開 ticket）。**VRL demux 兩條鐵則**（#539 §3）：(1) `parse_json(.message)` 成功 → `log_type=federation_audit`、merge 解出的欄位上來；失敗 → `log_type=gateway_operational`、保留原始 `.message`。**故意不**用 `exists(.tenant_id)` 當分流條件 —— JWT 失敗的請求在 audit JSON 裡沒 `tenant_id`（jwt_authn 在 claim injection 之前就拒了）但它們**有 forensic 價值**（攻擊掃描證據）。(2) **不丟**操作層日誌，把 Envoy stderr 路到第二條 stream，讓它第一次能被中央查詢，否則 pipeline 嚴格劣於現狀。`_stream_fields=app,k8s_namespace,log_type,tenant_id,status`（bounded 集合）；`pod_name`、`token_id`、`query` 刻意**不**進 stream（HPA churn / 高基數會炸 stream 索引，#539 §3 schema 表）。Vector → VictoriaLogs 走 elasticsearch `_bulk` sink + gzip。Grafana 接線：`k8s/03-monitoring/configmap-grafana.yaml` 新增 `victoriametrics-logs-datasource` provisioning、`deployment-grafana.yaml` 加 `GF_INSTALL_PLUGINS` 自動裝 plugin（air-gapped cluster 必須改 pre-bake image，runbook 有寫）。新 runbook [`platform-log-aggregation-runbook.md`](docs/internal/platform-log-aggregation-runbook.md) 涵蓋 deploy 順序、smoke-test LogsQL 範本（5 條：現在有 log 進來、`by tenant_id` 量、`status:~"5.."`、JWT 拒絕掃 token、操作層異常）、stream-field schema 鎖（改它是破壞性變更）、troubleshooting（Grafana 查不到 / VictoriaLogs PVC bind 不到 / Vector 讀 hostPath 失敗）、capacity 公式（`size ≈ audit_RPS × 1KB × retention × 1.3 / compression`，VictoriaLogs ~10x 壓縮）、Phase 3 SIEM fan-out 是 sink 加一條、上游不變。**硬規則**（#539 §2）：producer 永不 HTTP-push 到 store、log-store 掛掉永不能拖累 federation gateway —— 故 chart 刻意不暴露 direct-push 便利旋鈕。**架構意義**：[ADR-020](docs/adr/020-tenant-federation.md) IV-2f 在這之前的 `helm/federation-gateway` 留下「durable 中央 forensic store 為 follow-up #539」缺口，本 PR 端到端補齊；envoy.yaml 與 `audit-sidecar/Dockerfile` 內的 `#539 follow-up` 註解一併翻新為「已交付」。**範圍邊界**（§7 反創意）：本 PR **不**動 chargeback query log（#552 consumer #2、blocked on 觸發）、**不**做 SIEM fan-out（Phase 3、需 strict compliance customer 觸發）、**不**做 log-based dashboard / alert（另開 ticket）。詳 [#539](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/539) §0–§7。

- **Python SAST baseline — bandit + dev-rules Rule #5 code-driven enforcement（issue [#455](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/455)）**：dev-rules.md Rule #5 從 reviewer-convention 升級為 code-driven。新增 `.bandit` config + `.github/workflows/security-audit.yaml`（`scripts/tools/**` + `components/da-tools/**`、`bandit -ll -ii` = MEDIUM severity × MEDIUM confidence baseline）。Baseline 對 169 檔（52,816 LOC）跑出 13 個 medium+ findings、全部 triage 收斂為 0：1× B324 MD5 用 `usedforsecurity=False` 修正、1× B314 XML 與 11× B310 urlopen 走 inline `# nosec B<ID>  # rationale` 雙井號慣例（避開 bandit prose-as-test-id 警告）。**Rollout**：workflow `continue-on-error: true` 2 週 soak，無 false-positive flood 則翻硬 fail。bandit 覆蓋 Rule #5 shell / yaml_load / eval-exec-pickle / hardcoded-password（部分）；encoding / chmod 0o777 / stderr routing 三項 bandit 無原生 rule、暫留 reviewer convention。詳 [dev-rules.md §5](docs/internal/dev-rules.md#5-sast7-條安全-review-準則)。

- **Federation gateway 全域緊急斷路器（kill switch，issue [#551](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/551)）**：`helm/federation-gateway` 新增 `emergencyGlobalBlock` value（預設 `false`）。設為 `true` 時，gateway 的 Envoy route table 最上層多一條 `prefix: "/"` 的 `direct_response` 503 —— 每個請求直接回 `503`，不轉給 Layer 3 proxy 或 storage backend。用於 federation 事故（`prom-label-proxy` 0-day、storage 雪崩）時一鍵卸載**所有** federation 流量，取代逐租戶撤 token（撤銷另有 ~1-2min 最終一致延遲）。`tcpSocket` 健康探針不受影響（listener 仍接受連線）—— pod 不會被殺、開關可乾淨切回。經 GitOps 同步 + pod reload 生效（~3min）；需要瞬間切斷時 chart README 另記 `kubectl scale --replicas=0` 逃生門（即時但掉 in-flight、不入 git）。原 [#521](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/521) Gemini Day-2 review 的發想。chart version `0.2.3`→`0.2.4`。詳 chart README §Emergency global block。
- **Tenant federation 使用者文件（ADR-020 IV-2h，issue [#513](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/513)）**：新增 [`docs/integration/tenant-federation.md`](docs/integration/tenant-federation.md) —— 租戶 onboarding 的操作指南：取得 4h RS256 token（`POST /api/v1/federation/tokens`，需 tenant admin）、設定租戶側 Prometheus `/federate` scrape 與 Grafana data source（Bearer header、不走 URL）、支援的 read API（query／series／labels；**不支援** remote_read → 403）、2-tier policy（platform whitelist + tenant subset，附 sample subset YAML；明寫 whitelist 是治理機制非查詢期安全邊界）、配額回應碼對照（429／422／413／403／401 各自怎麼退避）。**Day-2 行為**兩條（Gemini round-4 review 要求）：撤銷的 ~1-2min 最終一致性（合規用語、明寫非缺陷）、斷線後「報復性 catch-up 查詢」反模式（超大範圍 `query_range` 撞 `--query.max-samples` → 422 → retry loop 卡死；正解是 `sample_limit` + 手動分段拉歷史）。定位明確區隔 [`federation-integration.md`](docs/integration/federation-integration.md)（ADR-004 平台內部 federation，方向相反）。順帶更新 `victoriametrics-integration.md` §7 的 federation 列：狀態 設計階段 → 已交付、修正「vmauth」措辭為 prom-label-proxy。詳 [ADR-020](docs/adr/020-tenant-federation.md)。
- **Federation offboarding 完整性：殭屍憑證偵測 + offboarding runbook（ADR-020，issue [#521](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/521)）**：租戶 offboarding 在本平台是「移除 `conf.d/<tenant>.yaml`」的 git 操作，但 federation 有兩樣東西不在那個檔裡、不會被一併帶走 —— token records（存在 `tenant-federation-store` ConfigMap，runtime state）與 per-tenant subset 檔（`conf.d/_federation/<tenant>.yaml`），形成殭屍憑證與孤兒設定檔。風險**低**（殭屍 token 對已刪租戶注入 `{tenant="X"}` 只回空集、無 live 資料外洩，且受 gateway per-token/per-tenant 限流 + 4h TTL 約束）—— 屬 offboarding-completeness／合規（SOC 2／ISO 27001）問題。交付 **Option A**：(1) 新增 [`tenant-offboarding-runbook.md`](docs/internal/tenant-offboarding-runbook.md) —— offboarding 的 federation 收尾程序（撤銷 token、刪 subset 檔、撤銷最終一致性的合規用語 ~1-2min）；(2) tenant-api 內建**被動偵測器** `OrphanDetector` —— 週期掃描，發現 token／subset 檔的母租戶已不在 conf.d 就噴 `slog` WARN + 更新 `/metrics` 的 `tenant_api_federation_orphaned_tokens`／`tenant_api_federation_orphaned_subset_files` gauge。偵測器**只觀測、不自動撤銷／刪檔**：自動依「租戶不在 conf.d」推論去撤銷，在 conf.d 暫態異常（GitOps sync 中、壞檔）時會誤殺活租戶憑證；warn-only 給同樣的「防遺忘」安全網卻零誤殺風險（A vs 自動 reconciler vs CI-pipeline 的決策過程見 [#521](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/521)）。詳 [ADR-020](docs/adr/020-tenant-federation.md) §Token model。
- **Federation 請求路徑 E2E 測試（ADR-020 IV-2j，issue [#516](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/516)）**：新增 `tests/federation-e2e/` —— docker-compose 把 federation 整條鏈拉起來（fixture-exporter → Prometheus ← prom-label-proxy ← Envoy gateway → mtail），host 端 pytest driver 走完整路徑驗 9 個情境：happy-path（簽 token → gateway 驗章 → proxy 注入 `{tenant="db-a"}` → storage）、跨租戶隔離（db-a token 的明確 `{tenant="db-b"}` selector 被 proxy 改寫回 db-a）、JWT enforcement（缺 token／偽簽章／錯 issuer／過期 token → 401，過期測試刻意把 exp 設在 clock-skew leeway 外）、撤銷傳播（改 revoked set → Lua reload 後 → 403）、Sybil 限流（多 token 打穿 per-tenant → 429 + mtail `rate_limited`）、1.5 MiB payload → Envoy buffer filter → 413、storage cap（查詢超 `--query.max-samples` → 422 + audit `bad_request`）、remote_read（`/api/v1/read` 及尾斜線變體 → 403）、Metadata API surface audit（IV-2g，issue [#512](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/512)：走完整 Prom HTTP API 表面 —— metadata endpoint 全部租戶隔離、無法隔離的 endpoint（`/targets`、`/status/*`、`/admin/*` 等）一律 404 不可達，杜絕 passthrough 洩漏）。**設計**：用 `helm template` 渲染**真實** gateway chart config 餵給 compose（no config drift —— 測的就是生產 artifact）；刻意不用 kind —— 對齊 repo 既有 `tests/e2e-bench` 不上 K8s 的決策、避開 Windows/WSL2/VirtioFS 環境的 I/O 成本（"test what you fly" 的範圍是請求路徑安全邏輯，那 100% 在 config + binary 裡）。**保真邊界**：測 request-path 安全邏輯（Envoy filter chain／Lua／proxy／storage cap），**不**測 K8s 編排層（Deployment／projected-volume swap／sidecar 起停）；tenant-api 不進 stack（其 token store 為 K8s ConfigMap-coupled），driver 以 tenant-api 同 claim shape 直接 RS256 簽 token。獨立 CI job `federation-e2e`，不進 `make test`／pre-commit／coverage gate。`make federation-e2e` 本地執行。詳 [ADR-020](docs/adr/020-tenant-federation.md) §後果（3-component coordination 風險）。
- **Federation storage blast-radius flags（ADR-020 IV-2c，issue [#508](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/508)）**：平台 Prometheus（`k8s/03-monitoring/deployment-prometheus.yaml`，即 federation-proxy 的 upstream）加上 ADR-020 Layer 1 的查詢硬上限 —— `--query.max-samples=5000000`（5M;tuning range 5M–50M）+ `--query.timeout=25s`,擋住單一 federation read 把 storage 打到 OOM 或 hang。兩 flag 皆為 **global**(此 Prometheus 同時服務平台 rule eval 與 federation read),5M 是 §Blast radius Layer 1 的 federation-driven 起點,內部 eval 若 false-positive 再上調(Prom 原生預設 50M / 2m)。`--query.timeout=25s` 刻意**短於** Layer 2 gateway 的 30s route timeout（**cascading timeout**,非等值）—— inner layer 先逾時才能砍 query、釋放記憶體並回精確 error code;等值會 race、gateway 先 504 而 Prometheus 仍在跑。`--query.max-samples` 與 pod `resources.limits.memory` **耦合**:cap 要當真正的 OOM 護欄,記憶體上限須裝得下「近上限查詢 + TSDB baseline」,故 pod limit 由 512Mi 上調 1Gi。global cap 也會誤殺平台自身的 recording/alerting rule —— 新增 `severity: critical` alert `PrometheusRuleEvaluationFailing`（`configmap-rules-platform.yaml`,監控 `prometheus_rule_evaluation_failures_total`),讓 rule eval 因撞 cap 而靜默失效時能被偵測,而非靠 YAML 註解。**範圍澄清**:本 repo 的 storage backend 是 raw k8s manifest 而非 Helm chart,VictoriaMetrics 為 BYO 整合（未由平台部署）—— 故 flag 落在 deployment manifest、issue 與 ADR Stage-4 的「Helm chart」措辭已修正;VM 對應 flag（`-search.maxUniqueTimeseries` 等）見 ADR §Blast radius Layer 1 表。詳 [ADR-020](docs/adr/020-tenant-federation.md) §Blast radius Layer 1。
- **Federation 稽核日誌 + 異常 metric（ADR-020 IV-2f，issue [#511](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/511)）**：`helm/federation-gateway` 補上 federation data-plane 稽核 —— Envoy access log 改成每 request 一筆結構化 JSON（`ts` / `tenant_id` / `token_id` / `method` / `path` / `query` / `status` / `duration_ms`），寫**兩個 sink**：`stdout`（持久、collector-ready 的合規軌跡）與 in-pod `emptyDir` 鏡像（供 metrics）。`query` 由獨立的 audit Lua filter **統一抽取** —— GET 從 URL query-string、POST 從 `application/x-www-form-urlencoded` body（`buffer` filter 提供 body，1 MiB 上限），取 `query=` 或 `match[]=`、URL-decode、截斷 2048；GET / POST 同一路徑、輸出格式一致。**filter 順序**：auth Lua（撤銷檢查 + header 注入）在限流器前、`buffer` + audit Lua 在限流器後 —— 被限流拒絕的請求不進 Envoy 記憶體緩衝，限流真正 bound 住 buffer 用量。新 metric `tenant_federation_requests_total{tenant,status}` 由 gateway pod 的 **mtail sidecar** tail `emptyDir` 鏡像產出（Envoy 原生 stats 無法產 per-tenant 高基數 label），`status` 為 HTTP code 分桶 enum（`ok` / `client_aborted`（status 0，client 提早中斷如 Grafana 取消查詢）/ `rate_limited` / `auth_failed` / `bad_request` / `backend_error`）；**logrotate sidecar** 以 10s 迴圈壓住鏡像大小（rename + Envoy `/reopen_logs`，不掉行；快迴圈 + 1 GiB emptyDir 上限防日誌洪流撐爆 pod 觸發 Kubelet 驅逐）。整組 metrics pipeline（sidecar + emptyDir + scrape）可由 `auditLog.enabled: false` 關閉 —— audit-sidecar image 尚未備妥時 gateway 仍能獨立運行（觀測 sidecar 不該能 down 掉主 gateway），stdout audit log 不受影響。新增 alert `FederationRejectionRateAnomaly` + `FederationGatewayBackendErrors`（`configmap-rules-platform.yaml` 的 `federation-audit` group，`severity: warning` — federation 屬平台自監控，併入 platform rule pack 而非另開 pack）+ `federation-audit` Grafana 儀表板。**架構修正**：原 ADR audit schema 的 `matched_whitelist_rule`（Data Plane Mirage 下查詢路徑不執行白名單）與 `series_returned`（Envoy 不解 response body 數 series）為幽靈欄位，砍除；`status` enum 砍掉不可能發生的 `rejected_whitelist`。**持久化邊界**：稽核日誌**不寫 PVC**（單一 RWO PVC 在 gateway 多副本 / `podAntiAffinity` 下無法掛載）—— durable 中央 forensic log store（Loki / SIEM）為 follow-up [#539](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/539)；IV-2f 交付 aggregate 層（metric，本即 durable + queryable）+ collector-ready emitter。control-plane 稽核（簽 token / 改 whitelist）軌跡沿用既有的 token Record ConfigMap + GitOps commit 歷史，不另建。sidecar 兩者共用 image（`audit-sidecar/Dockerfile`，Alpine + mtail + logrotate）。chart version `0.1.1`→`0.2.0`。詳 [ADR-020](docs/adr/020-tenant-federation.md) §Audit log + anomaly metric。
- **Federation admission validator（ADR-020 IV-2e PR-B，issue [#510](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/510)）**：在 PR-A 的 2-tier policy 之上補上 admission validator —— whitelist 加入新 metric 時，驗該 metric 是否真能 federate。tenant-api 首次新增 Prometheus client（`internal/federation/admission.go`），**只走 Series metadata API**（`GET /api/v1/series`）、不用 range query —— range query 會把 24h raw sample 載進記憶體、對高基數 metric 把 Prometheus 打到 OOM。三態：metric 有帶 `tenant` 的 series → **Pass**；有 sample 但**沒有任何** series 帶 `tenant` → **Hard block**（federate 後對所有租戶都是 empty vector）；24h 無 sample → **Warn**（cold-start / sparse metric 合法，需 `--force`）。判準是「**沒有任何** series 帶 `tenant`」而非「有 series 缺 `tenant`」—— K8s 共享叢集裡 `up` / `container_*` 等 metric 租戶 pod 帶 label、平台 pod 不帶，proxy 已隔離、平台 series 無害；探測用 `metric{tenant!=""}`（非空即 Pass，該 series 也是 PII 掃描的真實租戶樣本）。每次查詢三重 bound（`limit=1` + `io.LimitReader` + `context` 5s timeout），validator 自身不會變 DoS 來源；後端不可達 / timeout 視為 Warn。另含 **PII label-name heuristic**：label 名命中 `email` / `customer` / `user_ip` 等樣式 → advisory soft warning。`--force` bypass：`PUT /api/v1/federation/policy` body 加 `force` / `reason`，**hard block 不可 force**；soft warning（Warn / PII）force 通過時，user + reason + metrics 寫進該次 git commit message 的 `[Bypass-Validator]` trailer —— GitOps 不可繞、不 rotate 的稽核軌跡（trailer 欄位 CR/LF 淨化，防注入偽造）。admission 只驗**新增**的 metric（與現行 whitelist 取差集），並行檢查（bounded 8）、單次 PUT 新增上限 30；寫 git 前檢查 request context 未取消（防 timeout 後的殭屍寫入）。新 flag `--federation-prometheus-url`（空值停用 admission，whitelist 編輯僅 schema 檢查）。詳 [ADR-020](docs/adr/020-tenant-federation.md) §前提約束。
- **Federation 2-tier policy schema + endpoint（ADR-020 IV-2e PR-A，issue [#510](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/510)）**：tenant-api 新增 federation 的 2-tier metric 政策 —— **platform whitelist**（maintainer 管，`_federation_policy.yaml`，平台策展、提供 federation 的 metric catalogue）+ **per-tenant subset**（租戶自選子集，`conf.d/_federation/<tenant>.yaml`）。**定位澄清**：whitelist 是 **governance / discovery** 機制，非 query-time 安全邊界 —— prom-label-proxy 只做 `{tenant="<X>"}` label 注入、無 metric-name allowlist 能力，跨租戶隔離 100% 來自 label 注入、與 whitelist 無關（ADR-020 §MVP 範圍 流程圖原列「proxy 拒絕白名單外 metric」係 hallucination，本 PR 一併修正，並補上 hard-revocation 警告：從 whitelist 移除 metric 擋不住已知名稱的查詢，緊急阻斷須走 ingestion 階段 `relabel_configs` drop）。新 endpoint：`GET` / `PUT /api/v1/federation/policy`（whitelist，PUT 需 platform admin —— 即透過 `tenants: ["*"]` 的 admin group；`HasPermission(groups, "*", admin)` 只有 `*`-scoped rule 會中）、`GET` / `PUT /api/v1/tenants/{id}/federation`（tenant subset，PUT 需該租戶 admin，門檻對齊 token 簽發 #509）。核心驗證為 **2-tier containment**：tenant subset 的每個 metric 必須在 platform whitelist 內，超出即 `400`；另驗 metric 名合法性（Prometheus grammar）與去重。`GET /tenants/{id}/federation` 採 **read-repair**：回傳前把存檔的 subset 與當前 whitelist 取交集 —— whitelist 縮減後既有 subset 檔會殘留過期 metric，讀取端取交集即得當前合法子集，毋須掃改租戶檔（GitOps mass-commit 災難）。兩層 policy **刻意分檔**：subset 一檔一租戶，租戶自助改 subset 不會在共用檔上互相 Git merge conflict，維持 per-tenant blast-radius 隔離（subset 不放進 `<tenant>.yaml` —— 該檔的 `PUT /tenants/{id}` 是 full-file replace，混放會被覆蓋）。驗證為手寫 Go（對齊 codebase 慣例，不引入 json-schema 依賴）。新增 `internal/federation/policy.go`（`PolicyManager` embed `configwatcher.Watcher`，SHA-256 熱載）+ handler + gitops `WriteFederationPolicyFile` / `WriteFederationSubsetFile`。此為 #510 的 **PR-A**；admission validator（Prometheus metadata 查詢 + 三態 + PII heuristic + `--force` commit trailer）為 PR-B。詳 [ADR-020](docs/adr/020-tenant-federation.md) §MVP 範圍 / §前提約束。
- **Tenant-api Helm chart federation 接線（ADR-020 IV-2m，issue [#519](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/519)）**：`helm/tenant-api` chart 新增 `federation` 區塊（預設關閉），把已 merge 的 federation token endpoint 實際接上 Helm 部署 —— 在此之前 endpoint 程式碼雖在 main，但 chart 沒掛簽章金鑰、沒接 token store，`helm install` 起不來。啟用後 chart：(1) 預建空的 `tenant-federation-store` ConfigMap —— tenant-api 在 runtime 才寫 `store.json` / `revoked.txt`，template 刻意不渲染 `data`，Helm three-way merge 因此不會在 `helm upgrade` 時重置它、不會清掉有效 token（`resource-policy: keep` 再擋 `helm uninstall`）；(2) 加一組 `Role` + `RoleBinding`，以 `resourceNames` 把 tenant-api 的 K8s API 權限鎖死成「對那一個 ConfigMap 的 `get` + `update`」，無 namespace-wide `create`；(3) 掛載 `da-tools fed-key`（IV-2l）帶外產出的簽章金鑰 Secret 為 `defaultMode: 0440` volume（檔主為 root、process 非 root，靠 fsGroup 65534 群組位讀取）；(4) pod 開回 `automountServiceAccountToken`（in-cluster client 需要，ServiceAccount 預設關閉）；(5) 透過 values 暴露 `--federation-key` / `--federation-store` / `--federation-token-ttl` 三個 flag。既有部署的啟用步驟見 `values.yaml` 的 `federation` 區塊註解。chart version `2.8.0`→`2.9.0`。注意：issue #519 body 寫的「store 是 pod-local JSON 檔、MVP 限 `replicaCount=1`」已被 Posture B（#520 ConfigMap-backed store）取代，本實作以 ConfigMap store 為準。詳 [ADR-020](docs/adr/020-tenant-federation.md) §Token model。
- **Federation 簽章金鑰 bootstrap / 輪替（ADR-020 IV-2l，issue [#518](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/518)）**：新增 `da-tools fed-key` 命令（`scripts/tools/ops/federation_keygen.py`）—— 產生 / 輪替 federation JWT 的 RS256 簽章金鑰。私鑰直接吐成 Kubernetes Secret manifest 到 stdout（`da-tools fed-key | kubectl apply -f -`，記憶體→pipe→etcd,不落地、不進剪貼簿）；stdout 為互動式終端時拒絕輸出（防 operator 漏接 `| kubectl` 把私鑰印進 terminal scrollback）；公鑰寫成 JWKS 檔供 federation-gateway 的 `jwt.jwks`。每把公鑰的 `kid` 是它的 **RFC 7638 JWK thumbprint**；`--rotate --existing-jwks` 把新公鑰併入現有 JWKS（kid 區分舊新,grace-period overlap）。tenant-api 同步改動：簽 token 時對載入金鑰算同一個 RFC 7638 thumbprint、注入 `kid` JWT header —— gateway 的 `jwt_authn` 因此能用 `kid` O(1) 選鑰,不必遍歷 JWKS（關閉輪替期「壞簽章 flood × N 把鑰 = N 倍 RSA」的放大攻擊面）。輪替標準流程（計畫性 grace overlap / 私鑰外洩緊急汰換）見新增的 [`federation-key-rotation-runbook.md`](docs/internal/federation-key-rotation-runbook.md)。詳 [ADR-020](docs/adr/020-tenant-federation.md) §Token model。
- **Federation API gateway Helm chart（ADR-020 IV-2b，issue [#507](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/507)）**：新增 `helm/federation-gateway` chart — ADR-020 Layer 2 的 federation API gateway，以 **Envoy**（`distroless-v1.38.0`）實作。它是「簽發 token 不做 server-side revocation list」的對價控制。每個 request 走 cheap-before-expensive 的 filter chain：per-IP 粗粒度 rate limit（在任何 crypto 前先擋偽造 token flood）→ `jwt_authn` RS256 驗章（local JWKS + jwt_cache + 60s clock-skew leeway，`from_headers` only 故 URL 帶 token 一律拒絕、不會進 log）→ Lua filter（查 revoked-set + 把驗證過的 `tenant_id`/`token_id` 用 `replace()` 覆寫進 trusted header，故 header spoofing 結構上不可能）→ per-token + per-tenant 雙層 `local_ratelimit`（防單一 token 濫用 + 防租戶 round-robin 16 個 token 的 Sybil）→ 轉送 upstream。`mode` 二選一：`prom-label-proxy`（注入 header 轉 Layer 3 proxy）或 `vm-cluster`（rewrite path 到 `/select/<tenant_id>/prometheus/` 轉 vmselect）。revoked-set 由 tenant-api 寫進 `tenant-federation-store` ConfigMap、gateway 掛載後 Lua 以 time-gated cache 重讀（tmpfs projected volume，microsecond 記憶體讀；缺檔 fail-open）。audit log 在此層做（JSON access log 帶驗證過的 claim）。rate limit 為 per-instance 軟性控制 — 硬上限是 Layer-1 storage cap。Day-4 resiliency 比照 IV-2a（HPA / PDB / anti-affinity / graceful shutdown）。`envoy --mode validate` 驗證 config 通過。詳 [ADR-020](docs/adr/020-tenant-federation.md) §Blast radius Layer 2。
- **Federation read-path proxy Helm chart（ADR-020 IV-2a，issue [#506](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/506)）**：新增 `helm/federation-proxy` chart — ADR-020 Layer 3 的租戶隔離 proxy，部署 `prom-label-proxy`：改寫 PromQL、把 `{tenant_id="<X>"}` 強制注入每個 selector 與 metadata API，front 任何相容 Prometheus query API 的後端（Prometheus / Thanos / VictoriaMetrics 單機）。實作盤點修正了 ADR 的兩個架構誤解：(1) vmauth 是 auth router、**不**解析 PromQL 也不注入 label；(2) vmauth 靠靜態 `auth.yml` 路由，無法消化動態簽發的 federation JWT — 故 **vmauth 不納入本 chart**，VM cluster 的 Layer 3 隔離改由 gateway（IV-2b）直接 URL rewrite 到 accountID 路徑處理。metadata API enforcement（`-enable-label-apis`）hardcode 不可 override；`-error-on-replace` 刻意不啟用（預設靜默覆蓋租戶 label — 隔離等價但允許 SRE 直接複製貼上帶 `tenant_id` 的 query）；NetworkPolicy 預設限定只有 federation gateway 能連入（proxy 信任 gateway 設的 tenant header，跳層即破防）；HPA 依 CPU 70% 擴容。prom-label-proxy image pin `v0.13.0`。Day-4 resiliency：soft pod anti-affinity（replica 跨節點分散）+ PodDisruptionBudget（node drain / cluster upgrade 保活）+ 原生 `preStop.sleep` 與 `terminationGracePeriodSeconds` 45s（rollout / scale-down 不腰斬 in-flight query）+ `GOMEMLIMIT`（防 AST 解析 burst 撞 cgroup 上限被 OOMKill）。audit log 因 prom-label-proxy 無原生支援，移交 gateway（IV-2b）。詳 [ADR-020](docs/adr/020-tenant-federation.md) §Blast radius Layer 3。
- **Tenant federation token endpoint（ADR-020 IV-2，issues [#509](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/509) / [#520](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/520)）**：tenant-api 新增 `POST` / `GET` / `DELETE /api/v1/federation/tokens` — 為租戶簽發短效（預設 4h）RS256 JWT，供其向 label-injection proxy（vmauth / prom-label-proxy）拉取自己的 metrics 子集回租戶側 infra 自管（ADR-020 §Token model）。簽發需對目標租戶具 `admin` 權限（資料域外持出，門檻高於 config write）；token claim 帶 `tenant_id` / `token_id`（proxy 注入 label、gateway 取 rate-limit key 的跨組件契約）+ `aud=tenant-federation`（防 cross-service replay）。`DELETE` 為真撤銷（ADR-020 Posture B）：移除 bookkeeping record 並把 token id 寫入 gateway 消費的 revoked set，最終一致 — 約 1-2 分鐘內隨 ConfigMap projected-volume sync 生效。token record 存於跨 replica 共用的 Kubernetes ConfigMap（`--federation-store` 指定其名、Helm chart 預建），tenant-api 維持 stateless、可多 replica，不入 db 也不入 git conf.d。濫用防線：每租戶同時最多 16 個有效 token、每分鐘簽發上限，超出分別回 `409` / `429`。新增 `internal/federation` package（RS256 簽章器 + ConfigMap-backed record store）；`--federation-key` 未設時整個 endpoint 不註冊。詳 [ADR-020](docs/adr/020-tenant-federation.md)。

### Changed

- **Lint adoption policy：open-source engine + Vibe wrapper（取代 DIY-only，epic [#448](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/448) / TRK-315）**：新 lint 預設採「既有 open-source engine（hadolint／kube-linter）優先 + Vibe wrapper 疊上專案政策（severity／中央 exemption／scope）」，取代過去逐案 DIY `check_*.py` 的 reactive whack-a-mole。**僅 greenfield 套用** —— 既有 ~50 支 DIY lint 不回頭遷移（明列 out of scope）。Container/k8s IaC SAST 4 層（TRK-311~314）為首批落地：L1 Dockerfile（hadolint）、L2 Helm template + L4 raw k8s manifest（kube-linter）、L3 values/manifest secret-shape（純 Vibe wrapper —— YAML-shape 檢查無對應 open-source engine）。統一 Severity→Action（Critical → BLOCK required-check／High → 中央 EXEMPTIONS 列管／其餘 INFO）+ consolidated 4 層 baseline（0 Critical / 11 baseline-High）收斂於 `docs/internal/iac-lint-baseline.md`；hybrid policy 規範寫入 `dev-rules.md §安全紀律`。
- **tenant-api server timeout + body-size 改 Helm-tunable（issue [#144](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/144)）**：`tenant-api` 的 HTTP server timeouts 與每 handler 1 MiB request body cap 從 hardcoded 改為 `TA_READ_TIMEOUT` / `TA_WRITE_TIMEOUT` / `TA_IDLE_TIMEOUT` / `TA_MAX_BODY_BYTES` env 驅動（時間 timeouts 早於本 issue 已 env-driven，本 PR 補 body cap + 全部接 Helm value）。`helm/tenant-api` 新增 `tenantApi.server.{timeouts.read,timeouts.write,timeouts.idle,maxBodyBytes}` values（預設值對齊 binary built-in，default upgrade 為 no-op），chart 條件式發 env vars（未 override 時不長 env block）。malformed env（負數、0、非數字）→ `slog.Warn` + fallback 預設，沿用 `RateLimitConfigFromEnv` pattern。8 個 handler 的 `io.LimitReader(r.Body, 1<<20)` 統一走 `d.MaxBody()` helper（unset 時 fallback `DefaultMaxBodyBytes`，避免 12 個既有 test fixture 連帶改動）。Chart `2.9.0`→`2.9.1`。`docs/api/tenant-api-hardening.md §5.2` 標 "moved to Helm"。詳 [ADR-009](docs/adr/009-tenant-manager-crud-api.md)。

### Fixed

- **Platform 日誌彙整 runtime smoke-test 兩個 chart bug + 兩個預設值缺口（issue [#539](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/539) Phase 1 follow-up）**：把 `helm/vector` + `helm/victorialogs` 真的部到 kind cluster 跑 runbook §2 五條 LogsQL，抓到兩個 chart-only review 沒蓋到的 runtime bug 與兩個預設值不對齊問題。**Bug 1 (vector)**：configmap.yaml 的 VRL `. = merge(., object!(parsed), deep: true) ?? .` 在 Vector 0.55+ 噴 `unnecessary error coalescing operation` —— `merge(object, object)` 是 infallible，`??` 是多餘的（VRL 編譯器把它當錯誤而非 warning，pod crashloop）。修：拿掉 `?? .`。**Bug 2 (vector)**：daemonset.yaml 把 hostPath 掛在 `/vector-data-dir`，但 Vector 預設 `data_dir` 是 `/var/lib/vector`（落在容器 root），跟 `containerSecurityContext.readOnlyRootFilesystem: true` 衝突 → `kubernetes_logs` 起不來「Could not create subdirectory ... Read-only file system」。修：configmap.yaml 顯式設 `data_dir: /vector-data-dir` 對齊掛載點。chart version `0.1.1`→`0.1.2`。**缺口 1 (victorialogs)**：`persistence.size` 預設 10Gi 跟 runbook §5 capacity 公式（federation audit ~50 RPS × 1 KB × 30d × 1.3 / ~10× 壓縮 ≈ 13 GB on disk）對不上，給 ~2× headroom 改 30Gi（runbook 已寫了該數字）。chart version `0.1.0`→`0.1.1`。**缺口 2 (vector)**：`metrics.enabled=true` 時沒有對應 Service，operator 想開 Prometheus scrape 還得手寫；補 `templates/service.yaml`（headless 因為每顆 Vector 是獨立 pod-IP scrape target，非 routing 後端），含 `prometheus.io/scrape` annotation。chart version 同上一次 bump。**驗證紀錄**：兩個 chart 在 kind cluster `helm install` → `log-generator` test pod 同步噴 JSON 與 plain-text 兩種行 → Vector tail + VRL demux → VictoriaLogs `_stream` 出現 `log_type=federation_audit` 與 `log_type=gateway_operational` 兩條 stream（schema 完全對齊 #539 §3 表：`app`/`k8s_namespace`/`log_type`/`tenant_id`/`status` 進 stream；`pod_name`/`token_id`/`query`/`path`/`method`/`ts`/`duration_ms` 留 data field）→ Grafana datasource provisioning + plugin 自動裝（`victoriametrics-logs-datasource` v0.27.1 從 grafana.com 抓）→ 透過 Grafana proxy API 查 `log_type:federation_audit` 取回 row。AC4 + AC5 端到端證實。

- **Federation 標籤名地雷：proxy 注入 `tenant_id`、平台 data layer 用 `tenant`（ADR-020 IV-2.0，issue [#505](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/505)）**：IV-2.0 前置 audit 盤點 data-layer 租戶 label 現況時抓到一個會讓 federation 全盤靜默失效的 mismatch —— `helm/federation-proxy`（IV-2a #506）以 `prom-label-proxy -label=tenant_id` 啟動、對每個 PromQL selector 強制注入 `{tenant_id="<X>"}`，但平台 data layer 既有的租戶 label 名是 **`tenant`**（Prometheus relabel `target_label: tenant`、threshold-exporter、tenant-scoped rule pack 一律 `on(tenant)`；`tenant_id` 在 `k8s/` data-layer 設定裡一次都沒出現）。注入名不符 → 配不到任何 series → **每一個 federated 租戶查詢回 empty vector**，範圍 100%。修正：`helm/federation-proxy` 的 `tenant.label` 預設由 `tenant_id` 改 `tenant`（chart README 同步、chart version `0.1.0`→`0.1.1`）；`docs/adr/020-tenant-federation.md` 把 prose 中「proxy 注入到 metric 的 label」一律對齊 `tenant`（JWT claim 仍名 `tenant_id` —— claim 名與 metric label 名為獨立命名空間，互不要求一致）。新增前置 audit 文件 [`federation-label-enrichment-audit.md`](docs/internal/federation-label-enrichment-audit.md)：metric family 現況盤點表 + federation whitelist 的 eligible / ineligible 初始清單（IV-2e #510 的輸入）+ follow-up（cAdvisor `container_*` 缺 scrape-time `tenant`、admission validator）。詳 [ADR-020](docs/adr/020-tenant-federation.md) §前提約束。
- **Federation token 每租戶 16-上限的 TOCTOU 競態（issue [#527](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/527)）**：`Manager.Issue` 原以「`store.list()` 數一次 → 比對 `>= maxTokensPerTenant` → `store.put()`」的 check-then-act 把關每租戶 token 上限,但 list 與 put 是兩次獨立 store 往返。多 replica 併發簽發同一租戶時,各 replica 都 `list()` 看到 < 16 → 都 `put()` append,16 上限被擊穿、Sybil 防線失效。修正把上限檢查**下推進 store 的寫入交易**:`configMapStore.put` 在 `RetryOnConflict` 閉包內、對當次載入的最新文件清點該租戶 live record,達上限即回 `ErrTokenLimitReached`(閉包每次 retry 都對新狀態重檢,故 check 與 append 是單一 atomic compare-and-swap);in-memory `store.put` 在同一把 mutex 下做等價檢查。`Manager.Issue` 移除前置 list 檢查,改由 `put` 單點權威把關。
- **Federation gateway 在 prom-label-proxy 模式拒絕 `remote_read`（issue [#529](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/529)）**：`helm/federation-gateway` 原以單一 `prefix: "/"` route 轉發所有路徑。`prom-label-proxy` 模式下,upstream 的 prom-label-proxy 只對文字查詢 API（`/api/v1/query[_range]`、`/series`、`/labels`、`/federate` 等）強制注入 tenant label —— Prometheus `remote_read`（`/api/v1/read`,Snappy-framed protobuf body）不在其列、無法被 label-scope。新增條件式 Envoy route:`prom-label-proxy` 模式對 `/api/v1/read` 直接回 `direct_response` 403,不再把 Layer 3 無法做租戶隔離的請求轉下去。`vm-cluster` 模式不受影響 —— `revoked_check.lua` 會把路徑改寫進租戶的 `/select/<id>/` accountID 空間,`remote_read` 連同隔離一併成立。gateway chart README 新增「Supported read APIs」段說明各模式可用的讀取 API;chart version `0.1.0`→`0.1.1`。`envoy --mode validate` 通過。
- **Federation gateway `/api/v1/read` 403 guard 的 non-canonical path 繞過（ADR-020 IV-2b 加固,hardens issue [#529](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/529)）**：#529 為 `prom-label-proxy` 模式加的 `/api/v1/read` 403 `direct_response` route 用 Envoy **exact `path:` match** —— `POST /api/v1/read/`（尾斜線）/ `POST /api/v1//read`（雙斜線）都不命中該 route,會 fall through 到 catch-all `prefix: "/"` 被轉給無法對 Snappy-framed remote_read body 做 tenant label-scope 的 Layer 3 prom-label-proxy,即一條跨租戶資料外洩路徑。修正兩處:(1) `HttpConnectionManager` 加 `merge_slashes` + `normalize_path`,在 routing 前把路徑正規化（`/api/v1//read` → `/api/v1/read`、RFC 3986 dot-segment);(2) 403 route 的 match 由 exact `path:` 改為 `path_separated_prefix: "/api/v1/read"` —— 涵蓋 `/api/v1/read` 與其所有 sub-path、且在 path-segment 邊界比對,故不會誤擋 `/api/v1/readiness` 類 sibling。此 bypass 由 #540（IV-2f audit-log）的 Gemini adversarial review 發現,刻意拆為獨立 security fix（base rebase 至 #540 merge 後的 main）。chart version `0.2.0`→`0.2.1`;`envoy --mode validate` 通過。詳 [ADR-020](docs/adr/020-tenant-federation.md) §Blast radius Layer 3。
- **Federation gateway `/api/v1/read` 403 guard 的 escaped-slash（`%2F`）殘留繞過（ADR-020 IV-2b 加固,hardens [#529](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/529) / [#542](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/542)）**：#542 為 `/api/v1/read` 403 route 補的 `merge_slashes` + `normalize_path` 收掉了雙斜線與 RFC 3986 dot-segment,且 `normalize_path` 依 RFC 3986 §6.2.2.2 會 percent-decode **unreserved** 八位元組（`%72`→`r`、`%2e`→`.`)—— envoy v1.38 實測 `%72ead` / `%2e` 類變體早已命中 403。但同條 RFC **刻意不解碼 reserved 字元**:百分號編碼的斜線 `%2F` 維持編碼,`/api/v1%2Fread` 因此不命中 `path_separated_prefix: /api/v1/read`、fall through 到 catch-all `prefix: "/"` 被轉給 upstream —— #542 宣稱「no non-canonical variant can slip past the guard」未收掉的最後一個變體。修正:`prom-label-proxy` 模式的 `HttpConnectionManager` 補 `path_with_escaped_slashes_action: UNESCAPE_AND_FORWARD`,在 routing 前把 `%2F`(與 `%5C`)解碼,路徑完全正規化後 403 guard 即無縫;`path_separated_prefix` 的 path-segment 邊界比對不受影響 —— `/api/v1%2Freadiness` 解碼為 `/api/v1/readiness` 仍是 sibling、不被誤擋(envoy v1.38 實測 7 變體確認)。**mode-scoped 到 `prom-label-proxy`**:該 403 guard 只存在於此模式,`vm-cluster` 由 `revoked_check.lua` 自行改寫路徑、不套此設定。chart version `0.2.1`→`0.2.2`;`helm template | envoy --mode validate` 通過。詳 [ADR-020](docs/adr/020-tenant-federation.md) §Blast radius Layer 3。
- **Bench-gate（Tier 1 + Tier 2）regression detector 對非確定性 `MB-sys` / `MB-heap-after-gc` false-RED → 改 scope 到確定性 metric（issue [#608](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/608)）**：兩條 bench-gate workflow（`bench-gate-pr.yaml` Tier 1 α=0.01／5%、`bench-gate-release.yaml` Tier 2 α=0.05／10%）的 regression 偵測都是「grep 整份 benchstat 輸出抓任何 `+N% (p<α)` 列」，**未依 metric column 限定範圍**。bench 透過 `b.ReportMetric` 發兩個 process-level 非確定性指標 —— `MB-sys`（`runtime.MemStats.Sys`）與 `MB-heap-after-gc`（`config_hierarchy_bench_test.go`），是 GC/runtime high-water 讀值、非 per-op work：跨 process 隨機漂移（同一份 code 連跑兩次會 sign-flip）、且 `-count` run 內 pseudo-replication 使 within-run variance≈0 → 任何 between-process offset 都被讀成「顯著」（p=0.000）→ 真實 Go PR 被 false-RED（[#502](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/502) spurious RED 的根因；[#459](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/459)／[#606](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/606) 獨立判定 `sys_bytes` 高水位爬升是 Go GC pacing 而非 code leak，互相佐證）。修正：兩條 workflow 的 benchstat 調用加 `-filter '.unit:(sec/op OR B/op OR allocs/op)'`，把偵測**與顯示**都限縮到確定性 per-op metric（benchstat 把 `ns/op` 正規化為 `sec/op`）；此 allowlist 自動排除 `MB-sys`／`MB-heap-after-gc`／`goroutines`／`affected-tenants` 及任何未來新增的 custom metric，grep/awk 偵測邏輯與 INCONCLUSIVE 防線**完全不動**。**MB-sys/heap 保留發出**（仍進 nightly `bench-record` artifact + release-attached baseline 供資源趨勢 informational 檢視），僅從兩條 gate 排除。控制實驗（Dev Container, real benchstat@latest, 真實 metric 欄位格式）：MB-sys-only +25% drift → regression=false（不再 RED）、注入 B/op +20% canary → regression=true（仍 RED）。**時效**：須在 v2.9.0 打 tag 前 merge（[#427](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/427)），否則 v2.9.0 首次 Tier 2 release 演練會被同一 MB-sys FP 污染。
- **Bench-gate（Tier 1 + Tier 2）對 benchstat 輸出格式漂移的 silent-pass 加固（PR [#611](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/611) review 期間發現，延伸 [#608](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/608)）**：#608 加的 `-filter` 讓 regression 偵測**雙重依賴** benchstat 的輸出格式 —— grep `+N% (p=...)` 列的 row 格式，加上 `-filter` 的 unit 名（`sec/op`／`B/op`／`allocs/op`）。benchstat 以 `@latest` 每次 CI 重裝，未來某版若改 row 格式或 rename 某個 unit，兩條 grep 會**全部 miss → regression=false → GREEN gate 靜默放行所有真實 regression**，且**無需任何 repo 變更**即可觸發（單純某次 benchstat 發版）；既有 `^cpu:` INCONCLUSIVE 防線抓不到（cpu header 不隨輸出格式改變消失）。修正：兩條 workflow 的 Compare step 在 `tee benchstat.txt` 後加 **fail-loud shape assertion** —— 要求 filtered 比較輸出至少含 1 個 metric-section header（`sec/op`／`B/op`／`allocs/op`）**且**至少 1 條帶 `± N%` 的 per-bench result row，否則 `::error::`（明示「pin benchstat 版本」）+ `exit 1`；偵測邏輯與 INCONCLUSIVE 防線不動，兩 tier 斷言完全相同。控制實驗（Dev Container, real benchstat@latest）：真實輸出 pass、empty／garbled／全-unit-rename／`± N%`-移除 四種 drift 全數 exit 1。
- **Federation 稽核 metric `tenant_federation_requests_total` 在生產環境從未被產出（ADR-020 IV-2f 修正,由 IV-2j E2E [#516](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/516) 首跑抓到）**：`helm/federation-gateway` 的 mtail 程式 `files/federation-audit.mtail` 以單一 regex `"tenant_id":"…".*"status":…` 從 Envoy JSON access log 抽 `tenant_id` 與 `status`,但該 regex 假設 `tenant_id` 在 `status` **之前**。Envoy 的 `json_format` access log 不論 config 內欄位順序,輸出一律把 key **依字母排序**(`status` < `tenant_id`),故 `status` 永遠在前、該 regex 永不命中。後果:mtail 雖逐行讀入 access log,`tenant_federation_requests_total{tenant,status}` 一個 sample 都不產出 —— IV-2f 的 federation 稽核 metric、`FederationRejectionRateAnomaly` / `FederationGatewayBackendErrors` alert、`federation-audit` 儀表板自 #540 merge 起即靜默全失效(audit log 本身的 JSON 欄位完整、不受影響)。修正:改用 mtail nested pattern 分別抓兩個 key —— `/[{,]"tenant_id":…/ { /[{,]"status":…/ { … } }`,與 JSON key 順序完全解耦;`[{,]` anchor 確保只命中真正的 top-level JSON key,不會誤中 `query` 值內的 `status` 子字串。本 bug 即由 IV-2j E2E 的 S5(Sybil 限流 → `rate_limited`)/ S7(storage cap → `bad_request`)情境首跑抓到 —— E2E 直接斷言這條 metric。chart version `0.2.2`→`0.2.3`。詳 [ADR-020](docs/adr/020-tenant-federation.md) §Audit log + anomaly metric。

### DX

- **兩條開發紀律 codify（epic [#570](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/570) retrospective）**：epic 評估抓到兩個跨任務通用的改善點，用最低成本釘進 SSOT（**不開新 epic、不 re-bloat tier-1 CLAUDE.md**）。**(1) dev-rules §P4「實證驅動」**：PR 宣稱的數字（token/行數/coverage/節省）須附可重現量測指令、新機制 PR body 須含「怎麼證明有效」——無量測 = 杜撰（#570 燒過 110 行/~1000 token 全估值被打臉）。為守 dev-rules 520 行 cap，順手 condense 既有 §P3 的 SOP 敘述、淨維持 518 行（**自身就示範了「不盲加」**）。**(2) `quarterly-audit-sop.md` skill 汰除鐵律**：本地 `vibe-*` skill 連續 2 季在其領域 0 觸發 = dead weight，季度 audit 強制刪除（治 epic 交付但 0-1 觸發的 Gap A，對齊 `feedback_speculative_drift_prefer_remove`）。

- **CLAUDE.md 瘦身 + epic #570 收尾核算（TRK-310）**：epic 因加 always-on 高頻地雷 + 3 skill + pointer 增長：起點 **110 行 / 7,706 字** → peak **133 行 / 9,804 字**。本項把既有 verbose 段下放/收 pointer（測試 Seam table → test-map.md、優先級宣告 + 環境層 bullets → inline、起手式 blockquote 合併），收回 **109 行 / 9,169 字**，5 條高頻地雷 + dev-rules Top 4 + 計數短語零損。**誠實核算（修正初版基於行數的「net-negative」誤述）**：行數 109 < 起點 110，但**字元/token 仍 +19%（7,706→9,169）—— line count 是誤導 proxy，token 才準**；slim 實際效果 = peak −6.5% 字元。epic 整體 tier-1 token net **取決於 plugin prune**（~900-1000 tok/turn，**估值、未在本 repo 量測**）是否 > CLAUDE.md 的 +~430 token，**非來自 CLAUDE.md 變小**。瘦身**安全性經 recall test 實證**：乾淨 subagent 冷讀，5 條 ⛔ 高頻地雷 + dev-rules Top 4 **100% 可抽出 → 壓縮沒埋死線**（方法 codified 進 [`quarterly-audit-sop.md`](docs/internal/quarterly-audit-sop.md)）。epic #570 至此全數收尾。

- **Upstream skill-system FR tracker（epic [#570](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/570) / TRK-309）**：新增 `docs/internal/skill-system-feature-requests.md`，收集 6 個**需上游（Anthropic / Cowork）做、Vibe 無法單方解決**的 skill-system 改善（project-scoped allowlist / keyword-gated lazy router / description style-guide+lint / anti-trigger metadata 標準化 / SKILL.md section anchor / usage telemetry CLI）+ 量化背景（~80 skill 描述 ≈ 4000 token/turn 全載）+ 糾錯 / 中長期發想附錄。來源：2026-05-21 superpowers / skill-system 評估；Vibe 內部能做的已落地（epic #570），本表是剩下的 upstream gap。不入 CLAUDE.md / doc-map catalog（internal SSOT，per CLAUDE.md internal-exempt 政策）；由 `hook-vs-skill-coverage.md` §關聯 cross-ref 避免 orphan。

- **兩個本地 skill：`vibe-release` + `vibe-brainstorm`（epic [#570](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/570) / TRK-306 + TRK-308）**：本地 skill 四 → 六。**`vibe-release`（TRK-306）**：五線版號 release 收尾 SOP，consume `feedback_release_wrapup_discipline` 的三條紀律（pre-tag audit / CHANGELOG distill + project-face refresh / roadmap milestone-link）+ release-type 分流（GA / interim DX / hotfix）；延伸 [#474](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/474) Layer 3 的 inline checklist 為系統化流程（Layer 1/2 的 docker build + Trivy 已被 #474 機械化進 `make pre-tag`）。**`vibe-brainstorm`（TRK-308）**：設計階段 Socratic ideation，借 superpowers `brainstorming` pattern + 從 ADR-020 federation epic 實際流程（四輪 strategic discussion + 兩輪外審）萃取的五個 Vibe 設計提問（reuse-over-build / MVP-vs-Future-Work / explicit trade-off / defer-with-trigger / blast-radius）；anti-trigger SKIP code-level（→ `engineering:debug` / `vibe-subagent-review`）。TRK-308 原 deferred-to-post-ADR-020，#380 closed 後解鎖。doc-as-code：CLAUDE.md「Skill 體系」四→六 + `hook-vs-skill-coverage.md` §5 同步。

- **Trigger-asymmetry safety net — PR-time component Docker build + Trivy gate（[#474](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/474)）**：堵「`release.yaml` 在 tag push 才 build image，build break 在最糟時機才爆」的 dormant-bug pattern（v2.8.0 release 30 分內中 2 次：[#472](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/472) 移檔 COPY、[#473](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/473) 缺 pkg COPY + CVE）。三層：**(L1)** 新 `.github/workflows/component-docker-build.yaml` — PR 觸碰 component build 輸入路徑時，matrix `docker build --load`（**hard gate**）+ Trivy（**informational**，exit-code 0）；forked PR 可跑（local load 不需 secret）。涵蓋 **全 4 個 production Dockerfile**：threshold-exporter / da-portal / tenant-api 自包含；**da-tools 以 stub 納入**（空 `tools/` + `touch` 預編 Go binary —— 因 Dockerfile 只 COPY+chmod 不執行 binary，stub 即可驗 COPY 路徑 / 語法的 #472/#473 類，不需搬 Go cross-compile 進 PR）。da-portal build 前自動 `mkdir -p docs/assets/vendor`（offline-mode COPY 來源，runtime CDN fallback）。self-review 時 4 個本地逐一 build 驗過。**(L2)** `make pre-tag` 新增 `docker-build-all`（hard gate）+ `trivy-scan-all`（informational）—— 把 release-time 才做的 image build 提前到 pre-tag。**(L3)** `github-release-playbook.md` Step 2.5 加 release wrap-up agent discipline + trigger-asymmetry workflow audit 表。**設計取捨**：Trivy 在 L1/L2 皆 informational（非 #474 原文的 exit-code 1）—— 採 [#448](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/448) 論點（PR-time 阻擋 CVE 會讓新 upstream CVE 無預警卡不相關 PR）；build 仍 hard gate。解鎖 TRK-306 `vibe-release` skill。

- **季度 rule-corpus drift 稽核 — `audit_rules_drift.py`（epic [#570](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/570) / TRK-307）**：新增 `scripts/ops/audit_rules_drift.py` + `make audit-rules` + [`quarterly-audit-sop.md`](docs/internal/quarterly-audit-sop.md)。掃 dev-rules / pre-commit hooks / vibe skills / memory feedback 卡，產 drift report（`docs/internal/audit-reports/rules-drift-YYYY-MM.md`）：count reconciliation（YAML-parse hook 切分 vs CLAUDE.md 宣告）、hook↔dev-rule 覆蓋缺口、重複候選（difflib ≥0.60）、feedback orphan / broken-ref、stale 卡。MANUAL 季度工具（不入 CI；只產 report 不自動修改）。與 `consolidate-memory` 互補（後者只掃 `~/.claude` memory）。**首跑即抓出自埋誤差**：更正 [#582](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/582) `hook-vs-skill-coverage.md` 把 hook 切分誤記為「50 auto + 14 manual」並反指 CLAUDE.md drift——那是 grep `stages:\s*\[manual\]` 配到 `jsx-babel-check-strict-linecount` 註解行（該 hook 是 auto，註解明寫 NOT manual）；YAML parse 確認真值 **51 auto + 13 manual + 3 pre-push**，CLAUDE.md 一直正確。

- **`vibe-subagent-review` skill — IaC-aware blast-radius review（epic [#570](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/570) / TRK-305）**：新增第 4 個本地 skill（`.claude/skills/vibe-subagent-review/`）。借 superpowers `subagent-driven-development` 的兩階段 review pattern，但對 Vibe 過半的 IaC 工作（Helm values / `.gotmpl` / Prometheus rules / VRL transforms）改採**副檔名路由**：`.go`/`.py` 走 spec→quality；`values.yaml`/template 走 **blast-radius**（selector/RBAC/NetworkPolicy/ConfigMap 連動）；`.vrl` 走 **schema cascade**（下游 SIEM payload）；Prometheus rules 走 **cardinality+severity**（dedup/Sentinel/四層路由）。定位為 [#448](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/448) 機械 SAST 的**互補**——顧機械 lint 抓不到的跨檔語義 cascade。CLAUDE.md「Skill 體系」三→四 + `hook-vs-skill-coverage.md` §5/§7 同步（IaC cross-file cascade 漏接由本 skill 補語義層）。

- **Hook / Skill 邊界稽核矩陣（epic [#570](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/570) / TRK-304）**：新增 `docs/internal/hook-vs-skill-coverage.md` 盤點全部 67 個 pre-commit/push 品質閘門（實測 50 auto + 14 manual + 3 pre-push）+ 2 PreToolUse session-guard + 3 本地 skill + engineering:* 重疊的 **owner 分類**：🔧 hook-enforced（機械自動擋，AI 不必重做）/ 🧠 skill-advised（AI 須自覺）/ 👁️ reviewer-only（純人工，最易漏）。標出 overlap（trailer 規則 4 層 / sed -i 5 層）、conflict（由 TRK-301 優先級仲裁）、🕳️ 漏接（推銷語言 / 架構圖 drift / IaC cross-file cascade / SAST 1·3·7 — 無機械防線）。`CLAUDE.md` §Pre-commit 品質閘門 加一行 pointer。動機：[#515](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/515) / [#522](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/522) / [#543](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/543) trailer 連燒 3 次暴露 AI 不清楚 hook↔skill 職責邊界。

- **CLAUDE.md AI-context 強化（epic [#570](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/570) / TRK-301~303）**：三項 AI agent context 收斂 —（1）**TRK-301** 新增「Skill 優先級宣告」段，明訂 `vibe-*` 本地 skill 在衝突時 supersede 環境層 `engineering:*` / session-bootstrap generic skill；（2）**TRK-302** 新增「⛔ 高頻地雷（always-on）」段，把被燒過 ≥2 次的 5 條 feedback（繁中回應 / commit trailer block 格式 / worktree edit path / `git add` 括號 glob / commit 前觸發 `vibe-dev-rules`）從 lazy-load 升為 always-on；（3）**TRK-303** `dev-rules.md` §4 doc-as-code 補架構圖（`architecture-and-design.md` Mermaid / C4）為 schema 等級需同步項，並對應 adversarial self-review 第 6 lens。純 AI-context 文件，無 code / 行為變更。

- **CI workflow 升級 `actions/checkout@v4`→`@v6`（Node 20 deprecation 清理）**：GitHub 將於 2026-09 自 runner 移除 Node 20，`actions/checkout@v4` 跑在 Node 20、每次 job 噴 deprecation warning。把僅存的 4 個仍用 `@v4` 的 job（`docs-ci.yaml` I-4 Runbook Smoke Test、`planning-status-sync.yaml`、`secret-scan.yml`、`self-review-pass2.yaml`）對齊 repo 其餘 43 處早已採用的 `@v6`；兩處引用 `@v4` 版號的 `CRITICAL: ... fetch-depth: 0` 註解一併更新。純 CI 基礎設施版本對齊，無行為變更。`azure/setup-helm@v4` 維持不動（尚無更新的 major 版本）。

- **`pr_preflight.py`：軟性 CI check 不再卡死 push + 本地擋下壞掉的 Self-Review-Pass-2 trailer（hardens [#543](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/543)）**：兩個機制性修補,堵 #543 暴露的死結。**(1) `check_ci_status` 區分硬／軟失敗**：`continue-on-error: true` 的 workflow job(如 `Validate Self-Review-Pass-2 trailer`)失敗時 `gh pr checks` 仍回 `fail`,但它**不擋 merge**。原本 `check_ci_status` 把任何 `fail` 一律判 FAIL → preflight BLOCKED → 不寫 `.preflight-ok` marker → pre-push gate 擋住「修那個軟性檢查」的 push,而唯一逃生門 `GIT_PREFLIGHT_BYPASS` 又被 agent 安全層 hard-block —— 純化妝品的軟紅燈升級成死結。新增 `_soft_fail_check_names()`:掃 `.github/workflows/*.{yml,yaml}`、收集 `continue-on-error: true` 的 job 名(資料驅動,未來的軟性 workflow 自動納入、不靠 allowlist;workflow 解析失敗則該 check 保守歸 HARD)。CI 全綠或只剩軟性紅燈 → **WARN**;有硬性失敗才 FAIL,且 headline 計數只算硬性失敗。與既有 `check_pr_mergeable` FAIL→WARN 同一精神。**(2) commit-msg hook 擋下壞 trailer**：`check_commit_msg_file` 新增 `validate_pass2_trailer_placement()` —— 訊息若帶 `Self-Review-Pass-2:` 行,用 git 原生 `git interpret-trailers --parse` 驗它真的被認成 trailer。git 只把**連續的底部段落**當 trailer block:中間夾一個空行(#543)或一行非 `Key: value`(#515／#522)就會把上方的行整段甩出、`Self-Review-Pass-2` 不再是 trailer。CI gate `Validate Self-Review-Pass-2 trailer` 是 soft-fail、會靜默放行 —— 改在本地 commit-msg 階段 ERROR 擋下,壞 trailer 根本 push 不出去。Regression tests：`tests/dx/test_pr_preflight_checks.py::TestSoftFailCheckNames` + `TestCheckCIStatus`(soft／hard 分流)+ `tests/dx/test_preflight_msg_validator.py`(trailer placement)。

- **`pr_preflight.py` 的 PR-mergeable 衝突檢查 FAIL → WARN**：`check_pr_mergeable` 原本在 GitHub 回報 `mergeable=CONFLICTING` 時判 **FAIL**,連帶不寫 `.preflight-ok` marker、pre-push gate 擋下 push。但 GitHub 的 mergeable 是**已 push 的 PR head** 視角:衝突在本地已用 rebase / merge 解掉、但還沒 push 時,GitHub 仍回 CONFLICTING —— 形成「修衝突的 push 被『有衝突』擋住」的雞生蛋死結。改判 **WARN**:與同樣 pre-push-unresolvable 的 `BLOCKED`(待 review approval)一致;真正的本地衝突仍由 `check_conflict()` 的 merge dry-run 權威把關,`check_pr_mergeable` 純資訊性。Regression test：`tests/dx/test_pr_preflight_checks.py::TestCheckPRMergeable::test_conflicting_warns`。

- **`bump_docs.py` inline-version 規則跳過 CHANGELOG 已發佈段落**（PR [#503](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/503) 根因修正）：inline-version-text 規則（掃 doc 內文 `於 vX.Y.Z` 形式的行內版號）原會掃進 CHANGELOG.md 已發佈的 `## [vX.Y.Z]` 段落，把記錄「某檔在 v2.8.0 被刪除」這類**歷史事實版號**誤判為 drift、flip 成當前版號。新增 `skip_released_changelog` rule flag + `_split_at_released_changelog()` helper：第一個 `## [vX.Y.Z]` heading 以下視為凍結歷史、排除於掃描之外，`## [Unreleased]` 以上的 in-flight 內容仍照常處理；`--check` 與 `--what-if` 兩條路徑一致套用。PR #503 當時把 `於` 改 `在` 閃避 regex 的迂迴改字不再是必要 workaround。Regression test：`tests/dx/test_bump_docs.py::TestSkipReleasedChangelog`。

- **doc-map / tool-map 產生器版號改讀 SSOT**：`generate_doc_map.py` 與 `generate_tool_map.py` 原本把 frontmatter `version:` 字串硬編在 Python source 的 frontmatter list literal 內 — `bump_docs.py --check` 偵測不到（版號藏在 runtime 產生的字串裡、非 checked-in frontmatter 欄位），每次平台發版得手動補丁兩支檔案（v2.8.0→v2.8.1 release commit 即中招，PR [#503](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/503)）。新增共用 helper `scripts/tools/_lib_versions.py`，三支 dx 文件產生器（doc-map / tool-map / cheat-sheet）改從 CLAUDE.md `## 專案概覽` lead-in 行讀平台版號 — anchor 與 `bump_docs.py` 的 platform write rule 一致，`bump_docs.py --platform` 一跑即自動傳遞，不再需要手動補丁。順帶修正 `generate_cheat_sheet.py` 自 v2.6.0 CLAUDE.md 改版面後即失效、一路 fallback 的舊 regex。doc-map 重新產生零 diff；tool-map 的 Shared Libraries 段落自動新增 `_lib_versions.py` 一列。

---

## [v2.8.1] — secret-scan 四層防線 + Planning SSOT + DX 工具鏈收斂 (2026-05-16)

### Security

- **Secret-scan multi-layer doc-as-code sync — #445 完成**（issue [#445](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/445) AC iv / Chunk 5 of 5，收尾）：補齊 L0/L1/L2/L3 secret-scan 多層防線的文件層。`docs/internal/dev-rules.md` 新增 **§安全紀律（Secret Hygiene）** — 四層防線指引 + 三條規範（`git commit --no-verify` 嚴禁繞過 secret scan、leak 發生即進 SOP、`--no-verify` 違規記入 post-mortem）；dev-rules.md size cap 500→520（§安全紀律 為實質新內容，`--no-verify` ban 是無法 code-enforce 的純文字規則 — 走 `check_devrules_size.py` 既訂的「改門檻需 CHANGELOG + PR-body 理由」程序）。`CLAUDE.md` 架構速查段加一行 Secret-scan 四層防線速覽。`docs/internal/github-release-playbook.md` §版號驗證 加註 release-time L3 digest verification step（`release.yaml` 自動跑，#445 AC iii）。`secret-leak-remediation-sop.md` 的 forward-reference 收斂（`dev-rules.md §安全紀律` + `secret-scan.yml` 兩個 target 現都已存在）。**doc-map 修正**：AC iv body 原列「doc-map.md 新增 secret-scan.yml + SOP 條目」，但 `generate_doc_map.py` 只索引 `docs/`（`SKIP_DIRS` 含 `internal/`）、`.github/workflows/` 完全不索引 — 故兩者皆不入 doc-map，不強加錯誤條目。**#445 五個 AC 全數完成**：AC v Remediation SOP（PR #492+#493）／ AC iii release digest verification（#494）／ AC i L1 pre-commit hook（#495）／ AC ii L2 server-side workflow（#496）／ AC iv 本 PR。

- **L2 server-side secret-scan workflow (trufflehog → SARIF → Code Scanning)**（issue [#445](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/445) AC ii / Chunk 4 of 5）：新 `.github/workflows/secret-scan.yml` — L0/L1/L2/L3 多層的 **L2 不可繞 server-side gate**（L1 pre-commit hook 可被 `--no-verify` 繞過，L2 在 GitHub 基礎設施上跑、與 contributor 本地工具無關）。**兩種 scan mode**：`pull_request` → diff-only scan（`trufflehog git --since-commit <merge-base>`，目標 ≤ 1 min）；`schedule`（nightly 02:00 UTC）+ `workflow_dispatch` → full git-history scan。**Diff scan 起點是 merge-base 不是 `base.sha`**（Gemini review of #496 — Git Ancestry Trap）：`pull_request.base.sha` 是 base 分支當下的 tip，若 main 在 PR 分支切出後又前進，該 SHA 不在 PR 分支 ancestry 內，trufflehog `--since-commit` 無法 bound walk 會 fallback 掃整段 history（破壞 ≤1min + 翻出無關舊 finding）；改用 `git merge-base HEAD origin/<base>` 算真正 fork 點。**Verified/unverified policy** 由新工具 `scripts/tools/lint/trufflehog_to_sarif.py` 持有（trufflehog 無原生 SARIF 輸出 — 只有 JSON / GitHub-Actions annotation format，故自寫 NDJSON→SARIF 2.1.0 converter）：verified finding（credential 確認活的）→ SARIF `error` level + converter exit 1 → workflow fail → PR merge 被擋 / nightly email maintainer；unverified|unknown → SARIF `warning` + exit 0 → 進 Code Scanning 但不擋 PR。**Forked-PR 處理**：fork 來的 PR 拿 read-only token 無法寫 security-events — diff scan 仍跑，SARIF 改走 `actions/upload-artifact` fallback（nightly full-scan 以 main-repo write token 補進 Security tab）；**刻意不用 `pull_request_target`**（用高權限 token 跑 fork 程式碼 = RCE-to-secrets 自殺）。Workflow `permissions:` 顯式最小化（`contents: read` / `security-events: write` / `pull-requests: read`，不繼承 repo default）；`concurrency` group + `cancel-in-progress` 防 Ghost-Green race；trufflehog 經官方 `install.sh` 以顯式 release tag `v3.95.3` 安裝（round-2 self-review 從 Docker image 改來 — 避免猜 container-image tag 字串 `v3.95.3` vs `3.95.3` 的 fabricated-pin 風險）。`GIT_LFS_SKIP_SMUDGE=1`：trufflehog `git` mode 內部會 `git clone` repo，本 repo 用 Git LFS 存 visual-regression PNG，clone checkout 觸發 LFS smudge 在 LFS object 缺失時失敗整個掃描掛掉 — secret scan 不需 PNG 二進位內容，skip smudge 讓 clone checkout 出 pointer files（純文字）即可。SARIF upload 兩個 step 都 `if: always()` + `steps.convert.outcome != 'skipped'` guard — verified finding 讓 converter exit 1 後 SARIF 仍須先上傳才讓 job 失敗傳播，但 trufflehog step 自己失敗時不該再噴 confusing 的 "SARIF not found"。Converter 24 unit tests（NDJSON parse / Git+Filesystem location 萃取 / verified 分類含 fail-safe「只有 literal True 才 block」/ SARIF shape / exit-code policy via fixtures），詳 `tests/lint/test_trufflehog_to_sarif.py`。Python tool count 161。

- **L1 pre-commit secret-scan hook (trufflehog)**（issue [#445](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/445) AC i / Chunk 3 of 5）：新 pre-commit hook `secrets-scan-staged` 在 `.pre-commit-config.yaml` 註冊，呼叫 `scripts/tools/lint/check_secrets_staged.sh` 對**僅 staged files**（不掃全 repo）跑 `trufflehog filesystem --fail --no-verification`。Offline 模式（不打 trufflehog 自家 verifier API）求快，L2 server-side workflow（#445 AC ii / Chunk 4 未 landed）會做 verified-API 互補檢查。**False-positive 兩條 escape**：(a) `.trufflehogignore` 在 repo root，newline-separated regex paths（同 `.gitignore` 風格）；(b) inline `# trufflehog:ignore` 在那行尾（Python / YAML / shell 通用）。**`git commit --no-verify` 明確禁止**：失敗訊息直接 quote SOP rule #1（ASSUME COMPROMISE. ROTATE FIRST.）+ 點名 L2 仍會在 push 時擋下，--no-verify 只是把同個 leak 帶到本地 clone 多撐幾分鐘。**Trufflehog binary 缺失處理**：soft-skip + loud warning（**不** block commit）— 印出 Linux install.sh / macOS brew / Windows release-archive 3 種安裝 hint 後 `exit 0`（**不**列 `go install` — trufflehog 的 go.mod 含 replace directives，`go install` 會直接 fail；round-3 self-review 親測證實後改為明確標註此路不通）。理由：(a) 對齊 repo 既有 commit-msg hook 慣例「don't block commits on a missing validator」；(b) L1 本就是可被 `--no-verify` 繞過的 best-effort shift-left 層（issue #445 framing），真正不可繞的 gate 是 L2 server-side；(c) 硬擋一行 doc fix 在 binary 安裝上是糟糕 DX，只會逼人用 `--no-verify`（連帶跳過所有 hook）。warning 每次 commit 都印，吵到裝為止。**Exit-code 區分**（round-3 self-review）：trufflehog `--fail` 命中 secret 時 exit 恰為 183；其他 non-zero = 工具自身錯誤（bad path / crash）。Hook 分流兩種訊息 — 183 印 ROTATE-FIRST SOP，其他 non-zero 印「scan TOOL errored，非必然 leak，別跑 rotation SOP」，兩者都 fail-closed（block commit）。Hook entry 走 `language: system`（trufflehog binary 由 contributor 安裝），符合 Vibe `.pre-commit-config.yaml` 既有 `repo: local` only 慣例。`.trufflehogignore` 初始空檔（header comment 寫使用規則），patterns 由實際 false positive 增量加入。Pre-commit hook 計數 50 → 51 auto-run（CLAUDE.md 同步更新）。Performance target P95 ≤ 5s（staged files 規模下，offline 模式預期 sub-second）。

- **L3 supply-chain digest verification at release time**（issue [#445](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/445) AC iii / Chunk 2 of 5）：在 `.github/workflows/release.yaml` 4 個 release job（exporter / da-tools / portal / tenant-api）的 image push 步驟後加 `Verify image digest` step，透過共用 shell helper `scripts/ops/verify_release_digest.sh` 跑 `skopeo inspect` 取 digest，並寫進 GitHub Actions job summary 作審計軌跡。**Two-tag verify semantic**（round-3 self-review 修正）：**永遠** probe `:v${VERSION}`（catches what we just pushed — silent push fail 防護）；當 chart-yaml 給且 `Chart.yaml appVersion ≠ $VERSION` 時 **額外** probe `:v${appVersion}`（catches chart claim with no image）。對 tenant-api（chart 2.8.0 wraps appVersion 2.7.0 的合法 decoupling）會 probe 兩個 tag；exporter / portal 因 `appVersion == version` 只 probe 一個；da-tools 無 chart 也只 probe 一個。**Catches**：(a) 上游 `docker/build-push-action` silent push 失敗；(b) Chart.yaml appVersion claim 對應 image 不存在；(c) GHCR 短暫 outage。**順道補 release-portal 缺漏的 Chart.yaml-vs-tag version-check**。Auth 走 `skopeo login --password-stdin` + `--authfile`（不用 `--creds USER:PASS` 把 token 暴露在 `argv` / `/proc/<pid>/cmdline`）。`GITHUB_TOKEN` 在每個 verify step 顯式以 `env:` 傳入（**GitHub Actions 不會自動 export 給 `run:` script，是 round-3 self-review 抓到的 critical bug — 沒這個 release CI 永遠 fail at exit 3**）。defensive：strip 掉 Chart.yaml appVersion 可能的 leading `v` prefix。skopeo 用 `sudo apt-get install` 在 ubuntu-latest runner 上裝（idempotent）。Helper script 3 個 exit code（1=arg error / 2=image not found or skopeo failed / 3=env misconfig），exit 3 含 `GITHUB_TOKEN` 漏傳的 fix hint。詳：`scripts/ops/verify_release_digest.sh` 內 docstring。

- **Secret Leak Remediation SOP — Gemini external-review amendment**（issue [#445](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/445) AC v / post-merge follow-up）：post-merge 外部 review 帶回 4 個我自己多面向 review 漏掉的盲區，全採納：(1) **CRITICAL re-infection vector** — Step 4b 從舊 clone `git format-patch` 後直接 `git am` 進新 clone 會把含 leaked secret 的 patch 原封不動帶回，下次 push 重新污染歷史；改加強制 `grep` 檢查步驟 + 3 條 fallback（`git apply --reject` 手挑 hunk / 舊 clone `rebase -i` drop / 接受重做）；(2) **JWT mass-logout 副作用** — Step 2 JWT rotate 會觸發全網 Mass Logout + API-to-API 401 storm，rotate 前要 ping SRE / 客服避免被誤判系統崩潰而 rollback；(3) **Build artifacts poisoning** — Step 3 propagation table 新增 "自動化建置產物" row（leaked commit 觸發的 CI build 已把 secret 烤進 Docker image / 靜態 build cache，需 GHCR 標記為 vulnerable 或刪除 + 從乾淨 commit rebuild）；(4) **GitHub Support SLA expectation** — Step 4c 補非 Enterprise 客服 24-72 小時甚至數日的等待現實，強化「不能依賴 cache invalidation 來止血、Rotate-First 才是根本」。同步擴 反 SOP 表 3 條。Method-level note：上一 PR 的多面向 review 5 lenses 結構正確但**應用深度不足** — Gemini 的 4 點全屬「Operational realism」lens 應該覆蓋但實際走得太抽象、沒具體 walk-through 每個 step 在現實 incident 中可能怎麼 backfire。memory `feedback_adversarial_self_review_for_ops_docs.md` 更新加深-of-application 提醒。

- **Secret Leak Remediation SOP**（issue [#445](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/445) AC v — Chunk 1 of 5）：新增 `docs/internal/secret-leak-remediation-sop.md`，定義公開 repo secret leak 發生時 contributor 不等 approve 直接 rotate 的 5-step response（Identify → Rotate → Notify affected systems / customers → Clean git history → Post-mortem）。鐵律 ASSUME COMPROMISE. ROTATE FIRST. 列頂；明訂 `git push -f` / BFG / `git filter-repo` 對已 push 到 public 的 secret 是安慰劑（fork / reflog / GHArchive / BigQuery dataset / SaaS cache 都還在）。Provider rotate 入口 cheat sheet（AWS / GCP / Azure / GitHub PAT / Slack / Stripe / 內部 DB / 內部 JWT signing key / 內部 OAuth client）。GitHub Support cache invalidation 工單範本。Decision tree、Triage Ownership（@vencil first + backup contact 待 v2.8.1 closure 前指派；可存取客戶資料 level 並行 Step 3b GDPR/客戶合約通知）、反 SOP「已知會放大傷害」表（先 push 再 rotate、等 approve、`git push --force` 無 lease 等 7 條）。本檔屬 #445 Chunk 1（doc-only standalone，零 CI 風險，day-2 review 明標「the most important AC」）；後續 Chunk 2-5 將補齊 L1 pre-commit hook (AC i)、L2 server-side GHA workflow (AC ii)、L3 release.yaml digest verification (AC iii)、doc-as-code sync (AC iv)。

### DX

- **移除 phantom no-op lint `check-techdebt-drift`**：`check_techdebt_drift.py` 的資料來源 `docs/internal/known-regressions.md` 已在 v2.8.0 被 phantom-delete，該 lint 隨即 graceful 退化為永遠 exit 0 的 no-op（`parse_registry()` 對缺檔回空 dict → `main()` 印「nothing to check」return 0）；其職責由繼任者 `check_planning_status_sync.py`（issue [#379](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/379) chunk 2b，ADR-019 Layer 3 — 從 PR commit trailer 驗 planning entry status sync）接手。移除 `scripts/tools/lint/check_techdebt_drift.py` + `tests/lint/test_check_techdebt_drift.py` + `.pre-commit-config.yaml` 的 `check-techdebt-drift` pre-push hook，pre-push hook 計數 4 → 3（`CLAUDE.md` 同步）；`tool-map.md` / `.en.md` 重新產生，`dev-rules.md` §P1 與 `planning-id-mapping.md` §影響的 lint 引用更新。

- **`_lib_compat.try_utf8_stdout()` Phase B sweep — 77 tools migrated**（issue [#489](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/489) Phase B）：對 scripts/tools/ 下 77 個 emit emoji 但無 stdout encoding 設定的工具（CLI tier — `def main(...)` 入口、非 library module）以 ast-based sweep 加入 `from _lib_compat import try_utf8_stdout` + `try_utf8_stdout()` 為 main() 第一行。Script 處理三類 edge case：(1) 多行 `from X import (a, b, c)` block — 用 ast `end_lineno` 找閉括號位置，不破壞 import；(2) `def main(): """docstring"""` — 偵測 first body node 是否為 docstring，是的話 `try_utf8_stdout()` 注入到 docstring 之後（保留 docstring 性質）；(3) 既有 `_THIS_DIR = Path(__file__).resolve().parent`（5 個檔案）— 偵測到不 clobber，重用既有變數並 `str(_THIS_DIR)` wrap 讓 `sys.path.insert` 對 Path/str 都通；anchor 點放在既有 `_THIS_DIR =` 之後（避免 NameError）。1 個 library module（`_grar_render.py` — 無 `def main()`，被 `generate_alertmanager_routes.py` import）刻意跳過（library 不該動 stdout）。7241 個 pytest 全綠（3 個 pre-existing flakes 與本次無關，stash-verified）。本次後 `grep -rn '^[^#]*sys\.stdout *= *io' scripts/` 回空 — legacy module-level pattern 完全退役（Phase A 退 `pr_preflight.py` + Phase B 退 77 個從未設定的工具 = 全部）。Phase B 與 Phase A 同 issue 但因 scope 大、commit 巨拆成獨立 PR 易 review。Method-level note：兩輪 sweep script bug 都靠 pytest catch（不是 ast parsing — ast OK 但 runtime semantic 錯）— 提醒「parses cleanly」≠「runs correctly」，full test 必跑。

- **`pr_preflight.py` 採用 `_lib_compat.try_utf8_stdout()`**（issue [#489](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/489) Phase A）：移除 module-level `io.TextIOWrapper(sys.stdout.buffer)` 副作用-on-import 舊 pattern，改用 PR #432 引入的 `from _lib_compat import try_utf8_stdout` + `main()` 第一行呼叫。`pr_preflight` 在 Windows hosts 每個 commit 都跑、又會 print emoji，屬於 hot path。本次遷移後 `scripts/` 全 repo 已無 legacy module-level pattern（grep `^[^#]*sys\.stdout *= *io` 回空）。84 個既有 test 全過。其餘 ~79 個 emit emoji 但無 encoding 設定的工具歸 `_lib_compat.py` docstring 既訂政策「proactive on next touch」處理，#489 closed-as-Phase-A-only（scope refined：原估 ~20 個 batch 遷，實測 79 個 + 既有政策不鼓勵 retroactive sweep）。

- **`diag_pr_ci.py` — PR CI 自動排障 CLI**（issue [#446](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/446)）：新工具 `scripts/tools/dx/diag_pr_ci.py` + `make diag-pr ARGS="<PR-N>"` 把 PR 失敗 check 摘要成 markdown / JSON 報告。4-endpoint 串接（`/pulls/{n}` → head sha → `/commits/{sha}/check-runs` paginated → 每個失敗 Actions check 的 `/actions/runs/{id}/jobs` + 全部失敗 check 的 `/check-runs/{id}/annotations`）。**架構選擇** per #446 day-2 review pivot：用 `subprocess.run(["gh", "api", ...])` 取代 `requests`/`urllib`（gh 處理 auth / pagination / rate-limit / retry，contributor 不用管 PAT；scripts/ 內 10 隻打 Prometheus/K8s/OPA 的 requests-using script 無 github.com 既有 pattern 要對齊）。**Prerequisite probe**（3 個 distinct exit codes）：exit 2 = `gh` 缺失或未認證（install / `gh auth login`）、exit 3 = api.github.com 不通（Cowork VM proxy 常見症狀，切 Windows MCP / Dev Container）、exit 1 = 工具內部錯誤、exit 0 = 工具成功（無論 CI 是否紅）。**Output**：`--markdown`（預設，每 check 截 5 條 annotation 避免破 GitHub PR comment 65K 上限）+ `--json`（不截，給 machine consumer）。**Edge cases**：external-app check（CodeCov 等，無 `/actions/runs/<id>/` 在 details_url）跳過 jobs 但仍嘗試 annotations；mid-flow `/jobs` 失敗不 abort，graceful degrade 為 "no job breakdown available"；PR-not-found 改 clean 訊息（不噴 raw gh stderr）。採用現代 stdout 設定 pattern — `from _lib_compat import try_utf8_stdout` + `main()` 第一行呼叫，取代 module-level `io.TextIOWrapper(sys.stdout.buffer)` 副作用-on-import 舊 pattern（per `_lib_compat.py` docstring「Apply this helper proactively when next touching one of those tools」)。34 unit tests 用 side_effect router pattern 把 4 個 endpoint 各 mock 到 fixture file（`tests/dx/fixtures/diag_pr_ci/*.json`，可 `gh api ... > fixture.json` 重錄），涵蓋 gh_api wrapper（含 paginate=True 自動拉長 timeout 30s→90s + explicit timeout override + TimeoutExpired 收斂為 GhApiError rc=124）/ 3-step prereq probe（含 rate-limit `remaining < 10` 早退 + auth-status timeout）/ 4-call sequential ordering / `app.slug == "github-actions"` 確定性判斷取代純 URL regex 啟發式 / 各 formatter（含 whitespace-only annotation message 不 crash + 空 jobs 用 neutral 訊息）/ `--json` 不截斷 / mid-flow error graceful degrade / PR-not-found dispatch。詳：[windows-mcp-playbook §已知陷阱速查 #64](docs/internal/windows-mcp-playbook.md#已知陷阱速查)。

- **Self-Review-Pass-2 trailer CI 軟性閘門**（issue [#454](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/454)）：新 `scripts/tools/dx/pr_preflight.py --check-pass2-trailer-strict` flag 用 git native trailer parser（`--format=%(trailers:key=Self-Review-Pass-2,valueonly=true,unfold=true)`，**不**用 regex — 自動處理 case-insensitive / multi-line folded / 「trailer 必須在 bottom paragraph 前空行」git 規則）掃 `<base>..HEAD` 範圍內所有 commit，任一 commit 含 trailer 即 PASS。Empty range 走 SKIP（exit 0）避免 false-fail on HEAD-on-base / behind-base 情境。新 workflow `.github/workflows/self-review-pass2.yaml` 走 `continue-on-error: true` 軟性失敗（adoption 穩定後切硬性），`actions/checkout@v4 fetch-depth: 0` 同 `planning-status-sync.yaml` trap pattern。PR Template 把 trailer block 從 conditional "Optional" 升格為 expected default 區段。Coordination：本 issue 是 v2.9.0 [#453](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/453) mutmut 的 enabling step — mutmut surviving mutation 出來時可倒推 `git log --format=%(trailers:...)` 找對應 PR 是否 claim 過 pass-2，識別 docstring vs code 偏離 pattern。Day-2 review 校正了 Gemini 原 proposal 的 "squash → check PR body" 前提（repo 的 `squash_merge_commit_message: COMMIT_MESSAGES` 拼接 commit msgs 不用 PR body，所以掃 git log 才對）。8 unit tests cover trailer-present / no-trailer + amend hint / empty-range SKIP / rev-list-fail with fetch-depth hint / log-fail / custom base-ref threading / git-not-on-PATH uniform error / CLI dispatch（`tests/dx/test_pr_preflight_orchestrator.py::TestCheckPass2TrailerStrict`）。

- **Windows MS Store Python stub 防呆**（issue [#436](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/436)）：Windows 11 fresh install 在 `%LOCALAPPDATA%\Microsoft\WindowsApps\` 放的 `python3.exe` App Execution Alias placeholder 通過 `command -v` 但執行回 exit 49（`Python was not found; run without arguments to install from the Microsoft Store`），讓 `scripts/hooks/commit-msg` 每次 commit 被擋 + `scripts/tools/dx/pr_preflight.py::check_scope_drift()` 永遠回報 `❌ Scope drift`。修法兩面：(i) commit-msg shell candidate list 改 `"py -3" "py" python3 python ...` 並加 `--version` probe（stub 失敗即跳下一個 candidate）；(ii) `pr_preflight.py` + `scripts/tools/lint/check_pr_scope_drift.py` 的 subprocess invocation 用 `sys.executable` 取代 bare `"python3"`，由當前 interpreter self-fork 繞過 `CreateProcess` PATH lookup。Regression test 鎖 `cmd[0] == sys.executable`（`tests/lint/test_check_pr_scope_drift.py::TestCheckToolMap::test_uses_sys_executable_not_bare_python3` + `tests/dx/test_pr_preflight_checks.py::TestCheckScopeDrift::test_uses_sys_executable_not_bare_python3`）。Trap codify 至 [windows-mcp-playbook §已知陷阱速查 #63](docs/internal/windows-mcp-playbook.md#已知陷阱速查)。Dev Container / WSL / Linux 不受影響（無 MS Store stub）。

### 文件治理

- **ADR frontmatter migration — #379 chunk 3 wrap-up**：ADR-001 ~ ADR-018（19 個檔案）加 ADR-019 frontmatter spec 欄位（`id` / `tracking_kind: adr` / `status: accepted` / `domain: <subsystem>` / `created_at` 從 `git log --diff-filter=A` 取首 commit 日期 / `updated_at: 2026-05-13`）。Status 從各 ADR 既有 `## 狀態` H2 區塊解析（emoji + bold name），mapping `Accepted/Extended → accepted`、`Proposed → proposed`、`Rejected → abandoned`、`Superseded → superseded`。Domain 來自 hand-authored mapping table（per-ADR judgment）。ADR-019 自身 self-bootstrap（之前是 SSOT spec 但沒套用自身規則）。`docs/internal/planning-index.md` 從 19 → 39 entries（accepted 20 / in-progress 2 / proposed 16 / done 1）。**Note**：`adr-index` 渲染不變，因 chunk 2a `generate_adr_index.py` 用 `## 狀態` 區塊解析、不依賴 frontmatter。`frontend-quality-backlog.md` 為 meta-policy 文件（無 entry）skip migration；`v2.8.0-planning*.md` + `known-regressions.md` phantom-deleted skip。
- **Backlog frontmatter migration — dx-tooling-backlog**（issue [#379](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/379) chunk 3）：18 個 `### TRK-001` ~ `TRK-018` entries 加 ADR-019 frontmatter spec yaml 區塊（`id` / `tracking_kind: dx` / `status` / `domain` / `created_at` / `updated_at`）。Default `status: proposed`，TRK-006 / -010 / -011 個別覆蓋為 `in-progress` / `done` 反映實際進度。`docs/internal/planning-index.md` 從 1 entry (ADR-020) 擴張為 19 entries (`in-progress 2 / done 1 / proposed 16`)。順帶修 chunk 2a `SECTION_YAML_RE` 的 multi-line heading bug：`(?P<heading>.+?)` with `re.DOTALL` 能跨多 H2/H3 backtrack 撈出整段 prose 當 title；改 `(?P<heading>[^\n]+?)` 限制單行。新增 regression test `test_heading_does_not_span_multiple_sections` pin 此 contract。Follow-up：其餘 backlog files（`frontend-quality-backlog.md` 等）migration 待後續 PR 處理。
- **Legacy ID code-side sweep**（issue [#379](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/379) chunk 1b code）：~150 個非-md 檔案（57 ts / 31 py / 24 jsx / 22 js / 13 go / 4 yaml / 3 tsx / 2 yml / 1 Makefile）內 328 處 `TD-NNN` / `TECH-DEBT-NNN` / `HA-NN` / `REG-NNN` 機械替換為對應 `TRK-NNN` alias（per `planning-id-mapping.md` §編號分區算法）。`check_skip_a11y_justification.py` 的 `RE_JUSTIFICATION` regex 同時擴張為 `(?:TRK-\d+[a-z]?|TD-\d+)`，過渡期兩種形式都接受。Exclude 6 個 lint pattern definition + test fixture 檔（`check_codename_leak.py` / `check_techdebt_drift.py` / `check_skip_a11y_justification.py` source + 3 個對應 test fixtures），這些刻意保留 legacy ID 用以驗 lint 自身。Post-sweep：2320 pytest 全通過（1 個 pre-existing platform-dependent failure 不關此 sweep）+ codename-leak 0 leaks + doc-links valid + tool-map.md regenerated。
- **Planning status sync CI gate**（issue [#379](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/379) chunk 2b）：新工具 `scripts/tools/lint/check_planning_status_sync.py` 實作 ADR-019 Layer 3 — 從 PR commit 範圍 (`<base>..HEAD`) 用 git native trailer parser (`git log --format='%(trailers:key=Resolves,valueonly=true,unfold=true)'`) 抽取 `Resolves|Closes|Fixes` trailer，對每個 in-scope ID（`TRK-NNN` / `ADR-NNN` / `S#NNN`，legacy `TD-`/`HA-`/`REG-` 不解析）驗：(1) repo 內有對應 planning entry、(2) `status: done`、(3) `pr_ref:` 對齊 current PR number。29 unit tests cover trailer parser（含 RFC-2822 blank-line semantics / lowercase verb / sub-PR letter suffix `TRK-230c` / legacy ID 不 match）+ validate_sync + ID regex 邊界。Default soft-warn（exit 0 + annotations，per ADR-019 Layer 3「黃燈」），`--strict` 升為 hard-fail。CI workflow `.github/workflows/planning-status-sync.yaml` 自動跑（`actions/checkout@v4 fetch-depth: 0` 關鍵 — 否則 `<base>..HEAD` 解不出）。Reuses chunk 2a `generate_planning_index.discover_all()` 的 4-source discovery。Python tool count 157 → 159（吸收 pre-existing +1 drift + 本 PR +1）。
- **Planning index 自動化**（issue [#379](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/379) chunk 2a）：新工具 `scripts/dx/generate_planning_index.py` 實作 ADR-019 Layer 2 — 從 4 個 source（`docs/**/*.md` top-of-file frontmatter / 嵌入式 yaml block / `flaky-tests.yaml` / code-comment `// TECH-DEBT(id=...)` 註解）發現帶 `tracking_kind:` 的 planning entry，分組（按 status × tracking_kind）渲染到 `docs/internal/planning-index.md` 哨點區塊。每個 entry 連結回 source path + line number。pre-commit drift gate `planning-index-check` + `make planning-index` 本地刷新。Top-level pre-commit hook 計數 49 → 50 auto-stage。
- **Migration Guide hub slim**（issue [#378](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/378) II-1）：`docs/migration-guide.md` 從 808 行瘦身至 357 行 (-56%)，雙語同步。新增 §5-Step 高階流程 作為中心導航（從 toolkit 安裝到 cutover 收斂的 5 步表格 + 各步驟 anchor 連到對應章節 / spoke）；§0-§13 各章節保留 anchor ID（byte-for-byte，舊書籤不失效），body 收斂為 2-4 句摘要 + 連到 `cli-reference.md` / `migration-engine.md` / `shadow-monitoring-sop.md` / `scenarios/incremental-migration-playbook.md` / `scenarios/multi-system-migration-playbook.md` 等既有 spoke。§7 維度標籤（無 clean spoke）以壓縮 inline 形式保留。`### Q:` FAQ 全保留。
- **ADR 索引自動化**（issue [#378](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/378) II-2）：新工具 `scripts/dx/generate_adr_index.py` 從 `docs/adr/` frontmatter + `## 狀態` 區塊自動渲染表至 `docs/architecture-and-design.md` 的 `<!-- ADR_INDEX_START/END -->` 哨點之間。新增 ADR 漏接 hub 索引的問題（ADR-018/019/020 都曾如此）由 pre-commit drift gate `adr-index-check` 機械擋下；本地用 `make adr-index` 重新渲染。Top-level pre-commit hook 計數 48 → 49 auto-stage。

---

## [v2.8.0] — 客戶導入管線 + 千租戶 Scale 驗證 + 自動化收斂 (2026-05-12)

v2.8.0 把 v2.7.0 的 Scale Foundation I（`conf.d/` 階層 + dual-hash 熱重載）推進為**可導入既有 kube-prometheus 客戶**的完整 pipeline：4 條 Go binary 把客戶 PromRule corpus 自動化轉成 Profile-as-Directory-Default `conf.d/` 樹，三條交付路徑（Docker / static binary 6-arch / air-gapped tar）+ supply-chain provenance（cosign keyless + SBOM）滿足全光譜部署環境。Tenant Manager 在 1000+ 租戶規模上以 server-side search + virtualization 維持 p99 < 200 ms。56 個 pre-commit hook（39 auto + 14 manual + 3 pre-push）把開發規範從 reviewer convention 升級為 mechanical net。

### Highlights — 5 條

- **客戶導入自動化** — 4 隻新 Go CLI（`da-parser` / `da-tools profile build` / `da-batchpr` / `da-guard`）把 kube-prometheus 客戶現有 `PrometheusRule` corpus 導入到 `conf.d/` Profile-as-Directory 架構；1000 租戶導入從一週縮到一天。安裝見 [Migration Toolkit Installation](docs/migration-toolkit-installation.md)。
- **Tenant Manager 邁入 1000+ 租戶規模** — UI 直打 `/api/v1/tenants/search`（伺服端 search / pagination，page_size 預設 50 / 上限 500）取 live data；Saved Views frontend 接 v2.5.0 已存在的 backend CRUD；TenantCard 加 Alert Builder / Routing Trace deep link。
- **Defaults 變動可預演** — `/api/v1/tenants/simulate` ephemeral primitive 讓 CI 與 UI 在 commit 前預測 `_defaults.yaml` 變動對 inheritance 的影響（無 disk IO、無 manager state mutation）；新 `da_config_blast_radius_tenants_affected` histogram 量化每 tick 受影響 tenant 分佈。
- **Q2 2026 CVE 集中收斂** — Grafana / Prometheus 3.x / Alertmanager / oauth2-proxy / alpine-git / Python base / Go toolchain 一次 bump，清掉 50+ CVE（13 個 CRITICAL、35+ HIGH），涵蓋 auth bypass、RCE、memory safety、TLS DoS。詳見 [#100](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/100)。
- **API 邊界硬化** — `tenant-api` body-content range validation 在邊界 fail-fast（patch key/value 超長、`_timeout_ms` 超過 1 小時、`_silent_mode` 非 enum 都直接 400 + 完整 violations array）；flat + nested mixed-mode 同 tenant id 重複從 WARN 升級為 hard error。

### 客戶導入管線（5-step chain，新功能）

- **`da-parser`**：PromRule YAML → canonical JSON。dialect 偵測（`prom` / `metricsql` / `ambiguous`）+ VM-only function allowlist（`vm_only_functions.yaml` 走 `go:embed`，CI freshness gate 偵測新版 metricsql 上游函數）+ `StrictPromQLValidator` + provenance header。`prom_portable: bool` 旗標讓客戶遷入 VM 後仍能識別「可回 Prom」的子集 — anti-vendor-lock-in 具體承諾
- **`da-tools profile build`**：cluster 相似 rules → median 演算法決定 cluster 共通閾值 → 寫 `_defaults.yaml`、偏離 tenant 寫 `<id>.yaml` 只含 override；fuzzy matching opt-in 套 duration-equivalence canonicalisation（`[5m]` ≡ `[300s]` ≡ `[300000ms]`）；遵循 [ADR-018](docs/adr/018-profile-as-directory-default.md) Profile-as-Directory-Default
- **`da-batchpr apply` + `refresh`**：Hierarchy-Aware 分塊 — `_defaults.yaml` 變更打 Base Infrastructure PR、tenant PRs 標 `Blocked by:`；`refresh --base-merged` 在 Base merge 後自動 rebase tenant PRs；`refresh --source-rule-ids` 對 parser bug fix 細粒度重生 patch PR
- **`da-guard`**：Schema / Routing / Cardinality / Redundant-override 四層檢查；`.github/workflows/guard-defaults-impact.yml` 自動跑 + sticky PR comment（marker-based update vs create）+ artifact 14d retention
- **Migration Toolkit 三條交付路徑**：(a) Docker pull `ghcr.io/vencil/da-tools`；(b) Static binary linux/darwin/windows × amd64/arm64 共 18 個 archive；(c) Air-gapped tar（`docker save` export）。每條路徑 cosign keyless 簽 + SBOM SPDX/CycloneDX；客戶 `make verify-release` 一鍵驗

### Scale Foundation III — 千租戶生產驗證

- **1000-tenant baseline land**：`make benchmark-report` 17 benches × count=6 跑 nightly cron；mixed-mode flat+hierarchy benches 加入 trend tracking。Cold load 112 ms / steady-state reload 1.3 ms @ 1000 tenants
- **`/api/v1/tenants/simulate` + Ephemeral Graph**：tenant.yaml dry-run preview（不污染 watch loop）；CI gate `TestSimulate_VsResolve_ParityHash` 鎖死「simulate=commit-後 preview」契約
- **5-anchor end-to-end alert fire-through harness**（`tests/e2e-bench/`）：從 `conf.d/` 寫入 → exporter reload → Prometheus alert trigger → Alertmanager dispatch → webhook receiver 的完整鏈；n=30 + bootstrap 95% CI；docker-compose 6-service stack
- **Bench-gate 兩層 CI 治理**（[#433](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/433)）：Tier 1 per-PR single-runner sequential（同 VM 同 CPU by construction → INCONCLUSIVE ~0%）；Tier 2 release-time cumulative drift report（informational）；`override: bench-regress-ok` label 走獨立 `bench-override-audit.yaml` workflow event-scoped 驗證

### Customer-Facing Surface

- **Server-side Search API** `GET /api/v1/tenants/search`：page_size cap 500 + closed-field free-text + RBAC-before-pagination + 30s TTL `tenantSnapshotCache`，p99 < 200 ms @ 1000T
- **Tenant Manager JSX**：API-first 3-layer priority chain（API → platform-data.json → DEMO）+ 429 retry-with-backoff + URL state（`useURLState` + `useDebouncedValue`）+ self-written `useVirtualGrid`（>50 才 virtualize）
- **Master Onboarding Dual Entry**：Import Journey 5 步（parser → profile build → batch-pr → guard inline CLI）vs Wizard Journey 5 步（cicd-setup → deployment → alert-builder → routing-trace → tenant-manager — 全 5/5 真 wizards）
- **TenantCard × Wizard 整合**：footer 三鈕（Alert / Route / Preview）deep link + `?tenant_id=` URL 參數預填 + 獨立 `simulate-preview.jsx` widget（4-state machine + 500ms debounce + AbortController）
- **Smart Views**：`useSavedViews` + `SavedViewsPanel` 接 v2.5.0 backend `/api/v1/views` CRUD；RBAC-aware（Save/Delete hidden when `canWrite=false`）

### 測試與 CI 基礎設施

- **`ConfigManager` test-only setter 注入**：`m.SetMetrics` / `m.SetLogger` / `m.SetClock` 取代 v2.7.x 三類 global-swap（`withIsolatedMetrics` / `log.SetOutput` / WatchLoop `time.Sleep`）；過去因 global state race 必須 serial 的測試現在 `t.Parallel`-eligible，full app pkg `-count=5 -race` 12.1s（previously 20.7s）。AI agent quickref → [`test-map.md` §測試注入 Seam](docs/internal/test-map.md)
- **Policy-as-Code lint**：`check_hardcode_tenant.py`（dev-rules #2 PromQL label selector）/ `check_subprocess_timeout.py`（FATAL）/ `check_jsx_loader_compat.py` / `check_playwright_rtl_drift.py` / `check_codename_leak.py`（[#462](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/462) / [#468](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/468)）等。56 hooks 共 39 auto + 14 manual + 3 pre-push
- **Tenant API hardening**：rate limit per-pod + `X-Request-ID` echo + tenant-scoped authz（4 endpoints）+ body-content range validation（go-playground/validator + struct tags + reservedKeyValidators registry）
- **`bump_docs.py` 跨四條 release line 機械 bump**：91+ 文件 frontmatter / helm Chart / Dockerfile / k8s cronjob image tag 一次同步（[#439](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/439)）

### 文件治理

- **Customer-facing docs codename leak sweep**（[#462](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/462) / [#468](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/468)）：22 customer-facing 文件、~213 處 codename → feature-name 替換；`check_codename_leak.py` PATTERNS 加 `DEC-X` / `v\d+\.\d+\.\d+-(final\|rc\d*\|alpha\|beta\|preview\d*)`；default scope 從 T0 + component README 擴到 T1（`docs/**` 排除 `internal/`）；新 `PER_FILE_ALLOWLIST` 機制收 ADR-019。Layer 2 self-healing glossary gate → [#469](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/469)
- **Planning ID namespace 統一為 TRK**（[#379](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/379) chunk 1）：依 [ADR-019 Option C refined hybrid](docs/adr/019-planning-ssot.md) 把 `TECH-DEBT-NNN` / `TD-NN` / `HA-NN` / `REG-NNN` 四個分散 namespace 統一為 `TRK-NNN`；新增 [`planning-id-mapping.md`](docs/internal/planning-id-mapping.md) 含完整對映表 + 三段分區編號政策
- **`docs/benchmarks.md` customer-first 重寫**（[#460](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/460)）：488/536 → 209/209 行（−57% / −61%），TL;DR 5 個數字 + v2.2.0 → v2.8.0 evolution + 1000-tenant Scale Gate + e2e fire-through + soak + sizing 七段；toolchain micro-bench detail 移到 [`benchmark-playbook.md §Engineering Reference Benchmarks`](docs/internal/benchmark-playbook.md)
- **ZH-primary SSOT policy lock**：v2.5.0 評估文 §7 原推薦切換 EN SSOT，pilot 工具於 `v2.7.0` 完成；v2.8.0 經內部維運手冊的 4-question audit（含「spec premise validation」一條）後 reverse 原計畫。Pilot 工具保留 dormant，trigger conditions 明確 codify

### Benchmark（1000 tenants, Dev Container, Intel Core 7 240H, Go 1.26.2 linux/amd64, `-benchtime=3s -count=6`）

| 指標 | 時間 | 語義 |
|:---|---:|:---|
| `FullDirLoad_1000` | 112 ms | Cold load（scan + YAML parse + merge + hash） |
| `IncrementalLoad_1000_NoChange` | 1.3 ms | Dual-hash steady-state reload（86x 快於 cold） |
| `Simulate_DeepChain` | 5 ms | `/simulate` endpoint per-call |
| 5-anchor e2e fire-through P99 | **4.98 s** | conf.d 寫入 → webhook receiver 全鏈（受 Prometheus 5 s scrape quantization 主導，near-flat 1000→5000 tenants；n=30, bootstrap 95% CI） |

SLO 維持 v2.7.0：cold load 112 ms / 1000 tenants；reload 熱路徑 1.3 ms 相對於預設 15 s scan_interval 僅 0.0087%，幾乎零 overhead。完整報告見 [`benchmarks.md`](docs/benchmarks.md)。

### ADR 新增（ADR-018 / 019 / 020，3 條）

- **ADR-018** Profile-as-Directory-Default：`_defaults.yaml` 為 cluster 共通閾值的權威位置
- **ADR-019** Planning SSOT：`TRK-NNN` namespace 統一政策（Option C refined hybrid）
- **ADR-020** Tenant Federation outline（v2.9.0+ design seed）

### Breaking changes

- **Mixed-mode duplicate tenant id**：flat + nested 同 tenant id 重複從 WARN 升級為 typed `*DuplicateTenantError` hard error。同 tenant id 出現在兩個位置會直接拒絕 load（state preservation invariant 保證舊 state 不被部分覆寫）
- **`tenant-api` body validation**：超出 range 的 patch 從 silently accept 變成 400 + 完整 violations array（`_timeout_ms` > 1 小時 / `_silent_mode` 非 enum / key|value 超長 等）。客戶 CI 若有 silently-tolerated bad payload 會在升 v2.8.0 後立刻 fail
- **`Open-mode` PUT/DELETE Groups**：tenant-scoped authz 補完後，open-mode 環境（缺 `_rbac.yaml`）下 PUT/DELETE Groups 從可寫變成 403。修補：補 5 行 `_rbac.yaml` `groups: [{name: dev, tenants: ["*"], permissions: [admin]}]`

### Upgrade notes

- 既有客戶：升 v2.8.0 後 mixed-mode duplicate tenant id 不再 silently tolerated — 升前先掃 `da-tools validate-config --strict` 確認沒有重複
- 開啟 Customer Migration Pipeline：見 [Migration Toolkit Installation](docs/migration-toolkit-installation.md)，三條交付路徑任選；客戶 `make verify-release` 驗 cosign + SBOM
- Tenant API hardening：升前讀 [`tenant-api-hardening.md`](docs/api/tenant-api-hardening.md) §3 affected endpoints + §5 known gaps（含 open-mode 升級 checklist）

---
## [v2.7.0] — 千租戶配置架構 + 元件健壯化 (2026-04-19)

v2.7.0 把租戶配置的資料結構升級為可支撐千租戶規模（`conf.d/` 階層 + `_defaults.yaml` 繼承引擎 + dual-hash 熱重載），把 v2.6.0 的 Design Token 定義推進到全面採用，並把測試與 CI 從「能跑」升級為「可規模化」。

### Scale Foundation I — 千租戶配置架構（ADR-016 / ADR-017）

- **`conf.d/<domain>/<region>/<env>/` 階層目錄**：任一層可放 `_defaults.yaml`，`L0 defaults -> L1 domain -> L2 region -> L3 tenant` 四層 deep merge，array replace / null-as-delete 語義明確
- **Dual-hash 熱重載**：`source_hash`（原始檔 SHA-256）+ `merged_hash`（canonical JSON SHA-256）並行追蹤，merged_hash 變才 reload；300ms debounce 吸收 K8s ConfigMap symlink rotation 的連續寫入
- **Mixed-mode**：舊扁平 `tenants/*.yaml` 與新 `conf.d/` 可共存，無強制一次遷移
- **`GET /api/v1/tenants/{id}/effective`**：回傳 merged config + 繼承鏈 + dual hashes，方便 debug 實際生效設定
- **新 CLI**：`da-tools describe-tenant`（含 `--what-if <file>` 模擬 `_defaults.yaml` 變動 -> diff merged_hash）+ `da-tools migrate-conf-d`（扁平 -> 階層自動 `git mv`，預設 `--dry-run`）
- **Schema 新增**：`tenant-config.schema.json` 加入 `definitions/defaultsConfig` + `_metadata.$comment`

### 元件健壯化

- **Design Token 全面遷移**：9 個 Tier 1 JSX 工具完成 Tailwind -> arbitrary value token 改寫（`wizard` / `deployment-wizard` / `alert-timeline` / `dependency-graph` / `config-lint` / `rbac` / `cicd-setup-wizard` / `tenant-manager` / `multi-tenant-comparison`）；剩餘 7 個 px-only 工具延 v2.8.0
- **`[data-theme]` 單軌 dark mode**（ADR-015）：移除 Tailwind `dark:` 雙軌橋接，解決 v2.6.0 誤用陷阱
- **Component Health Snapshot**（ADR-013）：`scan_component_health.py` 五維評分（LOC / Audience / Phase / Writer / Recency）-> Tier 1 = 11 / Tier 2 = 25 / Tier 3 = 3；新增 `token_density` 量化 token 採用進度
- **Colorblind 合規**（ADR-012）：`threshold-heatmap` 結構化 severity（不只靠顏色）
- **TECH-DEBT 類別獨立 budget**：從 REG budget 分出，不佔 REG P2/P3 配額
- **新 lint**：`check_aria_references.py` / `axe_lite_static.py` / `check_design_token_usage.py`

### 測試與基礎設施

- **`tests/` 子目錄分層**：`dx/` / `ops/` / `lint/` / `shared/`，匹配 `scripts/tools/` 的分層
- **1000-tenant synthetic fixture**：`generate_synthetic_tenants.py` 產可重現的千租戶資料，供 B-1 Scale Gate 量測
- **Blast Radius CI bot**：PR 變更自動計算影響的 tenants / rules / thresholds，comment 到 PR
- **Pre-commit**：31 auto + 13 manual-stage；`make pre-tag` 整合 `version-check` + `lint-docs` + `playbook-freshness-ll`

### Benchmark（1000 tenants, Intel Core 7 240H, Go 1.26.1, `-benchtime=3s -count=3`）

| 指標 | 時間 | 語義 |
|:---|---:|:---|
| `FullDirLoad_1000` | 112 ms | Cold load（scan + YAML parse + merge + hash） |
| `IncrementalLoad_1000_NoChange` | 2.45 ms | Dual-hash reload noop（45x 快於 cold） |
| `IncrementalLoad + MtimeGuard` | 1.30 ms | 加 mtime 短路（86x 快於 cold） |
| `MergePartialConfigs_1000` | 653 us | 階層 merge 本身 |

SLO：cold load 112 ms / 1000 tenants；reload 熱路徑 1.30 ms 相對於預設 15 s scan_interval 僅 0.0087%，幾乎零 overhead。完整報告見 [`benchmarks.md §3 v2.8.0 Scale Gate`](docs/benchmarks.md#3-v280-scale-gate-1000-tenant-實測)。

### ADR 新增（ADR-012~017，6 條）

colorblind 結構化 severity / component health + token_density / TECH-DEBT 獨立 budget / token 遷移策略 / 單軌 dark mode / `conf.d/` 階層 / `_defaults.yaml` 繼承引擎 + dual-hash 熱重載。

### Breaking changes

無。`conf.d/` 與繼承引擎為**新增能力**；舊扁平 `tenants/*.yaml` 完全向後相容，Schema 只新增不改動。

### Upgrade notes

- 既有使用者：不需變更
- 想採用 `conf.d/` 分層：見 `docs/scenarios/multi-domain-conf-layout.md` + `incremental-migration-playbook.md`，或 `da-tools migrate-conf-d --dry-run`
- 熱重載：dual-hash 預設啟用，debounce window 300ms 可用 `--scan-debounce` 調整

---

## [v2.6.0] — Operator 遷移路徑 × PR Write-back × 設計系統統一 (2026-04-07)

v2.6.0 的核心是「讓 enterprise 客戶能信賴地在 Operator 環境下運營」：建立完整的 ConfigMap → Operator 遷移工具鏈與對稱文件（ADR-008 addendum），引入 PR-based 非同步寫入支援 GitHub 與 GitLab 雙平台（ADR-011），統一設計系統消除三套平行 CSS 的技術債，並新增 4 個互動工具強化價值傳達。

### K8s Operator 完整遷移路徑

v2.3.0 引入的 Operator 指南是單一文件；v2.6.0 將其擴展為與 ConfigMap 路徑完全對稱的文件體系與工具鏈。

- **ADR-008 addendum**：正式記錄架構邊界宣言——threshold-exporter 不 watch 任何 CRD，CRD → conf.d/ 轉換由外部控制器或 CI 負責。含 Mermaid 邊界圖 + 三問判斷標準（ZH + EN 雙語）
- **`operator-generate` 大幅增強**：AlertmanagerConfig 6 種 receiver 模板（Slack, PagerDuty, Email, Teams, OpsGenie, Webhook），每種自動產出 `secretKeyRef` 引用 K8s Secret（零明文 credential）。新增 `--receiver-template`、`--secret-name`、`--secret-key` 參數
- **三態抑制規則 CRD 化**：Silent / Maintenance mode 自動包含在每個 AlertmanagerConfig 產出（4 條 inhibit rules）
- **Helm `rules.mode` toggle**：threshold-exporter chart 新增 `configmap | operator` 切換 + ServiceMonitor 條件模板，operator section 含 ruleLabels、serviceMonitor、receiverTemplate、secretRef
- **`da-tools migrate-to-operator`**（新增 CLI）：讀取現有 ConfigMap rules → 產出等效 CRD + 6 階段遷移清單（Discovery → Generate → Shadow → Compare → Switch → Cleanup）+ rollback 程序。`validate_tenant_name()` RFC 1123 驗證確保 CRD apply 不失敗
- **Operator Setup Wizard**（新增 JSX）：互動式偵測環境 → 選 CRD 類型 → 產出命令，每步驟含 contextual help + 常見陷阱提示
- **Kustomization.yaml 自動產生**：`operator-generate --kustomize` 產出標準格式，含 commonLabels + sorted resources + namespace
- **`drift_detect.py` Operator 模式**：`--mode operator` 透過 kubectl 取得 PrometheusRule CRD 的 spec.groups SHA-256，與本地 YAML 比對。kubectl timeout 30s + 三種錯誤處理
- **Decision Matrix**：提升到 Getting Started 層級，決策樹 + 10 維度比較表（ZH + EN）
- **文件對稱化**：`prometheus-operator-integration.md` 拆分為 4 組子文件（Prometheus / Alertmanager / GitOps / Shadow Monitoring）各含 ZH + EN 版本 + 2 hub 導航頁 = 10 篇新文件

### PR-based Write-back + 非同步 API

v2.5.0 的 tenant-api 只支援 direct write（API → YAML → git commit）。v2.6.0 新增 PR 模式與非同步批量操作，讓高安全環境能透過 code review 流程管理配置變更。

- **ADR-011**（新增 ADR，ZH + EN 雙語）：定調 PR lifecycle state model（pending / merged / conflicted）、GitHub PAT 權限與 Secret 管理策略、多 PR 合併衝突處理、eventual consistency 語義
- **PR-based write-back**：`_write_mode: direct | pr` 配置切換（`-write-mode` flag + `TA_WRITE_MODE` env）。UI 操作 → 建立 PR → reviewer 核准 → 合併。PR-mode API response 回傳 `pr_url` + `status: "pending_review"`
- **Batch PR 合併**：群組批量操作合併為單一 PR（非 N 個），減少 reviewer 負擔
- **Async batch operations**：`?async=true` query param 啟用非同步模式，回傳 `task_id` + `status: "pending"`。goroutine pool 執行，GET `/tasks/{id}` polling 查詢進度
- **Orphaned task 容錯**：Pod 重啟後 in-memory task state 遺失，GET `/tasks/{id}` 回傳 404 附帶 `pod_may_have_restarted` hint
- **SSE 即時通知**：`GET /api/v1/events` 端點，gitops.Writer 寫入成功後自動推播 `config_change` 事件。採用 Server-Sent Events 實作，零外部依賴
- **tenant-manager.jsx**：Pending PRs 提示 banner（頂部顯示待審核 PR 數量與連結，30s 輪詢）

### Platform Abstraction Layer + GitLab 支援

為使 PR write-back 成為平台無關的能力，抽取 platform interface 並新增 GitLab MR 支援。

- **`internal/platform/platform.go`**（新增）：`PRInfo` struct、`Client` interface（5 methods: CreateBranch / CreatePR / ListOpenPRs / ValidateToken / DeleteBranch）、`Tracker` interface（6 methods）。handler 只依賴 interface，provider 可替換
- **`internal/gitlab/`**（新增套件）：GitLab REST API v4 client，`PRIVATE-TOKEN` header 認證，`url.PathEscape` 支援含 `/` 的 `group/subgroup/project` 路徑。全部 5 個 `platform.Client` 方法 + 6 個 `platform.Tracker` 方法
- **Write mode 路由**：`--write-mode direct | pr | pr-github | pr-gitlab` 四種模式，`pr` 為 `pr-github` alias（向後相容）
- **On-Premise 支援**：GitHub Enterprise Server（`TA_GITHUB_API_URL`）+ 自託管 GitLab（`TA_GITLAB_API_URL`）。`SetBaseURL()` 已納入 `platform.Client` interface
- **Compile-time interface assertions**：`var _ platform.Client = (*Client)(nil)` + `var _ platform.Tracker = (*Tracker)(nil)` 確保型別安全
- **錯誤訊息衛生化**：`doRequest` 在 HTTP 4xx/5xx 時 log 完整 response body（debugging），回傳 caller 的 error 只含 status code（不洩漏 API body 給前端）。GitHub + GitLab 兩端一致
- **GitLab state 正規化**：`normalizeState()` 將 GitLab `opened` 映射為 `open`（與 GitHub 一致）
- **ListOpenPRs pagination**：per_page=100, 10 pages safety limit

### 設計系統統一

v2.5.0 暴露了三套平行 CSS 系統（CSS variables / Tailwind / inline styles）是所有無障礙問題的根源。v2.6.0 建立 design token SSOT 並全面遷移。

- **`docs/assets/design-tokens.css`**（新增）：統一 CSS variable 定義（11 個類別：color, spacing, typography, shadow, radius, transition 等），按 §1-§11 組織，命名規範 `--da-{category}-{element}-{modifier}`
- **Dark mode 三態切換**：`[data-theme="dark"]` attribute 取代 `@media (prefers-color-scheme: dark)`。Portal 加入 Light / Dark / System 三態切換按鈕，狀態存 localStorage（fallback: in-memory + cookie）
- **tenant-manager.jsx 遷移**：消除 454 行 hardcoded inline styles，全面切換至 CSS variables + Tailwind classes
- **focus-visible 全局化**：CSS 層統一實作，不再依賴各 JSX 檔案自行加入
- **index.html 統一**：legacy aliases（`var(--bg)`, `var(--muted)`）全面遷移至 `var(--da-*)` tokens
- **`docs/internal/design-system-guide.md`**（新增）：design token 命名規範、使用方式、`[data-theme]` 切換機制、Light/Dark/System 三態邏輯

### 價值傳達與互動工具

讓潛在使用者與現有客戶能快速量化平台的採用價值。

- **ROI Calculator 增強**：新增 Quick Estimate 模式（單一輸入即出結果）+ 完整三維計算（Rule Maintenance + Alert Storm + Time-to-Market）
- **Migration ROI Calculator**（新增 JSX）：輸入 PromQL 行數 / rules / tenants → coverage estimation + migration effort + break-even analysis
- **Cost Estimator**（新增 JSX, 827 lines）：tenants × packs × scrape interval × retention × HA replicas × deployment mode → Resource Summary + Monthly Cost + ConfigMap vs Operator 比較 + Quick Recommendation
- **Notification Template Editor**（大幅改版, 897 lines）：從 Previewer 升級為 Editor——可編輯 title/body 模板 + template variable autocomplete + validation（unmatched braces, char limits）+ live preview + export YAML/JSON + template gallery（Detailed/Compact/Bilingual presets）
- **architecture-and-design.md** 每個子主題加入 business impact 欄位（ZH + EN，O(M) vs O(N×M) 複雜度對比、Onboard 2hr→5min 等量化指標）
- **release-notes-generator.jsx** 新增 `generateAutoSummary()` 函式，CHANGELOG 角色分流自動摘要（per-role "What's new for you"）

### 測試與品質

- **Playwright axe-core 整合**：`@axe-core/playwright` 自動偵測 WCAG 違規，整合到既有 5 個 smoke tests + 新增 Operator Wizard 12 tests
- **Property-based testing**（新增 22 tests）：Hypothesis 覆蓋 tenant name RFC 1123 validation、SHA-256 hashing、drift detection symmetry、YAML round-trip、kustomization builder。`@settings(max_examples=100)` 確保覆蓋
- **Go `-race` 全通過**：Phase .e 發現並修復 async/taskmanager.go + ws/hub_test.go data race。`Get()` 改為回傳 deep copy snapshot 防止併發讀寫
- **大型 Python 工具重構**：`generate_alertmanager_routes.py`（1,474→1,645 lines，21 helpers extracted，>100 行函式 4→0）+ `init_project.py`（1,404→1,438 lines，6 helpers extracted）
- **aria-live regions**：tenant-manager.jsx 新增 4 個 region（sidebar, PRs banner, batch, tenant grid）+ threshold-heatmap.jsx 新增 3 個 region
- **Batch response summary**：tenant_batch.go + group_batch.go 回傳 `summary` 欄位（"N succeeded, M failed"）
- **version-consistency hook 擴展**：覆蓋 e2e/package.json、JSX 工具版號
- **tool-registry.yaml 對齊**：補齊 3 個缺失條目（rbac-setup-wizard, release-notes-generator, threshold-heatmap）

### 數字

| 項目 | v2.5.0 | v2.6.0 | 變化 |
|------|--------|--------|------|
| JSX 互動工具 | 38 | 42 | +4 |
| ADRs | 10 | 11（+ ADR-011）+ ADR-008 addendum | +1 |
| Operator 子文件 | 1 | 10（4 ZH + 4 EN + 2 hub） | +9 |
| Go test packages（`-race` clean） | — | 11 packages, 0 race | NEW |
| Property-based tests (Hypothesis) | 0 | 22 | NEW |
| Helm chart features | — | `rules.mode` toggle + ServiceMonitor | NEW |
| Write-back 模式 | 1（direct） | 4（direct / pr / pr-github / pr-gitlab） | +3 |
| Platform providers | 0 | 2（GitHub + GitLab） | NEW |
| Python 工具 | 91 | 95 | +4 |
| Pre-commit hooks | 19 auto + 9 manual | 19 auto + 10 manual | +1 manual |
| 環境變數（tenant-api） | ~10 | ~18 | +8（Write-back + GitLab） |

### 🐛 Bug Fixes

- `migrate_to_operator.py`：`discover_tenant_configs()` 靜默過濾無效 tenant 名稱 → 改為回報至 `analysis["issues"]` 清單
- `tracker.go`：`RegisterPR()` 同 tenant 可能重複 append → 改為 replace-or-append 邏輯
- `migration-roi-calculator.jsx`：2 個 label 未翻譯
- index.html：light-mode `.journey-phase-badge` + `.card-icon` 殘留 hardcoded hex color → 全部改用 design tokens
- README.md / README.en.md：badge 版號 v2.5.0 → v2.6.0
- troubleshooting.en.md：缺少 Prometheus Operator 章節 → 新增完整診斷+修正步驟+Rollback 程序
- troubleshooting.md：Operator 章節僅有診斷 → 補充三種修正步驟 + Rollback 程序

---

## [v2.5.0] — Multi-Tenant Grouping × Saved Views × E2E Testing (2026-04-06)

v2.5.0 在 v2.4.0 建立的 Tenant API 基礎上，實現租戶分群管理（ADR-010）、Saved Views、Playwright E2E 測試基礎，並新增 4 個互動工具。

### Multi-Tenant Grouping（ADR-010）

- 新增 `conf.d/_groups.yaml` 儲存結構：靜態 `members[]` 成員清單，Git 版本化，可 code review
- Group CRUD API：`GET/PUT/DELETE /api/v1/groups/{id}` + `POST /api/v1/groups/{id}/batch` 批量操作
- Permission-filtered listing：ListGroups 只回傳使用者有權限存取至少一個成員的 group
- 批量操作逐 tenant 驗證寫入權限，部分失敗不影響已成功項目

### Saved Views API

- 新增 `conf.d/_views.yaml`：持久化篩選條件（environment + domain + 自訂 filter 組合）
- CRUD 端點：`GET/PUT/DELETE /api/v1/views/{id}`，支援使用者自建常用視圖
- 與 Portal tenant-manager 整合：一鍵切換預設篩選

### Tenant Metadata 擴展

- 新增可選欄位：`environment`、`region`、`domain`、`db_type`、`tags[]`、`groups[]`
- 全部向後相容——未設定 metadata 的 tenant 不受影響
- Metadata 僅 API/UI 層使用，不影響 Prometheus metric cardinality

### RBAC 增強

- `_rbac.yaml` 新增 `environments[]` 和 `domains[]` 可選過濾欄位
- 支援「特定 group 只能管理 production 環境」等細粒度控制

### 新增互動工具（34 → 38 JSX tools）

- **Deployment Profile Wizard** (`deployment-wizard.jsx`)：互動式 Helm values 產生器
- **RBAC Setup Wizard** (`rbac-setup-wizard.jsx`)：互動式 `_rbac.yaml` 產生
- **Release Notes Generator** (`release-notes-generator.jsx`)：從 CHANGELOG 自動產生角色導向更新摘要
- **Threshold Heatmap** (`threshold-heatmap.jsx`)：跨 tenant 閾值分佈熱力圖 + 離群偵測 + CSV 匯出

### Playwright E2E 測試基礎

- 5 個 critical path spec（38 個 test case）：portal-home、tenant-manager、group-management、auth-flow、batch-operations
- Mock API 隔離（無外部依賴）、GitHub Actions CI 整合
- `tests/e2e/playwright.config.ts` + `.github/workflows/playwright.yml`

### CI/CD 改進

- tenant-api Go 測試納入 CI pipeline（2,115 行測試程式碼）
- Release 流程強化：`make pre-tag` 閘門、`bump_docs.py` 新增 tenant-api 版號線

### 數字

| 項目 | v2.4.0 | v2.5.0 | 變化 |
|------|--------|--------|------|
| JSX 互動工具 | 34 | 38 | +4 |
| ADRs | 9 | 10 | +1（ADR-010） |
| Playwright E2E specs | 0 | 5（38 test cases） | NEW |
| API 端點 | ~10 | ~16 | +6（groups + views） |

---

## [v2.4.0] — 防守深化 × 體質精簡 × 租戶管理 API (2026-04-05)

v2.4.0 的核心是「從能用到好管」：將 v2.3.0 release 暴露的手動痛點全面自動化（Phase A），對膨脹的核心檔案進行結構性重構（Phase B/B.5），引入 Tenant Management API 作為管理平面（Phase C），並重整 Playbook 體系（Phase D）。

### Phase A — 防守工具補強

將 v2.3.0 release 過程中手動發現的 6 類問題轉化為 pre-commit hook，auto hooks 從 13 個增至 19 個。

- **`check_build_completeness.py`**：`build.sh` ↔ `COMMAND_MAP` 雙向同步檢查，防止 Docker image 中工具遺漏
- **`check_bilingual_structure.py`**：ZH/EN 文件 heading hierarchy 骨架比對 + README 雙語導航對稱性
- **`check_jsx_i18n.py`**：`TOOL_META` ↔ `CUSTOM_FLOW_MAP` key set 一致性、`window.__t` 雙參數驗證
- **`check_makefile_targets.py`**：每個 `dx/generate_*.py` 和 `dx/sync_*.py` 工具被至少一個 Makefile target 引用
- **`check_metric_dictionary.py`**：`metric-dictionary.yaml` 與 Rule Pack YAML 交叉驗證，偵測 stale/undocumented entries
- **`check_cli_coverage.py` hook 化**：從測試升級為 pre-commit auto hook，cheat-sheet ↔ cli-reference ↔ COMMAND_MAP 三向一致
- **`_lint_helpers.py`**：抽取 `parse_command_map()`、`parse_build_sh_tools()`、`BUILD_EXEMPT` 等共用邏輯，消除 ~80 行重複

### Phase B — Go config.go 分拆 + 程式碼體質改善

- **config.go 拆分**（2,093 行 → 4 檔案）：`config_types.go`（268 行，型別定義）+ `config_parse.go`（277 行，YAML 解析）+ `config_resolve.go`（750 行，ResolveAt + 驗證）+ `config.go`（823 行，ConfigManager + 公開 API）
- 拆分為純結構移動，public API 語意不變，benchmark 差異 -0.3% ~ -5.0%（±5% 以內）
- **config_test.go table-driven 重構**：4,236 → 3,929 行（-7.2%），38 個重複 test function 收斂為 8 個 table-driven test，test function 總數 145 → 115
- Go 全部 145 測試通過，Python 3,657 passed / 44 skipped / 0 failed

### Phase B.5 — 文件與測試瘦身

Phase B 做到了「結構整理」，B.5 補做「內容精簡」。

- **合併 `context-diagram.md` → `architecture-and-design.md`**：~70% 重疊內容消除，淨刪 ~1,165 行，docs/ 檔案數 115 → 113
- **`incremental-migration-playbook.md` 瘦身**：1,165 行 → 575 行（-50.6%），冗長 JSON 範例改為摘要，手動 kubectl 序列改為 `da-tools` 命令
- **三態說明集中化**：`tenant-lifecycle.md` 的 60 行重複三態解釋改為 hyperlink + 3 行速查
- **版號全域修正**：44 處過時版號更新 + 文件計數修正
- 文件總計：docs/ -2,362 行（-6.4%），-2 個檔案

### Phase C — Tenant Management API（ADR-009）

新增 `components/tenant-api/` Go 元件，為 da-portal 加入 Backend API。

**架構決策（ADR-009）**
- API 語言選 Go：與 threshold-exporter 共用 `pkg/config` 解析邏輯，避免 Go↔Python 雙端維護
- 認證用 oauth2-proxy sidecar：API server 零 auth 程式碼，讀 `X-Forwarded-Email` / `X-Forwarded-Groups` header
- 寫回用 commit-on-write：UI 操作 → API → 修改 YAML → git commit（操作者名義），保留完整 audit trail
- RBAC 用 `_rbac.yaml` + `atomic.Value` 熱更新：lock-free 讀取，與 threshold-exporter reload 模式一致
- 不引入資料庫——Git repo 就是 database

**`pkg/config/` 抽取**
- 將 threshold-exporter 的型別與解析邏輯抽入 `components/threshold-exporter/app/pkg/config/`（`types.go` + `parse.go` + `resolve.go`）
- tenant-api 透過 `go.mod replace` directive 直接 import 共用型別

**API 端點**
- `GET /api/v1/tenants` — 租戶列表（支援 group/env 篩選）
- `GET/PUT /api/v1/tenants/{id}` — 單一租戶 CRUD
- `POST /api/v1/tenants/{id}/validate` — 乾跑驗證（不寫入）
- `POST /api/v1/tenants/batch` — 批量操作（`sync.Mutex` 同步，response 預留 `task_id`）
- `GET /api/v1/tenants/{id}/diff` — 預覽變更差異
- Health check / readiness probe / Prometheus metrics

**Portal 降級安全**：API 不可用時，tenant-manager.jsx 自動降級為 platform-data.json 唯讀模式。

**交付物**：Go binary + Docker image（distroless base）+ Helm chart + K8s manifests + 五線版號新增 `tenant-api/v*`

### Phase D — Playbook 重整 + 文件治理

- Playbook 結構化：testing-playbook 五段分層、benchmark-playbook 加入決策樹、windows-mcp-playbook 32 個 pitfall 分類索引
- `bump_docs.py` 自動計數功能：掃描並更新散落各處的工具數量、Rule Pack 數量等
- doc-map.md 自動生成預設包含 ADR

### 數字

| 項目 | v2.3.0 | v2.4.0 | 變化 |
|------|--------|--------|------|
| Pre-commit hooks | 13 auto + 7 manual | 19 auto + 9 manual | +6 auto, +2 manual |
| Go config.go | 2,093 行 × 1 檔 | 4 檔（268 + 277 + 750 + 823） | 結構拆分 |
| config_test.go | 4,236 行 / 145 函式 | 3,929 行 / 115 函式 | -7.2% / table-driven |
| docs/ 行數 | 37,059 | 34,697 | -2,362（-6.4%） |
| Components | 3 | 4（+ tenant-api） | +1 |
| ADRs | 8 | 9（+ ADR-009） | +1 |
| JSX 互動工具 | 29 | 34 | +5 |
| 版號線 | 4 | 5（+ tenant-api/v*） | +1 |
| Python 工具 | 84 | 91 | +7 |

---

## [v2.3.0] — Operator-Native × Management UI × Platform Maturity (2026-04-04)

v2.3.0 聚焦四大主題：Operator-Native 整合、Multi-Instance Management UI、Portal & Doc 成熟度、品質閘門升級。

### Phase .a — Portal & DX Foundation

**Self-Service Portal 模組化**
- `self-service-portal.jsx`（1,376 行）→ 5 個模組：`portal-shared.jsx`（共用常數/函式/元件）+ `YamlValidatorTab.jsx` + `AlertPreviewTab.jsx` + `RoutingTraceTab.jsx` + coordinator
- 新增 `dependencies` frontmatter 機制：jsx-loader.html 支援 YAML frontmatter 中宣告依賴，依序載入 → `loadDependency()` / `loadDependencies()` / `transformImports()`
- `window.__portalShared` 模式：共用模組透過全域變數註冊，tab 模組解構取用

**Template Gallery 外部化**
- 24 個模板 → `docs/assets/template-data.json`（雙語 `{zh, en}` 物件格式 + `category` 欄位）
- `template-gallery.jsx` 改為 `useEffect` fetch 載入，新增 loading/error 狀態
- 檔案大小：806 → 293 行（-64%）

**Portal Hub 五層重組**
- 29 個工具卡片從 2 區（Interactive / Advanced）→ 5 層級：Start Here、Day-to-Day、Explore & Learn、Simulate & Analyze、Platform Operations
- 新增 Quick Access 面板（5 個常用工具快捷連結）
- 每層級附色彩標籤（Onboarding / Core Workflow / Reference / What-If / Engineer）
- Role filter 同時作用於 Quick Access chips
- Tour 步驟更新、Footer 版號同步

**文件模板系統**
- 新增 `docs/internal/doc-template.md`：定義文件標準結構（frontmatter + 必要 section + Related Resources）
- 新增 `scripts/tools/lint/check_doc_template.py`：frontmatter 完整性 + Related Resources 存在性 + 版號一致性

**`_lib_python.py` 模組拆分**
- `_lib_python.py` → 4 個子模組：`_lib_constants.py`（守護值/常數）+ `_lib_io.py`（檔案 I/O）+ `_lib_validation.py`（驗證邏輯）+ `_lib_prometheus.py`（HTTP/Prometheus 查詢）
- 原檔保留為 re-export facade（向後相容，53 行）

**SAST Rule 7**
- 新增 `TestStderrRouting`：AST 掃描 `print("ERROR..."` / `print("Error..."` 確保附帶 `file=sys.stderr`
- 支援 literal string 和 f-string 兩種格式偵測

---

### Phase .b — Operator-Native + Federation

**ADR-008: Operator-Native Integration Path**
- 雙路整合架構決策：既有 ConfigMap 路徑保留，新增 Operator-Native 模式作為 BYO 方案
- 工具鏈適配而非平台重寫原則——threshold-exporter Go 核心語意不變
- 新增 `detectConfigSource()` 函式：逐級檢測 operator env var → git-sync `.git-revision` 文件 → configmap（預設）

**Prometheus Operator 整合指南**
- 新增 `docs/prometheus-operator-integration.md`（雙語 zh + en）：架構圖、CRD 對應、3 個部署場景（all-in-one / mixed / operator-only）
- BYO 文件清理：移除 Prometheus Operator appendices，改為重定向至新指南
- ServiceMonitor / PrometheusRule / AlertmanagerConfig CRD 映射表

**da-tools Operator 工具**
- **`da-tools operator-generate`** — 從 Rule Packs + Tenant 配置產生 PrometheusRule / AlertmanagerConfig / ServiceMonitor CRD YAML
  - 支援 `--namespace` / `--labels` / `--annotations` 自訂，`--output-format yaml | json`
  - 整合於 da-tools entrypoint + build.sh 打包
- **`da-tools operator-check`** — CRD 驗證工具：PrometheusRule 語法 + AlertmanagerConfig 路由合法性 + ServiceMonitor label 一致性
  - 支援 `--kubeconfig` / `--context` 直連 K8s 驗證，亦支援離線 YAML 驗證
  - Registered in CI lint pre-commit hooks

**Config Info Metric（四層感知）**
- 新增 `threshold_exporter_config_info{config_source, git_commit}` info metric
- 三種模式 + 自動偵測：
  - `configmap`（預設）：從 ConfigMap mount path 讀取 config version
  - `git-sync`：讀取 `.git-revision` 共享 volume 文件，提供 git commit SHA
  - `operator`：讀取 env var `CONFIG_SOURCE=operator` + `GIT_COMMIT=<sha>`
- `detectConfigSource()` 呼叫於 reload 時，確保 metric 實時反映部署形態

**Federation Scenario B（邊緣-中央分裂）**
- **`da-tools rule-pack-split`** — Rule Pack 聯邦分裂工具：
  - Part 1（正規化層）：邊緣側 metric value 驗證、單位轉換、異常值濾除 → 產生 Prometheus RecordingRules
  - Parts 2+3（閾值 + 警報層）：中央側聚合、cross-edge 關聯、全域告警決策 → 產生 Alerting Rules
  - 支援 `--operator` CRD 輸出 + `--gitops` 模式（目錄結構）
  - 關鍵特性：無狀態 split（idempotent）、邊緣 auto-healing（快照回滾）
- **`federation-integration.md` §8** — Scenario B 完整文件：三階段部署（邊緣佈建 → 中央策略 → 端對端驗證）、MTTR 優化、成本模型

**Go 單元測試（+12 tests，覆蓋率 87% → 94%）**
- WatchLoop 整合測試：無檔案變動 / 新增檔案 / 更新現有檔案
- `resolveConfigPath()` 三情案例：configmap flag / git-sync flag / 未設定（預設 configmap）
- `detectConfigSource()` 四情案例：configmap（預設）/ git-sync / operator / precedence（operator > git-sync > configmap）
- Config Info metric 收集器三情案例：各模式 value 驗證 + label 正確性
- Fail-Safe Reload E2E：config 不可讀時 fallback 邏輯

---

### Phase .c — Management UI + Intelligence

**Tenant Manager Data Foundation**
- 新增 `scripts/tools/dx/generate_tenant_metadata.py`：從 `conf.d/` 目錄結構推斷租戶 metadata
  - Rule Pack 推斷：根據 YAML 中 metric prefix 比對 Rule Pack 定義
  - 運營模式推斷：`_silent_mode` / `_state_maintenance` 標誌偵測
  - 路由通道推斷：`_routing` 配置解析
- 擴展 `scripts/tools/dx/generate_platform_data.py`：產出的 `platform-data.json` 新增 `tenant_groups` + `tenant_metadata` 結構
- Tenant metadata 版本化：支援 `--output-dir` 自訂輸出路徑，方便 GitOps 集成

**Tenant Manager UI 元件**
- 新增 `docs/interactive/tools/tenant-manager.jsx`（~650 行）：
  - 響應式卡片牆佈局，環境/層級徽章（dev/staging/prod + app/infra/platform）
  - 運營模式指示器：Normal / Silent / Maintenance 視覺標記 + expires 倒數
  - 批量操作：批次維護/靜默模式 YAML 產生器，支援日期範圍選擇
  - 篩選+搜尋：按環境/層級/模式多維度過濾，模糊搜尋租戶名
- 加入 `tool-registry.yaml` + Portal Hub Tier 1 (Day-to-Day 層級)

**閾值推薦 × Portal 智慧**
- 新增 `docs/assets/recommendation-data.json`：15 個核心指標的 P50/P95/P99 預計算資料
  - 資料來源：歷史基線 + 業界最佳實踐
  - 格式：`{metric_name: {p50, p95, p99, source, last_updated}}`
- 擴展 `docs/interactive/tools/AlertPreviewTab.jsx`：
  - Progress bar 上疊加 recommended value marker 視覺指示
  - Confidence badge（high/medium/low）顯示推薦可信度
  - 新增 "Apply Recommended Values" 按鈕，一鍵生成更新 YAML

**OPA/Rego 策略整合**
- 新增 `scripts/tools/ops/policy_opa_bridge.py`（~450 行）：tenant YAML → OPA input JSON 轉換 + 雙模式評估
  - 轉換函式：YAML 欄位 → OPA JSON 輸入格式映射（支援 nested policies）
  - 評估模式：REST API 模式（連接遠端 OPA 伺服器）+ 本地 opa binary 模式
  - 違規輸出格式轉換：OPA violations → da-tools 標準格式（location + description）
- `scripts/policies/examples/` 新增三個 Rego 範例策略：
  - `routing-compliance.rego`：路由規則命名 / receiver type / group_wait 範圍 validation
  - `threshold-bounds.rego`：閾值範圍檢查 / 關鍵指標預留冗餘
  - `naming-convention.rego`：租戶/告警 ID 命名規範 + Prefix 合法性
- 登記為 `da-tools opa-evaluate` 子命令 + CI lint 整合

**Portal i18n Lint 工具**
- 新增 `scripts/tools/lint/check_portal_i18n.py`（~250 行）：掃描 JSX 檔案尋找硬編碼字串
  - AST 解析：偵測 string literal 未用 `window.__t()` 包裝的情況
  - 支援 `--fix-mode`：自動生成修復建議（帶位置資訊）
  - 排除清單：URL / 特殊字元序列 / i18n 函式呼叫內部字串
- 加入 pre-commit manual-stage hooks 為 `check-portal-i18n`

---

### Phase .d — Quality Gate + CI Maturity

**GitHub Actions CI Matrix**
- 新增 `.github/workflows/ci.yml`：Python 3.10/3.13 × Go 1.22/1.26 矩陣（4 × 2 = 8 組合）
- 4 個主 jobs：lint（文件+工具格式）、python-tests（pytest + coverage）、go-tests（threshold-exporter）、lint-docs（SAST + doc 品質）
- pip/Go module 緩存策略、coverage artifacts 產生、失敗時自動 debug log 產出

**Coverage Gate 強制**
- `pyproject.toml` 新增 `fail_under = 85`，CI 強制 `--cov-fail-under=85` 執行
- README.md 新增 CI badge 與 coverage badge（green ≥85%、yellow 80–85%、red <80%）
- Python 工具預期整體覆蓋率 ≥85%

**Python 型別系統加強**
- `_lib_constants.py`、`_lib_io.py`、`_lib_validation.py`、`_lib_prometheus.py` 加入完整型別提示
- 新增 `mypy.ini`：strict mode for all `_lib_*` modules、relaxed mode for test files
- CI lint job 新增 `mypy scripts/tools/_lib_*.py --config-file=scripts/tools/mypy.ini` 步驟

**Integration + Snapshot 測試**
- `tests/test_tool_exit_codes.py`（parametrized）：全部 84+ 工具的 `--help` + invalid args exit code 合約測試
- `tests/test_pipeline_integration.py`：scaffold → validate → routes 完整 pipeline 端對端測試
- `tests/test_snapshot.py`：help output stability snapshot tests，支援 `--snapshot-update` CI 模式

**Pre-commit Hook 驗證確認**
- 確認 13 個 auto-run hooks + 7 個 manual-stage hooks 全部運作，Phase .a–.c 新增項目完全涵蓋
- `make pre-commit-audit` 新增 make 目標印出 hook 清單與觸發規則

---

## [v2.2.0] — 採用管線 + UX 升級 + 運維工具 (2026-03-17)

v2.2.0 聚焦三大主題：降低採用門檻的 Adoption Pipeline、Portal 互動體驗全面升級、配置運維新工具。新增 2 個 CLI 工具、3 個互動工具、Portal 三大 Tab 重構、24 個 Template Gallery 模板、5-tenant 展演腳本與 Hands-on Lab。

### 採用管線（Phase A — Adoption Pipeline）

- **`da-tools init`** — 專案骨架一鍵產生：CI/CD pipeline（GitHub Actions / GitLab CI）、`conf.d/` 目錄（含 `_defaults.yaml` + tenant YAML）、Kustomize overlays、`.pre-commit-config.da.yaml`，支援 `--non-interactive` 自動模式
- **GitOps CI/CD 整合指南** (`docs/scenarios/gitops-ci-integration.md`) — 三階段管線（Validate → Generate → Apply）、ArgoCD / Flux 整合、PR Comment Bot 工作流
- **Kustomize Overlays** — `configMapGenerator` 模式產生 threshold-config ConfigMap

### UX 升級（Phase B — Portal & Templates）

**Self-Service Portal 重構（3 Tab）**
- **Tab 1 (YAML Validation)**: Rule Pack 多選 → metric autocomplete → 動態 sample YAML 產生 → 即時驗證（含 pack-aware metric key 交叉檢查）
- **Tab 2 (Alert Preview)**: Pack-grouped 滑桿、視覺化閾值條、disabled/no-threshold 狀態顯示、severity dedup 說明
- **Tab 3 (Routing Trace)**: Metric+severity 輸入 → Alert origin → Inhibit check → 四層合併 → Domain Policy check → 通知派送 → NOC 副本

**Template Gallery 擴充（6 → 24 模板）**
- 7 場景模板：ecommerce、iot-pipeline、saas-backend、analytics、enterprise-db、event-driven、search-platform
- 13 Quick Start 模板：每個可選 Rule Pack 各一
- 4 特殊模板：maintenance、routing-profile、finance-compliance、minimal
- View mode 切換（All / Scenarios / Quick Start）+ Pack filter chips + Coverage summary

**新增互動工具**
- **CI/CD Setup Wizard** (`cicd-setup-wizard.jsx`) — 5 步精靈產生 `da-tools init` 命令：CI Platform → Deploy Mode → Rule Packs → Tenants → Review & Generate（第 27 個 JSX 工具）
- **Notification Template Previewer** (`notification-previewer.jsx`) — 6 種 receiver 通知預覽（Slack / Email / PagerDuty / Webhook / Teams / Rocket.Chat）+ Dual-Perspective annotation 展示 + Severity Dedup 說明（第 28 個）
- **Platform Health Dashboard** (`platform-health.jsx`) — 平台健康儀表板：元件狀態、租戶概覽、Rule Pack 使用分佈、Reload 事件時間線（第 29 個）

**展演與教學**
- **Demo Showcase** (`scripts/demo-showcase.sh`) — 5-tenant 完整展演腳本（prod-mariadb / prod-redis / prod-kafka / staging-pg / prod-oracle），7 步驟自動執行，支援 `--quick` 模式
- **Hands-on Lab** (`docs/scenarios/hands-on-lab.md`) — 30–45 分鐘 Docker-based 實戰教程，8 個練習覆蓋 init → validate → routes → routing trace → blast radius → three-state → domain policy

### 運維工具（Phase C — Operations）

- **`da-tools config-history`** — 配置快照與歷史追蹤：`snapshot` / `log` / `show` / `diff` 子命令，`.da-history/` 存儲，SHA-256 變更偵測，git-independent 輕量級版本控制

### 漸進式遷移 Playbook

- **`docs/scenarios/incremental-migration-playbook.md`** — 四階段雙軌並行遷移法（Strangler Fig Pattern）：Phase 0 Audit（`onboard` + `blind-spot`）→ Phase 1 Pilot（單一 domain 影子部署）→ Phase 2 Dual-Run（`shadow-verify` 品質比對）→ Phase 3 Cutover（逐 domain 切換）→ Phase 4 Cleanup。每步有 CLI 指令、預期輸出、回退方式
- **`architecture-and-design.md` §2.13** — 新增效能架構說明：Pre-computed Recording Rule vs Runtime Aggregation 的 PromQL 對比，解釋為什麼 tenant 增加不會導致 Prometheus CPU/Memory 暴增

### GitOps Native Mode

- **`da-tools init --config-source git`** — 產生 git-sync sidecar Kustomize overlay，threshold-exporter 直接從 Git 倉庫讀取配置，省去 ConfigMap 中間層。支援 SSH / HTTPS 認證、自訂分支與路徑。git-sync sidecar 寫入 emptyDir shared volume，threshold-exporter 的既有 Directory Scanner + SHA-256 hot-reload 機制無縫復用
- **`da-tools gitops-check`** — GitOps Native Mode 就緒度驗證工具，三個子命令：`repo`（Git 倉庫可達性 + 分支驗證）、`local`（本地 conf.d/ 結構驗證）、`sidecar`（K8s git-sync 部署狀態檢查），支援 `--json` 和 `--ci` 模式
- **Container Image Security Hardening** — 三層防護：base pin + build-time upgrade + attack surface reduction
  - threshold-exporter：`alpine` → `distroless/static-debian12:nonroot`（零 CVE，無 shell/apk/openssl）
  - da-tools：`python:3.13-alpine` → `python:3.13.3-alpine3.22` multi-stage build（修復 CVE-2025-48174, CVE-2025-15467）
  - da-portal：`nginx:1.28-alpine` → `nginx:1.28.0-alpine3.22` + `apk del libavif gd libxml2`（移除未使用 library，消除掃描器 false positive）

### 數字

| 項目 | v2.1.0 | v2.2.0 | 變化 |
|------|--------|--------|------|
| Python 工具 | 73 | 77 | +4 |
| da-tools CLI 命令 | 27 | 36 | +9 |
| JSX 互動工具 | 26 (+1 wizard) | 29 | +3 |
| Template Gallery 模板 | 6 | 24 | +18 |
| 場景文件 | 6 | 9 | +3 |
| Makefile targets | — | +1 (`demo-showcase`) | NEW |

---

## [v2.1.0] — 運維自助 + 告警智能化 + 性能優化 + 跨域路由 (2026-03-16)

v2.1.0 自 v2.0.0 起的全量升級。涵蓋 Go Exporter 增量熱載入、告警關聯分析、跨域路由架構 (ADR-006/007)、生態整合 (Backstage Plugin)、5 個新 CLI 工具、3 個互動工具、測試 +75%（1,759 → 3,070）、文件治理與正確性全面校正。

### Go Exporter 核心

**Incremental Hot-Reload (§5.6)**
- per-file SHA-256 index + parsed config cache，WatchLoop 增量重載路徑
- `ConfigManager` 新增 `fileHashes` / `fileConfigs` / `fileMtimes` 欄位
- `scanDirFileHashes()` — mtime guard + 輕量 hash check（mtime 未變直接跳過 I/O）
- `IncrementalLoad()` — 比對 per-file hash → 只重新解析 changed/added files → `mergePartialConfigs()`
- `fullDirLoad()` — 完整載入並初始化 cache（首次載入或 fallback）
- `applyBoundaryRules()` — 提取為獨立函式供共用
- **效能優化**：logConfigStats 取代 Resolve()、mtime guard、incremental merge（tenant 檔變動直接 patch）、byte cache（scan 快取復用，免除重複 I/O）
- 15 個 Go tests + 5 個 benchmarks（含 NoChange / OneFileChanged / ScanHashes / MergePartials）

**程式碼品質**
- 4 處 error print 修正為 `stderr` 輸出
- `parsePromDuration` / `isDisabled` / `clampDuration` 新增單元測試
- Go test 增加 config_test.go（801 行）+ config_bench_test.go（268 行）+ main_test.go（97 行）

### 跨域路由架構（ADR-006 + ADR-007）

**ADR-006: Tenant Mapping Topologies (1:1, N:1, 1:N)**
- 資料面映射方案：Prometheus Recording Rules 實現 1:N 映射（exporter 零修改）
- `generate_tenant_mapping_rules.py` — 讀取 `_instance_mapping.yaml`，產出 Recording Rules（36 tests）
- `scaffold_tenant.py` 新增 `--topology=1:N`、`--mapping-instance`、`--mapping-filter` 參數（9 tests）
- 範例設定檔 `_instance_mapping.yaml`

**ADR-007: Cross-Domain Routing Profiles**
- 四層合併管線：`_routing_defaults` → `routing_profiles[ref]` → tenant `_routing` → `_routing_enforced`
- `generate_alertmanager_routes.py` 擴展：profile 解析 + `check_domain_policies()` 驗證（21 tests）
- `scaffold_tenant.py` 新增 `--routing-profile` 參數
- 重構 `_parse_config_files()` → `_parse_platform_config()` + `_parse_tenant_overrides()` 子函式
- 範例設定檔 `_routing_profiles.yaml`、`_domain_policy.yaml`

**ADR-007 工具生態**
- `explain_route.py` — 路由合併管線除錯器：四層展開、`--show-profile-expansion`、`--json`、da-tools CLI 整合（25 tests）
- `check_routing_profiles.py` — CI lint 工具：未知 profile ref、孤立 profile、格式錯誤 constraints、`--strict` 模式（28 tests + pre-commit hook）

### 新增 CLI 工具

- **`da-tools test-notification`** — 6 種 receiver 連通性測試（webhook/slack/email/teams/pagerduty/rocketchat），Dry-run / CI gate / per-tenant 批次。57 tests，97% 覆蓋率
- **`da-tools threshold-recommend`** — 基於歷史 P50/P95/P99 的閾值推薦引擎，純 Python 統計，信心等級分級。54 tests，96% 覆蓋率
- **`da-tools alert-correlate`** — 告警關聯分析：時間窗口聚類 + 關聯分數 + 根因推斷，支援線上/離線模式。95% 覆蓋率
- **`da-tools drift-detect`** — 跨叢集配置漂移偵測：SHA-256 manifest 比對，pairwise 多目錄分析 + 修復建議。99% 覆蓋率
- **`da-tools explain-route`** — 路由合併管線除錯器（ADR-007），25 tests

### 生態整合

- **Backstage Plugin**：`components/backstage-plugin/` TypeScript/React plugin
  - `DynamicAlertingPage` + `DynamicAlertingEntityContent`
  - `PrometheusClient` API 層：via Backstage proxy 查詢 threshold / silent_mode / ALERTS
  - Entity 整合：`dynamic-alerting.io/tenant` annotation → 自動對應租戶

### 互動工具

- **Multi-Tenant Comparison** (`multi-tenant-comparison.jsx`)：Heatmap 色彩矩陣 + Outlier detection + Divergence Ranking（第 25 個 JSX 工具）
- **Alert Noise Analyzer** (`alert-noise-analyzer.jsx`)：MTTR 計算、震盪偵測、去重空間分析、Top noisy alerts（第 26 個）
- **ROI Calculator** (`roi-calculator.jsx`)：Rule 維護 / Alert Storm / Time-to-Market 三模型成本分析（第 27 個）

### DX Tooling

- **`check_frontmatter_versions.py`** — frontmatter version 全域掃描 + `--fix` 自動修復（29 tests）
- **`coverage_gap_analysis.py`** — per-file 覆蓋率排行報表（22 tests）
- **`check_bilingual_content.py`** — 雙語內容 CJK 比例 lint
- **`check_doc_links.py`** — 跨語言對應檔案驗證
- **`validate_all.py`** 增強：`--notify`（桌面通知）、`--diff-report`（CI 失敗自動 diff）
- **`generate_rule_pack_stats.py --format summary`** — Badge 風格單行輸出
- **Snapshot tests v2** — alert_correlate、drift_detect、bilingual_content 快照測試

### 安全加固

- SAST 規則擴充：6 rules 自動掃描（encoding + shell + chmod + yaml.safe_load + credentials + dangerous functions），189 patterns
- NetworkPolicy 精細化、container security context 強化
- 憑證掃描 + `.env` 防護 + `os.chmod 0o600` 補齊
- **CVE 緩解**：CVE-2025-15467 (openssl CVSS 9.8 pre-auth RCE) + CVE-2025-48174 (libavif buffer overflow)
  - 所有 Dockerfile 加入 `apk --no-cache upgrade` 拉取安全修補
  - `da-tools` base image pin 從 `python:3.13-alpine` → `python:3.13.2-alpine3.21`
- **CI Image Scanning**：release workflow 三個 image 均加入 Trivy 掃描（CRITICAL + HIGH 阻斷）

### 品質閘門

- Pre-commit hooks：12 → **13** 個 auto-run（新增 `routing-profiles-check`）
- `build.sh` 修補：新增遺漏的 `alert_correlate`、`notification_tester`、`threshold_recommend` 打包

### 測試覆蓋率

Python 測試總數從 v2.0.0 的 1,759 提升至 **3,070**（+75%）。v2.1.0 新增工具均達 95%+ 覆蓋率，5 個既有工具從 41–74% 提升至 63–99%。Coverage gate 維持 `fail_under=64`，實際整體覆蓋率高於此基線。

### 數字

| 項目 | v2.0.0 | v2.1.0 | 變化 |
|------|--------|--------|------|
| Python 工具 | 62 | 73 | +11 |
| da-tools CLI 命令 | 23 | 27 | +4 |
| JSX 互動工具 | 24 | 26 (+1 wizard) | +3 |
| ADRs | 5 | 7（006/007 Accepted） | +2 |
| Python 測試 | 1,759 | 3,070 | +1,311 |
| Pre-commit hooks | 12 + 5 manual | 13 + 5 manual | +1 |
| Go tests (new files) | — | +3 files (1,166 lines) | NEW |

### Benchmark — Incremental Hot-Reload（Go, `-count=3` median）

| Benchmark | ns/op | B/op | allocs/op |
|-----------|------:|-----:|----------:|
| IncrementalLoad_NoChange_10 | 165,700 | 34,272 | 176 |
| IncrementalLoad_NoChange_1000 | 1,528,000 | 2,027,264 | 13,085 |
| IncrementalLoad_OneFileChanged_10 | 220,600 | 73,280 | 241 |
| IncrementalLoad_OneFileChanged_1000 | 6,908,000 | 6,652,880 | 22,211 |
| ScanDirFileHashes_1000 | 1,206,000 | 1,985,200 | 13,012 |

### 文件治理與正確性

**Root README (zh/en) 增強**
- 開頭改為問題導向定位（規則膨脹 + 變更瓶頸），新增「適用場景」聲明與版本 badge
- 「關鍵設計決策」表新增 ADR 連結欄 + Sentinel 三態控制、四層路由合併兩行
- Quick Start 下方新增「生產部署」指引，文件導覽新增 Day-2 Operations 路徑

**ADR 生命週期更新**
- ADR-006/007：`📋 Proposed` → `✅ Accepted (v2.1.0)`，checklist 改為實作摘要 + 後續方向
- ADR-004：「現況與後續方向」替代舊 Roadmap 段落
- ADR-001/003：新增 v2.1.0 living-doc 狀態行
- ADR-002/004：新增「相關決策」交叉引用（ADR-005/006）

**architecture-and-design.md**
- 新增 §2.12 Routing Profiles 與 Domain Policies（四層合併管線 Mermaid 圖）
- 「本文涵蓋內容」補上三態模式、Dedup、路由系統
- 拆分文件導覽表移除過時 §N 前綴
- ADR-006 工具引用、Rule Pack 數量修正、雙語 annotation 章節翻譯

**benchmarks.md 重構**
- §8（Alertmanager Idle-State）合併至 §10（Under-Load）作為 baseline 比較表
- §13（pytest-benchmark）去重：移除與 §7 重複的 route generation 行
- 傳統方案估算加註推算基礎（per-rule ~0.3ms / ~60KB）
- 自引用修正、相關資源連結格式修正

**docs/README.md (zh/en) 去重**
- 移除與 root README 重複的「工具速查」22 行表 → 精簡為摘要 + 連結
- 移除重複的「快速命令」和「版本與維護」段落

**Component README 修正**
- threshold-exporter：斷裂 §11.1 引用 → 指向 `gitops-deployment.md`
- da-tools：版號表 `v2.0.0` → `v2.1.0`，移除過時措辭
- da-portal：`24 JSX tools` → `26`，image tag `v2.0.0` → `v2.1.0`
- backstage-plugin：移除不存在的 `(§5.13)` 引用

**交叉引用修正**
- ADR-006 (zh/en)：`§2.6` → `§2.3`（Tenant-Namespace 映射模式）
- ADR README (zh/en)：ADR-006/007 badge 更新為 ✅ Accepted

### 🐛 Bug Fixes

- 修復 `entrypoint.py` help text 遺漏 `validate-config` 命令
- 4 處 Python error output 修正為 stderr
- `da-tools build.sh` TOOL_FILES 補齊遺漏工具

---

## [v2.0.0] — Alert Intelligence + Full-Stack DX Overhaul (2026-03-15)

v2.0.0 正式版。自 v1.11.0 起的全量升級：76 個 commits、346 個檔案變更（+73,057 / -12,023）。涵蓋 Go Exporter 增強、Rule Pack 擴展、告警智能化、互動工具生態、文件全面重構、測試工程化、專案結構正規化。

> **版號說明**：v1.12.0 / v1.13.0 / v2.0.0-preview 系列皆為開發中版本（無 Git tag / GitHub Release），統一於 `v2.6.0` 正式釋出。

### 🔧 Go Exporter 增強

**Tenant Profiles（四層繼承）**
- Go schema 新增 `Profiles map[string]map[string]ScheduledValue` 欄位
- `applyProfiles()` fill-in pattern：Load 階段展開 profile 至 tenant overrides（僅填入未設定的 key）
- `_profiles.yaml` boundary enforcement：LoadDir 限制 profiles 只能從該檔載入
- `ValidateTenantKeys()` 擴展：`_profile` 引用不存在的 profile → WARN
- 繼承順序：Global Defaults → Profile → Tenant Override（tenant 永遠勝出）
- 13 個新 Go 測試案例

**Dual-Perspective Annotation**
- `platform_summary` annotation：Alert 同時攜帶 Platform 視角（NOC）和 Tenant 視角 summary
- 與 `_routing_enforced` 整合：NOC 收到 `platform_summary`，tenant 收到原始 `summary`

### 📦 Rule Pack 擴展（13 → 15）

- **JVM Rule Pack** (`rule-pack-jvm.yaml`)：GC pause rate、heap memory usage、thread pool — 7 alert rules（含 composite `JVMPerformanceDegraded`）
- **Nginx Rule Pack** (`rule-pack-nginx.yaml`)：active connections、request rate、connection backlog — 6 alert rules
- Projected Volume 13 → 15 ConfigMap sources，scaffold_tenant / metric-dictionary 同步更新

### 🚀 告警智能化（3 個新工具 + 1 個 Self-Service Portal）

**Alert Quality Scoring (`da-tools alert-quality`)**
- 4 項品質指標：Noise（震盪偵測）、Stale（閒置 14 天）、Resolution Latency（flapping 警告）、Suppression Ratio
- 三級評分（GOOD/WARN/BAD）+ per-tenant 加權分數（0–100）
- 輸出：text / `--json` / `--markdown`，CI gate：`--ci --min-score 60`
- 57 個測試，89.8% 覆蓋率

**Policy-as-Code (`da-tools evaluate-policy`)**
- 宣告式 DSL：10 種運算子（required / forbidden / gte / lte / matches / one_of ...）
- `when` 條件式、萬用字元目標（`*_cpu`）、dot-path 嵌套（`_routing.receiver.type`）
- Duration 比較、tenant 排除、error/warning 雙嚴重度
- CI gate：`--ci` 有 error 違規 exit 1
- 106 個測試，94.0% 覆蓋率

**Cardinality Forecasting (`da-tools cardinality-forecast`)**
- 純 Python 線性回歸（無 numpy）：趨勢分類（growing/stable/declining）+ 風險等級（critical/warning/safe）
- 觸頂天數預測 + 預計日期，可設基數上限（`--limit`）和預警天數（`--warn-days`）
- CI gate：`--ci` 有 critical 風險 exit 1
- 61 個測試，93.5% 覆蓋率

**Tenant Self-Service Portal (`self-service-portal.jsx`)**
- 三分頁 SPA：YAML 驗證（schema + routing guardrails）、告警預覽（滑桿模擬）、路由視覺化（樹狀圖）
- 瀏覽器端執行，零後端依賴，雙語支援（zh/en）

**Self-Hosted Portal (`da-portal` Docker image)**
- `ghcr.io/vencil/da-portal` — nginx:alpine 靜態 image，打包 24 JSX tools + Hub + Guided Flows + vendor JS
- 企業內網 / air-gapped 部署：`docker run -p 8080:80`，免 build step
- Volume mount 客製化：`platform-data.json`、`flows.json`、`nginx.conf`（含 Prometheus reverse proxy placeholder 解決 CORS）
- CI/CD：`portal/v*` tag 觸發 `release.yaml` 自動 build + push GHCR

### 🛠️ DX 自動化工具（+8 個新工具）

**Operations**
- **`shadow_verify.py`**：Shadow Monitoring 三階段驗證（preflight / runtime / convergence）
- **`byo_check.py`**：BYO Prometheus & Alertmanager 整合驗證（取代手動 curl + jq）
- **`grafana_import.py`**：Grafana Dashboard ConfigMap 匯入（sidecar 掛載 + verify + dry-run）
- **`federation_check.py`**：多叢集 Federation 整合驗證（edge / central / e2e 三模式）

**Scalable Configuration Governance**
- **`assemble_config_dir.py`**：Sharded GitOps 組裝工具 — 多來源 conf.d/ 合併、SHA-256 衝突偵測、assembly manifest
- **`da_assembler.py`**：ThresholdConfig CRD → YAML 輕量 controller（Watch / One-shot / 離線渲染 / Dry-run）
- **ThresholdConfig CRD**（`dynamicalerting.io/v1alpha1`）：namespace-scoped + RBAC + printer columns

**DX 工具迭代**
- `validate_all.py`：`--profile` + `--watch`（CSV timing trend）、`--smart`（git diff → affected-check 自動跳過）
- `bump_docs.py`：`--what-if`（全 238 rules 審計）
- `generate_cheat_sheet.py` / `generate_rule_pack_stats.py`：`--lang zh/en/all` 雙語
- `check_doc_freshness.py`：false-positive 修正 + `--fix`
- `check_translation.py`：cross-dir + lang fix
- `check_includes_sync.py`：`--fix`（自動建立缺失 .en.md stub）

### 🎯 互動工具生態（0 → 24 JSX tools）

**工具矩陣**：23 個位於 `docs/interactive/tools/` + 1 個 `docs/getting-started/wizard.jsx`
- Config：Playground、Lint、Diff、Schema Explorer、Template Gallery
- Rule Pack：Selector、Matrix、Detail、PromQL Tester
- 運維：Alert Simulator/Timeline、Health Dashboard、Capacity Planner、Threshold Calculator
- 學習：Architecture Quiz、Glossary、Dependency Graph、Runbook Viewer、Onboarding Checklist
- 展示：Platform Demo、Migration Simulator、CLI Playground、Self-Service Portal

**基礎設施**
- **tool-registry.yaml**（單一真相源）→ `sync_tool_registry.py`（`make sync-tools`）自動同步 Hub 卡片 + TOOL_META + JSX frontmatter
- **platform-data.json**（共用資料源）：從 Rule Pack YAML 萃取（15 packs, 139R + 99A），JSX 工具 fetch 共用
- **jsx-loader.html**：瀏覽器端 JSX transpiler + `TOOL_META`（related footer）+ `__PLATFORM_DATA` 預載 + Guided Flow 模式
- **tool-consistency-check**（pre-commit）：Registry ↔ Hub ↔ TOOL_META ↔ JSX ↔ MD 一致性驗證

**Guided Flows**
- `flows.json` 多步引導流程（onboarding / tenant-setup / alert-deep-dive），`?flow=onboarding` 啟動
- Cross-step data（`__FLOW_STATE` + sessionStorage）、progress persistence、completion tracking
- Conditional steps + checkpoint validation（`__checkFlowGate()` Next 按鈕閘門）
- Custom flow builder：`?flow=custom&tools=...` Hub 互動式 builder，24 工具全覆蓋
- Flow analytics：進度條、完成率、drop-off 步驟偵測

### 🌐 Bilingual Annotations (i18n)

- **Rule Pack 雙語 annotation**：`summary_zh` / `description_zh` / `platform_summary_zh` — 三個 Pilot Pack（MariaDB, PostgreSQL, Kubernetes）
- **Alertmanager template fallback**：Go `or` function 優先中文、自動 fallback 英文（所有 receiver 類型）
- **CLI i18n**：`detect_cli_lang()` 偵測 `DA_LANG`/`LANG` → argparse help 雙語切換（23 個 CLI 命令）
- **check_bilingual_annotations.py**：Rule Pack 雙語覆蓋率驗證（pre-commit manual stage）

### 📄 文件全面重構

**結構重組**
- architecture-and-design.md 拆分為 6 個專題文件（benchmarks / governance-security / troubleshooting / migration-engine / federation-integration / byo-prometheus-integration）
- 3 個角色入門指南（for-platform-engineers / for-domain-experts / for-tenants）zh/en
- 全面雙語化：33 → 46 對 `.en.md` 文件
- MkDocs Material 站點：CJK 搜尋、tags、i18n 切換、abbreviation tooltips
- Glossary（30+ 術語）+ 5 ADRs + JSON Schema（VS Code 自動補全）

**內容修訂**
- 根 README (zh/en) 重寫：角色導向痛點敘事（Platform / Tenant / Domain / Enterprise）
- architecture-and-design.en.md：補 §2.3 Tenant-Namespace Mapping、修 §3.1（15 packs + `prometheus-rules-*` 命名）、補 Bilingual Annotations
- Benchmarks 重寫：5 輪實測數據統一採集（idle + under-load + routing + alertmanager + reload）
- 6 份文件精簡（avg -23%）：移除過時內容、手動 curl 改為 da-tools CLI 引用
- Scenario CLI 修正：`tenant-lifecycle.md` (zh/en) 修正 4 個不存在的 CLI flags
- Tool-map 重生成：62 個工具完整覆蓋（之前僅 18 個）

**文件 CI 工具鏈（13 tools）**
- `validate_mermaid.py` / `check_doc_links.py` / `check_doc_freshness.py` / `doc_coverage.py`
- `add_frontmatter.py` / `doc_impact.py` / `check_translation.py` / `check_includes_sync.py`
- `sync_glossary_abbr.py` / `sync_schema.py` / `generate_cheat_sheet.py` / `inject_related_docs.py`
- `validate_all.py`：統一驗證入口

### 🔒 Security Audit & Hardening

- **程式碼安全**：ReDoS 防護（regex 長度限制）、URL 注入白名單、SSRF scheme 白名單（http/https only）、Prototype pollution 過濾（`__proto__`/`constructor`）、YAML 100KB 上限、`os.chmod` 補齊
- **文件安全加固**：HTTP→HTTPS 範例、webhook 驗證升為 error、`--web.enable-lifecycle` 安全註解、Grafana 密碼警告、新增「生產環境安全加固」章節

### 🏗️ 專案結構正規化

- **scripts/tools/ 三層子目錄化**：62 個工具分入 `ops/`（30）、`dx/`（18）、`lint/`（13）+ root（1 + 1 lib）
  * Docker flat layout 相容（dual sys.path + build.sh 自動 strip）
- **JSX 工具搬遷**：22 個工具 `docs/` → `docs/interactive/tools/`，registry/flows/loader/hub 路徑同步
- **測試歸位**：`test_assemble_config_dir.py`、`test_da_assembler.py`、`test_flows_e2e.py` 統一搬入 `tests/`
- **generate_tool_map.py 重寫**：自動掃描 ops/dx/lint/root 子目錄

### 🧪 測試工程化（14 輪系統化重構）

| 項目 | v1.11.0 | v2.0.0 | 變化 |
|------|---------|--------|------|
| 測試檔案 | 5 | 40 | +35 |
| 測試數量 | ~790 | 1,759 | +969 |
| Go 測試 | 97 | 110 | +13 |
| Coverage gate | 無 | 64%（`setup.cfg`） | NEW |
| Test markers | 無 | 5（slow/integration/benchmark/regression/snapshot） | NEW |
| Factories | 無 | 12（`factories.py` + `PipelineBuilder`） | NEW |

**關鍵里程碑**：
- Wave 5-6：pytest 遷移、SAST 掃描器（189 rules）、整合測試
- Wave 7-8：property-based tests（Hypothesis）、snapshot tests（18 JSON）、coverage gate
- Wave 9-10：factories 拆分、domain policy、deepdiff structured diff
- Wave 11-12：unittest→pytest batch migration、metric_dictionary fixture
- Wave 13：conftest re-export cleanup、duplicate removal、factory docstrings
- Wave 14-16：parametrize、scaffold snapshots、benchmark baseline、validate_all coverage
- Wave 17：coverage attack — baseline_discovery（31→55%）、backtest_threshold（32→70%）、batch_diagnose（49→71%）
- Wave 18：parametrize sweep — 合併重複測試方法

### 🛡️ 品質閘門

- **Pre-commit hooks**：0 → 12 個 auto-run + 5 個 manual-stage（schema / translation / flow E2E / jsx-babel / i18n coverage）
- **新增 hooks**：`tool-map-check`、`doc-map-check`、`rule-pack-stats-check`、`glossary-check`、`changelog-lint`、`version-consistency`、`includes-sync`、`platform-data-check`、`repo-name-check`、`tool-consistency-check`、`structure-check`、`doc-links-check`
- **Docker CI 修正**：build.sh 自動 strip sys.path hack + 觸發路徑 `**/*.py` + 3 個遺漏工具打包修正
- **Conventional Commits** + `generate_changelog.py` 自動化

### 📦 Dependency Upgrades

- **Prometheus**: v2.53.0 → v3.10.0（PromQL 相容性已驗證，15 個 Rule Pack 無影響）
- **Alertmanager**: v0.27.0 → v0.31.1
- **configmap-reload**: v0.14.0 → v0.15.0
- **Grafana**: 11.1.0 → 12.4.1
- **kube-state-metrics**: v2.10.0 → v2.18.0
- **Go**: 1.22 → 1.26.1（go.mod + Dockerfile + CI）
- **Frontend CDN**: React 18.2.0 → 18.3.1、Babel 7.23.9 → 7.26.4、Lucide 0.383.0 → 0.436.0

### 📊 Numbers

| 項目 | v1.11.0 | v2.0.0 | 變化 |
|------|---------|--------|------|
| Rule Packs | 13 | 15 | +2 |
| Python 工具 | ~20 | 62 | +42 |
| da-tools CLI 命令 | 20 | 23 | +3 |
| JSX 互動工具 | 0 | 24 | +24 |
| 文件（docs/ .md） | ~20 | 68 | +48 |
| 雙語文件對 | 0 | 46 | +46 |
| Python 測試 | ~790 | 1,759 | +969 |
| 測試檔案 | 5 | 40 | +35 |
| Pre-commit hooks | 0 | 12 + 5 manual | +17 |
| Docker images | 2 | 3 | +1 (da-portal) |

### 📈 Benchmark（v2.0.0，15 Rule Packs，Kind 叢集）

**Idle-State（2 tenant，237 rules，43 rule groups）：**

| 指標 | v1.11.0 (13 packs) | v2.0.0 (15 packs) | 變化 |
|------|-------|-------|------|
| Total Rules | 141 | 237 | +96 |
| Rule Groups | 27 | 43 | +16 |
| Eval Time / Cycle | 20.3ms | 23.2ms | +2.9ms |
| p50 per-group | 1.23ms | 0.39ms | 改善 |
| p99 per-group | 6.89ms | 4.89ms | 改善 |
| Prometheus CPU | 0.014 cores | 0.004 cores | — |
| Prometheus Memory | 142.7MB | 112.6MB | — |
| Exporter Heap (×2 HA) | 2.4MB | 2.2MB | — |
| Active Series | ~6,037 | 6,239 | +202 |

**Go Micro-Benchmark（Intel Core 7 240H，`-count=5` median）：**

| Benchmark | ns/op (median) | B/op | allocs/op |
|-----------|------:|-----:|----------:|
| Resolve_10Tenants_Scalar | 12,209 | 26,488 | 61 |
| Resolve_100Tenants_Scalar | 100,400 | 202,777 | 520 |
| Resolve_1000Tenants_Scalar | 1,951,206 | 3,848,574 | 5,039 |
| ResolveAt_10Tenants_Mixed | 34,048 | 40,052 | 271 |
| ResolveAt_100Tenants_Mixed | 405,797 | 462,636 | 2,622 |
| ResolveAt_1000Tenants_Mixed | 5,337,575 | 5,258,548 | 26,056 |
| ResolveAt_NightWindow_1000 | 5,404,213 | 5,223,925 | 25,056 |
| ResolveSilentModes_1000 | 86,700 | 186,086 | 10 |

**Route Generation Scaling（Python `generate_alertmanager_routes.py`）：**

| Tenants | Wall Time | Routes | Inhibit Rules |
|---------|-----------|--------|---------------|
| 2 | 181ms | 3 | 2 |
| 10 | 196ms | 8 | 10 |
| 50 | 248ms | 41 | 50 |
| 100 | 327ms | 80 | 100 |

---

> **歷史版本 (v0.1.0–v1.11.0)：** 詳見 [`CHANGELOG-archive.md`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/CHANGELOG-archive.md)
