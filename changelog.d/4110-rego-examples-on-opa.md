---
section: Added
topic: ci
issues: [4110]
created: 2026-11-21T01:27:18+00:00
---
- **opa-evaluate 文件的 rego 範例在 CI 上以真的 OPA 求值（ci；[#4110](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4110)）**：`docs/cli-reference.md`／`.en.md` 以 `<!-- rego-example: <名稱> -->` 標記的 rego 區塊，在 Lint Documentation job 對 `tests/fixtures/opa/<名稱>/` 的樹跑 `--dry-run` 的 input 求值，violations 須與 `expected.json` 相同；中英兩份的範例須逐字相同，每個範例都要有 fixture。OPA 的版本與 checksum 只寫在 `tests/fixtures/opa/opa.env`，CI 從它下載並驗證。文件原本內嵌、沒驗過的範例改成兩段完整範例：時段值高於門檻（`input.served`）與有設定但整天沒送出（`input.unserved`）。
