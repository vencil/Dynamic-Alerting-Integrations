---
section: Changed
topic: rule-packs
issues: [3679]
created: 2026-11-07T10:32:33+00:00
---
- **runtime canary 的編譯規則改由編譯器寫出，落後即擋（rule-packs；[#3679](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3679)）**：canary 的 recipe 移到 `tests/rulepacks/runtime-canary-recipe/`；`runtime-canary.rules.yaml` 拆成編譯器整份寫出的 `runtime-canary.compiled.rules.yaml` 與手寫的 `runtime-canary.meta.rules.yaml`。`make runtime-canary-compile-check`（pre-commit 與 CI）重編比對，落後就紅，`make runtime-canary-compile` 重產。這次重產補上了先前漏掉的 `maintenance` label 與 `runbook_url`／`owner`／`tier` annotation。`compile_custom_alerts.py --check` 改為同時逐位元組比對（同名重複規則、group 欄位、檔頭也算落後）；`--out` 指向出貨 pack 以外的檔時，落後訊息與檔頭改為點名該檔與重產它的指令。
