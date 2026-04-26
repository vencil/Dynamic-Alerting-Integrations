---
title: "Security Audit Runbook"
tags: [documentation, security, governance]
audience: [maintainer, ai-agent]
version: v2.8.0
verified-at-version: v2.8.0
lang: zh
---
# Security Audit Runbook

> Trivy-driven CVE audit + bump methodology。每季跑一次 + release 前必跑 +
> 重大 CVE 公告 trigger。dogfood：本 runbook 由 Q2 2026 audit
> ([#100](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/100))
> 直接 codify 而來，先有實作再 codify。

**互引**：
- Sandbox + Windows escape commit 路徑詳解 → [`windows-mcp-playbook.md`](windows-mcp-playbook.md) §修復層 C.1
- Branch + PR + commitlint 規範 → [`dev-rules.md`](dev-rules.md) #12
- CHANGELOG 寫入慣例 → [`CHANGELOG.md`](../CHANGELOG.md) v2.8.0 editorial guideline

---

## 1. When to run

**強制 (Must)**：
- ⛔ **Release 前必跑**（pre-tag）— 跟 `make pre-tag` / `make benchmark-report` 並列
- ⛔ **重大 CVE 公告當天** — 例：oauth2-proxy CVE-2026-34457 (CRITICAL auth bypass)、Go security release

**建議 (Should)**：
- 季 cadence（Q1/Q2/Q3/Q4）— Q2 2026 是首次完整 dogfood
- 任何 image base / Go toolchain / 主要依賴 minor bump 前

**Skip 條件**：
- patch-level bump 且 trivy 已驗 base tag 0/0
- 純 doc / config 改動

---

## 2. Pre-audit setup

### Trivy 工具鏈

不需在 host 安裝 trivy — 用 docker:

```bash
# Pull trivy + warm DB cache (~92MB, 5min TTL)
docker volume create trivy-cache
docker run --rm -v trivy-cache:/root/.cache/trivy \
  aquasec/trivy:latest image --download-db-only

# 一次性命令模板
docker run --rm \
  -v trivy-cache:/root/.cache/trivy \
  -v //var/run/docker.sock:/var/run/docker.sock \
  aquasec/trivy:latest image \
    --severity HIGH,CRITICAL \
    --quiet --scanners vuln --skip-db-update \
    --format table \
    <image:tag>
```

**版本記錄**：Q2 2026 用 `aquasec/trivy:0.70.0`，DB date 2026-04-26。每季跑時記下版本到 audit 摘要。

### 工作目錄

```bash
mkdir -p /tmp/cve-scan
# Reports 寫到 /tmp/cve-scan/<phase>-<image>.txt
# 每張 image 一個檔，方便 diff
```

---

## 3. Inventory phase

完整 grep pattern（每季都跑一次，盤點所有 version pin）：

```bash
# 1. Container base images
grep -rn "^FROM " components/ tests/ --include="Dockerfile*"

# 2. K8s manifest images
grep -rnE "image:.*:" k8s/ --include="*.yaml"

# 3. Helm values 與 templates
grep -rnE "(repository|tag):" helm/ --include="*.yaml"

# 4. Docker compose
grep -rnE "image:.*:" tests/ --include="docker-compose*.yml"

# 5. Go toolchain
grep -rn "^go " --include="go.mod"

# 6. Python deps
cat requirements*.txt 2>/dev/null
grep -E "PyYAML|promql-parser|croniter" components/*/Dockerfile 2>/dev/null

# 7. GitHub Actions（supply-chain）
grep -rnE "uses:" .github/workflows/ | grep -v "^#"

# 8. Pre-commit hooks
grep -E "^  rev:" .pre-commit-config.yaml
```

**輸出格式**：盤點結果寫成 markdown table，每行 `<file:path> — <name> <version>`。
Q2 2026 盤點輸出 72 個 pinned surface 作為起點。

---

## 4. Scan phase

### 4.1 雙比對方法（current vs target）

不要只 scan 當前 pin — 要同時 scan **bump target**。否則無法判斷「升上去能修多少」。

```bash
# 對每張 image：先 scan current
trivy image <image>:<current-tag> > /tmp/cve-scan/current-<image>.txt

# 再 scan 預期 target
trivy image <image>:<target-tag> > /tmp/cve-scan/target-<image>.txt

# Diff 計數
HIGH_CUR=$(grep -c "│ HIGH" /tmp/cve-scan/current-<image>.txt)
HIGH_TGT=$(grep -c "│ HIGH" /tmp/cve-scan/target-<image>.txt)
echo "$image: HIGH $HIGH_CUR -> $HIGH_TGT"
```

### 4.2 批次 scan script 模板

```bash
# scripts/scan-all.sh — 在 audit 機器上一次跑完
IMAGES=(
  "grafana/grafana:12.4.1|p0-grafana"
  "quay.io/oauth2-proxy/oauth2-proxy:v7.7.1|p0-oauth2proxy"
  ...
)
for entry in "${IMAGES[@]}"; do
  IFS='|' read -r img name <<< "$entry"
  trivy image --severity HIGH,CRITICAL --skip-db-update \
    --format table "$img" > "/tmp/cve-scan/${name}.txt" 2>&1
  H=$(grep -c "│ HIGH" "/tmp/cve-scan/${name}.txt")
  C=$(grep -c "│ CRITICAL" "/tmp/cve-scan/${name}.txt")
  printf "%-50s HIGH=%-4s CRITICAL=%-4s\n" "$img" "$H" "$C"
done
```

---

## 5. Reachability triage

**核心原則**：trivy 報出的 CVE ≠ 我們真正暴露的攻擊面。逐項 triage。

### 5.1 過濾條件（自動降級）

| Filter | 範例 CVE | 為什麼可降級 |
|---|---|---|
| **32-bit only** | CVE-2026-31789（OpenSSL heap overflow on 32-bit systems） | 我們部署 amd64，不可達 |
| **BSD-only** | CVE-2026-39883（otel-go BSD `kenv` path） | Linux 容器不執行 BSD path |
| **CMS / S/MIME path** | OpenSSL CVE-2026-28388/89/90 (NULL ptr in CMS) | nginx / 多數 client 不處理 S/MIME |
| **Image processing** | libpng / libjpeg / libavif CVEs | 純靜態 / API server 不解碼圖片 |
| **Specific data source / module** | pgx CVE-2026-33816（PostgreSQL）、libxslt（XSLT module） | 看 config，未啟用即 dead code |

### 5.2 Triage 紀錄格式

每個 CVE 在 PR body 與 issue 都要列：

```markdown
| CVE | Class | Reachable in our deploy? |
|---|---|---|
| CVE-2026-34457 | CRITICAL — health-check User-Agent bypass | ✅ public Ingress |
| CVE-2026-31789 | CRITICAL — OpenSSL heap overflow | ❌ 32-bit only, N/A on amd64 |
| CVE-2026-33816 | CRITICAL — pgx memory safety | ❌ no PostgreSQL data source（驗證見 configmap-grafana.yaml） |
```

### 5.3 Audit prerequisite pattern

**有條件可降級的 CVE 必須先驗證 config，再決定是否 bump**。Q2 2026 範例：
- #98 grafana：bump 前先確認 `configmap-grafana.yaml` 沒 PostgreSQL data source → pgx CRITICAL 降為 dead-code
- 把驗證寫進 issue acceptance criteria，PR body 引用驗證結果

---

## 6. ⚠ Built-image vs tag-scan distinction

**這是 Q2 2026 audit 的最重要 lesson**：scan base tag 的數字 ≠ 我們實際部署 image 的數字。

### 範例：#99 nginx 案例

Audit 報「nginx:1.28-alpine3.23 帶 13 HIGH + 2 CRITICAL」，但 `components/da-portal/Dockerfile` 已內建：

```dockerfile
FROM nginx:1.28-alpine3.23
RUN apk --no-cache upgrade && \
    apk --no-cache del nginx-module-image-filter nginx-module-xslt && \
    if apk info -e libavif 2>/dev/null; then exit 1; fi
```

實際 build 出來的 `da-portal` image：**0 / 0 clean**。

### SOP：自家 build 的 image 必須 scan built image，不是 base tag

```bash
# WRONG — 只 scan base
trivy image nginx:1.28-alpine3.23

# RIGHT — build 完再 scan
docker build -t da-portal-test -f components/da-portal/Dockerfile .
trivy image da-portal-test
```

如果自家 Dockerfile 已有 hardening layer（`apk upgrade` / module removal / 包升級），**不要為了 audit 數字假修**。先 build + scan 確認，若已乾淨：直接 close issue 並附 trivy proof（Q2 2026 PR 流程：#99 → 不 PR，issue 加 comment 後 close）。

### 第三方 image 才看 tag

只有 `grafana/grafana:*`、`prom/prometheus:*` 這類自己不 build 的 third-party image，才把 tag scan 當 ground truth。

---

## 7. Issue → PR cadence

### 7.1 Umbrella + sub-issues

每次 audit 開：
- **1 個 umbrella issue**：`security: Q<N> <YEAR> CVE audit umbrella — N actionable bumps`
- **N 個 sub-issues**：每個 actionable bump 一個

Sub-issue body 樣板（10 段固定結構）：

```markdown
## Context
（為什麼 bump、CVE 嚴重性、為什麼這個 target version）

## Affected files
- `path/to/file:line` — 當前 pin
- ...

## Trivy evidence
| Pin | HIGH | CRITICAL | 主要 CVE |
|---|---:|---:|---|
| current | N | M | ... |
| target  | 0 | 0 | clean ✅ |

## Reachability
（哪些可達、哪些不可達 + 為什麼）

## Acceptance criteria
- [ ] 所有 X 個 file 改完
- [ ] CI green: `pre-commit run --all-files`
- [ ] Local re-scan: trivy image <target> 0 / 0
- [ ] CHANGELOG entry under `## [Unreleased] — Security`

## References
- Audit run: <date>
- Upstream advisory link
```

### 7.2 Cross-link

```bash
# Sub-issues 都 ref 回 umbrella
for n in <sub-issue numbers>; do
  gh issue comment "$n" --body "Tracked in umbrella #$UMB — Q$N $YEAR CVE audit."
done

# Umbrella body 列所有 sub-issue 並用 - [ ] checkbox
```

### 7.3 合併 issue 的時機

⚠ **同一檔案被兩個 issue 改 → 合併成一個 PR**。

Q2 2026 範例：#96 (prometheus) + #97 (alertmanager) 都動 `tests/e2e-bench/docker-compose.yml`，分開兩 PR 第二個必 rebase conflict。**合併寫單一 PR、commit message + body 同時 `Closes #96` + `Closes #97`**。

---

## 8. PR mechanics

### 8.1 Title / scope / labels

```
chore(audit): bump <image> <old> -> <new> (<short reason>)
                ^^^^^
                必須是 audit（不是 security — 不在 commitlint scope enum）
```

Labels：`security` + `deps` + `P0|P1|P2`

P 分級：
- **P0**：reachable RCE / auth bypass / public-facing
- **P1**：reachable DoS / supply-chain / EOL
- **P2**：低 reachability / 需先驗證 / 殘留 OS layer 等 upstream

### 8.2 Commit + push 路徑

⛔ **不要直接 `git commit` + `git push`** — Windows pre-commit 會卡 `head-blob-hygiene`（FUSE phantom lock）。

⛔ **不要用 `--no-verify`** — hook 會擋。

✅ **Sandbox 路徑**（詳見 [`windows-mcp-playbook.md`](windows-mcp-playbook.md) §修復層 C.1 #3-#4）：

```bash
# Step 1 — Sandbox hook gate（dev container 跑 pre-commit）
SKIP=head-blob-hygiene PRECOMMIT_LOG=_sandbox_hooks_<n>.log \
  bash scripts/ops/run_hooks_sandbox.sh <files...>
# 期望輸出：HOOKS STATUS=PASS FILES=<n> DURATION=<s>s

# Step 2 — Windows native commit
# 寫 _msg.txt（UTF-8 without BOM；多行 / CJK / em-dash 都 OK，commit-file 走
# Python pipe 進 git commit -F -，不會被 cmd codepage 吃掉，見陷阱 #58）
# 推薦用 Write tool / heredoc 而非 echo（echo 對特殊字元不安全）
scripts/ops/win_git_escape.bat commit-file _msg.txt

# Step 3 — Push
scripts/ops/win_git_escape.bat push origin <branch>
```

`SKIP=head-blob-hygiene` 是必加，否則 Windows native pre-commit 在掃 940+ HEAD blob 時卡死（FUSE 慢 + dentry cache）。CI 端 Linux native 跑得起來，所以本地 skip 不影響品質閘門。

### 8.3 CHANGELOG entry 樣板

放在 `## [Unreleased]` 的 `### Security` subsection（沒有就新加）：

```markdown
- **<元件> <old> → <new>（v2.8.0, [#<sub>](url) of umbrella [#<umb>](url) Q<N> <YEAR> CVE audit）** —
  bump <files>。clear <CVE 列表>。Trivy 0.70.0 audit <date>：current N/M → target 0/0。
  <reachability 註記>。詳見 [#<umb>](url) audit 摘要。
```

每筆 3-5 行，避免敘事化（per CHANGELOG editorial guideline）。

---

## 9. Conflict patterns（多 PR 並行）

### 9.1 CHANGELOG `### Security` rebase chain

每個 audit PR 都加新 entry → 後 merge 的 PR 都會跟前一個 conflict。**已知 pattern，不是 bug**。

解法 SOP：

```bash
git fetch origin main
git checkout chore/<branch>
git rebase origin/main

# 每次都會在 CHANGELOG.md 同位置 conflict
# 解法：保留兩個 entry，按 merge 順序排
# （已 merged 的在上面，自己這次的接在後面）

# 編輯 CHANGELOG.md 移除 <<< === >>> 標記
git add CHANGELOG.md
git rebase --continue

# Force-push 需要 user 明確授權（hook 會擋）
git push --force-with-lease origin <branch>
```

### 9.2 同檔多次改動（k8s+helm 並行）

oauth2-proxy 這類 Helm + k8s 都 pin 的元件，常會 drift（Q2 2026 發現 v7.7.1 vs v7.15.1）。**單一 PR 一次對齊到同一 target**，不要分兩 PR 解 drift。

### 9.3 Auto-mode 下的 force-push 授權

Force-push 即便對 feature branch 也會被 hook 擋。需要 user 明確說「授權 force-push PR #<N> feature branch」（不接受「授權你」這種模糊話術）。事先告知並列出指令本體。

---

## 10. 收尾 / 後續

### 10.1 Umbrella close template

所有 sub-issue 解完後，在 umbrella 留 final comment：

```markdown
## ✅ Audit complete — all N actionable items resolved

**Timeline**: <date range>

| # | Issue | PR | Outcome |
|---|---|---|---|
| #X | ... | #Y | ✅ merged |

## CVE delta（trivy <ver> confirmed）
- HIGH: <sum-before> → <sum-after>
- CRITICAL: <sum-before> → 0 reachable

## Operational notes & follow-ups
1. <upstream rebase 等待中的 image>
2. <next audit 提醒>

Closing umbrella.
```

然後 `gh issue close <umb> --reason completed`。

### 10.2 Unreleased follow-ups（upstream 等待類）

某些 CVE 不能由我們修，要等 upstream rebase image：
- Grafana Alpine OS-layer（11 HIGH + 2 CRITICAL，等 Grafana team rebase）
- otel-go SDK kenv path（BSD-only，自動清）

**處理方式**：寫進 umbrella close 的 "Operational notes"，下季 audit 重 scan 時自動發現是否已 clear。**不要**留長期 open issue。

### 10.3 Doc 更新（本 runbook 自身）

每次 audit 完成 → 回頭更新本 runbook：
- 新踩到的坑加進 §9 conflict patterns
- 新發現的 CVE filter type 加進 §5 reachability triage
- trivy 版本 / DB date 更新到 §2

不另起 retrospective 檔；本 runbook 是 living document。

---

## 附錄 A：Trivy false-positive 速查

| 出現位置 | 為什麼 false-positive |
|---|---|
| OpenSSL CVE 標 "32-bit systems" | amd64 不可達 |
| otel-go SDK CVE-2026-39883 | BSD `kenv`，Linux 不執行 |
| pgx/v5 CVEs in Grafana | 沒配 PostgreSQL data source 即 dead code |
| libpng / libavif in static-server image | 不處理 image decode |
| moby authz CVE | 不直接呼叫 docker daemon 的服務不可達 |

## 附錄 B：常用 commitlint scope（audit 相關）

| Scope | 用於 |
|---|---|
| `audit` | CVE 升版（**首選**） |
| `tools` | 升 da-tools 內部 Python 套件 |
| `dx` | 升 devcontainer / pre-commit / 開發環境工具 |
| `ci` | 升 GitHub Actions / workflow 套件 |

⛔ `security` 不在 enum，會被 commitlint 拒絕。
