---
title: Lessons Learned Archive
tags: [archive, lessons-learned, internal]
audience: [platform-engineers, sres, contributor]
version: v2.6.0
lang: zh
---

# Lessons Learned Archive

> **Purpose**: 歸檔 Playbook 中已不再適用或已固化為正式 SOP 的 lesson-learned 條目。保留歷史脈絡，避免主 Playbook 膨脹。

## 歸檔原則

依 `CLAUDE.md` §Playbook 維護原則 #2 知識退火 — lesson-learned 跨越兩個 minor 版本時強制三選一：

1. **固化為正式規範** — 加入 dev-rules.md 或 Playbook 主文
2. **標記 🛡️ 已自動化** — workaround 已由 pre-commit / CI hook 取代
3. **歸檔至本文** — 原陷阱已不可能再現（工具鏈升級、API 變更、環境遷移）

## 已歸檔條目

### GitHub Release Playbook

#### `v*` tag 觸發 exporter build

**狀態**: 🗄️ 已歸檔（v* 不再觸發 CI）

**歷史脈絡**: v2.4.x 時期，`v*` tag 會同時觸發 exporter CI build，導致 Multi-repo 場景下誤觸發 exporter pipeline。自 v2.5.0 起 `release.yaml` 已拆分 tag pattern（`v*` → platform、`exporter/v*` → exporter），此陷阱不再可能再現。

#### `mkdocs gh-deploy` 連續失敗

**狀態**: 🗄️ 已歸檔（workaround 已轉移至 Windows-MCP #29-30）

**歷史脈絡**: 早期 MkDocs gh-deploy 在 Windows 環境下因 PowerShell cp950 編碼導致連續失敗。Workaround 已固化為 Windows-MCP Playbook #29-30 的 PowerShell UTF-8 環境變數設定。

### Windows-MCP Playbook

#### GitHub Release `already_exists` 422

**狀態**: 🗄️ 已歸檔（PATCH 繞道已固化為 Re-tag SOP）

**歷史脈絡**: REST API `POST /releases` 在 tag 已存在時回傳 422 `already_exists`。Workaround：先 `GET /releases/tags/<tag>` 取 id，再 `PATCH /releases/<id>`。此 workaround 已固化為 github-release-playbook.md Re-tag 完整 SOP，不再需要作為 lesson-learned 追蹤。

### Testing Playbook

#### v2.1.0 Backstage Plugin 整合模式（#4-5）

**狀態**: 🗄️ 已歸檔（Backstage 整合已穩定，模式已固化）

**歷史脈絡**: v2.1.0 引入 Backstage plugin 整合。Entity annotation 和 proxy 模式已成為標準做法，不再需要作為 LL 追蹤。

- #4: Entity annotation 是 Backstage ↔ 外部系統的慣例橋樑：`dynamic-alerting.io/tenant` annotation 標註在 Backstage entity 上
- #5: Backstage proxy 避免 CORS 問題：PrometheusClient 透過 `/api/proxy/prometheus/` 路徑查詢

#### v2.1.0 DX Enhancement — `--diff-report` / `--format` 模式（#7-8）

**狀態**: 🗄️ 已歸檔（純功能記錄，不是陷阱）

**歷史脈絡**: 記錄 `--diff-report` 的 git restore 注意事項和 `--format summary` badge 風格。這是功能設計記錄而非踩坑教訓。

- #7: `--diff-report` 實作要注意 git restore：fix → diff → `git checkout .` 三步驟
- #8: `--format summary` badge 風格：一行輸出適合 CI badge

#### v2.1.0 Lint Tool — 反向驗證模式（#5-6）

**狀態**: 🛡️ 已歸檔（已固化為 `check_cli_coverage.py` lint rule）

**歷史脈絡**: 反向驗證（以 COMMAND_MAP 為 SOT 反查文件）模式已實作為 pre-commit hook `cli-coverage-check`，每次 commit 自動執行。

- #5: 🛡️ 反向驗證 > 正向驗證：以 COMMAND_MAP 為 single source of truth
- #6: Warning vs Error 分級：docs 裡多出的命令是 warning 非 error

## 維護原則

- **不刪除**：歸檔是保留，不是刪除。原條目仍可被搜尋與追溯。
- **雙向連結**：主 Playbook 的歸檔條目保留 `🗄️ 已歸檔` 標記與本文連結。
- **新增歸檔**：每個 minor release 後檢視 Playbook，依三選一原則決定去留。
