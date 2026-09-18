---
title: "Hook / Skill 邊界稽核矩陣"
tags: [internal, dx, governance, ai-agent]
audience: [ai-agents, maintainers]
version: v2.9.0
verified-at-version: v2.8.1
lang: zh
---

# Hook / Skill 邊界稽核矩陣（TRK-304）

盤點每道品質閘門的 **owner**：哪些機械擋（不必重做）、哪些只在 CI 擋（push 才紅）、哪些只有 skill／文件在講（漏做就進 repo）。本檔不寫計數：hook 數的 SSOT 是 `.pre-commit-config.yaml` 的 YAML parse，由 CI 的 `bump_docs --sync-counts --check` 對 CLAUDE.md 校驗（[#1664](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1664)；grep 數 hook 會配到註解，TRK-307）。

## TL;DR — 四類 owner

| Owner 類型 | 意思 | AI 該怎麼做 |
|---|---|---|
| 🔧 **hook-enforced** | 機械自動擋（commit / push / PreToolUse 時） | 不要重做；失敗時讀 stderr 修 |
| ⚙️ **CI-only** | 只在 CI 跑，本地無 hook | 改到對應輸入時本地手動跑該 gate（§4.5） |
| 🧠 **skill-advised** | skill / 文件層有規則，無機械強制 | 必須自覺套用 |
| 👁️ **reviewer-only** | 純人工 review convention | 必須自覺；review 才被退 |

死亡組合：以為某事是 hook-enforced（其實是 ⚙️ 或 👁️）→ 不做 → push 吃 CI 紅燈或被退件。

---

## 1. Pre-push gates — 🔧 由 `scripts/ops/prepush_dispatch.sh` 執行

| Gate | Trigger | 涵蓋 | 失敗代價 | Reference |
|---|---|---|---|---|
| 擋直推 main | 每次 `git push` | dev-rule #12 | push 被拒 | `scripts/ops/protect_main_push.sh` |
| 要求 preflight marker | 每次 `git push`（main/master 直接放行） | `make pr-preflight` 跑過 | push 被拒 | `scripts/ops/require_preflight_pass.sh` |
| mkdocs strict | 被推的 commit 改到符合守衛裡 `DOC_RE` 的檔（⛔ SSOT 在該腳本，本表刻意不重列；⚠️ 它**不限於 `docs/**`**），**或**該 ref 的 base 判不出來（fail-safe 一律建站） | dev-rule #4 site-root 語意 | push 被拒（Tier 1）/ CI backstop（Tier 2） | `scripts/ops/pre_push_mkdocs_strict.sh` |

- 不是 pre-commit hook。安裝配方只有一條：`bash scripts/ops/install_prepush_hook.sh`（冪等，串接既有 lfs hook 為 `pre-push.chained`）；**誰來跑它有四條路**——人類手動／AI session 由 `session-init.py` 的 `_heal_git_hooks` 代跑（裝不起來會出聲，不是靜默失敗）／web session 由 `.claude/hooks/session-start.sh` 代跑／多 repo 的 web session 兩邊都不跑（[#1719](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1719)：`.claude/settings.json` 整份不載入）。「接上了沒」由 `make pr-preflight` 的 `Local hooks` 回答，`--skip-hooks` 略不過（[#1689](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1689)、[#1664](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1664)、[#1811](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1811)）。`pre-commit install --hook-type pre-push` 不是替代方案。
- 多 refspec 同推時只看第一列的殘差已由 #1689 修掉（dispatcher 自己讀 stdin；釘在 `tests/ops/test_prepush_hook_wiring.py`）；mkdocs 守衛對被推的那顆 commit 建站而非工作樹（[#1690](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1690)）。
- 動到 `docs/**` 想提早看：`make lint-docs-mkdocs`。

---

## 2. PreToolUse session-guards — 🔧 僅限 project root == 本 repo 的 checkout

| Guard | 觸發 | 涵蓋 | Reference |
|---|---|---|---|
| `session-init.py` | 第一次 `Bash`/`Write`/`Edit`/`MultiEdit` | 關 VS Code Git + session marker + liveness heartbeat | `scripts/session-guards/session-init.py` |
| `preflight_bash.py` | 每次 `Bash`/`Write` | 攔 `sed -i` 掛載路徑（dev-rule #11）+ 攔 `_*.bat`/`_*.ps1`/`_*.cmd` 出 whitelist（Trap #54） | `scripts/session-guards/preflight_bash.py` |
| `skill_usage.py` | 每次 `Skill` | skill 觸發帳本（JSONL；`--stats` 給 quarterly audit 的汰除判準） | `scripts/session-guards/skill_usage.py` |
| `paths_map.py` | 每次 `Edit`/`Write`/`MultiEdit`/`Bash` | 命中 `.agents/paths-map.json` 的 glob 就以 `additionalContext` 注入「先讀哪一節＋一句約束」；每 session 每列最多兩次（Bash 命中一次、編輯類命中一次，唯讀的 `cat` 不會吃掉編輯時的那次）；Bash 只認指令裡**存在於磁碟**的路徑 | `scripts/session-guards/paths_map.py` |
| `stop_evidence.py` | `Stop`（主 agent 每回合結束） | 最後一則訊息含宣稱詞卻無 `$ ` 證據區塊、或任一 fence 裡的 `$ 指令`（任意縮排）不等於本回合 transcript 的 Bash/PowerShell tool_use 跑過的整條指令或其 `&&`／`;`／`\|` 一段 ⇒ exit 2 **一次**（`stop_hook_active` 與 per-prompt marker 保證不迴圈；transcript 還沒寫到這個 prompt 時只查形狀並在 stderr 說明；子代理跑的指令不算本回合） | `scripts/session-guards/stop_evidence.py` |

已知不涵蓋：
- 多 repo web session（project root 是本 repo 上層）整份 `.claude/settings.json` 不載入，上表全部涵蓋為零（[#1719](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1719)；起手式先查 `/tmp/vibe-session-start-hook.ran`）。
- matcher 不含 `PowerShell` 工具與 MCP 寫入類工具（`stop_evidence.py` 的來源比對認 PowerShell tool_use，但 `paths_map.py` 不看它的指令）。
- `sed -i` 攔截只認絕對掛載路徑；相對路徑放行（#824）。
- `paths_map.py` 看不見 Bash 即將**建立**的檔案（存在性是它過濾雜訊 token 的唯一方法）；`stop_evidence.py` 不套 `SubagentStop`、不是 required check。
- hook 失敗不 block 也不餵 stderr（只有 exit 2 會）；launcher 對「找不到直譯器」以 `additionalContext` fail-loud，其餘由 `session-guard-liveness-check` 在 commit 時攔（它從命令字串推導 guard 檔、釘五支的 event／matcher 接線、並驗 paths-map）。

| 失敗策略（#824） | 類型 | 理由 |
|---|---|---|
| fail-open | lint / format hooks | 壞掉的 lint 不該卡死日常作業 |
| fail-loud（`additionalContext`，不 block） | session guards / 衛生 guard | 全面 fail-closed 會把 session 變磚；風險面是 git 可恢復的損害 |
| fail-closed（exit 2） | security-critical（secret 外洩類） | 不可恢復的損害 |

新 hook / session-guard 的 AC 必須含 live-fire 證據（真實 harness 觸發 + 可觀測輸出），不得僅 code review（#824）。

---

## 3. Pre-commit auto hooks — 🔧 commit 時自動

完整定義見 [`.pre-commit-config.yaml`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/.pre-commit-config.yaml)；下表按職能分組，不做計數依據。

| 職能群 | hook ids | 對應規範 | 涵蓋 |
|---|---|---|---|
| **檔案衛生 / 安全** | `file-hygiene` `sed-damage-guard` `session-guard-liveness-check` `head-blob-hygiene` `secrets-scan-staged` `bat-ascii-purity-check` `ad-hoc-git-scripts-check` `repo-name-check` `codename-leak-check` `codename-gate-check` `hardcode-tenant-check` `window-x-no-fallback-check` | #2 #11、安全紀律 L1、Trap #45/#54、#824 | NUL/EOF、secret（trufflehog）、tenant hardcode、codename leak、session-guard 可執行性 |
| **文件 drift / 計數** | `tool-map-check` `doc-map-check` `adr-index-check` `planning-index-check` `rule-pack-stats-check` `glossary-check` `changelog-lint` `changelog-format` `changelog-no-tbd-check` `version-consistency` `devrules-size-check` `commit-scope-doc-drift` `dev-rules-enforcement-check` `cli-contract-check` | #4 Doc-as-Code | 源↔生成計數一致性；`cli-contract-check` 是文件裡的 da-tools 命令 ↔ 活 argparse；`changelog-format` 另對 `[Unreleased]` 設上限：整段對 base 長出的字元，每個新增 bullet 可帶 1,000、沒有新 bullet 時整段 ≤ 1,000（PR 上 base 是 `origin/$GITHUB_BASE_REF`、本地是 HEAD） |
| **doc 連結 / 雙語** | `doc-links-check` `html-doc-links-check` `structure-check` `bilingual-structure-check` `bilingual-content-check` `bilingual-annotations-check` `includes-sync` | #9 #10 雙語政策、#4 | 連結有效性、ZH/EN 結構同步、CJK 純度 |
| **JSX / portal** | `design-token-usage` `axe-lite-static` `jsx-i18n-check` `jsx-babel-check` `undefined-tokens-check` `jsx-loader-compat-check` `dist-source-consistency-check` `skip-a11y-justification-check` `playwright-lint` `playwright-rtl-drift-check` `tool-consistency-check` `cli-coverage-check` `build-completeness-check` | #9 i18n、TRK-237/239 | token 合規、a11y、ESM、dist↔source |
| **平台資料 / routing** | `platform-data-check` `routing-profiles-check` `metric-dictionary-check` | 四層路由、Cardinality | Rule Pack ↔ metric 交叉驗證 |
| **測試治理** | `flaky-registry-check` `property-coverage-check` `verify-diff-check` | TRK-010、property-pilot、#1185 PR2 | flaky registry schema、coverage drift、source→test 映射（原 ⚙️ CI-only，#1185 PR2 升為 hook） |
| **Python 安全 / 可攜** | `subprocess-timeout-audit`（FATAL）`open-encoding-audit`（warn-only） | S#74、PR-2.5 | timeout kwarg、encoding kwarg |
| **Shell 正確性** | `shellcheck`（`--norc --include=SC2006,SC1071,SC1072,SC1073,SC1008`） | lint-policy hybrid | 反引號命令替換；額外的 SC10xx 與 `--norc` 是 fail-closed 用（ShellCheck 沉默＝零輸出 exit 0） |
| **可達性** | `makefile-targets-check` | — | DX tools ↔ Makefile/pre-commit 可達 |

---

## 4. Pre-commit manual hooks — 🔧 需手動觸發

`pre-commit run --hook-stage manual --all-files` 或 `make lint-docs` 觸發；不在 commit 時自動跑——最容易被誤當「自動會擋」，其實不會。

| hook id | 用途 | 何時該手動跑 |
|---|---|---|
| `iac-helm-sast-check` | Container SAST L2：Helm template | 改 helm/ 後（CI 硬閘） |
| `k8s-manifests-sast-check` | Container SAST L4：raw k8s manifest | 改 k8s/ 後（CI 硬閘） |
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

---

## 4.5 ⚙️ CI-only gates — 無本地 hook，只 CI 攔

清單非窮舉，收錄最常被誤判為 hook-enforced 的幾支。

| Gate | 本地手動跑 | 涵蓋 | 何時該手動跑 |
|---|---|---|---|
| **AST SAST 契約**（`tests/shared/test_sast.py`） | `pytest tests/shared/test_sast.py` | `scripts/tools/` 全檔 AST（open-encoding / eval / 硬編碼機密等；`subprocess-timeout` 不在此，它是本機 hook） | 改 `scripts/tools/**` 後 |
| **Python SAST（bandit）**（`security-audit.yaml`） | `bandit -c .bandit -r scripts/tools components/da-tools -ll -ii` | dev-rules §5 items 2/4/5/6；hard-fail 但未列 required check | 改 `scripts/tools/**` / `components/da-tools/**` 後 |
| **工具 exit-code / bilingual-help 契約** | `pytest tests/shared/test_tool_exit_codes.py tests/shared/test_bilingual_help_contract.py` | da-tools exit 0/1/2（#452）、`--help` 雙語 | 改工具 CLI / help 後 |
| **pre-commit hook 計數一致性** | `python scripts/tools/dx/bump_docs.py --sync-counts --check` | CLAUDE.md 的 hook 計數 | 增刪 pre-commit hook 後 |
| **OpenAPI spec drift** | `make api-docs` | tenant-api swag 標註 ↔ spec | 改 handler 標註、**或標註可達的任何 struct**（`internal/rbac`、`internal/platform`、`internal/federation/fedpolicy` 的型別也在 spec 的 definitions 裡）後；CI 上紅在 `go-tests-tenant-api` 的「Verify OpenAPI spec is up-to-date」步驟，看起來像 Go 測試失敗 |
| **契約測試**（schemathesis） | `make contract-test` | tenant-api 全 method fuzz | 改 tenant-api API 後 |
| **行尾政策**（`tests/dx/test_line_ending_policy.py`，[dev-rules #11b](dev-rules.md)） | `pytest tests/dx/test_line_ending_policy.py` | 寫文字的呼叫必須明確傳字串字面值 `newline=` | 改任何寫檔的 Python 後 |

行尾這條與 §3 的 `open-encoding-audit`（warn-only）是姊妹規則但在不同執行點；若頻繁跳閘，判準同 `verify_diff` 的先例（本地成本低、可用 `files:` 限縮 ⇒ 升為 hook，#1185）。

---

## 5. 本地 skills — 🧠 advisory

| Skill | 涵蓋 | 與 hook 關係 |
|---|---|---|
| `vibe-workflow` | 起手式、7 陷阱、從改動到 PR | 補集：hook 做機械起手式，skill 講卡住時怎麼救 |
| `verifying-claims` | 宣稱前的路由 → agent-rulebook | 無對應 hook |
| `vibe-playbook-nav` | 任務→Playbook 章節路由 | 無對應 hook |
| `vibe-subagent-review` | 副檔名路由 review（code spec→quality / IaC blast-radius）、收 review 處置、長時 agent ledger | 補集 #448：機械層單檔 SAST 由 #448，本 skill 顧跨檔 cascade（TRK-305） |
| `vibe-release` | 六線版號 release 收尾 SOP | #474 機械化 Layer 1/2；`draft-advisory-check` 只在本地 `make pre-tag` 路徑，直接 push tag 仍繞過（TRK-306） |
| `vibe-brainstorm` | 設計階段五問 + locked decision + 外審 | 無對應 hook（TRK-308） |
| `vibe-converge` | 多輪修正：decidability gate、跨輪三類、停止規則、`ROUNDS.jsonl` | 刻意無 hook：`make converge-status` 只觀測不擋（TRK-360） |
| `vibe-security-audit` | 週期性深度安全稽核 harness | 與 diff-scoped `/security-review` 互補、不進 CI（#1001） |

優先級仲裁見 [CLAUDE.md §Skill 優先級宣告](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/CLAUDE.md)（TRK-301）：衝突時 `vibe-*` supersede 環境層 generic skill（`vibe-workflow` > session-bootstrap、repo 規範 > `engineering:code-review` 的 git/commit 部分、`vibe-playbook-nav` > 跨 K8s/Helm/release/E2E generic）。

---

## 6. engineering:* 環境 skill 重疊

| engineering: skill | Vibe 對應 owner | 結論 |
|---|---|---|
| `engineering:code-review` | CLAUDE.md／AGENTS.md 不可協商項 + dev-rules.md + pre-commit + commit-msg hook | git/commit/trailer 部分以 repo 規範為準 |
| `engineering:debug` | `vibe-playbook-nav`（debug 章節） | reproduce 方法用 engineering，環境 trap 用 playbook |
| `engineering:testing-strategy` | `test-map.md` | 策略用 engineering，Vibe 專屬 seam 用 test-map |
| `engineering:deploy-checklist` | `github-release-playbook` + `make pre-tag` | Vibe release 用 playbook + `vibe-release` |
| `engineering:incident-response` | `secret-leak-remediation-sop` | secret 事故用 Vibe SOP |

---

## 7. Overlap / 漏接

### 🔁 Overlap（多為刻意冗餘）

- **Commit trailer 規則 = 3 層**：dev-rules §P1 + `commit-msg` hook `validate_pass2_trailer_placement`（唯一機械擋）+ CLAUDE.md／AGENTS.md 不可協商項。
- **`sed -i` 檔案衛生 = 5 層**：dev-rule #11 + `preflight_bash.py`（PreToolUse）+ `sed-damage-guard`（pre-commit）+ CLAUDE.md 高頻地雷 + `vibe-workflow`。web session 形態下 PreToolUse 那層不存在（§2）。
- **「散文裡指名的東西必須存在」= 4 層，互不涵蓋、沒有一層是全樹的**（TRK-379）：

  | 機制 | 述詞 | 範圍 | 跑在哪 |
  |---|---|---|---|
  | `tests/ops/test_wrapped_path_references.py` | 折行後才出現且 resolve 的路徑 | 全 repo | CI |
  | `tests/shared/test_mutation_catalog.py::TestKillTestNamesAnchored` | 懸空測試名（dataclass 欄位） | mutation catalog | CI |
  | `scripts/tools/lint/check_dev_rules_enforcement.py` | 懸空 hook 名（inline-code） | `dev-rules.md` 一個檔 | 🔧 pre-commit |
  | `tests/dx/test_list_subprocess_only_modules.py::test_prose_names_no_test_that_does_not_exist` | 懸空測試名（backtick 內） | 兩個檔 | CI |

  第二支明文排除 comment/docstring 裡的名字，不能吸收第四支；第四支的 backtick 錨點放寬會偽造名字（[#1640](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1640)、[#1453](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1453)）。

### 🕳️ 漏接（機械防線缺席，或只在某一種 checkout 形態下存在）

| 項目 | 現狀 owner | 風險 | 補位 |
|---|---|---|---|
| **推銷語言**（dev-rule #6） | 👁️ reviewer-only | 進 repo 才被退 | keyword-scan lint 候選 |
| **架構圖 drift**（Mermaid/C4） | 🧠 TRK-303 第 6 lens + dev-rule #4 | code 改了圖沒同步 | 人工 lens |
| **IaC cross-file cascade** | 🧠 `vibe-subagent-review` | 改 selector 漏改 NetworkPolicy/ServiceMonitor | skill 補語義層；#448 補機械層 |
| **多輪修正不收斂** | 🧠 `vibe-converge` + `make converge-status`（不進 CI / pre-commit） | 每輪淨增未受審面；對不可判的問題連寫多版述詞 | 刻意不做成 gate（#1457）；工具只驗帳本格式，不驗 evidence 是否跑過 |
| **Agent 指引 SSOT 漂移**（改 `.claude/**` 而非 `.agents/`） | 🔧 `gen-agent-adapters-check` | 轉接檔被手改後重生即丟失 | 已機械化（stale / missing / extra / SSOT 缺失）；不保證內容正確 |
| **SAST 7 條的 1/3/7**（encoding/chmod/stderr） | 👁️ reviewer convention（bandit 只蓋 2/4/5/6） | 進 repo | dev-rule #5 明列 |
| **A-13**（`test.skip()` / `test.fixme()`）在 worktree 內 | 🔧 `playwright-lint`，但只在有 `tests/e2e/node_modules` 的 checkout 跑得起來 | 新 worktree 對它是壞的 | [#1428](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1428)：三入口收斂到 `e2e_spec_lint.sh`，`tests/lint/test_e2e_spec_lint.py` 釘住三者真的執行它且 CI job 不得帶 `if:` / `continue-on-error`；該 job 非 required |
| **起手式 + `sed -i` 衛生**在 web session 內 | 🔧 `.claude/settings.json` 的 hook，只在 project root == 本 repo 時載入 | hook 根本沒被註冊，畫面與「沒有這條規則」無法區分 | [#1719](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1719)：SessionStart hook + `/tmp/vibe-session-start-hook.ran` marker 可觀測；⛔ marker 只有「有／無」兩態，分不出「hook 沒被呼叫」與「被呼叫但定位 repo root 時就 exit 1」（後者發生在 marker 寫入前）；根因修法未裁決 |
| **nested `CLAUDE.md`**（`tools/portal/CLAUDE.md`） | 🔧 Claude Code 原生，但只在 Read 工具讀該子樹時帶入 | Bash `cat` 不觸發 | [#1757](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1757)：推廣前先量真實觸發率 |

最後三列同族：機制存在但只在某形態下能執行——缺依賴（A-13）、缺註冊（#1719）、缺叫用形式（nested）。判「有沒有 hook」時要連問依賴在不在、註冊載入了沒、叫用方式會不會觸發。

---

## 8. AI agent 使用指引

1. Commit / push 前先掃 🕳️ 與 🧠——沒人機械擋。
2. 不要重做 🔧（先確認 project root 是本 repo，否則見 §2）；⚙️ CI-only 本地不跑、push 才紅，改到對應輸入時手動跑。
3. 改對應檔後記得跑 §4 manual hooks。
4. trailer 格式照 CLAUDE.md 高頻地雷 #2 寫對，省一輪 commit 重試。

## 關聯

- [CLAUDE.md §Pre-commit 品質閘門](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/CLAUDE.md)
- [`dev-rules.md`](dev-rules.md)
- [`.pre-commit-config.yaml`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/.pre-commit-config.yaml)
- epic [#570](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/570) / TRK-307 / TRK-310
