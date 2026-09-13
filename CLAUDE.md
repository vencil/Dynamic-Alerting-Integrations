---
title: "CLAUDE.md — AI 開發上下文指引"
tags: [ai-agent, onboarding, internal]
audience: [ai-agent, maintainers]
version: v2.9.0
lang: zh
---

# CLAUDE.md — AI 開發上下文指引

> 這份是**路由表 + 不可協商項**。專案細節（架構、版本歷程、四層防線、工具清單）一律在被連結的檔案裡，**這裡刻意不複製**——複製出來的第二份必然先腐爛，而讀者無從得知讀到的是哪一份。
>
> 同理，本檔**不寫沒有機制在維持的計數**（有幾個 skill、幾個設計概念、幾條規範）。那種數字要嘛靠人一次次追、要嘛悄悄變錯，而知道「有東西在守、去哪裡跑它」才是有用的。
>
> ⛔ **反過來也成立：本檔裡凡是還留著的數字，都是有機制的，動它會弄紅閘門。** 目前只有兩個，各自在原地標註了守它的是誰。要判斷某個數字屬哪一類，別用 grep 找那個數字——機制是「算出來再比對」而不是把數字寫死。權威清單在 `bump_docs.py` 的 `_build_count_rules()`：
>
> ```bash
> python3 scripts/tools/dx/bump_docs.py --sync-counts --check   # 對帳；DEAD 規則會 fail-closed
> ```

## ⛔ 起手式：先確認閘門在不在

