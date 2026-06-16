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

- **掃描物**：
  - `rule-packs/rule-pack-*.yaml` — 元件 rule-pack **canonical source**（含 `rule-pack-kubernetes.yaml`，5 處手寫 topology-trap 註解其中 3 處在此）。
  - `tests/rulepacks/*.rules.yaml` — 平台自監控 pack 的 **extract**（`configmap-rules-platform.yaml` 為 ConfigMap-wrapped、pint 無法 parse；extract 是唯一可掃形式）。這把 **ADR-025 guardian**（`Watchdog` + `AlertmanagerWebhookNotificationsFailing`）+ SSE-reconnect sentinel 納入 gate——「守護者不該裸奔在靜態分析網外」。同目錄的 `*_test.yaml` promtool spec 非 rule 檔，被 `parser.include` 略過。
- k8s ConfigMap 副本 + `operator-manifests/` 為 ConfigMap-wrapped → 不掃；三份副本一致性由 `check_rulepack_sync.py` 守。
- **`--offline`**（CI 無需 Prometheus；跳過 `promql/series` / `promql/cost`）。引擎：CI 裝 pint binary（release tag `v0.86.0`），`check_pint.py` fallback docker tag `0.86.0`；兩者版本一致由 `tests/lint/test_check_pint.py` 守。
- **供應鏈**：CI 的 pint binary 下載在 install 前先過 `scripts/ops/_verify_download.sh` 比對 pinned SHA-256（與 promtool / hadolint / kube-linter 同一把關），mismatch 即 fail，再落 docker fallback。docker fallback 仍以 mutable tag 拉取（digest-pin 為後續項）。

## Consolidated baseline（against `main`）— Bug/Warning = **0** 必須

| 引擎 | scope | Bug/Fatal | Warning | Information |
|---|---|---:|---:|---:|
| pint (`alerts/template`) | rule-packs/ + tests/rulepacks/ extracts（263 entries） | **0** | **0** | 3（非阻擋） |

> `check_pint.py` 另有 **entry-count tripwire**：`--ci` 下若 pint 掃到 0 entries（`parser.include` 改名/typo 配不到檔）→ **fail loud**，避免 gate 靜默失效卻 CI 噴綠。

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

## 殘留 scope 盲區（defer-with-trigger）

平台 pack 的 **extract** 已納入（Watchdog 等），但只有**有 promtool 測試的 2 條**（sse-reconnect / watchdog）有 extract；`configmap-rules-platform.yaml` 其餘 ~18 條（`DefaultsTruncationStorm`〔`count without(tenant)`〕/ `ThresholdExporterAbsent` 等）**無 extract → 仍未涵蓋**。**Defer-with-trigger**：為其餘平台告警建 extract（或改用可掃的 platform rule-pack source）——觸發：平台 pack 新增 aggregation 類告警、或首次平台規則 topology-trap 漏網。連帶需豁免平台 sentinels（同 `*ExporterAbsent` 模式）。

## 關聯

- `.pint.hcl`（repo root）— pint config（exemption 中央 registry）
- [`lint-policy.md`](lint-policy.md) — lint class / bypass / allowlist 治理
- ADR-025 deferred「規則靜態檢查」項；Watchdog / canary 同 epic 的姊妹項
