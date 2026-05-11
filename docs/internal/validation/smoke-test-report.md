---
title: "I-4 Runbook Smoke Test Report"
tags: [validation, runbook, troubleshooting, smoke-test]
audience: [maintainers, sre]
version: v2.7.0
lang: zh
---

# I-4 Runbook Smoke Test Report

> **Scope**: 對 [`docs/integration/troubleshooting-checklist.md`](../../integration/troubleshooting-checklist.md) 內所有 `jq` / `amtool` / `promtool` / `yq` 命令做**離線語法驗證**，避免凌晨 on-call 拿到 typo 命令。
>
> **Tool**: [`scripts/tools/lint/smoke_test_i4_runbook.sh`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/scripts/tools/lint/smoke_test_i4_runbook.sh)（30 assertions across 19 entries）
>
> **Run cadence**: maintainer 在 I-4 內文 PR review 時手動跑一遍；未來可考慮加進 CI（需 dev container 裝 amtool / promtool / yq）

## Latest run

- **Date**: 2026-05-11
- **Result**: 30/30 PASS ✅
- **Tooling versions**: jq-1.6 / yq v4.40.5 / amtool 0.27.0 / promtool 2.50.0
- **Environment**: vibe-dev-container（Linux）

## Coverage matrix

依 §4.2 deploy-readiness review 的「right diagnostic metric」+「order matters」軸覆蓋：

| I-4 §  | 命令類別 | 測試方式 | Pass |
|---|---|---|---|
| §1.1.1 NetworkPolicy scrape | YAML(NetworkPolicy) | yq eval | ✅ |
| §1.2.1 Rule reload | jq(Prom runtimeinfo) | mock fixture | ✅ |
| §1.2.2 Shadow label removal | jq×2(Prom rules + AM alerts) | mock fixtures | ✅✅ |
| §1.3.1 AM matcher order | amtool config routes test | arg parse | ✅ |
| §1.3.2 Silencer drift | jq + amtool×2 | mock + arg parse | ✅✅✅ |
| §1.4.2 Prom queue_config | YAML | yq eval | ✅ |
| §1.4.4 Cardinality top-20 | jq×3 (length / group_by sort / keys) | mock Prom series | ✅✅✅ |
| §1.5.1 HA reload race | jq×2 (runtimeinfo + config) | mock fixtures | ✅✅ |
| §1.5.2 Dashboard UID drift | jq×4 (panels / datasources / meta.provisioned / .walk rewrite) | mock Grafana | ✅✅✅✅ |
| §1.6.1 Dual-write drift | jq(tsdb_head_series numeric) | mock Prom scalar | ✅ |
| §2.1.1 PromQL syntax | promtool check rules | wrap in synthetic rules YAML | ✅✅ |
| §2.1.2 Tenant violations | jq×2 (extract + length) | mock state.json | ✅✅ |
| §2.1.3 Orphan rule | jq×2 + amtool alert add | mock state + arg parse | ✅✅✅ |
| §2.3 Migration state | jq×4 (read schema / migrate / manifest / state-split) | mock state.json | ✅✅✅✅ |

**Total: 30 assertions, all passing.**

## What this catches

針對 Gemini SRE retrospective 提醒的「typo-prone areas」：

✅ **jq filter syntax**——所有 `.data.result[0].value[1]` / `group_by(.__name__) | map(...)` / `.[] | select(.name == "...")` 樣式都語法正確
✅ **amtool arg parsing**——`silence add` / `silence query` / `alert add` / `config routes test` 命令的 flag 名稱與位置 OK
✅ **promtool rule parsing**——`mysql_up == 0` / `rate(...)` 之類的 PromQL 通過 `promtool check rules` 驗證
✅ **yq YAML parsing**——`NetworkPolicy` ingress + `prometheus.yml` `queue_config` block 都是 valid YAML

## What this does NOT catch

離線驗證的本質限制——以下需要 live cluster 才能驗：

❌ **真實 API response shape drift**：若 Prom 升版改 `/api/v1/runtimeinfo` 的 JSON 結構，本 smoke test 仍會 pass（用的是 mock fixture）。Mitigation：每次 Prom 升版重抓 fixture。
❌ **AM 真實 silencer 操作的副作用**：`amtool silence add` 命令語法 OK，但實際在生產 AM 跑會建立 silence；smoke test 只驗 arg parsing。
❌ **kubectl exec 進到的 pod 行為**：`kubectl exec <pod> -- wget ...` 語法 OK，但實際 pod 是否有 `wget` / `curl` 取決於 image。Mitigation: I-4 entries 多處用 `wget -qO-` 因為 Prom / VM official image 都有。
❌ **跨命令 pipeline 邏輯**：smoke 只看單一命令；`A | B` 兩條都過，組合在一起的語意是否合理仍須人 review。

## 自動化 / CI integration

Smoke test 在 **CI 自動執行**（`.github/workflows/docs-ci.yaml` 的 `i4-runbook-smoke-test` job），觸發條件：

- `docs/**/*.md` 任何變動
- `scripts/tools/lint/**/*.sh` 任何變動
- `mkdocs.yml` 變動

**Tool versions pinned**（CI 與本地一致）：
- amtool: 0.27.0（prometheus/alertmanager release）
- promtool: 2.50.0（prometheus/prometheus release）
- yq: v4.40.5（mikefarah/yq release）
- jq: ubuntu-latest 內建（~1.6+）

**CI 成本**：~10s 工具下載 + ~5s smoke test = ~15s/PR（針對 docs PR 才觸發；不影響其他類 PR）

**為什麼自動化而非手動 cadence**：手動腳本一定 bit-rot——即使 churn 低，工具版本升級 / Prom API 結構變動 / 新 entry 加入時都需要重跑。依賴 maintainer 記憶等於押注未來不會出錯（[§4.2.1 self-review meta-lesson](../../integration/troubleshooting-checklist.md#421-self-review-是必要不充分important-meta-lesson) 已記錄這個失敗模式）。

### Future expansion

當 [issue #405](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/405) 工具 ship 後，smoke test 加入 assertions：

- `da-tools state reconcile`（替代 state-migrate + manifest-regenerate）— 跑在 mock state-dir 上驗 idempotency
- `da-tools silencer-drift-check --silences-file ...`（offline-first 設計）— 用 mock JSON 驗 drift detection
- `da-tools rule-pack-diff` — 兩個 rule-pack snapshot 跑 diff、驗 output format

## Source of truth

- Test script: [`scripts/tools/lint/smoke_test_i4_runbook.sh`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/scripts/tools/lint/smoke_test_i4_runbook.sh)
- I-4 source: [`docs/integration/troubleshooting-checklist.md`](../../integration/troubleshooting-checklist.md)
- Methodology origin: post-#377 retrospective Q2 + [I-4 §4.2 deploy-readiness review SOP](../../integration/troubleshooting-checklist.md#42-runbook-pr-review-sop-will-this-actually-deploy-at-3am)
