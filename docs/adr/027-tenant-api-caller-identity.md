---
title: "ADR-027: tenant-api 的呼叫端身分只在自己的 pod 內建立，網路上不轉送人類身分"
tags: [adr, security, tenant-api, identity, rbac, GHSA-3g2h-rf85-5rrv]
audience: [platform-engineers, contributors]
version: v2.9.0
lang: zh
id: ADR-027
tracking_kind: adr
status: accepted
domain: tenant-api
created_at: 2026-07-03
updated_at: 2026-11-13
---

# ADR-027: tenant-api 的呼叫端身分只在自己的 pod 內建立，網路上不轉送人類身分

> **Language / 語言：** **中文 (Current)** | [English](./027-tenant-api-caller-identity.en.md)

## 狀態

✅ **Accepted**（2026-11-13，owner 核可）。§9 的每個子句都有對應的檢查，見「實作位置」。全文於 2026-11-04 改寫，同日 owner 裁決瀏覽器入口、threshold-govern 與範圍軸 shadow 模式三項。

2026-11-05 修訂：§10 改為實測結果，採用 oauth2-proxy 的結構化設定檔（alpha config）；tenant-api 由同一份設定檔決定信任哪些 header（§2、§7）；§6 的「一次生效」限於會拒絕舊設定的變更。

2026-11-07 修訂：§7 的離線檢查也列出標記讀不到、卻被有範圍限制的規則套到的租戶，沒有限制規則套到的壞檔不再影響結束碼；環境與 domain 標記改由不受任何範圍限制的平台管理員變更，只限部分環境或 domain 的管理員不再能改；標記讀不到的租戶檔也只由這種管理員經 API 覆寫；標記與寫入範圍改在寫入鎖內、對寫入實際落地的那棵樹判定，兩種寫入模式相同。

2026-11-13 修訂：§9 的每個子句有固定編號，§實作位置 改由原始碼中的 `adr-check:` 標記產生，沒有檢查的子句自動列出；§5 與 §9 第 4 項的「simulate 回原 body」改為如實的「`code` 與 `error` 文字不變」，因 tenant-api 的錯誤回應另帶 `request_id`。

引用檢查（§9 第 8 項）已上線：repo 內以草稿編號引用本 ADR 的地方已改寫，舊編號對照表刪除。

本 ADR 取代 2026-07 一份未公開的草稿。那份草稿的方向是「逐跳認證」：每個把身分轉給 tenant-api 的元件各自加上 workload token，先只做稽核、觀察數週後再強制。依那個方向合併的程式碼不是只做稽核、就是預設關閉，而且都不在任何已發布的 tenant-api image 裡。本 ADR 撤回那個方向，理由見 §背景 與 §考慮過但否決；那段期間完成、與方向無關的修正（`_rbac.yaml` 空檔拒絕存取、移除以 header 決定來源 IP 的中介層）保留。

