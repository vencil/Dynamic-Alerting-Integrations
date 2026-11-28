---
section: Changed
topic: da-tools
issues: [3981]
created: 2026-11-28T21:11:15+08:00
---
- **⚠️ breaking：`generator-service --work-dir` 必須是私有目錄，底下的寫入改經 runtime 入口（[#3981](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3981)）**：`--work-dir` 不存在時以 `0700` 建立；已存在的必須屬於執行身分（euid）、group 與 others 沒有任何權限位，且不可是 symlink，否則結束碼 `2` 拒絕啟動，訊息點名 `--work-dir` 與原因，`jobs/` 不清空。原本不論擁有者與權限都照用，symlink 也照著走。工作目錄底下的目錄一律 `0700`、檔一律 `0600`，檔先刪除再以 `O_EXCL|O_NOFOLLOW` 建立，不會經 symlink 寫出。tenant-api chart 的 `--work-dir` 改為 emptyDir 掛載點底下的 `/var/lib/da-generator/work`：emptyDir 的根目錄屬於 root 且開放寫入，會被拒絕。自行部署時把 `--work-dir` 指到掛載點底下的子目錄。判定的結果、HTTP 狀態與結束碼不變。
