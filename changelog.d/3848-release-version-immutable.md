---
section: Changed
topic: release
issues: [3848]
created: 2026-11-13T22:40:00+08:00
---
- **已發布的版號不可變（release.yaml；[#3848](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3848)）**：每個發布動作（簽章、promote、`helm push`）之前，`check_release_immutable.py` 先問 registry。映像正式 tag 已存在時讀它的 `org.opencontainers.image.revision`：是這次的 commit 就不重新 promote，不是或沒有 label 就失敗。chart 版號已存在時與這次的封包逐檔比對（Chart.yaml 以 YAML 比），相同就不重推，不同就失敗並列出檔案。只有 registry 回 404 才算第一次發布，其他回應一律失敗。所有 release build 都寫入 revision label。⚠️ 移除 `make chart-push`；已發布的版號要修正只能發新版，見 release playbook 陷阱表 #14。
