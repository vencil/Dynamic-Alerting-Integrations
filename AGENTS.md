# AGENTS.md — 跨 AI agent 的專案指引

> 本檔是 [AGENTS.md 開放標準](https://agents.md/)（Linux Foundation AAIF）的實作，Codex / Cursor / Copilot / Gemini CLI / Grok 等原生讀取。
>
> **Claude Code 讀的是 [`CLAUDE.md`](CLAUDE.md)**（它不讀 AGENTS.md），內容更深、含 Claude 專屬的 hook 與 skill 接線。兩者共用同一份規範來源，差別只在深度。

## 這個專案是什麼

Multi-Tenant Dynamic Alerting 平台：config-driven、SHA-256 hot-reload 的告警規則產生與路由系統。Go（`threshold-exporter` / `tenant-api`）+ Python 工具鏈 + Helm/K8s + Prometheus/Alertmanager。架構速覽見 [`docs/architecture-and-design.md`](docs/architecture-and-design.md)。

## 常用指令

```bash
make dc-up                       # 起 Dev Container（Go 測試需要它）
make dc-test                     # 容器內跑測試
make dc-go-test MOD=... PKG=...  # 縮小範圍的 Go 測試（單 package 秒級）
python3 -m pytest tests/ -q      # Python 測試（本機可直接跑）
pre-commit run --files <files>   # 變更檔的品質閘門
make lint-docs                   # 文件 lint
make pr-preflight                # ⛔ PR merge 前必跑
```

## 不可協商的六條

這七條是被實際燒過 ≥2 次才升為 always-on 的，其餘規範在下一節索引：

1. **⛔ 禁止直推 `main`** — 一律開 branch → PR → owner 同意後 merge。pre-push hook 會擋。
2. **回應語言** — 面向使用者的散文一律**繁體中文**。
3. **Commit trailer block** — 所有 trailer（`Refs:` / `Resolves:` / `Co-authored-by:`）須為**最底部單一連續段落、全部 `Key: value` 格式**。夾空行或無冒號的裸行會劈裂 block，git 丟棄上方行，CI gate 轉紅。
4. **禁止對掛載路徑用 `sed -i`** — 會截斷缺少 EOF 換行的檔案。用讀取 + 改寫，或 pipe。
5. **`git add` 用括號 glob 後必驗** — bash 的 `[01]` 只配 `0`/`1` 不配 `2`；跑 `git diff --cached --stat` 確認 staged set。
6. **Doc-as-Code** — 影響 API / schema / CLI / 計數的變更，須同步 `CHANGELOG.md` + `CLAUDE.md` + `README.md`。
7. **⛔ 沒有本則訊息內的驗證輸出，就不准宣稱通過** — 「測試過了」「lint 乾淨」「build 成功」「修好了」都是**主張**。每個主張要對得上一條**這一輪實際跑過**的指令與它的輸出：上一輪的結果不算、部分檢查不算、「應該會過」不算、agent 回報成功不算（要自己看 diff）。跑不了就說跑不了——**「量不到」與「量了沒事」必須可區分**。

## 規範在哪裡（索引，非複本）

⛔ 這裡**刻意不複製規範內容**——複製會製造這個 repo 最常燒的那種 drift。規範只有一份，在下列位置：

| 主題 | SSOT |
|---|---|
| 完整 13 條開發規範 | [`docs/internal/dev-rules.md`](docs/internal/dev-rules.md) |
| 品質閘門的 owner（哪些機械擋、哪些要自覺） | [`docs/internal/hook-vs-skill-coverage.md`](docs/internal/hook-vs-skill-coverage.md) |
| 測試注入 seam 與平行化決策樹 | [`docs/internal/test-map.md`](docs/internal/test-map.md) |
| Python 工具總表 | [`docs/internal/tool-map.md`](docs/internal/tool-map.md) |
| 追蹤 ID（`TRK-NNN`）對照 | [`docs/internal/planning-id-mapping.md`](docs/internal/planning-id-mapping.md) |
| Release 六線版號 SOP | [`docs/internal/github-release-playbook.md`](docs/internal/github-release-playbook.md) |

## 工作流 skills

以下是本專案的工作流程知識。**它們是純 markdown，任何 agent 都能直接讀**——支援 skill 機制的（如 Claude Code）會在情境符合時自動載入，不支援的請在對應情境自行閱讀。

<!-- BEGIN GENERATED SKILL INDEX -->

| Skill | When it applies |
|---|---|
| [`vibe-brainstorm`](agents/skills/vibe-brainstorm/SKILL.md) | 設計階段的 Socratic ideation — 用提問逼出 MVP 範圍、explicit trade-off、defer-with-trigger，加 proposer≠critic 內部對抗 + validate-direction，再走外部 adversarial review。Use when designing a new ADR / new component / epic decomposition / `RFC:` 討論 / 評估技術選型。SKIP for code-level debugging（用 `engineering:debug`）或 PR review（用 `vibe-subagent-review`）——這是「還沒寫 code、在決定要做什麼」的階段。 |
| [`vibe-converge`](agents/skills/vibe-converge/SKILL.md) | 多輪修正的收斂協議 —— decidability gate（開工前先問「這題用手上的證據判得出來嗎」）、跨輪交接契約（只有 verified claim / open question / 已打死方向表跨輪）、面積預算、三條停止規則。Use when 同一個缺陷進入第 2 輪（含）以後的修正、拿到 review finding 要開始修、或出現「每修一輪就冒出新洞、審不完」的感覺。SKIP for 第一輪實作、單檔 doc-only、以及還沒開始寫 code 的設計階段（用 `vibe-brainstorm`）。 |
| [`vibe-dev-rules`](agents/skills/vibe-dev-rules/SKILL.md) | Vibe 專案 13 條開發規範（dev-rules.md）的快速參考 + 最常違反 Top 4 深入說明。Use before git commit / push, when refactoring multi-tenant logic, when editing mount-path files, when touching API / schema / CLI / counts that require doc sync, or when unsure whether an action follows Vibe conventions. Also use when user asks "can I do X" about project conventions, or when about to hardcode a tenant id, use sed -i, or push directly to main. |
| [`vibe-playbook-nav`](agents/skills/vibe-playbook-nav/SKILL.md) | Route Vibe 任務到對應 Playbook 章節，避免通讀全文。Use when starting work that touches K8s, docker exec, release/tagging, conf.d, benchmark, Playwright E2E, Go race flake debugging, port-forward, Helm, PowerShell REST, or Windows-side git escape hatch. Also use when the user says "我要做 XXX，需要看哪份 Playbook？" or when unsure which Playbook section applies to the current task. |
| [`vibe-release`](agents/skills/vibe-release/SKILL.md) | Vibe 六線版號 release 收尾 SOP — make pre-tag → CHANGELOG distill + project-face refresh → 未發布 draft advisory 檢查 → 6-line tag push → gh release ×6。Use when wrapping a Vibe release：user 說「release 收尾 / 進入 phase e / 準備 release」、問「release 準備好了嗎」、branch 名 `chore/v*-release-wrapup`、或動到 `make pre-tag` / 六線 tag push / `gh release create`。延伸 #474 Layer 3 的 inline checklist 為系統化流程。 |
| [`vibe-security-audit`](agents/skills/vibe-security-audit/SKILL.md) | 全 component 週期性深度安全稽核 — Recon→平行 Hunt(Vibe 專屬攻擊面向)→對抗式 Validate(finder≠verifier 換模型)→Synthesize,跑在隔離 worktree 快照上。Use when 新信任邊界 GA 前(federation / L7 identity / machine identity)、security incident 後的「還漏什麼」sweep、或季度深稽核。SKIP for per-PR diff review(用內建 `/security-review`,那是 diff-scoped 單 agent)、code-level debug(用 `engineering:debug`)、單檔變更 review(用 `vibe-subagent-review`)。 |
| [`vibe-subagent-review`](agents/skills/vibe-subagent-review/SKILL.md) | IaC-aware 兩階段 review — code 走 spec→quality、IaC 走 blast-radius,含對抗式 review 紀律（finder≠verifier 自審 / verify-before-assert / only-actionable）。Use after a multi-file PR or an `Agent` implementation run, before commit — 特別是改動含 Helm values / .gotmpl / Prometheus rules / VRL transforms（這類「爆炸半徑優先」非單純 code quality）。補 #448 機械 SAST 抓不到的 cross-file cascade（改 selector 連動 NetworkPolicy / ServiceMonitor / ConfigMap 等）。Also use BEFORE spawning long-running（>15 min）reviewer / verifier subagents — 內含長時驗證 agent 可觀測性協議（預設 `Workflow` 編排；raw `Agent` 為例外、須寫 `dev/<scope>/PROGRESS.jsonl` ledger；單 agent ~15 min 上限）。SKIP if change is single-file doc-only or single-file test-only. |
| [`vibe-workflow`](agents/skills/vibe-workflow/SKILL.md) | Vibe session 起手式 + 最常踩的 7 個坑 + 標準開發 session 工作流。Use at the start of any Vibe working session (especially first Bash/Edit/Write call), when encountering FUSE phantom lock / stale git index / docker exec returning empty stdout / port-forward residue / pre-commit lock artifacts / ad-hoc script rejection, or when planning the end-to-end flow from code change through commit to PR. Also use when the user mentions "起手式", "FUSE 卡住", "docker exec 沒輸出", "win-commit", or when orienting to how Vibe's dev loop is supposed to run. |

<!-- END GENERATED SKILL INDEX -->

subagent 角色提示詞在 [`agents/roles/`](agents/roles/)。

## 這棵樹怎麼維護

```text
agents/                     ← SSOT，改這裡
  skills/<name>/SKILL.md
  roles/<name>.md

.claude/skills/**           ← 生成物（Claude Code 只認這個路徑）
.claude/agents/**           ← 生成物
AGENTS.md                   ← 手寫散文 + 機器維護的 skill 索引區塊（本檔）
```

改完 SSOT 跑 `make agent-adapters`。`gen-agent-adapters-check` pre-commit hook 會擋住漂移；轉接檔頭部的 `GENERATED from ...` 那行就是它的來源位址。⛔ 不要編輯 `.claude/` 底下的轉接檔——下一次 `--generate` 會覆蓋掉。**本檔（`AGENTS.md`）是例外**：散文直接改這裡，只有 `BEGIN/END GENERATED SKILL INDEX` 之間那塊由機器維護。

⚠️ 用複製而非 symlink，是因為本 repo 支援 Windows 逃生門，而 Windows host 上 symlink 實測會壞（PR #1457 有三支測試因此 error）。代價是每次 skill 編輯動到兩個檔、skill 文字在 git 裡存在兩份；drift gate 就是防第二份變成第二個真相源的東西。
