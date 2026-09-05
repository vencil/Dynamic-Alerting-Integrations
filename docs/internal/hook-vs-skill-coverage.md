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

## TL;DR — AI 起手式只要記四類 owner

| Owner 類型 | 意思 | AI 該怎麼做 |
|---|---|---|
| 🔧 **hook-enforced** | 機械自動擋（commit / push / PreToolUse 時） | **不要重做** hook 會跑的檢查；信任它擋。失敗時讀 stderr 修 |
| ⚙️ **CI-only** | 只在 CI 跑，**本地無 hook**（pre-commit / pre-push 都不攔） | **本地看不到、push 才紅**；別「信任 hook 會擋」。改到對應輸入時本地手動跑該 gate（清單見 §4.5） |
| 🧠 **skill-advised** | skill / 文件層有規則，但**無機械強制** | **必須自覺套用**；沒人會擋，漏做 = 進 repo |
| 👁️ **reviewer-only** | 純人工 review convention，無 hook 無 skill | **必須自覺**；最容易漏，review 才被退 |

死亡組合：以為某事是 hook-enforced（其實是 ⚙️ CI-only 或 👁️ reviewer-only）→ 不做 / 信任 hook 會擋 → **push 吃 CI 紅燈** / reviewer 退件。本表就是消除這種誤判。

