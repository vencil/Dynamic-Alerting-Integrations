---
title: "Migration Toolkit 安裝指南（da-tools / da-guard）"
tags: [migration, toolkit, installation, da-guard, da-tools, phase-c, v2.8.0]
audience: [platform-engineers, sre, customer-ops]
version: v2.7.0
lang: zh
---

# Migration Toolkit 安裝指南（da-tools / da-guard）

> **Language / 語言：** **中文 (Current)** | [English](./migration-toolkit-installation.en.md)

> **適用版本**：`tools/v2.8.0` 起（C-11 packaging 落地後的所有 release）。  
> 早期版本（≤ `tools/v2.7.0`）只有 Docker image 一條交付路徑。

## 為什麼需要 Migration Toolkit

把客戶現有的 Prometheus alerting rule corpus（PromRule CRD / Alertmanager YAML）導入到 Dynamic Alerting Platform 的 conf.d/ Profile-as-Directory-Default 架構（[ADR-019](adr/019-profile-as-directory-default.md)），需要一連串工具串接：

```
PromRule corpus → C-8 parser → C-9 cluster + translator → C-10 batch PR → C-12 guard validation → conf.d/
```

C-11 Migration Toolkit 把這條 pipeline 打包成可離線跑、可在 air-gapped 環境跑、可 自動驗 binary integrity 的客戶可用工具集。

**目前包含**：

| 工具 | 介面 | 用途 |
|---|---|---|
| `da-tools` | Python CLI | 既有 41+ 個運維 / 配置生成 / 政策評估子命令；新增 `guard` + `batch-pr` 子命令 wrapping da-guard / da-batchpr |
| `da-guard` | Go binary | C-12 Dangling Defaults Guard CLI（schema / routing / cardinality / redundant-override 四層檢查）|
| `da-batchpr` | Go binary | C-10 Migration Batch PR Pipeline CLI（apply / refresh / refresh-source 子命令；plan → 開 PR → Base merge 後 rebase / data-layer hot-fix）|

未來 PR 加入 `da-toolkit parser` 等 Go 子命令時，本指南會更新。

## 三種交付路徑

依客戶環境選一條：

| 路徑 | 適用 | 啟動成本 | 升級成本 |
|---|---|---|---|
| **A. Docker pull from ghcr.io** | 有對外 registry 連線的客戶（最常見）| 低 | 低（`docker pull :v<new>`）|
| **B. Static binary download** | 不想用 Docker、只要 `da-guard` 跑 pre-commit / GitHub Actions | 中 | 中（重下載 + 替換）|
| **C. Air-gapped tar import** | 完全不能對外的客戶（金融 / 政府 / 軍工）| 高 | 高（每次升級都要重新匯入）|

每個 GitHub Release（`tools/v*` tag）同時提供三條路徑的 assets，客戶選任一即可。

---

## 路徑 A：Docker pull from ghcr.io

```bash
# 拉最新 stable
docker pull ghcr.io/vencil/da-tools:v2.7.0

# 跑單次命令
docker run --rm ghcr.io/vencil/da-tools:v2.7.0 --help
docker run --rm ghcr.io/vencil/da-tools:v2.7.0 guard --help

# 掛 conf.d 進去跑 guard
docker run --rm \
    -v "$(pwd)/conf.d:/conf.d:ro" \
    ghcr.io/vencil/da-tools:v2.7.0 \
    guard defaults-impact --config-dir /conf.d --required-fields cpu,memory
```

**內含**：Python `da-tools` CLI + bundled `da-guard` Linux/amd64 binary（`/usr/local/bin/da-guard`）。`da-tools guard` 子命令會自動在 image 內找到 `da-guard`，不需另外設 `$DA_GUARD_BINARY`。

**Trivy CVE scan** 在 release 時自動跑（`CRITICAL` / `HIGH` 級別 fail-fast）。Image SBOM + 簽章在 `tools/v2.8.0` Release notes 列出（cosign 簽章 PR-3 deferred）。

---

## 路徑 B：Static binary download

每個 Release 提供兩組 6 個 cross-compiled binary（共 12 個 archive）：

**`da-guard`**（C-12 Dangling Defaults Guard）：

