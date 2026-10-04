# third_party：vendored 第三方原碼

## `yaml.v3/` — `gopkg.in/yaml.v3` v3.0.1 + 線性判重 patch

| 項目 | 內容 |
|---|---|
| 上游 | `gopkg.in/yaml.v3` **v3.0.1**（`go.sum`：`h1:fxVm/GzAzEWqLHuvctI91KS9hhNmmWOoWu0XTYJS7CA=`） |
| 套用的 patch | [`yaml.v3-uniquekeys.patch`](yaml.v3-uniquekeys.patch)：grafana/go-yaml PR #1（v3 分支 commit [`a1d5f0f`](https://github.com/grafana/go-yaml/commit/a1d5f0fedb1c96de6b7def259bf2e99362375690)，"Make duplicate mapping key detection linear for large mappings"）**只取 `decode.go` 那一段**；`+`／`-` 行與該 commit 逐字相同，只把 `@@` 行號平移 -3 對齊 v3.0.1 |
| 使用者 | `components/threshold-exporter/app/go.mod`、`components/tenant-api/go.mod` 的 `replace gopkg.in/yaml.v3 => …/third_party/yaml.v3` |
| 授權 | 上游 `LICENSE`／`NOTICE` 原樣保留在 `yaml.v3/` 內 |
| 守衛 | `tests/ops/test_vendored_yaml_v3.py` |

### 為什麼（#2681）

v3.0.1 解碼 mapping 時，重複 key 的檢查是兩兩比對（`decode.go` 的 `(*decoder).mapping`），
key 數 n 的成本是 O(n²)。一份頂層 32000 個 key、約 300KB 的 `_domain_policy.yaml`，
`routingpolicy.UnmarshalPolicy` 要約 3–4 秒；tenant-api 啟動、每次熱重載、PR 模式下的每個
PUT 都會重新解析，da-guard 與 exporter 也走同一段 decode。

patch 讓超過 48 個 key 的 mapping 改用 hash map 判重，錯誤訊息與排列順序與兩兩比對相同；
48 個 key 以下仍走原本的兩兩比對。

⛔ **只帶入判重那一段。** grafana fork 的其他差異都會改變行為，刻意不取：
block scalar encode 的換行、`Node.DecodeWithOptions`、`0` 解成 `time.Duration`、
上游 v3.0.2 之後的 backport。

### 守衛在擋什麼

`tests/ops/test_vendored_yaml_v3.py`：

1. patch 檔以 SHA-256 釘住——要放寬「允許與上游不同的範圍」，就得在同一個 diff 改測試裡的常數。
2. `yaml.v3/decode.go` 必須恰好含 patch 的新內容一次；換回舊內容後，整個目錄的 Go module
   hash（`h1:`，`go.sum` 用的 dirhash）必須等於 v3.0.1 在 sum.golang.org 的值。
   任何其他檔案多一個 byte、多一個檔、少一個檔都會轉紅。

⛔ 不要手改 `yaml.v3/` 底下的檔案，也不要讓格式化工具碰它（pre-commit 已排除這個目錄）。

### 如何重新產生

在 repo 根目錄執行：

```bash
dir=$(cd "$(mktemp -d)" && go mod download -json gopkg.in/yaml.v3@v3.0.1 \
      | sed -n 's/^[[:space:]]*"Dir": "\(.*\)",$/\1/p')
dst=components/threshold-exporter/app/third_party/yaml.v3
rm -rf "$dst" && cp -r "$dir" "$dst" && chmod -R u+w "$dst"
patch -p1 -d "$dst" < components/threshold-exporter/app/third_party/yaml.v3-uniquekeys.patch
python3 -m pytest -q tests/ops/test_vendored_yaml_v3.py
```

`go mod download` 會先以 checksum database 驗過 v3.0.1 才給路徑；`patch` 應無 offset／fuzz 提示。

### 上游安全修補要手動 backport

這份原碼不會跟著上游動：上游（或 `go.yaml.in/yaml/v3`）發布的安全修補**不會**自動進來，
本 repo 的 Renovate 也沒有開 `gomod` manager。

⛔ **掃描器確定看不到它。** binary 的 buildinfo 記的是
`gopkg.in/yaml.v3 v3.0.1 => ./third_party/yaml.v3 (devel)`。#2681 盲審用有漏洞的
v3.0.0-2021… 實測：不 replace 時 trivy 0.74.0 報 CVE-2022-28948、govulncheck v1.8.0 報
GO-2022-0603；改成目錄 replace 後兩者都**不報**（trivy 把套件記成路徑、沒有版本）。
所以 release.yaml 與 nightly-image-scan.yaml 的 image 掃描不會替這份原碼報任何公告。

**這是沒有機器守住的風險。** 本 repo 沒有 trivy fs、dependabot 設定檔、govulncheck 或 osv
查詢可以接手（CodeQL default setup 分析的是程式碼，不查 module 公告）。GitHub 的
dependency graph 仍可能從 `require gopkg.in/yaml.v3 v3.0.1` 列出它，但 Dependabot alerts
有沒有開、replace 後是否照列，都未查證，不能當成補償控制。

人工檢查（每次發版前，或看到 YAML 相關公告時）：

```bash
curl -sS -X POST https://api.osv.dev/v1/query \
  -d '{"package":{"name":"gopkg.in/yaml.v3","ecosystem":"Go"},"version":"3.0.1"}'
```

回 `{}` 代表 OSV 對 v3.0.1 沒有公告。把版本換成 `3.0.0` 會回 `GHSA-hp87-p4gw-j4gq`，
可用來確認查詢本身有效。`go.yaml.in/yaml/v3` 的公告也要看：同一段程式的修補可能只發在
那條線上。

- 看到 yaml.v3 的安全公告時，要人工判斷是否影響 v3.0.1，必要時把修補做成第二份 patch
  一起套用，並同步更新守衛（`_PATCH_SHA256` 與允許的 hunk）。
- 上游若收下等效的線性判重，就移除 `replace` 與本目錄，改回一般相依。
