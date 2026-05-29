---
title: "try-local Dogfood Runbook — cold-walk #1 (Windows / Git Bash)"
tags: [internal, onboarding, dogfood, try-local, runbook]
audience: [maintainers]
version: v2.8.1
lang: zh
status: living
related-issue: "#141 (Track A — try-local demo dogfood)"
---

# try-local Dogfood Runbook — cold-walk #1

> **這是什麼**：依 issue #141 Track A，maintainer 以「新訪客視角」**只照文件**冷走 try-local
> demo 路徑，記錄真實 timeline、每個摩擦點、以及 independence baseline。**不看 source、不靠
> 既有知識**——卡住即記為 friction。內部、maintainer-local；`docs/internal/**` 免雙語鏡像 / 不入 catalog。

## Run metadata

| 項目 | 值 |
|---|---|
| Run # | 1 |
| 日期 | 2026-05-29 |
| 走的路徑 | 完整 stack（`docker compose up -d`，6 服務 + 2 seed） |
| Host | Windows 11 · **Git Bash (MINGW64)** · 原生 `C:\` 路徑 |
| ⚠️ 環境偏離 | README「Windows / WSL2（必讀）」段**明確要求 WSL2 + Docker Desktop(WSL2 backend)、不要從 `C:\` 跑**。本次刻意以 Git Bash + 原生路徑走，量「文件指定環境外」的真實摩擦。WSL2 下的 independence 另估（見下）。 |
| Docker | 29.5.2 / Compose v5.1.3 |
| 進入點文件 | `README.md` §在本機試用 → `try-local/README.md`（#465/#466/#467 產） |

## Timeline（真實時戳，UTC）

| 時間 | 步驟（文件原話） | 結果 |
|---|---|---|
| 14:43:21 | 起手（讀 README hub → try-local/README） | — |
| 14:43:43 | `cp .env.example .env` | ✅ clean |
| 14:44:00→14:45:03 | `docker compose up -d`（**63s**） | ✅ 6 服務全 up、Prometheus healthy、`seed-git` one-shot exit；tenant-api 為前次 cached build（冷機約 +1min build） |
| 14:46:16 | 驗證鏈（等同 `make smoke-local` 的 5 檢查） | ✅ **全 PASS**（見下）；但 **canned 指令跑不動**——`jq` 缺 |
| ~14:47 | `git -C try-local/seed/conf.d log --oneline` | ✅ `c323ee4 try-local seed: initial tenant config` |
| ~14:47 | da-tools guard one-liner | ❌ **FAILED**（stale syntax + path mangling）→ 修正後 ✅ |
| 14:48:20 | `make clean-local` | ❌ `make: command not found` → raw `docker compose down -v` ✅ |
| 14:49:03 | 完成、零殘留、無 git mutation | ✅ |

**TTV（time-to-value）**：起手 → 看到 critical 紅燈 ≈ **< 3 分鐘**（含 stack 起 63s + alert `for:30s`/recording-rule 15s）。文件宣稱「< 1 分鐘」指核心雙星起來；看到 *firing* 告警實測 ~2–3min（文件排錯列已標「給它 ~1–2 分鐘」，一致）。

## 鏈路驗證（5 檢查，等同 smoke.sh）

`make smoke-local` 因缺 `jq` 跑不動 → 改用 `curl + py` 復刻同樣 5 個斷言，全 PASS：

1. Prometheus `:9090` — `MariaDBHighConnectionsCritical` **firing**（critical × 1）✅
2. tenant-api `:8080/api/v1/me`（帶 oauth2 headers）→ **200** ✅
3. browser path：portal `:8081/api/v1/me`（**無 header**，靠 dev-bypass 注入身分）→ **200** ✅
4. tenant-api `:8080/api/v1/tenants` → **2**（`cache-demo` + `db-demo`）✅
5. portal `:8081/` → **200** ✅

→ exporter→Prometheus→rule-pack fire-chain + tenant-api 授權閘 + portal proxy 全鏈在 Git Bash / 原生 `C:\` 路徑下**實際可跑**。

## Friction log

### F1 — 環境警告 vs 實際（minor，偏 over-warning）
- **現象**：README 要求 Windows 必用 WSL2、勿從 `C:\` 跑。實測 MINGW64 + 原生 `C:\` 路徑下，up / 驗證 / down happy path **全部正常**（唯一 bind mount `seed/conf.d` RW git commit 也正常）。
- **判定**：警告比實際保守。保守沒錯（bind-mount edge case 仍可能咬），但可能**過度勸退** Git Bash 使用者。
- **獨立性**：✅ 不影響（只是嚇人）。

### F2 — `jq` 未預裝，但只在排錯表才提（doc-gap）
- **現象**：headline 驗證 `make smoke-local` 需 `jq`；Git Bash 預設無 `jq` → 指令直接 fail。
- **doc 狀態**：README 排錯表有「找不到 jq → 安裝 jq」，但**不在 `make smoke-local` 旁的 prereq**，是反應式（先撞牆才看到）。
- **獨立性**：⚠️ 需越過文件主線（翻排錯表 / 裝套件）才過。

### F3 — da-tools one-liner **指令本身是壞的**（real bug，環境無關）🔴
- **文件原話**：`docker run ... ghcr.io/vencil/da-tools:${TOOLS_TAG} guard /conf.d`
- **現象**：`Error: unknown guard subcommand ... Available: defaults-impact`
- **根因（兩層）**：
  1. **stale syntax（環境無關，WSL2 也錯）**：v2.8.0 image 的 CLI 是 `guard <subcommand>`，唯一 subcommand 是 `defaults-impact`，且需 `--config-dir <path>`。文件的 `guard /conf.d`（把 `guard` 當吃 positional dir）**對不上 shipped CLI**。
  2. **Git Bash path-mangling**：MSYS 把容器內參數 `/conf.d` 改寫成 `C:/Program Files/Git/conf.d`。
- **修正後可跑**（實測 ✅，掃 2 租戶 0 error）：
  ```bash
  MSYS_NO_PATHCONV=1 docker run --rm -v "$PWD/seed/conf.d:/conf.d:ro" \
    ghcr.io/vencil/da-tools:v2.8.0 guard defaults-impact --config-dir /conf.d
  ```
- **獨立性**：❌ 照文件**必失敗**；要靠 source/CLI help 才修得出來。**這是 #141 Track A 定義的「real doc bug」**。

### F4 — `make` 未預裝（doc-gap）
- **現象**：`make smoke-local` / `make clean-local` 皆 `make: command not found`（Git Bash 預設無 make）。
- **fallback**：`docker compose ... down -v --remove-orphans`（即 `clean-local` target 包的東西）可代 cleanup；`bash try-local/smoke.sh` 可代 smoke（但仍需 jq）。
- **doc 狀態**：`make` 完全沒列為 prereq。
- **獨立性**：⚠️ 兩個 headline 指令（驗證 + 清理）都卡 make。

## Independence baseline

逐「文件主線步驟」算「**只照文件、不看 source、不問人**」能否獨立完成：

| # | 步驟 | 獨立？ |
|---|---|---|
| 1 | `cp .env.example .env` | ✅ |
| 2 | `docker compose up -d` | ✅ |
| 3 | 開 4 個 URL 看到對應畫面 / 紅燈 | ✅ |
| 4 | `make smoke-local`（驗鏈） | ❌（缺 jq + make） |
| 5 | `git -C seed/conf.d log`（GitOps 體感） | ✅ |
| 6 | da-tools guard one-liner | ❌（F3 stale syntax） |
| 7 | `make clean-local`（清理） | ❌（缺 make） |

- **本次環境（Git Bash，文件指定環境外）**：4 / 7 ≈ **57%** → **未達 ≥80% 門檻**。
- **文件指定環境（WSL2）推估**：F2/F4（jq/make）apt 即得、F1/path-mangling 不發生 → 僅 **F3（da-tools stale syntax）** 為環境無關真壞 → ~6 / 7 ≈ **86%** → **達標**。

**結論**：≥80% independence **只在 README 指定的 WSL2 環境成立**。文件正確地 mandate WSL2，但 (a) 沒給 Git-Bash fallback、(b) `jq`/`make` 未列 up-front prereq、(c) da-tools one-liner 連 WSL2 都是壞的。

## 待修 doc gaps（→ 開 PR，同 v2.9.0 cycle）

| ID | 檔案 | 修法 | 優先 |
|---|---|---|---|
| F3 | `try-local/README.md` §想試 da-tools | `guard /conf.d` → `guard defaults-impact --config-dir /conf.d`；補 Git Bash 的 `MSYS_NO_PATHCONV=1`（或 `//conf.d`） | **高**（指令本身壞，環境無關） |
| F2+F4 | `try-local/README.md` | 在 `make smoke-local` 旁明列 prereq `make` + `jq`；或給無 make 環境 fallback（`bash try-local/smoke.sh` / `docker compose down -v`） | 中 |
| F1 | `try-local/README.md` §Windows | 可選：註明 happy path 在 Git Bash 也能跑（只是不保證 bind-mount edge case），降低過度勸退 | 低 |

