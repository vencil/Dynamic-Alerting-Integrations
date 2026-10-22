# third_party：vendored 第三方原碼

## `yaml.v3/` — 上游 `go.yaml.in/yaml/v3` v3.0.5 + 線性判重 patch

| 項目 | 內容 |
|---|---|
| 上游 | `go.yaml.in/yaml/v3` **v3.0.5**（`gopkg.in/yaml.v3` 改名後的同一個套件；sum.golang.org：`h1:N6y/pJk8buWs9NY5ERU2HSMfm+IuD/OtfdAnq6kESPw=`） |
| 套用的 patch | [`yaml.v3-uniquekeys.patch`](yaml.v3-uniquekeys.patch)：grafana/go-yaml PR #1（v3 分支 commit [`a1d5f0f`](https://github.com/grafana/go-yaml/commit/a1d5f0fedb1c96de6b7def259bf2e99362375690)，"Make duplicate mapping key detection linear for large mappings"）**只取 `decode.go` 那一段**；`+`／`-` 行與該 commit 逐字相同，只把 `@@` 行號平移 -3。v3.0.5 的 `decode.go` 在這一段與 v3.0.1 相同，patch 原檔不變、套上時沒有 offset |
| 使用者 | `components/threshold-exporter/app/go.mod`、`components/tenant-api/go.mod` 的 `replace gopkg.in/yaml.v3 => …/third_party/yaml.v3` |
| 匯入路徑 | 程式一律 import `gopkg.in/yaml.v3`，由上列 `replace` 換成本目錄；`require gopkg.in/yaml.v3 v3.0.1` 只是佔位的版號（`replace` 不帶版號，任何版本都換成本目錄），binary 的 buildinfo 照舊記成 `gopkg.in/yaml.v3 v3.0.1 => ./third_party/yaml.v3`。目錄內的 `go.mod` 是上游原樣（`module go.yaml.in/yaml/v3`），見下方「module 行與匯入路徑不同」 |
| 授權 | 上游 `LICENSE`／`NOTICE` 原樣保留在 `yaml.v3/` 內 |
| 守衛 | `tests/ops/test_vendored_yaml_v3.py` |

過去還有三份 patch，都已刪除：

- 「只認空格」（`Decoder.SpacesOnly`，讓 scanner 照 PyYAML 只認空格為分隔字元），只有 Go 端讀 `_domain_policy.y*ml` 的程式會打開；ADR-036 第 3 步刪除那段程式時一併刪除，`yaml.go`、`yamlh.go`、`scannerc.go` 回到上游原樣。
- 「非特定 tag」（#2730 §6，`(*parser).scalar` 對非 plain 且寫了 `!` 的 scalar 保留 `Node.Tag == "!"`，讓 `! "<<"` 成為 merge key）；ADR-036 第 8 步刪除：exporter、da-guard、tenant-api 都先經 `da-guard parse-tree` 的判定，它從原文找出 `!` 並判為讀不了（`non_specific_tag`），帶 `!` 的 conf.d 檔與 tenant-api 自己讀的管理設定到不了解碼。`decode.go` 的那一段回到上游原樣：`! "<<"` 是字面上的 key `<<`，`! "true"` 是字串 `"true"`。
- 「不可雜湊 key 的 merge」（#2913，見下方）：當時基底是 v3.0.1，從上游 v3.0.2 逐字移植；基底升到 v3.0.5 後由上游提供，patch 刪除（ADR-036 §11，#3206）。

### 為什麼（#2681）

v3.0.1 解碼 mapping 時，重複 key 的檢查是兩兩比對（`decode.go` 的 `(*decoder).mapping`），
key 數 n 的成本是 O(n²)；v3.0.5 仍是如此。一份頂層 32000 個 key、約 300KB 的 `_domain_policy.yaml`，
當時 Go 端的 policy 讀取（`pkg/routingpolicy`，已於 ADR-036 第 3 步刪除）要約 3–4 秒；tenant-api 啟動、每次熱重載、PR 模式下的每個
PUT 都會重新解析，da-guard 與 exporter 也走同一段 decode。

patch 讓超過 48 個 key 的 mapping 改用 hash map 判重，錯誤訊息與排列順序與兩兩比對相同；
48 個 key 以下仍走原本的兩兩比對。

⛔ **只帶入判重那一段。** grafana fork 的其他差異都會改變行為，刻意不取：
block scalar encode 的換行、`Node.DecodeWithOptions`、`0` 解成 `time.Duration`。

### 為什麼（#2913）：同一個 mapping 有非純量 key 又有 `<<`

