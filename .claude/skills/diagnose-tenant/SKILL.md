# Skill: diagnose-tenant

## Description
快速診斷 Tenant 健康狀態。
**設計原則：Exception-based Reporting。**
- 正常時：僅回傳 `{"status": "healthy"}` (極省 Token)。
- 異常時：回傳錯誤原因與關鍵 Log。

## Usage
當使用者詢問 "db-a 健康嗎？" 或 "檢查 db-b" 時使用。

## Command
```bash
python3 scripts/tools/diagnose.py <tenant>