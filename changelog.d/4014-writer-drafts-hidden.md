---
section: Changed
topic: tools
issues: [4014]
created: 2026-11-17T16:58:40+00:00
---
- **⚠️ BREAKING：onboard、scaffold、migrate 的 YAML 產物各寫進 `-o` 底下自己的點目錄，`-o` 落在服務中的 conf.d 裡也不會被服務，串在同一個 `-o` 上也不互蓋（tools；[#4014](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4014)）**：先前 onboard 的草稿租戶寫在 `phase1-routing/<tenant>.yaml`，`-o` 指到服務中的 conf.d 時被直接服務、所有檢查都是綠的；scaffold 會整份換掉那裡的 `_defaults.yaml`。現在草稿 conf.d 在 `-o/.onboard/conf.d/`、`-o/.scaffold/conf.d/`、`-o/.migration/conf.d/`（onboard 的 `phase2-rules/_defaults-suggestion.yaml` 改名 `_defaults.yaml`），onboard Phase 3 的 YAML 在 `-o/.onboard/`；CSV、`onboard-hints.json`、報告留在原處。這次有寫租戶、草稿裡卻已有這次不寫的租戶檔，或要建的目錄是檔案時 rc 2、什麼都不寫。scaffold `--profile` 要先把 `_profiles.yaml` 放進 `.scaffold/conf.d/`。
