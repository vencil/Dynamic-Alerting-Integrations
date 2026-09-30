---
section: Fixed
topic: da-tools
issues: [1444]
created: 2026-09-29T16:10:00+00:00
---
- **`da-tools init --ci gitlab` 產出的 GitLab job 呼叫得到工具了（issue 1444）**：這些 job 清掉映像的 ENTRYPOINT 後，在 shell 裡經由 `/usr/local/bin/da-tools` 這個 symlink 呼叫 `da-tools <cmd>`，而 dispatcher 以 symlink 所在目錄找工具腳本，所以每個子命令都回 `Tool script not found`、結束碼 2（在真 GitLab runner 上實測；已發布的 v2.9.0 映像同樣如此）。改為先解開 symlink 再找工具。`docker run` 直接呼叫映像的用法不受影響。
