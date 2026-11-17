---
section: Changed
topic: tenant-api
issues: [3943]
created: 2026-11-15T22:00:00+00:00
---
- **⚠️ breaking：產生器容器只在三支判定程式的自檢通過後才 `ready`（[ADR-036](docs/adr/036-single-parser-effective-config.md) §3；[#3943](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3943)）**：新增 `da-guard self-check`、`da-read self-check`、`da-crdecode --self-check`，以打包在程式裡的樣本跑過分派表的每一項並比對預期輸出（通過：結束碼 0、恰好印 `{"self_check":"passed"}`）。服務綁定 socket 後立刻跑（跑完前 `pending`、不 ready），之前判定前程式根本不存在也回報 ready；判定或預熱的工具故障只由通過的自檢清除，跑完全程的判定不再清除。`GET /v1/status` 新增 `self_check`（`state`、`failures`、`running`），新旗標 `--self-check-seconds`、`--self-check-timeout-seconds`，新 metric `da_generator_self_checks_total{result}`。自檢不影響判定結果。產生器映像的三支程式必須與服務同版：沒有自檢的舊版程式讓容器一直未就緒。