| OS | ARCH | 檔名 |
|---|---|---|
| Linux | amd64 | `da-guard-linux-amd64.tar.gz` |
| Linux | arm64 | `da-guard-linux-arm64.tar.gz` |
| macOS | amd64 | `da-guard-darwin-amd64.tar.gz` |
| macOS | arm64 (Apple Silicon) | `da-guard-darwin-arm64.tar.gz` |
| Windows | amd64 | `da-guard-windows-amd64.zip` |
| Windows | arm64 | `da-guard-windows-arm64.zip` |

**`da-batchpr`**（C-10 Migration Batch PR Pipeline，v2.8.0 起）：

| OS | ARCH | 檔名 |
|---|---|---|
| Linux | amd64 | `da-batchpr-linux-amd64.tar.gz` |
| Linux | arm64 | `da-batchpr-linux-arm64.tar.gz` |
| macOS | amd64 | `da-batchpr-darwin-amd64.tar.gz` |
| macOS | arm64 (Apple Silicon) | `da-batchpr-darwin-arm64.tar.gz` |
| Windows | amd64 | `da-batchpr-windows-amd64.zip` |
| Windows | arm64 | `da-batchpr-windows-arm64.zip` |

每份 archive 內含**一個** binary（或 `<name>.exe`），再加一份**單一** `SHA256SUMS` 檔案 list 全部 12 個 archive 的 hash。客戶可以只下載自己需要的（純驗 conf.d/ 拿 `da-guard` 就好；要走 batch PR 再加 `da-batchpr`）。

### 安裝範例（Linux/macOS）

```bash
# 下載 + 驗 hash + 解壓 + 放 PATH
TAG=tools/v2.7.0
OS=linux            # or darwin, windows
ARCH=amd64          # or arm64
URL=https://github.com/vencil/Dynamic-Alerting-Integrations/releases/download/${TAG}

curl -fsSLo da-guard.tar.gz "${URL}/da-guard-${OS}-${ARCH}.tar.gz"
curl -fsSLo SHA256SUMS "${URL}/SHA256SUMS"

# 驗 hash（任何不符立即拒絕）
sha256sum --check --ignore-missing SHA256SUMS

tar xzf da-guard.tar.gz
sudo install -m 0755 da-guard-${OS}-${ARCH} /usr/local/bin/da-guard
da-guard --version    # 應印出 da-guard v2.7.0
```

### 安裝範例（Windows）

```powershell
$TAG = "tools/v2.7.0"
$Url = "https://github.com/vencil/Dynamic-Alerting-Integrations/releases/download/$TAG"

Invoke-WebRequest -Uri "$Url/da-guard-windows-amd64.zip" -OutFile da-guard.zip
Invoke-WebRequest -Uri "$Url/SHA256SUMS" -OutFile SHA256SUMS

# 比對 hash
Get-FileHash da-guard.zip -Algorithm SHA256
# 對照 SHA256SUMS 同 filename 的那一行

Expand-Archive -Path da-guard.zip -DestinationPath .
# 移到 PATH 任一目錄（e.g. C:\Tools\）
```

### 跑 da-guard 對 conf.d/ 校驗

```bash
da-guard --config-dir conf.d/ \
    --required-fields cpu,memory \
    --cardinality-limit 500 \
    --format md
```

Exit code：`0` 通過 / `1` 偵測到 error 級 finding（block CI）/ `2` caller error。完整 flag 參考見 `components/threshold-exporter/README.md` § da-guard CLI（位於 MkDocs site 範圍外，請從 GitHub 端開啟）。

---

## 路徑 C：Air-gapped tar import

針對完全不能 pull from `ghcr.io` 的環境（內網 isolated registry / no-internet builds）。

### 一次性 import 流程

```bash
TAG=tools/v2.7.0
VER=2.7.0
URL=https://github.com/vencil/Dynamic-Alerting-Integrations/releases/download/${TAG}

# 1. 下載 image tar + SHA256
curl -fsSLo da-tools-image.tar.gz "${URL}/da-tools-image-v${VER}.tar.gz"
curl -fsSLo da-tools-image.tar.gz.sha256 "${URL}/da-tools-image-v${VER}.tar.gz.sha256"

# 2. 驗 hash
sha256sum --check da-tools-image.tar.gz.sha256

# 3. 把整個 tar.gz 搬到 air-gapped 環境（USB / 內網 file transfer / etc.）

# 4. 在 air-gapped 環境裡 import 進本地 docker
gunzip -c da-tools-image.tar.gz | docker load
# 印出: Loaded image: ghcr.io/vencil/da-tools:v2.7.0

# 5. 重新 tag 到內網 registry（選用）
docker tag ghcr.io/vencil/da-tools:v2.7.0 internal-registry.corp/da-tools:v2.7.0
docker push internal-registry.corp/da-tools:v2.7.0
```

