---
section: Changed
topic: release
issues: [3848]
created: 2026-11-13T22:40:00+08:00
---
- **已發布的版號不可變（release.yaml；[#3848](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3848)）**：寫 registry 與 GitHub Release 只經 `release_publish.py`，檢查在發布動作裡面。映像正式 tag 已存在時讀 `org.opencontainers.image.revision`：是這次的 commit 就什麼都不寫（`latest` 也不動），否則失敗；要發布時 `latest` 先寫、版號最後寫。chart 已存在時逐檔比對（Chart.yaml 以 YAML 比），相同不推、不同失敗。Release 資產不再 `--clobber`：全在不傳、只有一部分就失敗。只有 registry 回 404 才算第一次發布（新 repository 帶 `GITHUB_TOKEN` 回 `404 MANIFEST_UNKNOWN`，2026-11-13 實測），其他回應一律失敗；同一個 tag 的 run 排隊不並行。⚠️ 移除 `make chart-push`；已發布的版號要修正只能發新版，見 release playbook 陷阱表 #14。
