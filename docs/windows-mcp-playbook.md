# Windows-MCP — Dev Container 操作手冊 (Playbook)

> 從 `CLAUDE.md` 抽取。詳細記錄透過 Windows-MCP Shell 操作 Dev Container 的最佳實踐與已知陷阱。

## 前提

kubectl/kind/go 僅在 Dev Container (`vibe-dev-container`) 內可用，不可直接從 Windows-MCP Shell 執行。

## 核心模式 — `Start-Process` + 檔案重定向

```powershell
# 正確方式: 用 Start-Process 執行 docker exec，將輸出寫入檔案
Start-Process -FilePath 'C:\Program Files\Docker\Docker\resources\bin\docker.exe' `
  -ArgumentList @('exec','-w','/workspaces/vibe-k8s-lab','vibe-dev-container','<command>','<args>') `
  -NoNewWindow -Wait `
  -RedirectStandardOutput 'C:\temp\out.txt' `
  -RedirectStandardError 'C:\temp\err.txt'

# 讀取結果 (用 ReadAllText，不要用 Get-Content — 後者常有 pipeline 問題)
[System.IO.File]::ReadAllText('C:\temp\out.txt')
```

## 常見陷阱與解法

1. **`docker` 直接呼叫無輸出**: PowerShell pipeline 問題。不要用 `docker ps | Select-Object`，改用 `Start-Process` + 檔案重定向。
2. **`bash -c '...'` 引號被吞**: PowerShell 會拆解 bash -c 後的引號。解法: 拆成獨立 arguments 傳入 `-ArgumentList @()`，或先用簡單指令確認可達性。
3. **Go 測試**: `docker exec -w /workspaces/vibe-k8s-lab vibe-dev-container go test -v ./...` — 注意 `-w` 設工作目錄。
4. **長時間測試**: Windows-MCP Shell 預設 30s timeout。長測試用 `Desktop Commander start_process` (支援 600s)。
5. **kubeconfig 過期**: Dev Container 重啟後需重新匯出: `kind export kubeconfig --name dynamic-alerting-cluster --kubeconfig /root/.kube/config`。
6. **Port-forward 殘留**: 測試失敗後 port-forward 不會自動清理。下次測試前先: `docker exec vibe-dev-container pkill -f port-forward`。
7. **PyYAML**: Dev Container 內需確保已安裝: `pip3 install pyyaml`。`_lib.sh` 的 `get_cm_value()` 依賴此套件。

## Phase 3 新增 — UTF-8 / Emoji 輸出問題

### 8. docker exec 輸出含 UTF-8 Emoji 時完全消失
當 Python/Bash 腳本輸出含有 emoji (✅❌📦📄⚙️ 等) 時，`Start-Process -RedirectStandardOutput` 和 `Desktop Commander start_process` 都會得到**空檔案/空輸出**，但 exit code 正常。

- **現象**: `[Reading 0 new lines (total: 0 lines)]` + exit code 0。檔案 size = 0。
- **根因**: Windows PowerShell 無法正確處理 Docker stdout 中的 UTF-8 multi-byte emoji。
- **影響範圍**: 所有含 emoji 的 Python print、bash echo -e、中文字混合 emoji 輸出。
- **判斷通過/失敗**: 腳本必須使用 `set -euo pipefail`，exit code 0 即代表全部通過。

**解法 (按優先順序):**

```powershell
# 方法 1 (推薦): 直接寫入 workspace mount，用 Read tool 讀取
docker exec vibe-dev-container bash -c \
  'cd /workspaces/vibe-k8s-lab && bash tests/test.sh > output.txt 2>&1'
# 然後用 Read tool 讀 /sessions/.../mnt/vibe-k8s-lab/output.txt

# 方法 2: docker cp 從容器內複製
docker exec vibe-dev-container bash -c 'script > /tmp/out.txt 2>&1'
docker cp vibe-dev-container:/tmp/out.txt C:\temp\out.txt

# 方法 3: 用 tr 去掉 emoji (如果只需要最後幾行摘要)
docker exec vibe-dev-container bash -c \
  "bash tests/test.sh 2>&1 | tail -3 | tr -d '\xc2\xa0\xe2\x9c\x85'"
```

### 9. `Desktop Commander start_process` vs `Windows-MCP Shell`
兩者用於 `docker exec` 時行為一致 — 都會吞 UTF-8 emoji 輸出。差別:

| | Windows-MCP Shell | Desktop Commander |
|---|---|---|
| Timeout | 10s (預設) | 600s (可設) |
| Exit code | 看 Status Code | 看 Process completed with exit code |
| 長指令 | Start-Process + 重定向 | start_process (自動等待) |
| 適用場景 | 快速指令 | 長時間測試 (>30s) |

## 指令快速參考

```bash
# Dev Container 內 — 透過 docker exec 執行
docker exec vibe-dev-container kind get clusters
docker exec vibe-dev-container kubectl get pods -A
docker exec -w /workspaces/vibe-k8s-lab vibe-dev-container go test -v ./...
docker exec -w /workspaces/vibe-k8s-lab vibe-dev-container make component-build COMP=threshold-exporter
docker exec -w /workspaces/vibe-k8s-lab vibe-dev-container make component-deploy COMP=threshold-exporter
docker exec -w /workspaces/vibe-k8s-lab vibe-dev-container bash tests/integration-2c.sh
```