之後客戶內部 CI / pre-commit 直接用 `internal-registry.corp/da-tools:v2.7.0` 即可。`da-guard` binary 已 bundle 在 image 內 `/usr/local/bin/da-guard`，無需另外傳輸。

### 純 binary 走 air-gapped 也可以

如果客戶不用 Docker，直接走路徑 B 把 6 個 binary archive + `SHA256SUMS` 一起 download → USB 帶進去 → 解壓即可。每個 binary 都是 statically linked，無 runtime dep。

---

## 三條路徑共通的 hash verification

每個 Release 提供：

| Asset | 內容 |
|---|---|
| `SHA256SUMS` | 六個 binary archive + 六個 raw binary 的 hash（路徑 B / C 都會用到）|
| `da-tools-image-v<X.Y.Z>.tar.gz.sha256` | air-gapped image tar 的 hash（路徑 C 用）|

`tools/v2.8.0` 起所有 Release artefact 都附 SHA-256。GPG / cosign 簽章是 C-11 PR-3 工作（DEC-J 部分待定 — 客戶 security team 要求驗簽方式才會啟用對應路徑）。

## 升級

| 路徑 | 升級指令 |
|---|---|
| A (ghcr.io) | `docker pull ghcr.io/vencil/da-tools:v<NEW>` |
| B (binary) | 重新走「下載 + 驗 hash + 替換 `/usr/local/bin/da-guard`」 |
| C (air-gapped) | 重複 import 流程（每次 release 都要走一次）|

跨主版本（例 `tools/v2.x → tools/v3.x`）升級時，請先讀對應 release notes 的 Breaking changes 段落。`tools/v2.8.0` 起 [Release notes 模板](https://github.com/vencil/Dynamic-Alerting-Integrations/releases/tag/tools/v2.8.0) 自動 generate（含 commit log 連結），人為 review 後才正式 publish。

## 驗證 da-guard 可在 customer repo 工作

落地後第一次跑 sanity check：

```bash
cd <customer-repo>/conf.d
da-guard --config-dir . --required-fields cpu --format md
```

預期輸出：`✅ No findings — defaults change is safe to merge.`（或 `❌ N errors found, M warnings`）

CI 整合範例見 [`.github/workflows/guard-defaults-impact.yml`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/.github/workflows/guard-defaults-impact.yml)（C-12 PR-5 的 customer template，可整份 copy 過去）。

## 故障排除

| 症狀 | 可能原因 | 處置 |
|---|---|---|
| `da-guard: command not found` | binary 沒進 `$PATH` | 確認 `which da-guard` 印出路徑；放到 `/usr/local/bin/` 或自訂目錄並加進 PATH |
| `SHA256SUMS` 對不上 | 下載中斷 / MITM | 重下載；驗網路連線安全；放棄當下版本改用較舊已知好的 |
| Docker pull 401 / 403 | ghcr.io 對 anonymous 拒絕（私有 image / rate limit）| 若是 rate limit 等 1 小時；若需 auth：`docker login ghcr.io` |
| `gunzip: invalid magic` | tar.gz 損毀或下載成 HTML 錯誤頁 | 用 `file <path>` 確認是 gzip data；重下載 |
| Air-gapped `docker load` 後 `image not found` | tag 沒對上 | `docker images | grep da-tools` 看實際 tag；用 image ID 直接跑 `docker run <id>` |

進一步問題請開 [GitHub issue](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/new) 並附 `da-guard --version` + 觀察到的 exit code + reduced reproducer。

## 相關文件

- [`migration-guide.md`](migration-guide.md) — 整體遷移流程
- [`scenarios/incremental-migration-playbook.md`](scenarios/incremental-migration-playbook.md) — 增量遷移 playbook（含 Emergency Rollback Procedures）
- [`adr/019-profile-as-directory-default.md`](adr/019-profile-as-directory-default.md) — 為什麼 conf.d/ 走 sparse-override 形狀
- [`cli-reference.md` § guard](cli-reference.md#guard) — `da-tools guard defaults-impact` 完整選項
