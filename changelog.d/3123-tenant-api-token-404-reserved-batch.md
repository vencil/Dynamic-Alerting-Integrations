---
section: Security
topic: tenant-api
issues: [3123]
created: 2026-10-17T13:03:08+00:00
---
- **BREAKING — tenant-api 撤銷 token 的拒絕改回 404，租戶 id `batch` 保留（[#3123](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3123)）**：`DELETE /federation/tokens/{id}` 無權撤銷時回與 token 不存在相同的 404（原為帶租戶 id 的 403）。批次 PR 分支是 `tenant-api/batch/<時間>`，與名為 `batch` 的租戶分不開，所以 `batch` 不能再經 tenant-api 寫入（PUT、批次、custom alerts、federation 子集回 400）；讀取與撤銷它的 token 不受影響。已有租戶叫 `batch` 的：在 conf.d 用 git 把設定檔與其中的租戶 key 改成新 id（offboarding runbook 的 `find` 找出 `.yaml`／`.yml`），federation 子集與 token 照 offboarding runbook 清掉後以新 id 重建。