⛔ **不要假設 hook 跑過了。** 在**多 repo 的 web session** 裡，Claude Code 的 project root 是本 repo 的**上層**，於是它讀 `/home/user/.claude/settings.json`，本 repo 的 `.claude/settings.json` **整份不載入**——連同兩支 PreToolUse guard 與 `permissions` 區塊（[#1719](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1719)）。

第一件事是量它，不是信它：

```bash
cat /tmp/vibe-session-start-hook.ran     # 沒有這個檔 = hook 沒跑
```

有 `RESULT=ok` ⇒ 閘門已就緒。**檔案不存在或 `RESULT=failed` ⇒ 手動跑一次**：

```bash
CLAUDE_CODE_REMOTE=true CLAUDE_PROJECT_DIR="$PWD" bash .claude/hooks/session-start.sh
```

⚠️ **在那之前 `.git/hooks/` 可能是空的——commit 不受任何閘門保護**，且 `dev-rules #11` 的 `sed -i` 攔截也不存在。

**為什麼非跑不可**：remote session 每次都從全新 shallow clone 起，前一個 session 裝的東西不存活。而缺件的失敗方向**一律是靜默或誤導**，不是明顯的紅——沒有 `pre-commit` 則 `.git/hooks/` 全空且無提示；shallow clone 無 tag 會讓 image-pin 檢查報「git tag 不 resolve」，讀起來像 pin 打錯；沒有 `pytest` 則整族測試 uncollectable，「沒有失敗」與「什麼都沒跑」無法區分。`session-start.sh` 補齊這些，版本一律取自 `requirements/ci-constraints.txt`（本 repo 的 SSOT）。

⚠️ shallow clone 會讓 `tests/lint/` 的凍結量測 error，`git fetch --depth=1000 origin main` 後就過。

## ⛔ 不可協商

違反這幾條會燒掉至少一輪。每條都附機制理由，不是慣例偏好。

1. **回應語言** — 對人的輸出一律**繁體中文**（本則回應、PR / issue 留言、commit 訊息、`docs/**`）。⚠️ **repo 內工具印給 operator 的字串不在此列**，沿用該工具既有語言慣例（`scripts/tools/**` 目前為英文）；改既有字串時不得只翻被改到的那幾行。詳 [`dev-rules.md` §9c](docs/internal/dev-rules.md)。
2. **Commit trailer block** — trailer 行（`Refs:` / `Self-Review-Pass-2:` / `Co-authored-by:`）須為**最底部單一連續段落、全 `Key: value` 格式**。夾空行或無冒號的裸行會劈裂 block，git 丟棄其上各行，CI gate 因此紅。多項目 / 純文件 commit 依 [`dev-rules.md` §P1](docs/internal/dev-rules.md) 改在 body prose 列 ID，**不寫 `Resolves` 裸行**。⛔ `Self-Review-Pass-2` 這個字串是被釘住的：`test_the_enforced_trailer_key_is_named_in_both_always_on_files` 要求 CLAUDE.md 與 [`AGENTS.md`](AGENTS.md) **都**指名它，否則只讀可攜檔的 agent 永遠不會知道有這道閘門（TRK-377）。刪掉它會讓 Python Tests 轉紅。
3. **Worktree edit path** — 在 worktree 內編輯須 anchor worktree 路徑。main repo 同時 checked out，用 main-repo 路徑會**悄悄**落到 main。
4. **`git add` 括號 glob** — bash `[01]` 只配 `0`/`1` 不配 `2`。任何括號 glob 後必跑 `git diff --cached --stat` 驗 staged set。
5. **⛔ 沒有本則訊息內的驗證輸出，就不准宣稱通過** — 「測試過了 / lint 乾淨 / 修好了」都是**主張**，每一個都要對得上**這一輪實際跑過**的指令與其輸出。上一輪的結果不算、部分檢查不算、「應該會過」不算、subagent 回報成功不算（自己看 diff）。⚠️ 本 repo 燒過的具體形狀是**管線遮蔽 exit code**：`cmd | head; echo $?` 讀到的是 `head` 的 rc；要 rc 就別接管線。跑不了就說跑不了——**「量不到」與「量了沒事」必須可區分**。
6. **禁止直推 main** — 一律 branch → PR → owner 明示後 merge。pre-push hook 攔截（`scripts/ops/protect_main_push.sh` + `require_preflight_pass.sh`）。
7. **禁止對掛載路徑用 `sed -i`** — 會截斷缺少 EOF 換行的檔案。用 Read+Edit 或 pipe。
8. **Doc-as-Code** — 影響 API / schema / CLI 的變更須同步 `CHANGELOG.md` + `CLAUDE.md` + `README.md`。
9. **Tenant-Agnostic** — Go / PromQL / fixture 禁止 hardcode tenant id（例如 `db-a`）。
10. **commit / push 前先觸發 `vibe-dev-rules` skill** — pre-commit hook 不攔所有 Vibe gate（如 `make lint-docs-mkdocs`），skip-and-recover 會多燒 2+ 個 push cycle。

完整規範（受眾是 contributor／人）見 [`dev-rules.md`](docs/internal/dev-rules.md)。

⚠️ **跨 repo 的 AI 行為約束是另一份**：[`agent-rulebook.md`](docs/internal/agent-rulebook.md)（D-01～D-09，含路由表與成本上限）。⛔ 兩份刻意分開——不要把認識論紀律寫進 `dev-rules.md`。

## 往哪裡看

⛔ 先查這張表再開始，不要靠記憶重建規則。

| 情境 | 去哪 |
|---|---|
| session 起手 / FUSE 卡死 / docker exec 無輸出 / port-forward 殘留 | `vibe-workflow` skill |
| commit / push / refactor 前 | `vibe-dev-rules` skill → [`dev-rules.md`](docs/internal/dev-rules.md) |
| 寫 lint 或守衛前、宣稱買到偵測力前、寫「沒有 X 涵蓋」前、判 CI 綠燈前 | [`agent-rulebook.md`](docs/internal/agent-rulebook.md) |
| K8s / docker / release / conf.d / benchmark / E2E 要看哪份 playbook | `vibe-playbook-nav` skill |
| multi-file PR、`Agent` 跑完後、spawn 長時 reviewer 前 | `vibe-subagent-review` skill |
| release 收尾 / 打 tag | `vibe-release` skill → [`github-release-playbook.md`](docs/internal/github-release-playbook.md) |
| 新 ADR / 新 component / epic 拆解 / 技術選型 | `vibe-brainstorm` skill |
| 同一缺陷進入第 2 輪修正、或每修一輪就冒新洞 | `vibe-converge` skill |
| 新信任邊界 GA 前 / incident 後 / 季度深稽核 | `vibe-security-audit` skill |
| 架構概念、設計原理 | [`architecture-and-design.md`](docs/architecture-and-design.md)、spoke 在 [`docs/design/`](docs/design/) |
| 測試怎麼寫、注入 seam、`t.Parallel` 決策 | [`test-map.md`](docs/internal/test-map.md) |
| 公開文件在哪 | [`doc-map.md`](docs/internal/doc-map.md) |
| Python 工具在哪（CLI：`da-tools <cmd> --help`） | [`tool-map.md`](docs/internal/tool-map.md)；JSX 工具 SOT 在 [`tool-registry.yaml`](docs/assets/tool-registry.yaml) |
| Planning / Tracking ID（新項目一律 `TRK-NNN`） | [`planning-id-mapping.md`](docs/internal/planning-id-mapping.md)、[ADR-019](docs/adr/019-planning-ssot.md) |
| 本機起整套 stack | [`try-local/README.md`](try-local/README.md) |
| secret 洩漏處置（ASSUME COMPROMISE / ROTATE FIRST） | [`secret-leak-remediation-sop.md`](docs/internal/secret-leak-remediation-sop.md) |
| IaC lint baseline、Severity→Action、豁免列管 | [`iac-lint-baseline.md`](docs/internal/iac-lint-baseline.md) |
| 哪些事機械強制、哪些要 AI 自覺、哪裡漏接 | [`hook-vs-skill-coverage.md`](docs/internal/hook-vs-skill-coverage.md) |
| 版本歷程、in-flight 工作 | [`CHANGELOG.md`](CHANGELOG.md) |

⚠️ **測試注入 seam 的適用範圍是 `components/threshold-exporter/app/*_test.go`，不是全 repo 鐵則**。該範圍內鐵則是「metrics / logger / watch 這三者一律走 seam」，不等於全面禁止 global swap。**由 `t.Parallel()` 測試寫入的 process-global 必須用冪等 reset**，不能 save-then-restore（那是「最後一個 cleanup 贏」，會還原掉別的測試的寫入）；且 reset 只解決清理、**不提供隔離**——平行測試各自需要不同值時，全域本身就是錯的機制。完整對照表與決策樹見 test-map.md。

## Skill 體系

⛔ **SSOT 在 [`agents/skills/`](agents/skills/)，不是 `.claude/skills/`**（TRK-361）。後者是 `make agent-adapters` 的**生成物**——Claude Code 只認那個路徑所以必須存在，但改它會被 `gen-agent-adapters-check` 擋下、下次重生也會覆蓋。subagent 角色提示詞同理：SSOT 在 [`agents/roles/`](agents/roles/)，`.claude/agents/` 是生成物。

根目錄 [`AGENTS.md`](AGENTS.md)（AAIF 中性標準，Codex / Cursor / Copilot / Gemini CLI / Grok 原生讀）是**手寫散文**，刻意不複製規範內容；其中**只有 `BEGIN/END GENERATED SKILL INDEX` 之間**由 `agents/skills/` 的 frontmatter 生成，其餘直接編輯即可。

各 skill 的觸發時機見上方路由表；每支的完整內容在它自己的 `SKILL.md`，本檔不複述。

環境層 skills（`docx` / `pptx` / `xlsx` / `pdf` / `engineering:*` / `data:*` 等）**Claude 自主判斷使用**，不需逐次徵詢：判斷符合即讀 SKILL.md 執行（使用前單行說明用途）、可多 skill 串接；發現該裝沒裝的可主動搜尋並建議。

### Skill 優先級宣告（衝突仲裁；TRK-301）

多 skill 同時匹配時，本地 `vibe-*` 優先於環境層 generic，**僅限「Vibe 已有專屬流程」的範圍**：`vibe-workflow` > 環境層 session-bootstrap；`vibe-dev-rules` > `engineering:code-review` 的 git / commit / branch / trailer 部分；`vibe-playbook-nav` > 跨 K8s / Helm / release / E2E 的 generic 指引。環境層 skill 仍負責其專業領域（`engineering:debug` 的 reproduce、`data:*` 的分析等），不在此範圍者照常自主使用。

## Pre-commit 品質閘門

110 auto-run + 13 manual-stage hooks，清單見 [`.pre-commit-config.yaml`](.pre-commit-config.yaml)。

⛔ 上面那組數字由 `bump_docs.py --sync-counts` 自動同步——**改寫這個句型會讓同步規則變 DEAD、`Version Consistency` 轉紅**（它 fail-closed 在「規則撈不到東西」而不是靜默放行）。要改句型請一併改 `_build_count_rules()` 的 `pattern`。

手動觸發：

```bash
pre-commit run --all-files                       # auto stage
pre-commit run --hook-stage manual --all-files   # manual stage（較重）
```

⚠️ **pre-push 守衛不在那份清單裡**（[#1689](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1689)）。擋直推 main／要求 preflight marker／mkdocs strict 由 [`prepush_dispatch.sh`](scripts/ops/prepush_dispatch.sh) 執行，安裝走 `bash scripts/ops/install_prepush_hook.sh`（冪等；會把既有的 pre-push hook——全新 clone 上是 git-lfs 的——移到 `pre-push.chained` 並繼續執行）。「守衛在不在 push 路徑上」由 `make pr-preflight` 的 `Local hooks` 列回答。

⛔ 它們**不能**放回 `.pre-commit-config.yaml`：pre-commit 只餵 hook **一個** refspec，於是「同時推 `feat/x` 和 `main`」會讓 main 對守衛隱形。

## 專案概覽

**Multi-Tenant Dynamic Alerting 平台 (v2.9.0)** — config-driven、SHA-256 hot-reload、Directory Scanner。架構見 [`architecture-and-design.md`](docs/architecture-and-design.md)；**版本歷程與 in-flight 工作一律以 [`CHANGELOG.md`](CHANGELOG.md) 為準**，本檔不複述。

⛔ 上面那個版號**不是裝飾**：`_lib_versions.read_platform_version()` 以 `## 專案概覽` 標題為錨、抓 `Multi-Tenant Dynamic Alerting 平台 (vX.Y.Z)` 這個確切句型，`check_frontmatter_versions` / doc-map / tool-map / version-consistency 四處共用它。**改動這一行或這個標題名會讓那四支 lint 一起 rc 2**——要改版號請連同 release 流程一起改，不要順手重寫句型。

**語言策略（policy locked）**：**中文為主 SSOT + 英文為輔**（`foo.md` ZH / `foo.en.md` EN），**不執行 ZH→EN 遷移**。Phase 1 pilot 工具保留為 dormant option，不執行也不刪除。重新評估的觸發條件與完整評估依據見 [#145](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/145#issuecomment-4587136920)（trigger 觸發時 reopen 取用）。

**Release** 走六線版號（`v*` / `exporter/v*` / `tools/v*` / `portal/v*` / `recipe-preview/v*` / `tenant-api/v*`）。`recipe-preview/v*` 是「同步升」線——每次平台 release 重 tag、非獨立 cadence，防 bundled-compiler drift。完整步驟見 [`github-release-playbook.md`](docs/internal/github-release-playbook.md)。

## 開發環境

> **主路徑** Dev Container 做所有事（code / test / commit / push，優先 `make dc-*`）；**逃生門** FUSE 卡死時用 Windows 原生 git（`make win-commit` / `scripts/ops/win_git_escape.bat`）。目標：不讓任何 session 因 FUSE 卡死。

- **Dev Container**：`make dc-up` / `dc-test` / `dc-run CMD="..."`（或 `docker exec -w /workspaces/vibe-k8s-lab vibe-dev-container <cmd>`）。⚠️ raw `docker exec` 走的是 **root**（非 `remoteUser`）——依賴一律 system-wide 安裝以確保兩種身分皆可用。容器落後於 `devcontainer.json` 時 `make dc-run` 會 **exit 4** 並提示 rebuild（`VIBE_SKIP_DC_DOCTOR=1` 可繞過）。
- **測試**：Python 測試直接跑；**Go 測試需 Dev Container**（`make dc-go-test`，用 `MOD=` / `PKG=` 縮小範圍，單 package 秒級）。掛載路徑的檔案清理要走 `docker exec ... rm -f`。
- **K8s MCP** 常 timeout → fallback `docker exec`；**Prometheus / Alertmanager** 走 `port-forward` + `localhost:9090/9093`。

必記的 Makefile 入口：

- `make pr-preflight` — ⛔ PR merge 前必跑，寫 `.git/.preflight-ok.<SHA>` marker（marker 綁 sha，commit 後要重跑）。剛證 hooks 綠可用 `make pr-preflight-quick`。
- `make pre-tag` — ⛔ 打 tag 前必跑（version-check + lint-docs + 未發布 draft advisory 檢查 + docker build hard gate；需 docker / trivy / gh）。
- `make lint-docs` — 一站式文件 lint。
- `make session-cleanup` — session 結束清理。
- `make api-docs` — 從 tenant-api swag 標註產生 OpenAPI spec（改 handler 標註後必跑，CI 有 drift check）。
- `make contract-test` — schemathesis 契約測試。
- `make platform-data` — 重新產生 Rule Pack 數據。
- `make portal-build` / `make test-portal` — portal JSX bundle 與 Vitest。
- `make win-commit MSG=_msg.txt FILES="a b"` — FUSE 卡死時的 hook-gated Windows commit（siblings：`fuse-commit` / `fuse-locks` / `recover-index`）。

### Agent 開的 PR：review 迴路上四個「看起來綠／看起來卡」的坑

⛔ 這四項都不是偶發，是**結構性**的，每個 agent-opened PR 都會遇到：

1. **CodeRabbit 會不會審，取決於 PR 是用哪條路徑開的。** 作者是 **bot 帳號**（`claude[bot]`，例如走 `GH_TOKEN` + REST 開的）它直接 `Review skipped — Bot user detected`；作者是**人的帳號**（例如經 GitHub MCP 以 owner 身分開的）它**會審**。實據：[#1838](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/1838)（`claude[bot]`）被跳過、[#1841](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/1841)（`vencil`）審完並回 `No actionable comments`。⇒ **要知道這層在不在，先看 PR 的作者是誰**——預設「有」會高估 review 覆蓋，預設「沒有」會漏掉它真的給過的訊號。
2. **就算觸發過一次，後續 push 也不會自動再審**——[`.coderabbit.yaml`](.coderabbit.yaml) 設了 `auto_incremental_review: false`。⚠️ 連帶效果：PR 頁面的 **Merge Risk 橫幅停在被審過的那個 commit**，修完之後仍寫著舊 finding，容易被讀成現況。
3. **agent 解不開未 resolve 的 review thread。** resolve 只有 GraphQL 的 `resolveReviewThread`，而 agent session 的 GraphQL 只開放釘選的 PR-review 操作（其餘 403），REST 無對應端點。⇒ 若 branch protection 要求 conversation resolution，**CI 全綠的 PR 會停在 `mergeable_state: blocked` 而 agent 推不動**，只能請人在 UI 按。
4. **「為什麼 blocked」在 agent 這側量不到**：`GET /branches/main/protection` 對 app token 回 403。能做的是**差分**——比對前後兩個 head 的 check 名單＋結論，相同就代表不是 CI 造成的，再往 review / thread 方向找。

⚠️ 兩個容易誤讀的讀數：本 repo 只用 check-runs，`GET /commits/<sha>/status` **一律回 `state: pending` 且 `contexts` 為空**——那是空集合的既有行為，不是「還有東西沒跑完」；權威訊號是 `mergeable_state`。另外 `mergeable_state: unstable` 的意思是 required checks 都過了、只有非必要的 check 紅，**可以 merge**。

⚠️ 編輯 PR body 會重觸發帶 `edited` 的 workflow，多燒一輪 CI——改 body 前先想清楚代價。
