---
section: Fixed
topic: ci
issues: [2348]
created: 2026-09-28T14:29:48+00:00
---
- **bench-workload-effect 的 overlay 編譯失敗不再一律叫人「縮小 overlay_helpers」（ci；[#2348](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2348)）**：「Verify the overlay tree builds」改以 `-gcflags=-e` 取完整錯誤，並依編譯器點名的檔案分流：錯誤落在永遠會被疊上去的 `*bench_test.go` 時，明講縮小 helper 無效、W/R 要等換參考版本（吸收事件，ADR-032 §待決 1）；只落在 helper 檔才提示縮小。`benchmark-playbook.md` §工作定義效應 明文化：參考版本 `exporter/v2.9.0` 上 W/R 目前不可用，改到此 workflow 的 PR 會紅這項非必要 self-test，夜跑 M/R 不受影響。
