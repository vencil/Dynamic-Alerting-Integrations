---
title: "FUSE Cache 重建 Level 1–5 — 完整 RCA + 動機脈絡"
tags: [archive, automation-origins, windows-mcp, fuse]
audience: [maintainers, ai-agent]
codified-as: make fuse-reset (Level 1+3) + make session-cleanup (Level 4)
original-playbook: windows-mcp-playbook.md
codified-at-version: v2.8.0
status: archived
lang: zh
---

# FUSE Cache 重建 Level 1–5 — 原 RCA

> **⚠️ 本文件為 archive**：playbook 主檔（[`windows-mcp-playbook.md`](../../windows-mcp-playbook.md) §修復層 B）已壓成決策樹形式 + `make fuse-reset` / `make session-cleanup` 兩個 make target 為主。本 archive 保留每層原由與設計脈絡，供 automation 自身出問題時 debug、或 diagnose 新 FUSE 故障模式時參照。

## 適用情境

當檔案殘影 / phantom lock 反覆出現、`rm` 過的檔案還看得到、或 git index 與磁碟內容對不上時，按以下層次逐步重建（輕 → 重）。優先跑 `make fuse-reset`（自動串 Level 1 + Level 3），不行再依層級爬上去。

## Level 1 — Cowork VM 端 drop dentry/inode cache

```bash
sync
echo 2 | sudo tee /proc/sys/vm/drop_caches   # 需要 sudo；Cowork VM 常沒給
```

**作用**：只影響 VM 側的 kernel cache。無 sudo 時跳過，不影響後面層級。

**為何先試這層**：成本最低，若是 VM 側 dentry stale，重新 `ls` 即可解。Cowork VM 通常無 sudo，因此這層常常沒效——`make fuse-reset` 會嘗試但不依賴成功。

## Level 2 — Cowork UI 把 workspace unmount 再重選（最實用）

在 Cowork 桌面應用側邊欄把目前選取的資料夾取消，再重新選一次同樣的資料夾。這會讓 Cowork 重啟 FUSE driver 的 per-session state，等效於 FUSE userspace cache 冷啟動。

**統計**：9 成的殘影問題這一步就能解決。但需手動操作 Cowork UI，無法 scripted。

## Level 3 — Windows 端把壓住 inode 的 process 清掉

爛掉的 FUSE cache 多半是 Windows 上的 VS Code 或 Git for Windows 背景程序持續握著 file handle，讓 FUSE 以為檔案 busy → 快取無法驗證一致性。對應動作（`make fuse-reset` 自動跑 a/b/c）：

```powershell
# (a) 關 VS Code 背景 Git 掃描
python scripts/session-guards/vscode_git_toggle.py off

# (b) 清 stale .git/*.lock
bash scripts/session-guards/git_check_lock.sh --clean

# (c) 砍殘留的 port-forward / helm / kubectl / git process
Get-Process Code, git, pre-commit -ErrorAction SilentlyContinue | Stop-Process -Force
```

**為何 (a) 是 v2.8.0 後 PreToolUse hook 也做的事**：session-init.py 在 first tool call 時自動跑 (a)；Level 3 的 (a) 是 **手動 fallback** 給 hook 失效情境，並非冗餘。

## Level 4 — 整個 Session 重啟（核彈選項）

```bash
make session-cleanup
```

然後**關 Cowork 桌面應用**、重開、開新 session。這會重建 FUSE driver process 跟所有 kernel mount 狀態。

**何時用**：Level 1–3 都試過、Level 2 unmount-remount 也清不掉殘影。重啟 Cowork 等於 FUSE driver 完全 cold start，殘影會在新 session 消失。

## Level 5 — 深層診斷（最後手段）

用 Sysinternals `handle64.exe` 列出誰還握著 `vibe-k8s-lab/` 下的 file handle：

```powershell
# 下載 handle64.exe：https://learn.microsoft.com/sysinternals/downloads/handle
handle64.exe -accepteula -nobanner "vibe-k8s-lab"
# 找到 PID 後：
Stop-Process -Id <PID> -Force
```

若仍有殘影，跑 `chkdsk C: /scan`（唯讀掃描，不影響 FUSE）檢查底層 NTFS metadata 是否出錯。

**驗證重建成功**：`ls -la .git/ | grep -E 'lock|index'`（應該無 `*.lock`）+ `git status -sb`（應該無「殘影檔案」）。

## 為何 codify 成 `make fuse-reset` 而非 script-everything

- Level 1 / 3 是純自動化，可 scripted → `make fuse-reset` 串起來
- Level 2 / 4 / 5 涉及 Cowork UI 或 Windows admin 互動，無法純 sandbox-side 跑
- 因此 playbook 主檔留決策樹，archive 留每層原由——operational 路徑（`make fuse-reset` → unmount-remount → `make session-cleanup`）已可清晰指引，不需逐次重讀完整 RCA
