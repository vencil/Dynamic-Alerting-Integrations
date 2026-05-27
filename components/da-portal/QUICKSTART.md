# da-portal — interactive alerting & ops tools, right in your browser

> **把複雜的 PromQL 與告警配置封進一整套瀏覽器互動工具——打開就玩，零安裝。**
> *Interactive alerting/ops tools in your browser — zero install.*

|  |  |
|---|---|
| **What / 是什麼** | self-service portal：alert builder / PromQL tester / threshold calculator / cost estimator / rule-pack selector…一整套互動工具（完整清單見 [Interactive Tools Hub](../../docs/interactive-tools.md)）。*A self-service portal of interactive tools.* |
| **Why / 為什麼** | 把告警/維運能力封成**打開瀏覽器就能玩**的工具，零安裝、零 npm、零 build。*Complex PromQL & alert config, packaged as browser tools.* |
| **Who / 給誰** | 任何想「先看看這平台能幹嘛」的人 |
| **Try（≤2 min）** | `docker run --rm -p 8080:80 ghcr.io/vencil/da-portal:v2.8.0` → 開 http://localhost:8080 |
| **→ You'll see** | 互動工具的儀表板，**立即可玩**。*A dashboard of interactive tools, instantly.* |

> 🎯 **主要服務對象**：Tenant（瀏覽器自助調閾值 / Saved View，見 [Tenant 角色指南](../../docs/getting-started/for-tenants.md)）；Platform Engineer 為租戶除錯時亦常用。

**Prerequisite**：Docker 20.10+。

## Try it

```sh
docker run --rm -p 8080:80 ghcr.io/vencil/da-portal:v2.8.0
#   → 開瀏覽器：http://localhost:8080
```

**不需要 npm、不需要 `portal-build`、不需要任何後端**——這是 da-portal 的 packaging 優勢：published image 內含預先 build 好的 ESM bundle，`docker run` 一行即玩 ~34 個**純前端**工具（calculator / tester / selector / estimator…）。

> ⚠️ **旗艦 Tenant Manager / Saved Views / Simulate 需要 tenant-api 後端**——單跑 portal 時它們會顯示連線錯誤（這是預期的）。要看**活的** Tenant Manager（管理真實租戶、模擬變更），跑「核心雙星」：
>
> ```sh
> # 在 try-local/ 目錄
> docker compose up da-portal tenant-api
> ```
>
> 約 10 秒，portal 後端有 tenant-api 撐腰，旗艦工具全活。

## Next
- ← **先玩整套**：[`try-local/`](../../try-local/)（Mode 0 核心雙星 + 完整監控網格與告警紅燈）
- 📖 **深入配置 / 完整工具清單**：[`README.md`](README.md)（本元件的 reference）
- → **上 production**：[`helm/da-portal/`](../../helm/da-portal/)（nginx 靜態服務 + ingress / oauth2-proxy 接 tenant-api）
