---
section: Changed
topic: lint
issues: [3416]
created: 2026-10-30T05:30:00+00:00
---
- **文件裡的 ```yaml 範例改由單一清冊判定，區塊以 fence 屬性宣告自己是什麼（lint；[#3416](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3416)）**：`check_md_yaml_drift`（schema／fences／crd）、`check_threshold_unit_sanity` 與 Go 的 `_defaults.yaml` 載入測試都從 `scripts/tools/_lib_doc_yaml_inventory.py` 取區塊、消費者與讀法，不再各自判斷。寫法是 attr_list：```` ```yaml {.confd} ````、`{.k8s}`、`{.helm-values}`、`{.prom-rules}`、`{.alertmanager}`、`{.prometheus}`、`{.illustrative reason="…"}`、`{.confd .expect-refused=<kind>}`、`{.confd .expect-invalid}`；詞彙封閉，不認得的屬性、沒有大括號的寫法都報錯。⚠️ 破壞性：`<!-- md-yaml-drift: ignore — … -->` 標記退役，留著會報錯；沒宣告的區塊若 parse-tree 拒收（多份文件、重複鍵、語法錯誤；頂層不是 mapping 除外）一律報錯並要求宣告，先前兩支 lint 都放過的重複鍵區塊因此現形。三個 hook 與 unit-sanity 改為也需要 da-crdecode／da-read。
