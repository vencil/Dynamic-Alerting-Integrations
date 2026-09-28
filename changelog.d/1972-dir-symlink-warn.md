---
section: Fixed
topic: exporter-config
issues: [1972]
created: 2026-09-28T10:19:38+00:00
---
- **conf.d 裡的目錄 symlink 不再無聲吞掉租戶（exporter + da-tools；[#1972](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1972)）**：ConfigMap `items[].path` 帶子目錄（如 `team-a/x.yaml`）時，kubelet 只建頂層目錄 symlink `team-a -> ..data/team-a`，exporter 與 da-tools 都不跟進，底下的租戶先前沒被載入且沒有任何訊息。仍然不跟進，但 exporter 每次掃描會對這種 symlink 印一行 WARN，da-tools 把它列為無法使用的項目（`_` 開頭的也會點名）。判準：symlink 的最終目標若在 conf.d 內、且路徑不經過 `.` 開頭的目錄，walker 本來就會掃到，不算遺失、不報；`..data` 本身也不報。⚠️ `validate_config` 在這種佈局下 rc 從 0 變成 1（`yaml_syntax` FAIL），先前的 rc 0 是在租戶根本沒讀到時回報通過。`assemble_config_dir --check` 與 `compile_custom_alerts --allow-empty` 的 rc 不變（0），只多印一行 WARN。扁平 key 佈局的行為與輸出不變。
