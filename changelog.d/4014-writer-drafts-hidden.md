---
section: Changed
topic: tools
issues: [4014]
created: 2026-11-17T16:58:40+00:00
---
- **⚠️ BREAKING：onboard 與 scaffold 的 YAML 產物改寫進 `-o` 底下點開頭的目錄，`-o` 落在服務中的 conf.d 裡也不會被服務（tools；[#4014](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4014)）**：先前 onboard 的草稿租戶寫在 `phase1-routing/<tenant>.yaml`，`-o` 指到服務中的 conf.d 時被直接服務，所有檢查都是綠的；scaffold 會整份換掉那裡的 `_defaults.yaml`，其他租戶的鍵全部失去宣告。現在兩者的 conf.d 檔與 migrate 一樣寫進草稿樹 `-o/.conf.d/`（onboard 的 `phase2-rules/_defaults-suggestion.yaml` 改名 `.conf.d/_defaults.yaml`），onboard Phase 3 的 YAML 寫進 `-o/.onboard/`；CSV、`onboard-hints.json`、報告留在原處。`.conf.d/` 已有這次不寫的租戶檔時 rc 2、什麼都不寫。scaffold `--profile` 要先把 `_profiles.yaml` 放進 `.conf.d/`。
