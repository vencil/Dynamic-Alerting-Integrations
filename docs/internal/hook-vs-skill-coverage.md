---
title: "Hook / Skill 邊界稽核矩陣"
tags: [internal, dx, governance, ai-agent]
audience: [ai-agents, maintainers]
version: v2.8.1
verified-at-version: v2.8.1
lang: zh
---

# Hook / Skill 邊界稽核矩陣（TRK-304）

> **用途**：盤點 Vibe 所有「品質閘門」的 **owner**——哪些由 hook 機械強制（AI 不必自己做）、哪些只是 skill / 文件層的 advisory（AI **必須自覺**做，否則沒人擋）、哪些根本無自動防線（漏接）。
>
> **為什麼需要這張表**：[#515](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/515) / [#522](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/522) / [#543](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/543) Self-Review-Pass-2 trailer 連燒 3 次 = AI 不清楚哪些事 hook 會擋、哪些得自己顧。AI session 啟動讀這張表，就知道**哪些重做是浪費 token、哪些漏做會 push 才爆**。

---

## TL;DR — AI 起手式只要記三類 owner

| Owner 類型 | 意思 | AI 該怎麼做 |
|---|---|---|
| 🔧 **hook-enforced** | 機械自動擋（commit / push / PreToolUse 時） | **不要重做** hook 會跑的檢查；信任它擋。失敗時讀 stderr 修 |
| 🧠 **skill-advised** | skill / 文件層有規則，但**無機械強制** | **必須自覺套用**；沒人會擋，漏做 = 進 repo |
| 👁️ **reviewer-only** | 純人工 review convention，無 hook 無 skill | **必須自覺**；最容易漏，review 才被退 |

死亡組合：以為某事是 hook-enforced（其實是 reviewer-only）→ 不做 → reviewer 退件 / 進 repo。本表就是消除這種誤判。

> **📊 Count reconciliation（建表時實測）**：pre-commit hook 實測 **50 auto + 14 manual + 3 pre-push = 67**（以 [`.pre-commit-config.yaml`](../../.pre-commit-config.yaml) `stages:` 為準）。CLAUDE.md / dev-rules 記為「51 auto + 13 manual + 3 pre-push」——**總數 67 相同，但 auto/manual 切分漂 1**（`jsx-babel-check-strict-linecount` 已從 pre-commit 改為 `stages: [manual]`，CLAUDE.md 計數未跟）。**這本身就是本表論點的活例**：count split drift 無任何 lint 攔（count 一致性 lint 多半驗總數或特定生成檔，不驗此 inline split）。建議 TRK-310 校正 CLAUDE.md 計數時一併處理。

---

## 1. Pre-push gates（3）— 🔧 機械，push 時最後防線

| Gate | hook id | Trigger | 涵蓋 | 失敗代價 | Reference |
|---|---|---|---|---|---|
| 擋直推 main | `protect-main-push` | 每次 `git push` | dev-rule #12 | push 被拒 | `scripts/ops/protect_main_push.sh` |
| 要求 preflight marker | `require-preflight-pass` | push 到 main 前 | 確保 `make pr-preflight` 跑過 | push 被拒（無 marker） | `scripts/ops/require_preflight_pass.sh` |
| mkdocs strict | `mkdocs-strict-pre-push` | push 含 `docs/**` / `mkdocs.yml` / `README.md` | dev-rule #4 mkdocs site-root 語意 | push 被拒（Tier 1）/ CI backstop（Tier 2） | `scripts/ops/pre_push_mkdocs_strict.sh` |

> **AI 注意**：mkdocs strict 是 push 時才跑——但 `vibe-dev-rules` skill 要你 **commit 前**先跑（`feedback_vibe_dev_rules_skill_before_commit`），別等 push 才發現 site-root link 壞掉。

---

## 2. PreToolUse session-guards（2）— 🔧 機械，tool 呼叫時

| Guard | 觸發 | 涵蓋 | Reference |
|---|---|---|---|
| `session-init.py` | 第一次 `Bash`/`Write`/`Edit`/`MultiEdit` | 關 VS Code Git + 寫 session marker（起手式 codified） | `scripts/session-guards/session-init.py` |
| `preflight_bash.py` | 每次 `Bash` | 攔 `sed -i` 掛載路徑（dev-rule #11）+ 攔 `_*.bat`/`_*.ps1`/`_*.cmd` 出 whitelist（Trap #54） | `scripts/session-guards/preflight_bash.py` |

> 這兩支讓「起手式」「檔案衛生」從 skill-advised 升級為 hook-enforced——AI 不必每次手動跑起手式，hook 代勞。

---

## 3. Pre-commit auto hooks（50）— 🔧 機械，commit 時自動

> 完整定義見 [`.pre-commit-config.yaml`](../../.pre-commit-config.yaml)。下表按職能分組；**AI 不需在 review 階段重做這些**——commit 時自動跑，失敗會擋。

| 職能群 | hook ids | 對應規範 | 涵蓋 |
|---|---|---|---|
| **檔案衛生 / 安全** | `file-hygiene` `sed-damage-guard` `head-blob-hygiene` `secrets-scan-staged` `bat-ascii-purity-check` `ad-hoc-git-scripts-check` `repo-name-check` `codename-leak-check` `hardcode-tenant-check` `window-x-no-fallback-check` | #2 #11、安全紀律 L1、Trap #45/#54 | NUL/EOF、secret（trufflehog）、tenant hardcode、codename leak |
| **文件 drift / 計數** | `tool-map-check` `doc-map-check` `adr-index-check` `planning-index-check` `rule-pack-stats-check` `glossary-check` `changelog-lint` `changelog-no-tbd-check` `version-consistency` `devrules-size-check` `commit-scope-doc-drift` `dev-rules-enforcement-check` | #4 Doc-as-Code | 各種「源↔生成」計數一致性 |
| **doc 連結 / 雙語** | `doc-links-check` `html-doc-links-check` `structure-check` `bilingual-structure-check` `bilingual-content-check` `bilingual-annotations-check` `includes-sync` | #9 #10 雙語政策、#4 | 連結有效性、ZH/EN 結構同步、CJK 純度 |
| **JSX / portal** | `design-token-usage` `axe-lite-static` `jsx-i18n-check` `jsx-babel-check` `undefined-tokens-check` `jsx-loader-compat-check` `dist-source-consistency-check` `skip-a11y-justification-check` `playwright-lint` `playwright-rtl-drift-check` `tool-consistency-check` `cli-coverage-check` `build-completeness-check` | #9 i18n、TRK-237/239 | token 合規、a11y、ESM、dist↔source |
| **平台資料 / routing** | `platform-data-check` `routing-profiles-check` `metric-dictionary-check` | 四層路由、Cardinality | Rule Pack ↔ metric 交叉驗證 |
| **測試治理** | `flaky-registry-check` `property-coverage-check` | TRK-010、property-pilot | flaky registry schema、coverage drift |
| **Python 安全 / 可攜** | `subprocess-timeout-audit`（FATAL）`open-encoding-audit`（warn） | S#74、PR-2.5 | timeout kwarg、encoding kwarg |
| **可達性** | `makefile-targets-check` | — | DX tools ↔ Makefile/pre-commit 可達 |

---

## 4. Pre-commit manual hooks（14）— 🔧 機械但**需手動觸發**

> 不在 commit 時自動跑；`pre-commit run --hook-stage manual --all-files` 或 `make lint-docs` 觸發。**這類最容易被 AI 誤當「自動會擋」**——其實不會，得記得手動跑（或 CI 才擋）。

| hook id | 用途 | 何時該手動跑 |
|---|---|---|
| `schema-check` | Go→JSON Schema drift | 改 Go struct / schema 後 |
| `translation-check` | 雙語結構一致 | 改外部面向 ZH 文件後 |
| `flow-e2e-check` | Guided Flow E2E smoke | 改 portal flow 後 |
| `jsx-babel-check-strict-linecount` | JSX 行數 soft-cap | 改大 JSX 後 |
| `i18n-coverage-check` | i18n 覆蓋報告 | 改 i18n 後 |
| `check-doc-reading-time` | >15 min 需拆 | 寫長文件後 |
| `check-doc-freshness` | >90 天 stale | 定期 |
| `path-metadata-consistency-check` | path/metadata 一致（warn） | 移檔後 |
| `check-doc-template` | 文件模板合規 | 新文件 |
| `check-portal-i18n` | Portal JSX i18n | 改 portal 後 |
| `orphan-doc-check` | 孤兒文件偵測 | 新增/刪文件後 |
| `glossary-coverage-check` | 高頻詞 glossary 覆蓋 | 引入新術語後 |
| `md-yaml-drift-check` | MD YAML 範例 ↔ schema | 改 schema 範例後 |
| `playwright-e2e` | Portal E2E smoke | 改 portal 後 |

> 上表 14 個確為實測 `stages: [manual]`（含 `jsx-babel-check-strict-linecount` — 即 CLAUDE.md「13」漏算的那個）。以 [`.pre-commit-config.yaml`](../../.pre-commit-config.yaml) 為 SSOT。

---

## 5. 本地 skills（3）— 🧠 advisory，AI 自覺觸發

| Skill | 涵蓋 | owner 性質 | 與 hook 關係 |
|---|---|---|---|
| `vibe-workflow` | 起手式、7 陷阱、FUSE/docker/port-forward | advisory（起手式部分已被 session-init hook 機械化） | **補集**：hook 做機械起手式，skill 講「卡住時怎麼救」 |
| `vibe-dev-rules` | 12 規範 + Top 4 | advisory（多數規範有對應 hook，但 commit 前提醒靠 skill） | **前置**：在 hook 擋下之前先自覺（省 push cycle） |
| `vibe-playbook-nav` | 任務→Playbook 章節路由 | advisory | 無對應 hook（純導航） |

> 優先級仲裁見 [CLAUDE.md §Skill 優先級宣告](../../CLAUDE.md)（TRK-301）：衝突時 `vibe-*` supersede 環境層 generic skill。

---

## 6. engineering:* 環境 skill 重疊

| engineering: skill | Vibe 對應 owner | 結論 |
|---|---|---|
| `engineering:code-review` | `vibe-dev-rules` + 51 pre-commit + commit-msg hook | git/commit/trailer 部分以 vibe-dev-rules 為準（TRK-301） |
| `engineering:debug` | `vibe-playbook-nav`（debug 章節） | 互補：reproduce 方法用 engineering，環境 trap 用 playbook |
| `engineering:testing-strategy` | `test-map.md` + vibe-dev-rules（測試 seam） | 策略用 engineering，Vibe 專屬 seam 用 test-map |
| `engineering:deploy-checklist` | `github-release-playbook` + `make pre-tag` + #474 | Vibe release 用 playbook + TRK-306（規劃中） |
| `engineering:incident-response` | `secret-leak-remediation-sop` | secret 事故用 Vibe SOP |

---

## 7. Overlap / Conflict / 漏接 findings

### 🔁 Overlap（冗餘，多半 intentional 為安全）

- **Commit trailer 規則 = 4 層**：dev-rules §P1（文件）+ `commit-msg` hook `validate_pass2_trailer_placement`（機械擋）+ `vibe-dev-rules` skill（commit 前提醒）+ CLAUDE.md 高頻地雷（always-on）。**唯一機械擋的是 commit-msg hook**；其餘 3 層是 advisory。→ TRK-310 收尾時 CLAUDE.md 版可縮 1-liner 指 dev-rules §P1（DRY）。
- **檔案衛生（sed -i）= 5 層**：dev-rule #11 + `preflight_bash.py`（PreToolUse 機械擋）+ `sed-damage-guard`（pre-commit）+ CLAUDE.md 高頻地雷 + `vibe-workflow`。機械擋有 2 層（PreToolUse + pre-commit），夠厚。

### ⚖️ Conflict（優先級歧義，由 TRK-301 仲裁）

- `vibe-workflow` vs 環境層 session-bootstrap generic skill → vibe-workflow 優先（已宣告）
- `vibe-dev-rules` vs `engineering:code-review`（git/commit）→ vibe-dev-rules 優先（已宣告）
- `vibe-playbook-nav` vs 跨 K8s/Helm/release/E2E generic 指引 → vibe-playbook-nav 優先（已宣告）

### 🕳️ 漏接（無機械防線 — AI 必須自覺，最高風險）

| 項目 | 現狀 owner | 風險 | 補位計畫 |
|---|---|---|---|
| **推銷語言**（dev-rule #6） | 👁️ reviewer-only（明文「未由 pre-commit hook 自動掃描」） | 進 repo 才被 review 退 | dev-rules backlog 有 keyword-scan lint 候選 |
| **架構圖 drift**（Mermaid/C4） | 🧠 skill-advised（TRK-303 第 6 lens）+ dev-rule #4 | code 改了圖沒同步 | 人工 lens；6 個月後評估 auto-detector |
| **IaC cross-file cascade** | ❌ 無（既有 lint 只做單檔 SAST，見 #448） | 改 selector 連動 NetworkPolicy/ServiceMonitor 漏改 | TRK-305 `vibe-subagent-review`（規劃中） |
| **SAST 7 條的 1/3/7**（encoding/chmod/stderr） | 👁️ reviewer convention（bandit 只 native 蓋 2/4/5/6） | 進 repo | dev-rule #5 已明列；reviewer 把關 |

---

## 8. AI agent 使用指引

1. **Commit / push 前**：先掃本表「🕳️ 漏接」+「🧠 skill-advised」——這些沒人機械擋，必須自覺做。
2. **不要重做 🔧 hook-enforced 的事**（51 auto + 3 pre-push + 2 PreToolUse）——浪費 token，hook 會擋。
3. **記得手動跑 §4 manual hooks**（改對應檔後）——它們不在 commit 自動跑，漏了 CI 才擋。
4. **trailer 規則**信任 commit-msg hook 會擋，但格式自覺照 CLAUDE.md 高頻地雷 #2 寫對（省一輪 commit 重試）。

---

## 關聯

- [CLAUDE.md §Pre-commit 品質閘門](../../CLAUDE.md)
- [`dev-rules.md`](dev-rules.md)（12 規範 + §P trailer 紀律）
- [`.pre-commit-config.yaml`](../../.pre-commit-config.yaml)（hook SSOT）
- epic [#570](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/570) / TRK-307（季度 audit 消費本表）/ TRK-310（CLAUDE.md 瘦身參考本表 overlap 段）
