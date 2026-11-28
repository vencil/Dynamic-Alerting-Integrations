---
section: Changed
topic: ci
issues: [4224]
created: 2026-11-28T00:00:00+00:00
---
- **CI 建 Go binary 改用 main 的最新建置快取（[#4224](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4224)）**：建置 da-guard、da-read、da-crdecode、tenant-api 的 job 改走 `.github/actions/go-build-cache`。`setup-go` 自己的 cache 只以 `go.sum` 當 key，key 已存在就不再存，所以每個 job 還原的都是 `go.sum` 上次變動那天的快取，之後的 Go 變更在每個 job 都重新編譯。新 key 雜湊 Go 版本、`go_build.sh` 與各 module 的 `go.mod`／`go.sum`／`.go`；未命中時退回同 Go 版本最新的一份。只有 push 到 main 時跑的 `go-build-cache.yaml` 會寫入快取，而且只保留最新一份，避免擠掉其他快取。PR 只還原、不寫入。由 `tests/ops/test_go_build_cache.py` 守住。
