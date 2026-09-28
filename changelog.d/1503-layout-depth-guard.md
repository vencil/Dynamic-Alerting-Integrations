---
section: Changed
topic: ci
issues: [1503]
created: 2026-09-28T08:14:37+00:00
---
- **出貨工具「數層數找 repo」的靜態守衛補上已知盲點，並接進 CI Lint job（[#1503](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1503)）**：`check_build_completeness.py` 的佈局深度規則現在把 `parents[N]` 算成 N+1 層、逐段累加鏈式 `parents`、依序追蹤同一 scope 內的 `_THIS_DIR = os.path.dirname(__file__)` 類賦值，並計入字面 `..` / `os.pardir` 路徑段；docstring 的七個已知盲點逐條標明「已關」或「保留＋理由」。`ci.yml` 的 Lint job 新增 `pre-commit run build-completeness-check --all-files`，這條規則在 CI 不再只靠 pytest twin 帶到。新規則掃出的三處 `sys.path.insert` 祖父目錄（映像內會把 `/` 加進 `sys.path`）已一併移除。
