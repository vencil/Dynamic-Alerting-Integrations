---
section: Changed
topic: da-tools
issues: [3276]
created: 2026-10-29T00:48:54+00:00
---
- **呼叫端給的路徑改由同一個判定回答（ops 與 dx 工具；[#3276](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3276)）**：`--config-dir`、`--conf-d`、`--policy`、`--what-if`、`--state` 等呼叫端給的路徑位於執行者進不去的上層之下時，不再以 traceback 或「not found」結束，而是指名旗標、路徑與原因；原因的措辭跟著該行的語言走（中文工具與 `DA_LANG=zh` 下的雙語工具說「不存在」「無法存取（EACCES）」「不是目錄」，不再夾英文 strerror），文件形狀不變。`--json` 的 reason 改為 `{dir,file,path}_not_{found,accessible}`。**Breaking**：policy-engine 的 `config_dir_not_found`、`policy_file_not_found` 改為 `dir_not_found`、`file_not_found`。結束碼不變（tenant-verify 改為 2，見 [#3393](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3393)），以下除外：reword-chain 的 mapping 檔、write-baseline-marker 的 `--baseline` 不存在或不是一般檔由 1 改為 2；analyze-probe 的 `--from-log` 是目錄或讀不到、`--archive` 讀不到由 traceback（1）改為 2；doc-coverage 的 `--repo-root` 是檔案時由 0 改為 2。
