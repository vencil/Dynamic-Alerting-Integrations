# tenant-api

> 多租戶 alerting 平台的**配置寫入 / 讀取 API**:RBAC 過濾的 CRUD、批次、async task、SSE 事件流,寫入會落成真實 git commit 或可審查 PR/MR——**零資料庫**。
>
> *GitOps-native, RBAC-scoped tenant config API. Writes become Git commits / reviewable PRs. No database.*

## 給誰看

這份是**元件層的技術文件**,依**文件型別**組織(不依組織角色——角色路徑在 [`docs/getting-started/`](../../docs/getting-started/)):

| 你是… | 看這裡 |
|-------|--------|
| **平台工程師 / 維運**(部署、設定 RBAC / 寫回 / 聯邦、接 oauth2-proxy) | 本 README(參考) + [QUICKSTART](QUICKSTART.md)(5 分鐘跑起來) |
| **API 整合 / 自動化**(CI 寫 config、拉 federation token、串 SSE) | 下方 [API 參考](#api-參考) + [QUICKSTART 的整合路徑](QUICKSTART.md) |
| **想自助改告警的租戶 / 領域專家**(人類,用 UI) | 不需碰這個 API → 用 **Tenant Manager portal**;入門見 [租戶指南](../../docs/getting-started/for-tenants.md) |

> 💡 **想先把它跑起來?** → **[QUICKSTART.md](QUICKSTART.md)**(`docker run` + `curl /me`,看到零 DB 的 RBAC 身分)。本篇是完整 endpoint **參考**。
>
> 認證不在這裡做——身分由前置的 **oauth2-proxy**(處理 SSO 登入的前置 proxy)sidecar 注入 header,本服務信任它。

## 這個服務做什麼 / 不做什麼

**做**

- 租戶 / 群組 / saved view 的 CRUD、批次操作、effective config 解析(含繼承來源鏈)
- 租戶**自助宣告式告警**(Custom Alerts):租戶從平台提供的參數化 recipe 產生合法告警,**不需寫 PromQL**(人類走 portal,自動化走 API)
- 租戶設定寫入時做 schema 驗證、交同 pod 的產生器容器判定(routing 與 domain policy 都由它判),再以 git commit-on-write 或開 PR/MR 寫回(群組、saved view 等 tenant-api 自己的紀錄寫進 state store,見[聯邦](#聯邦federation)一節表格下方的說明)
- 以 RBAC 對租戶列表 / 群組成員 / pending PR / async task 結果做**逐呼叫者**過濾
- 租戶聯邦:簽發短效 token 供租戶拉取自己的 metrics 子集、管理聯邦白名單與每租戶子集
- 維運硬化面:逐呼叫者限流、`X-Request-ID` 回拋、request body 大小與內容範圍驗證

**不做**

- **不做認證**——身分來自 oauth2-proxy 注入的 `X-Forwarded-Email` / `X-Forwarded-Groups`
- **不做 schema 演化**——YAML schema 由 threshold-exporter 的 config 套件擁有
- **不做持久化 task store**——async task 存在記憶體,pod 重啟後消失(polling 收到 404 視為 task 遺失)。持久狀態只有聯邦 token 記錄與 tenant-api 自己的紀錄(群組、saved view、聯邦白名單與子集、AccountID 帳本),各存於一個 ConfigMap(單機為一個目錄),非資料庫
- **不做 PR / MR 合併**——建立後等人工 review,只追蹤狀態

## 架構速覽

- **chi router** + 標準 middleware 鏈(RequestID / Logger / Recoverer / Timeout);**不用 `RealIP`**——它會用 client 送的 `X-Forwarded-For`/`X-Real-IP` 覆寫 `RemoteAddr`(可偽造),故保留真 TCP peer(ADR-027)
- **`X-Request-ID` 回拋**:把 request id 寫回 response header,方便對應後端 log
- **逐呼叫者限流**:sliding-window,預設 100 req/min/caller,可關閉
- **RBAC**:`_rbac.yaml` 定義 group → tenant → permission,熱重載(預設 30s)。**未設 `--rbac`** 進入 open-read 模式(啟動 WARN);**設了 `--rbac` 但檔案零 group**(打錯字/空檔)→ **fail-closed 拒絕存取**(ADR-027 MED-8),`--rbac-empty-open` 為 rollback 逃生
- **逐租戶授權**:群組 / view / batch / PR 列表 / task 結果的每個成員都再過一次 per-tenant RBAC
- **GitOps Writer**:schema 驗證 → 寫 YAML → `git commit`(operator email 當 author,service account 當 committer)
- **產生器容器判定每筆寫入**([ADR-036](../../docs/adr/036-single-parser-effective-config.md) §3–§5,[#2903](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2903)):凡是會改變 conf.d 檔案內容的寫入(`PUT /tenants/{id}`、custom-alerts `PUT`、tenant／group 批次,直寫與 PR 兩種模式、同步與 async),在寫入鎖之下、commit 之前,把「要寫入的 base commit + 每個檔的完整新內容」送給同 pod 的產生器容器(`da-tools generator-service`,經 Unix socket,`--generator-socket` 必填),由它對寫入後的整棵樹跑 `generate-routes --validate --strict`;通過才 commit。直寫模式的 base 是鎖下的 HEAD,PR 模式是分支起點;批次整批一次判定。期限:送出請求剩餘的時間(`deadline_ms`;async 批次從 task 開始起算 `--write-timeout`)。驗的和寫的必須是同一份:回應的 base commit、每個檔的 sha256 與部署端 policy 檔(`--deploy-policy`,與產生器的 `--policy` 同一份)的 sha256 都要相符,不符就重驗一次,仍不符(或剩餘時間不夠重驗)回 429;commit 也只落在判定的那個 base 上(直寫時判定期間 HEAD 被外部 commit 移動 → 409,不 commit),且 staging 後 index 的內容須與判定的位元組相同(`.gitattributes` 改了內容 → 422 `normalized`)。`PUT` 內容與現有檔案、也與 HEAD 裡的那份都逐位元組相同時直接成功,不問產生器(只有工作目錄相同、尚未 commit 時照常判定並 commit)。結果對應:有 finding → 422 `GENERATOR_FINDINGS`(列出 finding,與這次寫入的檔／租戶相關的排前面並標 `related`;時間夠時再判一次寫入前的 base,標出 `pre_existing`;不相關的 finding 只給對所有租戶有寫入權限的呼叫者看,其他人只看到數量);產生器這次沒能判定 → `GENERATOR_REJECTED`,`reason` 為 `busy`／`deadline`／`git_read`／`mismatch`／`no_time` 時 429(帶 `Retry-After`),`memory`／`incomplete`／`too_large`(整批寫入後的檔案太大,請拆批)／`invalid_request`、`normalized`(git 屬性——`.gitattributes` 的 eol、text 或 filter——在 staging 時改了檔案內容,commit 的位元組不會是判定過的那份)時 422;產生器不可用 → 503 `VALIDATOR_UNAVAILABLE`(連不上、逾時無回應、回應無法解析、找不到 amtool、版本太舊)或 `POLICY_UNAVAILABLE`(部署端 policy 檔不可用),原因在 `reason`。`GENERATOR_REJECTED` 與 503 都只回 code、`reason` 與固定說明,細節只寫進 server log(訊息可能含路徑、例外內容或其他租戶的檔名,不回給 client);不認得的 reason 一律顯示 `unknown`。任何拒收都不寫入、不 commit、不留暫存
- **衝突偵測**:寫入後比對 commit 的 parent 與寫入前的 HEAD;若期間被外部 commit 移動 → 回 409
- **租戶檔解析**:一個租戶的設定檔可拼成 `<id>.yaml` 或 `<id>.yml`(副檔名大小寫不敏感),讀取與寫入都解析**同一個實際檔案**——寫入落在既有檔上,只有全新租戶才用 `.yaml` 預設拼法。同一個 id 被兩種拼法同時宣告時 **fail-loud**:`GET`／`PUT`／diff／custom-alerts 只看檔名就回 409；`/effective` 則在兩個檔都確實宣告這個租戶時才回 409,另一個拼法是 `tenants: {}`、只宣告別的租戶、解析不了、dangling symlink 或指向目錄的 symlink 時照常回 200(判定用 exporter 同一支 walker)。兩者都是 `CONFLICT`,訊息只帶兩個基底檔名(如 `<id>.yaml`、`<id>.yml`),不含伺服器路徑([#2511](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2511))，`POST /validate` 回 200 `valid: false`，租戶清單整個以 500 失敗並指名兩個檔(與 threshold-exporter 的 `DuplicateTenantError` 同一立場,不做優先序猜測)
- **內容範圍驗證**:固定欄位走 struct validator,`Patch` / `Filters` map 走逐 key 規則,違反一次回完整清單
- **API 管理與 git 管理**([#3064](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3064),breaking):tenant-api 只管理檔案(依檔名找到的 `<id>.yaml`)是「API 形狀」的租戶——只有一份 YAML document、`tenants:` 只宣告這個租戶且 id 等於檔名、頂層與 `tenants:` 底下沒有 anchor／alias／merge key／帶 tag 的 key(租戶自己段落裡面的 anchor／alias 不受限)。這種檔只屬於該租戶,`GET` 照舊回原文、`source_hash`、`custom_alerts`(`managed_by: "api"`),`X-DA-Base-Hash` 與 custom-alerts 的 `base_hash` 照常使用。其他形狀(多租戶共用檔、檔名與 id 不同、多份 document、頂層 anchor、不是 YAML)是 git 管理:`GET` 只回 effective 結果與 `managed_by: "git"`、`git_managed_reason`,不回 `raw_yaml`、`source_hash`、`custom_alerts`,`/effective` 的 `source_hash` 也對任何呼叫者為空(帶 `source_hash_withheld: true`);`PUT`、custom-alerts、批次、群組批次(兩種寫入模式)一律回 409 `TENANT_GIT_MANAGED`(批次是該筆結果的 code),`/diff` 同,`/validate` 回 `valid: false` 加 `code: TENANT_GIT_MANAGED`;請直接改 git。判定只有一份(`internal/confd/managed.go`):走 YAML 節點判形狀,再併入 exporter 解碼器讀到的 id 作為縱深防禦。完整契約見 [tenant-api-hardening §3.1](../../docs/api/tenant-api-hardening.md)
- **憑證遮罩**([#1560](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1560)):租戶檔的 receiver 憑證(webhook / Slack / Teams / Rocket.Chat 的 URL、PagerDuty key、SMTP 密碼、整個 `http_config` / `tls_config` / header map,以及 Alertmanager 同類欄位)只給**對該租戶有寫入權限**的呼叫者看——判定與 `PUT /tenants/{id}` 的閘門是同一個述詞,所以看到遮罩的人一定不能 `PUT`。其他人在 `GET /tenants/{id}`(`raw_yaml`、`custom_alerts`)、`/effective`、`POST /diff` 看到的值是 `<masked: write permission required>`,回應帶 `masked: true`;`raw_yaml` 是重新序列化、**不含註解**的版本(註解掉的舊憑證也算憑證)。依 key 名比對、不限位置,所以同名但不是憑證的值(例如某個 `url`)也會被遮。租戶段落內用了 anchor / alias / merge key 時無法確定遮得乾淨,`GET` 改回空的 `raw_yaml` 與 `custom_alerts` 加 `raw_yaml_withheld: true`,`/diff` 回 422 `MASKED_PREVIEW_UNAVAILABLE`。git 管理的租戶(見下方「API 管理與 git 管理」)任何人都拿不到原文,`/effective` 的 `source_hash` 也對任何人為空並帶 `source_hash_withheld: true`。遮罩後的 `/diff` 對兩側的遮罩版本比對,只改憑證的提案 `has_diff` 為 `false`;提案超過租戶文件大小上限或 request body 上限回 413。`PUT` 的 body 只要有任何值解碼後等於這個佔位字串就回 400(`/validate` 回 `valid: false`),避免遮罩後的內容被寫回蓋掉真值。被遮罩的人拿到的 `source_hash` / `merged_hash` 是空字串(否則可離線驗證猜測的憑證);key 帶 `!!str` 以外的明確 tag 也算無法確定遮乾淨。完整說明見 [tenant-api-hardening §3.7](../../docs/api/tenant-api-hardening.md)
- **Routing 與 domain policy 只由產生器判**(**breaking**,[#2903](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2903),[ADR-036](../../docs/adr/036-single-parser-effective-config.md) §4):tenant-api 不再自己讀 `_domain_policy.yaml`、不再自己判 routing。domain policy 違規(含 `require_critical_escalation`)、receiver 形狀、不是 mapping 的 `_routing`、matcher 或 `group_by` 的值、產生器不讀的鍵,以及 `_domain_policy.yaml`／`.yml` 讀不了,都是產生器對寫入後整棵樹回報的 finding,回 422 `GENERATOR_FINDINGS`(見上一項),直寫與 PR 模式、`PUT` 與批次都一樣;不再有 403 `POLICY_VIOLATION`、tenant-api 自己判 policy 時的 503 `POLICY_UNAVAILABLE`,PUT 這幾類也不再回 400 `INVALID_BODY`。critical 告警可能被提早攔走的警告(產生器的 WARN)不會擋寫入,也不再附在回應的 `warnings`。Go 端只留兩項 400:租戶寫了扁平鍵 `_routing_receiver_type`(租戶鍵驗證擋下,`BAD_REQUEST`,[#2983](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2983)),以及 receiver 憑證欄位是遮罩佔位字串(`INVALID_BODY`,見上方「憑證遮罩」)。批次 patch 的鍵驗證照舊:只能寫 `_routing` 與 `_routing_profile` 這兩個 `_routing` 開頭的鍵
- **Async task**:worker goroutine 池跑批次,`/api/v1/tasks/{id}` polling
- **SSE 事件**:寫入成功後廣播 `config_change`,讓 UI 即時更新
- **Path traversal 防護**:租戶 id 驗證拒絕 `..`、`/`、`\`
- **寫入套用 tenant id 規則**(ADR-035):寫入的 id 必須是 DNS-1123 label(小寫字母、數字與 `-`,首尾為字母或數字,長度上限見 `docs/schemas/tenant-config.schema.json` 的 `definitions.tenantId`)。`PUT`、`POST /{id}/validate`、custom-alerts `PUT`、聯邦子集 `PUT` 在 handler 先回 400,租戶 batch 則是該筆結果 `status: error`(整批拒收,見下方批次列);writer 的咽喉點 `gitops.guardTenantID` 對每個租戶寫入再判一次(`ErrInvalidTenantID`,`PUT` 等端點回 400),漏了 handler 那一關也寫不進去。讀取端點不套用,既有的不合規 id 仍可讀
- **自帶輕量 Prometheus metrics**(無額外 client 函式庫依賴)、安全預設(header read timeout、1 MB body 上限、non-root 容器)

## API 參考

> 慣例:除 Health / Identity / Metrics 外皆需身分(oauth2-proxy header)。「權限」欄為該端點要求的 RBAC 動作;標「逐租戶」者會對每個受影響租戶再驗一次。路徑不帶 `{id}` 租戶的端點,`read` / `write` 指**至少對一個租戶**有該權限([#2520](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2520);只看呼叫者自己的規則,不看租戶是否存在),回應內容再由 handler 逐租戶過濾;標 `(*)` 的端點則要求規則寫了 `tenants: ["*"]`。

### Health / Identity / Metrics(無需認證)

| Method | Path | 說明 |
|--------|------|------|
| `GET` | `/health` | Liveness——永遠 200 |
| `GET` | `/ready` | Readiness——config 目錄無法存取（或啟用 human socket 時其自測失敗）回 503；body 只有 `status` 與固定的 `reason`，路徑與錯誤原文只進 server log（[#2811](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2811)） |
| `GET` | `/metrics` | Prometheus 文字格式 |

### 租戶配置

| Method | Path | 權限 | 說明 |
|--------|------|------|------|
| `GET` | `/api/v1/me` | 身分(不需授權) | 當前呼叫者的 email + groups + 命中他的規則摘要;沒有任何授權也回 200(空的 `permissions` / `accessible_tenants`),缺身分 401 |
| `GET` | `/api/v1/tenants` | read | 列出 RBAC 可見的租戶;設定檔存在但無法使用的租戶以**降級列**回傳(只有 `id` + `config_error`,見下方「降級列」),不再靜默消失;每筆的 `config_derived`(`silent_targets` / `maintenance_active`)見下方「依設定推算的狀態」;每筆帶 `managed_by`(`api` / `git`,git 時加 `git_managed_reason`),與 `GET /tenants/{id}` 對同一檔的判定相同,檔名與宣告 id 不同的檔標 `id_mismatch` 與 `metadata_incomplete`、不再是不帶任何標記的空列,可見性比照降級列(`unreadable` / `not_regular_file` 的降級列不帶這兩個欄位,[#3157](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3157)) |
| `GET` | `/api/v1/tenants/search` | read | 伺服端 search / filter / 分頁(`q` / `environment` / `tier` / `domain` / `db_type` / `tag` / `page_size` / `offset` / `sort`);與 `/tenants` 共用快照快取。回應頂層的 `config_derivation` 附推算時間,並列出解析失敗而被跳過的檔案(`parse_failed_files`) |
| `GET` | `/api/v1/tenants/{id}` | read | 取得 raw YAML + `resolved_thresholds`(套根目錄的 defaults carrier、根目錄平台檔的 `tenants:` 區塊與租戶選用的 `_profile`,見下方「單一租戶端點與 conf.d 範圍」);`.yaml` / `.yml` 兩種拼法皆可解析,同一 id 兩種拼法並存回 409;對該租戶沒有寫入權限的呼叫者看到遮罩後的憑證(見上方「憑證遮罩」);`declared_elsewhere` 為 `true` 表示伺服器目前讀到的樹裡,有自己的檔以外的檔宣告同一 id——也就是 409 `TENANT_DECLARED_ELSEWHERE` 判的條件,判法同 `/diff`。它不保證寫入的回應:寫入可能先因別的原因被拒,PR 模式則以最新 base 判定(只是布林、不點名檔案;conf.d walk 跑不了時欄位不出現,[#3172](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3172)) |
| `GET` | `/api/v1/tenants/{id}/effective` | read | 沿 `_defaults.yaml` 鏈逐層合併租戶覆寫後的**設定**(對齊 `describe_tenant`),不是 exporter 會 emit 的值(見下方「單一租戶端點與 conf.d 範圍」)+ 繼承來源鏈 + 雙重 hash(`source_hash` / `merged_hash`,供變更偵測);值照原文保留,defaults 鏈、根目錄缺 `defaults:` 包裝、平台檔／租戶檔值解析不了而 exporter 不送的,在 `not_served` 逐 key 標原因(profile 層被丟掉的值與過期 override 目前不標),defaults 鏈上 exporter 整份不讀的語法錯檔當空檔、列在 `chain_parse_failed`(仍回 200,[#2296](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2296));同一 id 的兩種拼法都宣告該租戶時回 409;對該租戶沒有寫入權限的呼叫者看到遮罩後的憑證(見上方「憑證遮罩」) |
| `GET` | `/api/v1/tenants/{id}/access` | read | 輕量 RBAC 授權探測:可讀該租戶回 `200 {allow,tenant,permission}`、否則 `403`。供姊妹服務(如 recipe-preview #657)重用 tenant-isolation 決策、不重寫 RBAC 也不過度取得設定 |
| `GET` | `/api/v1/audit/tenants/{id}/access-report` | platform admin(非 org-scoped) | 逆向存取稽核報告:列出「誰、經哪條規則、在什麼 org 條件下」能存取該租戶(shadow/enforce 雙態並列;audit-only,不參與授權)。`?include=org_values` 展開 org 值、`?view=redacted` 去識別化投影;非 admin 恆定 403(防租戶枚舉)。redacted 視圖無法消除 grant 存在性本身的 org-membership 推論(value-pinned org rule 的 grant entry 即弱識別)。**⚠️ environments/domains 為 rule 原文照錄、僅約束租戶清單可見性、不阻擋 read-by-id/write**——受影響 grant 以機器可讀欄 `constraints_not_evaluated` 標示,稽核判讀勿當作存取邊界 |
| `POST` | `/api/v1/audit/tenants/{id}/access-report/dry-run` | platform admin(非 org-scoped) | what-if 稽核:body 送候選 `_rbac.yaml`(`{"candidate":{"rbac_yaml":"..."}}`),與 live 基準各算一份逆向報告並做結構化 diff(changed / added / removed;以 rule name 對齊,rename 呈現為 removed+added)。純模擬、不寫入;query 同上(`include` / `view`);orgs 沿用 live `_tenant_orgs.yaml`;候選解析失敗回 400 `CANDIDATE_INVALID`;非 admin 恆定 403(同上) |
| `PUT` | `/api/v1/tenants/{id}` | write | 寫入(驗證 → 產生器判定 → commit / PR),body 整檔取代租戶檔。只寫 API 管理的租戶([#3064](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3064),**breaking**):git 管理的租戶(共用檔等)回 409 `TENANT_GIT_MANAGED`(在 `X-DA-Base-Hash` 之前判,PR 模式在最新 base 上判);body 也必須是 API 形狀,只宣告 `{id}`,否則 400;body 格式錯誤回 400;同一 id 兩種拼法並存回 409;租戶已由其他檔宣告回 409 `TENANT_DECLARED_ELSEWHERE`(見下方「單一租戶端點與 conf.d 範圍」);conf.d 有讀不了的項目時拒收(**breaking**,#2939,見 [API 硬化](../../docs/api/tenant-api-hardening.md) §3.8):別處有無法解析的 symlink(懸空、迴圈、路徑中段不是目錄)回 409 `TENANT_TREE_UNREADABLE`(在 git 修);tenant-api 那份 conf.d 有伺服器讀不了的項目(FIFO 等非一般檔、讀取失敗的檔、列不出的子目錄)回 500 `TENANT_STORE_FAULT`(在伺服器或 volume 修,兩種並存時回這個);要寫的租戶檔本身是這種 symlink(含指向自己的迴圈)時照常寫入、換成一般檔,本身讀不了或是 FIFO 時回 409 `TENANT_CONFIG_NOT_LOADABLE`(任何角色皆同);直寫模式下，租戶檔在 tenant-api 那份工作樹（含 index）與 HEAD 不同時（tenant-api 以外未 commit 的修改、staged、刪除、未追蹤的同名檔、`skip-worktree`／`assume-unchanged` 標記，或經 symlink 抵達時沿路連結或 repo 內目標被改；目標在 repo 外、巢狀 repo 或 submodule 內則不判；只擋進寫入鎖時已存在的不一致）回 409 `TENANT_FILE_DIRTY`（**breaking**，[#1535](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1535)；固定訊息、不含路徑；`/validate`、`/diff` 同判；batch 為整批 422、該筆 `code: TENANT_FILE_DIRTY`），先在 repo 裡 commit 或捨棄再重試；直寫中途失敗（含程序被殺）而沒能放回的租戶檔，由寫檔前就寫下的 publish marker 記著，下一次寫入或重啟時在分支讀得到後先收尾，收尾完成就不再因此回這個錯誤（收尾前寫入失敗並指出 marker）（[#3173](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3173)） |
| `POST` | `/api/v1/tenants/{id}/validate` | read | Dry-run 驗證,不寫入;依 `PUT` 的拒收順序判(#3062);git 管理的租戶回 `valid: false`、`warnings` 是 `PUT` 的 409 文字、`code: TENANT_GIT_MANAGED`(直寫模式;PR 模式只檢查 body)([#3064](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3064));conf.d 有讀不了的項目時回 `valid: false`,`warnings` 與 `PUT` 的 409 `TENANT_TREE_UNREADABLE`／500 `TENANT_STORE_FAULT`／409 `TENANT_CONFIG_NOT_LOADABLE` 同一段文字(PR 模式只檢查 body、不讀樹,不預告這些拒收) |
| `POST` | `/api/v1/tenants/{id}/diff` | read | 預覽 unified diff(標頭是 `current/<id>.yaml` / `proposed/<id>.yaml`,不含伺服器路徑);提議側是 `PUT` 要寫入的 body;`PUT` 會拒的回同一個狀態碼與 body(#3062),git 管理的租戶回 409 `TENANT_GIT_MANAGED`([#3064](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3064));同一 id 兩種拼法並存回 409;租戶已由其他檔宣告回 409 `TENANT_DECLARED_ELSEWHERE`;conf.d 有讀不了的項目時回與它預覽的 `PUT` 相同的 409 `TENANT_TREE_UNREADABLE` 或 500 `TENANT_STORE_FAULT`;對該租戶沒有寫入權限的呼叫者拿到的是兩側都遮罩後的 diff(見上方「憑證遮罩」) |
| `POST` | `/api/v1/tenants/batch` | read + 逐租戶 write | 批次**部分合併** patch(只改指定 key、保留其餘 key 與註解,非整檔取代;逐筆 RBAC;`?async=true` 走 task 池)。**全有全無、一次 commit**([#2900](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2900)):每筆 op 先過既有檢查(tenant id、寫入權限,再於寫入鎖內依序合併、驗證,後面的 op 判在前面 op 留下的樹上;整批套用後的樹再由產生器判一次),全部通過才把改到的檔一起寫入——直寫模式一個 commit,PR 模式一支只有一個 commit 的 PR/MR。任何一筆被拒就整批不寫:回 **422**,`status: rejected`,`results` 依請求順序逐筆列出,被拒的是 `status: error`(`message`、`code`),其餘是 `status: not_applied`;`summary` 如 `1 failed, 2 not applied`。非同步批次在執行時才判定,整批拒收時 task 為 `failed`、`error` 說明整批拒收,`results` 同上。批次在 `<git dir>/batch-worktrees/` 下另開的暫存 git worktree 裡套用與判定(直寫模式複製服務中 conf.d 的現況,PR 模式取最新 origin base),全部通過才在那裡 commit;直寫模式再以 compare-and-swap 把分支推進到該 commit,並只更新這批改到的檔。先推進分支、再更新工作目錄。保證範圍:讀者不會看到從未 commit 的內容——批次進行中、被拒、取消或中途失敗時看到的都是原狀;更新工作目錄的那一小段期間,可能看到這批的檔有的已是新版、有的仍是舊版,但都是已 commit 的內容。若在推進分支後、更新工作目錄前崩潰,下次啟動或下一次寫入取得寫入鎖後,會先把這批的路徑重設成分支上的內容(只動這批的 conf.d 路徑)。這個補救本身失敗時(例如 repo 損壞、取不到那個 commit),紀錄檔 `<git common dir>/tenant-api-batch-publish` 會保留,之後**每一次寫入都失敗**,直到補救成功;server log 的錯誤會指出這個檔——若它記的 commit 已不存在,先確認服務中的工作目錄與分支一致,再刪掉它。暫存 worktree 用完即刪,程序被殺留下的會在下次啟動時清掉。PR 模式在最新 base 上判出的拒收同樣是 422 逐筆結果(`code` 如 `FORBIDDEN`、`TENANT_DECLARED_ELSEWHERE`、`TENANT_CONFIG_NOT_LOADABLE`、`TENANT_TREE_UNREADABLE`、`CONFIG_DECODE_ERROR`),不開 PR、不留分支。寫入後的樹有無法解析的 symlink(本批各筆的租戶檔除外)時,直寫與 PR 模式都整批回 422,每一筆沒有其他拒收原因的 op 帶 `code: TENANT_TREE_UNREADABLE`;tenant-api 那份 conf.d 有伺服器讀不了的項目時整批回 500 `TENANT_STORE_FAULT`(不是逐筆:問題不屬於任何一筆)(#2939,見 [API 硬化](../../docs/api/tenant-api-hardening.md) §3.8);要寫的租戶檔本身是 FIFO 等非一般檔、目錄或讀不了時,該筆 `code: TENANT_CONFIG_NOT_LOADABLE`;合併後的檔與原檔逐個 yaml.v3 節點比對,編輯以外的寫法(style、引號、tag、原文、anchor、別名所指)必須完全相同,被編輯的鍵解碼後也要是新值或已刪除,否則(重新輸出會連帶改到沒編輯的內容)拒收,同樣是 `TENANT_CONFIG_NOT_LOADABLE`,`config_error` 為 `not_round_trippable`([#2903](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2903),ADR-036 §4)。整批層級的失敗(寫入佇列滿 503 `WRITE_OVERLOADED`、worktree 停在 PR 分支 503 `TREE_NOT_ON_BASE`、取消或 git 逾時而分支沒動的 503、commit 失敗 500)都不寫入;唯一例外是推進分支回錯、又讀不回分支的 503「可能已寫入,請重新整理」([#3173](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3173)),那時保留的 publish marker 會在下次寫入或重啟時把工作目錄帶到分支上。直寫模式的 409 `CONFLICT` 有兩種,以 `committed` 區分:`false` 是批次執行期間 repo 被 tenant-api 以外的人改動,什麼都沒寫,重新讀取後重送;`true`(帶 `commit`)是整批**已經** commit,只是伺服器端收尾沒完成,先重新讀取確認,不要直接重送。每筆 op 另可帶 `unset: ["_routing"]` 刪除 key(目前只接受 `_routing` 與 `_routing_receiver_type`;後者是 tenant-api 以前會寫入的扁平鍵,產生器不讀,留在檔裡時這個租戶的其他寫入都會被 key 驗證擋下,所以要先刪掉它,[#2983](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2983);刪掉即回到 `_routing_defaults` + profile,重新啟用被停用的路由;key 或租戶不存在時為 no-op:不寫入、不開 PR、不建立租戶)。routing 與 domain policy 由產生器對整批套用後的整棵樹判:樹上任何會擋下的 finding 都讓整批拒收(422 `GENERATOR_FINDINGS`),包括 patch 沒碰到的租戶既有的違規 |

> **寫入回應**:`PUT /{id}` 回 `{"status","tenant_id"}`;PR 模式另含 `pr_url` / `pr_number`(CI 可據此取得待審 PR)。request body 直接送租戶 YAML,不需特定 `Content-Type`。

> **降級列(#1680)**:`GET /api/v1/tenants` 與 `/search` 對「conf.d 裡有這個租戶檔、但檔案無法使用」的租戶回一列 `{"id":"<id>","config_error":"<原因>"}`,其餘欄位全空——它的 metadata 是**未知**,不是「未標記」。`config_error` 的值是穩定契約:`unreadable`(stat/讀取失敗,例如斷掉的 symlink、權限不足)、`not_regular_file`(例如指向目錄的 symlink)、`malformed_yaml`(不是合法 YAML)、`invalid_config`(語法層面可解析為 YAML,但無法載入為租戶設定:結構不對、只有型別化解碼才會抓到的錯誤(例如重複的 key)、泛型解碼(解成 `any`)失敗的檔(#3063 起,例如顯式 tag 與內容不符的純量 `!!int abc`、未知頂層 key 內的重複 key;`/effective` 與路由產生器同樣不收),或宣告了非 UTF-8 的租戶 id——threshold-exporter 會整份跳過這種檔);健康的列沒有這個欄位。空檔視為可用(列出一列無 metadata 的租戶,與過去相同)。**只有**命中規則對 `environments` 與 `domains` **都不設限**的呼叫者看得到降級列,shadow 與 enforce 模式皆然——受限呼叫者看不到,因為該租戶真實的環境/域讀不出來,不能當成未標記放行;org 軸照常判定(組織清單來自 `_tenant_orgs.yaml`,不受壞檔影響)。`/search` 的 metadata 篩選(`environment` / `tier` / `domain` / `db_type` / `tag`)不會命中降級列,`q` 仍比對其 `id`。`GET /api/v1/tenants/{id}` 對 `malformed_yaml` / `invalid_config` 的檔回 **200** 與同義的 `config_error`;API 形狀的檔另帶 `raw_yaml`、`source_hash`,讓編輯器仍能開檔修正。不是 YAML 的檔、共用檔等是 git 管理([#3064](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3064)),不帶 `raw_yaml` 與 `source_hash`,寫入一律 409 `TENANT_GIT_MANAGED`,要在 git 修;exporter 不 serve 這種檔,所以**不帶** `resolved_thresholds`、`custom_alerts`、`validation_warnings` / `validation_notices`(不是空陣列——空陣列會被讀成「確定沒有」)。對這種檔的部分更新——`PUT /{id}/custom-alerts`、`POST /tenants/batch`、`POST /groups/{id}/batch`——一律拒絕且不寫入:前者回 409 `TENANT_CONFIG_NOT_LOADABLE` 並帶 `tenant_id`、`config_error`,batch 在直寫模式是該筆結果 `code: TENANT_CONFIG_NOT_LOADABLE`(整批 422 不寫)、PR 模式同樣是該筆結果、整批 422(判定以 PR 分支所依據的最新 origin base 為準)。須先修好租戶檔本身:只有 API 形狀的 `invalid_config` 租戶檔可以用整檔 `PUT /{id}` 整份覆蓋修回(#2373、#2405),git 管理的壞檔(不是 YAML、`tenants:` 不是 mapping 等)一律在 git 修;其他 `config_error`(讀不到、不是一般檔)能否以 `PUT` 修回取決於底層檔案狀態,不保證,必要時直接改 git。⚠️ API 形狀的現有檔,tenant-api 的 eol 檢查**解碼不了**它時,整檔 `PUT` 只接受**對所有租戶都有寫入權限**的呼叫者:命中一條 `tenants: ["*"]`、授予 `write`(或 `admin`)且不帶 `org-scope` / `environments` / `domains` 的規則——前綴樣式(如 `svc-*`)不算,帶 scope 限制的 `"*"` 規則不論 scope 軸處於 shadow 或 enforce 都不算。其他呼叫者回 400、檔案不動,訊息指明需要全租戶寫入權限,否則直接改 git。直寫模式下 `POST /{id}/validate` 的判定跟著呼叫者走;PR 模式的 dry-run 只檢查 body、不讀現有檔,所以反映不了這項限制(可能回 `valid: true`,實際 `PUT` 仍回 400)。放行時,eol 檢查把舊檔的 end-of-life recipe 用量當成**零**:body 不含 eol recipe 就放行,含了就擋——不會因為解析不了舊檔就放寬;解析得了的檔照實際用量比對;檔案存在卻讀不到(權限、I/O、指向目錄)則直接拒絕寫入。「解析不了」以 eol 檢查的解碼為準(只解碼 `tenants:` 區塊);不是 YAML 的檔是 git 管理,到不了這一步(#3064)。federation 的 orphan 偵測、啟動時的 registry 完整性檢查與寫入平面的檔案解析刻意**不看檔案內容**,壞檔租戶在這些平面仍算活著——所以 API 形狀的壞檔仍能以 `PUT` 修回來。

> **metadata 不完整(#2370)**:租戶的 metadata 是根目錄平台檔 `tenants.<id>._metadata` 與租戶檔 `_metadata` 的逐鍵合併(同鍵租戶檔贏)。tenant-api 讀不到根目錄平台檔(stat/讀取失敗或逾時)時,`GET /api/v1/tenants` 與 `/search` 的每個健康列只帶租戶檔自己的 metadata 值,並標 `"metadata_incomplete": true`;可見性比照降級列(只有命中規則對 `environments` 與 `domains` 都不設限的呼叫者看得到),`/search` 的 metadata 篩選不會命中,`q` 只比對 `id`。平台檔可讀時，只有兩種列仍帶這個欄位(可見性同樣比照降級列):租戶檔自己的 `_metadata` 解碼不了;或檔案是 git 管理、且檔內沒有宣告這一列的 id(`id_mismatch` 等,[#3157](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3157))。寫入判定在平台檔讀不到時只用租戶檔的 `_metadata`。

#### 依設定推算的狀態(`config_derived`)

`GET /tenants` 與 `/tenants/search` 的每個租戶帶 `config_derived`:`silent_targets`(會被靜音的 severity)與 `maintenance_active`。它回答的是「依 tenant-api 手上這份 conf.d,threshold-exporter 會 emit 什麼」,**不是**從 Alertmanager 或線上 exporter 觀測到的狀態——rollout 有時間差,conf.d 範圍也不同(見下方「單一租戶端點與 conf.d 範圍」)。UI 文案標「依設定推算」。

- **同一份計算**:config 由 `config.LoadDir`(exporter 的冷載入)取得,狀態由 `OperationalStatesAt(now).ByTenant` 在每次請求時判讀,所以 `expires` 以請求當下為準。舊的 `silent_mode` / `maintenance` 欄位是租戶檔裡的原始值(`disable` 也是非空字串),不代表狀態。
- **快取**:載入結果放在快照快取裡,不是每個請求都重載。載入只在**不必等待**就拿得到 GitOps writer 的鎖時進行,所以不會讀到寫入進行中的樹,讀取也不會等寫入;單次載入持鎖有時限,超時(例如讀到會卡住的檔案)就放開鎖、回傳不帶 `config_derived` 的清單,並在卡住的那次返回前不再開始新的載入。writer 每段寫入結束時(成功或失敗)會把快照標為過期,**過期的快照不會再被當成已知狀態回傳**:writer 正在寫入時,尚未過期的快照照常回傳(`config_loaded_at` 標出它的時間,可能超過 TTL),已過期的則回傳不帶 `config_derived` 的清單;同一時間只有一個請求在重載,其他請求等它的結果,不會把「另一個請求在重載」誤判成「寫入進行中」。PR 模式下,工作樹不在 base branch 上、或有 loader 會讀到的未提交變更(已追蹤檔案的修改,或未追蹤的 `.yaml`/`.yml`)時(例如 PR 寫入後切回 base 失敗),不推算、不快取,各租戶不帶 `config_derived`;其他未追蹤檔案(如寫入中斷留下的暫存檔)不影響。沒有推算時,原始的 `silent_mode` / `maintenance` 也不回傳。API 以外的變更(git pull、ConfigMap 更換)最慢在 TTL 到期後生效——tenant-api 沒有 conf.d watcher。
- **解析失敗的檔案**:exporter 會跳過無法解析的檔案,這些租戶沒有 `config_derived`(根目錄的租戶檔以上方的降級列出現)。`/tenants/search` 的 `config_derivation.parse_failed_files` 列出這些檔案,讓呼叫端分得出「檔案壞了」與「沒有這個租戶」;子目錄的檔案與 `_` 平台檔只出現在這裡。
- **誰看得到什麼**:只有不受限的呼叫者(open mode,或規則對 org、environments、domains 都不設限的 platform admin)拿到相對於 conf.d 的完整路徑,以及載入失敗時 loader 的原始訊息(`load_error`,已去掉伺服器上的絕對路徑)。其他呼叫者的 `load_error` 是固定字串,不含任何檔名或租戶 id;`parse_failed_files` 只給檔名(不含目錄),且只列出呼叫者依降級列規則看得到的租戶的檔案,其餘不列也不計數(#3123:不再有 `parse_failed_hidden`)。conf.d 整份載入失敗(例如同一租戶宣告在兩個檔)時,各租戶不帶 `config_derived`。

#### 單一租戶端點與 conf.d 範圍

- **平台檔的 `tenants:` 區塊**:根目錄平台檔(`_` 開頭、非未被選用的 defaults carrier,例如 `_platform.yaml`)的 `tenants.<id>` 是平台替該租戶設的預設值。`GET /{id}` 的 `resolved_thresholds` 與 `/effective` 都套用它,規則與 exporter 的 `/metrics` 相同:租戶檔逐 key 優先、平台檔不能建立租戶、多個平台檔依檔名排序後者優先(#2019、#2208)。唯一刻意的差異是 `_metadata`:這兩個端點沿用 `/effective` 的規則,不繼承平台檔替租戶設的 `_metadata`;`/metrics` 的 metadata 則會帶入。`GET /{id}` 沒有 metadata 欄位,所以目前看不到這個差異。租戶檔寫舊拼法(如 `mysql_cpu`)、平台檔寫新拼法(`mysql_threads_running`)時,生效的是平台值(與 `/metrics` 相同),`validation_notices` 會說明租戶的值沒有套用,並提示改成新拼法。子目錄的平台檔、隱藏檔、無法解析的平台檔都不套用。
- **驗證只判租戶自己寫的 key**:`validation_warnings`(寫入這份檔會被擋的原因)與寫入驗證只看租戶檔本身;平台檔替該租戶設的 key 有問題(未知 key、`expires:` 格式錯、懸空的 `_critical`、未知 `_profile`、錯誤的 version label、舊拼法)**不擋寫入**,改以 `validation_notices`(寫入回應的 notices 同)回報,訊息帶 `platform file <檔名>, entry tenants.<id>:`(檔名是相對 conf.d 的檔名,不含伺服器路徑),要到那個平台檔修(#2208)。
- **展開 `_profile`**:`GET /{id}` 的 `resolved_thresholds` 與寫入驗證都照 exporter 的 `/metrics` 展開租戶選用的 profile(#1385):profile 定義在根目錄平台檔的 `profiles:`(`_profiles.yaml`、defaults carrier 或其他根目錄 `_` 檔;子目錄的檔、未被選用的 carrier、無法解析的檔都不算),多個檔定義同名 profile 時依檔名排序、逐 key 後者優先;選用的是租戶檔的 `_profile`,沒寫才用平台檔 `tenants:` 區塊替它選的。優先序:租戶檔 > 平台檔 `tenants:` 區塊 > profile(只補缺)> defaults;profile 不會補 `optional_overrides` 宣告而無平台值的 key。profile 讀自同一次有時限的根目錄讀取,不另外讀檔。
- **profile 的問題不擋寫入**:租戶的 `_profile` 指向任何根目錄平台檔都沒定義的名字,仍回 unknown profile(與過去相同,擋寫入);有定義的不再誤報。profile 補給租戶的那些 key 有問題(未知 key、`expires:` 格式錯、懸空的 `_critical`、錯誤的 version label、宣告而無平台值的 key)**不擋寫入**,改以 `validation_notices` 回報,訊息帶 `platform file <檔名>, profile "<名稱>"`,要到那個檔修。值本身無法解析(例如 `abc`)與租戶檔相同:不報,解析時退回 defaults。exporter 對這兩類情況(未知 profile、宣告而無平台值的 key)寫的 WARN,tenant-api 不寫進自己的 log——同樣的資訊已在回應的 warnings / notices 裡。
- **讀根目錄平台檔有時限**:`GET /{id}` 會讀根目錄所有 `_*.yaml` / `_*.yml`。讀取進行中才進來的 `GET /{id}`(不論哪個租戶)共用那一次讀取,看到的是**那次讀取開始時**的檔案內容——讀取開始後才改寫的平台檔,這些請求看不到;這個過期時間窗最多等於那一次讀取的耗時,上限是讀取逾時。讀取結束之後才開始的請求一定重讀,讀完的結果不保留。讀不完(例如卡住的掛載;名為 `_x.yaml` 的 FIFO 自 [#2939](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2939) 起不開不讀,不會卡住)時回 500 `INTERNAL_ERROR`(固定訊息,不含路徑),一次卡住的讀取只佔一條 goroutine,不會隨並發請求數增加;卡住的那次讀取返回前,之後的 `GET /{id}` 立即回 500;刪掉那個檔不會解除,要讓讀取返回或重啟 tenant-api。
- **只認 `<id>.yaml` 租戶檔**:tenant-api 以 conf.d 頂層的 `<id>.yaml`(或 `.yml`)認租戶檔。宣告在其他檔(子目錄檔,或頂層共用檔的 `tenants:`)的租戶,`GET` 回 404、`PUT` 回 409 `TENANT_DECLARED_ELSEWHERE`(batch 見下),不會寫進 `<id>.yaml`;custom-alerts 只接受既有的 `<id>.yaml`,沒有這個檔時回 404——同一 id 出現在兩個檔,exporter 會拒收整份設定(#2078)。`<id>.yaml` 已存在但沒宣告任何租戶(例如 `tenants: {}`)時仍是 API 形狀:`GET` 回 200 並顯示該檔內容,`PUT` 會把這個 id 寫進去;`<id>.yaml` 只宣告了另一個 id 時是 git 管理(`id_mismatch`,見上方「API 管理與 git 管理」):`GET` 只回 effective 結果,`PUT` 回 409 `TENANT_GIT_MANAGED`。檔名與宣告的 id 不同、或一個檔宣告多個租戶的樹,`da-tools validate-config` 的 `tenant_file_name` 列報 FAIL(#3157),在 CI 就擋下。`<id>.yaml` 與另一個檔都宣告同一 id 時,那棵樹 exporter 本來就不收,連更新 `<id>.yaml` 也回 409,要先移除另一份宣告;`GET` 以 `declared_elsewhere: true` 標出(#3172)。`<id>.yaml` 存在但沒宣告這個 id、只有別的檔宣告時,旗標也是 `true`:那不是重複宣告,exporter 照常從別的檔載入,但寫入守衛一樣不讓 id 寫進 `<id>.yaml`。batch(直寫與 PR 模式皆然)遇到以上任一情況都不回 409,而是整批回 422(不寫入),該筆結果帶 `status: error` 與 `code: TENANT_DECLARED_ELSEWHERE`。回應不含宣告它的那個檔名。這個判定每次寫入都掃描整棵 conf.d,掃描有時限;掃描失敗或逾時時不寫檔:`PUT` 等端點回 500 `INTERNAL_ERROR`(固定訊息);batch 在判某一筆時掃描失敗(PR 模式,或直寫模式判 op 時)整批回 422、該筆 `code: INTERNAL_ERROR`;直寫模式把 conf.d 複製進暫存 worktree 時就失敗(例如建不出複製的目錄)則不寫入,同步的 batch 回 500 `INTERNAL_ERROR`(通用的內部錯誤訊息),非同步的 batch 則任務以 `failed` 結束。複製不判斷項目能不能讀:FIFO 等非一般檔、讀取失敗的檔在暫存 worktree 裡是指向原項目的 symlink,列不出的子目錄不複製,這些由寫入後整棵樹的判定拒收(500 `TENANT_STORE_FAULT`,#2939)。若是 conf.d 裡有讀取會卡住的檔(例如卡住的掛載),刪掉那個檔不會解除,要重啟 tenant-api;名為 `*.yaml` 的 FIFO 自 #2939 起不開不讀,不會卡住。子目錄檔在 ConfigMap 部署本就不生效——[扁平組裝](../../docs/integration/gitops-deployment.md#3-configmap-assembly)會丟掉子目錄檔並 WARN。`/effective` 遞迴掃描整棵樹,找得到這些租戶;同一 id 被兩個以上的檔宣告時回 409 `CONFLICT`。訊息依 walker 回報的**前兩個**宣告檔決定:兩個都是頂層的 `<id>.yaml`／`<id>.yml` 時帶這兩個基底檔名,否則(子目錄檔——含子目錄內兩種拼法並存——或共用檔的 `tenants:`)是固定文字、不含檔名;完整路徑只進 server log(#2511)。有第三份以上的宣告時,訊息可能只列兩個拼法、不提其餘的檔,也可能因走訪順序而是固定文字。`/effective` 的其他內部錯誤(例如 conf.d 根目錄不存在)回 500 固定訊息、不帶路徑;只有某個檔解碼失敗時保留解碼器原文(`parse defaults[i]: …`／`parse tenant: …`,不含路徑)。list 只列 `<id>.yaml` 租戶;子目錄檔解析失敗只出現在 `parse_failed_files`。
### Custom Alerts(租戶自助告警)

租戶從平台提供的**參數化 recipe** 產生告警(免寫 PromQL)。**人類請用 Tenant Manager portal 的 RecipeBuilder**(選 recipe、填參數、一鍵 commit);以下端點供**自動化 / 整合**直接呼叫。

| Method | Path | 權限 | 說明 |
|--------|------|------|------|
| `GET` | `/api/v1/tenants/{id}/metrics` | read | Metric 探索:回傳該租戶近期出現的 metric 名稱,供 RecipeBuilder 選取(伺服端強制鎖該租戶 label) |
| `PUT` | `/api/v1/tenants/{id}/custom-alerts` | write | 寫入該租戶的 custom-alert recipe 集合(驗證後 commit / PR 回 GitOps);conf.d 有讀不了的項目時回 409 `TENANT_TREE_UNREADABLE` 或 500 `TENANT_STORE_FAULT`(同 `PUT /tenants/{id}`);租戶檔本身讀不了時回 409 `TENANT_CONFIG_NOT_LOADABLE` |

### 群組 / View

| Method | Path | 權限 | 說明 |
|--------|------|------|------|
| `GET` | `/api/v1/groups` | read | 列出群組(自動隱藏成員全不可讀的群組) |
| `GET` | `/api/v1/groups/{id}` | read | 取得群組 |
| `PUT` | `/api/v1/groups/{id}` | write + 逐成員 write | 寫入;對所有 `members` 都需 write,否則回 403 + 不足清單。`members` 至少一個(清空用 DELETE);id 已被看不到的群組佔用時回 409 `GROUP_ID_UNAVAILABLE`(換 id,勿重試) |
| `DELETE` | `/api/v1/groups/{id}` | write + 逐成員 write | 刪除(同上權限)。沒有成員的舊群組只有不受限的 platform admin 看得到、刪得掉 |
| `POST` | `/api/v1/groups/{id}/batch` | read + 逐成員 write | 對群組全成員部分合併 patch(只改指定 key、保留其餘;同步 / 非同步);`unset` 與 `POST /tenants/batch` 同義。每個成員展開成一筆 op、走與 `POST /tenants/batch` 同一條管線:patch 值檢查(違規 400)、逐成員 RBAC + domain policy;PR 模式整組合成**一支** PR(`status: pending_review`),不直接 commit 到 base branch(#2339)。與 `POST /tenants/batch` 相同**全有全無、一次 commit**:任一成員被拒就整批 422 `rejected`、不寫入;`results` 與 `summary` 只列呼叫者讀得到的成員,讀不到的成員被拒時同樣整批拒收。不存在的成員是 `skipped`,不使整批失敗 |
| `GET` | `/api/v1/views` | read(*,非 org-scoped) | 列出 saved view |
| `GET` `PUT` `DELETE` | `/api/v1/views/{id}` | read / write(*,非 org-scoped) | Saved view CRUD。view 沒有擁有者、不按租戶過濾,所以仍要求 `"*"` 權限([#2520](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2520));也不屬於任何組織,所以那條規則不能帶 `org-scope`,帶了一律 403;open mode 照舊可讀([#3160](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3160)) |

### Async / 事件

| Method | Path | 權限 | 說明 |
|--------|------|------|------|
| `GET` | `/api/v1/tasks/{id}` | read(限提交者) | Async task polling;task id 為 128-bit 隨機值,非提交者回與不存在相同的 404;結果以呼叫者當下的 RBAC 過濾 |
| `GET` | `/api/v1/prs` | read | Pending PR / MR 列表;不可讀的租戶自動隱藏,`?tenant=<id>` 不可讀回空陣列;批次 PR/MR 須讀得到它涵蓋的每個租戶 |
| `GET` | `/api/v1/events` | read | SSE 即時事件流(`config_change`);只送呼叫者讀得到的租戶的事件,不屬於單一租戶的 state 事件只送讀得到全部租戶的呼叫者 |

### 聯邦(Federation)

讓租戶安全拉取**自己的** metrics 子集:平台維護白名單與每租戶子集,並簽發短效 token 供租戶向 read-path proxy 取數。

| Method | Path | 權限 | 說明 |
|--------|------|------|------|
| `GET` | `/api/v1/federation/policy` | read | 取得平台聯邦白名單(只有指標名、不點名租戶;租戶管理員挑自己的子集時要用) |
| `PUT` | `/api/v1/federation/policy` | admin(平台層級) | 更新白名單(新增 metric 會跑資料層 admission 檢查;軟性警告需 `force=true` + 理由才放行,理由記入該次寫入的 `state_write` 稽核事件 `notes`) |
| `POST` | `/api/v1/federation/tokens` | admin(對 body 的租戶) | 簽發短效 token(預設 4h);token 本體只在回應出現一次 |
| `GET` | `/api/v1/federation/tokens?tenant_id=<id>` | admin(對該租戶) | 列出該租戶未過期的 token 記錄(不含 token 本體) |
| `DELETE` | `/api/v1/federation/tokens/{id}` | admin(對該 token 的租戶) | 撤銷 token;最終一致,約 1–2 分鐘內隨設定同步生效。無權撤銷回與 token 不存在相同的 404 |
| `POST` | `/api/v1/federation/accounts/backfill` | admin(平台層級) | 替 conf.d 裡還沒有 `account_id` 的租戶一次配發(單一寫入);冪等,重跑不會配新號。回應列出這次配到號的租戶與已有號的租戶數。用途見 [tenant-log-query §2.1](../../docs/integration/tenant-log-query.md) |
| `GET` | `/api/v1/tenants/{id}/federation` | read | 取得該租戶的聯邦 metric 子集(讀 state store 的 `_federation.<id>.yaml`);無紀錄回空子集 |
| `PUT` | `/api/v1/tenants/{id}/federation` | admin | 更新該租戶的聯邦 metric 子集(需該租戶 admin;子集不得超出平台白名單);寫入 state store 的 `_federation.<id>.yaml` |

> 群組、saved view、聯邦白名單與子集、AccountID 帳本是 tenant-api 自己的紀錄,存在 state store(`--state-store`,見下方環境變數),**不在 conf.d、不產生 git commit 或 PR**([#2856](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2856))。每次寫入記一筆結構化 `state_write` 事件(key、entity、author、notes)。寫入錯誤:持續撞上並行寫入回 409、store 連不上回 503(帶 `Retry-After`)、ConfigMap 將超過約 900 KiB 回 507。

> token 記錄存於跨 replica 共用的 Kubernetes ConfigMap(由 Helm chart 預建),服務維持 stateless、可多 replica。濫用防線:每租戶同時最多 16 個有效 token + 每分鐘簽發上限,超出分別回 409 / 429。未設定簽章金鑰時,`/federation/tokens` 與 `/federation/accounts/backfill` 都不註冊。

## 維運

### 限制與上限

| 項目 | 預設 | 可調 |
|------|------|------|
| 逐呼叫者限流 | 100 req/min | `TA_RATE_LIMIT_PER_MIN`(`0` 關閉) |
| Request body | 1 MB | `TA_MAX_BODY_BYTES` |
| Batch request body (tenants + groups) | 256 KiB | `TA_MAX_BATCH_BODY_BYTES` |
| 單一租戶文件 | 64 KiB | `TA_MAX_TENANT_DOC_BYTES` |
| 批次操作數 | 1–1000 / 次 | — |
| Search page_size | 1–500(預設 50) | — |
| Patch key / value 長度 | ≤ 256 / ≤ 1024 字元 | — |

### 限流回應格式

```json
{ "error": "rate limit exceeded for user@example.com; try again in 12s",
  "code": "RATE_LIMITED", "retry_after_s": 12 }
```

同步輸出 `Retry-After` header。`/health` / `/ready` / `/metrics` 永遠不限流。

### 衝突語義

寫入時記錄 git HEAD;若 commit 的 parent 與寫入前 HEAD 不符(期間有外部 commit 落地)→ 回 409,呼叫者應 refresh 後重試。

state store 的寫入(群組、saved view、聯邦白名單與子集、AccountID 帳本)以 ConfigMap 的 `resourceVersion` 做 compare-and-swap:輸掉競爭會以最新內容重算後重試,重試用盡才回 409。

### Open-read 模式

未配置 `_rbac.yaml` 時所有讀寫端點放行(僅守 path traversal)。**僅供單人 dev,切勿上 production。**

## 可觀測性

### Metrics(`/metrics`)

Prometheus 文字格式(`text/plain; version=0.0.4`)。「何時出現」寫「恆有」的,程序一起來就輸出(計數從 0 起);其餘只在條件成立時才有,沒有時整族不輸出。這張表由 `internal/handler/metrics_readme_parity_test.go` 對原始碼比對名稱、型別、label,對 `testdata/metrics.golden` 比對「值:」列出的 label 值。

| Metric | Type | 何時出現 | 用途 |
|--------|------|----------|------|
| `tenant_api_up` | Gauge | 恆有 | 值恆 1 |
| `tenant_api_uptime_seconds` | Gauge | 恆有 | 程序啟動至今秒數 |
| `tenant_api_requests_total` | Counter | 恆有 | API 請求總數 |
| `tenant_api_errors_total` | Counter | 恆有 | 回應狀態碼 ≥ 400 的請求數 |
| `tenant_api_writes_total{result}` | Counter | 恆有 | conf.d 的寫入嘗試數,依結果分。一次嘗試＝一次對 git／forge 的寫入動作:直接模式一次 commit,PR 模式一個分支加開 PR/MR;一個 batch(含 group batch、`?async=true` 的在 task 結束時記)不論幾個租戶都只算一次。`ok` = 2xx(含沒有變更);`conflict` = 409(repo 或租戶檔不在寫入需要的狀態,例如被同時改動、base hash 過期、租戶由別的檔宣告);`error` = 伺服器端失敗(5xx、429——直接寫入時 git／檔案失敗自 #1535 起回 500,也在此列——以及 forge token 權限不足回的 403、batch 已 commit 但沒收尾乾淨的 409 `committed: true`——這一種其實已寫入)。請求本身不合法、權限不足、被 route generator 判定有 findings 的 4xx 不算;client 斷線(請求自身的 context 已取消)後失敗的嘗試也不算(仍成功的照記 `ok`;逾時 `DeadlineExceeded` 是伺服器端的,照記 `error`)。async batch 的 task panic 不算(由 `TenantApiAsyncTaskPanicked` 涵蓋)。groups／views／federation 這些 state store 紀錄不是 conf.d,不算。值:`conflict` / `error` / `ok`(告警 `TenantApiWriteErrorsHigh`) |
| `tenant_api_rate_limit_rejections_total` | Counter | 恆有 | 被逐呼叫者限流擋下的請求數;限流關閉時恆 0 |
| `tenant_api_rate_limit_active_callers` | Gauge | 恆有 | 滾動視窗內仍有請求的呼叫者數(背景 sweeper 控管記憶體) |
| `tenant_api_federation_orphaned_tokens` | Gauge | 恆有 | 租戶已不在 conf.d、卻仍有效的聯邦 token 記錄數;非 0 照 [租戶下架 runbook](../../docs/internal/tenant-offboarding-runbook.md) 處理 |
| `tenant_api_federation_orphaned_subset_files` | Gauge | 恆有 | 已不在 conf.d、仍在 state store 留有 federation subset 紀錄的租戶數(名稱寫 files,實際按租戶計) |
| `tenant_api_identity_audit_total{result}` | Counter | 恆有 | 機器身分(KSA／TokenReview)稽核結果,只記錄、不參與授權;未開 `--machine-identity-audit` 時全為 0。值:`mismatch` / `no_token` / `unknown_issuer` / `unknown_workload` / `verified` / `verify_failed` |
| `tenant_api_scope_would_deny_total{axis}` | Counter | 恆有 | shadow 模式放行、但切 enforce 後會被拒的次數;`increase()` 在觀察期內維持 0 才切對應的 `--rbac-*-scope-enforce`。值:`metadata` / `metadata_write` / `org` / `org_write` |
| `tenant_api_config_reload_failures_total{component}` | Counter | 恆有 | 該設定重載解析失敗次數(失敗時沿用上一份正確設定)。值:`RBAC` / `federation-policy` / `groups` / `tenantorg` / `views` |
| `tenant_api_config_last_reload_successful{component}` | Gauge | 恆有 | 該設定最後一次重載是否成功;0 = 目前正用舊設定。值:`RBAC` / `federation-policy` / `groups` / `tenantorg` / `views` |
| `tenant_api_policy_available` | Gauge | 恆有 | 1 = 產生器容器能判定寫入:scrape 時讀它的 `/v1/status`,`ready` 為真(部署端 policy 檔可用、有 amtool、產生器不比 policy 要求的版本舊);0 = 不能,或狀態讀不到(容器不可達),此時所有會改變檔案內容的寫入都回 503(告警 `TenantApiPolicyUnavailable`)。不再反映 repo 裡的 `_domain_policy.yaml`,也不再「沿用上一份可用內容」(#2903) |
| `tenant_api_async_task_panics_total` | Counter | 恆有 | 非同步批次(`?async=true`)的工作 panic 的次數;該 task 標為 `failed`(`error` 不帶 panic 內容),panic 與堆疊依 task id 寫進 log,worker 繼續處理後續 task。應維持 0(告警 `TenantApiAsyncTaskPanicked`) |
| `tenant_api_store_unreadable_entries{reason}` | Gauge | conf.d 掃描成功過一次之後 | tenant-api 自己那份 conf.d 裡伺服器讀不了、git 也記錄不了的項目數,依 walker 的 reason 分:`special_file`(設定檔名的 FIFO／socket／裝置檔)、`read_error`、`walk_error`、`stat_error`(例如權限不足;懸空、迴圈、路徑中段不是目錄這類無法解析的 symlink 不算)。scrape 時由 tenant-api 自己掃 conf.d(只分類、不解析內容,兩次之間有最短間隔,不會卡在 FIFO 上)。**還沒有任何一次掃描成功前不輸出**(未知不等於 0);之後掃描失敗時沿用上一次成功的值。非 0 時每一筆寫入都回 500 `TENANT_STORE_FAULT`;應維持 0(告警 `TenantApiStoreUnreadable`) |
| `tenant_api_store_scan_last_success_timestamp_seconds` | Gauge | conf.d 掃描成功過一次之後 | 上一列那次成功掃描的 unix 秒數;掃描失敗時不前進。tenant-api 在跑、這個值卻不存在或久未前進(超過規則設定的閾值)時,上一列的值不可信(告警 `TenantApiStoreScanStale`) |
| `tenant_api_dev_auth_bypass_active` | Gauge | 恆有 | 1 = `--dev-bypass-auth` 開著(僅限本機開發,正式環境須為 0) |
| `tenant_api_sse_clients` | Gauge | 恆有 | 目前連線中的 SSE(`/api/v1/events`)客戶端數 |
| `tenant_api_human_socket_up` | Gauge | 設了 `--human-socket` | 1 = human-plane Unix socket 回應了 readiness 自我探測 |
| `tenant_api_forge_circuit_state{provider}` | Gauge | PR／MR 寫回模式 | forge 斷路器狀態:0 = closed、1 = half-open、2 = open(forge 故障,寫入回 503) |
| `tenant_api_forge_pr_conflicts{provider}` | Gauge | PR／MR 寫回模式,tracker 同步過一次後 | 上次同步時處於 merge conflict 的 PR／MR 數 |
| `tenant_api_generator_rejections_total{reason}` | Counter | 恆有 | 產生器容器沒能判定而拒收的寫入數(`GENERATOR_REJECTED`,#2903);全部未寫入。值:`deadline` / `memory` / `incomplete` / `git_read` / `busy` / `too_large` / `invalid_request` / `mismatch` / `no_time` / `normalized` |
| `tenant_api_generator_scrape_success` | Gauge | 恆有 | 1 = 這次 scrape 讀到產生器容器自己的 `/metrics` 並附在輸出最後(它只有 Unix socket,由 tenant-api 轉出:`da_generator_*`,含版本 `da_generator_build_info`、判定峰值記憶體 `da_generator_judgement_peak_rss_bytes`,告警 `GeneratorJudgementMemoryNearLimit` 讀它);0 = 這次讀取失敗(逾時 2 秒、連不上或讀取錯誤皆是),不附 |

### Request 對應

每筆 request 回 `X-Request-ID`(自動產生或沿用客戶傳入)。後端用結構化 JSON log 輸出,每行帶 `request_id`,5xx 升為 WARN;回報問題時附上此 id 即可 grep 後端 log。`TA_LOG_LEVEL` 控制 verbosity。

### SSE 事件

```text
event: config_change
data: {"type":"config_change","tenant_id":"db-a-prod","timestamp":"2026-05-03T10:00:00Z","detail":"tenant config updated"}
```

## 設定

### 環境變數

預設欄寫的是**沒設這個變數時的實際值**;`(空)` = 空字串,意義寫在說明欄。表格由 `cmd/server/env_readme_parity_test.go` 對原始碼比對:變數名單兩向一致,預設值能從原始碼推得的逐一比對(推不出的幾個在測試裡列明原因)。

**基本**

| 變數 | 預設 | 說明 |
|------|------|------|
| `TA_CONFIG_DIR` | `/conf.d` | 租戶 YAML 目錄 |
| `TA_GIT_DIR` | (空) | Git repository 根目錄;空 = 同 `TA_CONFIG_DIR` |
| `TA_STATE_STORE` | (空) | **必填**。tenant-api 自己的紀錄（群組、saved views、federation 白名單與子集、AccountID 帳本）存放處：`configmap`（Kubernetes，正式環境）或 `dir`（單機，**僅供開發與 try-local**，見 [dir 模式的外部寫入者](#dir-模式的外部寫入者必須拿-flock)）。不再放在 conf.d（[#2856](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2856)） |
| `TA_STATE_CONFIGMAP` | `tenant-api-state` | `configmap` 模式的 ConfigMap 名稱（Helm chart 預建） |
| `TA_STATE_NAMESPACE` | (空) | 上述 ConfigMap 所在 namespace;空 = pod 自身 namespace |
| `TA_STATE_DIR` | (空) | `dir` 模式的目錄，必須在 `TA_CONFIG_DIR` 之外。⛔ 外部程序改其中檔案前必須先對 `$TA_STATE_DIR/.lock` 取得 exclusive flock，否則與 tenant-api 的寫入重疊時會被靜默覆蓋（窗口短且不可預測；[#1535](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1535)） |
| `TA_RBAC_PATH` | (空) | `_rbac.yaml` 路徑;**不設**(且不帶 `--rbac`)= open-read。⛔ 設成空字串或只有空白、或 `--rbac=` 一律**拒絕啟動**,不會當成沒設而進 open-read([#2491](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2491)) |
| `TA_ADDR` | `:8080` | HTTP listen address |
| `TA_GENERATOR_SOCKET` | (空) | **必填**。產生器容器(`da-tools generator-service`)的 Unix socket;每筆會改變 conf.d 內容的寫入 commit 前都問它(ADR-036,#2903)。不設或只有空白就**拒絕啟動**。產生器必須以同一個 uid 執行、讀同一個 git repo |
| `TA_DEPLOY_POLICY` | (空) | 部署端 policy 檔(產生器 `--policy` 的同一份,唯讀掛載)的路徑;tenant-api 算它的 sha256 核對產生器判定時用的是同一份。空 = 產生器也不帶 `--policy`(兩邊須一致,否則寫入回 429 `mismatch`) |
| `TA_HUMAN_SOCKET` | (空) | 給 human plane(同 pod 的 oauth2-proxy)用的 Unix socket 路徑;設了就在這個 socket 上**另外**提供同一套路由。空 = 只有 TCP |
| `TA_LOG_LEVEL` | `info` | `debug` / `info` / `warn` / `error`;其他值當成 `info` |

**請求上限與逾時**

| 變數 | 預設 | 說明 |
|------|------|------|
| `TA_RATE_LIMIT_PER_MIN` | `100` | 逐呼叫者限流;`0` 關閉;非整數值回退預設並印 WARN |
| `TA_MAX_BODY_BYTES` | `1048576` | request body 上限(bytes) |
| `TA_MAX_BATCH_BODY_BYTES` | `262144` | **兩個**批次端點(`POST /tenants/batch`、`POST /groups/{id}/batch`)的 request body 上限(bytes)。比 `TA_MAX_BODY_BYTES` 緊,因為批次在持有 single-writer token 期間會把整個 body 反覆解析,超量會延遲其他租戶的寫入;group batch 更把同一個 patch 套用到每個成員。超量回 **413** (`code: PAYLOAD_TOO_LARGE`),訊息寫「at least N bytes」——只讀到 `limit+1`,精確長度結構上不可知 |
| `TA_MAX_TENANT_DOC_BYTES` | `65536` | 單一租戶文件的 parse 前上限(bytes)。量的是寫入路徑實際會解析的那份文件 —— 在合併路徑上那是**合併後的整份檔案**,不是請求裡的 patch |
| `TA_READ_TIMEOUT` / `TA_WRITE_TIMEOUT` / `TA_IDLE_TIMEOUT` | `15s` / `30s` / `60s` | HTTP server timeout(大批次 + 慢 git push 時可調高 write timeout) |
| `TA_SSE_HEARTBEAT` | `25s` | SSE 每個客戶端的 heartbeat 間隔;須小於下游 proxy 的 idle timeout。`0` 關閉(會讓卡住的閒置連線再度累積) |
| `TA_SSE_WRITE_TIMEOUT` | `10s` | SSE 單次寫入期限,卡住的客戶端在期限後釋放;`0` 關閉 |
| `TA_SSE_MAX_LIFETIME` | `0s` | SSE 連線最長存活時間;`0` = 不限 |

**寫回(Git／PR／MR)**

| 變數 | 預設 | 說明 |
|------|------|------|
| `TA_WRITE_MODE` | `direct` | `direct` / `pr` / `pr-github` / `pr-gitlab`。⛔ 前後空白先 trim,trim 後須逐字等於這四個之一;大小寫敏感,不符者(含 `DIRECT`、打錯字、`--write-mode=` 空值、`TA_WRITE_MODE` 設成空字串)一律**拒絕啟動**,不會退回 `direct`;不設才是 `direct`([ADR-034](../../docs/adr/034-legal-value-as-fallback.md)) |
| `TA_WRITE_QUEUE_DEPTH` | `5` | 排在進行中那筆寫入後面、可等待的寫入數;再多的直接拒絕。`0` = 不排隊 |
| `TA_GIT_BASE_BRANCH` | `main` | PR／MR 模式下,本地 git 開分支的基準 branch(conf.d repo 預設 branch 不是 `main` 時要設) |
| `TENANT_API_GIT_TIMEOUT` | `60s` | 單一 git 指令的期限(⚠️ 前綴是 `TENANT_API_`,不是 `TA_`) |
| `TA_GIT_FETCH_TIMEOUT` | `5s` | PR 寫入前在寫入鎖內 fetch 基準 branch 的期限;forge 變慢時快速回 503、放掉鎖 |
| `TA_GITHUB_TOKEN` / `TA_GITHUB_REPO` / `TA_GITHUB_API_URL` | (空) | GitHub PR 模式:token 與 repo(`owner/repo`)必填;API URL 供 Enterprise |
| `TA_GITHUB_BASE_BRANCH` | `main` | GitHub PR 的目標 branch |
| `TA_GITLAB_TOKEN` / `TA_GITLAB_PROJECT` / `TA_GITLAB_API_URL` | (空) | GitLab MR 模式:token 與 project(`group/project` 或數字 ID)必填;API URL 供自託管 |
| `TA_GITLAB_TARGET_BRANCH` | `main` | GitLab MR 的目標 branch |
| `GIT_COMMITTER_NAME` / `GIT_COMMITTER_EMAIL` | (空) | service account 身分;空時 fallback 到 author |

**聯邦**

| 變數 | 預設 | 說明 |
|------|------|------|
| `TA_FEDERATION_KEY` | (空) | 簽發聯邦 token 的私鑰 PEM 路徑;空 = 聯邦 token 端點不註冊 |
| `TA_FEDERATION_STORE` | `tenant-federation-store` | 存放聯邦 token 記錄的 ConfigMap 名稱(Helm chart 預建) |
| `TA_FEDERATION_NAMESPACE` | (空) | 上述 ConfigMap 所在 namespace;空 = pod 自身 namespace |
| `TA_FEDERATION_TOKEN_TTL` | `4h` | 聯邦 token 效期 |
| `TA_FEDERATION_PROMETHEUS_URL` | (空) | 聯邦准入檢查查詢的 Prometheus／VictoriaMetrics base URL;空 = 不做准入檢查 |
| `TA_FEDERATION_HEARTBEAT_INTERVAL` | `5m` | 撤銷證據通道的存活 heartbeat 間隔;須遠小於 reconciler 的 30m 視窗 |

**身分與 RBAC**

| 變數 | 預設 | 說明 |
|------|------|------|
| `TA_IDENTITY_CLAIM_HEADERS` | (空) | `claim=Header-Name` 逗號分隔,宣告從哪些受信任 header 讀取具名 claim。空 = 不讀 |
| `TA_RBAC_EMPTY_OPEN` | `false` | `--rbac` 指到的檔解析出 0 個 group 時,改為 open-read(預設 fail closed) |
| `TA_RBAC_METADATA_SCOPE_ENFORCE` / `TA_RBAC_METADATA_WRITE_SCOPE_ENFORCE` / `TA_RBAC_ORG_SCOPE_ENFORCE` | `false` | 各 scope 軸由 shadow 切成 enforce;切之前看 `tenant_api_scope_would_deny_total` 對應的 `axis` |
| `TA_MACHINE_IDENTITY_AUDIT` | `false` | 以 TokenReview 稽核呼叫端的 ServiceAccount token;只記錄,不影響授權。需要 in-cluster config |
| `TA_MACHINE_IDENTITY_AUDIENCE` | `tenant-api` | 上述稽核要求的 audience |
| `TA_MACHINE_IDENTITY_ISSUER` | (空) | 稽核的 issuer 允許清單(逗號分隔);空 = 全部交給 TokenReview |

**本機開發(⛔ 不得用於正式環境)**

| 變數 | 預設 | 說明 |
|------|------|------|
| `TA_DEV_BYPASS_AUTH` | `false` | 沒有 oauth2-proxy header 時注入開發身分;在 Kubernetes 內啟動會 panic([ADR-022](../../docs/adr/022-dev-auth-bypass-four-layer-containment.md)) |
| `TA_DEV_BYPASS_EMAIL` | `dev@local` | 注入的身分 email |
| `TA_DEV_BYPASS_GROUPS` | `demo-admins` | 注入的 IdP group(逗號分隔;須在 `_rbac.yaml` 對得到租戶) |

布林開關(上表預設為 `false` 的那幾支)的值是同名 flag 的**預設值**,一律以 `strconv.ParseBool` 解讀——與命令列走的是同一支解析器。接受 `1` / `t` / `T` / `TRUE` / `true` / `True` / `0` / `f` / `F` / `FALSE` / `false` / `False`;未設或只有空白 = 維持該 flag 自己的預設;**其餘任何值一律啟動失敗**,不會靜默退成 false——這幾支開關決定某道檢查跑不跑,打錯字若退成 false 就與「刻意關掉」無法區分([#1599](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1599))。⚠️ **`yes` / `on` 在 #1599 之前被當成 true,現已改為啟動失敗**;`t` / `T` 則相反,過去被靜默當成 false,現在是 true。

### RBAC YAML

```yaml
groups:
  - name: platform-admins
    tenants: ["*"]
    permissions: [read, write, admin]

  - name: db-operators
    tenants: ["db-a-*", "db-b-*"]
    permissions: [read, write]

  - name: viewers
    tenants: ["*"]
    permissions: [read]
```

上例的 `db-operators` 只拿得到 `db-a-*`、`db-b-*` 兩組租戶:`/tenants`、`/groups`、`/prs`、`/events` 等列表端點的路由閘門只要求**至少對一個租戶**有讀取權([#2520](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2520);先前要求 `tenants: ["*"]`,這類規則一律 403),列表內容由 handler 逐租戶過濾。閘門只看呼叫者自己的規則:規則列了不存在的租戶也過得了、拿到空列表,所以 200 / 403 不透露租戶存在與否;帶 `org-scope` 的規則在呼叫者沒有該 claim、且 org 軸是 enforce 時不算數。零條規則的設定檔照舊 fail-closed(MED-8)。仍要求 `"*"` 的:views、`PUT /federation/policy`、`POST /federation/accounts/backfill`、`/audit/...`,而且這幾支的 handler 都要求那條 `"*"` 規則不帶 `org-scope`([#3160](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3160))。`/me` 只要求身分、不看授權:它只回呼叫者自己命中的規則,沒有任何授權(含零條規則的 MED-8)也回 200 與空的授權摘要。

支援以環境 / 域 metadata 做進一步過濾(細節見 `internal/rbac/` 註解)。**沒標記該 metadata 的租戶**在受限規則下預設**仍放行**(SHADOW,行為與過去一致),但每次會計入 `tenant_api_scope_would_deny_total{axis="metadata"}`(**單調遞增 counter**、僅重啟歸零)。待 `increase(tenant_api_scope_would_deny_total{axis="metadata"}[soak窗])` 在整個 soak 窗維持 **0**(看增長率非絕對值),並**掃過確認沒有落單租戶**(counter 是 traffic-driven:沒被 list 到的租戶不會觸發)後,設 `--rbac-metadata-scope-enforce`(或 `TA_RBAC_METADATA_SCOPE_ENFORCE=1`;helm `--set rbac.metadataScopeEnforce=true`)切成**沒標記就拒絕**(fail-closed,ADR-027 / LD-6 P1)。

⚠️ 上述 `environments[]` / `domains[]` 在 #1597 之前**只約束 list 可見性**——一條 `environments: [dev]` 的規則,可以 PUT 一個它在清單裡根本看不到的 prod 租戶。#1597 把**同一條軸接上寫入平面**,並且是**兩處**判定:寫入前的租戶現況(pre-state),以及寫入後租戶會有的 metadata(post-state)——後者擋的是「一次寫入就把租戶改標到自己 scope 外」這條繞路。post-state 對 PUT 讀的是 body 提議的 `_metadata`;對 `POST /tenants/batch` 與 `POST /groups/{id}/batch`,凡 patch 寫到 `_profile` 的 op(#2830 之後它會決定 environment / domain;batch 不能寫 `_metadata`,見 #2859),讀的是租戶檔現況先疊同一租戶在本批中排在它前面的 op、再疊該 op 之後的結果,不通過時該筆回 `error`、整批不寫入(422)。PR 模式下,pod 本機樹只在啟動時同步、可能落後,所以本機樹上的判定只能提早拒絕;**權威判定在寫入鎖內、以 PR 分支所依據的最新 origin base 再做一次**,pre-state 與 post-state 都判,PUT 與 batch 皆然(#2857)。不通過時 PUT 回 403 `FORBIDDEN`(帶 `tenant_id`)、batch 回 422 該筆結果 `code: FORBIDDEN`(整批不寫),都不開 PR、不留分支。這次判定一律執行;shadow 下的 `{axis="metadata_write"}` 只在最新 base 算出的 environment/domain 與本機判過的不同時才多記一筆(同一筆寫入不重複計數,org 軸不重記),所以 soak 窗量得到「只在最新 base 上才越界」的寫入。base 上的租戶檔讀不了時不當成越權,由後續的合併與驗證回它本來的錯誤。org 軸同樣以最新 base 的 `_tenant_orgs.yaml` 判定(讀不了時 PUT 回 500 `CONFIG_DECODE_ERROR`,batch 整批回 422、該筆結果 `code: CONFIG_DECODE_ERROR`);batch 的逐筆訊息不帶 op 索引;`POST /groups/{id}/batch` 的結果只列呼叫者讀得到的成員,讀不到的成員被拒時不具名(仍整批拒收)。

這條軸有**自己的開關** `--rbac-metadata-write-scope-enforce`(或 `TA_RBAC_METADATA_WRITE_SCOPE_ENFORCE=1`;helm `--set rbac.metadataWriteScopeEnforce=true`)。⛔ **刻意不共用** `--rbac-metadata-scope-enforce`:那支管的是 list 平面**早已綁定**的軸(只剩「未標記」那一支還在遷移),共用的話,一個已經跑完 list 平面 soak、旗標開著的部署一升級就當場被收緊、沒有任何過渡窗——這正是 ADR-027 D4 給 org 軸獨立旗標的同一個理由。預設 SHADOW:寫入照舊放行,只計入 `tenant_api_scope_would_deny_total{axis="metadata_write"}`,`increase(...[soak窗])` 撐過整個窗維持 0 才 flip。⚠️ 它與 `{axis="metadata"}` **量的不是同一件事**:後者量「未標記租戶」的寬容度(是一份補標記的待辦清單),前者量「這次 flip 會開始拒絕哪些**真實寫入**」(是一份 blast-radius 清單)。讀取平面不受這支旗標影響(read-by-id 的 metadata 軸是另一次遷移),故 `PermRead` 的 403 訊息刻意不提這條建議——指向一個轉不動的旋鈕正是要避免的事。

org 軸(租戶→組織,`_tenant_orgs.yaml`;ADR-027 / LD-6 P4)獨立於 metadata 軸、有**自己的 enforce 開關**:`--rbac-org-scope-enforce`(或 `TA_RBAC_ORG_SCOPE_ENFORCE=1`;helm `--set rbac.orgScopeEnforce=true`)。同一支開關**原子地**支配 org-scoped 規則的**三個**判定面——list 可見性(P4a)、per-tenant 寫入 / admin 授權(含 federation token 簽發,P4b)、與 read-by-id 單租戶讀取(P4c),不存在只翻其一的中間態。預設 SHADOW:沒掛 org 的租戶仍放行,但分別計入 `tenant_api_scope_would_deny_total{axis="org"}`(read/visibility 面:list + read-by-id/collection)與 `{axis="org_write"}`(寫入面);翻 enforce 的判準是**兩軸** `increase(...[soak窗])` 皆維持 0(單調 counter、看增長率),並掃過確認沒有漏標租戶。✅ read-by-id(GET `/{id}`、`/access` 等單租戶讀取)已接 org 軸(P4c),閉掉了 enforce 空窗期的 read-only IDOR/enumeration-oracle,即 #1040 enforce 翻閘的硬前置。⚠️ 一個 labeled 租戶在 shadow 就已於三面 org-enforce(labeled-mismatch 兩模式皆拒);真正的行為變更觸發點是 **labeling**,故 labeling 前須確認 org claim 經每個 PEP(尤其 recipe-preview 的 `PREVIEW_CLAIM_HEADERS`)送達 tenant-api。

### RBAC match 規則(claims-aware)

`_rbac.yaml` 規則可加選配的 `match:` 區塊(ADR-027 / LD-6 P3)。**沒有 `match:` 的規則行為完全不變**——`name` 即比對的 IdP group(同一條求值路徑的退化情形,非新分支);有 `match:` 時 `name` 變成純標籤 / 稽核識別:

```yaml
groups:
  - name: platform-admins        # 無 match:name 即比對的 IdP group(既有行為)
    tenants: ["*"]
    permissions: [read, write, admin]

  - name: org-4821-operators     # 有 match:name 只是標籤
    match:
      groups: [operators]        # OR-within:命中其一即可
      claims:
        org: [ORG-4821]          # claim key → 允許值清單(OR-within)
    tenants: ["*"]
    permissions: [read, write]
```

求值語意:**條件種類之間 AND**(`groups` 條件與每一個 claim key 條件都要成立)、**同一條件清單內 OR**(命中其一即可)。claim 比對是**精確字串相等**(tenants 的 `*` / prefix pattern 語意不外溢到 claims)。多規則命中時 permissions / scope 取**聯集**(與既有行為一致)。`match.groups` 與 `match.claims` 擇一即可(純 claim 規則 / 純 group 規則皆合法)。

Fail-closed 護欄:

- principal **缺少**規則引用的 claim、或值不在允許清單 → 該規則不命中(缺 claim fail-closed)。
- **空的 `match: {}`**(或條件全空)→ 載入錯誤(空 match ≠ match-all)。
- **null 的 `match:`**(裸 `match:`、`match: null`、或子條件全被註解掉)→ 載入錯誤:此形式在結構上與「沒有 `match:`」無法區分,若放行會靜默退化成 legacy group-name 規則、丟失 claim 收斂,故一律 fail-loud(要 legacy 行為就整段移除 `match:` key)。
- `match.claims` 引用**未在 `--identity-claim-headers` 宣告**的 key → 載入錯誤(未宣告的 claim key 在執行期永遠不可能命中——沉默的死規則必須 fail-loud)。

⚠️ **嚴格解析(breaking-for-invalid-configs)**:`_rbac.yaml` 改以 strict mode 解析——**未知欄位(如 `mach:` 打錯字)是載入錯誤**,不再被靜默忽略(靜默忽略會讓 match 規則退化成比作者意圖更寬的 group-name 規則 = 提權面)。合法的既有 config 完全不受影響;無效 config **啟動即 FATAL**,hot-reload 則**保留 last-good** 並記 WARN。

⚠️ **遷移警語**:把既有規則「改寫」成 match 規則等於**立即收窄**(原本靠 name 命中的使用者若不滿足新條件會馬上失去存取)。建議先**並行新增** match 規則、確認命中面後再撤舊規則。

**Namespace 誠實界線**(單一 trusted-hop MVP):claim key 即命名空間單位——部署**不得**把兩個不同上游來源對映到同一個 claim key(平台無從區分);真正的 issuer namespace 待 JWT 驗簽(D2-A)時由 `iss` 天然提供。

### RBAC 組織範圍(org-scope,身分綁定;LD-6 P4)

規則可加選配的 `org-scope: <claimKey>`,把該規則的租戶範圍**限縮到「使用者身分裡那個經驗證的組織 claim,落在該租戶所屬組織清單」的租戶**——組織清單來自平台管理層維護的 `_tenant_orgs.yaml`(admin-only、非租戶自屬性),以租戶 ID 反查。**一條規則涵蓋所有組織**,範圍由使用者身分動態決定(不在規則裡寫死組織代碼):

```yaml
# _rbac.yaml
groups:
  - name: 組織維運
    match:
      groups: [operators]
    org-scope: org-code        # 限縮到「使用者 org-code ∈ 該租戶組織清單」的租戶
    permissions: [read, write]

# _tenant_orgs.yaml(平台管理層維護,admin-only,無寫入 API,GitOps/管理員直改)
tenant_orgs:
  team-alpha-01: [ORG-4821]              # 屬單一組織
  team-alpha-02: [ORG-4821, ORG-5533]    # 1:N,屬多個組織
  team-beta-01:  []                       # 已建但未指派(unlabeled)
```

- **opt-in、預設零行為變化**:沒有 `org-scope:` 的規則行為完全不變(如平台管理員 `tenants: ["*"]` 照樣看全部、不受影響)。`org-scope:` 引用的 claim key **必須在 `--identity-claim-headers` 宣告**,否則載入錯誤。
- **fail-closed**:使用者無該 org claim、或 org 不在租戶清單 → 該規則不命中;**未指派組織(空清單)的租戶**在 enforce 下不可見(shadow 下仍可見+計數,供管理員補齊對應)。
- **判定面涵蓋(P4a+P4b+P4c)**:org 軸作用於 list 可見性(`GET /api/v1/tenants`,P4a)、per-tenant 寫入 / admin 授權(PUT tenant、custom-alerts、batch、group batch、federation token 簽發 / 清單 / 撤銷,P4b)、與 read-by-id 單租戶讀取(GET `/{id}`、`/effective`、`/metrics`、`/access`、`/federation`、POST `/diff`、`/validate`,P4c),外加 group 成員 / PR / task 清單的 collection 讀取過濾(P4c);全由**同一支** `--rbac-org-scope-enforce` 原子支配(見上方 org 軸段落)——不存在「list 藏、寫入開、直讀開」的中間態。read-by-id 的 org gate 在 rbac middleware(handler 讀檔前),故 enforce 下 cross-org 與不存在同回 403(不洩漏存在性);`/access` 為 recipe-preview PEP 契約(#657),org-deny 一律 403(existence-blind,不回 404)。
- **設定變更順序**:先落 `_tenant_orgs.yaml`(補齊 org 對應)、後落引用 org-scope 的 `_rbac.yaml`——兩者是各自 hot-reload 的檔,反序會有一個輪詢窗內 org-scoped 規則對尚未標記的租戶全 deny(fail-closed、非洩漏,但徒增 would-deny 噪音)。
- `_tenant_orgs.yaml` 走 strict 解析(typo=載入錯誤),org **僅**從此檔以租戶 ID 反查、**絕不**讀租戶自己的 `_metadata`(組織是授權邊界、非租戶可改屬性)。

### 身分 claims 縫

`--identity-claim-headers`(或 `TA_IDENTITY_CLAIM_HEADERS`;helm `--set identity.claimHeaders.<claimKey>=<Header-Name>`)以逗號分隔的 `claimKey=Header-Name` 對宣告「哪個 trusted-hop 標頭 → 哪個具名 claim」,例如 `org=X-Auth-Request-Org,region=X-Auth-Request-Region`。設定後請求 principal 會載運這些具名 claims,`GET /api/v1/me` 回應多出 `claims` 欄位;claims 在 `_rbac.yaml` 的 `match:` 規則引用時**參與授權**(見上節「RBAC match 規則」;ADR-027 / LD-6 P2+P3),未被任何規則引用的 claim 只載運、不影響決策。標頭與 `X-Forwarded-Groups` 同一信任邊界:必須由 trusted hop(oauth2-proxy)注入且對外 strip-and-set,不可被 client 偽造;空值 / 缺席的標頭不會成為 claim;同名標頭出現**多行**時該 claim 直接拒載並記警告(平台側 backstop:防 proxy 誤用 append 時的 first-value 劫持——Go `Header.Get` 只取第一行)。預設空 = 縫關閉,行為與 JSON 輸出完全不變;格式錯誤(缺 `=`、空 key、空 header 名、重複 key、key 超出 `[A-Za-z0-9_.-]`、header 名超出 `[A-Za-z0-9_-]`——含 `=`/空白的名字真實請求永遠帶不到,寧可啟動就擋)啟動即失敗(fail-loud)。

## 寫回模式

| 模式 | 行為 | 適用 |
|------|------|------|
| `direct` | 直接 `git commit`，**不 push** | 與 exporter 共用同一棵樹的環境（try-local）；chart 部署下寫入送不到 exporter，confDir 用預設 emptyDir 時重啟即失，需 `writeBack.directAcknowledgeNotDelivered=true` 才肯 render（#2855） |
| `pr` / `pr-github` | 建 feature branch + GitHub PR | GitHub.com / Enterprise |
| `pr-gitlab` | 建 feature branch + GitLab MR | GitLab.com / 自託管 |

PR 模式啟動時會驗證 token + 連線;失敗只印 WARN(後續開 PR 會回 503)。分支名是 `tenant-api/<租戶>/<UTC 秒>-<12 位十六進位亂數>`(批次 `tenant-api/batch/<UTC 秒>-<亂數>`);分支推不上 origin(被拒、連不上、逾時)時回 503 `GIT_PUSH_FAILED` + `Retry-After`、不開 PR,可直接重試;push 逾時但遠端已收下時 origin 可能留下沒有 PR 的分支,需人工清理([#1724](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1724),詳見 [hardening §5.5](../../docs/api/tenant-api-hardening.md))。範例:

```bash
# GitHub Enterprise
export TA_WRITE_MODE=pr-github
export TA_GITHUB_TOKEN=...                # 需 contents:write + pull_requests:write
export TA_GITHUB_REPO=org/config-repo
export TA_GITHUB_API_URL=https://github.internal.example.com/api/v3

# GitLab 自託管
export TA_WRITE_MODE=pr-gitlab
export TA_GITLAB_TOKEN=...                # 需 api scope
export TA_GITLAB_PROJECT=infra/alerting-config
export TA_GITLAB_API_URL=https://gitlab.internal.example.com
```

## 部署

> Kubernetes 上的完整導引(Helm values、oauth2-proxy、PR 寫回、HA)以 **[平台工程師指南 §部署 tenant-api](../../docs/getting-started/for-platform-engineers.md)** 為準,本節只給最小指令。

```bash
# Helm（版本見 Releases / CHANGELOG；省略 --version 取最新，或 --version <x.y.z> 釘版）
helm install tenant-api oci://ghcr.io/vencil/charts/tenant-api \
  -n monitoring --create-namespace -f values-override.yaml
# 或指向本地 chart：helm install tenant-api ./helm/tenant-api -n monitoring -f values-override.yaml
```

`values-override.yaml` 必須設 `writeBack.mode`(沒有預設值,不設 chart 就不 render;#2855),PR 模式另需 `gitRepoUrl`、`writeBack.github.repo`(或 `writeBack.gitlab.project`)與 `writeBack.token.secretName`,欄位說明見 chart 的 `values.yaml`。

Chart 會建立:Deployment + oauth2-proxy sidecar、Service、RBAC ConfigMap、NetworkPolicy、PDB。

對外入口：設 `ingress.enabled=true` 與 `ingress.hosts` 會多建一個 Ingress（預設關閉，#2027）。後端固定是 Service 的 `http` port（→ oauth2-proxy :4180），**不可設定**——internal port 8080 信任注入的身分 header，永遠不經 Ingress 對外（GHSA-3g2h-rf85-5rrv）；`oauth2Proxy.enabled=false` 時 render 直接失敗。

本機 Docker(**僅 Linux**:下面把主機上的 Unix socket bind-mount 進容器,macOS／Windows 的 Docker Desktop 在 VM 裡跑容器,主機的 socket 接不過去——請改用 [try-local](../../try-local/README.md))。從 repo root build,因 go.mod 需 threshold-exporter 模組。tenant-api 需要產生器服務(`TA_GENERATOR_SOCKET`,#2903):conf.d 要是 git repo,產生器與 tenant-api 同一個 uid;完整步驟見 [QUICKSTART](QUICKSTART.md) 路徑 A,或直接用 [try-local](../../try-local/README.md)(compose 已含產生器容器)。

```bash
docker build -t tenant-api -f components/tenant-api/Dockerfile .
# 產生器服務(PATH 上要有 amtool):
python3 scripts/tools/ops/generator_service.py --socket "$(pwd)/gen/generator.sock" \
  --repo "$(pwd)/conf.d" --config-subdir . --work-dir "$(pwd)/gen/work" &
# TA_STATE_*：群組、views、federation 設定與 AccountID 帳本存在 conf.d 之外（必填，#2856）
docker run -p 8080:8080 --user "$(id -u):$(id -g)" -v "$(pwd)/conf.d:/conf.d" \
  -e TA_GENERATOR_SOCKET=/run/da-generator/generator.sock -v "$(pwd)/gen:/run/da-generator" \
  -e TA_STATE_STORE=dir -e TA_STATE_DIR=/state -v "$(pwd)/state:/state" tenant-api
```

Smoke test:

```bash
curl -s localhost:8080/health
curl -s localhost:8080/metrics
```

### state store 的備份與還原

tenant-api 自己的紀錄（群組、saved views、federation 白名單與子集、AccountID 帳本）在 state store 裡，一份就是完整快照，匯出不需要工具（[#2884](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2884)）：

```bash
# Kubernetes（configmap 模式）
kubectl -n tenant-api get configmap tenant-api-state -o yaml > tenant-api-state-backup.yaml
# 單機（dir 模式）：複製整個 TA_STATE_DIR
cp -a /state ./tenant-api-state-backup
```

⛔ **不要用 `kubectl apply`（或把目錄複製回去）還原。** 備份之後才配發的 AccountID 不在備份裡，`next_account_id` 也會倒退，下一個要 logs token 的租戶就拿到同一個 id——它讀得到前一個租戶還在保留期內的 log（[ADR-021](../../docs/adr/021-tenant-log-query-federation.md)）。還原後的帳本格式完全正常；擋住它的是 AccountID 高水位（見下）：帳本倒退到上一次配發之前時，運行中的 pod 拒絕配發、重啟的 pod 拒絕啟動，直到用 `state restore` 合併回去。

還原一律用同一個 binary 的子命令，在 tenant-api 的 pod 裡跑（沿用 pod 的 ServiceAccount 與 `TA_*` 設定，不新增網路端點）：

```bash
# 先看會改什麼（不寫入）
kubectl -n tenant-api exec -i deploy/tenant-api -- \
  tenant-api state restore --from - --dry-run --keys _groups.yaml,_views.yaml < tenant-api-state-backup.yaml
# 確認後去掉 --dry-run 再跑一次
```

- `--from`：`kubectl get configmap ... -o yaml` 或 `-o json` 的輸出檔、dir 模式的目錄，或 `-` 讀標準輸入。備份至少要有一筆紀錄、且每個 key 都是 tenant-api 的 state 紀錄；其他 ConfigMap（例如 `threshold-config`、federation token store）或 conf.d 目錄會被拒絕（rc 1），不會被當成空備份「還原成功」。
- **帳本（`_account_registry.yaml`）一律合併、不覆蓋**：配發取「現行帳本、備份、仍有效的 logs token」三者聯集；`next_account_id` 取現行、備份加上安全邊際、聯集中最大 id + 1、AccountID 高水位 + 1 四者最大，只升不降。同一租戶在兩處的 id 不同、或同一個 id 屬於兩個租戶時，整次還原拒絕（rc 1）、什麼都不寫，並列出衝突。帳本不能列在 `--keys`。
- **安全邊際**（`--account-id-margin N`，預設 conf.d 租戶數的兩倍、至少 10000）：加在備份的 `next_account_id` 上。帳本與 federation token store 一起從同一份備份還原時（預設兩者在同一個 namespace），備份之後配發的 id 沒有任何地方記得，只能整段跳過；邊際就是跳過的長度。加在備份上，所以同一份備份以同樣的邊際還原兩次，第二次不會再變（租戶數變動使預設邊際改變時，會再往上跳，計數器仍只升不降）。conf.d 讀不到時要明示 `--account-id-margin`。
- **`--no-backup`**：不讀備份，只合併現行帳本、有效 logs token 與高水位。用在帳本空白、高水位還在（重新安裝）而 tenant-api 拒絕啟動時。需要啟用 federation（高水位與 token 都在 federation token store）；不能與 `--from`、`--keys`、`--account-id-margin` 並用。
- **其他紀錄只有列在 `--keys` 才整筆覆蓋**；沒列的會印在「not restored」一行。
- 啟用 federation（設了 `TA_FEDERATION_KEY`）時會讀 federation token store 取有效 logs token 的 `account_id` 與 AccountID 高水位；讀不到就拒絕還原。
- dir 模式下伺服器與子命令以檔案鎖（flock）序列化寫入，伺服器運行中也可以還原。
- 結束碼：0 完成（或 dry run 完成）、1 拒絕或失敗、2 用法錯誤。
- **AccountID 高水位**（[#2928](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2928)）：配發過的最大 AccountID 另記在 federation token store ConfigMap 的 annotation `tenant-api.vibe/account-id-high-water`（旁邊的 `…-base` 記下預留當時帳本的 `next_account_id`），不在 `tenant-api-state` 裡，所以蓋回舊備份不會讓它倒退。每次配發先預留高水位、再寫帳本，配發前比對：
  - `next_account_id` 高於高水位：正常；高水位落後（升級後第一次啟動）就補齊。
  - 介於 base 與高水位之間：上一次預留沒寫進帳本（寫入失敗，或剛好那一次寫入被蓋回），跳過整段，記一筆 `account_id_high_water_skip` WARN。Backfill 一批算一次預留，失敗時整批跳過。
  - 低於 base：帳本倒退到更早的狀態，拒絕配發（HTTP 500，Backfill 即使沒有要配發的租戶也拒絕）、開機時拒絕啟動，指示跑 `state restore`。已有 id 的租戶照常取得原 id。

  ⚠️ 升級後第一次啟動以當下帳本為基準。能改 token store ConfigMap 的人也能改小高水位，這部分交給叢集 RBAC。
- 啟用 federation 時，tenant-api 開機會比對有效 logs token 與帳本：token 的 `account_id` ≥ `next_account_id`、或與帳本對該租戶的配發不一致，就拒絕啟動並列出不一致的 token。帳本被舊備份蓋回時就是這個形狀；用 `state restore` 以同一份備份重做還原即可把這些 id 併回。此時 pod 起不來（CrashLoopBackOff），`kubectl exec` 進不去；改用 `kubectl debug` 複製一個 pod 並把主程式換成子命令（沿用同一個 ServiceAccount、`TA_*` 環境變數與掛載）：

  ```bash
  POD=$(kubectl -n tenant-api get pod -l app.kubernetes.io/name=tenant-api -o name | head -1)
  kubectl -n tenant-api debug "$POD" -i --copy-to=tenant-api-restore --container=tenant-api -- \
    tenant-api state restore --from - --dry-run < tenant-api-state-backup.yaml
  kubectl -n tenant-api delete pod tenant-api-restore   # 用完刪掉
  ```

⚠️ 已知限制：帳本與 federation token store 一起被還原成同一份舊備份時，高水位與 token 紀錄也一樣舊，只剩安全邊際擋著：備份之後配發的 id 數超過邊際，之後的配發**可能重發它們**（跨租戶 log 洩漏）。備份越新，需要的邊際越小；預期配發量大時調高 `--account-id-margin`。


### dir 模式的外部寫入者必須拿 flock

`dir` 模式**只供開發與 try-local**；正式環境用 `configmap` 模式：tenant-api 以 ConfigMap 的 `resourceVersion` 做樂觀並行控制，讀取之後若紀錄被別人改過，它的寫入會被 API server 拒絕、重讀後再套用，不會蓋掉別人的修改。

`dir` 模式每筆紀錄是 `TA_STATE_DIR` 下的一個檔案。tenant-api 寫入時先對 **`$TA_STATE_DIR/.lock`** 取得 exclusive `flock(2)`，在鎖內重讀紀錄、套用變更、以暫存檔加 rename 寫回，才釋放鎖。這把鎖是 **advisory**：只擋同樣去拿它的程序。因此：

- ⛔ **沒拿鎖就改檔，與 tenant-api 的寫入重疊時會被靜默覆蓋。** tenant-api 在鎖內才讀紀錄，所以只有落在它某次寫入「讀取 → rename 寫回」之間的外部修改會被蓋掉；這個窗口很短、何時出現無法預測，不能拿「試過沒事」當安全。被蓋掉時它寫回的是「它讀到的內容加上它的變更」——你的修改不見了，API 照樣回成功，log 也沒有任何警告。
- **拿了同一把鎖再讀改寫，修改會保留**：tenant-api 等你釋放鎖後才讀紀錄，以你寫入的內容為基礎。`tenant-api state restore` 就是這樣與運行中的伺服器並存的。例外：聯邦白名單／子集的 PUT 是整筆取代，見下方限制。

外部寫入者（腳本、手動修改）的規則：

1. 對 `$TA_STATE_DIR/.lock` 取得 exclusive flock，**涵蓋整段讀取→修改→寫回**；只在寫回那一下拿鎖不夠。
2. 寫回時用同目錄下以 `.` 開頭的暫存檔再 `mv`：tenant-api 的讀取（API 讀取、週期重載）不拿鎖，直接覆寫檔案可能讓它讀到寫了一半的內容。
3. 不要刪除或替換 `.lock` 本身：flock 鎖的是那個檔案，換掉之後雙方各鎖各的，互斥就不存在了。

```bash
# 例：在 try-local 的 state volume 上改一筆紀錄。compose 專案名是 da-try-local，volume 實名帶前綴；
# --user 必須是 tenant-api 的 uid（try-local 為 10001），見下方限制。alpine 的 busybox flock 用的是 flock(2)
docker run --rm --user 10001:10001 -v da-try-local_tenant-api-state:/state alpine:3.20 \
  flock /state/.lock sh -c 'sed "s/old/new/" /state/_views.yaml > /state/.edit && mv /state/.edit /state/_views.yaml'
```

限制：

- ⛔ `.lock` 與紀錄檔必須是 tenant-api 的 uid 讀寫得到的。以 root 跑外部寫入者時，若 `.lock` 還不存在，flock(1) 會以 root 建一個 0644 的 `.lock`，之後 tenant-api 開它（`O_RDWR`）會 permission denied，**每一次 state 寫入都回 503**；紀錄檔同理（寫回的檔案若 tenant-api 讀不到，讀取與寫入都失敗）。用 tenant-api 的 uid 跑，或事後 `chown`。
- 鎖只在同一台主機、同一個檔案系統上互斥；非 Unix 平台（以及 AIX、Solaris）的 build 不拿 flock，該處 tenant-api 只有行程內互斥。
- 拿鎖防的是「並行讀改寫遺失其中一方」。API 本身就是整筆取代的寫入（例如 `PUT /api/v1/federation/policy`、`PUT /api/v1/tenants/{id}/federation`）照語意以請求內容蓋掉整筆紀錄，與有沒有拿鎖無關。
- 群組、views、聯邦白名單在 tenant-api 內有快照。快照只在兩種時候更新：同類紀錄經 API 寫入之後（群組寫入只重載群組，views、白名單同理），以及週期重載（`--reload-interval`，預設 30s；內容的 SHA-256 沒變就不重新解析）。外部修改在那之前不會反映到這些 API 的讀取結果。外部寫入了無法解析的 YAML 時，週期重載只記 WARN（與 reload-failure 指標）、保留舊快照；而該紀錄的 API 寫入（群組、views）會因為解析不了現存內容而失敗（500 `CONFIG_DECODE_ERROR`），直到有人把檔案修好。
- try-local 的 `seed/state-seed.sh` 不拿鎖，因為它在 tenant-api 啟動**之前**執行，沒有並行的寫入者。

## 開發

```bash
go test ./... -race          # 全測試 + race detector
golangci-lint run            # lint
go build -o tenant-api ./cmd/server
```

PR 合併前以 repo 層 `make pr-preflight` 統一把關。

## 延伸閱讀

- **跑起來**:[QUICKSTART.md](QUICKSTART.md) ·  **整套體驗**:[`try-local/`](../../try-local/)(portal 改 config → 本服務 commit → exporter 熱重載 → 告警)
- **角色指南**:[平台工程師](../../docs/getting-started/for-platform-engineers.md) · [領域專家](../../docs/getting-started/for-domain-experts.md) · [租戶](../../docs/getting-started/for-tenants.md)
- **版本歷程**:[CHANGELOG.md](../../CHANGELOG.md) · 版號線 `tenant-api/v*` → `ghcr.io/vencil/tenant-api` image + Helm chart
- **設計與 API 深度**:[架構與設計](../../docs/architecture-and-design.md) · [API 文件](../../docs/api/README.md)
