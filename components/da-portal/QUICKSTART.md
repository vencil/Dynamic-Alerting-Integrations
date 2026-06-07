# da-portal — 在瀏覽器裡玩的告警與維運互動工具

> **把複雜的 PromQL 與告警配置，封成「打開瀏覽器就能玩」的一整套互動工具。**
> *Interactive alerting/ops tools in your browser — open and play.*

|  |  |
|---|---|
| **是什麼** | 一個 self-service portal，內含 44 個互動工具：alert builder / PromQL tester / threshold calculator / cost estimator / routing trace / config lint…（完整清單見 [Interactive Tools Hub](../../docs/interactive-tools.md)） |
| **解決什麼** | 不用先讀懂 PromQL、不用裝任何工具，就能試算閾值、預覽告警、驗證設定 |
| **怎麼跑** | 一顆 nginx 靜態 image，`docker run` 一行即起；工具是**預先 build 好的**，你這端不需要 npm、不需要 build |
| **打開會看到** | 一個互動工具的儀表板，**立即可玩** |

> 🎯 **這個 portal 服務誰？** 三種角色，各有專屬上手指南：
> - **Tenant（租戶）** — 瀏覽器自助調閾值、存 Saved View → [Tenant 指南](../../docs/getting-started/for-tenants.md)
> - **Platform Engineer / SRE** — 為租戶除錯、規劃容量、驗證路由 → [Platform 指南](../../docs/getting-started/for-platform-engineers.md)
> - **Domain Expert** — 把監控知識落成 Rule Pack / 預算守則 → [Domain Expert 指南](../../docs/getting-started/for-domain-experts.md)

**前置需求**：Docker 20.10+。

## 馬上試（≤ 2 分鐘）

```sh
docker run --rm -p 8080:80 ghcr.io/vencil/da-portal:v2.8.0
#   → 開瀏覽器：http://localhost:8080
```

大多數工具是**純前端**（calculator / tester / selector / estimator…），單跑這顆 image 就能離線玩。

> ⚠️ **少數旗艦工具需要後端**——Tenant Manager / Saved Views / Simulate 會打 `tenant-api`，單跑 portal 時它們會顯示連線錯誤（這是預期的）。要看**活的** Tenant Manager（管理真實租戶、模擬變更、Save 落真實 git commit），在 [`try-local/`](../../try-local/) 起「核心雙星」：
>
> ```sh
> cd try-local
> docker compose up da-portal tenant-api    # 約 10 秒，旗艦工具全活
> ```

## 接下來

- **先玩整套** → [`try-local/`](../../try-local/)：一鍵起完整監控網格 + 真實 critical 告警紅燈
- **進階配置 / 完整參考** → [`README.md`](README.md)：build、部署、客製、troubleshooting
- **上 production** → [`helm/da-portal/`](../../helm/da-portal/)：ingress + oauth2-proxy 接 tenant-api
