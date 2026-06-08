---
title: "Secret Leak Remediation SOP"
tags: [documentation, security, sop, incident-response]
audience: [maintainers, on-call, contributors]
version: v2.9.0
verified-at-version: v2.8.0
lang: zh
---
# Secret Leak Remediation SOP

> 公開 repo 一旦推上含 secret 的 commit，**這份 SOP 是你接下來 10 分鐘內要做的事**。
> 不要花時間讀完整份文件 — 先看下方鐵律與 5 步驟，邊做邊細看。
> **相關文件：** [`dev-rules.md` §安全紀律](dev-rules.md) · `.github/workflows/secret-scan.yml`（L2 server-side scanner，其失敗 annotation 連回本 SOP）· [GitHub Release Playbook](github-release-playbook.md)

---

## 🚨 鐵律：ASSUME COMPROMISE. ROTATE FIRST.

公開 repo 一旦推上含 secret 的 commit，**駭客 scraper 在數秒到數分鐘內就抓到**（GitGuardian、TruffleHog 等服務本身就是不斷掃公開 commit 的，攻擊者也跑類似工具）。

`git push -f` / BFG / `git filter-repo` **是安慰劑** — 以下管道仍持有原 commit blob，洗不掉：

- 任何在你 force-push 前已 `git fetch` 的 fork
- GitHub 內部 reflog（保留期未公開，業界經驗數週至數月；Step 4c 工單可加速 invalidation）
- GitHub Actions workflow run history（已執行的 job log 含 leaked secret，需單獨清理）
- GHArchive 第三方資料集（每小時 snapshot 整個 GitHub event stream）
- BigQuery `github_repos` public dataset（季度級別 snapshot）
- 各家 secret-scanning SaaS 自己的歷史快取

**第一步、第二步、第三步**：到 secret provider（AWS / GCP / 資料庫 / Slack / ...）console 把那把 key **Revoke / Rotate**。

**洗 git history 是次要善後**，順序在 Rotate 之後。Step 4 之前不要碰 git。

---

## 5-Step Response

### Step 1 — Identify（< 1 分鐘）

打開 secret-scan workflow 失敗的 annotation 或 PR comment，記下：

- **Secret provider**（AWS / GCP / Azure / Slack / GitHub Token / Stripe / 內部 DB / 內部 JWT / 內部 OAuth client / ...）
- **疑似 key 內容**（前 4 + 後 4 字元，整段 key 不要貼到外部系統）
- **commit SHA + 檔案路徑 + 行號**（filter-repo Step 4 會用到）
- **commit author + 時間**（post-mortem 用）

如果同一 commit 含 **多個 secret**，每個獨立 rotate；**全部都要做 Step 2**，不要因為趕時間漏掉。

### Step 2 — Rotate（5 分鐘內完成；無需任何人 approve）

去對應 provider console，**自己直接 rotate 不等 review**。本 repo 任何 contributor 都有授權執行此步驟 — 等 approval 等的時間就是 attacker 在用那把 key 的時間。

⚠️ Provider UI 路徑會隨時間變動 — 下表是 2026-Q2 當下的入口；若 menu 名稱不符請查 provider 自家最新文件「rotate / revoke credentials」段，**不要因為找不到對的選單就停下 Step 2**。

