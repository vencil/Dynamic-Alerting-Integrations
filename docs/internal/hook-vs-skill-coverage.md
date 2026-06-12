---
title: "Hook / Skill 邊界稽核矩陣"
tags: [internal, dx, governance, ai-agent]
audience: [ai-agents, maintainers]
version: v2.9.0
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

> **📊 Count reconciliation**：pre-commit hook 為 **69 auto + 14 manual + 3 pre-push = 86**（YAML parse 重數於 2026-06-12；#824 新增 `session-guard-liveness-check`、TRK-242 residue 清理移除三重失效的 `flow-e2e-check`），與 CLAUDE.md 宣告一致。下文 §3/§4 的職能分組表為 v2.8.1 盤點時的快照、其後新 hook 僅逐案補列——**計數以 `.pre-commit-config.yaml` YAML parse 為準**，分組表供職能導覽不做計數依據。
>
> **更正（TRK-307，時值 v2.8.1 = 51/13/3）**：本表初版（PR #582）曾誤記「50 auto + 14 manual」並反指 CLAUDE.md 計數漂移——那是用 grep `stages:\s*\[manual\]` 數的結果，**配到了 `jsx-babel-check-strict-linecount` 的註解行**（該 hook 註解明寫 "Auto-stage (NOT manual)"，曾被提議 manual 但 PR #162 改回 auto）。TRK-307 的 `audit_rules_drift.py` 用 **YAML parse**（非 grep）重數，確認當時為 51/13/3，CLAUDE.md 一直是對的。**教訓：hook 計數要 YAML parse，grep 會配到註解 / 文字**——audit 工具上線首次執行即抓出此自埋誤差。

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
| `session-init.py` | 第一次 `Bash`/`Write`/`Edit`/`MultiEdit` | 關 VS Code Git + 寫 session marker + 刷 liveness heartbeat（起手式 codified） | `scripts/session-guards/session-init.py` |
| `preflight_bash.py` | 每次 `Bash`/`Write` | 攔 `sed -i` 掛載路徑（dev-rule #11）+ 攔 `_*.bat`/`_*.ps1`/`_*.cmd` 出 whitelist（Trap #54） | `scripts/session-guards/preflight_bash.py` |

> 這兩支讓「起手式」「檔案衛生」從 skill-advised 升級為 hook-enforced——AI 不必每次手動跑起手式，hook 代勞。兩支自 #824 起一律經 `run-hooks.sh` launcher 啟動（功能性直譯器探測；`session-guard-liveness-check` pre-commit gate 防回歸）。
>
> **已知不涵蓋（負空間，#824 取證後誠實列出）**：
> - matcher 只含 `Bash|Write|Edit|MultiEdit`——**`PowerShell` 工具與 MCP 寫入類工具（Desktop Commander / Windows-MCP 等）不觸發任何 guard**。PowerShell-first session 的第一個 mutating call 不會跑起手式；MCP 寫檔完全繞過檔案衛生攔截。
> - `preflight_bash` 的 `sed -i` 攔截需命令文字含**絕對**掛載路徑——cwd 在 repo 內的**相對路徑** `sed -i` 同樣危險但放行（原設計刻意寬網不擋誤殺；收緊與否見 #824）。
> - hook 失敗（直譯器壞 / script crash）依協議**不會 block 也不會餵 stderr 給模型**（只有 exit 2 會）——launcher 對「找不到直譯器」以 `additionalContext` JSON fail-loud 補位，其餘失效 class 由 `session-guard-liveness-check` 在 commit 時攔。

### Hook 失敗策略分級（#824 codify）

| 類型 | 失敗策略 | 理由 |
|---|---|---|
| Lint / format hooks | fail-open（退化成沒檢查），warn 即可 | 寫壞的 lint 不應卡死日常作業 |
| **Session guards / 衛生 guard** | **fail-loud**：`additionalContext` JSON（exit 0）把失效訊息餵給模型，**不 block** | 全面 fail-closed 會把 session 變不可恢復的磚（env 壞 → 連修復能力都被擋）；guard 失效的風險面是 git 可恢復的損害，爆炸半徑不對稱 |
| Security-critical（secret 外洩類） | fail-closed：exit 2 block + stderr 餵模型 | 不可恢復的損害（外洩即起跑 Rotate-First）值得擋下一切 |

> **新 hook / session-guard 的 AC 必須含 live-fire 證據**（真實 harness 觸發 + 可觀測輸出，如 telemetry event），不得僅 code review——#824 的教訓：session-init 上線時從未在真實 harness spawn 路徑驗收，cp950 crash + Store-stub 兩層失效靜默七週，telemetry 寫滿卻無消費者。

---

## 3. Pre-commit auto hooks（69）— 🔧 機械，commit 時自動

> 完整定義見 [`.pre-commit-config.yaml`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/.pre-commit-config.yaml)。下表按職能分組；**AI 不需在 review 階段重做這些**——commit 時自動跑，失敗會擋。

