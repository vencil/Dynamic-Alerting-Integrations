---
title: "Level 6 — Cowork VM rename-trick（FUSE phantom lock 終極繞道）"
tags: [archive, automation-origins, windows-mcp, fuse]
audience: [maintainers, ai-agent]
codified-as: Trap #44 + scripts/ops/win_git_escape.bat
original-playbook: windows-mcp-playbook.md
codified-at-version: v2.8.0
status: archived
lang: zh
---

# Level 6 — Cowork VM 內的 rename-trick

> **⚠️ 本文件為 archive**：本 recovery vector 已被 [Trap #44](../../windows-mcp-playbook.md#已知陷阱速查) 取代 — 「Phantom lock 薛丁格態下唯一可靠解法是 win_git_escape.bat 走 Windows 原生 git」。本 archive 保留作為歷史 RCA 與設計脈絡：若 Windows 逃生門也壞時的最後手段、或新踩 FUSE phantom 案例的對照組。
>
> **實戰請走**：`scripts/ops/win_git_escape.bat` / `make win-commit`，見主 playbook §修復層 C.

## 觸發情境

2026-04-10 遇到的案例：Cowork 桌面無法重選資料夾、沒有 PowerShell、沒有 docker、沒有 sudo。phantom `.git/index.lock`（inode `7599824371576445`）被 stat/exists 看見，但 `ls`、`open`、`unlink`、`shutil.copy` 全部 ENOENT 或 EPERM。同時 `os.unlink` 在整個 `.git/` 下都回 EPERM（FUSE 層 block unlink）。

當 Level 1（drop_caches）/ Level 2（unmount-remount）/ Level 4（session-cleanup）/ Level 5（handle64.exe）全部不可用，且也沒有 Windows-MCP 可走逃生門時，rename-trick 是最後招式。

## 關鍵觀察

**CREATE 仍可以成功、RENAME 也可以成功** — 即使 phantom dentry 把 unlink 全擋掉。於是可以繞過：

```python
import os
# (1) 建一個其他名字的檔案
fd = os.open('.git/_scratch.tmp', os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
os.close(fd)

# (2) 把它 rename 到 phantom 路徑 — rename 會 override 掉 phantom dentry，
#     讓 .git/index.lock 變成一個真正存在的 0-byte 檔案
os.rename('.git/_scratch.tmp', '.git/index.lock')

# (3) 再 rename 走 — 此時 .git/index.lock 已是真檔，rename 成功後 dentry 消失
os.rename('.git/index.lock', '.git/_old_lock.tmp')

# (4) 驗證 phantom 已清除
assert 'index.lock' not in os.listdir('.git')
assert not os.path.exists('.git/index.lock')

# (5) 測試 git 的 O_CREAT|O_EXCL 現在可以用
fd = os.open('.git/index.lock', os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
os.close(fd)
os.rename('.git/index.lock', '.git/_old_lock2.tmp')  # 讓 git 可以自己 acquire
```

清理殘留的 `.git/_old_lock*.tmp` 需要等下次 Level 2/4 cold-restart — 這些 0-byte 檔案不影響 git 操作。

## 為何 rename 可行

FUSE 的 rename 走 `create+unlink` path 的相反操作（由 userspace driver 代為執行 NTFS 層的 `MoveFileEx`），而 Windows 的 `MoveFileEx` 在 phantom dentry 情況下會對齊到真實 NTFS 狀態，等於強制 dentry 重新 validate 一次。同理，`O_CREAT|O_EXCL` 在 phantom dentry 下會 EEXIST，但 rename-over 不會。

## 為何 superseded

- Windows 逃生門 `scripts/ops/win_git_escape.bat` 走 Windows 原生 git，從根本繞開 FUSE phantom — 不需在 phantom dentry 下做任何巧妙繞道。
- v2.8.0 PR #44 加入 `scripts/ops/fuse_plumbing_commit.py` / `make recover-index` 兩個更上游的 plumbing 路徑，覆蓋更多 phantom-lock 失效情境。
- rename-trick 留下的 `_old_lock*.tmp` 需 cold-restart 才能清，操作面卡 follow-up 動作；逃生門無此尾巴。

只有在 Windows-MCP 也不可用（極罕見：FUSE 已壞 + Windows 端 PowerShell 也無回應）才會落到 rename-trick — 此時更實際的選擇是直接重啟 Cowork session。