v3.0.1 解碼 merge key 時，把見過的 key 記在 `map[interface{}]bool`（`(*decoder).merge`
與 `(*decoder).mapping` 的 `mergedFields`）。key 若是 sequence 或 mapping（`? [a]`、
`? {k: v}`），解出來是 slice／map，寫進這個 map 會觸發 Go runtime panic
`hash of unhashable type`；`handleErr` 只把 yaml 自己的錯誤轉回 error，這個 panic 原樣往外丟。
decode 進 typed struct 才會走到（進 `yaml.Node` 不會，進 `map[string]interface{}` 回
`invalid map key`）。exporter 的 reload（`loadFile`／conf.d walker → `ParseTenantFile`）與
tenant-api 的寫入驗證、批次 worker 都會走到，結果是整個程序 crash。

上游 v3.0.2 起把這兩處讀寫改走 `setPossiblyUnhashableKey`／`getPossiblyUnhashableKey`：
在函式內 `recover()`，再以 `failf` 轉成一般的 decode 錯誤。這是基底從 v3.0.1 升到 v3.0.5 的原因；
守衛另有一條測試指名這兩個 helper 必須在 `decode.go` 裡。

### 升級基底時比對了什麼（#3206）

v3.0.1 到 v3.0.5 的原始碼差異：`decode.go` 只差上面這段修正；`parserc.go`、`scannerc.go`、
`readerc.go`、`apic.go`、`writerc.go`、`yamlprivateh.go` 只差 gofmt 的空白與註解；`resolve.go`、
`encode.go`、`sorter.go` 相同。寫出端有兩處改動：新增預設不開的 `Encoder.CompactSeqIndent`，
以及 emitter 處理註解後換行的修正。

讀法：以兩份固定語料的全部輸入（`tests/shared/parse_tree_corpus.json` 的 `rows`，加上
`merge_key_policy_corpus.json` 的全部列，共 1659 份，含 UTF-16／latin-1 編碼的列），
各編一支程式，分別以舊基底（v3.0.1 + 兩份 patch）與新基底（v3.0.5 + 線性判重）建置，
逐份輸出：節點樹（kind、style、tag、值、anchor、行列、三種註解）、解碼進 `any` 與
`map[string]any` 的結果或錯誤、`config.ParseTenantFile`／`ParseConfigFile`、`parsetree.Parse`，
以及節點樹重新 `Marshal` 的文字。兩邊輸出逐 byte 相同。對照組：拿掉不可雜湊修正的舊基底，
同一批輸入有 1 份由錯誤變成 panic，比對抓得到這類差異。repo 內 399 份可解析的 YAML 重新
`Marshal`，兩邊也逐 byte 相同。

寫法有一處不同：值以換行開頭的字串寫成 literal block 時，v3.0.1 的 emitter 漏寫開頭的空行
（`"\nteam"` 寫成 `|4-` 加 `team`，讀回是 `"team"`），v3.0.5 會寫出來、讀回不變。受影響的是
tenant-api 的 custom alerts 合併（`internal/customalerts` 的 `literalBlockKeeps` 先試寫再讀回，
不一致才改用雙引號）：這種字串過去寫成 `"\nteam"`，現在寫成 literal block；兩種寫法讀回都是同一個字串。
同一個原因，tenant-api 的部分更新（批次 patch、`PUT .../custom-alerts`）遇到開頭有空行的 block scalar，
過去重新輸出會改掉它而拒收（`not_round_trippable`），現在重新輸出不變，照常寫入；合併後的不變式檢查不變
（`internal/handler/batch_patch_diff_test.go` 也以產生器讀回比對這幾種寫法）。

### module 行與匯入路徑不同

目錄內的 `go.mod` 寫的是 `module go.yaml.in/yaml/v3`，而兩個 module 以 `gopkg.in/yaml.v3`
引用它。Go 對指向本機目錄的 `replace` 不比對 module 行（`cmd/go/internal/modload/modfile.go`
的特例，Go 1.26.8 實測 `go build`、`go mod tidy` 都通過），所以不必為了改名另加 patch。

⚠️ 那段原始碼註明日後要移除這個特例。屆時建置會直接失敗，報
`module declares its path as: go.yaml.in/yaml/v3 but was required as: gopkg.in/yaml.v3`，
不會靜默換成別的原碼；處理方式是另加一份只改 `go.mod` module 行的 patch，並同步更新守衛。

