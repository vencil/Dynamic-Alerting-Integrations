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

[pint](https://cloudflare.github.io/pint/)（Cloudflare OSS Prometheus rule linter）的 Vibe 採用基線。**ADR-025 deferred 項**（規則靜態檢查）。引擎 pint + thin wrapper（`scripts/tools/lint/check_pint.py`），config 在 repo root `.pint.hcl`，對齊 [hybrid lint policy](lint-policy.md)（adopt OSS engine, don't DIY）。

## 為什麼採用（marginal value）

`lint_custom_rules.py` 是 **policy/governance**（denied funcs / range caps / required tenant）；**零**現存機制檢查 PromQL **正確性**。本 repo 最常被燒的類別是 **「bare aggregation 把 topology labels 砍掉 → `and` 變空 → 告警永遠靜默不觸發」**（rule-pack-kubernetes.yaml:40/167/347、rate.yaml:18 等，至今只靠**手寫註解 + 1 個 regression test** 守）。pint 的 **`alerts/template`** check 把這個 hazard class **機械化**——expr 的聚合砍掉某 label，但 alert template 用了它 → flag。這是 pint 的 killer value，也是本 gate 唯一 hard-gate 的 check。

## Severity → Action 對照（SSOT）

| Severity | Action | 來源 |
|---|---|---|
| **Bug / Fatal** | **BLOCK PR**（CI job `Lint Rule Packs`，`check_pint.py --ci`，無 escape） | `alerts/template` label-flow 違反 + pint parse/syntax error |
| **Warning** | 視 `.pint.hcl` 而定（目前 idiom-noisy checks 已 disabled，見下） | — |
| **Information** | **非阻擋**（不列管） | pint 提示 |

> hard-gate 由 `check_pint.py` 轉發 pint 的 exit code（pint 預設 Bug 以上 → 非 0）。severity/exemption policy **原生**寫在 `.pint.hcl`（中央 audited registry），wrapper 不 re-parse pint 輸出（thin，不重造引擎已有的能力）。

## Scope / engine

- **掃描物**：`rule-packs/rule-pack-*.yaml`（**canonical source**）。k8s ConfigMap 副本 + `operator-manifests/` 為 ConfigMap-wrapped，pint 無法 parse；三份副本一致性由 `check_rulepack_sync.py` 守。
- **`--offline`**：跳過需 live Prometheus 的 checks（`promql/series` / `promql/cost`）→ CI 無需 Prometheus。
- 引擎取得：CI 裝 pint binary（`v0.86.0`，curl release），`check_pint.py` fallback 到 docker image `ghcr.io/cloudflare/pint:0.86.0`。版本同步點：`check_pint.py` `PINT_VERSION` ↔ `.github/workflows/ci.yml` 的 `PINT=`。

## Consolidated baseline（against `main`，2026-06-15）— Bug/Warning = **0** 必須

| 引擎 | scope | Bug/Fatal | Warning | Information |
|---|---|---:|---:|---:|
| pint (`alerts/template`) | `rule-packs/rule-pack-*.yaml` | **0** | **0** | 3（非阻擋） |

## Disabled checks（idiom false-positives，非 bug）

下列 default checks 對本 repo 的**既有 intentional 慣用法**全是 false-positive，於 `.pint.hcl` match-all `disable`：

| Check | 為何 disable |
|---|---|
| `alerts/comparison` | 把 `absent()`-based `*ExporterAbsent` / sentinel 告警判成「always firing」——它們本來就是（by design） |
| `promql/impossible` | 把刻意的 `... or vector(0)` 空向量防護判成 dead code |
| `rule/dependency` | 把刻意拆分的 recording-rule group ↔ alerting-rule group 判成跨群依賴 |

> **覆蓋 trade-off（defer-with-trigger）**：這三項是 **match-all（全域）disable**,簡單但也犧牲了對**非 idiom** 新規則的覆蓋——例如 `promql/impossible` 全關 → 一條真的「永遠不觸發」的新告警（dead-code label 不匹配）會溜過、`alerts/comparison` 全關 → 真的漏寫比較運算子的 always-firing 新告警會溜過。MVP 接受（idiom FP 是真的、killer value 是 `alerts/template`）。**觸發**:若上述兩類非 idiom bug 實際漏網,改用 `match`-scoped disable（只對 sentinel/recording-rule 名稱關）以收回 ordinary 規則的覆蓋。

## Exemptions（`alerts/template`）— 中央 registry 在 `.pint.hcl`

| Match（rule name `(.+ExporterAbsent\|VersionAwareThresholdInert)`） | Rationale |
|---|---|
| 名稱**結尾**為 `ExporterAbsent` 的 sentinel，或正好是 `VersionAwareThresholdInert` | platform-scoped sentinels：expr 刻意把 `tenant` 聚合掉（告警是平台級、render 空 → drop），但 repo 的 required-labels policy（`lint_custom_rules.py`）**強制**每條規則帶 `tenant` label → 此處「template 用了 query 不會有的 label」**是 by design 非 bug**。新規則若真的砍掉它需要的 label，name 不 match → **照樣被抓**。 |

> pint **auto-anchors** 每個 `match.name`（`X` 解析為 `^X$`，[pint docs](https://cloudflare.github.io/pint/configuration.html)），故 `.+ExporterAbsent` 已是「**結尾**為 ExporterAbsent」、僅**含**該子字串的名稱（如 `FooExporterAbsentButBuggy`）**不**豁免（對抗式 probe 實證會被抓）。不需顯式 `^...$`。

## ⚠️ Scope 盲區（platform self-monitoring rules 未涵蓋；defer-with-trigger）

本 gate 掃 **`rule-packs/rule-pack-*.yaml`（15 個元件 pack，含 `rule-pack-kubernetes.yaml`——5 處手寫 topology-trap 註解其中 3 處在此**，已涵蓋）。**但 `k8s/03-monitoring/configmap-rules-platform.yaml` 的 ~20 條平台自監控告警（`ThresholdExporterAbsent` / `DefaultsTruncationStorm`〔`count without(tenant)`〕/ `Watchdog` / `AlertmanagerWebhookNotificationsFailing` 等）NOT 涵蓋**——它是 ConfigMap-wrapped（pint 無法 parse）、且**無 `rule-pack-platform.yaml` canonical source**，連 promtool 用的 extract（`tests/rulepacks/platform-*.rules.yaml`）也被 `parser.include` 排除。諷刺的是 `DefaultsTruncationStorm` 的註解正引用 #651「bare-aggregation strips topology」教訓，卻不在 gate 內。
**Defer-with-trigger**:擴 `parser.include` + scan `tests/rulepacks/platform-*.rules.yaml`(extract)涵蓋平台 pack——觸發:平台 pack 新增 aggregation 類告警時,或首次有平台規則 topology-trap 漏網。需連帶豁免平台 sentinels（同 `*ExporterAbsent` 模式）。

> 編輯既有 intentional-pattern 規則導致 pint flag 時：在 `.pint.hcl` 擴充 match，或對該規則加 inline `# pint disable alerts/template` 註解 + rationale。**勿** disable 整個 check。

## 關聯

- `.pint.hcl`（repo root）— pint config（exemption 中央 registry）
- [`lint-policy.md`](lint-policy.md) — lint class / bypass / allowlist 治理
- ADR-025 deferred「規則靜態檢查」項；Watchdog/canary 同 epic 的姊妹項
