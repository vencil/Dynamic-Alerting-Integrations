---
section: Added
topic: da-tools
issues: [3206]
created: 2026-10-28T01:10:00+00:00
---
- **`da-read`（`alertmanager`／`prometheus`／`prometheus-rules`）與通用化的 `da-crdecode` 進 da-tools 映像（[#3206](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3206)，[ADR-036](docs/adr/036-single-parser-effective-config.md) §12）**：`da-read`（獨立 Go module `components/da-read`）以消費者自己的載入函式讀檔，只印原文寫了的鍵、值是消費者的讀法、密鑰照原值，載入函式搬走的鍵印在搬去的位置（⚠️ `bearer_token`／`bearer_token_file` 印成 `authorization: {type: Bearer, credentials|credentials_file}`，不再印出被清空的原鍵），拒收或寫了卻對不上的鍵一律 exit 2：`alertmanager FILE|-` 用 Alertmanager 的 `config.Load`（版本同映像裡的 amtool），`prometheus FILE|-` 用 Prometheus 的 `config.Load`（註冊全部服務探索），`prometheus-rules FILE|-...` 用 `rulefmt.Parse`，一次讀多檔、逐檔回 `problem`；Prometheus 版本同部署的 Prometheus，兩者都有釘值測試。⚠️ 服務探索帶進各家雲端 SDK 與 Kubernetes client，映像因此變大。`da-crdecode` 改照 `kubectl apply -f` 切文件（多份文件、空文件略過、`-` 讀標準輸入）；⚠️ 空檔現在是零份文件。repo 內以 `make da-read-build`／`make da-crdecode-build` 建到 `.build/`。
