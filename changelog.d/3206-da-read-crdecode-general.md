---
section: Added
topic: da-tools
issues: [3206]
created: 2026-10-28T01:10:00+00:00
---
- **`da-read alertmanager`、`da-crdecode` 通用化，兩支都進 da-tools 映像（[#3206](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3206)，[ADR-036](docs/adr/036-single-parser-effective-config.md) §12）**：新的 `da-read alertmanager FILE|-`（獨立 Go module `components/da-read`）以 Alertmanager 自己的 `config.Load` 讀設定，只印原文寫了的鍵、值是 Alertmanager 的讀法、密鑰照原值；Alertmanager 拒收或原文寫了卻在讀法裡找不到的鍵一律 exit 2。它編進的 Alertmanager 版本等於映像裡 amtool 的版本（釘值測試守住）。`da-crdecode` 改照 `kubectl apply -f` 切文件：一個檔可有多份文件、空文件略過、`-` 讀標準輸入；⚠️ 空檔現在是零份文件，結尾的 `---` 不再被拒收。repo 內以 `make da-read-build`／`make da-crdecode-build` 建到 `.build/`。
