---
section: Fixed
topic: da-tools
issues: [3983]
created: 2026-11-22T03:00:00+00:00
---
- **`generator-service` 的 `--work-dir` 不可用時，錯誤訊息點名旗標；`--socket` 被拒絕時不再先清空工作目錄（[#3983](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3983)）**：`--work-dir` 是個檔案、路徑中途是檔案、或它底下的 `jobs` 不是目錄時，服務照舊拒絕啟動（結束碼 `2`、不移除既有的東西），訊息由 `[Errno 17] File exists: '…/jobs'` 改為 `--work-dir <路徑>: cannot be used as the work directory: …`。`--socket` 的位置上有不是 socket 的東西、它的目錄不存在、或各項上限互相矛盾時，拒絕改在動到 `--work-dir` 之前發生（先前 `jobs` 已被清空才拒絕）。`jobs` 清不掉時，訊息帶的是真正的原因，不再是 `File exists`。旗標清單（`caller_path_manifest.yaml`）新增 `runtime` 這種 kind：工具為了自己執行而建立、不是留給人讀的結果，由指名的測試守住「路徑不可用時的行為」；`--socket` 與 `--work-dir` 改標為它，`output` 旗標的例外清單隨之移除。
