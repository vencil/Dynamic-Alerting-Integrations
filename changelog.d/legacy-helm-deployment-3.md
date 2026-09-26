---
section: Added
topic: helm-deployment
issues: []
created: 2026-09-26T17:00:00+00:00
---
- **self-hosted GitLab CE SSO（helm）**：tenant-api 與 da-portal 的 oauth2-proxy sidecar 新增 `--oidc-issuer-url`／`--scope`／`--gitlab-group` 及 cookie 撤權延遲參數，`provider: gitlab` 可接 self-hosted 實例；GitLab 群組以全路徑解入 `X-Forwarded-Groups`，免 EE。全部為選填、預設空，既有 `github` provider 行為不變。附客戶導入指南 `gitlab-ce-sso.md`。