實作與發布追蹤在 [#1040](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1040)。

## 摘要

**問題**：tenant-api 用 `X-Forwarded-Email` / `X-Forwarded-Groups` 兩個 header 決定呼叫者是誰，而這兩個 header 可以從網路上送進來。能連到 tenant-api 的 TCP API 埠、或連到會把這兩個 header 轉給它的元件，就能主張別人的身分，包括平台管理員。今天唯一擋住這件事的是 NetworkPolicy；CNI 不執行 NetworkPolicy 時就沒有防線。這是 [GHSA-3g2h-rf85-5rrv](https://github.com/vencil/Dynamic-Alerting-Integrations/security/advisories/GHSA-3g2h-rf85-5rrv) 的根因；先前發布的修正只做了 L4 止血。

**決定**：

1. 呼叫端身分只在 tenant-api 自己的 pod 內建立，來源只有同一個 pod 裡的 oauth2-proxy，經 Unix domain socket 進來（§1）。
2. 用哪一種方式取得身分，由連線進來的 listener 決定，不由 request 內容決定（§2）。
3. TCP 只提供 metrics 與健康檢查，不提供 API（§3）。
4. 目前沒有機器呼叫端；出現時的做法與觸發條件先訂好（§4）。
5. 任何元件都不再把人類身分跨網路轉送。整個部署只有一個 oauth2-proxy；portal 與 recipe-preview 移進 tenant-api 的 pod（§5）。
6. 拒絕舊設定的變更在同一個 breaking release 生效，不設觀察期與開關（§6）。
7. 範圍軸預設強制、沒有 shadow 模式，升級前以離線檢查列出受影響的租戶（§7）。
8. 這些決定以測試與 lint 表達，本文只指向它們（§9）。

## 背景

### 身分怎麼建立

tenant-api 的 RBAC、`/me`、federation token 簽發、git 寫入的 author，吃的是同一個身分物件，由 header 解析而來：`X-Forwarded-Email` / `X-Forwarded-Groups`，以及設定的 claim header。另有 rate limit 與 request log 直接讀 `X-Forwarded-Email`，不經這個物件。header 的內容沒有簽章，tenant-api 無法自行驗證；它可信的前提只有一個：送 header 的一定是負責登入的 oauth2-proxy。

### 這個前提今天不成立

| 進入 tenant-api 的路徑 | 帶的身分 | 誰保證它是真的 |
|---|---|---|
| 瀏覽器 → da-portal nginx → tenant-api TCP | nginx 轉送的 `X-Forwarded-*` | 只有 NetworkPolicy |
| 瀏覽器 → da-portal → recipe-preview → tenant-api TCP（權限探測） | recipe-preview 轉送的使用者 header | 只有 NetworkPolicy |
| threshold-govern CronJob → tenant-api TCP | 自己宣告的 group | 只有 NetworkPolicy |
| 同 pod oauth2-proxy → tenant-api TCP（UDS 未啟用時，現行預設） | oauth2-proxy 設定的 header | 無：tenant-api 分不出它和網路上的呼叫者 |
| 同 pod oauth2-proxy → tenant-api UDS（選用） | oauth2-proxy 設定的 header | pod 邊界 |

人類的主要入口是第一列，不是最後一列。2026-07 的草稿以為人類走最後一列，只要把人類流量移到 UDS、並要求其他路徑帶 token 即可；但第一、二列轉送的是**使用者**身分，帶 token 只能證明「轉送的元件是真的」，證明不了「它轉送的使用者是真的」。

### 為什麼撤回「逐跳認證」

- 每多一個轉送身分的元件，就多一組必須同時設對的設定：token audience、namespace、Pod label、NetworkPolicy、image 版本、開關。2026-11 盤點時，維持安全需要多組設定一起設對，其中數組只靠人工同步。
- 稽核階段只能證明「合法呼叫端都帶了 token」，證明不了沒有人偽造；偽造者只要讓 token 與 header 一致即可。
- 依該方向合併了多個 PR，強制那一步的程式碼一行都沒有；而它要觀察的機器呼叫端只有一個（threshold-govern），在已發布的 image 上從未能執行。
- 前提錯（人類走哪條路）加上配對一直增加，代表該換的是方向，不是再補一層。

## 決定

### 1. 身分只在 tenant-api 的 pod 內建立

tenant-api 唯一接受的身分來源，是同一個 pod 裡的 oauth2-proxy 完成登入後、經 pod 內 Unix domain socket（UDS）送進來的 header。不在 pod 內的元件，不論是否也在 oauth2-proxy 後面，都不能替使用者主張身分。目前沒有機器身分（§4）。

**取捨**：oauth2-proxy 成為唯一的人類身分驗證者，它被攻陷就等於身分被攻陷；換到的是「身分可信」不再取決於叢集的網路設定。

### 2. listener 決定身分來源

- UDS listener：從 header 建立身分。
- TCP listener：沒有身分；只服務 §3 的端點。TCP request 帶了任何身分 header（`X-Forwarded-Email`、`X-Forwarded-Groups`、oauth2-proxy 設定檔中注入的 claim header）一律回 400，讓仍在轉送身分的舊設定立即失敗，而不是被默默忽略。
- 連線來自哪個 listener，由伺服器在建立連線時標記，request 無法改寫。
- 所有需要呼叫者身分的地方（含 rate limit 與 request log）都讀身分物件，不直接讀 header。
- 開發模式的驗證旁路改為直接注入身分物件，不再注入 header。
- UDS 不再是選項：tenant-api 啟動時沒有 UDS 就不啟動。pod 內只有 tenant-api 與 oauth2-proxy 兩個容器能掛載這個 socket。
- 信任哪些 header，由 oauth2-proxy 實際使用的設定檔決定，不另設名單：同一份設定檔掛進兩個容器，tenant-api 啟動時讀其中的 `injectRequestHeaders`，得知 email、group 與各 claim 由哪個 header 帶來。oauth2-proxy 對每個注入的 header 預設先清除 client 帶來的同名值。
- tenant-api 只接受意義不含糊的設定檔，其餘一律拒絕啟動：
  - 每個身分 header 恰好取自一個 session claim（email 取 `email`、group 取 `groups`，其餘為允許的額外 claim），不加前綴、不轉成 basic auth、不用固定值（secret），不保留 client 值（`preserveRequestValue`），不關閉 header 名稱正規化；沒有注入 email 與 group 也拒絕。
  - oauth2-proxy 會先展開設定檔裡的環境變數參照、且以不分大小寫比對欄位名稱；tenant-api 的解讀必須與它相同，所以設定檔含環境變數參照、或欄位名稱不是標準拼法時，一律拒絕。
- 免登入路由不得涵蓋 `/api/`：符合免登入條件的請求（`--skip-auth-route` 一類旗標、信任的來源 IP、preflight 放行）若帶著有效 session，oauth2-proxy 仍會注入身分 header，而且略過 email 網域與 group 的限制。這類旗標不在設定檔裡，由 chart 的 render 測試守住（§9 第 3 項）。

**取捨**：多一個必須存在的 socket 與共享 volume，且 tenant-api 要解析 oauth2-proxy 設定檔中與 header 注入有關的那一小段；換到的是「哪裡可以主張人類身分」變成拓撲事實，「proxy 注入什麼」與「tenant-api 信什麼」在每一種部署方式下都是同一份檔案。

### 3. TCP 平面

- TCP 只註冊根路徑的 metrics、存活與就緒檢查，精確比對路徑（API 底下同名的路徑不算）；其餘一律 404。API 不在 TCP 上。
- 驗證旁路沿用 [ADR-022](./022-dev-auth-bypass-four-layer-containment.md) 的四層隔離，且只能作用在 UDS 平面。
- 本機試用、契約測試與其他開發環境沿用同一拓撲：pod 內的 oauth2-proxy 以全路徑免登入執行，身分由 UDS 平面的驗證旁路注入。不提供跳過 oauth2-proxy 的部署方式。

### 4. 機器呼叫端：目前沒有

- 唯一曾規劃的機器呼叫端是 threshold-govern 的寫入路徑（`--apply`），它從未在任何已發布的 image 裡執行過。本 ADR 移除它的 CronJob、寫入路徑與相關告警；只讀、不經 tenant-api 的建議（dry-run）保留。
- **觸發條件**：出現第一個需要呼叫 tenant-api 的機器呼叫端，且它的 image 通過 image-pin capability 檢查（確實能執行）。
- **屆時的做法**（已選定，觸發前不實作）：TCP 上加 API 路由，只接受 audience 為 tenant-api 的 ServiceAccount token，以 `k8s.io/apiserver` 現成的 TokenReview 驗證器驗證；只採用 username、丟棄 TokenReview 附帶的 group；權限寫在 `_rbac.yaml` 的機器 subject 欄位，以完整 username 精確比對，人類 group 不得以 `system:` 開頭。任何 Pod 都能為自己申請這種 token，所以安全性來自「subject 有沒有被授權」，不是「有沒有 token」。
- 觸發前保持現狀的理由：為一個不存在的呼叫端蓋身分平面，會多出 TokenReview 的權限、快取、schema 與檢查，全部沒有消費者。
- **federation token**：簽發、列出、撤銷是租戶管理員的人類操作，經 portal（§5）進行。需要定期自動重新簽發的部署是機器呼叫端，適用上面的觸發條件；在那之前不提供非瀏覽器的簽發方式。

### 5. 不在網路上轉送人類身分

整個部署只有一個 oauth2-proxy，位在 tenant-api 的 pod 裡，依路徑分流：

- `/api/` 前綴 → tenant-api 的 UDS。portal 對後端的每一個呼叫都必須在這個前綴下，包括原本的 `/preview`。
- 其餘路徑 → 同一個 pod 裡的 portal 靜態檔容器。這個容器不讀身分 header，也不把請求轉送到任何地方。
- `/api/` 下未登入或 session 過期的請求回 401，不轉址到 IdP；portal 收到 401 時導去登入，不改顯示示範資料。
- 原本由 da-portal nginx 回的「simulate 未提供」501，改由 tenant-api 回，狀態碼、`code` 與 `error` 文字不變（tenant-api 的錯誤回應另帶 `request_id`）。

連帶的變更：

- **da-portal**：不再有自己的 oauth2-proxy、Ingress 與反向代理。da-portal chart 只保留不經登入的純靜態部署（Tier-1）。
- **recipe-preview**：移進 tenant-api 的 pod，只經 pod 內的 UDS 提供純計算，比照 [ADR-036](./036-single-parser-effective-config.md) 的 generator。瀏覽器呼叫 tenant-api，tenant-api 依呼叫者身分授權後再呼叫 recipe-preview；recipe-preview 不接收、不轉送任何身分，也不再有網路埠。
- 「relay」這個概念整個移除：不再有 relay 清單，也不再有「已驗證的轉送者」這種身分類別。
- 部署只剩一個 Ingress、一個 redirect URL、一個 session cookie；兩個 chart 之間不再有需要人工同步的登入設定。

**取捨**：tenant-api 的 pod 多兩個容器、共用資源，portal 的 image 版本要釘在 tenant-api chart 裡；換到的是使用者身分永遠只經過 oauth2-proxy → UDS 這一跳，且「誰能登入」只有一個判定點。

### 6. 一次生效，不設觀察期

- 會拒絕舊設定的變更（TCP 不再提供 API、刪除轉送與開關、範圍軸強制）在同一個 breaking release 生效，所有受影響的元件一起發布。發布說明寫明升級後被拒絕的舊設定長什麼樣子。
- 新拓撲中純新增、不拒絕任何舊設定的部分（例如 tenant-api 的 pod 加入 portal 容器）可以先合併；期間若有其他原因發布，這些部分會先出現在該版本，不使任何現有部署變得較不安全。
- 稽核、轉送、「UDS 是否啟用」、「是否啟用 oauth2-proxy」、範圍軸 shadow、open mode 相關的開關全部刪除。chart 的 key 由 render 測試斷言不存在；tenant-api 的環境變數從已知清單移除，舊設定仍帶著它們時，既有的「未知 `TA_*` 變數拒絕啟動」機制會讓 tenant-api 不啟動（§9）。
- 理由：採用者極少；稽核與 shadow 階段證明不了沒有偽造或沒有人受影響；而這些開關都不在任何已發布的 image 裡，觀察期實際上從未開始。

### 7. 授權範圍（scope）模型

角色來自 IdP group，範圍（哪些租戶、環境、domain、組織）來自已驗證的身分屬性。

- **哪些屬性可以當授權依據**：只有 oauth2-proxy 從簽章 token 取出、使用者改不了的 claim。claim header 只在 UDS 上讀取，且只認 oauth2-proxy 設定檔中注入的那些（§2）；tenant-api 沒有獨立的 claim header 設定。oauth2-proxy 只有 OIDC 類的 provider（含 GitLab）會取出額外 claim，GitHub provider 不會，所以 GitHub 部署只能用 group 決定範圍。
- **值要帶來源命名空間**，避免不同來源的同名值互相冒用。
- **決定範圍的資料由平台控管**：組織與租戶的對應寫在平台管理員才能改的設定層。環境與 domain 標記目前寫在各租戶設定檔的 `_metadata` 裡，可經寫入 API 修改；改為只有不受任何範圍限制的平台管理員能變更這些標記，也就是有一條規則對 `tenants: ["*"]` 授予 admin，且不帶組織、環境或 domain 範圍；其他寫入若變更它們即拒絕。標記決定哪些規則涵蓋這個租戶，改標記等於改授權結構，效果與修改 `_rbac.yaml` 相同，而 API 不提供修改 `_rbac.yaml` 的途徑。只限部分環境的管理員若能改標記，就能把租戶在自己管的環境之間搬動，讓目的環境的每條規則都看得到它；寫入前後的範圍檢查擋不住這種搬動，因為兩個狀態都在它的範圍內。標記讀不到的租戶檔（無法解析，或 `_metadata` 解不開），整檔覆寫無法證明它保留了原本的標記，例如不帶標記的內容可能正把原標記刪掉，所以整檔覆寫這種檔也只限這種管理員；壞檔由這種管理員或經 git 修復。只改其他欄位的部分寫入（不動範圍欄位的 batch 操作、custom alerts）照原樣保留 `_metadata`，不受此限。本身是無法解析的 symlink、或能解析但沒有宣告這個租戶的租戶檔，沒有任何標記可保留，視同尚無檔案。寫入範圍與標記都在寫入鎖內、對寫入實際落地的那棵樹再判定一次（直接寫入模式是服務中的設定樹，PR 模式是分支切出的最新 base）：在鎖外的判定之後、取得鎖之前，排在前面的寫入可能已經改了這個租戶檔，鎖外的判定只能提早拒絕。
- **一律強制，沒有 shadow 模式**：讀取範圍、寫入範圍、組織範圍各自獨立判定，沒有「只記錄不擋」的選項。未標記的租戶在有範圍限制的規則下不可見、不可寫。
- **升級前以離線檢查列出受影響的租戶**：擴充 `tenant-api config check`（現在只檢查單一檔案能否解析），讓它同時讀取 `_rbac.yaml`、組織對應與租戶設定樹，對每條範圍軸列出「有範圍限制的規則 × 該規則因此不授予的租戶」：未標記的租戶要補標記；標記讀不到的租戶（租戶檔壞掉）在任何限制環境或 domain 的規則下都不會出現在租戶清單裡，要修租戶檔。查到就非零退出；不被任何限制規則套到的壞檔不影響結束碼，它的授權不因範圍軸而改變。它只讀設定，結果固定、不依賴流量；升級前用新版 image 跑，CI 也跑。
- **沒有 open mode**：未設定 `_rbac.yaml`、或檔內沒有任何規則時，拒絕所有存取；「已驗證即可讀取」的模式與恢復它的開關刪除。
- 逆向查詢保留：輸入租戶，列出哪些規則能碰它，供稽核使用。

GitLab group 以 full path（`org/subteam`）原樣比對，不加 provider 前綴；同一個部署要接第二個 IdP 時再引入前綴（觸發條件見 §考慮過但否決）。

### 8. 仍然保留的縱深防禦

- NetworkPolicy 保留，但不再是唯一控制；本 ADR 的安全性不依賴 CNI 是否執行它。
- oauth2-proxy 在登入時取得 group 並存在 session 裡；使用者被移出 group 後，權限要到 session 重新整理或到期才更新（各 provider 在重新整理時是否重抓 group 不同），以縮短 session 時效緩解。

### 9. 決定以測試與 lint 表達

本文是說明；下列檢查才是這些決定的單一來源。每個子句有固定編號（例如 `3b`）。實作某個子句的測試或 CI 步驟，在原始碼上一行標記 `adr-check: ADR-027/9.3b`；§實作位置 由這些標記產生，沒有任何標記的子句列為尚無檢查。

1. TCP 平面（§2、§3）：
   - **1a** TCP 只註冊 §3 列舉的路徑（精確比對）。
   - **1b** 其餘路徑回 404。
   - **1c** TCP 上帶任何身分 header 回 400。
2. 身分只在 UDS 平面建立，以架構測試表達（§2）：
   - **2a** 從 header 建立身分只發生在 UDS listener 的路徑上。
   - **2b** rate limit、request log 與驗證旁路都不讀身分 header。
3. chart render（§2、§5）：
   - **3a** 整個部署只有一個 oauth2-proxy。
   - **3b** `/api/` 未登入回 401。
   - **3c** 只有 tenant-api 與 oauth2-proxy 掛載 human socket。
   - **3d** UDS 必定存在。
   - **3e** oauth2-proxy 與 tenant-api 掛載同一份設定檔。
   - **3f** 正式部署的 oauth2-proxy 沒有任何涵蓋 `/api/` 的免登入條件（§3 的開發拓撲依 ADR-022 隔離，不在此限）。
   - **3g** portal 容器實際載入的 nginx 設定（不論來自 chart 或 image）沒有任何轉送。
4. portal（§5）：
   - **4a** portal 原始碼呼叫的每個後端路徑都在 `/api/` 前綴下。
   - **4b** simulate 路徑回 501，`code` 與 `error` 文字與原本相同。
5. 不轉送身分、刪除的設定不存在（§4、§5、§6）：
   - **5a** 掃描 helm、k8s manifest 與 rule-pack：沒有任何設定把身分 header 跨網路轉送給任何元件。
   - **5b** 同一掃描中，§6 刪除的 chart key 不存在。
   - **5c** 同一掃描中，threshold-govern 的 CronJob、它在 NetworkPolicy 的放行與它的告警都不存在。
   - **5d** §6 刪除的環境變數不在 tenant-api 的已知清單裡。
6. 授權範圍（§7）：
   - **6a** oauth2-proxy 設定檔不符合 §2 的形狀時，tenant-api 拒絕啟動。
   - **6b** 解析時拒絕不符的形狀，fixture 至少涵蓋：保留 client 值、固定值、前綴、claim 對錯、缺 email 或 group、環境變數參照、非標準拼法的欄位名稱。
   - **6c** 沒有規則的 `_rbac.yaml` 拒絕所有存取。
   - **6d** 不存在恢復 open mode 的開關。
   - **6e** 非平台管理員的寫入若變更租戶的環境或 domain 標記、或覆寫標記讀不到的租戶檔，即被拒絕。
   - **6f** 6e 的判定與寫入範圍都在寫入鎖內、對寫入落地的那棵樹進行。
7. 離線檢查（§7）：
   - **7a** `tenant-api config check` 對每條範圍軸列出未標記與標記讀不到的租戶並非零退出，以固定 fixture 測試。
   - **7b** 它與 server 租戶清單的判定逐條規則一致。
   - **7c** CI 對 repo 內的範例設定執行它。
8. 引用檢查（本 ADR 與 [#1667](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1667)）：
   - **8a** repo 內寫成 `ADR-NNN §小節` 的引用（同一行緊接著列出的其他小節也算）都必須對應到該 ADR 存在的小節（以小節標題的編號或名稱比對）。
   - **8b** `ADR-027` 後接 2026-07 草稿的小節編號（`D2-B`、`LD-6 P4b`、`MED-8` 之類，完整清單在檢查本身）一律失敗。
9. oauth2-proxy 設定檢查（§10）：
   - **9a** CI 以 chart 釘住的 oauth2-proxy image（同一個 digest）對 chart 產生的設定檔執行設定檢查（`--config-test`）。
   - **9b** CI 以同版本的 oauth2-proxy 程式庫斷言分流結果與 §10 的範例一致。

### 10. oauth2-proxy 的分流設定

已以 chart 釘住的 oauth2-proxy 版本（v7.15.5）實測：

- **legacy 旗標做不到**：`--upstream=unix://…` 的路由路徑固定為 `/`，API 無法放在 `/api/`；portal 也在 `/`，兩者衝突，設定驗證失敗（`multiple upstreams found with path "/"`）。
- **結構化設定檔（`--alpha-config`）做得到**：每條 upstream 的路徑與目的地分開設定。

設定（節錄）：

```yaml
upstreamConfig:
  upstreams:
  - id: api
    path: /api/
    uri: unix:///run/ta-human/human.sock
  - id: portal
    path: /
    uri: http://127.0.0.1:8081
```

分流結果：

| 請求路徑 | 送到 |
|---|---|
| `/api/v1/me?x=1` | UDS，路徑與 query 原樣 |
| `/api/` | UDS |
| `/api`、`/apix` | portal |
| `/`、`/index.html` | portal |

改用結構化設定檔後，provider、header 注入與 listener 位址都寫在設定檔裡，對應的 legacy 旗標不能再並用（帶了 oauth2-proxy 就無法解析旗標而拒絕啟動）；session、cookie、`--api-route`（未登入回 401 而非轉址）等其他旗標照舊。oauth2-proxy 專案標示這個檔案的結構可能在 minor 版之間變動；檔案以嚴格模式解析，出現不認得的欄位時 oauth2-proxy 拒絕啟動，所以結構變動會在升級時立即失敗，而不是被默默忽略。§9 第 9 項讓這件事在 CI 就失敗。

退路（不採用，保留紀錄）：由 Ingress 依路徑把 `/api/` 導到 tenant-api 的 oauth2-proxy、portal 不經登入。§7 的 claim header 注入同樣需要結構化設定檔，退路並不能免除它。

## 考慮過但否決

- **逐跳認證＋稽核後強制（2026-07 草稿）**：見 §背景。轉送使用者身分的元件，帶 token 只證明元件本身；設定配對隨元件數增加。
- **對外界面不變，只把稽核切成強制**：仍然信任 da-portal 與 recipe-preview 轉送的使用者身分，問題從「任何能連到 tenant-api 的 Pod」縮小成「任何能連到 da-portal 內部埠的 Pod」，沒有消除。
- **da-portal 把瀏覽器的 session cookie 轉給 tenant-api 的 oauth2-proxy（兩個 proxy 共用 session）**：仍是在網路上轉送人類憑證；cookie secret 分在兩個 namespace，無法機械檢查一致；「誰能登入」有兩個判定點。
- **Ingress 依路徑分流、portal 不經登入（作為主方案）**：Ingress 不能跨 namespace 指向 Service，同一個 host 要靠兩個 Ingress 由 controller 合併；host、redirect URL、TLS 要跨 chart 人工配對。保留為 §10 的退路。
- **pod 內 portal nginx 在前、把 `/api/` 轉給同 pod 的 oauth2-proxy（以避開結構化設定檔）**：portal 容器重新出現轉送，§9 第 3 項失去意義；§7 的 claim header 仍需結構化設定檔，兩頭的代價都要付。
- **chart 從同一份 values 同時產生 oauth2-proxy 設定與 tenant-api 的 claim header 設定**：只有 Helm 部署機械一致，本機試用的 compose 要另抄一份。
- **tenant-api 另設旗標，宣告 oauth2-proxy 會覆寫哪些 header**：旗標本身是另一份需要人工同步的設定，設錯時 tenant-api 無從察覺。
- **tenant-api 自己提供 portal 靜態檔**：要在 Go 裡重做 CSP、快取與 SPA fallback，並把 portal 與 tenant-api 的版號綁在一起。
- **修好 threshold-govern 並同時建好機器身分**：見 §4；為唯一一個從未執行過的呼叫端建一整個身分平面。觸發條件見 §4。
- **保留範圍軸的 shadow 模式（預設關閉）**：would-deny 計數只在有流量時才動，證明不了沒有人受影響；租戶是可列舉的，決定性的離線檢查較可靠（Gatekeeper、Kyverno 對可列舉的資源也以背景掃描為準，流量式 dry-run 只用在無法列舉的對象）。
- **以逆向查詢當升級前檢查**：一次只查一個租戶、需要管理員 session，metadata 軸也不評估。
- **recipe-preview 留在原處、改以 TokenReview 驗證 tenant-api**：仍需要網路埠與一組驗證設定；移進 pod 少一組設定。
- **oauth2-proxy 用 `--extra-jwt-issuers` 驗機器 token**：本地驗簽沒有撤銷；ServiceAccount token 的 audience 由申請者自己指定，仍需比對 subject；叢集 issuer 的 JWKS 是否能從 Pod 取得依叢集設定而定（未驗）。§4 觸發時再評估。
- **kube-rbac-proxy sidecar 或以 SubjectAccessReview 授權機器**：機器權限會寫在 Kubernetes RBAC，成為第二套授權來源，逆向查詢看不到。
- **轉送使用者 token（RFC 8693 token exchange、on-behalf-of）**：GitHub 與 GitLab CE 的 access token 是不透明的，沒有可驗證的使用者 token 可轉送；能發 JWT access token 的 OIDC 客戶，直接由 tenant-api 驗證即可（見下方延後項）。
- **以 OIDC ID token 當 API 憑證**：ID token 的 audience 是 OAuth client 而非 API，可跨服務重放；業界指引是不應送給 API（[oauth.net](https://oauth.net/id-tokens-vs-access-tokens/)）。
- **驗證使用者的 JWT access token（原草稿的 D2-A）**：延後。觸發：客戶的 IdP 能發 audience 為 tenant-api、且含 group 的 JWT access token（[RFC 9068](https://datatracker.ietf.org/doc/html/rfc9068)）。屆時以新的身分來源加在 UDS 平面，不改 §1–§5。
- **相信 loopback 來源 IP**：同 pod 的其他容器也是 loopback；以 header 決定來源 IP 的中介層可被偽造。
- **HMAC 簽章 header**：金鑰的分發與輪替要自己做，等於自己實作一個較弱的 ServiceAccount token。
- **SPIFFE/SPIRE、service mesh**：為少數呼叫端維運一整套身分平面。觸發：出現跨叢集或非 Kubernetes 的機器呼叫端、或要求通道綁定的身分。
- **身分代理（Dex / Keycloak）**：Dex 的 access token 不透明，Keycloak 能發可驗證的 access token 但需要資料庫與持續修補。觸發：同一部署需要多個登入來源。
- **GitLab / GitHub group 加 provider 前綴**：單一 provider 時沒有碰撞。觸發：同一部署接第二個 IdP。

## 後果與已知限制

- **Breaking**：
  - 仍經 da-portal 或 recipe-preview 轉送身分、或在 TCP 上呼叫 API 的請求會被拒絕；受影響的元件必須一起升級。
  - 需要登入的 portal 改由 tenant-api 的部署提供；da-portal chart 只剩純靜態部署。
  - threshold-govern 的 CronJob、寫入路徑與告警移除，文件中對應的段落一併刪除。
  - federation token 改經 portal 簽發；對外文件中直接以 curl 打 tenant-api 的範例改寫（那些範例原本就沒有帶任何憑證）。
  - 未設定或沒有規則的 `_rbac.yaml` 不再允許讀取。
  - 範圍軸一律強制：未標記的租戶在有範圍限制的規則下不可見；寫入首次受環境與 domain 範圍約束，已標記但不在範圍內的寫入也會被拒；組織範圍軸是新功能。升級前以 `tenant-api config check` 列出並補齊。
  - 只有不受任何範圍限制的平台管理員能經 API 變更環境與 domain 標記、或覆寫標記讀不到的租戶檔；管理員都帶環境或 domain 限制的部署，標記與這類壞檔只能經 git 修改。
  - 本機試用環境（try-local）與在 TCP 上帶身分 header 的測試，依新拓撲改寫。
- oauth2-proxy 是唯一的人類身分驗證者：版本以 digest 釘住並追蹤其安全公告。
- 結構化設定檔的結構可能隨 oauth2-proxy 的 minor 版變動；升級 oauth2-proxy 時可能需要同步修改 chart 產生的設定檔，tenant-api 解析的那一段也一樣（§10）。
- GitHub provider 不提供額外 claim，該類部署的範圍只能由 group 決定（§7）。
- tenant-api 的 pod 同時承載 API、portal 與 recipe-preview，三者一起擴縮。
- 被移出 group 的使用者，權限在 session 更新前仍有效（§8）。
- GHSA-3g2h-rf85-5rrv 目前列出的修補版本是 L4 止血那一版；本 ADR 的 release 發布後，需更新該 advisory 的修補版本與說明。

## 實作位置

由 `scripts/tools/dx/gen_adr_checks.py` 依原始碼中的 `adr-check:` 標記產生，不要手改；改了標記後執行 `python3 scripts/tools/dx/gen_adr_checks.py --write`。路徑相對於 repo 根目錄，括號內是標記下方的測試或 CI 步驟。

<!-- BEGIN GENERATED: adr-checks section=9 -->
- **1a**：`components/tenant-api/cmd/server/tcp_router_test.go`（`TestTCPRouter_RegistersOnlyTheProbeRoutes`）
- **1b**：`components/tenant-api/cmd/server/tcp_router_test.go`（`TestTCPRouter_EverythingElseIs404`）
- **1c**：`components/tenant-api/cmd/server/tcp_router_test.go`（`TestTCPRouter_IdentityHeaderIs400`）
- **2a**：`components/tenant-api/internal/rbac/devbypass_test.go`（`TestResolveIdentity_IgnoresHeadersOnTCP`）；`components/tenant-api/internal/rbac/identity_arch_test.go`（`TestHeaderResolutionOnlyInResolveIdentity`、`TestIdentityHeaderNamesOnlyInTheirOwners`）
- **2b**：`components/tenant-api/internal/rbac/identity_arch_test.go`（`TestRateLimitLogAndBypassReadNoRequestHeader`）
- **3a**：`tests/helm/test_identity_single_hop.py`（`test_no_raw_manifest_runs_an_oauth2_proxy`、`test_the_only_oauth2_proxy_is_in_the_tenant_api_pod`）
- **3b**：`tests/oauth2-proxy/chart_test.go`（`TestChartAPIRouteIsTheSameInEveryVariant`）；`tests/oauth2-proxy/image_config_test.sh`
- **3c**：`tests/helm/test_human_socket_guard.py`（`test_render_default_points_upstream_at_socket`）；`tests/helm/test_tenant_api_portal_container.py`（`test_portal_container_mounts_no_identity_or_secret`）
- **3d**：`components/tenant-api/cmd/server/tcp_router_test.go`（`TestCheckIdentityPlaneFlags`）；`tests/helm/test_human_socket_guard.py`（`test_deployment_wires_the_socket`、`test_render_default_points_upstream_at_socket`、`test_render_without_proxy_fails`、`test_socket_path_is_a_chart_constant`）
- **3e**：`tests/helm/test_oauth2_proxy_alpha_config.py`（`test_both_containers_read_the_same_file`）
- **3f**：`tests/oauth2-proxy/chart_test.go`（`TestChartConfig`、`TestChartOAuth2ProxyEnvIsAllowlisted`）
- **3g**：`tests/helm/test_tenant_api_portal_container.py`（`test_portal_runs_the_charts_config_and_it_forwards_nothing`）
- **4a**：`tools/portal/tests/api-fetch.test.ts`（`apiFetch: the path`）；`tools/portal/tests/network-calls-only-in-api-fetch.test.ts`（`network calls only in apiFetch`）
- **4b**：`components/tenant-api/cmd/server/simulate_route_test.go`（`TestSimulateNotProvided`）；`tests/shared/test_portal_simulate_not_provided.py`（`test_tenant_api_answers_the_same_body`、`test_tenant_api_answers_the_same_code`）
- **5a**：`tests/helm/test_identity_single_hop.py`（`test_no_raw_manifest_or_rule_pack_names_an_identity_header`、`test_no_rendered_object_but_the_oauth2_proxy_config_names_an_identity_header`）
- **5b**：`tests/helm/test_tenant_api_machine_identity_removed.py`（`test_a_removed_key_fails_the_render`、`test_values_yaml_carries_no_removed_key`）；`tests/helm/test_tenant_api_scope_switches_removed.py`（`test_a_removed_key_fails_the_render`、`test_values_yaml_carries_no_removed_key`）
- **5c**：`tests/shared/test_threshold_govern_write_path_removed.py`（`test_no_chart_mentions_threshold_govern`、`test_no_network_policy_admits_the_threshold_govern_pod`、`test_no_threshold_govern_cronjob`、`test_no_threshold_governance_stale_alert`、`test_tenant_api_chart_has_no_threshold_govern_allowance`）
- **5d**：`components/tenant-api/cmd/server/env_check_test.go`（`TestCheckTAEnv_RefusesSettingsADR027Removed`）
- **6a**：`components/tenant-api/cmd/server/identity_config_startup_test.go`（`TestMainRefusesAnAmbiguousOAuth2ProxyConfig`）
- **6b**：`components/tenant-api/internal/proxyconfig/proxyconfig_test.go`（`TestLoad_RefusesAmbiguousShapes`、`TestParse_Refusals`）
- **6c**：`components/tenant-api/internal/rbac/empty_config_mode_test.go`（`TestEmptyRuleSet_EveryEntryPointDenies`、`TestEmptyRuleSet_LegacyShimsDeny`）
- **6d**：`components/tenant-api/cmd/server/env_check_test.go`（`TestCheckTAEnv_RefusesSettingsADR027Removed`）；`tests/helm/test_tenant_api_scope_switches_removed.py`（`test_a_removed_key_fails_the_render`、`test_the_default_render_sets_no_removed_variable`、`test_values_yaml_carries_no_removed_key`）
- **6e**：`components/tenant-api/internal/handler/label_guard_test.go`（`TestLabelGuardBatchDirect`、`TestLabelGuardPutDirect`、`TestLabelGuardPutNewTenant`、`TestLabelGuardPutPRModeUnreadableBase`、`TestLabelGuardUnreadableLabelsBefore`）
- **6f**：`components/tenant-api/internal/handler/fresh_base_scope_test.go`（`TestPRModeOrgScopeJudgedOnFreshBase`、`TestPRModeScopeAuthoritativeOnFreshBase`、`TestPRModeScopeJudgedEvenWhenTheTreeMovedMidRequest`）；`components/tenant-api/internal/handler/label_guard_test.go`（`TestLabelGuardBatchDirectRelabelLandsFirst`、`TestLabelGuardBatchPRModeBaseDrift`、`TestLabelGuardPutDirectRelabelLandsFirst`、`TestLabelGuardPutPRModeBaseDrift`、`TestLabelGuardPutPRModeUnreadableBase`、`TestScopeGateDirectNoopFirstOpStillJudged`、`TestScopeGateDirectRelabelLandsFirst`）
- **7a**：`components/tenant-api/cmd/server/scope_check_test.go`（`TestConfigCheckScope_Fixture`）
- **7b**：`components/tenant-api/internal/handler/scope_gap_planes_test.go`（`TestScopeGapsMatchEveryPlane`）
- **7c**：`.github/workflows/ci.yml`（`Scope check on the try-local sample config (ADR-027 §7)`）
- **8a**：`tests/lint/test_check_adr_section_refs.py`（`test_existing_sections_pass`、`test_missing_sections_fail`、`test_the_repository_is_clean`）
- **8b**：`tests/lint/test_check_adr_section_refs.py`（`test_draft_code_needs_adr_027_before_it`、`test_withdrawn_draft_codes_fail`）
- **9a**：`.github/workflows/ci.yml`（`Run --config-test in the chart's oauth2-proxy image (by digest)`）；`tests/oauth2-proxy/image_config_test.sh`
- **9b**：`tests/oauth2-proxy/chart_test.go`（`TestChartConfig`）；`tests/oauth2-proxy/routing_test.go`（`TestADRSection10Routing`）；`tests/oauth2-proxy/version_test.go`（`TestLibraryVersionIsTheChartImage`）
<!-- END GENERATED: adr-checks -->

## 相關

- [#1040](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1040)：實作與發布追蹤
- [#1667](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1667)：ADR-027 檔案不存在
- [#962](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/962)：Identity & Routing epic
- [GHSA-3g2h-rf85-5rrv](https://github.com/vencil/Dynamic-Alerting-Integrations/security/advisories/GHSA-3g2h-rf85-5rrv)
- [ADR-009](./009-tenant-manager-crud-api.md)：認證交給 oauth2-proxy 的原始決定
- [ADR-022](./022-dev-auth-bypass-four-layer-containment.md)：開發模式驗證旁路的隔離
- [ADR-036](./036-single-parser-effective-config.md)：以 pod 內 UDS 提供計算的先例
- [`governance-security.md`](../governance-security.md)：L4 止血與 L7 根因的說明
