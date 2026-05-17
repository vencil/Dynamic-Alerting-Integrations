---
title: "Federation 簽章金鑰輪替 Runbook"
tags: [internal, runbook, federation, security]
audience: [platform-engineer, sre]
version: v2.8.1
lang: zh
---

# Federation 簽章金鑰輪替 Runbook

ADR-020 IV-2l。federation token 是 tenant-api 用 RS256 私鑰簽的 JWT;
API gateway（IV-2b）用對應公鑰的 JWKS 驗章。本 runbook 是這把金鑰的
**生成、輪替、緊急汰換**標準流程。

工具:`da-tools fed-key`(`scripts/tools/ops/federation_keygen.py`)。

## 金鑰的兩個消費端

| Artifact | 消費端 | 怎麼交付 |
|---|---|---|
| 私鑰 PEM | tenant-api(`--federation-key`) | `da-tools fed-key` 吐出的 Secret manifest |
| 公鑰 JWKS | federation-gateway(`jwt.jwks`) | `da-tools fed-key` 寫出的 `--jwks-out` 檔 |

每把公鑰的 `kid` 是它的 RFC 7638 thumbprint。tenant-api 簽 token 時對
載入的金鑰算同一個 thumbprint 當 `kid` header,gateway 用 `kid` 在
JWKS 裡 O(1) 選鑰 —— 故 JWKS 裡放多把鑰(輪替期)不會讓驗章變慢。

## 1. 首次 bootstrap

```sh
da-tools fed-key --namespace monitoring | kubectl apply -f -
```

- 私鑰 → Secret `monitoring/tenant-federation-signing-key`(直接套用,不落地)。
- 公鑰 JWKS → `./federation-jwks.json`。把它的內容設成 federation-gateway
  chart 的 `jwt.jwks` value 並 `helm upgrade` gateway。
- `helm upgrade` tenant-api,讓它掛載該 Secret、設定 `--federation-key`。

## 2. 計畫性輪替(grace-period overlap)

⚠️ **順序鐵則**:gateway 必須**先**認得新公鑰,tenant-api **才能**開始用
新私鑰簽。順序顛倒 → 新 token 在 gateway 一律驗不過,直到 gateway 追上。
**因此計畫性輪替不要用 `| kubectl apply` 一行流** —— 私鑰 Secret 要先存檔、
最後才套用。

1. **產新金鑰、併入現有 JWKS**(私鑰存檔,先不套用):
   ```sh
   ( umask 077 && da-tools fed-key --rotate --existing-jwks federation-jwks.json \
       --jwks-out federation-jwks.json > new-signing-key.secret.yaml )
   ```
   `federation-jwks.json` 現含**舊+新兩把**公鑰;新私鑰 Secret 在
   `new-signing-key.secret.yaml`。
   ⚠️ **`umask 077` 不可省**:`>` 重導向建檔的權限由 shell umask 決定,預設
   常是 `0644`(全機器可讀)。該檔內含私鑰 PEM,故用 subshell `umask 077`
   讓它**建檔即 `0600`**,沒有任何 world-readable 的時間窗。套用後即刪除。
2. **先更新 gateway**:把合併後的 `federation-jwks.json` 設進
   federation-gateway 的 `jwt.jwks`、`helm upgrade` gateway。此刻 gateway
   同時接受**舊鑰或新鑰**簽的 token。
3. **等 gateway rollout 完成**(chart 依 jwks ConfigMap checksum 自動滾)。
4. **再套用新私鑰**:`kubectl apply -f new-signing-key.secret.yaml`,並
   `helm upgrade` / 重啟 tenant-api 載入新私鑰。此後新 token 用新 `kid`。
5. 舊 `kid` 的 token 在 4h TTL 內仍驗得過(舊公鑰還在 JWKS)。
6. **(可選清理)** 等 > 4h(舊 token 全數過期)後,可把舊公鑰從
   `federation-jwks.json` 的 `keys[]` 移除再 `helm upgrade` gateway。
   因為驗章是 `kid`-based,留著舊鑰**無害**(沒有 token 會再帶它的
   `kid`),此步純為整潔,非必要。

> **中止 / 復原**:計畫性輪替在步驟 4(套用新私鑰)前都可無痛中止 ——
> tenant-api 還在用舊私鑰簽,gateway 多掛一把新公鑰無任何副作用,直接停手即可。
> 若 `federation-jwks.json` 不慎遺失,它可從 gateway 現行 `jwt.jwks` value
> 還原(`helm get values <gateway-release>`)—— JWKS 是公鑰、本就無機密。

## 3. 緊急汰換(私鑰疑似外洩)

私鑰外洩 = 攻擊者能偽造**任何**租戶、任何 `token_id` 的 token。revoked-set
是 per-token 機制,**擋不住**無法窮舉的偽造 token id —— 唯一的處置是立刻
汰換金鑰,且**不做 grace overlap**:

1. 產全新金鑰(**不要** `--rotate`,新 JWKS 只含新鑰):
   ```sh
   ( umask 077 && da-tools fed-key --namespace monitoring > new-signing-key.secret.yaml )
   ```
   同 §2 step 1:`umask 077` subshell 讓私鑰檔建檔即 `0600`。
2. `helm upgrade` gateway,`jwt.jwks` 設成**只有新鑰**的 JWKS —— 舊公鑰
   立即從 JWKS 移除。所有舊鑰簽的 token(含偽造的)即刻全部失效。
3. `kubectl apply -f new-signing-key.secret.yaml` + 重啟 tenant-api。
4. 已合法簽發的舊 token 會一起失效 —— 緊急情境下安全性 > 可用性,接受
   租戶需重新取得 token。
5. 走 [`secret-leak-remediation-sop.md`](secret-leak-remediation-sop.md)
   的事件流程(ASSUME COMPROMISE / ROTATE FIRST),並把外洩記入 post-mortem。

## 注意事項

- 私鑰只透過 `da-tools fed-key` 的 stdout(Secret manifest)流動。首次
  bootstrap 直接 `| kubectl apply`、私鑰不落地最理想。計畫性輪替 / 緊急
  汰換**需要**先把 Secret manifest 存檔(見 §2 / §3)—— 此時**務必**用
  `( umask 077 && ... > file )` subshell,讓私鑰檔建檔即 `0600`;`>` 預設
  靠 shell umask(常是 `0644`,全機器可讀),含私鑰的檔不可如此。套用後刪檔。
- `fed-key` 偵測到 stdout 是互動式終端時會**直接拒絕並 exit 1** —— 避免
  漏接 `| kubectl` / `> file` 時把私鑰印上螢幕、殘留在終端 scrollback。
  故所有呼叫務必接 pipe 或重導向。
- `da-tools fed-key` shell-out 到 `openssl genpkey`(`genrsa` 在 OpenSSL
  3.0 已棄用),預設 RSA-2048(tenant-api 拒收 < 2048-bit)。
- JWKS 是公鑰,非機密 —— 可正常存放 / 進 git / 進 Helm values。
