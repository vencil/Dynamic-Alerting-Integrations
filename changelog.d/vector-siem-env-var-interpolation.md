---
section: Fixed
topic: log-federation
issues: []
created: 2026-09-29T16:06:01+00:00
---
- **SIEM fan-out 的 `${VAR}` 憑證寫法現在真的會展開（helm/vector 0.10.2）**：文件教的 `additionalSinks` 寫法 `default_token: ${SPLUNK_TOKEN}`（由 `extraEnv` / `extraEnvFrom` 提供）從來沒生效過——Vector 0.57.0 預設不展開設定檔裡的環境變數，sink 會把字面字串 `${SPLUNK_TOKEN}` 當 token 送出，而且不報錯。chart 現在在 vector container 設 `VECTOR_DANGEROUSLY_ALLOW_ENV_VAR_INTERPOLATION=true`，values／README／runbook 補上它的代價。⚠️ 行為變更：引用了卻沒提供的變數，現在會讓設定載入失敗、pod 起不來（以前是靜默送出字面字串）。`additionalSinks` 字串裡的裸 `$NAME` 也會被替換，字面 `$` 要寫成 `$$`。
