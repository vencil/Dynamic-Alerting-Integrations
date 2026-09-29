---
title: "工具鏈版本對照 — 本地 / CI 同版的 SSOT 與守衛"
tags: [internal, ci, dx, toolchain]
audience: [contributors, maintainers, ai-agent]
version: v2.9.0
lang: zh
---

# 工具鏈版本對照（本地 ↔ CI）

> 目的：本地（dev container、`make`、pre-commit）與 GitHub Actions 跑**同一套工具版本**，查案時「本地綠、CI 紅」不會是版本差造成的。[#1337](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1337)。
>
> ⛔ 本頁**刻意不寫版本號**——版本號只存在於下表「SSOT」欄那個檔案裡，寫在這裡就是第二份、必然先腐爛。要看現值，打開 SSOT 那一格；要知道升版時要一起改哪裡，看「消費端」與「守衛」兩欄。守衛會在任何一處沒跟上時轉紅。

## 升版的通則

1. 改 SSOT，再改同一列的每一個消費端——**同一個 PR**。
2. 有 checksum 的一起換（上游的 `sha256sums.txt` / `checksums.txt`；下載覆核一次再寫進去）。
3. 跑該列的守衛；守衛沒寫到的列，按「消費端」欄逐一 grep 確認。
4. 跨 major 的升級另開 PR，並在 PR 內附上游 release notes 的破壞性變更評估。

## 誰負責升：Renovate 還是手動

Renovate（repo 根目錄 `renovate.json`，由 `.github/workflows/renovate.yaml` 每週跑一次）只開 `custom.regex` 與三個內建 manager：`github-actions`、`dockerfile`、`devcontainer`（[#1354](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1354)）。每個 manager 各自一支 grouped PR；major 一律要在 Dependency Dashboard 打勾才開。

**判準**：一個 pin 的「另一側」若也是同一個 Renovate manager 會改到的 dep，交給 Renovate（同一支 grouped PR 一起動，守衛保持綠）；另一側**不是** Renovate dep，Renovate 只會升一半、守衛必紅，就在 `renovate.json` 明確 `enabled: false`，改由人按本頁對照表升。`tests/ops/test_renovate_config.py` 以案例逐一斷言下列歸屬。

| 交給 Renovate | 刻意保留手動（`renovate.json` 內 disabled） | 為什麼手動 |
|---|---|---|
| 每個 `uses:` 的 action 版本（同一 action 全部出現處同一支 PR） | `runs-on:`（github-runner）與 devcontainer base `image` | 兩者互綁（本表 Runner OS 列），分屬不同 manager、無法同 PR |
| | action `with:` 裡的工具版本（`node-version`、`python-version`、`go-version`、setup-helm `version`、trivy-action `version`、cosign `cosign-release`…） | 另一側是 devcontainer feature、Makefile、`install-*.sh` 或 lint wrapper 的 digest pin，都不是 Renovate dep |
| Dockerfile 的 `FROM` base image（`alpine`、`python`、`nginx` 等；python 的 minor 需 Dashboard 打勾） | `golang:*` builder | Go 的 SSOT 是 `go.mod`（本表 Go 列）；`test_go_toolchain_parity.py` 綁住 |
| | da-tools Dockerfile 的 `COPY --from=prom/alertmanager` | 已由 `custom.regex` 與部署的 Alertmanager 同 PR 升（#2294），不能有第二個 owner |
| forge-e2e 的 GitLab CE（`scripts/ops/forge_e2e_run.sh` 的 `GITLAB_CE_IMAGE` 預設值，tag＋digest；`custom.regex`，自己一支 group，major → Dashboard；[#2442](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2442)） | | |
| devcontainer feature 參照本身（`features/<name>:<major>`，皆為 major → Dashboard） | node / go / python feature 的 `version` | 與 CI `setup-*` 或 `go.mod` 互綁 |
| — | `sigstore/cosign-installer`、`anchore/sbom-action` | 仍會出現在 Dashboard，但每次都要打勾才開 PR（release.yaml：「bump deliberately」） |
| — | `tests/**`、`test/**` 下的一切（`ignorePaths`） | bench Dockerfile 與 federation-e2e compose 由各自的綁定測試守，不交給 Renovate |

⚠️ **這三個內建 manager 不做 digest／SHA pin**：全域 `pinDigests: true` 是給 #902 映像用的，對它們以 packageRule 關掉——否則第一次執行就會開一支把每個 `uses:` 改成 commit SHA、每個 `FROM` 加 `@sha256` 的 pin PR。要不要走 action SHA pin 是另一個決定，不是開 manager 的副作用。

⛔ **例外：第三方 action 一律 pin 到 commit SHA**（owner 拍板）。非 `actions/*`、`github/*` 的 action 寫成 `owner/repo@<40 位 SHA> # vX.Y.Z`，`renovate.json` 只對這份清單打開 `pinDigests`，由 Renovate 同時更新 SHA 與版本註解。理由：tag 可以被上游改指（tj-actions/changed-files，2025），而這些 action 跑在拿得到 `packages: write`／`id-token: write`（release.yaml）、`RENOVATE_TOKEN` 或 Docker Hub 帳密的 job 裡。清單與 workflow 的綁定由 `tests/ops/test_renovate_config.py` 的 `test_third_party_actions_are_sha_pinned_and_renovate_keeps_them_so` 守——新增第三方 action 時要同一個 PR 加進清單並 pin SHA。

## 對照表

| 工具 | SSOT | 消費端（升版時一起改） | 守衛 |
|---|---|---|---|
| Runner OS | 各 workflow job 的 `runs-on`（全部同值） | `.devcontainer/devcontainer.json` 的 base `image` tag | `tests/shared/test_toolchain_pin_parity.py`（runner） |
| Node.js | `.devcontainer/devcontainer.json` node feature `version`（major） | 每個 `actions/setup-node` 的 `node-version`（ci.yml portal、playwright.yml ×2、visual-baseline.yaml、commitlint.yaml、docs-ci.yaml）；Makefile `test-e2e` 說明 | 同上（node） |
| Python | `.devcontainer/devcontainer.json` python feature `version` | 每個 `actions/setup-python` 的 `python-version`（含 `${{ matrix.python }}` 展開後的值）；`requirements/ci-constraints.txt` 的版本是對這個 Python 解的 | 同上（python） |
| Go | 各 module 的 `go.mod` | Dockerfile builder、devcontainer go feature、CI `setup-go`（`go-version-file`）皆從它對齊 | 另一支 PR 補守衛 |
| golangci-lint | `.github/workflows/validate.yaml` `GOLANGCI_VERSION` | devcontainer go feature `golangciLintVersion`（⛔ 不帶 `v`） | `tests/ops/test_go_lint_module_coverage.py` |
| Trivy | `Makefile` `TRIVY_VERSION` | 每個 `aquasecurity/trivy-action` 步驟的 `version:`（release.yaml、component-docker-build.yaml、nightly-image-scan.yaml）；`.devcontainer/install-trivy.sh`（`TRIVY_VERSION` + `TRIVY_SHA256`） | `test_toolchain_pin_parity.py`（trivy） |
| Helm（全 repo） | devcontainer kubectl-helm-minikube feature `helm` | 每個 `azure/setup-helm` 的 `version:`（ci.yml Python Tests／Coverage／federation-e2e、release.yaml ×4） | `test_toolchain_pin_parity.py`（helm） |
| Helm（Container SAST job） | `scripts/tools/lint/check_iac_helm.py` `HELM_VERSION`（+ `HELM_DIGEST`） | ci.yml Container SAST job 的 `setup-helm` `version:` | 同上（「跑 `check_iac_helm.py` 的 job」由守衛自動辨識） |
| kubectl | devcontainer kubectl-helm-minikube feature `version`（⛔ 這個 feature 沒有 `kubectl` 選項） | 無 CI 消費端；跟著 kind 預設 node image 的 minor 走（±1 skew） | `test_toolchain_pin_parity.py`（kubectl、不得浮動） |
| kind | devcontainer `postCreateCommand` 的下載 URL 版本 + sha256 | `scripts/setup.sh` 等本地叢集流程；kind 版本決定預設 node image，kubectl 跟著調 | `test_toolchain_pin_parity.py`（kind） |
| Docker（dev container） | —（刻意 `latest`，理由見 devcontainer.json） | CI 端由 runner image 決定，兩邊都不釘 | 豁免登記在測試的 `_FLOATING_ALLOWED` |
| actionlint | `.pre-commit-config.yaml` `rev` | ci.yml `ACTIONLINT_VERSION`（兩個 job） | `tests/shared/test_actionlint_pin_parity.py` |
| ShellCheck | `.pre-commit-config.yaml` shellcheck-py `rev` | CI 以 `pre-commit run shellcheck` 呼叫同一個 hook，自動同版 | 升版需重量該 hook 註解中的 fail-closed 三道門 |
| hadolint | `scripts/tools/lint/check_iac_vibe_rules.py` `HADOLINT_VERSION`（+ digest） | ci.yml `HADOLINT_VERSION` + SHA-256 + cache key | `test_toolchain_pin_parity.py`（wrapper fallback） |
| kube-linter | `scripts/tools/lint/check_iac_helm.py` `KUBE_LINTER_VERSION`（+ digest） | ci.yml `KL` + `KL_SHA256` + cache key | 同上 |
| pint | `scripts/tools/lint/check_pint.py` `PINT_VERSION`（+ digest） | ci.yml pint 下載步驟 + cache key | `tests/lint/test_check_pint.py` |
| promtool（rule-pack gate） | `.devcontainer/install-promtool.sh` | ci.yml、nightly-vm-replay.yaml | `tests/preview/test_promtool_pin_parity.py` |
| promtool（docs-ci I-4） | docs-ci.yaml `PROMTOOL_VERSION` | 目前與 rule-pack gate 同版；major 須等於部署的 Prometheus image | `tests/ops/test_docs_ci_promtool_major.py`（只守 major） |
| amtool | 無自有 pin：docs-ci 與 da-tools image 都從 `k8s/03-monitoring/deployment-alertmanager.yaml` 的 alertmanager image 取出 | 與部署同一個 image（#2294） | `tests/ops/test_da_tools_amtool_pin_parity.py` |
| yq | docs-ci.yaml `YQ_VERSION` | 無其他消費端 | 無 |
| Vector | ci.yml `VECTOR_VERSION` | `.devcontainer/install-vector.sh`、`helm/vector` image tag / appVersion | `tests/shared/test_vector_pin_parity.py` |
| GitHub Actions | 無單一 SSOT：同一個 action 在全部 workflow 必須同一個 ref | `.github/workflows/**`（Renovate 的 github-actions manager 以單一 grouped PR 一起升，見上節） | `test_toolchain_pin_parity.py`（single version）、`tests/ops/test_renovate_config.py`（grouping） |
| Python 套件 | `requirements/ci-constraints.txt` | 每個 `pip install … -c`、devcontainer `postCreateCommand`、`.claude/hooks/session-start.sh` | `check_unpinned_deps`、`check_devcontainer_dep_parity.py` |

## 已知未統一（刻意保留，附理由）

- **Container SAST job 的 Helm 仍是 3.x**，其餘 CI 與 dev container 是 4.x。它必須與 `check_iac_helm.py` 的 Docker fallback 同版，而那個 fallback 是 image + digest 釘住的；要升，得連同 `HELM_VERSION`／`HELM_DIGEST` 一起重新解析 digest。守衛已把這一對綁住，升的時候不會只動一半。
- **hadolint、kube-linter、pint、golangci-lint 落後上游**：每一個都有「CI 二進位 + wrapper 的 digest-pinned fallback image」兩處要一起動，升版需要重新解析 image digest，另開 PR。
- **`download-artifact@v4`、`docker/*`、`dorny/paths-filter@v3`、`codeql-action@v3`、`configure-pages` / `deploy-pages` / `upload-pages-artifact`** 等各自已是單一版本，但上游已有更新的 major；跨 major 需逐一評估 release notes，不在「同 major 內統一」的範圍。
- **cosign／syft（release.yaml）**：安裝器 action 與二進位版本都刻意釘住（檔內註解：「bump deliberately」），簽章鏈不跟著一般升版走。
- **未 pin 的 npm 全域安裝**：commitlint.yaml 的 `@commitlint/*`、docs-ci.yaml 的 `@mermaid-js/mermaid-cli` / `puppeteer` 每次抓最新版——`check_unpinned_deps` 的 `BASELINE_ALLOWLIST` 明列這兩筆為已知例外（#1158 follow-up）。Node 釘到 24 之後，commitlint 最新版 `engines` 要求的 `>=22.12` 才第一次被滿足。
