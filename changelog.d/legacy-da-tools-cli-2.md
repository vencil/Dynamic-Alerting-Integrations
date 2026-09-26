---
section: Fixed
topic: da-tools-cli
issues: [1379, 1380, 1406, 1447, 1448, 1461, 1468, 1469, 1522, 1523, 1524, 1525, 1556, 1616, 1618, 1625, 1641, 1651, 1654, 1789]
created: 2026-09-26T17:00:00+00:00
---
- **⚠️ da-tools 不再「什麼都沒驗到卻回綠燈」（exit-code 契約）**：旗標有給但值不可用（路徑不存在、讀不到、非 UTF-8、壞 YAML、頂層不是 mapping）時，`validate-config --policy`／`--rule-packs`／`--policy-dsl`、`generate-routes --policy`、`lint`、`backtest --lookback`／`--git-diff`、`evaluate-policy --policy`、`policy-engine --config-dir` 由 `0` 改為 `2` 並指名該值；省略旗標仍是合法的略過，不要靠拿掉旗標轉綠。文件裡把網域清單直接傳給 `--policy` 的範例已改成政策 YAML 的 `allowed_domains:`。`validate` 比對出 mismatch／missing 由 `0` 改為 `1`、零比對組回 `2`；`validate-config` 對讀不進來的 conf.d 路徑改判 FAIL 並具名，`diagnose` 不再靜默截斷繼承鏈。輸出路徑寫不進去、直接讀取的 YAML 讀不進來（lint 類與 `deprecate_rule` 除外）、未知子命令改回 `2`（原多為 traceback 或 `1`）。exit-code、`--json`、`--ci`、`--dry-run` 與雙語 `--help` 五項 CLI 契約改由行為型測試守住（[#1556](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1556)、[#1469](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1469)、[#1406](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1406)）。
