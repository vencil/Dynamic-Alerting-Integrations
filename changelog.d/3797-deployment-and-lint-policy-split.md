---
section: Changed
topic: tools
issues: [3797]
created: 2026-11-14T12:20:57+00:00
---
- **⚠️ 部署端 policy 與 custom rule lint 政策拆成兩份檔（tools；[#3797](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3797)）**：`validate-config --policy` 以前同時交給 routes 判定與 lint，一份檔兩種讀法——lint 鍵在部署端被忽略、`allowed_domains` 在 lint 被忽略。現在 `--policy`（generate-routes、validate-config、explain-route、test-notification、migrate-credentials，以及 generator-service——它讀不了時寫入一律 503）只收 `allowed_domains`、`min_generator_version`、`shared_secrets`；lint 政策改用新旗標 `validate-config --lint-policy`（或 `lint --policy`），且只收 lint 自己的鍵。兩邊收到對方的鍵都結束碼 2 並指出該用哪個旗標。遷移：把混用檔拆成兩份，本 repo 為 `.github/deployment-policy.yaml` 與 `.github/custom-rule-policy.yaml`。
