---
section: Changed
topic: ci
issues: []
created: 2026-11-04T12:29:19+00:00
---
- **⚠️ BREAKING：Python 版本收成單一來源 `.python-version`（3.13.18），映像跟著 CI 的依賴版本**：
  - CI 的 setup-python 一律讀 `.python-version`；Dockerfile 的 `FROM python:`、devcontainer 與 YAML 裡的 python 映像都必須同版，由守衛擋下不一致。
  - 映像的 Python 由 3.13.15 升到 3.13.18。
  - da-tools 映像改以 `ci-constraints.txt` 安裝依賴：promql-parser 0.7.0 → 0.10.0、croniter 6.0.0 → 6.2.4，與 CI 測的版本相同。
  - CI 名稱不再帶版本：required check 由 `Python Tests (3.13)` 改為 `Python Tests`，branch protection 需同步更新；coverage artifact 改名為 `coverage-python`。
