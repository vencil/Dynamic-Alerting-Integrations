---
title: "Pint Lint Baseline — Prometheus rule static-analysis"
tags: [internal, lint, prometheus, rule-packs, ci]
audience: [platform-engineers, sre, contributors]
version: v2.9.0
lang: zh
status: active
domain: observability
created_at: 2026-06-15
updated_at: 2026-06-15
---

# Pint Lint Baseline — Prometheus rule static-analysis

[pint](https://cloudflare.github.io/pint/)（Cloudflare OSS Prometheus rule linter）的 Vibe **操作基線**（live config 的 policy / scope / exemptions）。引擎 pint + thin wrapper（`scripts/tools/lint/check_pint.py`），config 在 repo root `.pint.hcl`，對齊 [hybrid lint policy](lint-policy.md)（adopt OSS engine, don't DIY）。

> 採用評估 / marginal-value 論證（為何 pint additive、與 canary 的對比）見 **PR #843 描述**——那是決策快照、不在 repo 永久文件裡 rot。本文只記 operative residue。

唯一 hard-gate 的高 ROI check = **`alerts/template`**：機械化攔「expr 的聚合砍掉某 label，但 alert template 用了它 → `and` 變空 → 告警**永遠靜默不觸發**」（本 repo 燒過 5×，至今只靠手寫註解守）。

## Severity → Action 對照（SSOT）

| Severity | Action | 來源 |
|---|---|---|
| **Bug / Fatal** | **BLOCK PR**（CI job `Lint Rule Packs`，`check_pint.py --ci`，無 escape） | `alerts/template` label-flow 違反 + pint parse/syntax error |
| **Warning** | 視 `.pint.hcl` 而定（目前 idiom-noisy checks 已 disabled，見下） | — |
| **Information** | **非阻擋**（不列管） | pint 提示 |

> hard-gate 由 `check_pint.py` 轉發 pint 的 exit code（pint 預設 Bug 以上 → 非 0）。severity/exemption policy **原生**寫在 `.pint.hcl`（中央 audited registry），wrapper 不 re-parse pint 輸出（thin，不重造引擎已有的能力）。

## Scope / engine

- **掃描物**（`.pint.hcl` 的 `parser.include` 與 `check_pint.py` 的 `_PINT_ARGS` **兩邊都要有**——pint 只走 CLI 交給它的目錄，只加 include 是 dead config，見下方 tripwire）：
  - `rule-packs/rule-pack-*.yaml` — 元件 rule-pack **canonical source**（含 `rule-pack-kubernetes.yaml`，5 處手寫 topology-trap 註解其中 3 處在此）。
  - `tests/rulepacks/*.rules.yaml` — 平台自監控 pack 的 **extract**。這把 **ADR-025 guardian**（`Watchdog` + `AlertmanagerWebhookNotificationsFailing`）+ SSE-reconnect sentinel 納入 gate——「守護者不該裸奔在靜態分析網外」。同目錄的 `*_test.yaml` promtool spec 非 rule 檔，被 `parser.include` 略過。
  - `k8s/03-monitoring/configmap-rules-platform*.y(a)ml` — **實際部署的**平台自監控 pack。**原「ConfigMap-wrapped → pint 無法 parse、extract 是唯一可掃形式」的說法已作廢**：pint 自 **v0.19.0** 起可解析 ConfigMap `data:` 內嵌規則，前提是該路徑列進 `parser.relaxed`（repo pin 0.86.0）。`relaxed` 是 **per-path regex list 不是全域 boolean**，且**不可省**——只加 `include` 不加 `relaxed`，pint 會把整份 ConfigMap 當成畸形 rule 文件而報 **Fatal**（實測 entries=352 + CI 紅）。
- **只納 platform 前綴**，其餘 16 份 `configmap-rules-*.yaml` 不納。判準是機械的：那 16 份都由 `generate_rulepack_configmaps.py` 從已被掃的 `rule-packs/rule-pack-*.yaml` 生成並帶 `DO NOT EDIT` banner，`configmap-rules-platform.yaml` 是**唯一手寫**的一份（`grep -L "DO NOT EDIT" k8s/03-monitoring/configmap-rules-*.yaml` 正好只回它）。實測全納 17 份 → **689 entries（vs 393，+75%）且每個 finding 報兩次**（同一條 `alerts/for` 同時落在 `rule-packs/rule-pack-mariadb.yaml` 與 `k8s/03-monitoring/configmap-rules-mariadb.yaml`），等於一半輸出把開發者指向「不准編輯」的生成檔。副本一致性照舊由 `check_rulepack_sync.py` 守；`operator-manifests/` 同理不納。
- **用 prefix + 雙副檔名（`.ya?ml`）不用精確檔名**：與 pytest 側 `_PLATFORM_CM_PREFIX` / `_RULES_FILE_EXTS`（`tests/ops/test_generate_routes_orchestration.py`）同一判準，兩邊的 floor 都寫成「跨所有平台 rules ConfigMap 的**總和**」，所以拆檔仍成立。精確檔名會讓未來的 `configmap-rules-platform-federation.yaml` 對 pint 隱形、pytest 卻照掃（實測：prefix 型 394 entries，精確檔名型 393）。pint 的 regexp **全部 auto-anchored**（`X` 當 `^X$`），無須自己寫 `^...$`。
- **`--offline`**（CI 無需 Prometheus；跳過 `promql/series` / `promql/cost`）。引擎：CI 裝 pint binary（release tag `v0.86.0`），`check_pint.py` fallback docker tag `0.86.0`；兩者版本一致由 `tests/lint/test_check_pint.py` 守。
- **供應鏈**：CI 的 pint binary 下載在 install 前先過 `scripts/ops/_verify_download.sh` 比對 pinned SHA-256（與 promtool / hadolint / kube-linter 同一把關），mismatch 即 fail，再落 docker fallback。docker fallback image 亦已 **digest-pin**（`check_pint.py` `PINT_IMAGE` = `…:0.86.0@sha256:…`，multi-arch index digest），與 kube-linter / helm / hadolint fallback 同步 pin（供應鏈 sweep Part 2，#849 follow-up）。

## Consolidated baseline（against `main`）— Bug/Warning = **0** 必須

| 引擎 | scope | Bug/Fatal | Warning | Information |
|---|---|---:|---:|---:|
| pint (`alerts/template`) | rule-packs/ + tests/rulepacks/ extracts + platform ConfigMap（**393 entries**；納入 ConfigMap 前為 351） | **0** | **0** | 8（`alerts/for`，非阻擋） |

> 納入平台 ConfigMap 對**既有 351 entries 的影響為 0**：pint 的 problem 輸出 before/after **完全相同**，只有 entries 計數與 log header 變。新增的 2 筆 Information 都來自 ConfigMap 本身（`alerts/for`，低於 `--min-severity=warning` 不顯示、也低於 `--fail-on=bug` 不阻擋）。

> `check_pint.py` 的 **entry-count tripwire 已從「`entries == 0` 才紅」升級為下界 `_MIN_PINT_ENTRIES`（現值 385）**。理由：實測到的 silent-green **全都是健康的非零值配 exit 0**——(a) `parser.include` 加了 ConfigMap 但 `_PINT_ARGS` 沒加 `k8s/03-monitoring/` → entries=351（應為 393）、exit 0；(b) `configmap-rules-platform.yaml` 被改名/搬走 → 一樣 entries=351、exit 0。下界取值窗口：**必須 >351**（掉整個平台 pack 正是這個守衛要抓的 −42），**必須 <393**（entries 只會隨新增規則變大，低於今值才不會每加一條規則就要改數字）；385 留 34 的餘裕與 8 條刪除的鬆弛。形狀對齊 `tests/ops/test_generate_routes_orchestration.py` 的 `_MIN_PLATFORM_ALERTS`：**單一份**手維護的 floor，只往上調。
>
> 這個下界也是 **pint 版本 bump 後偵測解析行為改變的唯一機械手段**——`parser.relaxed` 是脆弱點，若未來版本不再套用它，平台 pack 靜靜貢獻 0 entries、其餘規則照樣掃綠，CI 不會有別的訊號；只有計數掉回 351 會紅。**每次動 `PINT_VERSION` 都要重量一次。**
>
> 同時修掉一個 **dead-code bug**：pint **無條件**上色（不偵測 TTY），wrapper 用 `capture_output=True` 收到的是 `\x1b[2mentries=\x1b[0m\x1b[94m393\x1b[0m`，原本的 `entries=(\d+)` **永遠 match 不到** → 舊的 `entries == 0` 守衛在 CI 其實從未生效。現在先剝 ANSI SGR 再比對，且 `--ci` 下**讀不到 `entries=` 即 fail**（不再靜默跳過守衛）。

## Disabled checks（idiom false-positives，非 bug）

下列 default checks 對本 repo 的**既有 intentional 慣用法**全是 false-positive，於 `.pint.hcl` match-all `disable`：

| Check | 為何 disable |
|---|---|
| `alerts/comparison` | 把 `absent()`-based `*ExporterAbsent` / sentinel 告警判成「always firing」——它們本來就是（by design） |
| `promql/impossible` | 把刻意的 `... or vector(0)` 空向量防護判成 dead code |
| `rule/dependency` | 把刻意拆分的 recording-rule group ↔ alerting-rule group 判成跨群依賴 |

> **覆蓋 trade-off（defer-with-trigger）**：這三項是 **match-all（全域）disable**，簡單但也犧牲了對**非 idiom** 新規則的覆蓋——`promql/impossible` 全關 → 真的「永遠不觸發」的新告警（dead-code label 不匹配，或荒謬的 `{phase="Running", phase="Failed"}` 同 label 兩值）會溜過；`alerts/comparison` 全關 → 真的漏寫比較運算子的 always-firing 新告警會溜過。MVP 接受（idiom FP 是真的、killer value 是 `alerts/template`）。**觸發**：若上述非 idiom bug 實際漏網，改用 `match`-scoped disable（只對 sentinel/recording-rule 名稱關）或在用到 `or vector(0)` 那幾行加 inline `# pint disable promql/impossible`，以收回 ordinary 規則的覆蓋。

## Exemptions（`alerts/template`）— 中央 registry 在 `.pint.hcl`

| Match（rule name `(.+ExporterAbsent\|VersionAwareThresholdInert)`） | Rationale |
|---|---|
| 名稱**結尾**為 `ExporterAbsent` 的 sentinel，或正好是 `VersionAwareThresholdInert` | platform-scoped sentinels：expr 刻意把 `tenant` 聚合掉（告警是平台級、render 空 → drop），但 repo 的 required-labels policy（`lint_custom_rules.py`）**強制**每條規則帶 `tenant` label → 此處「template 用了 query 不會有的 label」**是 by design 非 bug**。新規則若真的砍掉它需要的 label，name 不 match → **照樣被抓**。 |

> pint **auto-anchors** 每個 `match.name`（`X` 解析為 `^X$`，[pint docs](https://cloudflare.github.io/pint/configuration.html)），故 `.+ExporterAbsent` 已是「**結尾**為 ExporterAbsent」、僅**含**該子字串的名稱（如 `FooExporterAbsentButBuggy`）**不**豁免（對抗式 probe 實證會被抓）。不需顯式 `^...$`。
>
> 編輯既有 intentional-pattern 規則導致 pint flag 時：在 `.pint.hcl` 擴充 match，或對該規則加 inline `# pint disable alerts/template` 註解 + rationale。**勿** disable 整個 check。

## 平台 pack 盲區（已關閉）

**原盲區**：平台 pack 只有「有 promtool 測試的」才有 extract，其餘對 pint 隱形。**已由 `parser.relaxed` + 直掃 `configmap-rules-platform*.y(a)ml` 關閉**——現在掃的是**實際部署的那份**，不再依賴 extract 的覆蓋率。

現況（數字用 parse 數出、非 grep；改動平台 pack 後請重數）：

| 量測 | 值 |
|---|---:|
| `configmap-rules-platform.yaml` 全 pack 告警 | **42** |
| `tests/rulepacks/platform-*.rules.yaml` extract | 12 檔 / 涵蓋 **22** 條 |
| 全部 `tests/rulepacks/**/*.rules.yaml` 涵蓋的 CM 告警 | **26** |
| 納入前 **pint 完全看不到**的 CM 告警 | **16** |
| 其中真正吃到 `alerts/template` 保護的 | **6** |

那 6 條（有 aggregation 砍 label + template 用 `{{ $labels.X }}`，故 `alerts/template` 有東西可驗）：`ConfigBlastRadiusHighImpact` / `FederationAuditPipelineSilent` / `FederationGatewayBackendErrors` / `FederationRejectionRateAnomaly` / `ThresholdExporterTooFewReplicas` / `VectorBufferEventsDropped`。量法：把 CM 全部 42 條的 `summary` 注入一個 ghost label 跑 `pint lint -s`，數 `alerts/template` 命中 17 條，扣掉已有 extract 的 11 條 → 淨新增 6。其餘 10 條（如 `ConfigParseFailure`）expr 不做剝 label 的聚合，`alerts/template` 對它們無話可說——**納入 ≠ 每條都有保護**，這是誠實的分母。

**未涵蓋（deliberate）**：`alerts/annotation`（runbook_url 存在性）**不加**。實測 pint 驗不了「URL 指到的檔案存在」（把 runbook_url 改成不存在的檔案 → pint 綠、pytest 紅），而 `.pint.hcl` 的 `ignore{name}` 會變成第二份沒有 exit-lock 的豁免清單。這層留在 `tests/ops/test_generate_routes_orchestration.py::TestPlatformRunbookCoverageContract`。

## 關聯

- `.pint.hcl`（repo root）— pint config（exemption 中央 registry）
- [`lint-policy.md`](lint-policy.md) — lint class / bypass / allowlist 治理
- ADR-025 deferred「規則靜態檢查」項；Watchdog / canary 同 epic 的姊妹項
