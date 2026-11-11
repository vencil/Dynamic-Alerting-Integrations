---
section: Changed
topic: ci
issues: []
created: 2026-11-04T12:29:19+00:00
---
- **⚠️ BREAKING：Python 版本收成單一來源 `.python-version`，並升到 Python 3.14（3.14.10），映像跟著 CI 的依賴版本**：
  - CI 的 setup-python 一律讀 `.python-version`；Dockerfile 的 `FROM python:`、devcontainer 與 YAML 裡的 python 映像都必須同版，由守衛擋下不一致。
  - CI、devcontainer 與所有映像的 Python 由 3.13 升到 3.14.10（3.13 已只收安全修補）。3.14 的 `io.DEFAULT_BUFFER_SIZE` 由 8 KiB 改為 128 KiB，讀端已關閉的 stdout 較晚才報錯，結束碼仍是 2。
  - da-tools 映像的依賴版本改為與 `ci-constraints.txt` 相同（守衛擋下不一致）：promql-parser 0.7.0 → 0.10.0，即 CI 測的版本。
  - CI 名稱不再帶版本：required check 由 `Python Tests (3.13)` 改為 `Python Tests`，branch protection 需同步更新；coverage artifact 改名為 `coverage-python`。
