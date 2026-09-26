---
section: Added
topic: log-federation
issues: [609, 21402]
created: 2026-09-26T17:00:00+00:00
---
- **租戶可查詢「平台關於自己」的營運日誌（ADR-021 Phase 1）**：tenant-api 為每個租戶配發單調、永不回收的 `account_id`（`_account_registry.yaml`，另有 admin-only 的 backfill 端點；⚠️ 啟用 federation 時若 registry 空白或缺失而 conf.d 已有租戶，tenant-api 拒絕啟動），federation token 新增 `capability=logs`，簽出 audience 為 `tenant-federation-logs` 的 logs token；未帶 `capability` 時 metrics token 維持原樣。Vector 以 allowlist 淨化後把租戶的 federation audit 列雙寫進該租戶的 VictoriaLogs `AccountID` 分區，平台完整副本仍留在 `0:0`，兩份以 `log_event_id` 關聯；VictoriaLogs 另加查詢護欄（範圍 ≤7d、單次 ≤25s、並發 6）。federation-gateway 新增 `victorialogs` mode：只接受 logs audience、依已驗證的 claim 注入 `AccountID`、缺失或非法 claim 一律 403，且只開放 LogsQL 查詢端點。另附 per-account 查詢 metric、dashboard、兩條告警、租戶 onboarding 指南與隔離 E2E（[#609](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/609)）。
