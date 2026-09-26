---
section: Removed
topic: dx-tools
issues: [1454, 1848, 1868, 1869, 1884, 1974, 1984, 1989]
created: 2026-09-26T17:00:00+00:00
---
- **零呼叫端、早已跑不動或從未實作的內部工具與資產退役（internal、dx）**：移除 `doc_impact.py`、`analyze_tier1_fp_rate.py`、`check_portal_i18n.py`、`check_glossary_coverage.py`、`check_i18n_coverage.py`、`check_doc_template.py`、`inject_related_docs.py`、`fix_doc_links.py`、`scaffold_jsx_dep.py`、`scripts/demo.sh` 與 E2E 場景腳本，連同 `make demo`／`demo-full`／`jsx-extract`／`test-scenario-*`；旗標 `generate_nav.py --update`、`doc_coverage.py --badge`、`sync_tool_registry --sync-frontmatter`、`verify_diff --write-map`／`--map` 一併移除（帶舊旗標會被拒），`verify_diff` 映射改為現場建置。repo 內零引用的 `social-preview.svg`、`badge-data.json` 與 portal JSX 的 `audience`／`tags` frontmatter 刪除。公開文件 `verified-scenarios` 的「端到端驗證通過」宣稱改為照實列出各場景的守護者與缺口。詳 [#1984](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1984)、[#1454](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1454)、[#1974](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1974)。
