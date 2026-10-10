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
- 寫入時做 schema 驗證、domain policy 檢查,再以 git commit-on-write 或開 PR/MR 寫回
- 以 RBAC 對租戶列表 / 群組成員 / pending PR / async task 結果做**逐呼叫者**過濾
- 租戶聯邦:簽發短效 token 供租戶拉取自己的 metrics 子集、管理聯邦白名單與每租戶子集
- 維運硬化面:逐呼叫者限流、`X-Request-ID` 回拋、request body 大小與內容範圍驗證

**不做**

- **不做認證**——身分來自 oauth2-proxy 注入的 `X-Forwarded-Email` / `X-Forwarded-Groups`
- **不做 schema 演化**——YAML schema 由 threshold-exporter 的 config 套件擁有
- **不做持久化 task store**——async task 存在記憶體,pod 重啟後消失(polling 收到 404 視為 task 遺失)。唯一的跨 replica 持久狀態是聯邦 token 記錄(存於共用 ConfigMap,非資料庫)
- **不做 PR / MR 合併**——建立後等人工 review,只追蹤狀態

## 架構速覽

- **chi router** + 標準 middleware 鏈(RequestID / Logger / Recoverer / Timeout);**不用 `RealIP`**——它會用 client 送的 `X-Forwarded-For`/`X-Real-IP` 覆寫 `RemoteAddr`(可偽造),故保留真 TCP peer(ADR-027)
- **`X-Request-ID` 回拋**:把 request id 寫回 response header,方便對應後端 log
- **逐呼叫者限流**:sliding-window,預設 100 req/min/caller,可關閉
- **RBAC**:`_rbac.yaml` 定義 group → tenant → permission,熱重載(預設 30s)。**未設 `--rbac`** 進入 open-read 模式(啟動 WARN);**設了 `--rbac` 但檔案零 group**(打錯字/空檔)→ **fail-closed 拒絕存取**(ADR-027 MED-8),`--rbac-empty-open` 為 rollback 逃生
- **逐租戶授權**:群組 / view / batch / PR 列表 / task 結果的每個成員都再過一次 per-tenant RBAC
- **GitOps Writer**:schema 驗證 → 寫 YAML → `git commit`(operator email 當 author,service account 當 committer)
- **衝突偵測**:寫入後比對 commit 的 parent 與寫入前的 HEAD;若期間被外部 commit 移動 → 回 409
- **租戶檔解析**:一個租戶的設定檔可拼成 `<id>.yaml` 或 `<id>.yml`(副檔名大小寫不敏感),讀取與寫入都解析**同一個實際檔案**——寫入落在既有檔上,只有全新租戶才用 `.yaml` 預設拼法。同一個 id 被兩種拼法同時宣告時 **fail-loud**:`GET`／`PUT`／diff／custom-alerts 只看檔名就回 409；`/effective` 則在兩個檔都確實宣告這個租戶時才回 409,另一個拼法是 `tenants: {}`、只宣告別的租戶、解析不了、dangling symlink 或指向目錄的 symlink 時照常回 200(判定用 exporter 同一支 walker)。兩者都是 `CONFLICT`,訊息只帶兩個基底檔名(如 `<id>.yaml`、`<id>.yml`),不含伺服器路徑([#2511](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2511))，`POST /validate` 回 200 `valid: false`，租戶清單整個以 500 失敗並指名兩個檔(與 threshold-exporter 的 `DuplicateTenantError` 同一立場,不做優先序猜測)
- **內容範圍驗證**:固定欄位走 struct validator,`Patch` / `Filters` map 走逐 key 規則,違反一次回完整清單
- **憑證遮罩**([#1560](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1560)):租戶檔的 receiver 憑證(webhook / Slack / Teams / Rocket.Chat 的 URL、PagerDuty key、SMTP 密碼、`http_config` 的密碼 / bearer token / proxy URL,以及 Alertmanager 同類欄位)只給**對該租戶有寫入權限**的呼叫者看——判定與 `PUT /tenants/{id}` 的閘門是同一個述詞,所以看到遮罩的人一定不能 `PUT`。其他人在 `GET /tenants/{id}`(`raw_yaml`、`custom_alerts`)、`/effective`、`POST /diff` 看到的值是 `<masked: write permission required>`,回應帶 `masked: true`;`raw_yaml` 是重新序列化、**不含註解**的版本(註解掉的舊憑證也算憑證)。依 key 名比對、不限位置,所以同名但不是憑證的值(例如某個 `url`)也會被遮。檔案用了 anchor / alias / merge key、有多個 document 或不是 YAML 時無法確定遮得乾淨,`GET` 改回空的 `raw_yaml` 與 `custom_alerts` 加 `raw_yaml_withheld: true`,`/diff` 回 422 `MASKED_PREVIEW_UNAVAILABLE`。遮罩後的 `/diff` 對兩側的遮罩版本比對,只改憑證的提案 `has_diff` 為 `false`;提案超過租戶文件大小上限或 request body 上限回 413。`PUT` 的 body 只要有任何值解碼後等於這個佔位字串就回 400,避免遮罩後的內容被寫回蓋掉真值。`source_hash` / `merged_hash` 仍是原檔的 hash,未遮。完整說明見 [tenant-api-hardening §3.7](../../docs/api/tenant-api-hardening.md)
- **Domain policy**:寫入前檢查租戶的 domain 規則,違反回 403。判的是租戶**解析後**的 routing([#2280](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2280)):根目錄 `_routing_defaults` → `_routing_profile` 指向的 profile → 租戶 `_routing`,主 receiver、`overrides`、`routes` 每個 receiver 的 type 都判;`forbidden_receiver_types` 與 `allowed_receiver_types` 分開判,可同時回兩條(403 的 `violations[].target` 指出是哪個 route)。policy 讀 `_domain_policy.yaml` 與 `_domain_policy.yml`,兩檔可並存:domain 相加,同名 domain 以 `.yml` 整筆為準,與 route generator 相同([#2658](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/2658))。`domain_policies:` 存在但不是 mapping(含 `null`、`~`、只寫 key)判為讀不了,與 da-guard 的 `domain_policy_unusable` 同一判準([#2684](https://github.com/vencil/Dynamic-Alerting-Integrations/pull/2684)):熱重載保留該檔上一份讀得了的內容,另一個檔照常生效。**檔案存在(`Lstat` 看得到)卻讀不了——是目錄、dangling symlink、沒有權限、解析失敗(含下述與產生器對齊的整份拒收)——而且它從未讀成過**,就判 policy 不可用([#2486](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2486) Q7-2):直寫模式下 `PUT /tenants/{id}`、`POST /tenants/batch` 與 `POST /groups/{id}/batch` 中寫 `_routing_receiver_type` 或碰 `_routing`／`_routing_profile` 的請求回 503 `POLICY_UNAVAILABLE`(帶 `Retry-After`,不寫入;async 批次在執行時逐筆再判),其他寫入照常;`/ready` 維持 200(單一 replica + `Recreate`,NotReady 會讓整個 API 斷線),改由 `tenant_api_policy_available` 與告警 `TenantApiPolicyUnavailable` 反映;檔案不存在 = 沒有 policy(合法)。`--policy-unavailable-open` 為逃生開關。policy 檔的讀法以產生器(PyYAML)為準,讀不出相等結果就整份拒收(仍比產生器寬的已知缺口見本段句末):多文件、最上層 `tenants:` 不是 mapping、值的位置出現 `!!merge` 標記的節點都判不可用;`tenants` 項目一律取原文(`010`、`~`、`!!binary aGk=` 照字面);receiver type 項目照 PyYAML 型別讀(`! "null"` 不是字串:放在 `forbidden_receiver_types` 不禁止任何 type,放在 `allowed_receiver_types` 仍算非空、照樣限制,且不允許任何 type),PyYAML 建構失敗的項目(`!!int x`、`!custom x`)整份拒收;非特定 tag `!` 照 PyYAML 解讀(`! "true"` 是布林、`! "<<"` 是 merge key;[#2730](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2730));PyYAML 沒有 constructor 或建構失敗的值(`=`、`! "*"`、`!custom x`、`!!value x`、文件根 `!!merge`)寫在會被建構的位置也整份拒收;`require_critical_escalation` 為 PyYAML 拒收的值(`!!bool maybe`、`!!null {}`、`{<<: 1}`)同樣整份拒收(先前只關掉該約束),讀得出但不是布林的值(`"true"`、`1`)只讓該約束不生效,與產生器相同;帶 `%YAML 1.2`(1.1 以外的 1.x)或未知 directive 的 policy 檔整份拒收(產生器照讀;[#2759](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2759));空白照產生器規則讀:TAB 只能出現在引號、註解、block scalar 內,`key:<TAB>值`、行尾 TAB、`---<TAB>`、`%YAML<TAB>1.1`、`%YAML 1.1#c` 整份拒收;非 UTF-8(含帶 BOM 的 UTF-16)整份拒收;名為 null 的 domain key(`~:`)不算 domain([#2486](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2486))。仍比產生器寬、可能讀出產生器不讀之 policy 的已知缺口:巢狀很深的檔、flow 內以冒號結尾的 plain scalar、`!!set`、以 `!!omap`/`!!pairs` 為 key([#2759](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2759))。PR 模式判最新 base 時讀不了即拒寫(403 `POLICY_VIOLATION`;目錄與 dangling symlink 也算讀不了);profile / defaults 檔讀不開時記 warn、略過該層照判(fail-open)。範圍是 receiver type 與 `require_critical_escalation`(語意見 [config-driven 的 Domain Policies](../../docs/design/config-driven.md)),receiver 形狀由下一項判
- **Receiver 形狀**([#2295](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2295)):`PUT /api/v1/tenants/{id}` 的 body-only pre-flight(`POST /{id}/validate` 跑同一段,判定一致)檢查 body 自己寫的 receiver(`_routing` 的主 `receiver`、`overrides[].receiver`、`routes[].receiver`)——未知 type、缺必填、格式不對、PagerDuty 雙鍵、`send_resolved` 不是 YAML 布林、`http_config` 多種 auth 或 `proxy_url` Go `net/url` 解析不了,回 400 `INVALID_BODY`,每個問題一條 `violations[]`(`field` 如 `tenants.<id>._routing.receiver.url`),直寫與 PR 模式都不寫入。規則與 da-guard 共用 `threshold-exporter/app/pkg/receiverspec`。從 `_routing_defaults` 或 routing profile 繼承的 receiver 不在此判(da-guard 判解析後的);pre-flight 由 handler 在 policy 檢查之後呼叫(`gitops.ReceiverPreflight`),所以 policy 違規仍優先回 403
- **Async task**:worker goroutine 池跑批次,`/api/v1/tasks/{id}` polling
- **SSE 事件**:寫入成功後廣播 `config_change`,讓 UI 即時更新
- **Path traversal 防護**:租戶 id 驗證拒絕 `..`、`/`、`\`
- **寫入套用 tenant id 規則**(ADR-035):寫入的 id 必須是 DNS-1123 label(小寫字母、數字與 `-`,首尾為字母或數字,長度上限見 `docs/schemas/tenant-config.schema.json` 的 `definitions.tenantId`)。`PUT`、`POST /{id}/validate`、custom-alerts `PUT`、聯邦子集 `PUT` 在 handler 先回 400,租戶 batch 則是該筆結果 `status: error`;writer 的咽喉點 `gitops.guardTenantID` 對每個租戶寫入再判一次(`ErrInvalidTenantID`,`PUT` 等端點回 400),漏了 handler 那一關也寫不進去。讀取端點不套用,既有的不合規 id 仍可讀
- **自帶輕量 Prometheus metrics**(無額外 client 函式庫依賴)、安全預設(header read timeout、1 MB body 上限、non-root 容器)

## API 參考

> 慣例:除 Health / Identity / Metrics 外皆需身分(oauth2-proxy header)。「權限」欄為該端點要求的 RBAC 動作;標「逐租戶」者會對每個受影響租戶再驗一次。

### Health / Identity / Metrics(無需認證)

| Method | Path | 說明 |
|--------|------|------|
| `GET` | `/health` | Liveness——永遠 200 |
| `GET` | `/ready` | Readiness——config 目錄無法存取時回 503 |
| `GET` | `/metrics` | Prometheus 文字格式 |

### 租戶配置

| Method | Path | 權限 | 說明 |
|--------|------|------|------|
| `GET` | `/api/v1/me` | read | 當前呼叫者的 email + groups + RBAC 摘要 |
| `GET` | `/api/v1/tenants` | read | 列出 RBAC 可見的租戶;設定檔存在但無法使用的租戶以**降級列**回傳(只有 `id` + `config_error`,見下方「降級列」),不再靜默消失;每筆的 `config_derived`(`silent_targets` / `maintenance_active`)見下方「依設定推算的狀態」 |
| `GET` | `/api/v1/tenants/search` | read | 伺服端 search / filter / 分頁(`q` / `environment` / `tier` / `domain` / `db_type` / `tag` / `page_size` / `offset` / `sort`);與 `/tenants` 共用快照快取。回應頂層的 `config_derivation` 附推算時間,並列出解析失敗而被跳過的檔案(`parse_failed_files`) |
| `GET` | `/api/v1/tenants/{id}` | read | 取得 raw YAML + `resolved_thresholds`(套根目錄的 defaults carrier、根目錄平台檔的 `tenants:` 區塊與租戶選用的 `_profile`,見下方「單一租戶端點與 conf.d 範圍」);`.yaml` / `.yml` 兩種拼法皆可解析,同一 id 兩種拼法並存回 409;對該租戶沒有寫入權限的呼叫者看到遮罩後的憑證(見上方「憑證遮罩」) |
| `GET` | `/api/v1/tenants/{id}/effective` | read | 沿 `_defaults.yaml` 鏈逐層合併租戶覆寫後的**設定**(對齊 `describe_tenant`),不是 exporter 會 emit 的值(見下方「單一租戶端點與 conf.d 範圍」)+ 繼承來源鏈 + 雙重 hash(`source_hash` / `merged_hash`,供變更偵測);值照原文保留,defaults 鏈、根目錄缺 `defaults:` 包裝、平台檔／租戶檔值解析不了而 exporter 不送的,在 `not_served` 逐 key 標原因(profile 層被丟掉的值與過期 override 目前不標),defaults 鏈上 exporter 整份不讀的語法錯檔當空檔、列在 `chain_parse_failed`(仍回 200,[#2296](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2296));同一 id 的兩種拼法都宣告該租戶時回 409;對該租戶沒有寫入權限的呼叫者看到遮罩後的憑證(見上方「憑證遮罩」) |
| `GET` | `/api/v1/tenants/{id}/access` | read | 輕量 RBAC 授權探測:可讀該租戶回 `200 {allow,tenant,permission}`、否則 `403`。供姊妹服務(如 recipe-preview #657)重用 tenant-isolation 決策、不重寫 RBAC 也不過度取得設定 |
| `GET` | `/api/v1/audit/tenants/{id}/access-report` | platform admin(非 org-scoped) | 逆向存取稽核報告:列出「誰、經哪條規則、在什麼 org 條件下」能存取該租戶(shadow/enforce 雙態並列;audit-only,不參與授權)。`?include=org_values` 展開 org 值、`?view=redacted` 去識別化投影;非 admin 恆定 403(防租戶枚舉)。redacted 視圖無法消除 grant 存在性本身的 org-membership 推論(value-pinned org rule 的 grant entry 即弱識別)。**⚠️ environments/domains 為 rule 原文照錄、僅約束租戶清單可見性、不阻擋 read-by-id/write**——受影響 grant 以機器可讀欄 `constraints_not_evaluated` 標示,稽核判讀勿當作存取邊界 |
| `POST` | `/api/v1/audit/tenants/{id}/access-report/dry-run` | platform admin(非 org-scoped) | what-if 稽核:body 送候選 `_rbac.yaml`(`{"candidate":{"rbac_yaml":"..."}}`),與 live 基準各算一份逆向報告並做結構化 diff(changed / added / removed;以 rule name 對齊,rename 呈現為 removed+added)。純模擬、不寫入;query 同上(`include` / `view`);orgs 沿用 live `_tenant_orgs.yaml`;候選解析失敗回 400 `CANDIDATE_INVALID`;非 admin 恆定 403(同上) |
| `PUT` | `/api/v1/tenants/{id}` | write | 寫入(驗證 → policy → 寫入 → commit / PR;policy 判 body 解析後的**整份** routing,不含平台檔 `tenants:` overlay 提供的 `_routing_profile`);body 格式錯誤回 400;同一 id 兩種拼法並存回 409;租戶已由其他檔宣告回 409 `TENANT_DECLARED_ELSEWHERE`(見下方「單一租戶端點與 conf.d 範圍」) |
| `POST` | `/api/v1/tenants/{id}/validate` | read | Dry-run 驗證,不寫入 |
| `POST` | `/api/v1/tenants/{id}/diff` | read | 預覽 unified diff(標頭是 `current/<id>.yaml` / `proposed/<id>.yaml`,不含伺服器路徑);同一 id 兩種拼法並存回 409;租戶已由其他檔宣告回 409 `TENANT_DECLARED_ELSEWHERE`;對該租戶沒有寫入權限的呼叫者拿到的是兩側都遮罩後的 diff(見上方「憑證遮罩」) |
| `POST` | `/api/v1/tenants/batch` | read + 逐租戶 write | 批次**部分合併** patch(只改指定 key、保留其餘 key 與註解,非整檔取代;逐筆 RBAC + policy;`?async=true` 走 task 池)。每筆 op 另可帶 `unset: ["_routing"]` 刪除 key(目前只接受 `_routing`;刪掉即回到 `_routing_defaults` + profile,重新啟用被停用的路由;key 或租戶不存在時為 no-op:不寫入、不開 PR、不建立租戶)。patch 碰到 `_routing_profile` 或 `_routing`、或 unset `_routing` 時,把 patch 蓋上磁碟上的租戶 block(以及同一請求中同租戶前面已納入的 op)後判解析結果,違反的那筆被排除(PR 模式其餘照常開 PR);**只碰其他 key 的 patch 不判 routing**——磁碟上已違規的租戶,與 routing 無關的寫入不會因此被擋(與 PUT 不對稱) |

> **寫入回應**:`PUT /{id}` 回 `{"status","tenant_id"}`;PR 模式另含 `pr_url` / `pr_number`(CI 可據此取得待審 PR)。request body 直接送租戶 YAML,不需特定 `Content-Type`。

> **降級列(#1680)**:`GET /api/v1/tenants` 與 `/search` 對「conf.d 裡有這個租戶檔、但檔案無法使用」的租戶回一列 `{"id":"<id>","config_error":"<原因>"}`,其餘欄位全空——它的 metadata 是**未知**,不是「未標記」。`config_error` 的值是穩定契約:`unreadable`(stat/讀取失敗,例如斷掉的 symlink、權限不足)、`not_regular_file`(例如指向目錄的 symlink)、`malformed_yaml`(不是合法 YAML)、`invalid_config`(語法層面可解析為 YAML,但無法載入為租戶設定:結構不對、只有型別化解碼才會抓到的錯誤(例如重複的 key),或宣告了非 UTF-8 的租戶 id——threshold-exporter 會整份跳過這種檔);健康的列沒有這個欄位。空檔視為可用(列出一列無 metadata 的租戶,與過去相同)。**只有**命中規則對 `environments` 與 `domains` **都不設限**的呼叫者看得到降級列,shadow 與 enforce 模式皆然——受限呼叫者看不到,因為該租戶真實的環境/域讀不出來,不能當成未標記放行;org 軸照常判定(組織清單來自 `_tenant_orgs.yaml`,不受壞檔影響)。`/search` 的 metadata 篩選(`environment` / `tier` / `domain` / `db_type` / `tag`)不會命中降級列,`q` 仍比對其 `id`。`GET /api/v1/tenants/{id}` 對 `malformed_yaml` / `invalid_config` 的檔回 **200**,帶 `raw_yaml`、`source_hash` 與同義的 `config_error`,讓編輯器仍能開檔修正;exporter 不 serve 這種檔,所以**不帶** `resolved_thresholds`、`custom_alerts`、`validation_warnings` / `validation_notices`(不是空陣列——空陣列會被讀成「確定沒有」)。對這種檔的部分更新——`PUT /{id}/custom-alerts`、`POST /tenants/batch`、`POST /groups/{id}/batch`——一律拒絕且不寫入:前者回 409 `TENANT_CONFIG_NOT_LOADABLE` 並帶 `tenant_id`、`config_error`,batch 在直寫模式是該筆結果 `code: TENANT_CONFIG_NOT_LOADABLE`、PR 模式整批 409(判定以 PR 分支所依據的最新 origin base 為準)。須先修好租戶檔本身:`malformed_yaml` 與 `invalid_config` 的租戶檔可以用整檔 `PUT /{id}` 整份覆蓋修回(#2373、#2405);其他 `config_error`(讀不到、不是一般檔)能否以 `PUT` 修回取決於底層檔案狀態,不保證,必要時直接改 git。⚠️ tenant-api **解析不了**現有檔時,整檔 `PUT` 只接受**對所有租戶都有寫入權限**的呼叫者:命中一條 `tenants: ["*"]`、授予 `write`(或 `admin`)且不帶 `org-scope` / `environments` / `domains` 的規則——前綴樣式(如 `svc-*`)不算,帶 scope 限制的 `"*"` 規則不論 scope 軸處於 shadow 或 enforce 都不算。其他呼叫者回 400、檔案不動,訊息指明需要全租戶寫入權限,否則直接改 git。直寫模式下 `POST /{id}/validate` 的判定跟著呼叫者走;PR 模式的 dry-run 只檢查 body、不讀現有檔,所以反映不了這項限制(可能回 `valid: true`,實際 `PUT` 仍回 400)。放行時,eol 檢查把舊檔的 end-of-life recipe 用量當成**零**:body 不含 eol recipe 就放行,含了就擋——不會因為解析不了舊檔就放寬;解析得了的檔照實際用量比對;檔案存在卻讀不到(權限、I/O、指向目錄)則直接拒絕寫入。現有檔解析不了時,body 中自己以外的租戶區塊一律視為新增而被拒,所以這種檔無法經 API 保留其他租戶的區塊,需要時請直接改 git。「解析不了」以 eol 檢查的解碼為準(只解碼 `tenants:` 區塊,但整份仍須是合法 YAML);新增區塊的檢查以租戶設定的型別解碼整份,兩者對哪些壞檔解析得了不保證一致,必要時直接改 git。federation 的 orphan 偵測、啟動時的 registry 完整性檢查與寫入平面的檔案解析刻意**不看檔案內容**,壞檔租戶在這些平面仍算活著——所以 `PUT` 仍能把壞檔修回來。

#### 依設定推算的狀態(`config_derived`)

`GET /tenants` 與 `/tenants/search` 的每個租戶帶 `config_derived`:`silent_targets`(會被靜音的 severity)與 `maintenance_active`。它回答的是「依 tenant-api 手上這份 conf.d,threshold-exporter 會 emit 什麼」,**不是**從 Alertmanager 或線上 exporter 觀測到的狀態——rollout 有時間差,conf.d 範圍也不同(見下方「單一租戶端點與 conf.d 範圍」)。UI 文案標「依設定推算」。

- **同一份計算**:config 由 `config.LoadDir`(exporter 的冷載入)取得,狀態由 `OperationalStatesAt(now).ByTenant` 在每次請求時判讀,所以 `expires` 以請求當下為準。舊的 `silent_mode` / `maintenance` 欄位是租戶檔裡的原始值(`disable` 也是非空字串),不代表狀態。
- **快取**:載入結果放在快照快取裡,不是每個請求都重載。載入只在**不必等待**就拿得到 GitOps writer 的鎖時進行,所以不會讀到寫入進行中的樹,讀取也不會等寫入;單次載入持鎖有時限,超時(例如讀到會卡住的檔案)就放開鎖、回傳不帶 `config_derived` 的清單,並在卡住的那次返回前不再開始新的載入。writer 每段寫入結束時(成功或失敗)會把快照標為過期,**過期的快照不會再被當成已知狀態回傳**:writer 正在寫入時,尚未過期的快照照常回傳(`config_loaded_at` 標出它的時間,可能超過 TTL),已過期的則回傳不帶 `config_derived` 的清單;同一時間只有一個請求在重載,其他請求等它的結果,不會把「另一個請求在重載」誤判成「寫入進行中」。PR 模式下,工作樹不在 base branch 上、或有 loader 會讀到的未提交變更(已追蹤檔案的修改,或未追蹤的 `.yaml`/`.yml`)時(例如 PR 寫入後切回 base 失敗),不推算、不快取,各租戶不帶 `config_derived`;其他未追蹤檔案(如寫入中斷留下的暫存檔)不影響。沒有推算時,原始的 `silent_mode` / `maintenance` 也不回傳。API 以外的變更(git pull、ConfigMap 更換)最慢在 TTL 到期後生效——tenant-api 沒有 conf.d watcher。
- **解析失敗的檔案**:exporter 會跳過無法解析的檔案,這些租戶沒有 `config_derived`(根目錄的租戶檔以上方的降級列出現)。`/tenants/search` 的 `config_derivation.parse_failed_files` 列出這些檔案,讓呼叫端分得出「檔案壞了」與「沒有這個租戶」;子目錄的檔案與 `_` 平台檔只出現在這裡。
- **誰看得到什麼**:只有不受限的呼叫者(open mode,或規則對 org、environments、domains 都不設限的 platform admin)拿到相對於 conf.d 的完整路徑,以及載入失敗時 loader 的原始訊息(`load_error`,已去掉伺服器上的絕對路徑)。其他呼叫者的 `load_error` 是固定字串,不含任何檔名或租戶 id;`parse_failed_files` 只給檔名(不含目錄),且只列出呼叫者依降級列規則看得到的租戶的檔案,其餘只計入 `parse_failed_hidden`。conf.d 整份載入失敗(例如同一租戶宣告在兩個檔)時,各租戶不帶 `config_derived`。

#### 單一租戶端點與 conf.d 範圍

- **平台檔的 `tenants:` 區塊**:根目錄平台檔(`_` 開頭、非未被選用的 defaults carrier,例如 `_platform.yaml`)的 `tenants.<id>` 是平台替該租戶設的預設值。`GET /{id}` 的 `resolved_thresholds` 與 `/effective` 都套用它,規則與 exporter 的 `/metrics` 相同:租戶檔逐 key 優先、平台檔不能建立租戶、多個平台檔依檔名排序後者優先(#2019、#2208)。唯一刻意的差異是 `_metadata`:這兩個端點沿用 `/effective` 的規則,不繼承平台檔替租戶設的 `_metadata`;`/metrics` 的 metadata 則會帶入。`GET /{id}` 沒有 metadata 欄位,所以目前看不到這個差異。租戶檔寫舊拼法(如 `mysql_cpu`)、平台檔寫新拼法(`mysql_threads_running`)時,生效的是平台值(與 `/metrics` 相同),`validation_notices` 會說明租戶的值沒有套用,並提示改成新拼法。子目錄的平台檔、隱藏檔、無法解析的平台檔都不套用。
- **驗證只判租戶自己寫的 key**:`validation_warnings`(寫入這份檔會被擋的原因)與寫入驗證只看租戶檔本身;平台檔替該租戶設的 key 有問題(未知 key、`expires:` 格式錯、懸空的 `_critical`、未知 `_profile`、錯誤的 version label、舊拼法)**不擋寫入**,改以 `validation_notices`(寫入回應的 notices 同)回報,訊息帶 `platform file <檔名>, entry tenants.<id>:`(檔名是相對 conf.d 的檔名,不含伺服器路徑),要到那個平台檔修(#2208)。
- **展開 `_profile`**:`GET /{id}` 的 `resolved_thresholds` 與寫入驗證都照 exporter 的 `/metrics` 展開租戶選用的 profile(#1385):profile 定義在根目錄平台檔的 `profiles:`(`_profiles.yaml`、defaults carrier 或其他根目錄 `_` 檔;子目錄的檔、未被選用的 carrier、無法解析的檔都不算),多個檔定義同名 profile 時依檔名排序、逐 key 後者優先;選用的是租戶檔的 `_profile`,沒寫才用平台檔 `tenants:` 區塊替它選的。優先序:租戶檔 > 平台檔 `tenants:` 區塊 > profile(只補缺)> defaults;profile 不會補 `optional_overrides` 宣告而無平台值的 key。profile 讀自同一次有時限的根目錄讀取,不另外讀檔。
- **profile 的問題不擋寫入**:租戶的 `_profile` 指向任何根目錄平台檔都沒定義的名字,仍回 unknown profile(與過去相同,擋寫入);有定義的不再誤報。profile 補給租戶的那些 key 有問題(未知 key、`expires:` 格式錯、懸空的 `_critical`、錯誤的 version label、宣告而無平台值的 key)**不擋寫入**,改以 `validation_notices` 回報,訊息帶 `platform file <檔名>, profile "<名稱>"`,要到那個檔修。值本身無法解析(例如 `abc`)與租戶檔相同:不報,解析時退回 defaults。exporter 對這兩類情況(未知 profile、宣告而無平台值的 key)寫的 WARN,tenant-api 不寫進自己的 log——同樣的資訊已在回應的 warnings / notices 裡。
- **讀根目錄平台檔有時限**:`GET /{id}` 會讀根目錄所有 `_*.yaml` / `_*.yml`。讀取進行中才進來的 `GET /{id}`(不論哪個租戶)共用那一次讀取,看到的是**那次讀取開始時**的檔案內容——讀取開始後才改寫的平台檔,這些請求看不到;這個過期時間窗最多等於那一次讀取的耗時,上限是讀取逾時。讀取結束之後才開始的請求一定重讀,讀完的結果不保留。讀不完(例如名為 `_x.yaml` 的 FIFO、卡住的掛載)時回 500 `INTERNAL_ERROR`(固定訊息,不含路徑),一次卡住的讀取只佔一條 goroutine,不會隨並發請求數增加;卡住的那次讀取返回前,之後的 `GET /{id}` 立即回 500;刪掉那個檔不會解除,要讓讀取返回(FIFO 被開啟寫入)或重啟 tenant-api。
- **只認 `<id>.yaml` 租戶檔**:tenant-api 以 conf.d 頂層的 `<id>.yaml`(或 `.yml`)認租戶檔。宣告在其他檔(子目錄檔,或頂層共用檔的 `tenants:`)的租戶,`GET` 回 404、寫入(`PUT` / batch)回 409 `TENANT_DECLARED_ELSEWHERE`,不會寫進 `<id>.yaml`;custom-alerts 只接受既有的 `<id>.yaml`,沒有這個檔時回 404——同一 id 出現在兩個檔,exporter 會拒收整份設定(#2078)。`<id>.yaml` 已存在但沒宣告這個 id(例如 `tenants: {}`)時寫入也一樣回 409;`GET` 則回 200 並顯示該檔內容。`<id>.yaml` 與另一個檔都宣告同一 id 時,那棵樹 exporter 本來就不收,連更新 `<id>.yaml` 也回 409,要先移除另一份宣告。直寫模式的 batch 整體仍回 200,該筆結果帶 `status: error` 與 `code: TENANT_DECLARED_ELSEWHERE`。回應不含宣告它的那個檔名。這個判定每次寫入都掃描整棵 conf.d,掃描有時限;掃描失敗或逾時時不寫檔,`PUT` 等端點回 500,直寫模式的 batch 整體仍回 200、該筆 `code: INTERNAL_ERROR`。若是 conf.d 裡有讀不完的檔(例如名為 `*.yaml` 的 FIFO),刪掉那個檔不會解除,要重啟 tenant-api。子目錄檔在 ConfigMap 部署本就不生效——[扁平組裝](../../docs/integration/gitops-deployment.md#3-configmap-assembly)會丟掉子目錄檔並 WARN。`/effective` 遞迴掃描整棵樹,找得到這些租戶;同一 id 被兩個以上的檔宣告時回 409 `CONFLICT`。訊息依 walker 回報的**前兩個**宣告檔決定:兩個都是頂層的 `<id>.yaml`／`<id>.yml` 時帶這兩個基底檔名,否則(子目錄檔——含子目錄內兩種拼法並存——或共用檔的 `tenants:`)是固定文字、不含檔名;完整路徑只進 server log(#2511)。有第三份以上的宣告時,訊息可能只列兩個拼法、不提其餘的檔,也可能因走訪順序而是固定文字。`/effective` 的其他內部錯誤(例如 conf.d 根目錄不存在)回 500 固定訊息、不帶路徑;只有某個檔解碼失敗時保留解碼器原文(`parse defaults[i]: …`／`parse tenant: …`,不含路徑)。list 只列 `<id>.yaml` 租戶;子目錄檔解析失敗只出現在 `parse_failed_files`。
### Custom Alerts(租戶自助告警)

租戶從平台提供的**參數化 recipe** 產生告警(免寫 PromQL)。**人類請用 Tenant Manager portal 的 RecipeBuilder**(選 recipe、填參數、一鍵 commit);以下端點供**自動化 / 整合**直接呼叫。

| Method | Path | 權限 | 說明 |
|--------|------|------|------|
| `GET` | `/api/v1/tenants/{id}/metrics` | read | Metric 探索:回傳該租戶近期出現的 metric 名稱,供 RecipeBuilder 選取(伺服端強制鎖該租戶 label) |
| `PUT` | `/api/v1/tenants/{id}/custom-alerts` | write | 寫入該租戶的 custom-alert recipe 集合(驗證後 commit / PR 回 GitOps) |

### 群組 / View

| Method | Path | 權限 | 說明 |
|--------|------|------|------|
| `GET` | `/api/v1/groups` | read | 列出群組(自動隱藏成員全不可讀的群組) |
| `GET` | `/api/v1/groups/{id}` | read | 取得群組 |
| `PUT` | `/api/v1/groups/{id}` | write + 逐成員 write | 寫入;對所有 `members` 都需 write,否則回 403 + 不足清單 |
| `DELETE` | `/api/v1/groups/{id}` | write + 逐成員 write | 刪除(同上權限) |
| `POST` | `/api/v1/groups/{id}/batch` | read + 逐成員 write | 對群組全成員部分合併 patch(只改指定 key、保留其餘;同步 / 非同步);`unset` 與 `POST /tenants/batch` 同義。每個成員展開成一筆 op、走與 `POST /tenants/batch` 同一條管線:patch 值檢查(違規 400)、逐成員 RBAC + domain policy;PR 模式整組合成**一支** PR(`status: pending_review`),不直接 commit 到 base branch(#2339) |
| `GET` | `/api/v1/views` | read | 列出 saved view |
| `GET` `PUT` `DELETE` | `/api/v1/views/{id}` | read / write | Saved view CRUD |

### Async / 事件

| Method | Path | 權限 | 說明 |
|--------|------|------|------|
| `GET` | `/api/v1/tasks/{id}` | read | Async task polling;結果以呼叫者 RBAC 過濾,全不可讀回 403 |
| `GET` | `/api/v1/prs` | read | Pending PR / MR 列表;不可讀的租戶自動隱藏,`?tenant=<id>` 不可讀回空陣列 |
| `GET` | `/api/v1/events` | read | SSE 即時事件流(`config_change`) |

### 聯邦(Federation)

讓租戶安全拉取**自己的** metrics 子集:平台維護白名單與每租戶子集,並簽發短效 token 供租戶向 read-path proxy 取數。

| Method | Path | 權限 | 說明 |
|--------|------|------|------|
| `GET` | `/api/v1/federation/policy` | admin | 取得平台聯邦白名單 |
| `PUT` | `/api/v1/federation/policy` | admin | 更新白名單(新增 metric 會跑資料層 admission 檢查;軟性警告需 `force=true` + 理由才放行,並記入 commit) |
| `POST` | `/api/v1/federation/tokens` | admin(對 body 的租戶) | 簽發短效 token(預設 4h);token 本體只在回應出現一次 |
| `GET` | `/api/v1/federation/tokens?tenant_id=<id>` | admin(對該租戶) | 列出該租戶未過期的 token 記錄(不含 token 本體) |
| `DELETE` | `/api/v1/federation/tokens/{id}` | admin(對該 token 的租戶) | 撤銷 token;最終一致,約 1–2 分鐘內隨設定同步生效 |
| `POST` | `/api/v1/federation/accounts/backfill` | admin(平台層級) | 替 conf.d 裡還沒有 `account_id` 的租戶一次配發(單一 commit);冪等,重跑不會配新號。回應列出這次配到號的租戶與已有號的租戶數。用途見 [tenant-log-query §2.1](../../docs/integration/tenant-log-query.md) |
| `GET` | `/api/v1/tenants/{id}/federation` | read | 取得該租戶的聯邦 metric 子集;`_federation/<id>.yaml` / `.yml` 兩種拼法皆可解析,無檔回空子集,同一 id 兩種拼法並存回 409 |
| `PUT` | `/api/v1/tenants/{id}/federation` | admin | 更新該租戶的聯邦 metric 子集(需該租戶 admin;子集不得超出平台白名單);寫回既有檔,全新子集才用 `.yaml`,同一 id 兩種拼法並存回 409 |

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
| `tenant_api_writes_total` | Counter | 恆有 | 寫入次數(git commit) |
| `tenant_api_rate_limit_rejections_total` | Counter | 恆有 | 被逐呼叫者限流擋下的請求數;限流關閉時恆 0 |
| `tenant_api_rate_limit_active_callers` | Gauge | 恆有 | 滾動視窗內仍有請求的呼叫者數(背景 sweeper 控管記憶體) |
| `tenant_api_federation_orphaned_tokens` | Gauge | 恆有 | 租戶已不在 conf.d、卻仍有效的聯邦 token 記錄數;非 0 照 [租戶下架 runbook](../../docs/internal/tenant-offboarding-runbook.md) 處理 |
| `tenant_api_federation_orphaned_subset_files` | Gauge | 恆有 | 已不在 conf.d、仍留有 `conf.d/_federation/` subset 檔的租戶數(名稱寫 files,實際按租戶計) |
| `tenant_api_identity_audit_total{result}` | Counter | 恆有 | 機器身分(KSA／TokenReview)稽核結果,只記錄、不參與授權;未開 `--machine-identity-audit` 時全為 0。值:`mismatch` / `no_token` / `unknown_issuer` / `unknown_workload` / `verified` / `verify_failed` |
| `tenant_api_scope_would_deny_total{axis}` | Counter | 恆有 | shadow 模式放行、但切 enforce 後會被拒的次數;`increase()` 在觀察期內維持 0 才切對應的 `--rbac-*-scope-enforce`。值:`metadata` / `metadata_write` / `org` / `org_write` |
| `tenant_api_config_reload_failures_total{component}` | Counter | 恆有 | 該設定重載解析失敗次數(失敗時沿用上一份正確設定)。值:`RBAC` / `federation-policy` / `groups` / `policy` / `tenantorg` / `views` |
| `tenant_api_config_last_reload_successful{component}` | Gauge | 恆有 | 該設定最後一次重載是否成功;0 = 目前正用舊設定。值:`RBAC` / `federation-policy` / `groups` / `policy` / `tenantorg` / `views` |
| `tenant_api_policy_available` | Gauge | 恆有 | 1 = 每個存在的 domain policy 檔都讀得了、或沿用它自己上一份讀得了的內容;0 = 有檔存在卻讀不了且沒有上一份,直寫模式下受 policy 判定的寫入回 503 `POLICY_UNAVAILABLE`(告警 `TenantApiPolicyUnavailable`) |
| `tenant_api_policy_unavailable_open_total` | Counter | 恆有 | `--policy-unavailable-open` 開著時,在 policy 不可用下被放行的寫入數;一次寫入 = 一個 `PUT /tenants/{id}` 或 batch(tenant / group)中一個受 policy 判定的 op,batch 請求本身不另計;應維持 0 |
| `tenant_api_dev_auth_bypass_active` | Gauge | 恆有 | 1 = `--dev-bypass-auth` 開著(僅限本機開發,正式環境須為 0) |
| `tenant_api_sse_clients` | Gauge | 恆有 | 目前連線中的 SSE(`/api/v1/events`)客戶端數 |
| `tenant_api_human_socket_up` | Gauge | 設了 `--human-socket` | 1 = human-plane Unix socket 回應了 readiness 自我探測 |
| `tenant_api_forge_circuit_state{provider}` | Gauge | PR／MR 寫回模式 | forge 斷路器狀態:0 = closed、1 = half-open、2 = open(forge 故障,寫入回 503) |
| `tenant_api_forge_pr_conflicts{provider}` | Gauge | PR／MR 寫回模式,tracker 同步過一次後 | 上次同步時處於 merge conflict 的 PR／MR 數 |

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
| `TA_RBAC_PATH` | (空) | `_rbac.yaml` 路徑;空 = open-read |
| `TA_ADDR` | `:8080` | HTTP listen address |
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
| `TA_WRITE_MODE` | `direct` | `direct` / `pr` / `pr-github` / `pr-gitlab`。⛔ 前後空白先 trim,trim 後須逐字等於這四個之一;大小寫敏感,不符者(含 `DIRECT`、打錯字、`--write-mode=` 空值)一律**拒絕啟動**,不會退回 `direct`([ADR-034](../../docs/adr/034-legal-value-as-fallback.md)) |
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
| `TA_POLICY_UNAVAILABLE_OPEN` | `false` | 直寫模式下 domain policy 檔存在卻讀不了(且沒有上一份)時,仍放行受 policy 判定的寫入,不回 503;每放行一次寫入(一個 PUT 或 batch 中的一個 op)記 log 並計入 `tenant_api_policy_unavailable_open_total`(預設 fail closed) |
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

支援以環境 / 域 metadata 做進一步過濾(細節見 `internal/rbac/` 註解)。**沒標記該 metadata 的租戶**在受限規則下預設**仍放行**(SHADOW,行為與過去一致),但每次會計入 `tenant_api_scope_would_deny_total{axis="metadata"}`(**單調遞增 counter**、僅重啟歸零)。待 `increase(tenant_api_scope_would_deny_total{axis="metadata"}[soak窗])` 在整個 soak 窗維持 **0**(看增長率非絕對值),並**掃過確認沒有落單租戶**(counter 是 traffic-driven:沒被 list 到的租戶不會觸發)後,設 `--rbac-metadata-scope-enforce`(或 `TA_RBAC_METADATA_SCOPE_ENFORCE=1`;helm `--set rbac.metadataScopeEnforce=true`)切成**沒標記就拒絕**(fail-closed,ADR-027 / LD-6 P1)。

⚠️ 上述 `environments[]` / `domains[]` 在 #1597 之前**只約束 list 可見性**——一條 `environments: [dev]` 的規則,可以 PUT 一個它在清單裡根本看不到的 prod 租戶。#1597 把**同一條軸接上寫入平面**,並且是**兩處**判定:寫入前的租戶現況(pre-state),以及 PUT body 提議的 `_metadata`(post-state)——後者擋的是「一次 PUT 就把租戶改標到自己 scope 內」這條繞路。

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
| `direct` | 直接 `git commit` | dev、單人操作 |
| `pr` / `pr-github` | 建 feature branch + GitHub PR | GitHub.com / Enterprise |
| `pr-gitlab` | 建 feature branch + GitLab MR | GitLab.com / 自託管 |

PR 模式啟動時會驗證 token + 連線;失敗只印 WARN(後續開 PR 會回 503)。範例:

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

Chart 會建立:Deployment + oauth2-proxy sidecar、Service、RBAC ConfigMap、NetworkPolicy、PDB。

對外入口：設 `ingress.enabled=true` 與 `ingress.hosts` 會多建一個 Ingress（預設關閉，#2027）。後端固定是 Service 的 `http` port（→ oauth2-proxy :4180），**不可設定**——internal port 8080 信任注入的身分 header，永遠不經 Ingress 對外（GHSA-3g2h-rf85-5rrv）；`oauth2Proxy.enabled=false` 時 render 直接失敗。

本機 Docker(從 repo root build,因 go.mod 需 threshold-exporter 模組):

```bash
docker build -t tenant-api -f components/tenant-api/Dockerfile .
docker run -p 8080:8080 -v "$(pwd)/conf.d:/conf.d" tenant-api
# 或直接拉 published image（<version> 見 Releases / CHANGELOG）：
#   docker run -p 8080:8080 -v "$(pwd)/conf.d:/conf.d" ghcr.io/vencil/tenant-api:<version>
```

Smoke test:

```bash
curl -s localhost:8080/health
curl -s localhost:8080/metrics
```

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