## Review 擴充 — 全命令面 F3-class 審計（vibe-subagent-review 觸發後）

happy-path 只撞到我走過的指令；為避免漏掉同類 stale 指令，對整個 onboarding 命令面逐條對 shipped v2.8.0 image 實跑驗證：

| 來源 | 指令 | 結果 |
|---|---|---|
| `try-local/README` | `da-tools … guard /conf.d` | ❌ **F3**（唯一真 bug） |
| `da-tools/app/QUICKSTART:23` | `guard defaults-impact --config-dir … --cardinality-limit 50` | ✅ 實跑：red-flag over-budget tenant + exit 1，與文件「你會看到」一致 |
| `da-portal/QUICKSTART:21` | `docker run -p 8080:80 da-portal:v2.8.0` | ✅ image 存在、語法正確 |
| `tenant-api/QUICKSTART:42` | `docker run … tenant-api:v2.8.0`（TA_RBAC_PATH + 真 header） | ✅ image 存在；/me 帶 header→200 已由 try-local smoke 證實同一 handler |
| `hands-on-lab` | `da-tools init` / `:latest` | ✅ `init` 為真實（hidden）wizard 命令、`:latest` image 存在——**非 stale** |
| image 存在性 | da-portal / tenant-api / da-tools（v2.8.0 + latest） | ✅ 全部 `manifest inspect` 通過 |

