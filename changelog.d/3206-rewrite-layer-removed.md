---
section: Removed
topic: confd-family
issues: [3206]
created: 2026-10-27T20:51:10+00:00
---
- **刪除 Python 端以 PyYAML 改寫 conf.d 的那一層（[#3206](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3206)，[ADR-036](docs/adr/036-single-parser-effective-config.md) 步驟 11）**：`_lib_yaml_keys`／`_lib_io` 的改寫用 loader 與 dumper 移除，模仿 yaml.v3 的 loader 不再讀任何 conf.d。⚠️ 可見變化：`da-crdecode` 的輸出多了 `differs_from_text`（Kubernetes 讀法與原文不同的鍵與字串），物件鍵改照原文順序；舊版 `da-crdecode` 會被 `da_assembler --render-cr` 以 rc 2 拒用。`--render-cr` 的原文比對改由它回答：`<<` 照 Kubernetes 的順序展開、`!!omap`／自訂標籤照 Kubernetes 的讀法 render（先前拒收），所在 mapping 有鍵讀法不同時其下一律拒收。`check_md_yaml_drift` 的 schema 檢查需要 da-guard，文件範例改經 `parse-tree` 讀（`yes` 是字串）；exporter 整份拒收的 conf.d 範例（多份文件、重複鍵、白名單外的標記）改判為違規（`--ci` rc 1），刻意示範的要加 ignore 標記並寫理由。
