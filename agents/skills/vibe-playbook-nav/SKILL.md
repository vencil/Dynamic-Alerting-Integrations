---
name: vibe-playbook-nav
description: Route Vibe 任務到對應 Playbook 章節，避免通讀全文。Use when starting work that touches K8s, docker exec, release/tagging, conf.d, benchmark, Playwright E2E, Go race flake debugging, port-forward, Helm, PowerShell REST, or Windows-side git escape hatch. Also use when the user says "我要做 XXX，需要看哪份 Playbook？" or when unsure which Playbook section applies to the current task.
---

# vibe-playbook-nav — 任務分流 + Playbook 索引

Vibe 專案有五份 Playbook（`docs/internal/*-playbook.md`），每份數千行。不讀對章節直接動手是踩坑主因。本 skill 把任務類型對映到**具體章節**，30 秒找到上下文。

## 任務分流表

| 任務類型 | 必讀 | 選讀 | 跳過條件 |
|---------|------|------|---------|
| 跑 pytest / 新增測試 | [testing-playbook](../../../docs/internal/testing-playbook.md) 全文 + [test-map](../../../docs/internal/test-map.md) §Factory/Markers | — | 已熟悉 marker/fixture 慣例且非首次 |
| 修 Go test race / flake | testing-playbook §v2.6.x Go 並發測試 flake | — | — |
| 跑 benchmark / 效能分析 | [benchmark-playbook](../../../docs/internal/benchmark-playbook.md) 全文 | testing-playbook §負載注入 | — |
| docker exec / K8s 操作 | [windows-mcp-playbook](../../../docs/internal/windows-mcp-playbook.md) §核心原則 + §已知陷阱 | — | 只用 Cowork VM 跑 Python，不碰 docker |
| Release / 推 tag | [github-release-playbook](../../../docs/internal/github-release-playbook.md) 全文 | windows-mcp-playbook §PowerShell REST API | — |
| 新增 Python 工具 | testing-playbook §SAST 合規 + §程式碼品質 + test-map | — | 純修改現有工具（非新增） |
| 修改 conf.d/ 相關邏輯 | testing-playbook §conf.d/ YAML 格式陷阱 | — | — |
| **純文件修改** | **不需讀 Playbook** | — | ✅ pre-commit hooks 自動把關 |
| **純程式碼邏輯修改** | **不需讀 Playbook** | — | ✅ 不涉及 K8s/docker/release/conf.d |
| 負載測試 / Alert 驗證 | testing-playbook §負載注入 + §HA 相關測試 | benchmark-playbook §Under-Load | — |
| Playwright E2E | testing-playbook §Playwright E2E | — | — |
| 版號管理 / bump | github-release-playbook §版號驗證 + §da-tools 獨立 Release | — | — |
| FUSE 卡死需 Windows 逃生門 | windows-mcp-playbook §修復層 C + §Git 操作決策樹 | — | FUSE 正常運作時不需讀 |
| **git commit / push** | **不需讀 Playbook** | — | ✅ FUSE 正常時直接操作；卡住才查逃生門 |
| **PR merge 前收尾** | **不需讀 Playbook** | — | ✅ `make pr-preflight` 自動七項檢查 |

## Playbook 索引

| Playbook / Map | 涵蓋領域 |
|----------------|---------|
| [`testing-playbook.md`](../../../docs/internal/testing-playbook.md) | K8s 排錯、負載注入、程式碼品質、SAST、Playwright E2E |
| [`benchmark-playbook.md`](../../../docs/internal/benchmark-playbook.md) | Benchmark 方法論、執行環境、踩坑記錄 |
| [`test-map.md`](../../../docs/internal/test-map.md) | 測試架構：factories、markers、檔案對照、snapshot 工作流 |
| [`windows-mcp-playbook.md`](../../../docs/internal/windows-mcp-playbook.md) | Docker exec、Shell 陷阱、Port-forward、Helm 防衝突、PowerShell 環境、FUSE Phantom Lock、Windows 逃生門 |
| [`github-release-playbook.md`](../../../docs/internal/github-release-playbook.md) | Git push、Tag、GitHub Release、CI 觸發、PAT 權限 |

## Playbook 維護原則

Playbook 是 **living documents**，跟隨專案演進持續更新：

1. **Lesson Learned 回寫**：每次遇到新陷阱或發現更好做法，立即更新對應 Playbook（不是下次再說）
2. **知識退火**：LL 跨越兩個 minor 版本時強制三選一——固化為正式規範 / 標記 🛡️ 已自動化 / 歸檔至 `archive/`。`make playbook-freshness` 自動檢查各 Playbook 的 `verified-at-version` 欄位
3. **交叉引用**：Playbook 之間用相對連結互相引用，避免重複內容。環境層陷阱統一在 windows-mcp-playbook 維護
4. **全局 vs 領域**：CLAUDE.md 只放指引級摘要（指向哪個 Playbook），詳細步驟和陷阱清單放 Playbook 內
5. **驗證更新**：Playbook 內的數字（Rule Pack 數量、工具數量等）在版本升級時一併更新

## 使用法

1. 讀完任務分流表，找到當前任務對應列
2. 讀「必讀」欄位列出的 Playbook **章節**（不是整份）
3. 讀完再動手；不符「跳過條件」就不要略過
4. 遇到新陷阱 → 更新對應 Playbook + 更新此 skill 的分流表