**審計結論**：onboarding 命令面**只有 F3 一個真 bug**，且根因明確——`da-tools` QUICKSTART（#466，已 dogfood 驗證）語法正確，是 `try-local/README` 的 one-liner 自己 drift 成猜測語法。其餘命令、4 個 QUICKSTART、hands-on-lab、所有 published image 皆 clean。fix blast radius 小、無散落 staleness。
> 副帶觀察（非本 repo doc bug）：`da-tools --help` 頂層清單**沒列出** `guard` / `init`（兩者實際可跑）——image 的 help 完整度問題，另案。

## 已套用的 doc 修正（本 cycle）

- **F3** `try-local/README.md`：`guard /conf.d` → `guard defaults-impact --config-dir /conf.d` + Git Bash `MSYS_NO_PATHCONV=1` 註記。
- **F2+F4** `try-local/README.md`：smoke/clean 指令旁標 `make` 依賴 + 無-make fallback（`bash try-local/smoke.sh` / `docker compose down -v --remove-orphans`）。
- **F1**：審視後**刻意不改**——保守的 WSL2 警告可接受，弱化反而可能誘發 bind-mount edge case。

## 可抽進 `docs/scenarios/` 的通用段（#141 共用產出）
- 「5 檢查 smoke 斷言清單（critical firing + /me 兩路徑 + tenants==2 + portal 200）」是任何 onboarding/部署驗收都可重用的 payload-level 健檢樣板 → 候選抽成 generic「fire-through 驗收 checklist」。

## 復現
```bash
cd try-local && cp .env.example .env && docker compose up -d   # ~1–3min 看到紅燈
# 驗證（WSL2/有 jq+make）：make smoke-local
# 清理：make clean-local   (無 make: docker compose down -v --remove-orphans)
```
