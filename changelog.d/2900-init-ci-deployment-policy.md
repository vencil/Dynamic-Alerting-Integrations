---
section: Added
topic: da-tools
issues: [2900]
created: 2026-10-13T13:09:14+00:00
---
- **`da-tools init` 產生的客戶 CI 可選擇帶入部署端 policy 檔（ADR-036 §5；[#2900](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2900)）**：設了 CI 變數 `DA_TOOLS_POLICY`，`validate-config` 與 `generate-routes --validate --strict` 就帶 `--policy`，webhook 網域與 tenant-api 用同一份 `allowed_domains` 判定。GitHub 用 repository／organization variable，值是 policy 的 YAML 內容；GitLab 用 File 型 CI 變數（不要勾 Protected），非檔案的值會讓 job 失敗並說明原因。沒設時只印提示、不失敗，webhook 網域不檢查。custom rule `lint` 與 pre-commit hook 不帶 `--policy`。portal 的 CI/CD 設定精靈同步。設定方式見 [GitOps CI/CD 整合指南](docs/scenarios/gitops-ci-integration.md) §2.5。