| Provider | 入口 | 動作 |
|---|---|---|
| AWS | IAM → Users → Security credentials | Deactivate 舊 access key → Create new |
| GCP | IAM → Service Accounts → Keys | Disable → Add Key |
| Azure | App registrations → Certificates & secrets | Delete client secret → New client secret |
| GitHub PAT | Settings → Developer settings → Personal access tokens | Delete → Generate new |
| Slack Bot Token | App console → OAuth & Permissions | Regenerate (注意：所有用此 token 的服務需同步更新) |
| Slack webhook signing secret | App console → Basic Information → App Credentials | Regenerate Signing Secret → 重啟所有 receiver |
| Stripe API | Developers → API keys | Roll secret key |
| SSH private key（user / deploy key） | GitHub Settings → SSH and GPG keys / Repo Settings → Deploy keys | Delete leaked → 本地 `ssh-keygen -t ed25519` 重生 → upload 新 public key |
| TLS private key / 伺服器憑證 | 對應 CA console（Let's Encrypt 用 certbot revoke / 商業 CA 用 admin portal） | Revoke 舊憑證 → 重新簽發 → 部署到所有 termination 點 |
| NPM publish token | npmjs.com → Profile → Access Tokens | Revoke → Generate New Token（注意：CI 需同步更新） |
| PyPI publish token | pypi.org → Account → API tokens | Remove → Add API token |
| GitHub webhook secret | Repo Settings → Webhooks → 編輯目標 webhook | Regenerate Secret → 通知所有接收端同步 |
| 內部 DB（MariaDB / Postgres / ...） | DB shell: `ALTER USER ... IDENTIFIED BY '...'` 或對應 Helm/K8s Secret resource | 改密碼 → 同步更新使用該帳號的服務 |
| 內部 JWT signing key | 對應 Helm chart values / K8s Secret | 換新 key → 重啟所有 verifier；**舊 key 簽發的 token 視同立即失效**（⚠️ 副作用：**全網 Mass Logout** + API-to-API 401 storm，屬預期行為 — 啟動 rotate 前 ping SRE / 客服讓他們認得這波警報，避免被誤判成系統崩潰而 rollback） |
| 內部 OAuth client | 自家 OAuth server admin UI | Rotate client_secret → 通知所有 client app |
| Container registry token（GHCR / Docker Hub） | GHCR: GitHub PAT (packages:write scope) / Docker Hub: Account Settings → Security | 同上層 token 處理 |
| Observability API key（DataDog / NewRelic / Grafana Cloud / PagerDuty） | 各家 console → API keys / Integration settings | Revoke → 重新簽發 → 同步所有 agent / scraper |
| 其他 | 看 provider 自家文件「rotate credentials」段 | — |

**「無需 approve」是政策，不是建議**：本 repo 已在 `dev-rules.md` §安全紀律 明訂任何 contributor 撞到本場景都有 unilateral 授權執行 rotate。**事後 review 程序在 Step 5**，不阻擋 Step 2。

### Step 3 — Notify Affected Systems（10 分鐘內）

Rotate 完，要讓所有用舊 key 的服務切到新 key，否則生產會在你修 git 時掛掉。

| 影響範圍 | 動作 |
|---|---|
| K8s Deployment 透過 Secret 注入 | `kubectl rollout restart deployment/<name> -n <ns>` 在更新 Secret 後（pull 新 env） |
| Helm chart values | `helm upgrade <release> -f <values>` 帶新 secret |
| CI 環境變數 | 去 GitHub Actions → Settings → Secrets and variables 改值（不要塞 plaintext 到 workflow） |
| Local dev `.env` | 通知 contributor 改本機檔案；提供新 key 的 secure-channel 分發（**不要走 Slack public channel / repo issue**） |
| Customer-facing service | 若有對外公開的服務依賴此 key，**先確認 rotate 後該服務仍正常**；異常就 rollback 並重新規劃 |
| GitHub Actions run history | 已執行的 workflow run 若以 env / output 形式接觸過 leaked secret，run log 可能仍含其值 — 進 Actions → 該 workflow → 找出影響 run → **Delete workflow run**（API: `DELETE /repos/{o}/{r}/actions/runs/{id}`）。若 run 數量大可寫腳本批次刪 |
| **自動化建置產物（Build Artifacts / Container images）** | leaked commit 觸發的 CI build 可能已把 secret **烤進 Docker image / 靜態 build cache** — Vibe 的 release.yaml 對每個 git tag build 4 個 image（threshold-exporter / tenant-api / da-portal / da-tools），若 tag 與 leaked commit 重疊，需：(1) 把對應 image tag 從 GHCR 標記為 vulnerable 或直接刪除；(2) 從乾淨的 commit 重新 build + push；(3) 通知任何已 pull 該 image 的下游 cluster 重新 pull。GitOps（ArgoCD / Flux）或 PaaS（Vercel / Netlify / Render）的 auto-deploy 同樣需檢查 |
| Auto-post 到外部頻道的 webhook / integration | 若 repo 設有 commit→Slack / commit→Discord webhook，leaked commit 內容可能已被推到 channel — 通知 channel admin 刪訊息或截斷頻道歷史 |
| 客戶資料外洩疑慮 | 若 leaked secret 可存取客戶資料（DB password 等），啟動 **Step 3b** |

#### Step 3b — Customer Notification（僅當 leaked secret 可存取客戶資料時）

