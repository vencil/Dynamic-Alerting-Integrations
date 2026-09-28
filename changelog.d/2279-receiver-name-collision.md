---
section: Fixed
topic: alertmanager-routing
issues: [2279]
created: 2026-09-28T08:32:20+00:00
---
- **`generate-routes` 擋下同名 receiver（alertmanager-routing；[#2279](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2279)）**：租戶 id 沒有字元限制，租戶 `<t>` 的 `routes[0]`／`overrides[0]` receiver 可能和另一個叫 `<t>-route-0`／`<t>-override-0` 的租戶的主 receiver 同名；以前 `--validate` 結束碼 0、validate-config 判 PASS，寫出的設定 Alertmanager 會拒載。現在兩個來源產生同名 receiver 時，所有模式都結束碼 1、不寫檔、不 apply，不分 `--strict`，訊息點名雙方來源；validate-config 與 `--validate` 共用同一個判定。`--output-configmap --base-config` 的 base 若有和產生的 receiver 同名的 receiver（以前 base 那份會靜默蓋掉 conf.d 那份），也改為結束碼 1、不寫檔；平台固定的四個 receiver 名稱不算。`--apply` 的叢集合併不變。
