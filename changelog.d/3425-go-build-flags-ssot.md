---
section: Changed
topic: da-tools
issues: [3425]
created: 2026-10-30T15:42:28+00:00
---
- **映像與 release 裡的 Go binary 改為去除符號表與除錯資訊、且不帶建置路徑（da-tools / tenant-api / recipe-preview / exporter / tools；[#3425](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3425)）**：da-tools 映像的 da-guard、da-batchpr、da-parser、da-crdecode、da-read 與 `tools/v*` release 的離線 binary 先前既未 strip 也沒有 `-trimpath`，現在與 tenant-api、recipe-preview、threshold-exporter 映像裡的 binary 一律以 `CGO_ENABLED=0 go build -trimpath -ldflags "-s -w"` 建（後三者先前已 strip、但沒有 `-trimpath`）；da-tools 映像裡五支 binary `gzip -9` 後合計約小一半。建置旗標只寫在 `scripts/ops/go_build.sh` 一處，映像、release、CI、Makefile 與測試都經它建；`tests/ops/test_go_build_ssot.py` 量測試建出的 binary 是否為這個形狀，並掃描常見寫法的直接 `go build` 呼叫點。binary 的行為不變；stack trace 裡的原始碼路徑改以模組路徑起頭，不再帶建置機器的絕對路徑。
