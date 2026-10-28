---
section: Changed
topic: confd-family
issues: [3206]
created: 2026-10-28T01:11:00+00:00
---
- **`onboard_platform` 與 `check_retire_drift` 改以消費者的讀法讀檔（[#3206](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3206)，[ADR-036](docs/adr/036-single-parser-effective-config.md) §12）**：⚠️ `onboard_platform` 的 Alertmanager 設定改經 `da-crdecode`（ConfigMap 外層）與 `da-read alertmanager`（設定本身）讀，需要這兩支（`--da-crdecode-binary`／`--da-read-binary`、`$DA_CRDECODE_BINARY`／`$DA_READ_BINARY` 或 PATH）。值改用 Alertmanager 的讀法（matcher 正規化、duration 照 Alertmanager 的寫法），只抄原文寫了的 receiver 欄位；Alertmanager 會拒收的設定（例如只有 `receivers`、沒有 `route`）改為 rc 2 並印出原因，不再當成可分析。⚠️ `check_retire_drift` 的 Namespace manifest 改經 `da-crdecode` 讀，未加引號的數字 label（`instance: 010` 讀成 8）kubectl 會拒收，現在報錯（rc 2，點名檔案與文件序號、要求加引號），不再取原文。