上游的測試檔 import 的是 `go.yaml.in/yaml/v3`，只有把本目錄當成獨立 module 跑
（在 `yaml.v3/` 內 `go test .`）時才測到本目錄；exporter 與 tenant-api 的 `./...` 不含它們。

### 守衛在擋什麼

`tests/ops/test_vendored_yaml_v3.py`：

1. patch 檔以 SHA-256 釘住，並釘住它能改哪些檔、每檔幾段 hunk（`decode.go` 一段）——要放寬
   「允許與上游不同的範圍」，就得在同一個 diff 改測試裡的常數（`_PATCHES`）。`third_party/`
   底下的 `*.patch` 必須恰好是 `_PATCHES` 列的那些。
2. 每段 hunk 所改的檔必須恰好含該段的新內容一次；換回舊內容後，整個目錄的
   Go module hash（`h1:`，`go.sum` 用的 dirhash）必須等於 `go.yaml.in/yaml/v3` v3.0.5 在
   sum.golang.org 的值。任何其他檔案多一個 byte、多一個檔、少一個檔都會轉紅。

⛔ 不要手改 `yaml.v3/` 底下的檔案，也不要讓格式化工具碰它（pre-commit 已排除這個目錄）。

### 如何重新產生

在 repo 根目錄執行：

```bash
dir=$(cd "$(mktemp -d)" && go mod download -json go.yaml.in/yaml/v3@v3.0.5 \
      | sed -n 's/^[[:space:]]*"Dir": "\(.*\)",$/\1/p')
dst=components/threshold-exporter/app/third_party/yaml.v3
rm -rf "$dst" && cp -r "$dir" "$dst" && chmod -R u+w "$dst"
patch -p1 -d "$dst" < components/threshold-exporter/app/third_party/yaml.v3-uniquekeys.patch
python3 -m pytest -q tests/ops/test_vendored_yaml_v3.py
```

`go mod download` 會先以 checksum database 驗過 v3.0.5 才給路徑；`patch` 應無 offset／fuzz 提示。

### 上游安全修補要手動 backport

這份原碼不會跟著上游動：上游 `go.yaml.in/yaml/v3` 發布的安全修補**不會**自動進來，
本 repo 的 Renovate 也沒有開 `gomod` manager。

⛔ **掃描器確定看不到它。** binary 的 buildinfo 記的是
`gopkg.in/yaml.v3 v3.0.1 => ./third_party/yaml.v3 (devel)`。#2681 盲審用有漏洞的
v3.0.0-2021… 實測：不 replace 時 trivy 0.74.0 報 CVE-2022-28948、govulncheck v1.8.0 報
GO-2022-0603；改成目錄 replace 後兩者都**不報**（trivy 把套件記成路徑、沒有版本）。
所以 release.yaml 與 nightly-image-scan.yaml 的 image 掃描不會替這份原碼報任何公告。
buildinfo 上的 `v3.0.1` 是佔位版號，不是實際基底；掃描器就算讀它，也是在查錯的版本。

**這是沒有機器守住的風險。** 本 repo 沒有 trivy fs、dependabot 設定檔、govulncheck 或 osv
查詢可以接手（CodeQL default setup 分析的是程式碼，不查 module 公告）。GitHub 的
dependency graph 仍可能從 `require gopkg.in/yaml.v3 v3.0.1` 列出它，但 Dependabot alerts
有沒有開、replace 後是否照列，都未查證，不能當成補償控制。

人工檢查（每次發版前，或看到 YAML 相關公告時）：

```bash
curl -sS -X POST https://api.osv.dev/v1/query \
  -d '{"package":{"name":"go.yaml.in/yaml/v3","ecosystem":"Go"},"version":"3.0.5"}'
```

回 `{}` 代表 OSV 對 v3.0.5 沒有公告。把套件換成 `gopkg.in/yaml.v3`、版本換成 `3.0.0`
會回 `GHSA-hp87-p4gw-j4gq`，可用來確認查詢本身有效。舊路徑 `gopkg.in/yaml.v3` 的公告也要看：
同一段程式的修補可能只登記在其中一條路徑上。

- 看到 yaml.v3 的安全公告時，要人工判斷是否影響 v3.0.5；上游已有修補版本時，優先把基底
  升到那一版（照上面的步驟重新產生，並先做「升級基底時比對了什麼」的比對），不行才把修補做成
  另一份 patch，並同步更新守衛（`_PATCHES` 的 sha256 與允許的 hunk）。
- 上游若收下等效的線性判重，就移除 `replace` 與本目錄，改回一般相依（`go.yaml.in/yaml/v3`，
  各 import 一併改路徑）。