「可存取」涵蓋讀取（exfiltration risk）與寫入（corruption risk）— DB superuser password、production K8s admin token、long-lived OAuth bearer 等屬此 level；read-only metrics endpoint token 不算。

**法律與合約強制要求**，與 git 洗歷史平行進行：

- **GDPR**（歐盟 data subject）：72 小時內向歐盟 supervisory authority 通報；若 PII risk 高，同步通知 data subject
- **個資法**（臺灣 data subject，§12）：依事件嚴重程度，主動通知個案 data subject + 主管機關
- **CCPA / CPRA**（加州 data subject）：依法律 trigger 條件啟動 notification
- **HIPAA**（美國 healthcare data，若適用）：60 天內 notify affected individuals + HHS
- **客戶合約**：檢視合約 incident-notification clause（多數企業合約要求 24-72 小時內 notify；金融 / healthcare / gov 客戶可能更嚴格）
- **內部紀錄**：寫進 audit log，列入下一次客戶 review

> 本表**非窮舉** — 其他司法管轄區（GDPR-UK / PIPEDA / LGPD / APPI / PIPL ...）的合規要求請以法務 / DPO 判斷為準。

**Triage owner（誰決定要不要通知客戶）**：repo owner @vencil + backup @TBD。Step 1 confirm secret 真為「可存取客戶資料」level 時直接 ping 兩位。

### Step 4 — Clean Git History（重要但非緊急）

**只有 Step 2 完成、Step 3 進行中或完成後才做。** 順序不能換。

#### 4a — Rewrite local + force push

```bash
# 安裝 git-filter-repo（建議優於 BFG，更穩定）
pip install git-filter-repo

# 移除特定檔案的所有歷史
git filter-repo --path <path/to/leaked-file> --invert-paths

# 或：移除特定 blob hash（精準到單一 commit 的單一檔案）
git filter-repo --strip-blobs-with-ids <blobs.txt>

# Force-push 改寫後的歷史
git push --force-with-lease origin <branch>
git push --force-with-lease origin --tags
```

⚠️ **`--force-with-lease` 比 `--force` 安全** — 若有人在你 rewrite 期間 push 進 branch，會擋住而不是覆蓋。

#### 4b — 通知 contributor 強制 re-clone（不要 `git pull`）

Force-push 改寫歷史後，任何已 clone 此 repo 的 contributor 都得 **重新 clone**（不能 `git pull --rebase`，會把 leaked blob 從本地推回去）。發訊息範本：

```
@channel: 因 #<incident-id> 已 force-push 改寫 git history，請於下次工作前：

  1. cd somewhere-safe
  2. git clone git@github.com:vencil/Dynamic-Alerting-Integrations.git da-fresh
  3. 把任何本地未推上去的 branch 用 `git format-patch` 從舊 clone 撈出來
  4. ⚠️ 重要：套用 patch 前，先用 grep / 編輯器**檢查每一個 .patch 檔案**
     確認沒有把 leaked secret 一起帶過來（見下方 re-infection 警告）
  5. 在 da-fresh 用 `git am` apply 確認乾淨的 patch
  6. rm -rf 舊 clone

不要 `git pull` 舊 clone（會把 leaked blob 推回 main）。
```

🚨 **CRITICAL — Re-infection Vector**：如果你本地 branch 包含原始 leaked commit（你就是那個 push 進去的人，或你曾 `pull` 帶 leak 的 branch 並基於它繼續開發），`git format-patch` 會把 leaked secret **原封不動寫進 .patch 檔案**。直接 `git am` 進新 clone 等於把 leak 種回去，下次一 push 就再次污染剛洗乾淨的歷史。**檢查步驟必跑**：

```bash
# 對每個從舊 clone 撈出的 .patch 檔，跑檢查
for p in *.patch; do
  if grep -E '<leaked-key-fragment-或-pattern>' "$p"; then
    echo "❌ $p contains leaked secret — DO NOT git am"
    # 改用 git apply --reject 手工挑掉受污染的 hunk，或重做這個 patch
  fi
done
```

若 patch 受污染，**不能 `git am` 該檔**。處理方式擇一：(a) 用 `git apply --reject <patch>` + 手工挑掉含 secret 的 hunk；(b) 在舊 clone 上 `git rebase -i` drop 掉含 secret 的 commit 再重新 `format-patch`；(c) 接受該 branch 的 unmerged 工作必須重做（最安全，若 patch 改動小）。