> **📊 Count reconciliation**：⛔ **本行刻意不再自己抄一份數字（[#1664](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1664) 順帶）。** 先前它寫「102 auto + 13 manual + 3 pre-push = 118…與 CLAUDE.md 宣告一致」——當時對 SSOT（`bump_docs._count_precommit_hook_stages()`，由 `.pre-commit-config.yaml` YAML parse）重量，**三個數字沒有一個對得上**，也就是那句「一致」自己就是假的；而它是這份文件裡唯一一個**沒有寫入端也沒有檢查端**的計數副本，所以它必然再腐爛一次。⛔ **這裡刻意不重寫成新的三個數字**：`pre-push` 那一項在 [#1689](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1689) 之後已經不是 pre-commit 的 stage（守衛改由 `scripts/ops/prepush_dispatch.sh` 執行），所以連「幾個 stage」這個維度都換了母體——再抄一次只會製造第二次腐爛。權威值只有一處：`CLAUDE.md` 的 hook 計數，由 CI 的 `bump_docs --sync-counts --check` gate（見 §4.5）。⚠️ **下文 §3/§4 標題內的數字是當時的盤點快照、不由任何 gate 維護、且已知與 SSOT 不符**——標題保持原樣是為了不改動既有的 mkdocs anchor；要現值請跑 `bump_docs --sync-counts --check` 或讀 CLAUDE.md，不要從標題讀。CLAUDE.md 的 hook 計數自 #1185 PR2 起由 CI `bump_docs --sync-counts --check` gate（本身即 ⚙️ CI-only，見 §4.5）。下文 §3/§4 的職能分組表為 v2.8.1 盤點時的快照、其後新 hook 僅逐案補列——**計數以 `.pre-commit-config.yaml` YAML parse 為準**，分組表供職能導覽不做計數依據。
>
> **更正（TRK-307，時值 v2.8.1 = 51/13/3）**：本表初版（PR #582）曾誤記「50 auto + 14 manual」並反指 CLAUDE.md 計數漂移——那是用 grep `stages:\s*\[manual\]` 數的結果，**配到了 `jsx-babel-check-strict-linecount` 的註解行**（該 hook 註解明寫 "Auto-stage (NOT manual)"，曾被提議 manual 但 PR #162 改回 auto）。TRK-307 的 `audit_rules_drift.py` 用 **YAML parse**（非 grep）重數，確認當時為 51/13/3，CLAUDE.md 一直是對的。**教訓：hook 計數要 YAML parse，grep 會配到註解 / 文字**——audit 工具上線首次執行即抓出此自埋誤差。

---

## 1. Pre-push gates（3）— 🔧 機械，push 時最後防線

| Gate | Trigger | 涵蓋 | 失敗代價 | Reference |
|---|---|---|---|---|
| 擋直推 main | 每次 `git push` | dev-rule #12 | push 被拒 | `scripts/ops/protect_main_push.sh` |
| 要求 preflight marker | 每次 `git push`，但 **main/master 直接放行**（那歸擋直推 main 那支） | 確保 `make pr-preflight` 跑過 | push 被拒（無 marker） | `scripts/ops/require_preflight_pass.sh` |
| mkdocs strict | push 含 `docs/**` / `mkdocs.yml` / `README.md` | dev-rule #4 mkdocs site-root 語意 | push 被拒（Tier 1）/ CI backstop（Tier 2） | `scripts/ops/pre_push_mkdocs_strict.sh` |

> **AI 注意**：mkdocs strict 是 push 時才跑——但 `vibe-dev-rules` skill 要你 **commit 前**先跑（`feedback_vibe_dev_rules_skill_before_commit`），別等 push 才發現 site-root link 壞掉。

> ⛔ **本表沒有 `hook id` 欄，因為這三支不再是 pre-commit hook（[#1689](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1689)）。** 它們由 `scripts/ops/prepush_dispatch.sh` 執行，`.pre-commit-config.yaml` 裡**沒有**對應的 `stages: [pre-push]` 條目——有一支測試釘住「零條目」，因為重新加回去的那一份**是瞎的**（見下）而且會印 Passed。

> ⛔ **這三支不會自動安裝（[#1664](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1664)）。** 安裝只有一條：`bash scripts/ops/install_prepush_hook.sh`（冪等）。實測全新 clone 只照舊的單行說明做 ⇒ 直推 main 成功、畫面滿是綠色的 pre-commit 層 hook。「有沒有接上」由 `make pr-preflight` 的 `Local hooks` 回答，它對「沒接上」直接 FAIL 並印那條指令（`--skip-hooks` / `make pr-preflight-quick` 可略過）。
>
> ⛔ **安裝器會串接既有的 pre-push hook，而不是拒絕**——這不是方便，是必要：本 repo 有 `filter=lfs` 路徑而 `git lfs install` 是全域設定，所以**每一個全新 clone 的 `.git/hooks/pre-push` 一落地就是 git-lfs 的**。實測（Windows host、全新 clone）：拒絕覆寫 ⇒ 安裝器 rc=1，而 `make pr-preflight` 印的補救指令正是它 ⇒ 死路；改用舊指令 `pre-commit install --hook-type pre-push` ⇒ rc=0，但它把 lfs 那支 `#!/bin/sh` 的 hook 遷到 `pre-push.legacy`，之後**每次 push 都死在 `ExecutableNotFoundError: /bin/sh`**（pre-commit 在 Windows 自己解 shebang）——那條路在本次改動之前就是壞的。現在外來 hook 會被移到 `pre-push.chained`，由 dispatcher 用同樣的 argv 與 stdin 繼續呼叫。
>
> ⚠️ **`pre-commit install --hook-type pre-push` 不再是替代方案**：它裝出來的 hook 只看得到一列 refspec，而且只要設了 `core.hooksPath`（任何值）它直接 rc=1 拒絕安裝。

> ✅ **「只看得到一列 refspec」這個殘差已由 #1689 修掉，記在這裡是因為成因仍然是活的。** pre-commit 的 `hook_impl._pre_push_ns` 只回報 stdin **第一列**可推送的 ref；⛔ **git 是照 remote ref 名稱排序餵那些列的，不是照你在命令列打的順序**：實測 `git push origin main zzz` 與 `git push origin zzz main` 產生**逐字相同**的 stdin（`main` 在前）⇒ 「把 main 寫在前面」不是保命的方法；會藏住 main 的是**排序在 `refs/heads/main` 之前**的同批分支——而 dev-rule #12 要求的 `feat/` `fix/` `chore/` 全部落在那一側。實測（未修時）：`git push origin aaa main` 印 `Guard: block direct push to main ... Passed` 且 main 真的推上去了。⇒ **修法是不再把那三支註冊為 pre-commit 的 pre-push stage**；dispatcher 自己讀 stdin，所以 `--all` / `--mirror` 這類「一次多列」的形式也一併涵蓋，不必逐一宣稱。量測釘在 `tests/ops/test_prepush_hook_wiring.py`。第三支不讀 refspec（走 `@{u}` 比對），與此無關——它自己的缺陷是 [#1690](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1690)。

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

## 3. Pre-commit auto hooks（98）— 🔧 機械，commit 時自動

> 完整定義見 [`.pre-commit-config.yaml`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/.pre-commit-config.yaml)。下表按職能分組；**AI 不需在 review 階段重做這些**——commit 時自動跑，失敗會擋。

| 職能群 | hook ids | 對應規範 | 涵蓋 |
|---|---|---|---|
| **檔案衛生 / 安全** | `file-hygiene` `sed-damage-guard` `session-guard-liveness-check` `head-blob-hygiene` `secrets-scan-staged` `bat-ascii-purity-check` `ad-hoc-git-scripts-check` `repo-name-check` `codename-leak-check` `codename-gate-check` `hardcode-tenant-check` `window-x-no-fallback-check` | #2 #11、安全紀律 L1、Trap #45/#54、#824 | NUL/EOF、secret（trufflehog）、tenant hardcode、codename leak（L1 enumeration + L2 glossary-driven）、session-guard 可執行性 |
| **文件 drift / 計數** | `tool-map-check` `doc-map-check` `adr-index-check` `planning-index-check` `rule-pack-stats-check` `glossary-check` `changelog-lint` `changelog-no-tbd-check` `version-consistency` `devrules-size-check` `commit-scope-doc-drift` `dev-rules-enforcement-check` | #4 Doc-as-Code | 各種「源↔生成」計數一致性 |
| **doc 連結 / 雙語** | `doc-links-check` `html-doc-links-check` `structure-check` `bilingual-structure-check` `bilingual-content-check` `bilingual-annotations-check` `includes-sync` | #9 #10 雙語政策、#4 | 連結有效性、ZH/EN 結構同步、CJK 純度 |
| **JSX / portal** | `design-token-usage` `axe-lite-static` `jsx-i18n-check` `jsx-babel-check` `undefined-tokens-check` `jsx-loader-compat-check` `dist-source-consistency-check` `skip-a11y-justification-check` `playwright-lint` `playwright-rtl-drift-check` `tool-consistency-check` `cli-coverage-check` `build-completeness-check` | #9 i18n、TRK-237/239 | token 合規、a11y、ESM、dist↔source |
| **平台資料 / routing** | `platform-data-check` `routing-profiles-check` `metric-dictionary-check` | 四層路由、Cardinality | Rule Pack ↔ metric 交叉驗證 |
| **測試治理** | `flaky-registry-check` `property-coverage-check` `verify-diff-check` | TRK-010、property-pilot、#1185 PR2 | flaky registry schema、coverage drift、source→test 映射保鮮（`verify_diff --check`；原 ⚙️ CI-only，#1185 PR2 接成 hook） |
| **Python 安全 / 可攜** | `subprocess-timeout-audit`（FATAL）`open-encoding-audit`（warn） | S#74、PR-2.5 | timeout kwarg、encoding kwarg |
| **Shell 正確性** | `shellcheck`（OSS engine，`--norc --include=SC2006,SC1071,SC1072,SC1073,SC1008`） | lint-policy hybrid | 反引號命令替換：把 `` `cmd` `` 當排版引號寫進雙引號字串，bash 會**執行**它（`recover_index.sh` 曾因此在 diagnose-only 路徑重建 index、清掉 operator 暫存區）。額外的 SC10xx ＋ `--norc` 是 fail-closed 用——ShellCheck 只要對一個檔案**沉默**就零輸出、exit 0，而沉默有三道門：parse 失敗（SC1072/1073）、不支援的 shebang（**SC1071**）、以及 `disable=` 抑制（`.shellcheckrc` 由 `--norc` 擋掉；**檔內 `# shellcheck disable=` 刻意不擋**——那是工具正式的 opt-out 且會出現在 diff 裡，但要知道 `disable=all` 會連 SC2006 一起關掉）。⚠️ SC1008 **不是**門（實測：無法辨識的 shebang 仍會被完整分析、SC2006 照樣觸發），列入僅為防禦未來版本改變行為 |
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

> 上表 14 個為 YAML-parse 確認的 `stages: [manual]`（2026-06-12 重數；v2.8.1 後新增 `iac-helm-sast-check` / `k8s-manifests-sast-check`（#448）、退役 `flow-e2e-check`（TRK-242 residue 清理 — 守備與 auto-run `tool-consistency-check` 完全重複，增量檢查已併入該 lint））。`jsx-babel-check-strict-linecount` **不在此列**（它是 auto-stage；初版誤列，TRK-307 已更正）。以 [`.pre-commit-config.yaml`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/.pre-commit-config.yaml) 為 SSOT，計數用 YAML parse（見 `audit_rules_drift.py`）。

---

## 4.5 ⚙️ CI-only gates（無 pre-commit / pre-push hook，只 CI 攔）

> **根因級盲點**：這些 gate **沒有任何本地 hook**——commit / push 全綠、**只有 CI 會紅**。本表 v2.8.1 初版把「CI」維度折疊進 hook-enforced，導致 AI 誤以為「信任 hook 會擋」而在 push 後吃紅燈（一輪一個 push cycle）。改到對應輸入時**本地手動跑**該 gate。清單非窮舉，收錄 AI 最常誤判為 hook-enforced 的幾支。

| Gate | 本地手動跑 | 涵蓋 | 何時該手動跑 |
|---|---|---|---|
| **AST SAST 契約**（`tests/shared/test_sast.py`，1500+ tests） | `pytest tests/shared/test_sast.py` | `scripts/tools/` 全檔 AST 掃描（open-encoding / eval / 硬編碼機密 等 **6 規則**；⛔ `subprocess-timeout` **不在這 6 條裡**，它是 `.pre-commit-config.yaml` 的 FATAL **本機** hook `subprocess-timeout-audit`；dev-rules §5 第 4 條 `yaml.load` 自 #1643 起改由 bandit B506 強制，見下一列） | 改 `scripts/tools/**` 後 |
| **Python SAST（bandit B506 等）**（`.github/workflows/security-audit.yaml`） | `bandit -c .bandit -r scripts/tools components/da-tools -ll -ii` | dev-rules §5 items 2/4/5/6 的 native 規則；⛔ **hard-fail 但尚未列入 required check** | 改 `scripts/tools/**` / `components/da-tools/**` 後 |
| **工具 exit-code / bilingual-help 契約**（`test_tool_exit_codes.py`、`test_bilingual_help_contract.py`，數百 subprocess） | `pytest tests/shared/test_tool_exit_codes.py tests/shared/test_bilingual_help_contract.py` | da-tools 子命令 exit 0/1/2 約定（#452）、`--help` 雙語 | 改工具 CLI / help 後 |
| **pre-commit hook 計數一致性**（`bump_docs --sync-counts --check`） | `python scripts/tools/dx/bump_docs.py --sync-counts --check` | CLAUDE.md 的 pre-commit hook 計數——**唯一無本地 hook 的計數維度**（version / rulepack / tool / badge 計數已由 §3 auto hook `version-consistency` 本地攔） | 增刪 pre-commit hook 後（#1185 PR2 接進 CI Version Consistency job） |
| **OpenAPI spec drift** | `make api-docs` | tenant-api handler `@Router`/`@Param` 標註 ↔ 產出的 OpenAPI spec | 改 handler swag 標註後 |
| **契約測試**（schemathesis） | `make contract-test` | tenant-api 全 method fuzz（TRK-222/228） | 改 tenant-api API 後 |
| **行尾政策**（`tests/dx/test_line_ending_policy.py`，[dev-rules #11b](dev-rules.md)） | `pytest tests/dx/test_line_ending_policy.py` | `scripts/`+`components/`+`helm/` 全檔 AST：任何寫出文字的呼叫必須明確傳 `newline=` 且為字串字面值 | 新增/修改任何寫檔的 Python 後 |

> ⚠️ **行尾這條與 §3 的 `open-encoding-audit` hook 是姊妹規則、卻在不同執行點**：忘記 `encoding=` 在 commit 當下就會看到警告（該 hook 是 **warn-only**、exit 0、**不擋 commit**——見 §3 line 85 的 `(warn)` 與 `.pre-commit-config.yaml` 的 `--ci` 註解，殘留約 72 個 latent site 待清理後才會 flip 成 `--strict-open-encoding`）；忘記 `newline=` 則本地完全無聲，要 push 後才從 CI 紅燈得知。兩者掃的是**高度重疊的呼叫站點**（`_violations_in` 的 fail-closed 分支甚至刻意依賴 `encoding=` 的存在當「這是文字串流」的證據）。之所以先落在 pytest 而非 hook，是因為新增 pre-commit hook 有一串連鎖 gate（索引、`files:` regex、雙語 help、exit-code 契約…）；**若這條開始頻繁跳閘，判準同 `verify_diff` 的遷移先例——升為 hook 併進 `check_open_encoding.py`**（該支已有 `--ci` / `--strict` 嚴重度階梯與 `# …: ignore` 慣例可直接複用）。

> **遷移範例**：`verify_diff --check`（source→test 映射保鮮）**曾是本類**（唯一防線在 CI pytest 尾端的 `test_repo_check_is_green`），#1185 PR2 已接成 pre-commit hook `verify-diff-check` → 現屬 §3 hook-enforced，不在此表。判準「CI-only gate 若本地成本低、跳閘頻繁、可用 `files:` 限縮，就升為 hook」見 #1185（TRK-336）。

---

## 5. 本地 skills（8）— 🧠 advisory，AI 自覺觸發

| Skill | 涵蓋 | owner 性質 | 與 hook 關係 |
|---|---|---|---|
| `vibe-workflow` | 起手式、7 陷阱、FUSE/docker/port-forward | advisory（起手式部分已被 session-init hook 機械化） | **補集**：hook 做機械起手式，skill 講「卡住時怎麼救」 |
| `vibe-dev-rules` | 13 規範 + Top 4 | advisory（多數規範有對應 hook，但 commit 前提醒靠 skill） | **前置**：在 hook 擋下之前先自覺（省 push cycle） |
| `vibe-playbook-nav` | 任務→Playbook 章節路由 | advisory | 無對應 hook（純導航） |
| `vibe-subagent-review` | IaC-aware 兩階段 review（code spec→quality / IaC blast-radius） | advisory（cross-file 語義層，機械 SAST 抓不到） | **補集 #448**：機械層單檔 SAST 由 #448；本 skill 顧跨檔 cascade（TRK-305） |
| `vibe-release` | 六線版號 release 收尾 SOP（pre-tag / project-face / milestone-link） | advisory（release 紀律；docker+Trivy 部分已被 #474 機械化進 pre-tag，**Rule 4 未發布 draft advisory 檢查已被 #1295 機械化為 `draft-advisory-check`** — 但只在本地 `make pre-tag` 路徑，直接 push tag 仍繞過） | **延伸**：#474 把 Layer 1/2 機械化，本 skill 系統化 Layer 3 discipline（TRK-306） |
| `vibe-brainstorm` | 設計階段 Socratic ideation（MVP / trade-off / defer-trigger + 外審） | advisory（純設計流程） | 無對應 hook（設計階段，無 code 可機械驗）（TRK-308） |
| `vibe-converge` | 多輪修正的收斂協議（decidability gate / 跨輪交接契約 / 面積預算 / 輪數上限 5；⛔ 停止條件不是「零 finding」，且沒有終止條件） | advisory（同一缺陷第 2 輪起觸發） | **無對應 hook，刻意的**：`make converge-status` 只觀測不擋，不進 CI / pre-commit（#1457 剛刪掉六支「守衛的守衛」）（TRK-360） |
| `vibe-security-audit` | 週期性深度安全稽核 harness（Recon→平行 Hunt→對抗式 Validate→Synthesize，跑隔離 worktree 快照） | advisory（新信任邊界 GA 前 / incident 後 / 季度觸發） | **補集**：與 diff-scoped `/security-review` 互補、**不進 CI**（#1001） |

> 優先級仲裁見 [CLAUDE.md §Skill 優先級宣告](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/CLAUDE.md)（TRK-301）：衝突時 `vibe-*` supersede 環境層 generic skill。

---

## 6. engineering:* 環境 skill 重疊

| engineering: skill | Vibe 對應 owner | 結論 |
|---|---|---|
| `engineering:code-review` | `vibe-dev-rules` + 全部 pre-commit + commit-msg hook | git/commit/trailer 部分以 vibe-dev-rules 為準（TRK-301） |
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

### 🕳️ 漏接（機械防線缺席**或只在某一種 checkout 形態下存在** — AI 必須自覺，最高風險）

> ⚠️ 表頭原本只寫「無機械防線」。最後一列不是那個形狀——它**有** hook，只是那個 hook 在 worktree 裡跑不起來，而「有 hook」正是讓人不再自覺去做的理由。標題已放寬以涵蓋兩種。

| 項目 | 現狀 owner | 風險 | 補位計畫 |
|---|---|---|---|
| **推銷語言**（dev-rule #6） | 👁️ reviewer-only（明文「未由 pre-commit hook 自動掃描」） | 進 repo 才被 review 退 | dev-rules backlog 有 keyword-scan lint 候選 |
| **架構圖 drift**（Mermaid/C4） | 🧠 skill-advised（TRK-303 第 6 lens）+ dev-rule #4 | code 改了圖沒同步 | 人工 lens；6 個月後評估 auto-detector |
| **IaC cross-file cascade** | 🧠 `vibe-subagent-review`（TRK-305 已上線）；機械層仍待 #448 | 改 selector 連動 NetworkPolicy/ServiceMonitor 漏改 | skill 補語義層；#448 補機械層 SAST |
| **多輪修正不收斂**（同一缺陷第 2 輪起） | 🧠 `vibe-converge`（TRK-360）+ `make converge-status`（觀測，**不進 CI / 不進 pre-commit**） | 每輪淨增約 1000 行未受審新面；對資訊上不可判的問題連寫三版述詞；修法 commit 無人審；**以「零 finding」當終止條件把力氣導向鷹架**（`bf16d303` 實測：最近 60 顆 first-parent commit 中 self-serving 47 / product 13） | 刻意不做成 gate——#1457 剛刪掉六支「守衛的守衛」，對 review 流程再造一支會重演同一個病。工具檢查的是帳本**格式**（verified 有沒有附 evidence），**不檢查那段 evidence 是否真的跑過** |
| **Agent 指引 SSOT 漂移**（有人改 `.claude/**` 轉接檔而非 `agents/` SSOT） | 🔧 `gen-agent-adapters-check`（pre-commit，TRK-361） | 轉接檔被手改後下次重生就丟失；或 SSOT 已刪的 skill 仍被 vendor 探索到 | 已機械化：四類漂移（stale / missing / extra / SSOT 缺失）皆實測會擋。⚠️ 但**內容正確性**不在此 gate 範圍——它只保證兩側一致，不保證寫得對 |
| **SAST 7 條的 1/3/7**（encoding/chmod/stderr） | 👁️ reviewer convention（bandit 只 native 蓋 2/4/5/6） | 進 repo | dev-rule #5 已明列；reviewer 把關 |
| **A-13**（`test.skip()` / `test.fixme()`，任何寫法）在 **worktree** 內 | 🔧 `playwright-lint` hook，但**只在有 `tests/e2e/node_modules` 的 checkout 跑得起來** | `node_modules` 是 gitignored ⇒ 每一棵新開的 worktree 對它都是壞的、且不會自己好 | #1428：三個入口（hook／`make lint-e2e`／CI job）收斂到 `scripts/tools/lint/e2e_spec_lint.sh`（缺依賴時印 `cd tests/e2e && npm ci` 並 fail），並由 `tests/lint/test_e2e_spec_lint.py` 釘住三者真的**執行**它、以及 CI job 不得帶 `if:` / `continue-on-error`。⚠️ **CI 腿目前是 advisory**——`E2E Spec Lint (A-13)` **這個 job 自己**不在 main 的 required checks 內（`Smoke Tests (Chromium)` 也不在，但把後者設成 required 不會讓前者變 blocking）。要 blocking 見該票 |

> ⛔ **這一列的漏法是新的，值得單獨記**：前幾列都是「沒有機械防線」，這一列是 **gate 存在、寫得對、在主 checkout 綠，但只在那一種 checkout 形態下能執行**。§3 的「commit 時自動跑，失敗會擋」與 §8 第 2 點的「不要重做 hook-enforced 的事」對它**不成立**——判別時要問的不只是「有沒有 hook」，還有「這個 hook 的依賴，在我現在這棵樹裡在嗎」。

---

## 8. AI agent 使用指引

1. **Commit / push 前**：先掃本表「🕳️ 漏接」+「🧠 skill-advised」——這些沒人機械擋，必須自覺做。
2. **不要重做 🔧 hook-enforced 的事**（全部 auto hooks + §1 那三支 pre-push 守衛 + 2 PreToolUse；pre-commit stage 的確切數字見 §Count reconciliation，pre-push 那三支見 `scripts/ops/prepush_dispatch.sh` 的 `GUARDS`）——浪費 token，hook 會擋。**但 ⚙️ CI-only gate（§4.5：`test_sast` / `bump_docs` hook 計數 / OpenAPI drift / 契約測試）本地不跑、push 才紅**——別把它們當 hook-enforced；改到對應輸入時本地手動跑（否則吃一輪 CI 紅燈）。
3. **記得手動跑 §4 manual hooks**（改對應檔後）——它們不在 commit 自動跑，漏了 CI 才擋。
4. **trailer 規則**信任 commit-msg hook 會擋，但格式自覺照 CLAUDE.md 高頻地雷 #2 寫對（省一輪 commit 重試）。

---

## 關聯

- [CLAUDE.md §Pre-commit 品質閘門](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/CLAUDE.md)
- [`dev-rules.md`](dev-rules.md)（13 規範 + §P trailer 紀律）
- [`.pre-commit-config.yaml`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/.pre-commit-config.yaml)（hook SSOT）
- [`skill-system-feature-requests.md`](skill-system-feature-requests.md)（本表是 Vibe 內部能做的；upstream 需 Anthropic/Cowork 做的見該表，TRK-309）
- epic [#570](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/570) / TRK-307（季度 audit 消費本表）/ TRK-310（CLAUDE.md 瘦身參考本表 overlap 段）