| 職能群 | hook ids | 對應規範 | 涵蓋 |
|---|---|---|---|
| **檔案衛生 / 安全** | `file-hygiene` `sed-damage-guard` `session-guard-liveness-check` `head-blob-hygiene` `secrets-scan-staged` `bat-ascii-purity-check` `ad-hoc-git-scripts-check` `repo-name-check` `codename-leak-check` `codename-gate-check` `hardcode-tenant-check` `window-x-no-fallback-check` | #2 #11、安全紀律 L1、Trap #45/#54、#824 | NUL/EOF、secret（trufflehog）、tenant hardcode、codename leak（L1 enumeration + L2 glossary-driven）、session-guard 可執行性 |
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
| `iac-helm-sast-check` | Container SAST L2：Helm template（kube-linter + Vibe wrapper） | 改 helm/ 後（CI 有專屬 job 硬閘） |
| `k8s-manifests-sast-check` | Container SAST L4：raw k8s manifest（kube-linter） | 改 k8s/ raw manifest 後（CI 硬閘） |
| `schema-check` | Go→JSON Schema drift | 改 Go struct / schema 後 |
| `translation-check` | 雙語結構一致 | 改外部面向 ZH 文件後 |
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

> 上表 14 個為 YAML-parse 確認的 `stages: [manual]`（2026-06-12 重數；v2.8.1 後新增 `iac-helm-sast-check` / `k8s-manifests-sast-check`（#448）、移除三重失效的 `flow-e2e-check`（TRK-242 residue，檢查併入 auto-run `tool-consistency-check`））。`jsx-babel-check-strict-linecount` **不在此列**（它是 auto-stage；初版誤列，TRK-307 已更正）。以 [`.pre-commit-config.yaml`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/.pre-commit-config.yaml) 為 SSOT，計數用 YAML parse（見 `audit_rules_drift.py`）。

---

## 5. 本地 skills（6）— 🧠 advisory，AI 自覺觸發

| Skill | 涵蓋 | owner 性質 | 與 hook 關係 |
|---|---|---|---|
| `vibe-workflow` | 起手式、7 陷阱、FUSE/docker/port-forward | advisory（起手式部分已被 session-init hook 機械化） | **補集**：hook 做機械起手式，skill 講「卡住時怎麼救」 |
| `vibe-dev-rules` | 12 規範 + Top 4 | advisory（多數規範有對應 hook，但 commit 前提醒靠 skill） | **前置**：在 hook 擋下之前先自覺（省 push cycle） |
| `vibe-playbook-nav` | 任務→Playbook 章節路由 | advisory | 無對應 hook（純導航） |
| `vibe-subagent-review` | IaC-aware 兩階段 review（code spec→quality / IaC blast-radius） | advisory（cross-file 語義層，機械 SAST 抓不到） | **補集 #448**：機械層單檔 SAST 由 #448；本 skill 顧跨檔 cascade（TRK-305） |
| `vibe-release` | 五線版號 release 收尾 SOP（pre-tag / project-face / milestone-link） | advisory（release 紀律；docker+Trivy 部分已被 #474 機械化進 pre-tag） | **延伸**：#474 把 Layer 1/2 機械化，本 skill 系統化 Layer 3 discipline（TRK-306） |
| `vibe-brainstorm` | 設計階段 Socratic ideation（MVP / trade-off / defer-trigger + 外審） | advisory（純設計流程） | 無對應 hook（設計階段，無 code 可機械驗）（TRK-308） |

> 優先級仲裁見 [CLAUDE.md §Skill 優先級宣告](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/CLAUDE.md)（TRK-301）：衝突時 `vibe-*` supersede 環境層 generic skill。

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
| **IaC cross-file cascade** | 🧠 `vibe-subagent-review`（TRK-305 已上線）；機械層仍待 #448 | 改 selector 連動 NetworkPolicy/ServiceMonitor 漏改 | skill 補語義層；#448 補機械層 SAST |
| **SAST 7 條的 1/3/7**（encoding/chmod/stderr） | 👁️ reviewer convention（bandit 只 native 蓋 2/4/5/6） | 進 repo | dev-rule #5 已明列；reviewer 把關 |

---

## 8. AI agent 使用指引

1. **Commit / push 前**：先掃本表「🕳️ 漏接」+「🧠 skill-advised」——這些沒人機械擋，必須自覺做。
2. **不要重做 🔧 hook-enforced 的事**（51 auto + 3 pre-push + 2 PreToolUse）——浪費 token，hook 會擋。
3. **記得手動跑 §4 manual hooks**（改對應檔後）——它們不在 commit 自動跑，漏了 CI 才擋。
4. **trailer 規則**信任 commit-msg hook 會擋，但格式自覺照 CLAUDE.md 高頻地雷 #2 寫對（省一輪 commit 重試）。

---

## 關聯

- [CLAUDE.md §Pre-commit 品質閘門](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/CLAUDE.md)
- [`dev-rules.md`](dev-rules.md)（12 規範 + §P trailer 紀律）
- [`.pre-commit-config.yaml`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/.pre-commit-config.yaml)（hook SSOT）
- [`skill-system-feature-requests.md`](skill-system-feature-requests.md)（本表是 Vibe 內部能做的；upstream 需 Anthropic/Cowork 做的見該表，TRK-309）
- epic [#570](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/570) / TRK-307（季度 audit 消費本表）/ TRK-310（CLAUDE.md 瘦身參考本表 overlap 段）
