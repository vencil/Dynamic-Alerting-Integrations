---
title: "Trap #60 原 RCA — `generate_doc_map.py` FUSE fsync 中斷"
tags: [archive, automation-origins, windows-mcp, fuse]
audience: [maintainers, ai-agent]
codified-as: PR #56
original-playbook: windows-mcp-playbook.md
codified-at-version: v2.8.0
status: archived
lang: zh
---

# Trap #60 — FUSE fsync 中斷造成 regen 工具半寫檔案

> **⚠️ 本文件為 archive**：trap 已由 `scripts/tools/dx/_atomic_write.py` + regen 工具 `--safe` flag codified（PR #56, v2.8.0）。實戰請看 [`windows-mcp-playbook.md`](../../windows-mcp-playbook.md) 已知陷阱清單第 60 列的精簡版。
> 本 archive 保留原 RCA 作為 debug 參考：若 automation 自身出 bug、若需追溯設計脈絡、或若規則精神需驗證時閱讀。

## 情境

長 I/O regen 工具（`generate_doc_map.py` scan 所有 `.md`、`generate_tool_map.py` scan 所有 `scripts/tools/**/*.py`）在 FUSE-backed workspace 執行時，被 context-compaction 的 write-cache drop 在寫到一半時中斷，造成：

1. **輸出檔半寫半空** — `docs/internal/doc-map.md` / `tool-map.md` 內容截斷或全空
2. **`git status` 全面錯亂** — 整份 repo 的已追蹤檔被誤報成 "new file"（index metadata 被污染）

## 表現（diagnostic signals）

- `git status` 印數百行 `new file: docs/...`，但 `git diff --stat` 對這些檔的實際內容未動
- `wc -c <regen-output>` 遠小於預期（或為 0）
- `git update-index --refresh` 會自動補正一部分，但 index metadata drift 持續

## 根因

FUSE 寫 cache（Windows filesystem bridge）在 context compaction 時把 in-memory dirty pages drop 掉但 `fsync` 未落盤。Python `open("w") + write() + close()` 的 buffered I/O 在 close 時才 flush，cache drop 剛好打斷 flush 視窗，檔案呈半寫狀態；同時 git index 的 `stat(2)` metadata 在 ~1 分鐘視窗內出現 mtime / size 異常，觸發 git "new file" 誤判。

## 修法（long-term, codified PR #56）

`scripts/tools/dx/_atomic_write.py::atomic_write_text()`：
```python
def atomic_write_text(path, content, *, encoding="utf-8", newline="\n", mode=0o644):
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding=encoding, newline=newline) as fh:
        fh.write(content)
        fh.flush()
        if os.name == "posix":
            os.fsync(fh.fileno())  # durability barrier
    os.chmod(tmp, mode)             # umask-race-free permission
    os.replace(tmp, path)           # atomic rename(2)
```

原子搬檔的鍵值：`os.replace` 在 POSIX 走 `rename(2)` — 無論中途怎樣 crash，target path 只看到舊 inode 或新 inode，不會看到半寫狀態。

整合：`generate_doc_map.py --generate --safe`、`generate_tool_map.py --generate --safe`（opt-in，legacy 路徑保留）。Byte-identity verified（14967 bytes → 14967 bytes）。

## 出事救援（short-term，若尚未走 `--safe`）

| 嚴重度 | 指令 |
|---|---|
| 僅 index metadata 亂 | `git reset HEAD -- .`（unstage 所有誤報 new file） |
| index metadata 外加實際檔未改完整 | `git update-index --refresh` |
| HEAD corruption + index 亂 | `make recover-index`（v2.8.0 PR #44 plumbing 逃生門） |

## 原 playbook 歸檔入口

v2.8.0-planning §12.4 #5（historical trap ledger，maintainer-local / gitignored）。

## 相關 trap 交叉引用

- **Trap #57** — `head-blob-hygiene` hook 長時間無 output（同 FUSE 問題不同表徵）
- **Trap #59** — `.git/HEAD` NUL-byte 填充（同 FUSE cache drop 的另一種呈現）
- **Trap #62** — Dev Container 只掛主 worktree（無 automation 可救，需人工同步）
