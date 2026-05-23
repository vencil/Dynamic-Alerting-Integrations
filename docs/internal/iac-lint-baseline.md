---
title: "IaC Lint Baseline — Container/k8s SAST residual findings"
tags: [internal, lint, security, iac, ci]
audience: [contributors, maintainers]
version: v2.8.1
lang: zh
---

# IaC Lint Baseline — Container/k8s SAST 殘留 findings

> 本文件登記 [epic #448](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/448) Container/k8s IaC SAST 各層的**非阻擋（WARN / High）findings** + rationale + 預計修補時程。
>
> **為什麼需要 baseline 表**：SAST 嚴格度分兩級——**Critical → BLOCK PR**（必須 0）；**High / WARN → 不擋 merge，但須列管**（AC 7「任何 High 都需在 iac-lint-baseline.md 列入 + rationale」）。沒有 baseline 表，warning 會無限累積成無人看的雜訊；列管後每筆都有 owner 與退場條件。
>
> consolidated 全 4 層版本由 [TRK-315](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/596) 收斂。本檔隨各層 PR 增補。

## Severity → action 對照（Layer 1 採 BLOCK/WARN reduction）

| Wrapper action | 來源 | 行為 |
|---|---|---|
| **BLOCK** | hadolint `error` level、Vibe 規則 V0/V1/V2/V3 違反 | 擋 commit / PR（須 0；epic AC 7 Critical=0 必須）|
| **WARN** | hadolint `warning` level | 記入本表（High），不擋 merge |
| **INFO** | hadolint `info` / `style` level | 僅 log，不列管 |

> 全 4 層共用的 Critical/High/Medium/Low → BLOCK/WARN/INFO 統一表在 [TRK-314](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/595) 收斂；Layer 1 先用上述 reduction。

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

## Layer 2 — Helm template（TRK-312）

_待 TRK-312 落地後增補（kube-linter dual-mode + trivy config baseline）。_

## Layer 3 — Helm values secret-shape（TRK-313）

_待 TRK-313 落地後增補。_

## Layer 4 — k8s raw manifest（TRK-314）

_目前 repo 無獨立 raw k8s manifest（都走 Helm）→ stub，無 baseline。_

## 關聯

- [epic #448](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/448) — Container/k8s SAST 4-layer
- [`lint-policy.md`](lint-policy.md) — lint class / bypass / allowlist 治理
- [`dev-rules.md` §安全紀律](dev-rules.md) — IaC SAST 起手 pointer
- `.hadolint.yaml`（repo root）— Layer 1 engine config
