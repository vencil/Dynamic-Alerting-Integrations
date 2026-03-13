---
title: DX Tooling Backlog
lang: zh
---

# DX Tooling Backlog

> 已完成的平台功能與 DX 工具改善追蹤。
> 與產品 Roadmap（`architecture-and-design.md` §5）分開管理：§5 只放 forward-looking 方向。

## 狀態說明

- **候選** — 已識別但尚未排入
- **進行中** — 當前 iteration 正在實作
- **完成** — 已合併，標註完成 Round

---

## 候選

### validate_all.py `--diff-report`

Check fail 時自動產出 unified diff（expected vs actual），不用手動跑 generator 再 diff。

### check_doc_freshness.py Helm chart 版號檢查

目前只檢查 Docker image 版號，擴展到 `helm install/upgrade` 命令中的 `--version` flag 比對。

### generate_rule_pack_stats.py `--format summary`

產出單行摘要格式（如 `15 packs, 139 rec, 99 alert`），可嵌入 CLAUDE.md / README 作為 badge-like 引用。

### validate_all.py `--notify`

完成後發送桌面通知（配合 long-running `--watch` 模式），使用 OS native notification。

### check_doc_links.py 跨語言連結驗證

驗證 zh doc 引用的 en doc 確實存在（反之亦然），目前只做同語言內部連結。

---

## 完成紀錄

### 平台功能

| Round | 項目 | 摘要 |
|-------|------|------|
| R10 | Sharded GitOps — `assemble_config_dir.py` | SHA-256 衝突偵測、assembly manifest、多來源 conf.d/ 合併 |
| R10 | Assembler Controller — `da_assembler.py` | ThresholdConfig CRD → YAML 輕量 controller，watch / one-shot / offline render |
| R10 | ThresholdConfig CRD + RBAC | `k8s/crd/thresholdconfig-crd.yaml`、`assembler-rbac.yaml`、example CR |
| R10 | GitHub Pages Interactive Tools | `docs/interactive/index.html` + `jsx-loader.html` 改寫，瀏覽器端 JSX 載入 |

### DX 工具

| Round | 項目 | 摘要 |
|-------|------|------|
| R7 | generate_doc_map.py `--include-adr` | ADR 納入 doc-map，H1 title 萃取 |
| R7 | validate_docs_versions.py doc-file-count | 文件數自動驗證 + auto-fix |
| R7 | bump_docs.py `--what-if` | 232 rules 完整審計 |
| R8 | generate_cheat_sheet.py bilingual | `--lang zh/en/all` 雙語速查表 |
| R8 | check_doc_freshness.py false-positive fix | code-block-only 匹配 + stopword |
| R8 | check_translation.py cross-dir + lang fix | full-path pairing + empty-lang guard |
| R8 | validate_all.py `--profile` + `--watch` | CSV timing trend + file-watch polling |
| R9 | check_doc_freshness.py `--fix` | `.doc-freshness-ignore` 支援 |
| R9 | generate_rule_pack_stats.py `--lang` | `--lang zh/en/all` 雙語統計表 |
| R9 | check_includes_sync.py `--fix` | 自動建立缺失 .en.md stub |
| R9 | validate_all.py `--smart` | git diff → affected-check 自動跳過 |