**Open PR 處理**：force-push 重寫 `main` 後，所有 open PR 的 base SHA 失效，GitHub UI 會顯示為 "out of date / unmergeable"。每個 open PR 都需要 owner 重新 rebase：

```bash
git fetch origin main
git rebase origin/main          # 解 conflict（多為 cherry-pick 重複 diff）
git push --force-with-lease     # 更新 PR head
```

若 open PR 數量大，將上述指令發給各 PR owner 或在 PR comment 自動化。

#### 4c — 請 GitHub Support 清 cache

GitHub Web UI、API、reflog 仍會回應已被 rewrite 的 SHA 一段時間（業界經驗數週至數月）。提工單請 GitHub Support 加速 cache invalidation：

⏰ **SLA 期待管理**：非 Enterprise / Advanced Security 客戶的 GitHub Support 人工處理通常需 **24-72 小時甚至數日**。這段期間舊 SHA 仍可透過 API 取回 leaked blob — 這就是為什麼 Step 2（Rotate）必須優先：你不能依賴 cache invalidation 來止血，那只是 belt-and-suspenders 的 belt 部分。

[support.github.com](https://support.github.com) → **Contact GitHub Support** → 選 Account / Security → 範本如下：

> Subject: Request to purge cached blob/commit references after force-push (secret leak remediation)
>
> Repository: vencil/Dynamic-Alerting-Integrations
> Affected commit SHA(s): <SHA1>, <SHA2>
> Affected blob SHA(s): <blob_SHA>
> Force-push completed at: <ISO-8601 timestamp UTC>
>
> We performed force-push history rewrite following a secret leak incident.
> The associated secret has been rotated at the provider side (Step 2 of our
> incident SOP). Please expedite cache invalidation for the listed SHAs to
> reduce the public-exposure window of the (already-rotated) credential
> beyond what `git filter-repo` alone can achieve.
>
> Thank you.

### Step 5 — Post-mortem + 預防規則更新（隔天，正常工時內）

不是 incident 當下要做的事，但**不能跳過**。否則下次同類事件還會發生。

| 紀錄項 | 在哪 |
|---|---|
| Incident timeline（誰、何時、什麼動作） | 新 `docs/internal/incident-<YYYYMMDD>-<short-name>.md`，或 `archive/incidents/` |
| Leaked secret 的「分類」（已知格式 vs 高熵值；provider；長壽 vs 短壽） | 同上 |
| 為何 L0/L1/L2 三層沒擋下 | 同上 — 是 detection rule 缺漏？還是 contributor 用了 `--no-verify`？ |
| **新增/修正 detection rule** | 視診斷結果 — 若為 detection 缺漏，補 trufflehog 自訂 regex 或 pre-commit pattern；若為 `--no-verify` 濫用，看是否需要 server-side `pull_request_target` workflow 強化 |
| 通知客戶（如 Step 3b 已啟動） | 結案信，附 root cause + remediation 摘要 |

**Post-mortem 必須是 blameless**：本 SOP 鐵律之一是「自己 rotate 不等 approve」— 任何遵守本 SOP 的 contributor 不應因 incident 受到 negative review，後續 process 改善（lint 加強、教育、自動化）是團隊責任不是個人。

---

## 🧯 Decision Tree（incident 當下用得到）

```
偵測到 secret-scan workflow failure
                │
                ▼
   Step 1 — Identify provider + key
                │
                ▼
   是「可存取客戶資料」level？───── 是 ──┐
                │                       │
                否                      ▼
                │              並行：Step 2 + Step 3b
                ▼                       │
            Step 2 — Rotate ◄───────────┘
                │
                ▼
            Step 3 — Notify affected systems
                │
                ▼
     生產服務恢復正常？───── 否 ──► Rollback, 重新規劃 rotate
                │
                是
                ▼
            Step 4 — Clean git history
                │
                ▼
            Step 5 — Post-mortem（隔天）
```

---

## 📝 Triage Ownership

當 secret-scan workflow 觸發 **Verified finding**（活的 key 被偵測）：

| 角色 | 對象 | 何時 ping |
|---|---|---|
| **第一聯絡人** | @vencil（repo owner） | workflow failure 出現後立即（自動 @mention 在 PR comment） |
| **Backup contact** | @TBD（v2.8.1 closure 前需明確指派並更新本表） | 第一聯絡人 24 小時內無回應，或 leak 屬於「可存取客戶資料」level |
| **資安部門對接** | 客戶資安窗口（per 客戶合約 incident clause） | 僅 Step 3b 啟動時 |

**任何 contributor 都有授權執行 Step 2**（不等 approve）。Triage Ownership 是「決策層」職責（要不要通知客戶、要不要對外公告、post-mortem owner），不是「能不能 rotate」的 gate。

---

## ❌ 反 SOP — 已知會放大傷害的動作

| 動作 | 為什麼錯 |
|---|---|
| 先 `git push -f` 再 rotate | 推完到 rotate 完成這段時間，attacker 已用過那把 key 多次 |
| 等 owner approve 再 rotate | approve 等的每分鐘都是 attacker 用 key 的窗口；本 SOP 明訂 contributor 有 unilateral 授權 |
| 用 `git push --force`（無 lease） | 若有人在 rewrite 期間 push，會覆蓋對方工作；用 `--force-with-lease` 替代 |
| 把 leaked key 貼到 Slack / repo issue 討論 | 等於再 leak 一次到不同管道；改 secure channel（1Password Share、Signal、PGP email） |
| 把 rotate 後的**新 key** 寫進 commit message / PR description / changelog | rotate 完馬上 push 含新 key 的訊息 = 自己重演同樣 leak；新 key 僅透過 secret manager / K8s Secret / CI vault 分發 |
| 用 leak 發生時相同的管道分發新 key | 例如「leak 是因為 `.env` 進了 commit」，rotate 後又把新 key 放 `.env` 然後 Slack 截圖傳對方 — 風險路徑沒變 |
| 跳過 Actions run history 清理 | 已執行的 workflow run log 仍存 leaked secret，attacker 透過 GH API 可取回（Step 3 已列） |
| 跳過 Step 4c GitHub Support 工單 | reflog 視窗內仍可透過 API 取回 leaked blob |
| 跳過 Step 5 post-mortem | 下次同類事件還會發生；detection rule 不會自己長出來 |
| 用 BFG 而不是 `git filter-repo` | BFG 仍在維護但 git-filter-repo 是上游推薦繼任者，bug 較少 |
| 用 `git reset --hard <old-sha>` 然後 push | 只把 local HEAD 往回搬，遠端仍是 leaked commit；必須走 `git filter-repo` 真正改寫歷史 |
| Step 4b 從舊 clone `format-patch` 後直接 `git am`，不檢查 patch 內容 | 🚨 re-infection vector — 若舊 clone 的 local branch 含 leaked commit，patch 把 secret 原封不動帶進新 clone，下次一 push 就再次污染歷史。**`git am` 前必跑 `grep` 檢查每個 .patch** |
| 只洗 git history 不清 CI build artifacts | release.yaml 對 leaked commit 已 build 的 Docker image 仍含 secret（COPY / ARG / RUN 烤進 layer）— 須從 GHCR 標記為 vulnerable 或刪除並 rebuild |
| 期待 GitHub Support 快速清 cache 而把 Step 2 Rotate 延後 | 非 Enterprise 客服可能需數日；那段期間舊 SHA 仍可取回 leaked blob — Rotate-First 政策的根本理由就是不能依賴 cache invalidation 來止血 |

---

## 🔗 References

- [GitHub: Removing sensitive data from a repository](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/removing-sensitive-data-from-a-repository) — 官方 force-push 流程
- [git-filter-repo](https://github.com/newren/git-filter-repo) — 上游推薦的 history rewrite 工具
- [GitGuardian: State of Secrets Sprawl](https://www.gitguardian.com/state-of-secrets-sprawl-report) — 公開 repo secret 在被 push 後多快被 scraper 抓到的實測
- [`dev-rules.md` §安全紀律](dev-rules.md) — 本 repo `--no-verify` 嚴禁政策 + L0/L1/L2/L3 四層防線指引（issue [#445](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/445) AC iv）
- `.github/workflows/secret-scan.yml` — L2 server-side scanner（issue [#445](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/445) AC ii），其失敗 annotation 會 link 回本 SOP。此處刻意以路徑表示而非 markdown link — `.yml` 在 `.github/` 不屬 MkDocs doc-tree，加 link 會觸發 strict-mode broken-link 警告
