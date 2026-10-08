---
section: Changed
topic: confd-reader-consistency
issues: [2097]
created: 2026-10-07T18:02:30+00:00
---
- **`describe_tenant.py` 新增 `--replaces PATH`，`--what-if` 不再接受 conf.d 樹內既有檔（tools；[#2097](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2097)）**：⚠️ CLI 介面變更——沒帶 `--replaces` 卻把 `--what-if` 指向掃描清單上的既有檔（含 chain 上的 `_defaults.yaml`），現在回 rc 2。以前基準與模擬讀的是同一份 bytes，原地改值一律報「不 reload」（fail-open）；樹內不在 chain 上的檔則被整份當成新插入的 defaults 層。改用 `--what-if <修改後的副本> --replaces <樹內檔>`：基準讀樹內檔現況、模擬以副本取代它（chain carrier、根平台檔 `tenants:`／`profiles:`、租戶自己的檔），`substitution_type` 為 `substitute`、輸出新增 `replaces` 欄位，結果與 `da-guard effective` 對替換後的樹一致。`--replaces` 不是樹內既有檔、或與 `--what-if` 解析後是同一個檔，也回 rc 2。樹外的檔（`append-external`）與樹內不在清單上的路徑（`insert`）維持原行為。
